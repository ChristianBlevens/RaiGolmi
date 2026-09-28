@echo off
rem  Builds ONLY the launcher, keeping the disk it boots (build-launcher.ps1).
rem  For the launcher and a new disk, run build.bat instead.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build-launcher.ps1"
pause
