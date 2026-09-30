#  What build.ps1, build-disk.ps1 and build-launcher.ps1 share: asking, installing what is
#  missing, building a disk in WSL, and building the launcher. Dot-sourced by each; not run
#  on its own.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName PresentationFramework

$repo  = $PSScriptRoot
$msys2 = if ($env:RAIGOLMI_MSYS2) { $env:RAIGOLMI_MSYS2 } else { 'C:\msys64' }
# An Ubuntu already in WSL, whatever its version suffix; `Ubuntu` is the one installed if none.
# `wsl -l` writes UTF-16, which reaches here with NULs and a byte-order mark between names.
$ubuntu = (wsl.exe -l -q 2>$null) | ForEach-Object { $_ -replace '[^\w.\-]', '' } |
          Where-Object { $_ -like 'Ubuntu*' } | Select-Object -First 1
$distro = if ($ubuntu) { $ubuntu } else { 'Ubuntu' }
# Every disk either script builds, whatever its format; ignored by git and by the disk build.
$disks = Join-Path $repo 'disk'

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
}

# Each install changes PATH for new processes only; this session reads it again.
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
}

# A function returns everything its commands print, so the output goes to the console and
# the exit code alone is returned. `--exec`, never `--`: after `--` the distro's own shell reads
# the command first and expands its `$` before bash sees it.
function Wsl-Root([string]$command) {
    wsl.exe -d $distro -u root --exec bash -c $command | Out-Host
    return $LASTEXITCODE
}

# What a disk build needs: WSL with Ubuntu, and podman in it. Each entry: what it is, the command a person runs for it, how it is installed.
function Disk-Prerequisites {
    $missing = @()
    if (-not $ubuntu) {
        $missing += @{ What = "WSL2 with $distro (the disk is built there)"
                       How  = "wsl --install -d $distro, then finish its setup in the window it opens"
                       Do   = { wsl.exe --install -d $distro
                                $script:finishWsl = $true } }
        return $missing
    }
    if ((Wsl-Root 'command -v podman >/dev/null') -ne 0) {
        $missing += @{ What = "podman, in $distro"
                       How  = "wsl -d $distro -u root -- apt-get install -y podman"
                       Do   = { if ((Wsl-Root 'apt-get update -q && apt-get install -y podman') -ne 0) {
                                    Fail "Installing podman in $distro failed; apt's output is above." } } }
    }
    return $missing
}

# One popup for everything missing: with a yes each is installed, without it each is named
# with its command and nothing is built.
function Install-Missing($missing, [string]$entry) {
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
        Fail "Windows Hypervisor Platform is enabled from the next start. Restart Windows, then run $entry again."
    }
    if ($script:finishWsl) {
        Fail "Finish $distro's setup (a user name and password) in the window that opened, then run $entry again."
    }
}

# What `build-local.sh` makes for `$type` (a bootc-image-builder type: qcow2, raw,
# anaconda-iso; or oci-archive, the image alone), moved to `$target`. Built on ext4 under
# root's home: drvfs is slow to write an image to and holds no ownership.
function Build-InWsl([string]$type, [string]$target) {
    New-Item -ItemType Directory -Force (Split-Path $target) | Out-Null
    $wslRepo   = (wsl.exe -d $distro --exec wslpath -a ($repo -replace '\\', '/')).Trim()
    $wslTarget = (wsl.exe -d $distro --exec wslpath -a ($target -replace '\\', '/')).Trim()
    # WSL's DNS proxy (10.255.255.254) often times out on quay.io; the build alone is then
    # given public resolvers, and the distro's own resolver is left as it is.
    $resolvers = if ((Wsl-Root 'timeout 10 getent hosts quay.io >/dev/null') -ne 0) { "RESOLVERS='1.1.1.1 8.8.8.8' " } else { '' }
    # Called directly, not through Wsl-Root, so the builder keeps its terminal.
    wsl.exe -d $distro -u root --exec bash -c "${resolvers}OUT=/root/raigolmi-build TYPE=$type bash '$wslRepo/host/ci/build-local.sh'"
    if ($LASTEXITCODE -ne 0) { Fail "The $type build did not finish; its output is above." }
    # The builder names its file by type; the one image it wrote is the result. What else is
    # there is its manifest and the baked host images, and its layers stay in podman's store
    # for the next build.
    # No double quotes: Windows PowerShell passes them to wsl.exe unescaped.
    $moved = ("f=`$(find /root/raigolmi-build -maxdepth 2 -type f \( -name '*.qcow2' -o -name '*.raw' -o -name '*.iso' -o -name '*.ociarchive' \)); " +
              "[ `$(printf '%s\n' `$f | grep -c .) = 1 ] || { echo 'the build did not write one image:' `$f >&2; exit 1; }; " +
              "mv `$f '$wslTarget' && rm -rf /root/raigolmi-build")
    if ((Wsl-Root $moved) -ne 0) { Fail "The $type build finished but could not be moved to $target." }
}

