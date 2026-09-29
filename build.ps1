#  RaiGolmi in a window on Windows: builds the launcher (windows\RaiGolmi.exe) and the
#  machine's disk (disk\raigolmi.qcow2), which the launcher then boots, or upgrades that disk
#  in place when it is already there. Run build.bat; delete the disk first for a fresh one.
#  For the disk alone (bare metal, or a VM of your own), run build-disk.bat; for the
#  launcher alone, keeping the disk, run build-launcher.bat.
. (Join-Path $PSScriptRoot 'build-common.ps1')

$disk   = Join-Path $disks 'raigolmi.qcow2'
$record = Join-Path $env:LOCALAPPDATA 'RaiGolmi\disk.txt'

Install-Missing (@(Launcher-Prerequisites) + @(Disk-Prerequisites)) 'build.bat'

Build-Launcher
$upgrade = Test-Path $disk
if ($upgrade) { Build-Upgrade $disk } else { Build-Disk 'qcow2' $disk }

New-Item -ItemType Directory -Force (Split-Path $record) | Out-Null
Set-Content -Path $record -Value $disk -NoNewline

New-Shortcut
if ($upgrade) {
    Apply-Upgrade (Upgrade-Archive $disk)
    Write-Host 'Upgraded. RaiGolmi is starting on the new image, with everything on it kept.'
} else {
    Write-Host "Built. Run RaiGolmi.lnk here, or drag it wherever you like."
}
