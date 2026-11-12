from __future__ import annotations

import json
import os
import sys
from pathlib import Path


APP_NAME = "HWZ Bili 工具箱"
APP_VERSION = "1.1.0"
APP_ID = "HWZ-Bili-Downloader"
ORGANIZATION = "HWZ Studio"


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _resource_root() -> Path:
    """Root directory for files bundled with the application."""
    if _is_frozen():
        # PyInstaller one-file / one-dir both expose bundled data here.
        base = getattr(sys, "_MEIPASS", None)
        if base:
            return Path(base)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


RESOURCE_DIR = _resource_root()
WEB_DIR = RESOURCE_DIR / "web"
VENDOR_DIR = RESOURCE_DIR / "vendor"
FFMPEG_PATH = VENDOR_DIR / "bin" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")


def _default_data_dir() -> Path:
    override = os.environ.get("HWZ_BILI_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return base / "HWZ-Bili-Downloader"
    return Path.home() / ".local" / "share" / "hwz-bili-downloader"


DATA_DIR = _default_data_dir()
CACHE_DIR = DATA_DIR / "cache"
IMAGE_CACHE_DIR = CACHE_DIR / "images"
COVER_CACHE_DIR = CACHE_DIR / "covers"
TEMP_DIR = CACHE_DIR / "downloads"
PLAYER_DIR = CACHE_DIR / "player"
YTDLP_CACHE_DIR = CACHE_DIR / "yt-dlp"
LOG_DIR = DATA_DIR / "logs"
SETTINGS_FILE = DATA_DIR / "settings.json"
HISTORY_FILE = DATA_DIR / "history.json"
ACCOUNT_FILE = DATA_DIR / "account.json"
COOKIES_FILE = DATA_DIR / "cookies.json"
COOKIES_NETSCAPE_FILE = DATA_DIR / "cookies.txt"

APP_TITLE = "HWZ Bili 工具箱"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)


def ensure_runtime_dirs() -> None:
    for path in (
        DATA_DIR,
        CACHE_DIR,
        IMAGE_CACHE_DIR,
        COVER_CACHE_DIR,
        TEMP_DIR,
        PLAYER_DIR,
        YTDLP_CACHE_DIR,
        LOG_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)


def default_download_dir() -> Path:
    downloads = Path.home() / "Downloads"
    if not downloads.exists():
        downloads = Path.home() / "下载"
    if not downloads.exists():
        downloads = Path.home()
    return downloads / "HWZ Bili下载器"


DEFAULT_SETTINGS: dict = {
    "download_dir": str(default_download_dir()),
    "connections": 12,
    "save_cover": False,
    "open_folder_when_done": False,
    "auto_download": True,
    "theme": "light",
    "prefer_codec": "avc",
    "max_concurrent_downloads": 2,
}


def atomic_write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read_json(path: Path, fallback: object) -> object:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return fallback


def human_bytes(num: float | int | None) -> str:
    if not num or num <= 0:
        return "0 B"
    num = float(num)
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while num >= 1024 and idx < len(units) - 1:
        num /= 1024
        idx += 1
    if idx == 0:
        return f"{int(num)} {units[idx]}"
    return f"{num:.1f} {units[idx]}"
