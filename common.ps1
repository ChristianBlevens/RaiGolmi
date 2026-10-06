#  What setup.ps1 and the build scripts share: asking, installing what running RaiGolmi
#  needs, upgrading the machine in place, and the shortcut. Dot-sourced; not run on its own.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName PresentationFramework

$repo  = $PSScriptRoot
$msys2 = if ($env:RAIGOLMI_MSYS2) { $env:RAIGOLMI_MSYS2 } else { 'C:\msys64' }
# The launcher and the disk it boots, wherever they came from: a build or a download.
$exe   = Join-Path $repo 'windows\RaiGolmi.exe'
$disks = Join-Path $repo 'disk'
$disk  = Join-Path $disks 'raigolmi.qcow2'
$state = Join-Path $env:LOCALAPPDATA 'RaiGolmi'
# The published release the machine runs (setup.ps1). A local build removes it: the machine
# then runs what was built here.
$releaseRecord = Join-Path $state 'release.txt'

function Ask([string]$text) {
    [System.Windows.MessageBox]::Show($text, 'RaiGolmi', 'YesNo', 'Question') -eq 'Yes'
}

function Fail([string]$text) {
    Write-Host $text -ForegroundColor Red
    [System.Windows.MessageBox]::Show($text, 'RaiGolmi', 'OK', 'Error') | Out-Null
    exit 1
}

function Winget([string]$id) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Fail ("winget is not installed, and $id is downloaded through it. Install 'App " +
              "Installer' from the Microsoft Store, then run this again.")
    }
    winget install -e --id $id --accept-source-agreements --accept-package-agreements
    # A declined prompt or a failed install is said here; whether it is there is asked again.
    if ($LASTEXITCODE -ne 0) {
        Write-Host "winget exited $LASTEXITCODE installing $id." -ForegroundColor Yellow
    }
}

# Each install changes PATH for new processes only; this session reads it again.
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
}

# One popup for everything `$prerequisites` finds missing: with a yes each is installed and
# then asked for again, so one that did not install is named; without it each is named with
# its command and nothing is built.
function Install-Missing([scriptblock]$prerequisites, [string]$entry) {
    $missing = @(& $prerequisites)
    if ($missing.Count -eq 0) { return }
    $list = ($missing | ForEach-Object { " - $($_.What)" }) -join "`n"
    if (-not (Ask "RaiGolmi needs these, downloaded and installed:`n`n$list`n`nInstall them now?")) {
        Write-Host 'Nothing was installed. What RaiGolmi needs, and how to install each:'
        $missing | ForEach-Object { Write-Host " - $($_.What)`n     $($_.How)" }
        exit 1
    }
    $script:restart = $false
    $script:finishWsl = $false
    foreach ($m in $missing) {
        Write-Host "Installing $($m.What)..."
        & $m.Do
        Refresh-Path
    }
    if ($script:restart) {
        Fail "Windows Hypervisor Platform is enabled from the next start. Restart Windows, then run this again."
    }
    if ($script:finishWsl) {
        Fail "Finish $distro's setup (a user name and password) in the window that opened, then run $entry again."
    }
    $still = @(& $prerequisites)
    if ($still.Count -ne 0) {
        $list = ($still | ForEach-Object { " - $($_.What)`n     $($_.How)" }) -join "`n"
        Fail "These are still missing after installing (what went wrong is above):`n$list"
    }
}

