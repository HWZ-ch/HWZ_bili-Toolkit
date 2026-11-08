# 第三方组件与许可声明

HWZ Bili 下载器包含或依赖以下第三方组件。各组件的版权与许可归其各自作者所有。

## FFmpeg

- 文件：`vendor/bin/ffmpeg.exe`
- 版本：9.0.2 essentials build（Gyan.dev，启用 GPL 与 version3）
- 用途：以 `-c copy` 方式封装/合并 B 站 DASH 音视频流，不进行转码。
- 许可：GNU General Public License v3（全文见 `vendor/licenses/GPL-3.0.txt`）。
- 项目主页：https://ffmpeg.org/
- 源码与构建说明：https://ffmpeg.org/download.html
- Gyan.dev 构建：https://www.gyan.dev/ffmpeg/builds/

FFmpeg 是独立的可执行程序，应用通过命令行调用它。

## yt-dlp

- 版本：2026.10.26
- 用途：作为 B 站接口变动时的备用解析引擎。
- 许可：Unlicense（公共领域）。
- 项目主页：https://github.com/yt-dlp/yt-dlp

## pywebview

- 版本：6.2.1
- 用途：Windows 原生窗口与 WebView2 界面容器。
- 许可：BSD 3-Clause。
- 项目主页：https://pywebview.flowrl.com/

## pythonnet / clr-loader

- 许可：MIT。
- 项目主页：https://github.com/pythonnet/pythonnet

## Segno

- 用途：生成 B 站登录二维码 SVG。
- 许可：BSD。
- 项目主页：https://github.com/heuer/segno

## certifi

- 用途：HTTPS CA 证书包。
- 许可：Mozilla Public License 2.0。
- 项目主页：https://github.com/certifi/python-certifi

## Python / PyInstaller

- Python：PSF License。
- PyInstaller：GPLv2-or-later，附有允许打包专有程序的特殊例外。
- 项目主页：https://www.python.org/ ，https://pyinstaller.org/

## 字体

- Manrope：SIL Open Font License 1.1，https://github.com/sharanda/manrope

## 免责声明

本应用与哔哩哔哩（Bilibili）官方没有关联。B 站名称、图标及相关内容的权利归其权利人所有。
请仅将本工具用于个人学习与本地收藏，遵守平台条款与当地法律。
