#  What build.ps1 and build-disk.ps1 share: asking, installing what is missing, and building a
#  disk in WSL. Dot-sourced by both; not run on its own.
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

# What a disk build needs: WSL with Ubuntu, podman in it, and a resolver that reaches the
# image registry. Each entry: what it is, the command a person runs for it, how it is installed.
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
    # WSL's DNS proxy often cannot resolve the image registry; a fixed resolver can.
    if ((Wsl-Root 'timeout 10 getent hosts quay.io >/dev/null') -ne 0) {
        $missing += @{ What = "a DNS resolver $distro can reach quay.io with"
                       How  = "In ${distro}: point /etc/resolv.conf at 1.1.1.1 and set generateResolvConf = false in /etc/wsl.conf"
                       # wsl.conf is the user's (Ubuntu's own sets systemd there): one key is set, the rest kept.
                       Do   = { if ((Wsl-Root ("rm -f /etc/resolv.conf && printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' > /etc/resolv.conf && touch /etc/wsl.conf && " +
                                               "if grep -q '^[[:space:]]*generateResolvConf' /etc/wsl.conf; then sed -i 's/^[[:space:]]*generateResolvConf.*/generateResolvConf = false/' /etc/wsl.conf; " +
                                               "elif grep -q '^\[network\]' /etc/wsl.conf; then sed -i '/^\[network\]/a generateResolvConf = false' /etc/wsl.conf; " +
                                               "else printf '\n[network]\ngenerateResolvConf = false\n' >> /etc/wsl.conf; fi")) -ne 0) {
                                    Fail "Setting $distro's resolver failed; its output is above." } } }
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

# The disk in `$type` (a bootc-image-builder type: qcow2, raw, anaconda-iso) at `$target`.
# One already there is replaced only with a yes, since a machine may live on it.
function Build-Disk([string]$type, [string]$target) {
    if ((Test-Path $target) -and -not (Ask ("A disk is already built at $target.`n`nBuild a new " +
            "one in its place? Everything on the current one is lost."))) {
        Write-Host 'The disk is kept.'
        return
    }
    New-Item -ItemType Directory -Force (Split-Path $target) | Out-Null
    Write-Host "Building the $type disk in WSL (about 11 minutes the first time)..."
    $wslRepo   = (wsl.exe -d $distro --exec wslpath -a ($repo -replace '\\', '/')).Trim()
    $wslTarget = (wsl.exe -d $distro --exec wslpath -a ($target -replace '\\', '/')).Trim()
    # Built on ext4 under root's home: drvfs is slow to write a disk image to and holds no
    # ownership. Called directly, not through Wsl-Root, so the builder keeps its terminal.
    wsl.exe -d $distro -u root --exec bash -c "OUT=/root/raigolmi-build TYPE=$type bash '$wslRepo/host/ci/build-local.sh'"
    if ($LASTEXITCODE -ne 0) { Fail 'The disk did not build; its output is above.' }
    # The builder names its file by type; the one image it wrote is the disk. What else is
    # there is its manifest, and its layers stay in podman's store for the next build.
    # No double quotes: Windows PowerShell passes them to wsl.exe unescaped.
    $moved = ("f=`$(find /root/raigolmi-build -type f -name '*.qcow2' -o -type f -name '*.raw' -o -type f -name '*.iso'); " +
              "[ `$(printf '%s\n' `$f | grep -c .) = 1 ] || { echo 'the build did not write one image:' `$f >&2; exit 1; }; " +
              "mv `$f '$wslTarget' && rm -rf /root/raigolmi-build")
    if ((Wsl-Root $moved) -ne 0) { Fail "The disk built but could not be moved to $target." }
}