# A new disk in `$type` at `$target`. A disk already there is a machine's, and is upgraded
# in place instead (Build-Upgrade): a fresh one is had by deleting it first.
function Build-Disk([string]$type, [string]$target) {
    Write-Host "Building the $type disk in WSL (about 11 minutes the first time)..."
    Build-InWsl $type $target
}

# The host image alone, as one file beside the disk, which the machine on that disk switches
# to: its home, its layers and the daemon's state are under /var and are kept, and the image
# it ran before stays in the boot menu to go back to.
function Build-Upgrade([string]$disk) {
    Write-Host "A disk is already built at $disk, so it is upgraded in place, keeping everything on it."
    Write-Host 'Building the new host image in WSL...'
    Build-InWsl 'oci-archive' (Upgrade-Archive $disk)
}

# Where Build-Upgrade leaves the image for `$disk`: a path, not its return value, because a
# function returns everything its commands print and the build prints a great deal.
function Upgrade-Archive([string]$disk) {
    return Join-Path (Split-Path $disk) 'raigolmi-upgrade.ociarchive'
}

# `$archive` installed on the machine the launcher boots, then that machine started on it:
# the launcher is opened if it is not, the image copied in and switched to over the
# launcher's ssh, and the window closed, which shuts the guest down, and opened again. A
# patched daemon's unit drop-in would outlive the upgrade and run the old tree, so it goes.
function Apply-Upgrade([string]$archive) {
    # ssh writes to stderr while the guest boots, and under 'Stop' Windows PowerShell turns a
    # native command's stderr into a terminating error: each call's exit code is read instead.
    $ErrorActionPreference = 'Continue'
    $exe    = Join-Path $repo 'windows\RaiGolmi.exe'
    $config = Join-Path $env:LOCALAPPDATA 'RaiGolmi\ssh_config'
    $ssh    = Join-Path $env:SystemRoot 'System32\OpenSSH\ssh.exe'
    $scp    = Join-Path $env:SystemRoot 'System32\OpenSSH\scp.exe'
    if (-not (Test-Path $scp)) {
        Fail ("Windows' OpenSSH client is not installed ($scp is missing), and the upgrade " +
              "reaches the machine through it. The new image is at $archive.")
    }
    if (-not (Get-Process -Name RaiGolmi -ErrorAction SilentlyContinue)) {
        Write-Host 'Opening RaiGolmi, which boots the disk being upgraded...'
        Start-Process $exe -WorkingDirectory (Split-Path $exe)
    }
    Write-Host 'Waiting for the machine to answer...'
    $deadline = (Get-Date).AddMinutes(10)
    do {
        Start-Sleep -Seconds 5
        if (-not (Get-Process -Name RaiGolmi -ErrorAction SilentlyContinue)) {
            Fail "RaiGolmi closed before the machine answered. The new image is at $archive; run this again."
        }
        $answer = & $ssh -F $config raigolmi true 2>&1
    } while ($LASTEXITCODE -ne 0 -and (Get-Date) -lt $deadline)
    if ($LASTEXITCODE -ne 0) {
        Fail ("The machine did not answer over ssh within 10 minutes ($answer). The new image " +
              "is at $archive; run this again.")
    }
    Write-Host 'Copying the new image in...'
    & $scp -F $config $archive 'raigolmi:/var/tmp/raigolmi-upgrade.ociarchive'
    if ($LASTEXITCODE -ne 0) { Fail "Copying the new image into the machine failed; scp's output is above." }
    Write-Host 'Switching the machine to it...'
    # Once the machine runs an image from this path, `switch` finds the specification unchanged
    # and stages nothing; `upgrade` then reads the same path again and stages what is new.
    & $ssh -F $config raigolmi ('sudo bootc switch --transport oci-archive /var/tmp/raigolmi-upgrade.ociarchive && ' +
                                'sudo bootc upgrade && ' +
                                'rm -f /var/tmp/raigolmi-upgrade.ociarchive ~/.config/systemd/user/raigolmid.service.d/patched.conf')
    if ($LASTEXITCODE -ne 0) { Fail "The machine refused the new image; bootc's output is above. Nothing on it changed." }
    # A staged image is written into the boot entries only by a clean shutdown, and a guest
    # that dies on the way down loses it. Stopping the finalize unit runs that step now, the
    # way ostree's own tests do; the new image is then the next boot's whatever the shutdown.
    Write-Host 'Writing it into the boot entries...'
    & $ssh -F $config raigolmi ('sudo systemctl stop ostree-finalize-staged.service && ' +
                                '! systemctl is-failed --quiet ostree-finalize-staged.service && ' +
                                'test ! -e /run/ostree/staged-deployment && ' +
                                'sudo bootc status | grep -A6 ''^  rollback:'' | grep -q oci-archive')
    if ($LASTEXITCODE -ne 0) {
        & $ssh -F $config raigolmi 'sudo bootc status; journalctl -b -u ostree-finalize-staged --no-pager | tail -20'
        Fail ("The new image was staged but not written into the boot entries; bootc's status " +
              "and the finalize log are above. The machine still boots the image it had.")
    }
    Remove-Item $archive -ErrorAction Stop
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
    Start-Process $exe -WorkingDirectory (Split-Path $exe)
}

