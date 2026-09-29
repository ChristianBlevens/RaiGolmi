#!/bin/sh
# Docker keeps its images in overlay2 (host/docker/daemon.json), unpacked only. A machine
# whose Docker began on containerd's image store keeps that store's images, compressed and
# unpacked, where nothing reads them any more; they go once, with Docker's containers
# made from them. raigolmid recreates every container it runs, pulling or building what it
# needs.
set -eu
rm -rf /var/lib/containerd /var/lib/docker
mkdir -p /var/lib/raigolmi
touch /var/lib/raigolmi/docker-store-overlay2
