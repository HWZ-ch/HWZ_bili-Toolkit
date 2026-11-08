from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib import parse as urlparse

from . import config
from .bili import BiliClient, BiliError, fetch_remote_image, is_safe_image_url
from .downloader import DownloadManager, TaskBusyError
from .store import DownloadStats, HistoryStore, SettingsStore

log = logging.getLogger(__name__)

_IMAGE_CACHE_MAX_AGE = 60 * 60 * 24 * 30
_MAX_BODY = 1024 * 1024


class ApiRequestError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "bad_request") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class Application:
    def __init__(self) -> None:
        config.ensure_runtime_dirs()
        self.token = secrets.token_urlsafe(28)
        self.settings = SettingsStore()
        self.history = HistoryStore()
        self.bili = BiliClient(self.settings)
        self.manager = DownloadManager(self.settings, self.history, self.bili.cookie_header)
        self.stats = DownloadStats(self.history)
        self.started_at = time.time()
        self._threads: list[threading.Thread] = []
        self._start_background_tasks()

    def _start_background_tasks(self) -> None:
        if self.bili.cookie_header():
            thread = threading.Thread(target=self.bili.refresh_account, daemon=True)
            thread.start()
            self._threads.append(thread)
        cleaner = threading.Thread(target=self._clean_image_cache, daemon=True)
        cleaner.start()
        self._threads.append(cleaner)

    @staticmethod
    def _clean_image_cache() -> None:
        try:
            files = sorted(
                config.IMAGE_CACHE_DIR.glob("*"),
                key=lambda path: path.stat().st_mtime if path.exists() else 0,
            )
            cutoff = time.time() - _IMAGE_CACHE_MAX_AGE
            for item in files:
                try:
                    if item.is_file() and item.stat().st_mtime < cutoff:
                        item.unlink(missing_ok=True)
                except OSError:
                    continue
            try:
                current = sorted(
                    config.IMAGE_CACHE_DIR.glob("*"),
                    key=lambda path: path.stat().st_mtime if path.exists() else 0,
                )
                for item in current[:-800]:
                    try:
                        item.unlink(missing_ok=True)
                    except OSError:
                        continue
            except OSError:
                pass
        except OSError:
            return

    def bootstrap(self) -> dict:
        settings = self.settings.get()
        tasks = self.manager.list_tasks()
        history = self.history.list()
        free = total = 0
        try:
            usage = shutil.disk_usage(settings.get("download_dir") or str(Path.home()))
            free, total = usage.free, usage.total
        except OSError:
            pass
        return {
            "app": {
                "name": config.APP_NAME,
                "version": config.APP_VERSION,
                "ffmpeg": bool(self.manager._ffmpeg),
                "data_dir": str(config.DATA_DIR),
                "started_at": self.started_at,
                "platform": sys.platform,
            },
            "settings": settings,
            "defaults": {"download_dir": str(config.default_download_dir())},
            "account": self.bili.account_public(),
            "tasks": tasks,
            "history": history[:8],
            "stats": self.stats.summary(tasks),
            "disk": {"free": free, "total": total},
        }

    def image(self, url: str) -> tuple[bytes, str]:
        if not is_safe_image_url(url):
            raise ApiRequestError("图片地址不受支持。", 400, "invalid_image")
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
        matches = list(config.IMAGE_CACHE_DIR.glob(key + ".*"))
        for match in matches:
            if match.is_file() and match.suffix not in {".tmp"}:
                try:
                    data = match.read_bytes()
                    mime, _ = mimetypes.guess_type(str(match))
                    return data, mime or "image/jpeg"
                except OSError:
                    continue
        data, content_type = fetch_remote_image(url)
        subtype = content_type.split("/", 1)[-1].split(";")[0].strip().lower()
        extension = {
            "jpeg": ".jpg",
            "jpg": ".jpg",
            "png": ".png",
            "webp": ".webp",
            "gif": ".gif",
            "avif": ".avif",
        }.get(subtype, ".jpg")
        target = config.IMAGE_CACHE_DIR / f"{key}{extension}"
        try:
            temp = target.with_suffix(extension + ".tmp")
            temp.write_bytes(data)
            os.replace(temp, target)
        except OSError:
            pass
        return data, content_type

    def shutdown(self) -> None:
        try:
            self.manager.shutdown()
        except Exception:
            log.exception("Error while shutting down downloads")


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "HWZBili/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> Application:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------
    def _headers(self, status: int, content_type: str, content_length: int, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; font-src 'self'; connect-src 'self'; media-src 'self' blob:; "
            "object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        self.end_headers()

    def send_json(self, payload: Any, status: int = 200) -> None:
        body = _json_bytes(payload)
        self._headers(status, "application/json; charset=utf-8", len(body))
        self._write_body(body)

    def send_error_json(self, message: str, status: int = 400, code: str = "error") -> None:
        self.send_json({"ok": False, "error": message, "code": code}, status=status)

    def _write_body(self, body: bytes) -> None:
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------
    def _authorized(self, allow_query: bool = False) -> bool:
        header_token = self.headers.get("X-App-Token", "")
        if header_token and secrets.compare_digest(header_token, self.app.token):
            return True
        if allow_query:
            query = urlparse.parse_qs(urlparse.urlsplit(self.path).query)
            token = (query.get("t") or [""])[0]
            if token and secrets.compare_digest(token, self.app.token):
                return True
        return False

    def _require_auth(self, allow_query: bool = False) -> bool:
        if self._authorized(allow_query=allow_query):
            return True
        self.send_error_json("访问令牌无效，请从应用内重新打开。", 403, "forbidden")
        return False

    def _read_json(self) -> dict:
        length_header = self.headers.get("Content-Length")
        try:
            length = int(length_header or 0)
        except (TypeError, ValueError):
            length = 0
        if length > _MAX_BODY:
            raise ApiRequestError("请求内容过大。", 413, "body_too_large")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiRequestError("请求数据格式不正确。", 400, "invalid_json") from exc
        if not isinstance(data, dict):
            raise ApiRequestError("请求数据格式不正确。", 400, "invalid_json")
        return data

    # ------------------------------------------------------------------
    # HTTP methods
    # ------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse.urlsplit(self.path)
            path = parsed.path
            if path in {"/healthz", "/api/health"}:
                self.send_json({"ok": True, "version": config.APP_VERSION})
                return
            if path == "/api/image":
                if not self._require_auth(allow_query=True):
                    return
                query = urlparse.parse_qs(parsed.query)
                image_url = (query.get("u") or [""])[0]
                self._serve_image(image_url)
                return
            if path.startswith("/api/"):
                if not self._require_auth():
                    return
                self._handle_api_get(path, parsed)
                return
            self._serve_static(path)
        except ApiRequestError as exc:
            self.send_error_json(exc.message, exc.status, exc.code)
        except BiliError as exc:
            log.info("Bilibili API error %s: %s", exc.code, exc.message)
            self.send_error_json(exc.message, exc.status, exc.code)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception("Unhandled GET error")
            self.send_error_json("服务器内部错误，请稍后重试。", 500, "internal_error")

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urlparse.urlsplit(self.path)
            path = parsed.path
            if not path.startswith("/api/"):
                self.send_error_json("不支持的请求。", 404, "not_found")
                return
            if not self._require_auth():
                return
            payload = self._read_json()
            self._handle_api_post(path, payload)
        except ApiRequestError as exc:
            self.send_error_json(exc.message, exc.status, exc.code)
        except BiliError as exc:
            log.info("Bilibili API error %s: %s", exc.code, exc.message)
            self.send_error_json(exc.message, exc.status, exc.code)
        except TaskBusyError as exc:
            self.send_error_json(str(exc), 409, "task_busy")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception("Unhandled POST error")
            self.send_error_json("服务器内部错误，请稍后重试。", 500, "internal_error")

    def _handle_api_get(self, path: str, parsed: Any) -> None:
        app = self.app
        if path == "/api/bootstrap":
            self.send_json(app.bootstrap())
            return
        if path == "/api/settings":
            self.send_json({"settings": app.settings.get()})
            return
        if path == "/api/account":
            query = urlparse.parse_qs(parsed.query)
            if (query.get("refresh") or ["0"])[0] in {"1", "true"}:
                self.send_json({"account": app.bili.refresh_account()})
            else:
                self.send_json({"account": app.bili.account_public()})
            return
        if path == "/api/login/qr":
            self.send_json(app.bili.generate_login_qr())
            return
        if path == "/api/login/poll":
            query = urlparse.parse_qs(parsed.query)
            key = (query.get("key") or [""])[0]
            self.send_json(app.bili.poll_login_qr(key))
            return
        if path == "/api/tasks":
            tasks = app.manager.list_tasks()
            self.send_json(
                {
                    "tasks": tasks,
                    "stats": app.stats.summary(tasks),
                    "server_time": time.time(),
                }
            )
            return
        if path == "/api/history":
            items = app.history.list()
            self.send_json({"items": items, "count": len(items)})
            return
        if path == "/api/disk":
            settings = app.settings.get()
            try:
                usage = shutil.disk_usage(settings.get("download_dir") or str(Path.home()))
                self.send_json({"free": usage.free, "total": usage.total})
            except OSError:
                self.send_json({"free": 0, "total": 0})
            return
        raise ApiRequestError("接口不存在。", 404, "not_found")

    def _handle_api_post(self, path: str, payload: dict) -> None:
        app = self.app

        if path == "/api/parse":
            url = str(payload.get("url") or "").strip()
            page = payload.get("page")
            try:
                page = int(page) if page not in (None, "") else None
            except (TypeError, ValueError):
                page = None
            result = app.bili.parse(url, page)
            result["stats"] = app.stats.summary(app.manager.list_tasks())
            self.send_json(result)
            return

        if path == "/api/download":
            parse_id = str(payload.get("parse_id") or "")
            quality_id = str(payload.get("quality_id") or "")
            mode = str(payload.get("mode") or "video")
            save_cover = bool(payload.get("save_cover", app.settings.get().get("save_cover")))
            plan = app.bili.build_download_plan(parse_id, quality_id, mode, save_cover)
            task = app.manager.create_task(plan)
            self.send_json(
                {
                    "ok": True,
                    "task": task,
                    "stats": app.stats.summary(app.manager.list_tasks()),
                },
                status=201,
            )
            return

        if path == "/api/settings":
            if "prefer_codec" in payload:
                allowed = {"avc", "hevc", "av1"}
                if str(payload.get("prefer_codec")) not in allowed:
                    payload.pop("prefer_codec", None)
                elif payload.get("prefer_codec") != app.bili.settings.get().get("prefer_codec"):
                    app.bili.clear_parse_cache()
            settings = app.settings.update(payload)
            self.send_json({"ok": True, "settings": settings})
            return

        if path == "/api/logout":
            account = app.bili.logout()
            self.send_json({"ok": True, "account": account})
            return

        if path == "/api/history/delete":
            item_id = str(payload.get("id") or "")
            deleted = app.history.delete(item_id)
            self.send_json({"ok": deleted})
            return

        if path == "/api/history/clear":
            app.history.clear()
            self.send_json({"ok": True})
            return

        if path == "/api/open":
            self._handle_open(payload)
            return

        if path == "/api/cache/clear":
            active = [
                task for task in app.manager.list_tasks()
                if task.get("status") in {"queued", "preparing", "downloading", "pausing", "paused", "merging"}
            ]
            if active:
                raise ApiRequestError("请先暂停并取消正在下载的任务，再清理缓存。", 409, "task_active")
            for directory in (config.TEMP_DIR, config.IMAGE_CACHE_DIR, config.YTDLP_CACHE_DIR):
                try:
                    if directory.exists():
                        shutil.rmtree(directory, ignore_errors=True)
                    directory.mkdir(parents=True, exist_ok=True)
                except OSError:
                    continue
            app.bili.clear_parse_cache()
            self.send_json({"ok": True})
            return

        task_match = re.fullmatch(
            r"/api/tasks/([0-9a-fA-F]+)/(pause|resume|cancel|retry|remove)", path
        )
        if task_match:
            task_id, action = task_match.group(1), task_match.group(2)
            manager = app.manager
            if action == "pause":
                task = manager.pause_task(task_id)
                self.send_json({"ok": True, "task": task})
            elif action == "resume":
                task = manager.resume_task(task_id)
                self.send_json({"ok": True, "task": task})
            elif action == "cancel":
                task = manager.cancel_task(task_id)
                self.send_json({"ok": True, "task": task})
            elif action == "retry":
                task = manager.retry_task(task_id)
                self.send_json({"ok": True, "task": task})
            else:
                removed = manager.remove_task(task_id)
                self.send_json({"ok": removed})
            return

        raise ApiRequestError("接口不存在。", 404, "not_found")

    def _handle_open(self, payload: dict) -> None:
        app = self.app
        task_id = payload.get("task_id")
        history_id = payload.get("history_id")
        target = str(payload.get("target") or "")
        reveal = bool(payload.get("reveal", False))
        if target == "downloads":
            path = app.settings.download_dir()
            if not path.exists():
                path.mkdir(parents=True, exist_ok=True)
            _open_path(path)
        elif target == "data":
            _open_path(config.DATA_DIR)
            path = config.DATA_DIR
        elif task_id:
            path = app.manager.open_target(task_id=str(task_id), folder=not reveal)
            _open_path(path)
        elif history_id:
            path = app.manager.open_target(history_id=str(history_id), folder=not reveal)
            _open_path(path)
        else:
            raise ApiRequestError("没有指定要打开的位置。", 400, "invalid_target")
        self.send_json({"ok": True, "path": str(path)})

    # ------------------------------------------------------------------
    # Static assets
    # ------------------------------------------------------------------
    def _serve_static(self, path: str) -> None:
        if path in {"/", "/index.html"}:
            relative = Path("index.html")
        elif path == "/favicon.ico":
            relative = Path("assets") / "favicon.ico"
        elif path.startswith("/assets/"):
            relative = Path(path.lstrip("/"))
        else:
            self.send_error_json("页面不存在。", 404, "not_found")
            return
        web_root = config.WEB_DIR.resolve()
        target = (web_root / relative).resolve()
        try:
            target.relative_to(web_root)
        except ValueError:
            self.send_error_json("页面不存在。", 404, "not_found")
            return
        if not target.is_file():
            self.send_error_json("页面不存在。", 404, "not_found")
            return
        mime, _ = mimetypes.guess_type(str(target))
        if not mime:
            mime = "application/octet-stream"
        if mime.startswith("text/") or mime in {"application/javascript", "application/json"}:
            if "charset" not in mime:
                mime += "; charset=utf-8"
        try:
            data = target.read_bytes()
        except OSError:
            self.send_error_json("页面读取失败。", 500, "static_read_failed")
            return
        if relative == Path("index.html"):
            version = str(int(self.server.app.started_at)).encode("ascii")
            data = data.replace(b"__ASSET_VERSION__", version)
        cache = "no-store" if relative == Path("index.html") else "no-cache, max-age=0"
        self._headers(200, mime, len(data), cache=cache)
        self._write_body(data)

    def _serve_image(self, url: str) -> None:
        if not url:
            raise ApiRequestError("缺少图片地址。", 400, "missing_image")
        data, content_type = self.app.image(url)
        self._headers(200, content_type, len(data), cache="public, max-age=604800")
        self._write_body(data)


def _open_path(path: Path) -> None:
    if not path.exists():
        raise ApiRequestError("文件或文件夹不存在。", 404, "path_missing")
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError as exc:
        raise ApiRequestError("无法打开文件或文件夹。", 500, "open_failed") from exc


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], app: Application) -> None:
        self.app = app
        super().__init__(server_address, Handler)

    def handle_error(self, request: Any, client_address: Any) -> None:
        log.debug("HTTP connection error", exc_info=True)


class ServerController:
    def __init__(self, app: Application, port: int = 0) -> None:
        self.app = app
        self.httpd = LocalServer(("127.0.0.1", port), app)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def url(self, token: bool = True) -> str:
        base = f"http://127.0.0.1:{self.port}/"
        return f"{base}?t={self.app.token}" if token else base

    def stop(self) -> None:
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        try:
            self.httpd.server_close()
        except Exception:
            pass
        self.app.shutdown()
