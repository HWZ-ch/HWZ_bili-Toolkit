# Build and package HWZ Bili Toolkit with PyInstaller.
# Run from the repository root:
#   powershell -ExecutionPolicy Bypass -File .\build.ps1
# Set $env:PYTHON to an explicit Python executable if "python" is not on PATH.

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$Python = if ($env:PYTHON) { $env:PYTHON } else { "python" }

if (-not (Test-Path ".\vendor\bin\ffmpeg.exe")) {
    Write-Host "Fetching FFmpeg via imageio-ffmpeg..." -ForegroundColor Cyan
    & $Python -c "import imageio_ffmpeg, shutil, pathlib; pathlib.Path('vendor/bin').mkdir(parents=True, exist_ok=True); shutil.copy2(imageio_ffmpeg.get_ffmpeg_exe(), 'vendor/bin/ffmpeg.exe')"
    if ($LASTEXITCODE -ne 0) { throw "Could not fetch FFmpeg." }
}

& $Python -m PyInstaller .\HWZ-Bili.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed."
}

$built = Get-ChildItem -LiteralPath ".\dist" -Filter "*.exe" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if (-not $built) {
    throw "Build artifact not found in .\dist"
}

$destination = Join-Path $PSScriptRoot $built.Name
Copy-Item -LiteralPath $built.FullName -Destination $destination -Force
Write-Host ""
Write-Host "Build complete:" -ForegroundColor Green
Get-Item -LiteralPath $destination | Select-Object FullName, Length, LastWriteTime
