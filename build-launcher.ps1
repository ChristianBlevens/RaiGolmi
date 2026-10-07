#  The launcher alone (windows\RaiGolmi.exe), for a change to the window: the disk is not
#  touched, and the launcher boots the one it recorded. Run build-launcher.bat. For the
#  launcher and a new disk, run build.bat instead.
. (Join-Path $PSScriptRoot 'build-common.ps1')

Install-Missing { Launcher-Prerequisites } 'build-launcher.bat'
Build-Launcher
Open-RaiGolmi 'Built the launcher.'

