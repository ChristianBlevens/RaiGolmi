@echo off
rem  Builds ONLY the machine's disk, for bare metal or a VM of your own (build-disk.ps1).
rem  For RaiGolmi in a window on Windows, run build.bat instead.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build-disk.ps1"
pause
