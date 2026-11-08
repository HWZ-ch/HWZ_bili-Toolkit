from __future__ import annotations

import argparse
import ctypes
import logging
import os
import sys
import threading
import time
import traceback
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

from hwz import config
from hwz.server import Application, ServerController

log = logging.getLogger("hwz")


def setup_logging(debug: bool = False) -> None:
    config.ensure_runtime_dirs()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        file_handler = RotatingFileHandler(
            config.LOG_DIR / "app.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.INFO)
        root.addHandler(file_handler)
    except OSError:
        pass
    if debug and sys.stderr:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        stream.setLevel(logging.DEBUG)
        root.addHandler(stream)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("yt_dlp").setLevel(logging.WARNING)


def setup_windows() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "HWZStudio.BiliDownloader.1"
        )
    except Exception:
        pass
    try:
        # Crisp text on high-DPI screens; pywebview/WebView2 inherits this.
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def _fatal_message(text: str) -> None:
    log.error(text)
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(
                0,
                text,
                config.APP_NAME,
                0x00000010,  # MB_ICONERROR
            )
            return
        except Exception:
            pass
    try:
        print(text, file=sys.stderr)
    except Exception:
        pass


class WindowApi:
    """Small native bridge exposed to the web UI as window.pywebview.api."""

    def __init__(self) -> None:
        self._window = None
        self._closing = False
        self._hwnd: int | None = None
        self._normal_rect: tuple[int, int, int, int] | None = None

    def _find_hwnd(self) -> int | None:
        if os.name != "nt":
            return None
        if self._hwnd:
            try:
                if ctypes.windll.user32.IsWindow(self._hwnd):
                    return self._hwnd
            except Exception:
                pass
            self._hwnd = None
        try:
            import ctypes.wintypes as wintypes

            user32 = ctypes.windll.user32
            pid = os.getpid()
            found: list[int] = []
            callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

            def callback(hwnd, _lparam):
                process_id = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
                if process_id.value != pid or not user32.IsWindowVisible(hwnd):
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                title = ""
                if length:
                    buffer = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buffer, length + 1)
                    title = buffer.value
                if title == config.APP_TITLE:
                    found.append(int(hwnd))
                    return False
                return True

            user32.EnumWindows(callback_type(callback), 0)
            if found:
                self._hwnd = found[0]
                return self._hwnd
        except Exception:
            log.debug("Could not find native window handle", exc_info=True)
        return None

    def minimize(self) -> bool:
        if self._window:
            self._window.minimize()
            return True
        return False

    def _center_window(self) -> None:
        if os.name != "nt":
            return
        hwnd = self._find_hwnd()
        if not hwnd:
            return
        try:
            import ctypes.wintypes as wintypes

            user32 = ctypes.windll.user32

            class RECT(ctypes.Structure):
                _fields_ = [
                    ("left", ctypes.c_long),
                    ("top", ctypes.c_long),
                    ("right", ctypes.c_long),
                    ("bottom", ctypes.c_long),
                ]

            current = RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(current))
            width = max(1, current.right - current.left)
            height = max(1, current.bottom - current.top)
            work = RECT()
            if not user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0):
                return
            work_w = work.right - work.left
            work_h = work.bottom - work.top
            width = min(width, max(900, work_w - 120))
            height = min(height, max(640, work_h - 100))
            x = work.left + max(0, (work_w - width) // 2)
            y = work.top + max(0, (work_h - height) // 2)
            user32.SetWindowPos(hwnd, 0, x, y, width, height, 0x0040 | 0x0004)
            log.info("Window fitted to %s,%s %sx%s", x, y, width, height)
        except Exception:
            log.debug("Initial window fitting failed", exc_info=True)

    def toggle_maximize(self) -> bool:
        if os.name == "nt":
            hwnd = self._find_hwnd()
            log.info("toggle_maximize requested hwnd=%s", hwnd)
            if hwnd:
                try:
                    import ctypes.wintypes as wintypes

                    user32 = ctypes.windll.user32

                    class RECT(ctypes.Structure):
                        _fields_ = [
                            ("left", ctypes.c_long),
                            ("top", ctypes.c_long),
                            ("right", ctypes.c_long),
                            ("bottom", ctypes.c_long),
                        ]

                    if self._normal_rect is not None:
                        x, y, width, height = self._normal_rect
                        user32.SetWindowPos(
                            hwnd, 0, x, y, width, height, 0x0040 | 0x0004
                        )
                        self._normal_rect = None
                        log.info("Window restored to %s,%s %sx%s", x, y, width, height)
                    else:
                        current = RECT()
                        user32.GetWindowRect(hwnd, ctypes.byref(current))
                        self._normal_rect = (
                            current.left,
                            current.top,
                            max(1, current.right - current.left),
                            max(1, current.bottom - current.top),
                        )
                        work = RECT()
                        if not user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0):
                            work = RECT(0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
                        width = max(960, work.right - work.left)
                        height = max(640, work.bottom - work.top)
                        user32.SetWindowPos(
                            hwnd, 0, work.left, work.top, width, height, 0x0040 | 0x0004
                        )
                        log.info("Window expanded to %s,%s %sx%s", work.left, work.top, width, height)
                    return True
                except Exception:
                    log.debug("Native maximize failed", exc_info=True)
        if not self._window:
            return False
        try:
            from webview.window import State

            if self._window.state == State.MAXIMIZED:
                self._window.restore()
            else:
                self._window.maximize()
            return True
        except Exception:
            log.debug("Maximize toggle failed", exc_info=True)
            return False

    def close(self) -> bool:
        self._closing = True
        if self._window:
            self._window.destroy()
        return True

    def choose_folder(self, current: str = "") -> str:
        if not self._window:
            return ""
        try:
            import webview

            result = self._window.create_file_dialog(
                webview.FOLDER_DIALOG,
                directory=current or "",
                allow_multiple=False,
            )
            if result:
                return str(result[0])
        except Exception:
            log.exception("Folder picker failed")
        return ""

    def get_clipboard(self) -> str:
        """Read Unicode text from the Windows clipboard for the paste button."""
        if os.name != "nt":
            return ""
        try:
            import ctypes

            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            user32.OpenClipboard.argtypes = [ctypes.c_void_p]
            user32.OpenClipboard.restype = ctypes.c_bool
            user32.GetClipboardData.argtypes = [ctypes.c_uint]
            user32.GetClipboardData.restype = ctypes.c_void_p
            kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
            kernel32.GlobalLock.restype = ctypes.c_void_p
            kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
            if not user32.OpenClipboard(None):
                return ""
            try:
                handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
                if not handle:
                    return ""
                pointer = kernel32.GlobalLock(handle)
                if not pointer:
                    return ""
                try:
                    text = ctypes.wstring_at(pointer)
                finally:
                    kernel32.GlobalUnlock(handle)
                return str(text or "")[:300000]
            finally:
                user32.CloseClipboard()
        except Exception:
            log.debug("Clipboard read failed", exc_info=True)
            return ""

    def get_platform(self) -> str:
        return sys.platform

    def get_version(self) -> str:
        return config.APP_VERSION


def _screen_size() -> tuple[int, int]:
    if os.name == "nt":
        try:
            user32 = ctypes.windll.user32

            class RECT(ctypes.Structure):
                _fields_ = [
                    ("left", ctypes.c_long),
                    ("top", ctypes.c_long),
                    ("right", ctypes.c_long),
                    ("bottom", ctypes.c_long),
                ]

            rect = RECT()
            if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                return rect.right - rect.left, rect.bottom - rect.top
        except Exception:
            pass
    return 1366, 900


def run_desktop(server: ServerController, app: Application, debug: bool = False) -> None:
    import webview

    work_w, work_h = _screen_size()
    width = max(1080, min(1360, work_w - 80))
    height = max(700, min(880, work_h - 60))
    background = "#f5f6fa"
    try:
        theme = app.settings.get().get("theme")
        if theme == "dark":
            background = "#0f111a"
        elif theme == "system":
            background = "#0f111a"
    except Exception:
        pass

    api = WindowApi()
    window = webview.create_window(
        config.APP_TITLE,
        server.url(),
        js_api=api,
        width=width,
        height=height,
        min_size=(1040, 690),
        resizable=True,
        frameless=True,
        easy_drag=False,
        shadow=True,
        background_color=background,
        text_select=True,
        zoomable=False,
        confirm_close=False,
    )
    if window is None:
        raise RuntimeError("窗口创建失败")
    api._window = window

    closed = threading.Event()

    def on_closed() -> None:
        if not closed.is_set():
            closed.set()
            log.info("Window closed, shutting down")
            try:
                server.stop()
            except Exception:
                log.exception("Shutdown failed")

    def on_loaded() -> None:
        log.info("UI loaded")
        try:
            api._center_window()
        except Exception:
            log.debug("Window centering failed", exc_info=True)

    try:
        window.events.closed += on_closed
        window.events.loaded += on_loaded
    except Exception:
        log.debug("Could not bind window events", exc_info=True)

    webview.start(
        debug=debug,
        gui="edgechromium",
        private_mode=False,
        storage_path=str(config.DATA_DIR / "webview"),
    )
    if not closed.is_set():
        closed.set()
        try:
            server.stop()
        except Exception:
            log.exception("Shutdown failed")


def run_browser(server: ServerController, app: Application, no_window: bool) -> None:
    url = server.url()
    print(f"HWZ Bili 下载器已启动：{url}", flush=True)
    if not no_window:
        try:
            webbrowser.open(url)
        except Exception:
            log.exception("Could not open browser")
    stop_event = threading.Event()
    try:
        while not stop_event.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=config.APP_NAME)
    parser.add_argument("--browser", action="store_true", help="在默认浏览器中打开界面")
    parser.add_argument("--no-window", action="store_true", help="仅启动本地服务")
    parser.add_argument("--port", type=int, default=0, help="本地服务端口（0 表示自动）")
    parser.add_argument("--debug", action="store_true", help="输出调试日志")
    args = parser.parse_args(argv)

    setup_logging(args.debug)
    setup_windows()
    log.info("Starting %s %s (frozen=%s)", config.APP_NAME, config.APP_VERSION, getattr(sys, "frozen", False))
    log.info("Resources: %s", config.RESOURCE_DIR)
    log.info("Data: %s", config.DATA_DIR)

    try:
        app = Application()
        server = ServerController(app, port=max(0, int(args.port or 0)))
        server.start()
        log.info("Local UI: %s", server.url())
    except Exception as exc:
        log.exception("Could not start application")
        _fatal_message(f"启动失败：{exc}\n\n请查看日志：{config.LOG_DIR / 'app.log'}")
        return 1

    if args.browser or args.no_window:
        run_browser(server, app, args.no_window)
        return 0

    try:
        run_desktop(server, app, debug=args.debug)
    except Exception as exc:
        log.exception("Desktop window failed, falling back to browser")
        try:
            server.app.bili  # ensure app alive
        except Exception:
            pass
        try:
            opened = webbrowser.open(server.url())
        except Exception:
            opened = False
        if not opened:
            _fatal_message(
                "无法创建应用窗口。\n\n"
                f"请尝试在浏览器中打开：\n{server.url()}\n\n"
                f"日志：{config.LOG_DIR / 'app.log'}"
            )
            server.stop()
            return 1
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

