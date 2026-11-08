from __future__ import annotations

import base64
import hashlib
import html
import http.cookiejar
import http.cookies
import json
import logging
import math
import random
import re
import socket
import ssl
import string
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

import certifi
import segno

from . import config
from .store import SettingsStore, utc_now_iso

log = logging.getLogger(__name__)

_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
_URL_RE = re.compile(r"https?://[^\s<>\"'，。；;]+", re.I)
_BARE_BV_RE = re.compile(r"(?<![0-9A-Za-z])(BV[0-9A-Za-z]{10})(?![0-9A-Za-z])", re.I)
_BARE_AV_RE = re.compile(r"(?<![0-9A-Za-z])av(\d{1,15})(?![0-9A-Za-z])", re.I)
_BV_IN_URL_RE = re.compile(r"(BV[0-9A-Za-z]{10})", re.I)
_ALLOWED_HOSTS = {
    "bilibili.com",
    "www.bilibili.com",
    "m.bilibili.com",
    "b23.tv",
    "www.b23.tv",
    "bili2233.cn",
    "www.bili2233.cn",
}
_SHORT_HOSTS = {"b23.tv", "www.b23.tv", "bili2233.cn", "www.bili2233.cn"}
_IMAGE_HOST_SUFFIXES = ("hdslb.com", "biliimg.com", "bilivideo.com")