# The machine the launcher boots switched to a new host image, then started on it: the
# launcher is opened if it is not, the image switched to over the launcher's ssh — a local
# build's `$archive`, copied in, or a published `$image` the machine pulls itself — and the
# window closed, which shuts the guest down. The caller opens it again. A patched daemon's
# unit drop-in would outlive the upgrade and run the old tree, so it goes.
function Apply-Upgrade([string]$image, [string]$archive) {
    $ErrorActionPreference = 'Continue'
    $config = Join-Path $env:LOCALAPPDATA 'RaiGolmi\ssh_config'
    $ssh    = Join-Path $env:SystemRoot 'System32\OpenSSH\ssh.exe'
    $scp    = Join-Path $env:SystemRoot 'System32\OpenSSH\scp.exe'
    if (-not (Get-Process -Name RaiGolmi -ErrorAction SilentlyContinue)) {
        Write-Host 'Opening RaiGolmi, which boots the disk being upgraded...'
        Start-Process $exe -WorkingDirectory (Split-Path $exe)
    }
    Write-Host 'Waiting for the machine to answer...'
    $deadline = (Get-Date).AddMinutes(10)
    do {
        Start-Sleep -Seconds 5
        if (-not (Get-Process -Name RaiGolmi -ErrorAction SilentlyContinue)) {
            Fail "RaiGolmi closed before the machine answered. Run this again."
        }
        $answer = & $ssh -F $config raigolmi true 2>&1
    } while ($LASTEXITCODE -ne 0 -and (Get-Date) -lt $deadline)
    if ($LASTEXITCODE -ne 0) {
        Fail "The machine did not answer over ssh within 10 minutes ($answer). Run this again."
    }
    if ($archive) {
        Write-Host 'Copying the new image in...'
        & $scp -F $config $archive 'raigolmi:/var/tmp/raigolmi-upgrade.ociarchive'
        if ($LASTEXITCODE -ne 0) { Fail "Copying the new image into the machine failed; scp's output is above." }
        # Once the machine runs an image from this path, `switch` finds the specification
        # unchanged and stages nothing; `upgrade` then reads the same path again and stages
        # what is new.
        $switch = 'sudo bootc switch --transport oci-archive /var/tmp/raigolmi-upgrade.ociarchive && ' +
                  'sudo bootc upgrade && rm -f /var/tmp/raigolmi-upgrade.ociarchive'
        $staged = 'oci-archive'
    } else {
        Write-Host "Switching the machine to $image (it downloads the image itself)..."
        $switch = "sudo bootc switch $image"
        $staged = $image
    }
    & $ssh -F $config raigolmi "$switch && rm -f ~/.config/systemd/user/raigolmid.service.d/patched.conf"
    if ($LASTEXITCODE -ne 0) { Fail "The machine refused the new image; bootc's output is above. Nothing on it changed." }
    # A staged image is written into the boot entries only by a clean shutdown, and a guest
    # that dies on the way down loses it. Stopping the finalize unit runs that step now, the
    # way ostree's own tests do; the new image is then the next boot's whatever the shutdown.
    Write-Host 'Writing it into the boot entries...'
    & $ssh -F $config raigolmi ('sudo systemctl stop ostree-finalize-staged.service && ' +
                                '! systemctl is-failed --quiet ostree-finalize-staged.service && ' +
                                'test ! -e /run/ostree/staged-deployment && ' +
                                "sudo bootc status | grep -A6 '^  rollback:' | grep -qF $staged")
    if ($LASTEXITCODE -ne 0) {
        & $ssh -F $config raigolmi 'sudo bootc status; journalctl -b -u ostree-finalize-staged --no-pager | tail -20'
        Fail ("The new image was staged but not written into the boot entries; bootc's status " +
              "and the finalize log are above. The machine still boots the image it had.")
    }
    if ($archive) { Remove-Item $archive -ErrorAction Stop }
    Write-Host 'Restarting the machine on the new image...'
    # Closed as a person closes it, so the launcher stops taking frames before the guest
    # shuts down (Display.StopListening) and the shutdown cannot abort QEMU.
    foreach ($p in Get-Process -Name RaiGolmi -ErrorAction SilentlyContinue) {
        if (-not $p.CloseMainWindow()) { Fail 'RaiGolmi has no window to close; close it, then open it again to start on the new image.' }
    }
    Wait-Process -Name RaiGolmi -Timeout 300 -ErrorAction SilentlyContinue
    if (Get-Process -Name RaiGolmi -ErrorAction SilentlyContinue) {
        Fail 'The machine was upgraded but did not power off within 5 minutes. Close RaiGolmi and open it again to start on the new image.'
    }
}

# --- the window: the launcher and what it runs on --------------------------------------------

