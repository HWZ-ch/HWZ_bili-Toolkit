@echo off
setlocal
cd /d "%~dp0"
for %%F in ("%~dp0HWZ-Bili*.exe") do (
    start "" "%%~fF"
    exit /b 0
)
if exist "%~dp0dist" (
    for %%F in ("%~dp0dist\*.exe") do (
        start "" "%%~fF"
        exit /b 0
    )
)
echo HWZ Bili Downloader executable was not found.
pause
