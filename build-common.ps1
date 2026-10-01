#  What build.ps1, build-disk.ps1 and build-launcher.ps1 share beyond common.ps1: building a
#  disk in WSL, and building the launcher. Dot-sourced by each; not run on its own.
. (Join-Path $PSScriptRoot 'common.ps1')

# An Ubuntu already in WSL, whatever its version suffix; `Ubuntu` is the one installed if none.
# `wsl -l` writes UTF-16, which reaches here with NULs and a byte-order mark between names.
$ubuntu = (wsl.exe -l -q 2>$null) | ForEach-Object { $_ -replace '[^\w.\-]', '' } |
          Where-Object { $_ -like 'Ubuntu*' } | Select-Object -First 1
$distro = if ($ubuntu) { $ubuntu } else { 'Ubuntu' }

# A function returns everything its commands print, so the output goes to the console and
# the exit code alone is returned. `--exec`, never `--`: after `--` the distro's own shell reads
# the command first and expands its `$` before bash sees it.
function Wsl-Root([string]$command) {
    wsl.exe -d $distro -u root --exec bash -c $command | Out-Host
    return $LASTEXITCODE
}

# What a disk build needs: WSL with Ubuntu, and podman in it. Entries shaped as
# Run-Prerequisites' are.
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


# What building the launcher needs on top of running it: the SDK, which carries the runtime
# Run-Prerequisites would otherwise install.
function Launcher-Prerequisites {
    $missing = @(Run-Prerequisites | Where-Object { -not $_.Runtime })
    if (-not ((Get-Command dotnet -ErrorAction SilentlyContinue) -and
              ((& dotnet --list-sdks 2>$null) -match '^8\.'))) {
        $missing += @{ What = '.NET 8 SDK (builds the launcher)'
                       How  = 'winget install Microsoft.DotNet.SDK.8'
                       Do   = { Winget 'Microsoft.DotNet.SDK.8' } }
    }
    return $missing
}