# Published by ChristianBlevens/raigolmi-packages' workflow, one release per version.
$packages = 'https://github.com/ChristianBlevens/raigolmi-packages/releases'
$qemuVersion = '11.1.1-3.1'
$virglVersion = '1.3.0-1.1'
# Each file's SHA-256, from the release's own assets (GitHub lists each asset's digest), so a
# file replaced on the release, or changed on the way, is refused before pacman installs it
# unsigned. A new version changes these with the pins above.
$patchedSha256 = @{
    'mingw-w64-ucrt-x86_64-qemu-11.1.1-3.1-any.pkg.tar.zst'               = '47549307b46275c7ce75f92b57e20b10c9f32d127e31ec156181fa55ee72c88c'
    'mingw-w64-ucrt-x86_64-qemu-common-11.1.1-3.1-any.pkg.tar.zst'        = '7681f735c426fb38c8eb7d99b2354072bde30ef59bd5a49e1c594f19c4a01df0'
    'mingw-w64-ucrt-x86_64-qemu-guest-agent-11.1.1-3.1-any.pkg.tar.zst'   = '952a833c32cc1e96d95fc4d13f7f8425cbf68bd7025ea99a898d523d4454aa31'
    'mingw-w64-ucrt-x86_64-qemu-image-util-11.1.1-3.1-any.pkg.tar.zst'    = '576ee1f59b7c8c881979ded7fd65db386fd72ce5a98e3ca126e360f5ead97cd1'
    'mingw-w64-ucrt-x86_64-virglrenderer-1.3.0-1.1-any.pkg.tar.zst'       = '9d79600ca8d8a10eb7fb17e672a6e2e7d968a6eba590ae44efec6fe28d51f0e3'
}

function Msys([string]$command) {
    & "$msys2\usr\bin\env.exe" MSYSTEM=UCRT64 CHERE_INVOKING=1 /usr/bin/bash -lc $command
    if ($LASTEXITCODE -ne 0) { Fail "MSYS2 failed running: $command" }
}

# Every command handed to bash here carries no double quote: Windows PowerShell 5.1, which
# build.bat runs, passes one to a native program unescaped, and bash receives the command cut
# apart at it.

