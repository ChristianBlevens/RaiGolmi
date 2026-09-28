#  RaiGolmi in a window on Windows: builds the launcher (windows\RaiGolmi.exe) and the
#  machine's disk (disk\raigolmi.qcow2), which the launcher then boots. Run build.bat.
#  For the disk alone (bare metal, or a VM of your own), run build-disk.bat instead.
. (Join-Path $PSScriptRoot 'build-common.ps1')

$disk   = Join-Path $disks 'raigolmi.qcow2'
$record = Join-Path $env:LOCALAPPDATA 'RaiGolmi\disk.txt'
$virgl  = 'mingw-w64-ucrt-x86_64-virglrenderer'

function Msys([string]$command) {
    & "$msys2\usr\bin\env.exe" MSYSTEM=UCRT64 CHERE_INVOKING=1 /usr/bin/bash -lc $command
    if ($LASTEXITCODE -ne 0) { Fail "MSYS2 failed running: $command" }
}

# --- what the window needs, then what the disk needs ------------------------------------
$missing = @()

# Asked of the hypervisor platform itself, through the API QEMU's WHPX accelerator uses: the
# feature list can disagree with a platform that works. No WinHvPlatform.dll is no platform.
Add-Type -Namespace RaiGolmi -Name Whp -MemberDefinition @'
[DllImport("WinHvPlatform.dll")]
public static extern int WHvGetCapability(int code, out int present, uint size, out uint written);
'@
$hypervisor = $false
$present = 0; $written = 0
try {
    $hypervisor = ([RaiGolmi.Whp]::WHvGetCapability(0, [ref]$present, 4, [ref]$written) -eq 0) -and
                  ($present -ne 0)
} catch {
    # PowerShell wraps what a .NET call throws; only a missing DLL means no platform.
    $cause = $_.Exception
    while ($cause.InnerException) { $cause = $cause.InnerException }
    if ($cause -isnot [System.DllNotFoundException]) { throw }
}
if (-not $hypervisor) {
    $missing += @{ What = 'Windows Hypervisor Platform (QEMU runs the machine on it; needs a restart)'
                   How  = 'As administrator: dism /online /enable-feature /featurename:HypervisorPlatform /all'
                   Do   = { Start-Process dism.exe -Verb RunAs -Wait -ArgumentList `
                                '/online /enable-feature /featurename:HypervisorPlatform /all /norestart'
                            $script:restart = $true } }
}

if (-not ((Get-Command dotnet -ErrorAction SilentlyContinue) -and
          ((& dotnet --list-sdks 2>$null) -match '^8\.'))) {
    $missing += @{ What = '.NET 8 SDK (builds the launcher)'
                   How  = 'winget install Microsoft.DotNet.SDK.8'
                   Do   = { Winget 'Microsoft.DotNet.SDK.8' } }
}

if (-not (Test-Path "$msys2\usr\bin\bash.exe")) {
    $missing += @{ What = "MSYS2 at $msys2 (QEMU comes from it)"
                   How  = 'winget install MSYS2.MSYS2'
                   Do   = { Winget 'MSYS2.MSYS2' } }
}

if (-not (Test-Path "$msys2\ucrt64\bin\qemu-system-x86_64w.exe")) {
    $missing += @{ What = 'QEMU, in MSYS2'
                   How  = 'In the MSYS2 UCRT64 window: pacman -S mingw-w64-ucrt-x86_64-qemu'
                   Do   = { Msys 'pacman -S --needed --noconfirm mingw-w64-ucrt-x86_64-qemu' } }
}

# The stock virglrenderer cannot show the boot console (windows/virglrenderer/build.sh).
$patched = (Test-Path "$msys2\usr\bin\bash.exe") -and
           ((& "$msys2\usr\bin\env.exe" MSYSTEM=UCRT64 /usr/bin/bash -lc "pacman -Q $virgl" 2>$null) -match '1\.3\.0-1\.1')
if (-not $patched) {
    $missing += @{ What = 'the patched virglrenderer, compiled in MSYS2 (without it the boot screen never shows)'
                   How  = 'In the MSYS2 MSYS window, in windows\virglrenderer: bash build.sh'
                   Do   = { Push-Location (Join-Path $repo 'windows\virglrenderer')
                            try { & "$msys2\usr\bin\env.exe" MSYSTEM=MSYS CHERE_INVOKING=1 /usr/bin/bash -l ./build.sh }
                            finally { Pop-Location }
                            if ($LASTEXITCODE -ne 0) { Fail 'The virglrenderer build failed; its output is above.' } } }
}

$missing += @(Disk-Prerequisites)
Install-Missing $missing 'build.bat'

# --- the launcher, then its disk ---------------------------------------------------------
Write-Host 'Building RaiGolmi.exe...'
dotnet publish (Join-Path $repo 'windows\RaiGolmi.csproj') -c Release -nologo -v q -o (Join-Path $repo 'windows')
if ($LASTEXITCODE -ne 0) { Fail 'The launcher did not build; the compiler''s errors are above.' }

Build-Disk 'qcow2' $disk

New-Item -ItemType Directory -Force (Split-Path $record) | Out-Null
Set-Content -Path $record -Value $disk -NoNewline

# Made once there is a disk for it to boot. It sits beside build.bat, to run from here or drag
# anywhere, and points at the exe, which stays in windows\ beside the files it writes.
$exe = Join-Path $repo 'windows\RaiGolmi.exe'
$link = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $repo 'RaiGolmi.lnk'))
$link.TargetPath = $exe
$link.WorkingDirectory = Split-Path $exe
$link.IconLocation = "$exe,0"
$link.Save()
Write-Host "Built. Run RaiGolmi.lnk here, or drag it wherever you like."