class BiliError(Exception):
    def __init__(self, message: str, code: str = "bili_error", status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


class _YdlLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def debug(self, message: str) -> None:
        pass

    def info(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        text = str(message).strip()
        if text:
            self.warnings.append(text)

    def error(self, message: str) -> None:
        text = str(message).strip()
        if text:
            self.errors.append(text)


@dataclass
class ResolvedInput:
    canonical_url: str
    bvid: str | None
    aid: int | None
    page: int
    original_url: str


def _host_allowed(host: str) -> bool:
    host = (host or "").lower().split(":")[0]
    if host in _ALLOWED_HOSTS:
        return True
    return host.endswith(".bilibili.com") or host.endswith(".b23.tv")


def is_safe_image_url(url: str) -> bool:
    try:
        parsed = urlparse.urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    return any(host == suffix or host.endswith("." + suffix) for suffix in _IMAGE_HOST_SUFFIXES)


def _clean_html_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _perfect_int(value: Any) -> int:
    try:
        number = int(float(value or 0))
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _codec_family(codec: Any) -> str:
    value = str(codec or "").lower()
    if value.startswith("avc") or value.startswith("h264"):
        return "avc"
    if value.startswith("hev") or value.startswith("hvc") or value.startswith("h265"):
        return "hevc"
    if value.startswith("av01") or value.startswith("av1"):
        return "av1"
    if value.startswith("vp9"):
        return "vp9"
    if value.startswith("mp4a") or value.startswith("aac"):
        return "aac"
    return "other"


def _codec_label(family: str) -> str:
    return {
        "avc": "H.264",
        "hevc": "HEVC",
        "av1": "AV1",
        "vp9": "VP9",
        "aac": "AAC",
    }.get(family, "未知编码")


def _fps_value(fmt: dict) -> float:
    try:
        value = float(fmt.get("fps") or 0)
    except (TypeError, ValueError):
        return 0.0
    return round(value, 2)


def _is_hdr(fmt: dict) -> bool:
    dynamic_range = str(fmt.get("dynamic_range") or "").upper()
    note = str(fmt.get("format_note") or "").upper()
    return dynamic_range not in {"", "SDR"} or any(
        token in note for token in ("HDR", "杜比视界", "DOLBY VISION")
    )


def _quality_title(height: int, fps: float, hdr: bool) -> tuple[str, str]:
    if height >= 4320:
        base = "8K"
        desc = "8K 超清"
    elif height >= 2160:
        base = "4K"
        desc = "4K 超清"
    elif height >= 1440:
        base = "2K"
        desc = "2K 超清"
    elif height >= 1080:
        base = "1080P"
        desc = "1080P 高清"
    elif height >= 720:
        base = "720P"
        desc = "720P 高清"
    elif height >= 480:
        base = "480P"
        desc = "480P 标清"
    elif height > 0:
        base = f"{height}P"
        desc = f"{height}P 流畅"
    else:
        base = "原画"
        desc = "原画"
    if fps >= 48:
        base += "60"
        desc += " · 60帧"
    if hdr:
        desc += " · HDR"
    return base, desc


class BiliClient:
    def __init__(self, settings: SettingsStore) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        self._cookies: dict[str, str] = {}
        self._account: dict[str, Any] = {"is_login": False}
        self._parse_cache: dict[str, dict[str, Any]] = {}
        self._qr_sessions: dict[str, float] = {}
        self._auth_jar = http.cookiejar.CookieJar()
        self._auth_opener = urlrequest.build_opener(
            urlrequest.HTTPCookieProcessor(self._auth_jar),
            urlrequest.HTTPSHandler(context=_SSL_CONTEXT),
        )
        self._cookie_revision = 0
        self._wbi_key = ""
        self._wbi_key_ts = 0.0
        self._load_state()
        self._clean_parse_cache()

    # ------------------------------------------------------------------
    # Persistence / account
    # ------------------------------------------------------------------
    def _load_state(self) -> None:
        cookies = config.read_json(config.COOKIES_FILE, {})
        if isinstance(cookies, dict):
            self._cookies = {
                str(k): str(v)
                for k, v in cookies.items()
                if isinstance(k, str) and isinstance(v, (str, int, float))
            }
        account = config.read_json(config.ACCOUNT_FILE, {})
        if isinstance(account, dict) and account.get("is_login"):
            self._account = account
        else:
            self._account = {"is_login": False}

    def _save_state(self) -> None:
        config.atomic_write_json(config.COOKIES_FILE, self._cookies)
        config.atomic_write_json(config.ACCOUNT_FILE, self._account)
        self._write_netscape_cookies()

    def _write_netscape_cookies(self) -> None:
        if not self._cookies:
            try:
                config.COOKIES_NETSCAPE_FILE.unlink(missing_ok=True)
            except OSError:
                pass
            return
        expires = int(time.time()) + 60 * 60 * 24 * 30
        lines = [
            "# Netscape HTTP Cookie File",
            "# Generated by HWZ Bili Downloader. Do not edit while the app is running.",
        ]
        for name, value in self._cookies.items():
            if any(ch in name for ch in "\r\n\t"):
                continue
            safe_value = str(value).replace("\r", "").replace("\n", "")
            lines.append(f".bilibili.com\tTRUE\t/\tFALSE\t{expires}\t{name}\t{safe_value}")
        try:
            config.COOKIES_NETSCAPE_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            log.exception("Could not write Netscape cookies file")

    def reset_for_tests(self) -> None:
        with self._lock:
            self._cookies = {}
            self._account = {"is_login": False}
            self._parse_cache.clear()
            self._cookie_revision += 1
            for path in (config.COOKIES_FILE, config.ACCOUNT_FILE, config.COOKIES_NETSCAPE_FILE):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    @property
    def is_logged_in(self) -> bool:
        with self._lock:
            return bool(self._cookies and self._account.get("is_login"))

    def account_public(self) -> dict:
        with self._lock:
            data = dict(self._account)
        if not data.get("is_login"):
            return {"is_login": False}
        return {
            "is_login": True,
            "mid": data.get("mid"),
            "uname": data.get("uname") or "B站用户",
            "face": data.get("face") or "",
            "vip": bool(data.get("vip")),
            "vip_label": data.get("vip_label") or "",
            "level": data.get("level") or 0,
        }

    def cookie_header(self) -> str:
        with self._lock:
            return "; ".join(f"{k}={v}" for k, v in self._cookies.items())

    def cookie_file(self) -> Path | None:
        if not self._cookies:
            return None
        try:
            if not config.COOKIES_NETSCAPE_FILE.exists():
                self._write_netscape_cookies()
            return config.COOKIES_NETSCAPE_FILE
        except OSError:
            return None

    def _headers(self, referer: str = "https://www.bilibili.com/") -> dict[str, str]:
        headers = {
            "User-Agent": config.USER_AGENT,
            "Referer": referer,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": "https://www.bilibili.com",
        }
        cookie = self.cookie_header()
        if cookie:
            headers["Cookie"] = cookie
        return headers

    def _open(self, request: urlrequest.Request, timeout: float = 20) -> Any:
        return urlrequest.urlopen(request, timeout=timeout, context=_SSL_CONTEXT)

    def _get_json(
        self,
        url: str,
        referer: str = "https://www.bilibili.com/",
        timeout: float = 20,
        opener: Any | None = None,
    ) -> dict:
        request = urlrequest.Request(url, headers=self._headers(referer))
        try:
            if opener is None:
                response = self._open(request, timeout=timeout)
            else:
                response = opener.open(request, timeout=timeout)
            with response:
                raw = response.read()
        except urlerror.HTTPError as exc:
            if exc.code == 412:
                raise BiliError(
                    "B站拒绝了本次请求，请稍后重试，或登录账号后再试。",
                    code="risk_control",
                    status=429,
                ) from exc
            if exc.code == 403:
                raise BiliError(
                    "B站拒绝了本次请求（403），请稍后再试。", code="forbidden", status=403
                ) from exc
            raise BiliError(
                f"网络请求失败（HTTP {exc.code}），请稍后重试。", code="http_error", status=502
            ) from exc
        except (urlerror.URLError, TimeoutError, socket.timeout, ssl.SSLError) as exc:
            raise BiliError("无法连接到 B 站，请检查网络或稍后重试。", code="network_error", status=502) from exc
        try:
            return json.loads(raw.decode("utf-8", errors="replace"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BiliError("B站返回了无法识别的数据，请稍后重试。", code="bad_response", status=502) from exc

    def refresh_account(self, save: bool = True) -> dict:
        if not self._cookies:
            with self._lock:
                self._account = {"is_login": False}
            return self.account_public()
        try:
            data = self._get_json("https://api.bilibili.com/x/web-interface/nav")
        except BiliError:
            return self.account_public()
        payload = data.get("data") if isinstance(data, dict) else {}
        if data.get("code") != 0 or not isinstance(payload, dict) or not payload.get("isLogin"):
            account = {"is_login": False}
            with self._lock:
                self._account = account
            if save:
                try:
                    config.atomic_write_json(config.ACCOUNT_FILE, account)
                except OSError:
                    pass
            return self.account_public()
        vip = payload.get("vipStatus") == 1 or bool((payload.get("vip") or {}).get("status"))
        vip_label = ""
        if isinstance(payload.get("vip"), dict):
            vip_label = str((payload["vip"].get("label") or {}).get("text") or "")
        account = {
            "is_login": True,
            "mid": payload.get("mid"),
            "uname": _clean_html_text(payload.get("uname")),
            "face": str(payload.get("face") or ""),
            "vip": vip,
            "vip_label": vip_label,
            "level": payload.get("level") or 0,
            "updated_at": utc_now_iso(),
        }
        with self._lock:
            self._account = account
        if save:
            try:
                config.atomic_write_json(config.ACCOUNT_FILE, account)
            except OSError:
                log.exception("Could not save account")
        return self.account_public()

    def logout(self) -> dict:
        with self._lock:
            self._cookies = {}
            self._account = {"is_login": False}
            self._parse_cache.clear()
            self._cookie_revision += 1
            self._auth_jar.clear()
            self._auth_opener = urlrequest.build_opener(
                urlrequest.HTTPCookieProcessor(self._auth_jar),
                urlrequest.HTTPSHandler(context=_SSL_CONTEXT),
            )
            for path in (config.COOKIES_FILE, config.ACCOUNT_FILE, config.COOKIES_NETSCAPE_FILE):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        return self.account_public()

    # ------------------------------------------------------------------
    # QR login
    # ------------------------------------------------------------------
    def generate_login_qr(self) -> dict:
        # A fresh jar keeps stale cookies from a previous login attempt from
        # interfering with this QR session.
        with self._lock:
            self._auth_jar.clear()
            self._auth_opener = urlrequest.build_opener(
                urlrequest.HTTPCookieProcessor(self._auth_jar),
                urlrequest.HTTPSHandler(context=_SSL_CONTEXT),
            )
        data = self._get_json(
            "https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
            referer="https://www.bilibili.com/",
            opener=self._auth_opener,
        )
        if data.get("code") != 0:
            raise BiliError("二维码生成失败，请稍后再试。", code="qr_generate_failed", status=502)
        payload = data.get("data") or {}
        qr_url = str(payload.get("url") or "")
        qr_key = str(payload.get("qrcode_key") or "")
        if not qr_url or not qr_key:
            raise BiliError("二维码数据异常，请稍后再试。", code="qr_generate_failed", status=502)
        with self._lock:
            self._qr_sessions[qr_key] = time.time() + 180
            if len(self._qr_sessions) > 12:
                now = time.time()
                self._qr_sessions = {key: value for key, value in self._qr_sessions.items() if value > now}
        qr = segno.make(qr_url, error="m", boost_error=False)
        svg = qr.svg_inline(scale=8, border=3, dark="#14172a", light="#ffffff", omitsize=True)
        return {"key": qr_key, "svg": svg, "expires_in": 180}

    def poll_login_qr(self, qr_key: str) -> dict:
        qr_key = str(qr_key or "").strip()
        if not qr_key:
            raise BiliError("登录会话无效，请刷新二维码。", code="qr_invalid")
        with self._lock:
            expires = self._qr_sessions.get(qr_key)
        if not expires or expires < time.time():
            return {"state": "expired", "message": "二维码已过期，请刷新。"}
        url = (
            "https://passport.bilibili.com/x/passport-login/web/qrcode/poll?"
            + urlparse.urlencode({"qrcode_key": qr_key, "source": "web"})
        )
        request = urlrequest.Request(url, headers=self._headers())
        try:
            response = self._auth_opener.open(request, timeout=20)
        except urlerror.HTTPError as exc:
            raise BiliError("登录状态查询失败，请稍后再试。", code="qr_poll_failed", status=502) from exc
        except (urlerror.URLError, TimeoutError, socket.timeout) as exc:
            raise BiliError("登录状态查询失败，请检查网络。", code="qr_poll_failed", status=502) from exc
        with response:
            raw_headers = response.headers
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        if payload.get("code") != 0:
            raise BiliError("登录状态查询失败，请刷新二维码。", code="qr_poll_failed", status=502)
        data = payload.get("data") or {}
        status = data.get("code")
        if status == 0:
            with self._lock:
                cookies: dict[str, str] = {}
                for cookie in self._auth_jar:
                    if cookie.value and _host_allowed(cookie.domain.lstrip(".")):
                        cookies[cookie.name] = cookie.value
                for header in raw_headers.get_all("Set-Cookie") or []:
                    simple = http.cookies.SimpleCookie()
                    try:
                        simple.load(header)
                    except Exception:
                        continue
                    for name, morsel in simple.items():
                        if morsel.value:
                            cookies[name] = morsel.value
                self._cookies = cookies
                self._qr_sessions.pop(qr_key, None)
                self._parse_cache.clear()
                self._cookie_revision += 1
            self._write_netscape_cookies()
            try:
                config.atomic_write_json(config.COOKIES_FILE, self._cookies)
            except OSError:
                log.exception("Could not save login cookies")
            account = self.refresh_account()
            return {"state": "success", "message": "登录成功", "account": account}
        if status == 86101:
            return {"state": "pending", "message": "等待扫码"}
        if status == 86090:
            return {"state": "scanned", "message": "已扫码，请在手机上确认"}
        if status == 86038:
            return {"state": "expired", "message": "二维码已过期，请刷新"}
        return {"state": "pending", "message": "等待确认"}

    # ------------------------------------------------------------------
    # URL / metadata
    # ------------------------------------------------------------------
    def _resolve_short_url(self, short_url: str) -> str:
        request = urlrequest.Request(
            short_url,
            headers={
                "User-Agent": config.USER_AGENT,
                "Referer": "https://www.bilibili.com/",
                "Range": "bytes=0-0",
            },
        )
        try:
            with self._open(request, timeout=15) as response:
                return response.geturl()
        except urlerror.HTTPError as exc:
            final = exc.geturl()
            if final and final != short_url:
                return final
            raise BiliError("短链接解析失败，请尝试粘贴完整的视频链接。", code="short_url_failed") from exc
        except (urlerror.URLError, TimeoutError, socket.timeout, ssl.SSLError) as exc:
            raise BiliError("短链接解析失败，请检查网络后重试。", code="short_url_failed", status=502) from exc

    def resolve_input(self, raw_text: str, page_hint: Any = None) -> ResolvedInput:
        text = str(raw_text or "").strip()
        if not text:
            raise BiliError("请先粘贴 B 站视频链接。", code="empty_url")
        candidate = ""
        match = _URL_RE.search(text)
        if match:
            candidate = match.group(0).rstrip(".,;:!?)]}'\",，。；：！？）】》")
        else:
            bv = _BARE_BV_RE.search(text)
            av = _BARE_AV_RE.search(text)
            if bv:
                candidate = "https://www.bilibili.com/video/" + bv.group(1)
            elif av:
                candidate = "https://www.bilibili.com/video/av" + av.group(1)
            else:
                raise BiliError("没有识别到 B 站链接，请粘贴 BV / av / b23.tv 链接。", code="invalid_url")
        if not candidate.lower().startswith(("http://", "https://")):
            candidate = "https://" + candidate.lstrip("/")
        try:
            parsed = urlparse.urlsplit(candidate)
        except ValueError as exc:
            raise BiliError("链接格式不正确，请重新复制。", code="invalid_url") from exc
        host = (parsed.hostname or "").lower()
        if not host or not _host_allowed(host):
            raise BiliError("目前只支持 B 站视频链接（bilibili.com / b23.tv）。", code="unsupported_host")
        if host in _SHORT_HOSTS:
            candidate = self._resolve_short_url(candidate)
            try:
                parsed = urlparse.urlsplit(candidate)
            except ValueError as exc:
                raise BiliError("短链接跳转失败，请尝试完整链接。", code="short_url_failed") from exc
            host = (parsed.hostname or "").lower()
        if not host.endswith("bilibili.com"):
            raise BiliError("短链接没有指向 B 站视频页面。", code="unsupported_host")

        bvid_match = _BV_IN_URL_RE.search(parsed.path) or _BARE_BV_RE.search(text)
        av_match = re.search(r"/video/av(\d+)", parsed.path, re.I) or _BARE_AV_RE.search(text)
        bvid = bvid_match.group(1) if bvid_match else None
        aid = int(av_match.group(1)) if av_match else None
        if not bvid and not aid:
            raise BiliError(
                "暂时只支持 B 站普通视频（BV / av 链接）。番剧和直播请使用视频投稿链接。",
                code="unsupported_type",
            )
        query = urlparse.parse_qs(parsed.query)
        page = 1
        try:
            if page_hint is not None:
                page = max(1, int(page_hint))
            elif query.get("p"):
                page = max(1, int(query["p"][0]))
        except (TypeError, ValueError):
            page = 1
        canonical = (
            f"https://www.bilibili.com/video/{bvid}"
            if bvid
            else f"https://www.bilibili.com/video/av{aid}"
        )
        return ResolvedInput(canonical, bvid, aid, page, candidate)

    def _fetch_view(self, bvid: str | None, aid: int | None) -> dict:
        if bvid:
            url = "https://api.bilibili.com/x/web-interface/view?" + urlparse.urlencode({"bvid": bvid})
        else:
            url = "https://api.bilibili.com/x/web-interface/view?" + urlparse.urlencode({"aid": aid})
        data = self._get_json(url)
        code = data.get("code")
        if code != 0:
            message = str(data.get("message") or "")
            if code in {-400, -404, 400, 404, 62002, 62004}:
                raise BiliError(
                    "视频不存在或链接无效，请检查 BV / av 号是否正确。",
                    code="video_missing",
                    status=404,
                )
            if code in {-403, -10403}:
                raise BiliError("该视频有地区限制或需要登录，请登录后再试。", code="access_denied", status=403)
            if "风控" in message or code == -412:
                raise BiliError("B站风控校验失败，请稍后重试或登录账号。", code="risk_control", status=429)
            raise BiliError(f"视频信息获取失败：{message or '未知错误'}", code="view_failed", status=502)
        payload = data.get("data")
        if not isinstance(payload, dict) or not payload.get("title"):
            raise BiliError("没有获取到视频信息，请确认链接有效。", code="view_failed", status=502)
        return payload

    # ------------------------------------------------------------------
    # Play URL (WBI-signed, includes CDN backup URLs)
    # ------------------------------------------------------------------
    _MIXIN_KEY_ENC_TAB = [
        46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
        33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
        61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
        36, 20, 34, 44, 52,
    ]

    def _get_wbi_key(self) -> str:
        with self._lock:
            if self._wbi_key and time.time() < self._wbi_key_ts + 28:
                return self._wbi_key
        data = self._get_json("https://api.bilibili.com/x/web-interface/nav")
        payload = data.get("data") or {}
        wbi = payload.get("wbi_img") or {}
        img_url = str(wbi.get("img_url") or "")
        sub_url = str(wbi.get("sub_url") or "")
        lookup = "".join(
            part.rpartition("/")[2].partition(".")[0] for part in (img_url, sub_url)
        )
        if len(lookup) < 64:
            raise BiliError("B站签名数据获取失败，请稍后重试。", code="wbi_failed", status=502)
        key = "".join(lookup[index] for index in self._MIXIN_KEY_ENC_TAB)[:32]
        with self._lock:
            self._wbi_key = key
            self._wbi_key_ts = time.time()
        return key

    def _sign_wbi(self, params: dict) -> dict:
        key = self._get_wbi_key()
        signed = {str(k): str(v) for k, v in params.items()}
        signed["wts"] = str(round(time.time()))
        translation = str.maketrans("", "", "!'()*")
        signed = {k: v.translate(translation) for k, v in signed.items()}
        query = urlparse.urlencode(sorted(signed.items()))
        signed["w_rid"] = hashlib.md5((query + key).encode("utf-8")).hexdigest()
        return signed

    @staticmethod
    def _dm_params() -> dict[str, str]:
        def get_wh(width: int = 1920, height: int = 1080) -> list[int]:
            rnd = math.floor(114 * random.random())
            return [2 * width + 2 * height + 3 * rnd, 4 * width - height + rnd, rnd]

        def get_of(scroll_top: int = 10, scroll_left: int = 10) -> list[int]:
            rnd = math.floor(514 * random.random())
            return [
                3 * scroll_top + 2 * scroll_left + rnd,
                4 * scroll_top - 4 * scroll_left + 2 * rnd,
                rnd,
            ]

        return {
            "dm_img_list": "[]",
            "dm_img_str": base64.b64encode(
                "".join(random.choices(string.printable, k=random.randint(16, 64))).encode()
            )[:-2].decode(),
            "dm_cover_img_str": base64.b64encode(
                "".join(random.choices(string.printable, k=random.randint(32, 128))).encode()
            )[:-2].decode(),
            "dm_img_inter": json.dumps(
                {
                    "ds": [],
                    "wh": get_wh(),
                    "of": get_of(random.randint(0, 100), 0),
                },
                separators=(",", ":"),
            ),
        }

    def _fetch_play_info(self, bvid: str, cid: Any) -> dict:
        if not cid:
            raise BiliError("缺少视频 CID，无法解析清晰度。", code="cid_missing", status=400)
        params = {
            "bvid": bvid,
            "cid": str(cid),
            "fnval": "4048",
            "fnver": "0",
            "fourk": "1",
            "qn": "127",
            **self._dm_params(),
        }
        if not self.is_logged_in:
            params["try_look"] = "1"
        signed = self._sign_wbi(params)
        url = "https://api.bilibili.com/x/player/wbi/playurl?" + urlparse.urlencode(signed)
        data = self._get_json(
            url,
            referer=f"https://www.bilibili.com/video/{bvid}",
        )
        code = data.get("code")
        if code != 0:
            message = str(data.get("message") or "")
            if code in {-401, -352, 401, 352}:
                raise BiliError("B站风控校验失败，请稍后重试或扫码登录。", code="risk_control", status=429)
            if code in {-404, 404}:
                raise BiliError("视频流不存在或已失效。", code="playurl_missing", status=404)
            raise BiliError(
                f"视频清晰度获取失败：{message or '未知错误'}", code="playurl_failed", status=502
            )
        payload = data.get("data")
        if not isinstance(payload, dict):
            raise BiliError("视频清晰度数据异常，请稍后重试。", code="playurl_failed", status=502)
        return payload

    @staticmethod
    def _parse_rate(value: Any) -> float:
        try:
            text = str(value or "0")
            if "/" in text:
                left, right = text.split("/", 1)
                denominator = float(right)
                return round(float(left) / denominator, 2) if denominator else 0.0
            return round(float(text), 2)
        except (TypeError, ValueError, ZeroDivisionError):
            return 0.0

    def _formats_from_play_info(
        self, play_info: dict, bvid: str, page: int, duration: float
    ) -> list[dict]:
        dash = play_info.get("dash") or {}
        if not isinstance(dash, dict):
            return []
        support = {
            int(item.get("quality") or 0): str(
                item.get("new_description") or item.get("display_desc") or ""
            )
            for item in (play_info.get("support_formats") or [])
            if isinstance(item, dict)
        }
        referer = f"https://www.bilibili.com/video/{bvid}?p={page}"
        headers = {
            "User-Agent": config.USER_AGENT,
            "Referer": referer,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        formats: list[dict] = []

        def collect(item: dict, kind: str, abr_default: float = 0) -> None:
            if not isinstance(item, dict):
                return
            url = str(item.get("baseUrl") or item.get("base_url") or item.get("url") or "")
            if not url:
                return
            backups = item.get("backupUrl") or item.get("backup_url") or []
            if isinstance(backups, str):
                backups = [backups]
            backup_urls = []
            for backup in backups:
                backup = str(backup or "")
                if backup and backup != url and backup not in backup_urls:
                    backup_urls.append(backup)
            bandwidth = 0
            try:
                bandwidth = int(float(item.get("bandwidth") or 0))
            except (TypeError, ValueError):
                bandwidth = 0
            quality_id = _perfect_int(item.get("id"))
            try:
                size = int(item.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            mime = str(item.get("mimeType") or item.get("mime_type") or "")
            codecs = str(item.get("codecs") or "")
            if kind == "video":
                ext = "mp4" if "mp4" in mime else ("webm" if "webm" in mime else "mp4")
                width = _perfect_int(item.get("width"))
                height = _perfect_int(item.get("height"))
                fps = self._parse_rate(item.get("frameRate") or item.get("frame_rate"))
                quality_name = support.get(quality_id, "")
                dynamic = str(item.get("dynamic_range") or "").upper()
                if not dynamic and quality_id in {125, 126}:
                    dynamic = "HDR10" if quality_id == 125 else "DV"
                formats.append(
                    {
                        "url": url,
                        "backup_urls": backup_urls,
                        "format_id": str(quality_id or item.get("codecid") or ""),
                        "ext": ext,
                        "protocol": "https",
                        "vcodec": codecs,
                        "acodec": "none",
                        "width": width,
                        "height": height,
                        "fps": fps,
                        "tbr": round(bandwidth / 1000, 3) if bandwidth else 0,
                        "filesize": size,
                        "filesize_approx": int(bandwidth * duration / 8) if bandwidth and duration else 0,
                        "quality": quality_id,
                        "format": quality_name,
                        "format_note": quality_name,
                        "dynamic_range": dynamic or "SDR",
                        "http_headers": headers,
                    }
                )
            else:
                ext = "m4a" if "mp4" in mime else ("flac" if "flac" in mime else "m4a")
                bitrate = round(bandwidth / 1000, 3) if bandwidth else abr_default
                formats.append(
                    {
                        "url": url,
                        "backup_urls": backup_urls,
                        "format_id": str(quality_id or ""),
                        "ext": ext,
                        "protocol": "https",
                        "vcodec": "none",
                        "acodec": codecs or "mp4a.40.2",
                        "tbr": bitrate,
                        "abr": bitrate,
                        "filesize": size,
                        "filesize_approx": int(bandwidth * duration / 8) if bandwidth and duration else 0,
                        "http_headers": headers,
                    }
                )

        videos = dash.get("video") or []
        if isinstance(videos, list):
            for item in videos:
                collect(item, "video")
        audios = dash.get("audio") or []
        if isinstance(audios, list):
            for item in audios:
                collect(item, "audio", abr_default=128)
        # Some premium videos only expose their audio via dolby/flac.
        if not audios:
            dolby = (dash.get("dolby") or {}).get("audio") if isinstance(dash.get("dolby"), dict) else None
            if isinstance(dolby, list):
                for item in dolby:
                    collect(item, "audio", abr_default=256)
        return formats

    def _extract_formats(self, bvid: str, cid: Any, page: int, duration: float) -> tuple[dict, list[str]]:
        warnings: list[str] = []
        try:
            play_info = self._fetch_play_info(bvid, cid)
            formats = self._formats_from_play_info(play_info, bvid, page, duration)
            if formats:
                return {"formats": formats, "duration": duration}, warnings
            warnings.append("直接播放接口没有返回 DASH 流，已切换备用解析引擎。")
        except BiliError as exc:
            log.warning("Direct playurl failed for %s: %s", bvid, exc.message)
            warnings.append(exc.message)
        return self._extract_formats_ytdlp(bvid, page, duration)

    def _extract_formats_ytdlp(self, bvid: str, page: int, duration: float) -> tuple[dict, list[str]]:
        try:
            import yt_dlp
        except ImportError as exc:
            raise BiliError("下载引擎加载失败，请重新安装应用。", code="engine_missing", status=500) from exc

        logger = _YdlLogger()
        options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "noprogress": True,
            "no_color": True,
            "socket_timeout": 25,
            "retries": 2,
            "extractor_retries": 2,
            "cachedir": str(config.YTDLP_CACHE_DIR),
            "http_headers": {
                "User-Agent": config.USER_AGENT,
                "Referer": f"https://www.bilibili.com/video/{bvid}",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            "logger": logger,
        }
        cookie_file = self.cookie_file()
        if cookie_file:
            options["cookiefile"] = str(cookie_file)
        url = f"https://www.bilibili.com/video/{bvid}?p={max(1, int(page))}"
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:
            text = str(exc)
            lower = text.lower()
            log.warning("yt-dlp extraction failed for %s p%s: %s", bvid, page, text)
            if "premium" in lower or "大会员" in text:
                raise BiliError(
                    "该清晰度需要大会员，请先登录有权限的 B 站账号。",
                    code="premium_required",
                    status=403,
                ) from exc
            if "login" in lower and "cookie" in lower:
                raise BiliError(
                    "该视频需要登录后才能解析，请扫码登录后再试。",
                    code="login_required",
                    status=401,
                ) from exc
            if "412" in text or "risk" in lower:
                raise BiliError("B站风控拦截了本次解析，请稍后重试或登录账号。", code="risk_control", status=429) from exc
            if "unable to download" in lower or "timed out" in lower or "network" in lower:
                raise BiliError("解析超时，请检查网络后重试。", code="network_error", status=502) from exc
            raise BiliError(
                "视频清晰度解析失败，请稍后重试或换一个链接。",
                code="extract_failed",
                status=502,
            ) from exc

        if not isinstance(info, dict):
            raise BiliError("没有解析到视频流，请稍后再试。", code="extract_failed", status=502)
        if info.get("_type") == "playlist":
            entries = list(info.get("entries") or [])
            if not entries:
                raise BiliError("没有解析到可下载的分P视频。", code="extract_failed", status=502)
            selected = None
            for entry in entries:
                try:
                    if int(entry.get("playlist_index") or 0) == int(page):
                        selected = entry
                        break
                except (TypeError, ValueError):
                    continue
            info = selected or entries[0]
        formats = list(info.get("formats") or [])
        if not formats:
            raise BiliError(
                "没有找到可用的视频流，可能是付费、地区限制或直播内容。",
                code="formats_missing",
                status=403,
            )
        if info.get("_has_drm") or info.get("is_live"):
            raise BiliError("该内容受版权保护或为直播，暂不支持下载。", code="protected", status=403)
        return info, logger.warnings

    def _build_qualities(
        self,
        info: dict,
        duration: float,
        preferred_codec: str,
    ) -> tuple[list[dict], dict | None, dict | None]:
        formats = [
            item for item in (info.get("formats") or []) if isinstance(item, dict) and item.get("url")
        ]
        audio_formats = [
            item
            for item in formats
            if str(item.get("acodec") or "none") != "none"
            and str(item.get("vcodec") or "none") == "none"
        ]
        video_formats = [
            item
            for item in formats
            if str(item.get("vcodec") or "none") != "none"
            and str(item.get("acodec") or "none") == "none"
        ]
        combined_formats = [
            item
            for item in formats
            if str(item.get("vcodec") or "none") != "none"
            and str(item.get("acodec") or "none") != "none"
        ]
        audio_formats.sort(key=lambda item: float(item.get("abr") or item.get("tbr") or 0), reverse=True)
        best_audio = audio_formats[0] if audio_formats else None
        audio_public = self._audio_public(best_audio, duration) if best_audio else None
        if not video_formats and combined_formats:
            video_formats = combined_formats
        if not video_formats:
            raise BiliError("没有找到可下载的视频画面流。", code="formats_missing", status=403)

        groups: dict[tuple, list[dict]] = {}
        for item in video_formats:
            height = int(item.get("height") or 0)
            if height <= 0:
                continue
            fps = _fps_value(item)
            fps_bucket = 60 if fps >= 48 else (30 if fps >= 28 else 24)
            key = (height, fps_bucket, _is_hdr(item))
            groups.setdefault(key, []).append(item)
        if not groups:
            raise BiliError("没有找到有效的视频清晰度。", code="formats_missing", status=403)

        preference_order = {
            "avc": ["avc", "hevc", "av1", "vp9", "other"],
            "hevc": ["hevc", "avc", "av1", "vp9", "other"],
            "av1": ["av1", "hevc", "avc", "vp9", "other"],
        }.get(preferred_codec, ["avc", "hevc", "av1", "vp9", "other"])

        quality_items: list[dict] = []
        for (height, fps_bucket, hdr), candidates in groups.items():
            def codec_rank(item: dict) -> tuple[int, float]:
                family = _codec_family(item.get("vcodec"))
                try:
                    index = preference_order.index(family)
                except ValueError:
                    index = len(preference_order)
                return index, -float(item.get("tbr") or 0)

            chosen = sorted(candidates, key=codec_rank)[0]
            fps = _fps_value(chosen)
            family = _codec_family(chosen.get("vcodec"))
            short_label, label = _quality_title(height, fps, hdr)
            video_size = self._estimate_size(chosen, duration)
            combined = str(chosen.get("acodec") or "none") != "none"
            if combined:
                paired_audio = None
                total_size = video_size
            else:
                paired_audio = best_audio
                total_size = video_size + (
                    self._estimate_size(best_audio, duration) if best_audio else 0
                )
            quality_id = f"q{height}-{fps_bucket}-{family}" + ("-hdr" if hdr else "")
            quality_items.append(
                {
                    "id": quality_id,
                    "label": label,
                    "short_label": short_label,
                    "height": height,
                    "width": int(chosen.get("width") or 0),
                    "fps": int(round(fps)) if fps else 0,
                    "hdr": hdr,
                    "codec": _codec_label(family),
                    "codec_family": family,
                    "size": int(total_size or 0),
                    "note": str(chosen.get("format_note") or "").strip(),
                    "_video": self._trim_format(chosen),
                    "_audio": self._trim_format(paired_audio) if paired_audio else None,
                    "_combined": combined,
                }
            )
        quality_items.sort(key=lambda q: (q["height"], q["fps"], not q["hdr"]), reverse=True)
        if quality_items:
            quality_items[0]["best"] = True
        preferred = None
        for item in quality_items:
            if item["height"] == 1080 and not item["hdr"]:
                if preferred is None or item["fps"] > preferred["fps"]:
                    preferred = item
        if preferred is None:
            preferred = next(
                (item for item in quality_items if item["height"] <= 1080), quality_items[0]
            )
        for item in quality_items:
            item["default"] = item["id"] == preferred["id"]
        return quality_items, audio_public, self._trim_format(best_audio)

    @staticmethod
    def _trim_format(fmt: dict | None) -> dict | None:
        if not fmt:
            return None
        keys = (
            "url",
            "backup_urls",
            "format_id",
            "ext",
            "protocol",
            "vcodec",
            "acodec",
            "width",
            "height",
            "fps",
            "tbr",
            "abr",
            "filesize",
            "filesize_approx",
            "http_headers",
            "dynamic_range",
            "format_note",
        )
        return {key: fmt.get(key) for key in keys if key in fmt}

    @staticmethod
    def _estimate_size(fmt: dict | None, duration: float) -> int:
        if not fmt:
            return 0
        for key in ("filesize", "filesize_approx"):
            value = fmt.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return int(value)
        bitrate = fmt.get("tbr") or fmt.get("abr") or 0
        try:
            bitrate = float(bitrate)
        except (TypeError, ValueError):
            bitrate = 0
        if bitrate > 0 and duration > 0:
            return int(bitrate * 1000 / 8 * duration)
        return 0

    @staticmethod
    def _audio_public(fmt: dict | None, duration: float) -> dict | None:
        if not fmt:
            return None
        try:
            abr = int(round(float(fmt.get("abr") or fmt.get("tbr") or 0)))
        except (TypeError, ValueError):
            abr = 0
        return {
            "abr": abr,
            "codec": _codec_label(_codec_family(fmt.get("acodec"))),
            "size": BiliClient._estimate_size(fmt, duration),
            "ext": str(fmt.get("ext") or "m4a"),
            "format_id": str(fmt.get("format_id") or ""),
        }

    def _clean_parse_cache(self) -> None:
        now = time.time()
        with self._lock:
            expired = [
                key
                for key, record in self._parse_cache.items()
                if now - record.get("_created", 0) > 2700
            ]
            for key in expired:
                self._parse_cache.pop(key, None)
            while len(self._parse_cache) > 24:
                oldest = min(self._parse_cache, key=lambda k: self._parse_cache[k].get("_created", 0))
                self._parse_cache.pop(oldest, None)

    def clear_parse_cache(self) -> None:
        with self._lock:
            self._parse_cache.clear()

    def parse(self, raw_url: str, page_hint: Any = None) -> dict:
        resolved = self.resolve_input(raw_url, page_hint)
        view = self._fetch_view(resolved.bvid, resolved.aid)
        bvid = str(view.get("bvid") or resolved.bvid or "")
        aid = int(view.get("aid") or resolved.aid or 0)
        pages_data = list(view.get("pages") or [])
        if not pages_data:
            pages_data = [
                {
                    "cid": view.get("cid"),
                    "page": 1,
                    "part": str(view.get("title") or ""),
                    "duration": view.get("duration") or 0,
                }
            ]
        page_count = len(pages_data)
        page_number = min(max(1, int(resolved.page or 1)), page_count)
        page_info = pages_data[page_number - 1]
        cid = page_info.get("cid") or view.get("cid")
        page_title = _clean_html_text(page_info.get("part") or view.get("title"))
        duration = float(page_info.get("duration") or view.get("duration") or 0)
        info, warnings = self._extract_formats(bvid, cid, page_number, duration)
        try:
            duration = float(info.get("duration") or duration or 0)
        except (TypeError, ValueError):
            pass
        preferred_codec = str(self.settings.get().get("prefer_codec") or "avc")
        qualities, audio_public, best_audio = self._build_qualities(info, duration, preferred_codec)
        default_quality = next((item for item in qualities if item.get("default")), qualities[0])

        cover = str(view.get("pic") or info.get("thumbnail") or "")
        owner = view.get("owner") or {}
        stat = view.get("stat") or {}
        publish_date = ""
        try:
            timestamp = int(view.get("pubdate") or info.get("timestamp") or 0)
            if timestamp:
                publish_date = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
        except (OSError, ValueError, OverflowError):
            publish_date = ""

        parse_id = uuid.uuid4().hex[:16]
        public_qualities = []
        for item in qualities:
            public_qualities.append(
                {
                    "id": item["id"],
                    "label": item["label"],
                    "short_label": item["short_label"],
                    "height": item["height"],
                    "width": item["width"],
                    "fps": item["fps"],
                    "hdr": item["hdr"],
                    "codec": item["codec"],
                    "size": item["size"],
                    "note": item["note"],
                    "best": bool(item.get("best")),
                    "default": bool(item.get("default")),
                }
            )
        record = {
            "_created": time.time(),
            "parse_id": parse_id,
            "source_url": resolved.canonical_url
            + (f"?p={page_number}" if page_count > 1 else ""),
            "bvid": bvid,
            "aid": aid,
            "cid": cid,
            "title": _clean_html_text(view.get("title")),
            "page_title": page_title,
            "page": page_number,
            "page_count": page_count,
            "pages": [
                {
                    "page": int(item.get("page") or index + 1),
                    "title": _clean_html_text(item.get("part") or f"P{index + 1}"),
                    "duration": int(item.get("duration") or 0),
                    "cid": item.get("cid"),
                }
                for index, item in enumerate(pages_data)
            ],
            "cover": cover,
            "uploader": {
                "name": _clean_html_text(owner.get("name") or info.get("uploader") or "未知UP主"),
                "mid": owner.get("mid") or info.get("uploader_id"),
                "face": str(owner.get("face") or ""),
            },
            "duration": int(round(duration)) if duration else 0,
            "view_count": int(stat.get("view") or info.get("view_count") or 0),
            "like_count": int(stat.get("like") or info.get("like_count") or 0),
            "publish_date": publish_date,
            "description": _clean_html_text(view.get("desc") or info.get("description") or ""),
            "tags": [str(tag) for tag in (info.get("tags") or []) if tag][:12],
            "quality_label": default_quality["label"],
            "qualities": public_qualities,
            "default_quality_id": default_quality["id"],
            "audio": audio_public,
            "preferred_codec": preferred_codec,
            "guest_tip": not self.is_logged_in,
            "warnings": warnings[:2],
            "expires_in": 2700,
            "_qualities": qualities,
            "_best_audio": best_audio,
        }
        with self._lock:
            self._parse_cache[parse_id] = record
        self._clean_parse_cache()
        log.info(
            "Parsed %s p%s -> %s (%s qualities)",
            bvid,
            page_number,
            record["title"],
            len(public_qualities),
        )
        return self._public_record(record)

    def _public_record(self, record: dict) -> dict:
        public_keys = (
            "parse_id",
            "source_url",
            "bvid",
            "aid",
            "cid",
            "title",
            "page_title",
            "page",
            "page_count",
            "pages",
            "cover",
            "uploader",
            "duration",
            "view_count",
            "like_count",
            "publish_date",
            "description",
            "tags",
            "quality_label",
            "qualities",
            "default_quality_id",
            "audio",
            "preferred_codec",
            "guest_tip",
            "warnings",
            "expires_in",
        )
        result = {key: record.get(key) for key in public_keys}
        result["account"] = self.account_public()
        return result

    def get_parse_record(self, parse_id: str) -> dict:
        self._clean_parse_cache()
        with self._lock:
            record = self._parse_cache.get(str(parse_id))
            if not record:
                raise BiliError("解析结果已过期，请重新粘贴链接解析。", code="parse_expired", status=410)
            return record

    def select_quality(self, record: dict, quality_id: str) -> dict:
        qualities = record.get("_qualities") or []
        selected = next((item for item in qualities if item.get("id") == quality_id), None)
        if not selected:
            raise BiliError("所选清晰度已失效，请重新解析。", code="quality_expired", status=410)
        return selected

    def build_download_plan(self, parse_id: str, quality_id: str, mode: str, save_cover: bool) -> dict:
        record = self.get_parse_record(parse_id)
        mode = "audio" if mode == "audio" else "video"
        if mode == "audio":
            audio = record.get("_best_audio")
            if not audio or not audio.get("url"):
                raise BiliError("该视频没有可单独下载的音轨。", code="audio_unavailable", status=400)
            streams = [{"kind": "audio", "format": audio}]
            quality_label = "最佳音质"
            ext = "m4a"
        else:
            quality = self.select_quality(record, quality_id)
            video = quality.get("_video")
            audio = quality.get("_audio")
            if not video or not video.get("url"):
                raise BiliError("所选清晰度的视频流不可用，请重新解析。", code="stream_unavailable", status=410)
            streams = [{"kind": "video", "format": video}]
            if audio and audio.get("url") and not quality.get("_combined"):
                streams.append({"kind": "audio", "format": audio})
            quality_label = quality["label"]
            ext = "mp4"

        title = record.get("title") or "Bili Video"
        page_title = record.get("page_title") or ""
        page_number = int(record.get("page") or 1)
        page_count = int(record.get("page_count") or 1)
        filename_parts = [title]
        if page_count > 1 and page_title and page_title != title:
            filename_parts.append(f"P{page_number} {page_title}")
        elif page_count > 1:
            filename_parts.append(f"P{page_number}")
        filename = self._safe_filename(" - ".join(filename_parts))
        if mode == "video":
            filename += f" [{record.get('bvid') or 'Bili'}]"
        else:
            filename += " [音频]"
        return {
            "bvid": record.get("bvid"),
            "title": title,
            "page_title": page_title,
            "page": page_number,
            "page_count": page_count,
            "duration": record.get("duration") or 0,
            "cover": record.get("cover") or "",
            "uploader": (record.get("uploader") or {}).get("name") or "",
            "source_url": record.get("source_url") or "",
            "mode": mode,
            "quality_label": quality_label,
            "save_cover": bool(save_cover),
            "output_basename": filename,
            "ext": ext,
            "streams": streams,
        }

    @staticmethod
    def _safe_filename(name: str) -> str:
        name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", " ", str(name))
        name = re.sub(r"\s+", " ", name).strip(" .")
        if not name:
            name = "Bili Video"
        if len(name) > 130:
            name = name[:130].rstrip(" .")
        return name or "Bili Video"


def fetch_remote_image(url: str, timeout: float = 20) -> tuple[bytes, str]:
    if not is_safe_image_url(url):
        raise BiliError("图片地址不受支持。", code="invalid_image", status=400)
    parsed = urlparse.urlsplit(url)
    if parsed.scheme == "http":
        url = "https://" + url[len("http://") :]
    request = urlrequest.Request(
        url,
        headers={
            "User-Agent": config.USER_AGENT,
            "Referer": "https://www.bilibili.com/",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        },
    )
    try:
        with urlrequest.urlopen(request, timeout=timeout, context=_SSL_CONTEXT) as response:
            data = response.read(16 * 1024 * 1024 + 1)
            if len(data) > 16 * 1024 * 1024:
                raise BiliError("图片文件过大。", code="image_too_large", status=413)
            content_type = response.headers.get_content_type() or "image/jpeg"
            return data, content_type
    except urlerror.HTTPError as exc:
        raise BiliError("封面图片加载失败。", code="image_failed", status=502) from exc
    except (urlerror.URLError, TimeoutError, socket.timeout) as exc:
        raise BiliError("封面图片加载超时。", code="image_failed", status=502) from exc
