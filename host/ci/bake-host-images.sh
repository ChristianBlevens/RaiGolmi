#!/bin/bash
#  Bake the host's own images into the disk: bake-host-images.sh BASE FINAL WORKDIR
#
#  BASE is host/Containerfile, already built. Its own raigolmid names every image the disk
#  carries and the tag each will be asked for (`python -m raigolmid.hostimages`), computed over
#  the tree BASE ships — so the tags cannot drift from what the booted daemon computes. Each is
#  built from that same tree, copied out of BASE, and all go into one archive that
#  host/Containerfile.images puts under /usr as FINAL (raigolmid/raigolmid/hostimages.py).
#
#  Every podman call is `sudo` and --network=host, for build-local.sh's reasons.
set -euo pipefail

repo=$(cd "$(dirname "$0")/../.." && pwd)
base=$1
final=$2
work=$3
shipped=/usr/share/raigolmi

sudo rm -rf "$work"
mkdir -p "$work/tree" "$work/context"

listing=$(sudo podman run --rm --network=none "$base" \
    /usr/lib/raigolmi/bin/python -m raigolmid.hostimages)

if [[ -z $listing ]]; then
    echo "bake-host-images: $base names no host images" >&2
    exit 1
fi

container=$(sudo podman create "$base")
trap 'sudo podman rm -f "$container" >/dev/null' EXIT
sudo podman cp "$container:$shipped/." "$work/tree/"

tags=()
while IFS=$'\t' read -r tag context containerfile; do
    for path in "$context" "$containerfile"; do
        if [[ $path != "$shipped"/* ]]; then
            echo "bake-host-images: $tag builds from $path, which is not under $shipped" >&2
            exit 1
        fi
    done
    #  Qualified, because podman files an unqualified name under localhost/ and Docker would
    #  load it under that name; docker.io/ is the registry Docker drops when it names an image.
    sudo podman build --network=host -t "docker.io/$tag" \
        -f "$work/tree/${containerfile#"$shipped"/}" "$work/tree/${context#"$shipped"/}"
    tags+=("docker.io/$tag")
done <<<"$listing"

#  One docker-archive, so the layers the images share are stored once.
sudo podman save --multi-image-archive --format docker-archive \
    -o "$work/context/host-images.tar" "${tags[@]}"
sudo podman build --network=host --build-arg BASE="$base" -t "$final" \
    -f "$repo/host/Containerfile.images" "$work/context"
sudo rm -rf "$work"
