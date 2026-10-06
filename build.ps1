#  RaiGolmi in a window on Windows, built from this checkout: the launcher
#  (windows\RaiGolmi.exe) and the machine's disk (disk\raigolmi.qcow2), which the launcher then
#  boots, or that disk upgraded in place when it is already there. setup.bat downloads the
#  published ones instead; this is for a change to the code. Delete the disk first for a fresh
#  one. For the disk alone (bare metal, or a VM of your own), run build-disk.bat; for the
#  launcher alone, keeping the disk, run build-launcher.bat.
. (Join-Path $PSScriptRoot 'build-common.ps1')

Install-Missing { @(Launcher-Prerequisites) + @(Disk-Prerequisites) } 'build.bat'

Build-Launcher
$upgrade = Test-Path $disk
if ($upgrade) { Build-Upgrade $disk } else { Refuse-Synced $disk; Build-Disk 'qcow2' $disk }

Record-Disk
# The machine runs this build from here on, not a published release.
Remove-Item $releaseRecord -ErrorAction SilentlyContinue
New-Shortcut
if ($upgrade) {
    Apply-Upgrade -archive (Upgrade-Archive $disk)
    Start-Process $exe -WorkingDirectory (Split-Path $exe)
    Write-Host 'Upgraded. RaiGolmi is starting on the new image, with everything on it kept.'
} else {
    Write-Host "Built. Run RaiGolmi.lnk here, or drag it wherever you like."
}
