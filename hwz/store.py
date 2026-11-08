from __future__ import annotations

import copy
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clamp_connections(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return int(config.DEFAULT_SETTINGS["connections"])
    return max(4, min(24, number))


class SettingsStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data = copy.deepcopy(config.DEFAULT_SETTINGS)
        self.reload()

    def reload(self) -> None:
        loaded = config.read_json(config.SETTINGS_FILE, {})
        if not isinstance(loaded, dict):
            loaded = {}
        with self._lock:
            merged = copy.deepcopy(config.DEFAULT_SETTINGS)
            merged.update({k: v for k, v in loaded.items() if k in config.DEFAULT_SETTINGS})
            merged["connections"] = _clamp_connections(merged.get("connections"))
            merged["max_concurrent_downloads"] = max(
                1, min(4, int(merged.get("max_concurrent_downloads") or 2))
            )
            if merged.get("theme") not in {"light", "dark", "system"}:
                merged["theme"] = "light"
            download_dir = str(merged.get("download_dir") or "").strip()
            merged["download_dir"] = download_dir or str(config.default_download_dir())
            self._data = merged
        self._ensure_download_dir()

    def _ensure_download_dir(self) -> None:
        try:
            Path(self._data["download_dir"]).expanduser().mkdir(parents=True, exist_ok=True)
        except OSError:
            fallback = str(config.default_download_dir())
            try:
                Path(fallback).mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            with self._lock:
                self._data["download_dir"] = fallback

    def get(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    def update(self, patch: dict) -> dict:
        if not isinstance(patch, dict):
            return self.get()
        allowed = set(config.DEFAULT_SETTINGS)
        with self._lock:
            next_data = copy.deepcopy(self._data)
            for key, value in patch.items():
                if key not in allowed:
                    continue
                next_data[key] = value

            next_data["connections"] = _clamp_connections(next_data.get("connections"))
            try:
                next_data["max_concurrent_downloads"] = max(
                    1, min(4, int(next_data.get("max_concurrent_downloads") or 2))
                )
            except (TypeError, ValueError):
                next_data["max_concurrent_downloads"] = 2
            if next_data.get("theme") not in {"light", "dark", "system"}:
                next_data["theme"] = "light"
            for boolean_key in (
                "save_cover",
                "open_folder_when_done",
                "auto_download",
            ):
                next_data[boolean_key] = bool(next_data.get(boolean_key))

            new_dir = str(next_data.get("download_dir") or "").strip()
            if new_dir:
                try:
                    expanded = Path(os.path.expandvars(new_dir)).expanduser()
                    expanded.mkdir(parents=True, exist_ok=True)
                    if not expanded.is_dir():
                        raise OSError("not a directory")
                    next_data["download_dir"] = str(expanded.resolve())
                except OSError:
                    # Keep the previous known-good directory rather than saving
                    # a path that cannot receive files.
                    next_data["download_dir"] = self._data["download_dir"]
            else:
                next_data["download_dir"] = self._data["download_dir"]

            self._data = next_data
            config.atomic_write_json(config.SETTINGS_FILE, self._data)
            return copy.deepcopy(self._data)

    def download_dir(self) -> Path:
        with self._lock:
            return Path(self._data["download_dir"])


class HistoryStore:
    def __init__(self, limit: int = 300) -> None:
        self._lock = threading.RLock()
        self._limit = limit
        self._items: list[dict] = []
        self.reload()

    def reload(self) -> None:
        loaded = config.read_json(config.HISTORY_FILE, [])
        items = loaded if isinstance(loaded, list) else []
        cleaned: list[dict] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            if not item.get("created_at"):
                item["created_at"] = utc_now_iso()
            cleaned.append(item)
        cleaned.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
        with self._lock:
            self._items = cleaned[: self._limit]

    def _save_locked(self) -> None:
        config.atomic_write_json(config.HISTORY_FILE, self._items[: self._limit])

    def add(self, entry: dict) -> dict:
        record = dict(entry)
        record.setdefault("id", uuid.uuid4().hex)
        record.setdefault("created_at", utc_now_iso())
        with self._lock:
            self._items.insert(0, record)
            self._items = self._items[: self._limit]
            self._save_locked()
        return dict(record)

    def list(self) -> list[dict]:
        with self._lock:
            items = [dict(item) for item in self._items]
        for item in items:
            output = item.get("output_path")
            item["exists"] = bool(output and Path(output).is_file())
        return items

    def get(self, item_id: str) -> dict | None:
        with self._lock:
            for item in self._items:
                if item.get("id") == item_id:
                    result = dict(item)
                    break
            else:
                return None
        output = result.get("output_path")
        result["exists"] = bool(output and Path(output).is_file())
        return result

    def delete(self, item_id: str) -> bool:
        with self._lock:
            before = len(self._items)
            self._items = [item for item in self._items if item.get("id") != item_id]
            changed = len(self._items) != before
            if changed:
                self._save_locked()
            return changed

    def clear(self) -> None:
        with self._lock:
            self._items = []
            self._save_locked()

    def count(self) -> int:
        with self._lock:
            return len(self._items)


class DownloadStats:
    """Tiny aggregate used by the sidebar and dashboard."""

    def __init__(self, history: HistoryStore) -> None:
        self.history = history

    def summary(self, active_tasks: list[dict]) -> dict:
        active = [t for t in active_tasks if t.get("status") in {"downloading", "merging", "queued", "paused"}]
        speed = sum(float(t.get("speed") or 0) for t in active_tasks if t.get("status") == "downloading")
        return {
            "active": len(active),
            "speed": speed,
            "history_count": self.history.count(),
            "today_completed": self._today_completed(),
        }

    def _today_completed(self) -> int:
        from datetime import date

        today = date.today().isoformat()
        count = 0
        for item in self.history.list():
            created = str(item.get("completed_at") or item.get("created_at") or "")
            if created.startswith(today):
                count += 1
        return count