# Each package at the release's version and pinned; one without its pin is put back to stock
# by the next `pacman -Syu`. pacman's stderr stays inside bash: a native command's stderr is a
# terminating error here.
function Patched-Installed([string[]]$names, [string]$version) {
    if (-not (Test-Path "$msys2\usr\bin\bash.exe")) { return $false }
    $full = $names | ForEach-Object { "mingw-w64-ucrt-x86_64-$_" }
    $said = @(& "$msys2\usr\bin\env.exe" MSYSTEM=UCRT64 /usr/bin/bash -lc `
                "pacman -Q $($full -join ' ') 2>/dev/null; grep '^IgnorePkg = ' /etc/pacman.conf")
    -not ($full | Where-Object { ($said -notcontains "$_ $version") -or ($said -notcontains "IgnorePkg = $_") })
}

# A release's packages, pinned and then installed over the stock ones. Downloaded first and
# installed as local files: those are what MSYS2's pacman takes unsigned.
function Install-Patched([string]$tag, [string]$version, [string[]]$names) {
    if (Get-Process -Name qemu-system-x86_64w -ErrorAction SilentlyContinue) {
        Fail 'QEMU is running and holds the files being replaced: close the RaiGolmi window, then run this again.'
    }
    $dir = "$msys2\tmp\raigolmi-packages"
    if (Test-Path $dir) { Remove-Item -Recurse -Force $dir }
    New-Item -ItemType Directory -Force $dir | Out-Null
    # Windows PowerShell redraws its progress bar per chunk, which slows a download many times.
    $ProgressPreference = 'SilentlyContinue'
    $full = $names | ForEach-Object { "mingw-w64-ucrt-x86_64-$_" }
    foreach ($name in $full) {
        $file = "$name-$version-any.pkg.tar.zst"
        if (-not $patchedSha256.ContainsKey($file)) { Fail "No SHA-256 is pinned for $file in common.ps1." }
        Invoke-WebRequest "$packages/download/$tag/$file" -OutFile "$dir\$file" -UseBasicParsing
        $hash = (Get-FileHash "$dir\$file" -Algorithm SHA256).Hash.ToLower()
        if ($hash -ne $patchedSha256[$file]) {
            Fail "$file arrived with SHA-256 $hash, not the $($patchedSha256[$file]) pinned in common.ps1; nothing was installed."
        }
    }
    foreach ($name in $full) {
        Msys ("grep -qx 'IgnorePkg = $name' /etc/pacman.conf || " +
              "sed -i '/^\[options\]$/a IgnorePkg = $name' /etc/pacman.conf; " +
              "grep -qx 'IgnorePkg = $name' /etc/pacman.conf")
    }
    # The patched packages' dependencies resolve from the sync databases, and a fresh MSYS2's
    # are as old as its installer. MSYS2 supports only a whole-system upgrade, never -Sy alone,
    # and one that updates its core closes every MSYS2 process, this shell included, so the
    # first run may end early and the second finishes it (as msys2/setup-msys2 does). The
    # IgnorePkg pins above keep it off the patched packages.
    & "$msys2\usr\bin\env.exe" MSYSTEM=UCRT64 CHERE_INVOKING=1 /usr/bin/bash -lc 'pacman -Syu --noconfirm'
    Msys 'pacman -Syu --noconfirm'
    Msys 'pacman -U --noconfirm /tmp/raigolmi-packages/*.pkg.tar.zst'
    Remove-Item -Recurse -Force $dir
}

# What running RaiGolmi needs: the hypervisor platform, the QEMU it runs, the runtime the
# launcher runs on, and the ssh an upgrade goes through. Each entry: what it is, the command a
# person runs for it, how it is installed.
function Run-Prerequisites {
    $missing = @()

    # Asked of the hypervisor platform itself, through the API QEMU's WHPX accelerator uses: the
    # feature list can disagree with a platform that works. No WinHvPlatform.dll is no platform.
    Add-Type -Namespace RaiGolmi -Name Whp -MemberDefinition @'
[DllImport("WinHvPlatform.dll")]
public static extern int WHvGetCapability(int code, out int present, uint size, out uint written);
'@
    $hypervisor = $false
    $platform = $true
    $present = 0; $written = 0
    try {
        $hypervisor = ([RaiGolmi.Whp]::WHvGetCapability(0, [ref]$present, 4, [ref]$written) -eq 0) -and
                      ($present -ne 0)
    } catch {
        # PowerShell wraps what a .NET call throws; only a missing DLL means no platform.
        $cause = $_.Exception
        while ($cause.InnerException) { $cause = $cause.InnerException }
        if ($cause -isnot [System.DllNotFoundException]) { throw }
        $platform = $false
    }
    # With no hypervisor running, Windows reports what the firmware did with the CPU's
    # virtualization; while one runs it reads false, so it is asked only then. Firmware that
    # turned it off leaves the platform enabled and never running, whatever is installed.
    if (-not $hypervisor -and -not (Get-CimInstance Win32_ComputerSystem).HypervisorPresent -and
        (Get-CimInstance Win32_Processor | Where-Object { -not $_.VirtualizationFirmwareEnabled })) {
        Fail ("Virtualization is turned off in this PC's firmware, so no virtual machine can run. " +
              "Turn on Intel VT-x (sometimes 'Intel Virtualization Technology') or AMD SVM " +
              "in your BIOS/UEFI settings, start Windows again, then run this again.")
    }
    # The VM takes 8 GB (`windows/RaiGolmi.cs` `-m 8192`) and Windows needs room of its own
    # beside it; a 16 GB PC reads a little under 16 once firmware and graphics have theirs.
    $memory = (Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory
    if ($memory -lt 12GB) {
        Fail (("This PC has {0:N1} GB of memory. RaiGolmi's machine takes 8 GB of it and Windows " +
               "needs its own beside that, so it needs 16 GB.") -f ($memory / 1GB))
    }
    if (-not $hypervisor -and $platform) {
        if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') {
            Fail "Windows has changes waiting for a restart. Restart Windows, then run this again."
        }
        Fail ("Windows Hypervisor Platform is installed but its hypervisor is not running. " +
              "Restart Windows; if this still shows after a restart, Windows is set not to start " +
              "its hypervisor: as administrator run 'bcdedit /set hypervisorlaunchtype auto', " +
              "restart, then run this again.")
    }
    if (-not $hypervisor) {
        $missing += @{ What = 'Windows Hypervisor Platform (QEMU runs the machine on it; needs a restart)'
                       How  = 'As administrator: dism /online /enable-feature /featurename:HypervisorPlatform /all'
                       Do   = { Start-Process dism.exe -Verb RunAs -Wait -ArgumentList `
                                    '/online /enable-feature /featurename:HypervisorPlatform /all /norestart'
                                $script:restart = $true } }
    }

    # A framework-dependent exe: the SDK a local build installs carries this runtime too.
    if (-not ((Get-Command dotnet -ErrorAction SilentlyContinue) -and
              ((& dotnet --list-runtimes 2>$null) -match '^Microsoft\.WindowsDesktop\.App 8\.'))) {
        $missing += @{ What = '.NET 8 Desktop Runtime (the launcher runs on it)'
                       How  = 'winget install Microsoft.DotNet.DesktopRuntime.8'
                       Runtime = $true
                       Do   = { Winget 'Microsoft.DotNet.DesktopRuntime.8' } }
    }

    # An upgrade reaches the machine over ssh (Apply-Upgrade), and so do the launcher's
    # clipboard and files.
    if (-not (Test-Path (Join-Path $env:SystemRoot 'System32\OpenSSH\ssh.exe'))) {
        $missing += @{ What = "Windows' OpenSSH client (upgrades and the clipboard reach the machine through it)"
                       How  = 'As administrator: Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0'
                       Do   = { Start-Process powershell.exe -Verb RunAs -Wait -ArgumentList `
                                    '-NoProfile -Command Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0' } }
    }

    if (-not (Test-Path "$msys2\usr\bin\bash.exe")) {
        $missing += @{ What = "MSYS2 at $msys2 (QEMU comes from it)"
                       How  = 'winget install MSYS2.MSYS2'
                       Do   = { Winget 'MSYS2.MSYS2' } }
    }

    # The stock virglrenderer cannot show the boot console, and the stock QEMU has no discard
    # on Windows, so the disk image never gives back what the guest frees: both come patched
    # from ChristianBlevens/raigolmi-packages.
    if (-not (Patched-Installed @('virglrenderer') $virglVersion)) {
        $missing += @{ What = "the patched virglrenderer $virglVersion (without it the boot screen never shows)"
                       How  = "$packages/virglrenderer-$virglVersion"
                       Do   = { Install-Patched "virglrenderer-$virglVersion" $virglVersion @('virglrenderer') } }
    }
    if (-not (Patched-Installed @('qemu', 'qemu-common', 'qemu-guest-agent', 'qemu-image-util') $qemuVersion)) {
        $missing += @{ What = "the patched QEMU $qemuVersion (without it the disk image never shrinks)"
                       How  = "$packages/qemu-$qemuVersion"
                       Do   = { Install-Patched "qemu-$qemuVersion" $qemuVersion `
                                    @('qemu', 'qemu-common', 'qemu-guest-agent', 'qemu-image-util') } }
    }

    return $missing
}

