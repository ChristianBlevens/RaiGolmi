#  The machine's disk alone, for bare metal or a VM of your own: nothing of the Windows
#  launcher is built or installed. Run build-disk.bat. A disk already there is upgraded
#  instead: the new host image is built for the machine installed from it to switch to.
#  On Linux, host/ci/build-local.sh is the same build (TYPE=raw or anaconda-iso for bare
#  metal, oci-archive for an upgrade).
. (Join-Path $PSScriptRoot 'build-common.ps1')

$formats = [ordered]@{
    '1' = @{ Type = 'raw'; File = 'raigolmi.raw'
             Says = 'raw: an image written straight to a drive (Rufus, dd), which then boots it' }
    '2' = @{ Type = 'anaconda-iso'; File = 'raigolmi-installer.iso'
             Says = 'installer ISO: boots from a USB stick and asks which disk to install onto (untested)' }
    '3' = @{ Type = 'qcow2'; File = 'raigolmi.qcow2'
             Says = 'qcow2: a disk for QEMU or another hypervisor' }
}
Write-Host 'Which disk?'
$formats.GetEnumerator() | ForEach-Object { Write-Host "  $($_.Key)) $($_.Value.Says)" }
$choice = (Read-Host 'Number').Trim()
if (-not $formats.Contains($choice)) { Fail "No disk numbered '$choice'." }
$format = $formats[$choice]

Install-Missing @(Disk-Prerequisites) 'build-disk.bat'
$target = Join-Path $disks $format.File
if (Test-Path $target) {
    # The machine installed from it is not one this script can reach: the image is left for it.
    Build-Upgrade $target
    $archive = Upgrade-Archive $target
    Write-Host "Built: $archive. Copy it to the machine installed from $target, then run there:"
    Write-Host "  sudo bootc switch --transport oci-archive <where you copied it> && sudo systemctl reboot"
} else {
    Build-Disk $format.Type $target
    Write-Host "Built: $target"
}
