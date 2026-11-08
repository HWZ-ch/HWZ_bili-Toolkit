# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build definition for HWZ Bili Downloader."""

from PyInstaller.utils.hooks import collect_all, collect_data_files

datas = [("web", "web")]
binaries = [("vendor/bin/ffmpeg.exe", "vendor/bin")]
hiddenimports = [
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "clr_loader",
    "pythonnet",
    "pythonnet.runtime",
    "cffi",
    "certifi",
    "segno",
]

for package in ("yt_dlp", "webview", "pythonnet", "clr_loader"):
    try:
        package_datas, package_binaries, package_hidden = collect_all(package)
        datas += package_datas
        binaries += package_binaries
        hiddenimports += package_hidden
    except Exception as exc:  # pragma: no cover - build-time diagnostic
        print(f"warning: collect_all({package}) failed: {exc}")

datas += collect_data_files("certifi")

a = Analysis(
    ["app.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "numpy",
        "pandas",
        "matplotlib",
        "PIL",
        "scipy",
        "lxml",
        "openpyxl",
        "pytest",
        "IPython",
        "notebook",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "gtk",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="HWZ-Bili下载器",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="web/assets/favicon.ico",
    version="version_info.txt",
)