# The disk grows to 60 GB and is written to all the time, so under OneDrive every write would
# be uploaded. A new one is never made there; one already there is the user's to move.
function Refuse-Synced([string]$path) {
    foreach ($root in @($env:OneDrive, $env:OneDriveConsumer, $env:OneDriveCommercial)) {
        if ($root -and $path.StartsWith($root.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
            Fail ("$path would be inside OneDrive ($root), which would upload the machine's disk " +
                  "(up to 60 GB) every time it changes. Put the RaiGolmi folder somewhere OneDrive " +
                  "does not sync, such as C:\RaiGolmi, then run this again from there.")
        }
    }
}

# The launcher boots this disk from now on (it reads disk.txt).
function Record-Disk {
    New-Item -ItemType Directory -Force $state | Out-Null
    Set-Content -Path (Join-Path $state 'disk.txt') -Value $disk -NoNewline
}

# RaiGolmi.lnk beside the build scripts, to run from here or drag anywhere. It points at the
# exe, which stays in windows\ beside the files it writes; with no disk recorded, the launcher
# asks for one.
function New-Shortcut {
    $link = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $repo 'RaiGolmi.lnk'))
    $link.TargetPath = $exe
    $link.WorkingDirectory = Split-Path $exe
    $link.IconLocation = "$exe,0"
    $link.Save()
}
