from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import queue
import re
import shutil
import ssl
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

import certifi

from . import config
from .bili import BiliError, fetch_remote_image
from .store import HistoryStore, SettingsStore, utc_now_iso

log = logging.getLogger(__name__)

_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

CHUNK_SIZE = 4 * 1024 * 1024
READ_BLOCK = 256 * 1024
USER_AGENT = config.USER_AGENT


class DownloadError(Exception):
    def __init__(self, message: str, code: str = "download_failed") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class TaskStopped(Exception):
    pass


class PauseRequested(TaskStopped):
    pass


class CancelRequested(TaskStopped):
    pass


class TaskBusyError(Exception):
    pass


def _safe_filename(name: str, max_length: int = 150) -> str:
    cleaned = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", " ", str(name))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = "Bili Video"
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if cleaned.upper().split(".")[0] in reserved:
        cleaned = "_" + cleaned
    return cleaned[:max_length].rstrip(" .") or "Bili Video"


def _perfect_size(value: Any) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


STATUS_LABELS = {
    "queued": "排队中",
    "preparing": "准备中",
    "downloading": "下载中",
    "pausing": "正在暂停",
    "paused": "已暂停",
    "merging": "合并音视频",
    "completed": "已完成",
    "error": "下载失败",
    "cancelled": "已取消",
}


