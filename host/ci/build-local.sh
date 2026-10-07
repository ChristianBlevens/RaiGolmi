#!/bin/bash
#  Build the host image and a disk from it (TYPE: qcow2, raw or anaconda-iso) on a machine
#  with a working container runtime; or, with TYPE=oci-archive, the image alone as one file
#  (raigolmi-host.ociarchive) that a machine already installed switches to with
#  `bootc switch --transport oci-archive`, keeping everything under /var.
#
#  ⚠ bootc-image-builder needs --privileged and access to the host's container storage.
#
#  The published release is the same build in .github/workflows/publish.yml, run only when
#  dispatched; this is the build from a checkout (build.bat runs it in WSL).
set -euo pipefail

repo=$(cd "$(dirname "$0")/../.." && pwd)
image=${IMAGE:-localhost/raigolmi-host:latest}
#  Not under $repo by default: on WSL2 the checkout is on a Windows drive over drvfs,
#  which is slow to write a disk image to and cannot hold Unix ownership. Point OUT at
#  ext4 — somewhere under your home directory.
out=${OUT:-$HOME/raigolmi-build}
type=${TYPE:-qcow2}

#  RESOLVERS: nameservers this build alone resolves with, for a machine whose own resolver
#  cannot reach the registries (WSL's DNS proxy times out on quay.io). They are bound over
#  /etc/resolv.conf in a mount namespace the build runs in, as root: the machine's own files
#  are never changed, and nothing outlives the build.
if [ -n "${RESOLVERS:-}" ] && [ -z "${RESOLVERS_BOUND:-}" ]; then
    conf=$(mktemp)
    printf 'nameserver %s\n' $RESOLVERS > "$conf"
    exec sudo env RESOLVERS_BOUND="$conf" IMAGE="$image" OUT="$out" TYPE="$type" \
        unshare --mount --propagation private \
        bash -c 'mount --bind "$RESOLVERS_BOUND" /etc/resolv.conf && rm "$RESOLVERS_BOUND" && exec bash "$0"' "$0"
fi

mkdir -p "$out"

#  Every podman call is `sudo`: the builder below reads the image out of root's container
#  storage, and an image built rootless would sit in your own storage where it cannot be
#  found. The two must match or the disk step fails on an image that visibly exists.
#
#  --network=host on both podman calls: a private network means netavark programming NAT
#  through nftables, and WSL2's kernel rejects its ruleset ("nft did not return
#  successfully"). Nothing here needs isolation — the RUN steps only need to reach dnf.
#  The host image first, then the same image with the host's own images baked in: those are
#  built from the tree the first one ships (bake-host-images.sh).
base=localhost/raigolmi-host-base:latest
sudo podman build --network=host -t "$base" -f "$repo/host/Containerfile" "$repo"
bash "$repo/host/ci/bake-host-images.sh" "$base" "$image" "$out/host-images"

if [ "$type" = oci-archive ]; then
    sudo podman save --format oci-archive -o "$out/raigolmi-host.ociarchive" "$image"
else
    #  The storage mount is what lets the builder read the image just built into this
    #  machine's container storage rather than pulling it from a registry.
    #
    #  --rootfs: the Fedora bootc base declares no default root filesystem type and the builder
    #  refuses rather than choosing one. ext4 because Docker's overlay2 driver runs every body
    #  here and wants d_type, which ext4 always has; and because WSL's 6.6 kernel, which mounts
    #  the new disk during this build, cannot mount the XFS Fedora 44's mkfs.xfs makes (parent
    #  pointers and exchange-range are on by default and the builder passes no way to turn
    #  them off).
    #  The installer asks where to install (installer.toml); the disk types are sized by config.toml.
    config=config.toml
    [ "$type" = anaconda-iso ] && config=installer.toml
    sudo podman run --rm -it --privileged --network=host --security-opt label=type:unconfined_t \
        -v /var/lib/containers/storage:/var/lib/containers/storage \
        -v "$repo/host/ci/$config:/config.toml:ro" \
        -v "$out:/output" \
        quay.io/centos-bootc/bootc-image-builder:latest \
        --type "$type" --rootfs "${ROOTFS:-ext4}" "$image"
fi

sudo chown -R "$(id -u):$(id -g)" "$out"
echo "built under $out"
