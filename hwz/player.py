from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import config
from .bili import BiliClient, BiliError

log = logging.getLogger(__name__)

_ALLOWED_TARGET_FPS = {60, 120}
_ALLOWED_MAX_HEIGHTS = {0, 540, 720, 1080}
_PLAYLIST_FILE = "index.m3u8"


class PlayerError(Exception):
    def __init__(self, message: str, code: str = "player_error", status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def _run_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _header_block(headers: dict[str, str]) -> str:
    lines: list[str] = []
    for key, value in headers.items():
        name = str(key or "").strip()
        text = str(value or "").replace("\r", "").replace("\n", "").strip()
        if name and text:
            lines.append(f"{name}: {text}")
    return "\r\n".join(lines) + ("\r\n" if lines else "")


def _format_speed(raw: str) -> float:
    text = str(raw or "").strip().lower()
    if text.endswith("x"):
        text = text[:-1]
    try:
        return max(0.0, float(text))
    except (TypeError, ValueError):
        return 0.0


def _playlist_stats_from_text(text: str) -> tuple[int, float, bool]:
    count = 0
    available = 0.0
    for line in text.splitlines():
        if line.startswith("#EXTINF:"):
            count += 1
            available += _as_float(line[len("#EXTINF:") :].split(",", 1)[0], 0.0)
    return count, available, "#EXT-X-ENDLIST" in text


def _source_size(plan: dict) -> tuple[int, int]:
    width = _as_int(plan.get("video_width"), 0)
    height = _as_int(plan.get("video_height"), 0)
    if width > 0 and height > 0:
        return width, height
    if height > 0:
        return int(round(height * 16 / 9 / 2) * 2), height
    return 1920, 1080


def _scaled_size(plan: dict, max_height: int) -> tuple[int, int]:
    source_w, source_h = _source_size(plan)
    if max_height and source_h > max_height:
        out_h = max_height
        out_w = int(round(source_w * out_h / max(1, source_h) / 2) * 2)
        return max(2, out_w), max(2, out_h)
    # Keep even dimensions for yuv420p even when no cap is requested.
    return max(2, source_w // 2 * 2), max(2, source_h // 2 * 2)


class PlayerSession:
    def __init__(
        self,
        plan: dict,
        options: dict,
        ffmpeg_path: Path,
        cookie_header: str,
        directory: Path,
    ) -> None:
        self.id = uuid.uuid4().hex[:16]
        self.plan = plan
        self.options = options
        self.ffmpeg_path = ffmpeg_path
        self.cookie_header = cookie_header
        self.directory = directory
        self.playlist = directory / _PLAYLIST_FILE
        self.created_at = time.time()
        self.last_access = self.created_at
        self.status = "preparing"
        self.message = "正在启动本地插帧引擎"
        self.error = ""
        self.error_code = ""
        self.speed = 0.0
        self.encoded_fps = 0.0
        self.processed = 0.0
        self.segments = 0
        self.available = 0.0
        self.has_endlist = False
        self._process: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._monitor: threading.Thread | None = None
        self._stderr_lines: list[str] = []
        self._lock = threading.RLock()
        self._playlist_cache = b""

        self.source_fps = _as_float(plan.get("video_fps"), 0.0)
        self.target_fps = 0
        self.interpolated = bool(options.get("interpolate", True))
        if self.interpolated:
            self.target_fps = _as_int(options.get("target_fps"), 60)
            if self.target_fps not in _ALLOWED_TARGET_FPS:
                self.target_fps = 60
        self.max_height = _as_int(options.get("max_height"), 0)
        if self.max_height not in _ALLOWED_MAX_HEIGHTS:
            self.max_height = 0
        self.output_width, self.output_height = _scaled_size(plan, self.max_height)
        self._attempts = self._build_attempts()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PlayerError("无法创建插帧缓存目录，请检查磁盘空间。", "player_dir_failed", 500) from exc
        self._thread = threading.Thread(
            target=self._run,
            name=f"hwz-player-{self.id}",
            daemon=True,
        )
        self._thread.start()
        self._monitor = threading.Thread(
            target=self._monitor_playlist,
            name=f"hwz-player-monitor-{self.id}",
            daemon=True,
        )
        self._monitor.start()

    def _build_attempts(self) -> list[tuple[str, str]]:
        streams = [item for item in (self.plan.get("streams") or []) if isinstance(item, dict)]
        video = next((item for item in streams if item.get("kind") == "video"), None)
        audio = next((item for item in streams if item.get("kind") == "audio"), None)
        if not video:
            raise PlayerError("所选清晰度没有可播放的视频流。", "stream_unavailable", 410)

        def urls(item: dict | None) -> list[str]:
            if not item:
                return [""]
            fmt = item.get("format") or {}
            result = [str(fmt.get("url") or "")]
            for backup in fmt.get("backup_urls") or []:
                backup = str(backup or "")
                if backup and backup not in result:
                    result.append(backup)
            return result or [""]

        video_urls = urls(video)
        audio_urls = urls(audio)
        length = max(len(video_urls), len(audio_urls))
        attempts: list[tuple[str, str]] = []
        for index in range(length):
            video_url = video_urls[min(index, len(video_urls) - 1)]
            audio_url = audio_urls[min(index, len(audio_urls) - 1)] if audio else ""
            if video_url and (video_url, audio_url) not in attempts:
                attempts.append((video_url, audio_url))
        return attempts

    def _run(self) -> None:
        last_error = ""
        attempts = self._attempts or [("", "")]
        for index, (video_url, audio_url) in enumerate(attempts):
            if self._stop.is_set():
                break
            if index > 0:
                with self._lock:
                    self.status = "preparing"
                    self.message = "主线路不可用，正在切换备用线路"
                self._clear_output()
            ok = self._run_attempt(video_url, audio_url)
            if ok or self._stop.is_set():
                return
            with self._lock:
                last_error = self.error or last_error
        if self._stop.is_set():
            with self._lock:
                self.status = "stopped"
                self.message = "播放已停止"
        else:
            with self._lock:
                self.status = "error"
                self.error = last_error or "本地插帧处理失败，请重新解析后再试。"
                self.error_code = self.error_code or "player_failed"
                self.message = self.error

    def _run_attempt(self, video_url: str, audio_url: str) -> bool:
        if not video_url:
            with self._lock:
                self.error = "视频流地址为空，请重新解析。"
                self.error_code = "stream_unavailable"
            return False
        self._clear_output()
        command = self._build_command(video_url, audio_url)
        log.info("Starting interpolation session %s (%s -> %s)", self.id, self.source_fps, self.target_fps or "source")
        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.directory),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=_run_flags(),
            )
        except OSError as exc:
            with self._lock:
                self.error = "无法启动 FFmpeg 插帧引擎，请确认程序文件完整。"
                self.error_code = "ffmpeg_start_failed"
            log.exception("Could not start player ffmpeg")
            return False
        with self._lock:
            self._process = process
            self.status = "preparing"
            fps_label = f"{self.target_fps}fps" if self.interpolated and self.target_fps else "原始帧率"
            self.message = f"正在生成首批 {fps_label} 画面"
            self.error = ""
            self.error_code = ""
            self._stderr_lines.clear()

        progress_thread = threading.Thread(
            target=self._read_progress,
            args=(process,),
            name=f"hwz-player-progress-{self.id}",
            daemon=True,
        )
        error_thread = threading.Thread(
            target=self._read_stderr,
            args=(process,),
            name=f"hwz-player-stderr-{self.id}",
            daemon=True,
        )
        progress_thread.start()
        error_thread.start()
        returncode = process.wait()
        progress_thread.join(timeout=2)
        error_thread.join(timeout=2)
        with self._lock:
            self._process = None
        self._refresh_playlist()

        if self._stop.is_set():
            return True
        if returncode == 0:
            with self._lock:
                self.status = "ended"
                self.message = "插帧播放已完成"
            return True

        detail = self._error_detail()
        with self._lock:
            self.error = self._friendly_error(detail, returncode)
            self.error_code = "player_failed"
            self.message = self.error
        # A failed first attempt is often just an expired primary CDN URL.
        if self.segments == 0 and len(self._attempts) > 1:
            log.warning("Interpolation attempt failed, trying a backup CDN: %s", detail[-300:])
            return False
        return False

    def _build_command(self, video_url: str, audio_url: str) -> list[str]:
        headers = {
            "User-Agent": config.USER_AGENT,
            "Referer": str(self.plan.get("source_url") or "https://www.bilibili.com/"),
            "Origin": "https://www.bilibili.com",
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        if self.cookie_header:
            headers["Cookie"] = self.cookie_header
        header_text = _header_block(headers)

        command = [
            str(self.ffmpeg_path),
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
            "-progress",
            "pipe:1",
            "-stats_period",
            "0.5",
        ]
        for url in (video_url, audio_url):
            if not url:
                continue
            command += [
                "-headers",
                header_text,
                "-rw_timeout",
                "20000000",
                "-reconnect",
                "1",
                "-reconnect_streamed",
                "1",
                "-reconnect_delay_max",
                "5",
                "-i",
                url,
            ]

        filters = []
        filters.append(
            f"scale={self.output_width}:{self.output_height}:flags=lanczos"
        )
        if self.interpolated and self.target_fps:
            filters.append(f"minterpolate=fps={self.target_fps}:mi_mode=blend")
        filters.append("format=yuv420p")
        command += [
            "-filter_complex",
            f"[0:v]{','.join(filters)}[v]",
            "-map",
            "[v]",
        ]
        if audio_url:
            command += ["-map", "1:a:0?", "-shortest"]
        else:
            # Combined fallback streams can carry audio in the first input.
            command += ["-map", "0:a:0?"]
        command += [
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-crf",
            "21",
            "-profile:v",
            "high",
            "-level",
            "4.2",
            "-pix_fmt",
            "yuv420p",
            "-g",
            str(self.target_fps or max(30, int(round(self.source_fps or 30)))),
            "-keyint_min",
            str(self.target_fps or max(30, int(round(self.source_fps or 30)))),
            "-sc_threshold",
            "0",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-max_muxing_queue_size",
            "1024",
            "-f",
            "hls",
            "-hls_time",
            "1",
            "-hls_list_size",
            "0",
            "-hls_playlist_type",
            "event",
            "-hls_flags",
            "independent_segments+temp_file",
            "-hls_segment_type",
            "fmp4",
            "-hls_fmp4_init_filename",
            "init.mp4",
            "-hls_segment_filename",
            "seg_%05d.m4s",
            _PLAYLIST_FILE,
        ]
        return command

    def _read_progress(self, process: subprocess.Popen) -> None:
        stream = process.stdout
        if not stream:
            return
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", errors="replace").strip()
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key == "out_time_us":
                    with self._lock:
                        self.processed = max(0.0, _as_int(value, 0) / 1_000_000)
                elif key == "speed":
                    with self._lock:
                        self.speed = _format_speed(value)
                elif key == "fps":
                    with self._lock:
                        self.encoded_fps = max(0.0, _as_float(value, 0))
                elif key == "progress" and value == "end":
                    with self._lock:
                        self.message = "正在写入最后一段画面"
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _read_stderr(self, process: subprocess.Popen) -> None:
        stream = process.stderr
        if not stream:
            return
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                with self._lock:
                    self._stderr_lines.append(line)
                    if len(self._stderr_lines) > 80:
                        self._stderr_lines = self._stderr_lines[-80:]
        except OSError:
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _monitor_playlist(self) -> None:
        while not self._stop.is_set():
            process_alive = False
            with self._lock:
                process = self._process
                process_alive = bool(process and process.poll() is None)
            self._refresh_playlist()
            with self._lock:
                if self.status not in {"error", "stopped"}:
                    if self.segments > 0 and process_alive and self.status == "preparing":
                        self.status = "running"
                        self.message = (
                            "正在实时生成插帧画面"
                            if self.speed <= 0 or self.speed >= 0.9
                            else "生成速度较慢，播放时可能需要缓冲"
                        )
                    elif self.segments > 0 and self.status == "preparing":
                        self.status = "running"
                    if self.status == "running" and self.speed and self.speed < 0.85:
                        self.message = "生成速度较慢，播放时可能需要缓冲"
            if not process_alive and self.status in {"ended", "error", "stopped"}:
                break
            time.sleep(0.5)

    def _refresh_playlist(self) -> None:
        text = self.read_playlist_text()
        if text:
            segments, available, has_endlist = _playlist_stats_from_text(text)
        else:
            segments, available, has_endlist = 0, 0.0, False
        with self._lock:
            self.segments = segments
            self.available = available
            self.has_endlist = has_endlist

    def read_playlist_text(self) -> str:
        """Read the playlist without exposing a transient FFmpeg rewrite as 500."""
        for attempt in range(5):
            try:
                data = self.playlist.read_bytes()
            except OSError:
                data = b""
            if data.startswith(b"#EXTM3U"):
                with self._lock:
                    self._playlist_cache = data
                return data.decode("utf-8", errors="replace")
            if attempt < 4:
                time.sleep(0.035)
        with self._lock:
            cached = self._playlist_cache
        return cached.decode("utf-8", errors="replace") if cached else ""

    def read_playlist_bytes(self) -> bytes:
        self.read_playlist_text()
        with self._lock:
            return self._playlist_cache

    def _clear_output(self) -> None:
        try:
            if not self.directory.exists():
                return
            for entry in self.directory.iterdir():
                try:
                    if entry.is_dir():
                        shutil.rmtree(entry, ignore_errors=True)
                    else:
                        entry.unlink(missing_ok=True)
                except OSError:
                    continue
        except OSError:
            pass

    def _error_detail(self) -> str:
        with self._lock:
            return "\n".join(self._stderr_lines[-12:])

    @staticmethod
    def _friendly_error(detail: str, returncode: int) -> str:
        text = (detail or "").lower()
        if any(token in text for token in ("403", "forbidden", "404", "not found", "server returned")):
            return "视频流地址已失效，请重新解析后再播放。"
        if "no space left" in text or "disk full" in text:
            return "磁盘空间不足，无法生成插帧缓存。"
        if "cannot allocate memory" in text or "out of memory" in text:
            return "内存不足，请在播放设置中选择更低的输出分辨率。"
        if "unknown encoder" in text or "encoder not found" in text:
            return "当前 FFmpeg 不支持 H.264 编码，请重新安装完整版本。"
        if "invalid data" in text or "error while decoding" in text:
            return "视频编码无法解码，请尝试其他清晰度。"
        if returncode == 0:
            return "插帧处理提前结束，请重新解析后再试。"
        return "本地插帧处理失败，请重新解析或降低输出分辨率后重试。"

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            process = self._process
        if process and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
        if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=4)
        self._clear_output()
        try:
            self.directory.rmdir()
        except OSError:
            shutil.rmtree(self.directory, ignore_errors=True)
        with self._lock:
            self.status = "stopped"
            self.message = "播放已停止"

    def public(self) -> dict:
        self._refresh_playlist()
        with self._lock:
            duration = _as_float(self.plan.get("duration"), 0.0)
            progress = (self.available / duration) if duration > 0 else 0.0
            min_ready = 8.0 if self.target_fps >= 120 else 5.0
            ready = self.available >= min_ready or (self.status == "ended" and self.segments >= 1)
            return {
                "id": self.id,
                "status": self.status,
                "ready": ready,
                "message": self.message,
                "error": self.error,
                "error_code": self.error_code,
                "title": self.plan.get("title") or "B站视频",
                "page_title": self.plan.get("page_title") or "",
                "quality_label": self.plan.get("quality_label") or "",
                "duration": duration,
                "available": round(self.available, 2),
                "progress": round(min(1.0, max(0.0, progress)), 4),
                "segments": self.segments,
                "speed": round(self.speed, 2),
                "encoded_fps": round(self.encoded_fps, 1),
                "source_fps": round(self.source_fps, 2),
                "target_fps": self.target_fps,
                "interpolated": self.interpolated,
                "output_width": self.output_width,
                "output_height": self.output_height,
                "hls_url": f"/api/player/{self.id}/{_PLAYLIST_FILE}",
                "created_at": self.created_at,
            }


