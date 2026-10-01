@echo off
rem  Installs RaiGolmi from its published release, or updates it (setup.ps1).
rem  To build it from this checkout instead, run build.bat.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
pause