class DownloadTask:
    def __init__(
        self,
        plan: dict,
        output_path: Path,
        workdir: Path,
        connections: int,
        ffmpeg_path: Path | None,
        cookie_provider: Callable[[], str] | None = None,
    ) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.plan = plan
        self.output_path = output_path
        self.workdir = workdir
        self.connections = max(4, min(24, int(connections or 12)))
        self.ffmpeg_path = ffmpeg_path
        self.cookie_provider = cookie_provider
        self.created_at = utc_now_iso()
        self.started_at: str | None = None
        self.completed_at: str | None = None
        self.title = str(plan.get("title") or "Bili Video")
        self.page_title = str(plan.get("page_title") or "")
        self.page = int(plan.get("page") or 1)
        self.page_count = int(plan.get("page_count") or 1)
        self.cover = str(plan.get("cover") or "")
        self.uploader = str(plan.get("uploader") or "")
        self.source_url = str(plan.get("source_url") or "")
        self.bvid = str(plan.get("bvid") or "")
        self.quality_label = str(plan.get("quality_label") or "")
        self.mode = str(plan.get("mode") or "video")
        self.duration = int(plan.get("duration") or 0)
        self.save_cover = bool(plan.get("save_cover"))
        self.cover_path = ""
        self.output_size = 0
        self.error = ""
        self.error_code = ""
        self.message = "等待开始"
        self.status = "queued"
        self.progress = 0.0
        self.received = 0
        self.total = 0
        self.speed = 0.0
        self.eta: float | None = None
        self.merge_progress = 0.0
        self._streams: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._pause_requested = False
        self._cancel_requested = False
        self._thread: threading.Thread | None = None
        self._monitor_thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None
        self._last_bytes = 0
        self._started_monotonic = 0.0

    @property
    def approx_total(self) -> int:
        total = 0
        for stream in self.plan.get("streams") or []:
            fmt = stream.get("format") or {}
            value = fmt.get("filesize") or fmt.get("filesize_approx") or 0
            try:
                total += int(value)
            except (TypeError, ValueError):
                pass
        return total

    def output_name(self) -> str:
        return self.output_path.name

    def begin(self) -> None:
        with self._lock:
            self.status = "preparing"
            self.message = "正在准备下载…"
            self.started_at = utc_now_iso()
            self.started_monotonic = time.monotonic()
            self.error = ""
            self.error_code = ""
            self.progress = 0.0
            self.speed = 0.0
            self.eta = None

    def reset_for_run(self) -> None:
        with self._lock:
            self._stop.clear()
            self._pause_requested = False
            self._cancel_requested = False
            self.status = "queued"
            self.message = "等待开始"
            self.error = ""
            self.error_code = ""
            self.speed = 0.0
            self.eta = None

    def request_pause(self) -> bool:
        with self._lock:
            if self.status not in {"queued", "downloading", "preparing"}:
                return False
            self._pause_requested = True
            self._stop.set()
            if self.status in {"queued", "preparing"}:
                self.status = "paused"
                self.message = "已暂停"
            else:
                self.status = "pausing"
                self.message = "正在安全暂停…"
            self.speed = 0
            self.eta = None
            return True

    def request_cancel(self) -> bool:
        with self._lock:
            if self.status in {"completed", "cancelled", "error"}:
                return False
            self._cancel_requested = True
            self._stop.set()
            self.status = "cancelled"
            self.message = "已取消"
            self.speed = 0
            self.eta = None
            proc = self._process
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        return True

    def is_stopping(self) -> bool:
        return self._stop.is_set()

    def setup_streams(self, sizes: dict[str, int], existing: dict[str, int]) -> None:
        with self._lock:
            self._streams = {
                kind: {
                    "total": _perfect_size(size),
                    "downloaded": _perfect_size(existing.get(kind, 0)),
                }
                for kind, size in sizes.items()
            }
            self.total = sum(item["total"] for item in self._streams.values()) or self.approx_total
            self.received = sum(item["downloaded"] for item in self._streams.values())
            self._last_bytes = self.received
            if self.total > 0:
                self.progress = min(95.0, self.received / self.total * 95.0)
            self.speed = 0.0
            self.eta = None

    def add_progress(self, kind: str, amount: int) -> None:
        if amount <= 0:
            return
        with self._lock:
            state = self._streams.get(kind)
            if state is None:
                return
            state["downloaded"] += amount
            self.received += amount

    def set_stream_total(self, kind: str, total: int) -> None:
        with self._lock:
            state = self._streams.get(kind)
            if state is not None and total > 0:
                state["total"] = total
                self.total = sum(item["total"] for item in self._streams.values())

    def downloaded_total(self) -> int:
        with self._lock:
            return int(self.received)

    def update_speed(self, instant: float) -> None:
        with self._lock:
            if self.status not in {"downloading", "preparing"}:
                return
            if instant < 0:
                instant = 0
            if self.speed <= 0:
                self.speed = instant
            else:
                self.speed = self.speed * 0.55 + instant * 0.45
            if self.total > 0:
                remaining = max(0, self.total - self.received)
                self.eta = remaining / self.speed if self.speed > 1024 else None
                self.progress = min(95.0, 95.0 * self.received / self.total)
            if self.received > 0 and self.total > 0:
                self.progress = max(self.progress, 0.2)

    def set_downloading(self) -> None:
        with self._lock:
            if self._stop.is_set():
                return
            self.status = "downloading"
            self.message = f"{self.connections} 线程极速下载中"
            self.speed = 0.0

    def set_merging(self) -> None:
        with self._lock:
            self.status = "merging"
            self.message = "正在无损合并音视频…"
            self.speed = 0.0
            self.eta = None
            self.progress = max(95.0, self.progress)
            self.merge_progress = 0.0

    def set_merge_progress(self, value: float) -> None:
        with self._lock:
            self.merge_progress = max(0.0, min(1.0, value))
            self.progress = 95.0 + self.merge_progress * 4.6

    def mark_paused(self) -> None:
        with self._lock:
            if self._cancel_requested:
                return
            self.status = "paused"
            self.message = "已暂停，继续时会从断点恢复"
            self.speed = 0
            self.eta = None

    def mark_cancelled(self) -> None:
        with self._lock:
            self.status = "cancelled"
            self.message = "已取消"
            self.speed = 0
            self.eta = None

    def mark_error(self, message: str, code: str = "download_failed") -> None:
        with self._lock:
            self.status = "error"
            self.message = message
            self.error = message
            self.error_code = code
            self.speed = 0
            self.eta = None

    def mark_complete(self) -> None:
        with self._lock:
            self.status = "completed"
            self.progress = 100.0
            self.message = "下载完成"
            self.speed = 0
            self.eta = 0
            self.completed_at = utc_now_iso()
            try:
                self.output_size = self.output_path.stat().st_size
            except OSError:
                self.output_size = 0

    def set_message(self, message: str) -> None:
        with self._lock:
            self.message = message

    def public(self) -> dict:
        with self._lock:
            status = self.status
            payload = {
                "id": self.id,
                "title": self.title,
                "page_title": self.page_title,
                "page": self.page,
                "page_count": self.page_count,
                "cover": self.cover,
                "uploader": self.uploader,
                "bvid": self.bvid,
                "source_url": self.source_url,
                "quality_label": self.quality_label,
                "mode": self.mode,
                "status": status,
                "status_label": STATUS_LABELS.get(status, status),
                "progress": round(self.progress, 1),
                "received": int(self.received),
                "total": int(self.total),
                "speed": round(float(self.speed), 2),
                "eta": self.eta,
                "merge_progress": round(float(self.merge_progress), 3),
                "message": self.message,
                "error": self.error,
                "error_code": self.error_code,
                "duration": self.duration,
                "output_path": str(self.output_path) if status == "completed" else "",
                "output_name": self.output_path.name,
                "output_size": self.output_size,
                "cover_path": self.cover_path,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "completed_at": self.completed_at,
                "connections": self.connections,
                "can_pause": status in {"queued", "downloading", "preparing"},
                "can_resume": status == "paused",
                "can_cancel": status in {"queued", "preparing", "downloading", "pausing", "paused", "merging"},
                "can_retry": status == "error",
                "can_remove": status in {"completed", "cancelled", "error"},
            }
            return payload


