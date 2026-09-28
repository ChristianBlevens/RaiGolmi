#  The machine's disk alone, for bare metal or a VM of your own: nothing of the Windows
#  launcher is built or installed. Run build-disk.bat. On Linux, host/ci/build-local.sh is
#  the same build (TYPE=raw or anaconda-iso for bare metal).
. (Join-Path $PSScriptRoot 'build-common.ps1')

$formats = [ordered]@{
    '1' = @{ Type = 'raw'; File = 'raigolmi.raw'
             Says = 'raw: an image written straight to a drive (Rufus, dd), which then boots it' }
    '2' = @{ Type = 'anaconda-iso'; File = 'raigolmi-installer.iso'
             Says = 'installer ISO: boots from a USB stick and installs onto the FIRST disk it finds, erasing it' }
    '3' = @{ Type = 'qcow2'; File = 'raigolmi.qcow2'
             Says = 'qcow2: a disk for QEMU or another hypervisor' }
}
Write-Host 'Which disk?'
$formats.GetEnumerator() | ForEach-Object { Write-Host "  $($_.Key)) $($_.Value.Says)" }
$choice = (Read-Host 'Number').Trim()
if (-not $formats.Contains($choice)) { Fail "No disk numbered '$choice'." }
$format = $formats[$choice]

Install-Missing @(Disk-Prerequisites) 'build-disk.bat'
Build-Disk $format.Type (Join-Path $disks $format.File)
Write-Host "Built: $(Join-Path $disks $format.File)"
