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

# The machine runs this build from here on, not a published release.
Remove-Item $releaseRecord -ErrorAction SilentlyContinue
if ($upgrade) {
    # The launcher Apply-Upgrade opens boots the recorded disk, so it is recorded first.
    Record-Disk
    Apply-Upgrade -archive (Upgrade-Archive $disk)
    Open-RaiGolmi 'Upgraded on the new image, with everything on the machine kept.'
} else {
    Open-RaiGolmi 'Built.'
}