class DownloadManager:
    def __init__(
        self,
        settings: SettingsStore,
        history: HistoryStore,
        cookie_provider: Callable[[], str] | None = None,
    ) -> None:
        self.settings = settings
        self.history = history
        self.cookie_provider = cookie_provider
        self._lock = threading.RLock()
        self._tasks: dict[str, DownloadTask] = {}
        self._reserved_outputs: set[str] = set()
        self._shutdown = threading.Event()
        self._semaphore = threading.BoundedSemaphore(
            max(1, min(4, int(self.settings.get().get("max_concurrent_downloads") or 2)))
        )
        self._ffmpeg = self._find_ffmpeg()
        self._cleanup_old_temp()

    @property
    def ffmpeg_path(self) -> Path | None:
        return self._ffmpeg

    @staticmethod
    def _find_ffmpeg() -> Path | None:
        candidates = []
        if config.FFMPEG_PATH.exists():
            candidates.append(config.FFMPEG_PATH)
        located = shutil.which("ffmpeg")
        if located:
            candidates.append(Path(located))
        for candidate in candidates:
            try:
                result = subprocess.run(
                    [str(candidate), "-version"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=8,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                if result.returncode == 0:
                    return candidate
            except (OSError, subprocess.SubprocessError):
                continue
        log.warning("ffmpeg was not found; merged downloads will fail")
        return None

    def _cleanup_old_temp(self) -> None:
        now = time.time()
        try:
            for entry in config.TEMP_DIR.iterdir():
                if not entry.is_dir():
                    continue
                try:
                    if now - entry.stat().st_mtime > 60 * 60 * 24 * 7:
                        shutil.rmtree(entry, ignore_errors=True)
                except OSError:
                    continue
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Task creation and output paths
    # ------------------------------------------------------------------
    def _allocate_output_path(self, plan: dict) -> Path:
        output_dir = self.settings.download_dir()
        output_dir.mkdir(parents=True, exist_ok=True)
        base = _safe_filename(str(plan.get("output_basename") or plan.get("title") or "Bili Video"))
        quality = str(plan.get("quality_label") or "")
        if plan.get("mode") != "audio" and quality:
            safe_quality = _safe_filename(quality.replace(" · ", " "), 48)
            base = f"{base} {safe_quality}"
        ext = "m4a" if plan.get("mode") == "audio" else "mp4"
        base = base[:170].rstrip(" .") or "Bili Video"
        with self._lock:
            candidate = output_dir / f"{base}.{ext}"
            index = 2
            while str(candidate).lower() in self._reserved_outputs or candidate.exists():
                candidate = output_dir / f"{base} ({index}).{ext}"
                index += 1
            self._reserved_outputs.add(str(candidate).lower())
        return candidate

    def create_task(self, plan: dict, connections: int | None = None) -> dict:
        if self._shutdown.is_set():
            raise TaskBusyError("应用正在关闭，无法创建新任务。")
        if not plan.get("streams"):
            raise TaskBusyError("没有可用的视频流，请重新解析。")
        output_path = self._allocate_output_path(plan)
        task_id = uuid.uuid4().hex[:12]
        workdir = config.TEMP_DIR / task_id
        task = DownloadTask(
            plan,
            output_path,
            workdir,
            connections or int(self.settings.get().get("connections") or 12),
            self._ffmpeg,
            self.cookie_provider,
        )
        task.id = task_id
        try:
            workdir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            with self._lock:
                self._reserved_outputs.discard(str(output_path).lower())
            raise TaskBusyError("无法创建下载缓存目录，请检查磁盘空间或权限。") from exc
        with self._lock:
            self._tasks[task.id] = task
        self._start_task_thread(task)
        return task.public()

    def _start_task_thread(self, task: DownloadTask) -> None:
        thread = threading.Thread(
            target=self._run_task,
            args=(task,),
            name=f"hwz-download-{task.id}",
            daemon=True,
        )
        task._thread = thread
        thread.start()

    def _task_by_id(self, task_id: str) -> DownloadTask:
        with self._lock:
            task = self._tasks.get(str(task_id))
        if not task:
            raise TaskBusyError("下载任务不存在或已被移除。")
        return task

    def list_tasks(self) -> list[dict]:
        with self._lock:
            tasks = list(self._tasks.values())
        tasks.sort(key=lambda task: task.created_at, reverse=True)
        return [task.public() for task in tasks]

    def get_task(self, task_id: str) -> dict:
        return self._task_by_id(task_id).public()

    def pause_task(self, task_id: str) -> dict:
        task = self._task_by_id(task_id)
        with task._lock:
            if task.status not in {"queued", "downloading", "preparing"}:
                raise TaskBusyError("当前状态的下载任务无法暂停。")
        task.request_pause()
        return task.public()

    def resume_task(self, task_id: str) -> dict:
        task = self._task_by_id(task_id)
        with task._lock:
            if task.status != "paused":
                raise TaskBusyError("只有已暂停的任务可以继续。")
            if task._thread and task._thread.is_alive():
                raise TaskBusyError("任务仍在暂停中，请稍后再试。")
            task._stop.clear()
            task._pause_requested = False
            task._cancel_requested = False
            task.status = "queued"
            task.message = "已加入队列，准备继续"
            task.error = ""
            task.error_code = ""
        self._start_task_thread(task)
        return task.public()

    def retry_task(self, task_id: str) -> dict:
        task = self._task_by_id(task_id)
        with task._lock:
            if task.status not in {"error", "cancelled"}:
                raise TaskBusyError("当前任务不需要重试。")
            if task._thread and task._thread.is_alive():
                raise TaskBusyError("任务正在清理，请稍后再试。")
            task.reset_for_run()
            task.progress = 0.0
            task.received = 0
            task.total = 0
            task._streams = {}
        self._start_task_thread(task)
        return task.public()

    def cancel_task(self, task_id: str) -> dict:
        task = self._task_by_id(task_id)
        task.request_cancel()
        return task.public()

    def remove_task(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(str(task_id))
            if not task:
                return False
            if task.status not in {"completed", "cancelled", "error"}:
                raise TaskBusyError("请先取消正在进行的任务。")
            self._tasks.pop(task.id, None)
            self._reserved_outputs.discard(str(task.output_path).lower())
        if task.status != "completed":
            shutil.rmtree(task.workdir, ignore_errors=True)
        return True

    def open_target(self, *, task_id: str | None = None, history_id: str | None = None, folder: bool = False) -> Path:
        path: Path | None = None
        if task_id:
            task = self._task_by_id(task_id)
            path = task.output_path if task.status == "completed" else task.output_path.parent
        elif history_id:
            item = self.history.get(history_id)
            if not item or not item.get("output_path"):
                raise TaskBusyError("找不到该下载记录。")
            path = Path(item["output_path"])
        else:
            path = self.settings.download_dir()
        if folder and path.is_file():
            path = path.parent
        if not path.exists() and not folder:
            raise TaskBusyError("文件已被移动或删除。")
        if not path.exists() and folder:
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise TaskBusyError("文件夹不存在，且无法创建。") from exc
        return path

    # ------------------------------------------------------------------
    # Runner
    # ------------------------------------------------------------------
    def _run_task(self, task: DownloadTask) -> None:
        acquired = False
        try:
            while not self._shutdown.is_set():
                if task.is_stopping():
                    if task._cancel_requested:
                        task.mark_cancelled()
                        self._cleanup_workdir(task)
                    else:
                        task.mark_paused()
                    return
                if self._semaphore.acquire(timeout=0.25):
                    acquired = True
                    break
            if not acquired:
                task.mark_cancelled()
                return
            if task.is_stopping():
                if task._cancel_requested:
                    task.mark_cancelled()
                else:
                    task.mark_paused()
                return
            task.begin()
            self._download_task(task)
            if task.status == "completed":
                self._record_history(task)
                if bool(self.settings.get().get("open_folder_when_done")):
                    try:
                        os.startfile(str(task.output_path.parent))  # type: ignore[attr-defined]
                    except OSError:
                        pass
        except PauseRequested:
            task.mark_paused()
            log.info("Task %s paused", task.id)
        except CancelRequested:
            task.mark_cancelled()
            self._cleanup_workdir(task)
            log.info("Task %s cancelled", task.id)
        except DownloadError as exc:
            if task._cancel_requested:
                task.mark_cancelled()
                self._cleanup_workdir(task)
            elif task._pause_requested:
                task.mark_paused()
            else:
                task.mark_error(exc.message, exc.code)
                self._remove_partial_output(task)
                log.warning("Task %s failed: %s", task.id, exc.message)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI and log
            if task._cancel_requested:
                task.mark_cancelled()
                self._cleanup_workdir(task)
            elif task._pause_requested:
                task.mark_paused()
            else:
                task.mark_error("下载出现异常，请重试或重新解析视频。", "unexpected_error")
                self._remove_partial_output(task)
                log.exception("Unexpected error in task %s", task.id)
        finally:
            task._monitor_thread = None
            if acquired:
                try:
                    self._semaphore.release()
                except ValueError:
                    pass

    def _download_task(self, task: DownloadTask) -> None:
        if task.mode == "video" and not task.ffmpeg_path:
            raise DownloadError("未找到 ffmpeg 合并组件，请重新安装完整版本。", "ffmpeg_missing")
        task.workdir.mkdir(parents=True, exist_ok=True)
        streams = list(task.plan.get("streams") or [])
        if not streams:
            raise DownloadError("下载流为空，请重新解析。", "empty_streams")

        # Probe every stream first so the progress bar has real totals rather
        # than a bitrate estimate. This also detects CDNs that ignore Range.
        prepared: list[dict[str, Any]] = []
        probe_errors: list[Exception] = []

        def prepare_stream(stream: dict) -> dict | None:
            if task.is_stopping():
                return None
            try:
                return self._prepare_stream(task, stream)
            except Exception as exc:  # noqa: BLE001
                probe_errors.append(exc)
                return None

        with ThreadPoolExecutor(max_workers=max(1, len(streams))) as pool:
            futures = [pool.submit(prepare_stream, stream) for stream in streams]
            for future in futures:
                prepared.append(future.result())
        if task.is_stopping():
            raise self._stop_exception(task)
        if probe_errors:
            exc = probe_errors[0]
            if isinstance(exc, DownloadError):
                raise exc
            raise DownloadError("下载流连接失败，请检查网络后重试。", "probe_failed")
        prepared = [item for item in prepared if item]
        if len(prepared) != len(streams):
            raise DownloadError("部分下载流不可用，请重新解析视频。", "probe_failed")

        sizes = {item["kind"]: item["total"] for item in prepared}
        existing = {item["kind"]: item["existing"] for item in prepared}
        task.setup_streams(sizes, existing)
        task.set_downloading()
        monitor = threading.Thread(target=self._monitor_progress, args=(task,), daemon=True)
        task._monitor_thread = monitor
        monitor.start()

        worker_errors: list[Exception] = []
        threads: list[threading.Thread] = []
        for stream_info in prepared:
            thread = threading.Thread(
                target=self._download_stream_guarded,
                args=(task, stream_info, worker_errors),
                name=f"hwz-stream-{task.id}-{stream_info['kind']}",
                daemon=True,
            )
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join()
        if task._monitor_thread is not None:
            task._monitor_thread = None
        try:
            monitor.join(timeout=2)
        except RuntimeError:
            pass

        if worker_errors:
            first = worker_errors[0]
            if isinstance(first, DownloadError):
                raise first
            raise DownloadError("下载连接中断，请重试。", "stream_failed")
        if task.is_stopping():
            raise self._stop_exception(task)

        # Verify every part before joining.
        outputs = {}
        for stream_info in prepared:
            outputs[stream_info["kind"]] = self._combine_stream(task, stream_info)
        if task.is_stopping():
            raise self._stop_exception(task)

        task.set_merging()
        final_temp = task.output_path.with_name(
            f".{task.id}.{_safe_filename(task.output_path.stem, 100)}.part{task.output_path.suffix}"
        )
        try:
            self._merge_outputs(task, outputs, final_temp)
            if task.is_stopping():
                raise self._stop_exception(task)
            os.replace(final_temp, task.output_path)
            task.progress = 99.8
            if task.save_cover and task.cover:
                self._save_cover(task)
            task.mark_complete()
            shutil.rmtree(task.workdir, ignore_errors=True)
        except Exception:
            try:
                final_temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _prepare_stream(self, task: DownloadTask, stream: dict) -> dict:
        fmt = stream.get("format") or {}
        url = str(fmt.get("url") or "")
        if not url:
            raise DownloadError("视频流地址为空，请重新解析。", "stream_unavailable")
        headers = self._download_headers(task, fmt)
        kind = str(stream.get("kind") or "video")
        approx = _perfect_size(fmt.get("filesize") or fmt.get("filesize_approx"))
        urls = [url]
        for backup in fmt.get("backup_urls") or []:
            backup = str(backup or "")
            if backup and backup not in urls:
                urls.append(backup)
        total, range_supported, usable_urls, error = self._probe_candidates(urls, headers)
        if error and total <= 0:
            raise error
        if total <= 0:
            total = approx
        if total <= 0:
            raise DownloadError("无法获取下载文件大小，请重新解析。", "unknown_size")
        stream_dir = task.workdir / kind
        manifest_path = stream_dir / "manifest.json"
        fingerprint = f"{kind}:{fmt.get('format_id') or ''}:{total}"
        old_manifest = config.read_json(manifest_path, {})
        mismatch = False
        if isinstance(old_manifest, dict) and old_manifest:
            mismatch = (
                int(old_manifest.get("total") or 0) != total
                or int(old_manifest.get("chunk_size") or 0) != CHUNK_SIZE
                or bool(old_manifest.get("range_supported")) != bool(range_supported)
                or str(old_manifest.get("fingerprint") or "") != fingerprint
            )
        if mismatch:
            shutil.rmtree(stream_dir, ignore_errors=True)
        stream_dir.mkdir(parents=True, exist_ok=True)
        config.atomic_write_json(
            manifest_path,
            {
                "total": total,
                "chunk_size": CHUNK_SIZE,
                "range_supported": bool(range_supported),
                "fingerprint": fingerprint,
                "kind": kind,
            },
        )
        if range_supported:
            existing = self._existing_part_bytes(stream_dir, total)
        else:
            single = stream_dir / "single.part"
            existing = single.stat().st_size if single.exists() else 0
            if existing >= total:
                existing = 0
                try:
                    single.unlink(missing_ok=True)
                except OSError:
                    pass
        return {
            "kind": kind,
            "format": fmt,
            "url": usable_urls[0],
            "urls": usable_urls,
            "headers": headers,
            "total": total,
            "range_supported": range_supported,
            "stream_dir": stream_dir,
            "existing": existing,
            "connections": self._connections_for(task, kind),
        }

    def _connections_for(self, task: DownloadTask, kind: str) -> int:
        if kind == "audio":
            return max(2, min(6, task.connections // 3))
        return task.connections

    def _download_headers(self, task: DownloadTask, fmt: dict) -> dict[str, str]:
        headers = {}
        for key, value in dict(fmt.get("http_headers") or {}).items():
            if value is not None:
                headers[str(key)] = str(value)
        headers.setdefault("User-Agent", USER_AGENT)
        headers.setdefault("Referer", "https://www.bilibili.com/")
        headers["Accept-Encoding"] = "identity"
        headers["Connection"] = "keep-alive"
        cookie = ""
        try:
            cookie = task.cookie_provider() if task.cookie_provider else ""
        except Exception:
            cookie = ""
        if cookie:
            headers["Cookie"] = cookie
        return headers

    def _probe_candidates(
        self, urls: list[str], headers: dict[str, str]
    ) -> tuple[int, bool, list[str], DownloadError | None]:
        last_error: DownloadError | None = None
        for index, candidate in enumerate(urls):
            total, range_supported, error = self._probe_stream(candidate, headers)
            if not error and total > 0:
                ordered = [candidate] + [item for item in urls if item != candidate]
                return total, range_supported, ordered, None
            if error:
                last_error = error
        return 0, False, urls, last_error or DownloadError(
            "所有下载线路均不可用，请稍后重试。", "all_mirrors_failed"
        )

    def _probe_stream(
        self, url: str, headers: dict[str, str]
    ) -> tuple[int, bool, DownloadError | None]:
        request_headers = dict(headers)
        request_headers["Range"] = "bytes=0-1"
        request_headers["Accept-Encoding"] = "identity"
        req = urlrequest.Request(url, headers=request_headers)
        try:
            with urlrequest.urlopen(req, timeout=30, context=_SSL_CONTEXT) as response:
                status = int(getattr(response, "status", 0) or 0)
                content_range = response.headers.get("Content-Range") or ""
                content_length = _perfect_size(response.headers.get("Content-Length"))
                if status == 206:
                    total = self._parse_content_range_total(content_range)
                    if total > 0:
                        return total, True, None
                    return content_length, True, None
                if status == 200:
                    return content_length, False, None
                return 0, False, DownloadError("下载服务器返回了异常状态。", "http_status")
        except urlerror.HTTPError as exc:
            if exc.code in {403, 401, 410}:
                return 0, False, DownloadError(
                    "下载链接已失效，请重新粘贴链接解析。", "url_expired"
                )
            log.warning("Probe HTTP error: %s", exc)
            return 0, False, DownloadError(
                f"下载服务器返回 HTTP {exc.code}，请稍后重试。", "http_error"
            )
        except (urlerror.URLError, TimeoutError, OSError) as exc:
            log.warning("Probe network error: %r", exc)
            return 0, False, DownloadError("连接下载服务器失败，请检查网络。", "network_error")

    @staticmethod
    def _parse_content_range_total(content_range: str) -> int:
        match = re.search(r"/(\d+)\s*$", content_range or "")
        if match:
            return _perfect_size(match.group(1))
        return 0

    def _chunk_bounds(self, total: int) -> list[tuple[int, int]]:
        return [
            (start, min(total, start + CHUNK_SIZE) - 1)
            for start in range(0, total, CHUNK_SIZE)
        ]

    def _existing_part_bytes(self, stream_dir: Path, total: int) -> int:
        parts = stream_dir.glob("*.part")
        bounds = self._chunk_bounds(total)
        downloaded = 0
        for part in parts:
            match = re.fullmatch(r"(\d{5})\.part", part.name)
            if not match:
                continue
            index = int(match.group(1))
            if index >= len(bounds):
                try:
                    part.unlink()
                except OSError:
                    pass
                continue
            start, end = bounds[index]
            expected = end - start + 1
            try:
                size = part.stat().st_size
            except OSError:
                continue
            if size > expected:
                try:
                    part.unlink()
                except OSError:
                    pass
                continue
            downloaded += min(size, expected)
        return downloaded

    def _download_stream_guarded(
        self, task: DownloadTask, stream_info: dict, errors: list[Exception]
    ) -> None:
        try:
            self._download_stream(task, stream_info)
        except TaskStopped:
            return
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            task._stop.set()

    def _download_stream(self, task: DownloadTask, stream_info: dict) -> None:
        if stream_info["range_supported"]:
            self._download_stream_ranged(task, stream_info)
        else:
            self._download_stream_single(task, stream_info)

    def _download_stream_ranged(self, task: DownloadTask, stream_info: dict) -> None:
        stream_dir: Path = stream_info["stream_dir"]
        total = int(stream_info["total"])
        bounds = self._chunk_bounds(total)
        chunk_queue: queue.Queue = queue.Queue()
        for index in range(len(bounds)):
            chunk_queue.put(index)
        errors: list[DownloadError] = []
        failed = threading.Event()
        workers = max(1, min(int(stream_info["connections"]), len(bounds)))

        def worker() -> None:
            while not task.is_stopping() and not failed.is_set():
                try:
                    index = chunk_queue.get_nowait()
                except queue.Empty:
                    return
                try:
                    self._download_chunk(task, stream_info, index, bounds[index])
                except TaskStopped:
                    return
                except DownloadError as exc:
                    errors.append(exc)
                    failed.set()
                    return
                except Exception as exc:  # noqa: BLE001
                    errors.append(DownloadError("分片下载失败，请重试。", "chunk_failed"))
                    failed.set()
                    log.exception("Chunk worker error: %s", exc)
                    return
                finally:
                    chunk_queue.task_done()

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if task.is_stopping():
            raise TaskStopped()
        if errors:
            raise errors[0]

    def _download_chunk(
        self,
        task: DownloadTask,
        stream_info: dict,
        index: int,
        bounds: tuple[int, int],
    ) -> None:
        start, end = bounds
        expected = end - start + 1
        part = stream_info["stream_dir"] / f"{index:05d}.part"
        urls = list(stream_info.get("urls") or [stream_info["url"]])
        for attempt in range(4):
            if task.is_stopping():
                raise TaskStopped()
            current = part.stat().st_size if part.exists() else 0
            if current > expected:
                try:
                    part.unlink()
                except OSError:
                    pass
                current = 0
            if current == expected:
                return
            headers = dict(stream_info["headers"])
            headers["Range"] = f"bytes={start + current}-{end}"
            candidate_url = urls[attempt % len(urls)]
            req = urlrequest.Request(candidate_url, headers=headers)
            try:
                with urlrequest.urlopen(req, timeout=35, context=_SSL_CONTEXT) as response:
                    status = int(getattr(response, "status", 0) or 0)
                    if status != 206:
                        raise DownloadError(
                            "下载服务器不支持分段传输，请重试。", "range_unsupported"
                        )
                    content_range = response.headers.get("Content-Range") or ""
                    start_match = re.match(r"bytes\s+(\d+)-", content_range)
                    if start_match and int(start_match.group(1)) != start + current:
                        raise DownloadError("分片校验失败，请重试。", "range_mismatch")
                    with open(part, "ab") as fh:
                        while not task.is_stopping():
                            data = response.read(READ_BLOCK)
                            if not data:
                                break
                            fh.write(data)
                            current += len(data)
                            task.add_progress(stream_info["kind"], len(data))
                    if task.is_stopping():
                        raise TaskStopped()
                current = part.stat().st_size if part.exists() else 0
                if current >= expected:
                    if current > expected:
                        with open(part, "r+b") as fh:
                            fh.truncate(expected)
                    return
                # A short body is retried using the bytes already stored.
                if attempt < 3:
                    time.sleep(0.35 * (attempt + 1))
                    continue
                raise DownloadError("分片数据不完整，请重试。", "chunk_incomplete")
            except TaskStopped:
                raise
            except urlerror.HTTPError as exc:
                if exc.code in {401, 403, 410, 404}:
                    raise DownloadError("下载链接已过期，请重新解析视频。", "url_expired") from exc
                if attempt >= 3:
                    raise DownloadError(
                        f"下载连接异常（HTTP {exc.code}），请稍后重试。", "http_error"
                    ) from exc
                time.sleep(0.5 * (attempt + 1))
            except (urlerror.URLError, TimeoutError, OSError) as exc:
                if task.is_stopping():
                    raise TaskStopped() from exc
                if attempt >= 3:
                    raise DownloadError("网络中断，下载未完成，请重试。", "network_error") from exc
                time.sleep(0.7 * (attempt + 1))
        raise DownloadError("分片下载失败，请重试。", "chunk_failed")

    def _download_stream_single(self, task: DownloadTask, stream_info: dict) -> None:
        stream_dir: Path = stream_info["stream_dir"]
        total = int(stream_info["total"])
        dest = stream_dir / "single.part"
        urls = list(stream_info.get("urls") or [stream_info["url"]])
        last_error: Exception | None = None
        for candidate_url in urls:
            if task.is_stopping():
                raise TaskStopped()
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
            headers = dict(stream_info["headers"])
            headers.pop("Range", None)
            req = urlrequest.Request(candidate_url, headers=headers)
            written = 0
            try:
                with urlrequest.urlopen(req, timeout=40, context=_SSL_CONTEXT) as response:
                    with open(dest, "wb") as fh:
                        while not task.is_stopping():
                            data = response.read(READ_BLOCK)
                            if not data:
                                break
                            fh.write(data)
                            written += len(data)
                            task.add_progress(stream_info["kind"], len(data))
            except (urlerror.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if task.is_stopping():
                    raise TaskStopped() from exc
                continue
            if task.is_stopping():
                raise TaskStopped()
            if written > 0 and (not total or written >= total):
                return
            last_error = DownloadError("下载数据不完整，请重试。", "stream_incomplete")
        raise DownloadError("下载连接中断，请重试。", "network_error") from last_error

    def _combine_stream(self, task: DownloadTask, stream_info: dict) -> Path:
        stream_dir: Path = stream_info["stream_dir"]
        fmt = stream_info["format"]
        ext = str(fmt.get("ext") or ("mp4" if stream_info["kind"] == "video" else "m4a"))
        target = task.workdir / f"{stream_info['kind']}.{ext}"
        temp = target.with_suffix(target.suffix + ".joining")
        if not stream_info["range_supported"]:
            single = stream_dir / "single.part"
            if not single.exists():
                raise DownloadError("下载缓存丢失，请重试。", "temp_missing")
            os.replace(single, target)
            return target
        bounds = self._chunk_bounds(int(stream_info["total"]))
        with open(temp, "wb") as out:
            for index, (start, end) in enumerate(bounds):
                if task.is_stopping():
                    raise TaskStopped()
                part = stream_dir / f"{index:05d}.part"
                expected = end - start + 1
                try:
                    actual = part.stat().st_size
                except OSError as exc:
                    raise DownloadError("下载缓存文件丢失，请重试。", "temp_missing") from exc
                if actual < expected:
                    raise DownloadError("下载缓存校验失败，请重试。", "temp_incomplete")
                with open(part, "rb") as fh:
                    remaining = expected
                    while remaining > 0:
                        data = fh.read(min(1024 * 1024, remaining))
                        if not data:
                            break
                        out.write(data)
                        remaining -= len(data)
                if remaining > 0:
                    raise DownloadError("下载缓存校验失败，请重试。", "temp_incomplete")
        os.replace(temp, target)
        if target.stat().st_size <= 0:
            raise DownloadError("合并下载流失败，请重试。", "combine_failed")
        try:
            shutil.rmtree(stream_dir, ignore_errors=True)
        except OSError:
            pass
        return target

    def _merge_outputs(self, task: DownloadTask, outputs: dict[str, Path], final_temp: Path) -> None:
        if not task.ffmpeg_path:
            raise DownloadError("未找到 ffmpeg，无法合并音视频。", "ffmpeg_missing")
        video = outputs.get("video")
        audio = outputs.get("audio")
        inputs: list[Path] = []
        if task.mode == "audio":
            if not audio:
                audio = video
            if not audio:
                raise DownloadError("没有可用的音频流。", "audio_unavailable")
            inputs = [audio]
        else:
            if not video:
                raise DownloadError("没有可用的视频流。", "video_unavailable")
            inputs = [video]
            if audio:
                inputs.append(audio)
        command = [
            str(task.ffmpeg_path),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-progress",
            "pipe:1",
        ]
        for item in inputs:
            command += ["-i", str(item)]
        if len(inputs) == 2:
            command += ["-map", "0:v:0", "-map", "1:a:0"]
        else:
            command += ["-map", "0"]
        command += ["-c", "copy"]
        if task.title:
            command += ["-metadata", f"title={task.title}"]
        if task.uploader:
            command += ["-metadata", f"artist={task.uploader}"]
        command += [
            "-movflags",
            "+faststart",
            str(final_temp),
        ]
        log.info("Merging with ffmpeg: %s", " ".join(command[:8]))
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        task._process = process
        try:
            if process.stdout:
                for line in process.stdout:
                    if task.is_stopping():
                        try:
                            process.terminate()
                        except OSError:
                            pass
                        raise self._stop_exception(task)
                    line = line.strip()
                    if line.startswith("out_time_us="):
                        try:
                            micros = int(line.split("=", 1)[1])
                        except (ValueError, IndexError):
                            continue
                        if task.duration > 0:
                            task.set_merge_progress(min(0.995, micros / 1_000_000 / task.duration))
                    elif line.startswith("out_time_ms="):
                        try:
                            millis = int(line.split("=", 1)[1])
                        except (ValueError, IndexError):
                            continue
                        if task.duration > 0:
                            task.set_merge_progress(min(0.995, millis / 1000 / task.duration))
            stderr = process.stderr.read() if process.stderr else ""
            returncode = process.wait(timeout=30)
        finally:
            task._process = None
        if returncode != 0 or not final_temp.exists() or final_temp.stat().st_size <= 0:
            log.warning("ffmpeg failed (%s): %s", returncode, (stderr or "")[-2000:])
            raise DownloadError("音视频合并失败，请检查磁盘空间后重试。", "merge_failed")
        task.set_merge_progress(1.0)
        task.progress = 99.8

    def _save_cover(self, task: DownloadTask) -> None:
        if not task.cover:
            return
        target = task.output_path.with_suffix(".jpg")
        if target.exists():
            task.cover_path = str(target)
            return
        try:
            data, _ = fetch_remote_image(task.cover)
            if not data:
                return
            temp = target.with_suffix(".jpg.tmp")
            with open(temp, "wb") as fh:
                fh.write(data)
            os.replace(temp, target)
            task.cover_path = str(target)
        except (BiliError, OSError):
            log.info("Cover download skipped for task %s", task.id)

    def _monitor_progress(self, task: DownloadTask) -> None:
        last_time = time.monotonic()
        last_bytes = task.downloaded_total()
        while task._monitor_thread is threading.current_thread():
            if task.status != "downloading":
                return
            time.sleep(0.55)
            now = time.monotonic()
            if now - last_time <= 0:
                continue
            current = task.downloaded_total()
            instant = max(0.0, (current - last_bytes) / (now - last_time))
            last_bytes = current
            last_time = now
            task.update_speed(instant)

    def _stop_exception(self, task: DownloadTask) -> TaskStopped:
        if task._cancel_requested or task.status == "cancelled":
            return CancelRequested()
        return PauseRequested()

    def _cleanup_workdir(self, task: DownloadTask) -> None:
        shutil.rmtree(task.workdir, ignore_errors=True)

    def _remove_partial_output(self, task: DownloadTask) -> None:
        try:
            for item in task.output_path.parent.glob(f".{task.id}.*.part*"):
                item.unlink(missing_ok=True)
        except OSError:
            pass

    def _record_history(self, task: DownloadTask) -> None:
        try:
            self.history.add(
                {
                    "id": task.id,
                    "bvid": task.bvid,
                    "title": task.title,
                    "page_title": task.page_title,
                    "page": task.page,
                    "page_count": task.page_count,
                    "cover": task.cover,
                    "uploader": task.uploader,
                    "source_url": task.source_url,
                    "quality_label": task.quality_label,
                    "mode": task.mode,
                    "duration": task.duration,
                    "output_path": str(task.output_path),
                    "output_name": task.output_path.name,
                    "cover_path": task.cover_path,
                    "file_size": task.output_size,
                    "created_at": task.created_at,
                    "completed_at": task.completed_at or utc_now_iso(),
                }
            )
        except Exception:
            log.exception("Failed to write download history")

    def shutdown(self, timeout: float = 4.0) -> None:
        self._shutdown.set()
        with self._lock:
            tasks = list(self._tasks.values())
        for task in tasks:
            if task.status not in {"completed", "error", "cancelled"}:
                task.request_cancel()
                proc = task._process
                if proc and proc.poll() is None:
                    try:
                        proc.terminate()
                    except OSError:
                        pass
        deadline = time.monotonic() + max(0.5, timeout)
        for task in tasks:
            thread = task._thread
            if thread and thread.is_alive():
                thread.join(timeout=max(0.1, deadline - time.monotonic()))
