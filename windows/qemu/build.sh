#!/usr/bin/env bash
# MSYS2's QEMU with 004-file-win32-discard.patch, built for x86_64 only and installed in place
# of the stock one. Without it QEMU's Windows file driver has no discard, so what the guest
# frees stays in the disk image and the file only grows; with it the image is sparse and a
# discard releases its range in place.
set -euo pipefail
here=$(pwd)
prefix=mingw-w64-ucrt-x86_64
pkgs=("$prefix-qemu" "$prefix-qemu-common" "$prefix-qemu-guest-agent" "$prefix-qemu-image-util")

if tasklist | grep -qi qemu-system; then
  echo "QEMU is running: close the RaiGolmi window first." >&2
  exit 1
fi

pacman -S --needed --noconfirm base-devel git

work=$(mktemp -d)
git clone --depth 1 --filter=blob:none --sparse https://github.com/msys2/MINGW-packages "$work/p"
git -C "$work/p" sparse-checkout set mingw-w64-qemu
cd "$work/p/mingw-w64-qemu"

# The patch is written against 11.1.1; another version is a new reading, not a rebuild.
grep -qx '_base_ver="11.1.1"' PKGBUILD || { echo "PKGBUILD is not QEMU 11.1.1:" >&2; grep '^_base_ver=' PKGBUILD >&2; exit 1; }
rel=$(sed -n 's/^pkgrel=\([0-9]*\)$/\1/p' PKGBUILD)
[ -n "$rel" ] || { echo "PKGBUILD has no plain pkgrel" >&2; exit 1; }
cp "$here/004-file-win32-discard.patch" .
# One emulator (the PKGBUILD's own "faster testing" line), the patch applied, and the TCG
# plugins, which that build may not produce and nothing here uses, left out of the package.
sed -i \
  -e "s/^pkgrel=$rel\$/pkgrel=$rel.1/" \
  -e 's/^  msys2.examples.tests.sh$/  msys2.examples.tests.sh\n  004-file-win32-discard.patch/' \
  -e "/^sha256sums=/,/)\$/ s/)\$/\n            'SKIP')/" \
  -e 's/^  #apply_patch_with_msg whpx.\$I.patch$/  apply_patch_with_msg 004-file-win32-discard.patch/' \
  -e 's/^  #CONFIGURE_OPTS="--target-list=x86_64-softmmu"$/  CONFIGURE_OPTS="--target-list=x86_64-softmmu"/' \
  -e '/^  # Install tcg plugins to lib\/qemu\/plugins$/d' \
  -e '/^  mkdir -pv lib\/qemu\/plugins\/test lib\/qemu\/plugins\/contrib$/d' \
  -e '/^  install -t lib\/qemu\/plugins\//d' \
  PKGBUILD
[ "$(grep -c 004-file-win32-discard.patch PKGBUILD)" = 2 ] \
  && grep -qx '  CONFIGURE_OPTS="--target-list=x86_64-softmmu"' PKGBUILD \
  && grep -qx "pkgrel=$rel.1" PKGBUILD && ! grep -q 'lib/qemu/plugins' PKGBUILD \
  || { echo "the PKGBUILD edit did not take:" >&2; cat PKGBUILD >&2; exit 1; }

# --skippgpcheck: the tarball's signature needs its signer's key in this keyring; the
# tarball's sha256 in the PKGBUILD still pins it.
MINGW_ARCH=ucrt64 makepkg-mingw --cleanbuild --syncdeps --force --noconfirm --nocheck --skippgpcheck
pacman -U --noconfirm "${pkgs[@]/%/-11.1.1-$rel.1-any.pkg.tar.zst}"

# Without this the next `pacman -Syu` puts the stock QEMU back, and says so as it skips it.
for pkg in "${pkgs[@]}"; do
  grep -q "^IgnorePkg.* $pkg\( \|$\)" /etc/pacman.conf || grep -q "^IgnorePkg = $pkg\( \|$\)" /etc/pacman.conf \
    || sed -i "s/^\[options\]$/[options]\nIgnorePkg = $pkg/" /etc/pacman.conf
  grep -q "^IgnorePkg.*$pkg" /etc/pacman.conf || { echo "could not pin $pkg in /etc/pacman.conf" >&2; exit 1; }
done
rm -rf "$work"
echo "Installed the patched QEMU. The disk image now gives back what the guest frees."
