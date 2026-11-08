# vendor/bin

`ffmpeg.exe` is intentionally **not committed** to this repository because the
static build is larger than GitHub's per-file limit.

To build the application, run the included build script from the repository root:

```powershell
python -m pip install -r requirements-build.txt
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

`build.ps1` downloads a compatible FFmpeg binary via `imageio-ffmpeg` and places
it at `vendor/bin/ffmpeg.exe` before PyInstaller packaging.

FFmpeg is licensed under GPLv3 for the bundled build. See
`vendor/licenses/GPL-3.0.txt` and the project's `THIRD_PARTY_NOTICES.md`.
