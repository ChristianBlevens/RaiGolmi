#  RaiGolmi in a window on Windows, from the published release: installs what running it
#  needs, then downloads the launcher (windows\RaiGolmi.exe) and the machine's disk
#  (disk\raigolmi.qcow2) from ghcr.io, where build.bat would build them. Run setup.bat again to
#  update: a newer launcher replaces this one, and the machine already on the disk is upgraded
#  in place, keeping everything on it. build.bat builds both from this checkout instead.
. (Join-Path $PSScriptRoot 'common.ps1')

# Published by .github/workflows/publish.yml. `latest` (and `<commit>`) carry the launcher and
# a fresh disk; `host-<commit>` is the host image a machine already installed upgrades to.
$registry = 'ghcr.io'
$package  = 'christianblevens/raigolmi'

# curl.exe, which Windows carries: it resumes a download, and drops the registry's token when
# a blob redirects to the CDN that serves it. Its exit code is read, not its stderr.
function Curl-Json([string[]]$arguments) {
    $ErrorActionPreference = 'Continue'
    $said = & curl.exe -fsS @arguments
    if ($LASTEXITCODE -ne 0) { Fail "Asking $registry failed (curl exit $LASTEXITCODE): $($arguments[-1])" }
    return ($said -join "`n") | ConvertFrom-Json
}

# Anonymous: the package is public.
function Registry-Token {
    (Curl-Json @("https://$registry/token?scope=repository:${package}:pull")).token
}

# What `latest` names: the commit it was built from and its two files, by title.
function Latest-Release([string]$token) {
    $manifest = Curl-Json @('-H', "Authorization: Bearer $token",
                            '-H', 'Accept: application/vnd.oci.image.manifest.v1+json',
                            "https://$registry/v2/$package/manifests/latest")
    $files = @{}
    foreach ($layer in $manifest.layers) {
        $files[$layer.annotations.'org.opencontainers.image.title'] = $layer
    }
    $revision = $manifest.annotations.'org.opencontainers.image.revision'
    if (-not $revision -or -not $files['RaiGolmi.exe'] -or -not $files['raigolmi.qcow2']) {
        Fail ("$registry/${package}:latest is not a release this script knows: it names commit " +
              "'$revision' and files '$($files.Keys -join ', ')'.")
    }
    return @{ Revision = $revision; Files = $files }
}

# One file of the release at `$target`, checked against its digest before it is put there. A
# download cut off is resumed by the next run: the partial file is named by its digest.
function Download([string]$token, $layer, [string]$target) {
    $ErrorActionPreference = 'Continue'
    New-Item -ItemType Directory -Force (Split-Path $target) | Out-Null
    $partial = "$target.$($layer.digest.Substring(7, 12)).partial"
    $have = if (Test-Path $partial) { (Get-Item $partial).Length } else { 0 }
    if ($have -lt $layer.size) {
        $drive = [System.IO.DriveInfo]::new([System.IO.Path]::GetPathRoot($target))
        $needed = $layer.size - $have
        if ($drive.AvailableFreeSpace -lt $needed) {
            $shortfall = "{0} has {1:N1} GB free, and {2} needs {3:N1} GB more." -f $drive.Name,
                     ($drive.AvailableFreeSpace / 1GB), (Split-Path -Leaf $target), ($needed / 1GB)
            Fail "$shortfall Free some space on it, then run setup.bat again."
        }
        Write-Host ("Downloading $(Split-Path -Leaf $target) ({0:N1} GB)..." -f ($layer.size / 1GB))
        & curl.exe -fL --retry 5 -C - -o $partial -H "Authorization: Bearer $token" `
            "https://$registry/v2/$package/blobs/$($layer.digest)"
        # 23 is curl failing to write what it received: the drive filled, or refused the file.
        if ($LASTEXITCODE -eq 23) {
            Fail "Writing $partial failed (curl exit 23): $($drive.Name) may be full. Free some space on it, then run setup.bat again."
        }
        if ($LASTEXITCODE -ne 0) {
            Fail "Downloading $(Split-Path -Leaf $target) failed (curl exit $LASTEXITCODE). Run setup.bat again to resume it."
        }
    }
    $hash = 'sha256:' + (Get-FileHash $partial -Algorithm SHA256).Hash.ToLower()
    if ($hash -ne $layer.digest) {
        Remove-Item $partial
        Fail "$(Split-Path -Leaf $target) arrived as $hash, not $($layer.digest), and was removed. Run setup.bat again."
    }
    Move-Item -Force $partial $target
}

Install-Missing { Run-Prerequisites } 'setup.bat'

$token   = Registry-Token
$release = Latest-Release $token
$host_image = "$registry/${package}:host-$($release.Revision)"
$installed = if (Test-Path $releaseRecord) { (Get-Content $releaseRecord -Raw).Trim() } else { '' }
$short = $release.Revision.Substring(0, 12)

if ((Test-Path $disk) -and (Test-Path $exe) -and $installed -eq $release.Revision) {
    Write-Host "RaiGolmi is up to date (release $short)."
    exit 0
}

if (-not (Test-Path $disk)) {
    if (Get-Process -Name RaiGolmi -ErrorAction SilentlyContinue) {
        Fail 'RaiGolmi is running and holds its launcher. Close its window, then run setup.bat again.'
    }
    Download $token $release.Files['RaiGolmi.exe'] $exe
    Download $token $release.Files['raigolmi.qcow2'] $disk
    Record-Disk
    Set-Content -Path $releaseRecord -Value $release.Revision -NoNewline
    New-Shortcut
    Start-Process $exe -WorkingDirectory (Split-Path $exe)
    Write-Host "Installed release $short. RaiGolmi is starting; next time, open RaiGolmi.lnk here, or drag it wherever you like."
    exit 0
}

# A machine is already on the disk: it switches to this release's host image, which it
# downloads itself, and the launcher is replaced once its window has closed.
$next = "$exe.next"
Download $token $release.Files['RaiGolmi.exe'] $next
if (-not (Test-Path $exe)) { Move-Item $next $exe }
Record-Disk
Apply-Upgrade -image $host_image
if (Test-Path $next) { Move-Item -Force $next $exe }
Set-Content -Path $releaseRecord -Value $release.Revision -NoNewline
New-Shortcut
Start-Process $exe -WorkingDirectory (Split-Path $exe)
Write-Host "Updated to release $short. RaiGolmi is starting on it, with everything on the machine kept."
