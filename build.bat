@echo off
rem  Builds RaiGolmi in a window on Windows: the launcher and its disk (build.ps1).
rem  For the disk alone, for bare metal or a VM of your own, run build-disk.bat instead.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1"
pause
