#!/bin/sh
# The image before this one goes once this one has proven itself: raigolmid-drop-rollback
# runs after this boot's raigolmid answered. Nothing when this boot is itself the rollback,
# chosen from the boot menu because the newer image failed.
set -eu
status=$(ostree admin status)
case "$(printf '%s\n' "$status" | head -n 1)" in
  '* '*) ;;
  *) echo "booted from the rollback; both images are kept"; exit 0 ;;
esac
printf '%s\n' "$status" | grep -q ' (rollback)$' || exit 0
exec rpm-ostree cleanup --rollback
