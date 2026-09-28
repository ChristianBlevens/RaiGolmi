#!/usr/bin/env bash
# MSYS2's virglrenderer with 003-d3d11-texture-optional.patch, built and installed in place of
# the stock one. Without it virglrenderer answers EINVAL for a scanout that has no D3D11 texture
# (the boot console, the shutdown screen), QEMU refuses the scanout, and the window says
# "Display output is not active". With it QEMU hands that scanout over as a map, which the
# launcher already draws.
set -euo pipefail
here=$(pwd)
pkg=mingw-w64-ucrt-x86_64-virglrenderer

if tasklist | grep -qi qemu-system; then
  echo "QEMU is running and holds the DLL: close the RaiGolmi window first." >&2
  exit 1
fi

pacman -S --needed --noconfirm base-devel git

work=$(mktemp -d)
git clone --depth 1 --filter=blob:none --sparse https://github.com/msys2/MINGW-packages "$work/p"
git -C "$work/p" sparse-checkout set mingw-w64-virglrenderer
cd "$work/p/mingw-w64-virglrenderer"

# The patch is written against 1.3.0; another version is a new reading, not a rebuild.
grep -qx 'pkgver=1.3.0' PKGBUILD || { echo "PKGBUILD is not virglrenderer 1.3.0:" >&2; grep '^pkgver=' PKGBUILD >&2; exit 1; }
cp "$here/003-d3d11-texture-optional.patch" .
sed -i \
  -e 's/^pkgrel=1$/pkgrel=1.1/' \
  -e 's/^        002-no-ioccom.patch)$/        002-no-ioccom.patch\n        003-d3d11-texture-optional.patch)/' \
  -e "/^sha256sums=/,/)\$/ s/)\$/\n            'SKIP')/" \
  -e 's/^    002-no-ioccom.patch$/    002-no-ioccom.patch \\\n    003-d3d11-texture-optional.patch/' \
  PKGBUILD
[ "$(grep -c 003-d3d11-texture-optional.patch PKGBUILD)" = 2 ] && grep -q "^            'SKIP')" PKGBUILD \
  && grep -qx 'pkgrel=1.1' PKGBUILD || { echo "the PKGBUILD edit did not take:" >&2; cat PKGBUILD >&2; exit 1; }

MINGW_ARCH=ucrt64 makepkg-mingw --cleanbuild --syncdeps --force --noconfirm --nocheck
pacman -U --noconfirm "$pkg"-1.3.0-1.1-any.pkg.tar.zst

# Without this the next `pacman -Syu` puts the stock DLL back, and says so as it skips it.
grep -q "^IgnorePkg.*$pkg" /etc/pacman.conf || sed -i "s/^\[options\]$/[options]\nIgnorePkg = $pkg/" /etc/pacman.conf
grep -q "^IgnorePkg.*$pkg" /etc/pacman.conf || { echo "could not pin $pkg in /etc/pacman.conf" >&2; exit 1; }
rm -rf "$work"
echo "Installed the patched virglrenderer. Open RaiGolmi: the boot screen should now show."