# --- the window: the launcher and what it runs on --------------------------------------------

# Published by ChristianBlevens/raigolmi-packages' workflow, one release per version.
$packages = 'https://github.com/ChristianBlevens/raigolmi-packages/releases'
$qemuVersion = '11.1.1-3.1'
$virglVersion = '1.3.0-1.1'

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
        Invoke-WebRequest "$packages/download/$tag/$file" -OutFile "$dir\$file" -UseBasicParsing
    }
    foreach ($name in $full) {
        Msys ("grep -qx 'IgnorePkg = $name' /etc/pacman.conf || " +
              "sed -i '/^\[options\]$/a IgnorePkg = $name' /etc/pacman.conf; " +
              "grep -qx 'IgnorePkg = $name' /etc/pacman.conf")
    }
    Msys 'pacman -U --noconfirm /tmp/raigolmi-packages/*.pkg.tar.zst'
    Remove-Item -Recurse -Force $dir
}

# What the window needs: the hypervisor platform, the SDK the launcher builds with, and the
# QEMU it runs. Entries shaped as Disk-Prerequisites' are.
function Launcher-Prerequisites {
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

# windows\RaiGolmi.exe. A running launcher holds the file, and publishing over it fails with
# an error that does not say so.
function Build-Launcher {
    if (Get-Process -Name RaiGolmi -ErrorAction SilentlyContinue) {
        Fail 'RaiGolmi is running. Close its window, then build again.'
    }
    Write-Host 'Building RaiGolmi.exe...'
    dotnet publish (Join-Path $repo 'windows\RaiGolmi.csproj') -c Release -nologo -v q -o (Join-Path $repo 'windows')
    if ($LASTEXITCODE -ne 0) { Fail 'The launcher did not build; the compiler''s errors are above.' }
}

# RaiGolmi.lnk beside the build scripts, to run from here or drag anywhere. It points at the
# exe, which stays in windows\ beside the files it writes; with no disk recorded, the launcher
# asks for one.
function New-Shortcut {
    $exe = Join-Path $repo 'windows\RaiGolmi.exe'
    $link = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $repo 'RaiGolmi.lnk'))
    $link.TargetPath = $exe
    $link.WorkingDirectory = Split-Path $exe
    $link.IconLocation = "$exe,0"
    $link.Save()
}