class InterpolationPlayer:
    def __init__(
        self,
        settings: Any,
        bili: BiliClient,
        ffmpeg_path: Path | None,
    ) -> None:
        self.settings = settings
        self.bili = bili
        self.ffmpeg_path = ffmpeg_path
        self._lock = threading.RLock()
        self._sessions: dict[str, PlayerSession] = {}
        self._cleanup_old_sessions()

    def _cleanup_old_sessions(self) -> None:
        cutoff = time.time() - 60 * 60 * 12
        try:
            config.PLAYER_DIR.mkdir(parents=True, exist_ok=True)
            for entry in config.PLAYER_DIR.iterdir():
                if not entry.is_dir():
                    continue
                try:
                    if entry.stat().st_mtime < cutoff:
                        shutil.rmtree(entry, ignore_errors=True)
                except OSError:
                    continue
        except OSError:
            pass

    @property
    def available(self) -> bool:
        return bool(self.ffmpeg_path and Path(self.ffmpeg_path).exists())

    def capabilities(self) -> dict:
        return {
            "available": self.available,
            "engine": "ffmpeg",
            "target_fps": sorted(_ALLOWED_TARGET_FPS),
            "max_heights": [0, 540, 720, 1080],
            "note": "实时模式使用本地帧混合插帧，低延迟、低 CPU 占用。",
        }

    def create(self, parse_id: str, quality_id: str, options: dict | None = None) -> dict:
        if not self.available:
            raise PlayerError("未找到 FFmpeg，无法启动插帧播放。", "ffmpeg_missing", 503)
        options = options if isinstance(options, dict) else {}
        try:
            self.bili.get_parse_record(parse_id)
            plan = self.bili.build_download_plan(parse_id, quality_id, "video", False)
        except BiliError as exc:
            raise PlayerError(exc.message, exc.code, exc.status) from exc

        target_fps = _as_int(options.get("target_fps"), 60)
        if target_fps not in _ALLOWED_TARGET_FPS:
            target_fps = 60
        max_height = _as_int(options.get("max_height"), 0)
        if max_height not in _ALLOWED_MAX_HEIGHTS:
            max_height = 0
        if target_fps >= 120 and max_height not in {540, 720}:
            # 120fps at 1080p is unlikely to keep up with real-time playback
            # on typical laptops; 720p keeps the feature usable and smooth.
            max_height = 720
        interpolate = bool(options.get("interpolate", True))

        source_fps = _as_float(plan.get("video_fps"), 0.0)
        if source_fps >= 55 and target_fps <= 60:
            # The source is already high frame rate; no need to invent frames.
            interpolate = False
        normalized = {
            "target_fps": target_fps,
            "max_height": max_height,
            "interpolate": interpolate,
        }
        session = PlayerSession(
            plan=plan,
            options=normalized,
            ffmpeg_path=Path(self.ffmpeg_path),
            cookie_header=self.bili.cookie_header(),
            directory=config.PLAYER_DIR / uuid.uuid4().hex,
        )
        with self._lock:
            while len(self._sessions) >= 2:
                oldest_id = min(self._sessions, key=lambda key: self._sessions[key].created_at)
                old = self._sessions.pop(oldest_id, None)
                if old:
                    old.stop()
            self._sessions[session.id] = session
        try:
            session.start()
        except Exception:
            with self._lock:
                self._sessions.pop(session.id, None)
            session.stop()
            raise
        return session.public()

    def get(self, session_id: str) -> PlayerSession:
        with self._lock:
            session = self._sessions.get(str(session_id))
        if not session:
            raise PlayerError("插帧播放会话不存在或已结束。", "player_missing", 404)
        session.last_access = time.time()
        return session

    def status(self, session_id: str) -> dict:
        return self.get(session_id).public()

    def stop(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.pop(str(session_id), None)
        if not session:
            return False
        session.stop()
        return True

    def stop_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            try:
                session.stop()
            except Exception:
                log.debug("Could not stop player session", exc_info=True)


__all__ = ["InterpolationPlayer", "PlayerError", "PlayerSession"]
