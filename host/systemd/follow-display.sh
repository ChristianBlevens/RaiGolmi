#!/bin/sh
# Keeps each display at the mode it prefers when the display reports a change: a monitor
# swapped on metal, or the Windows launcher's window resized around the VM. Nothing here
# knows which.
#
# ⚠ Sway takes a display's preferred mode when the display appears and not after, and its
# own mode list for it goes stale, so `output X mode WxH` for the new mode answers success
# and changes nothing. The kernel's list is current and its first entry is the preferred
# mode. The display's own timings are tried first; a custom mode only when sway does not
# have them.
set -eu

current() {
    swaymsg -p -t get_outputs |
        awk -v o="$1" '$1 == "Output" { this = ($2 == o) } this && $1 == "Current" { print $3; exit }'
}

follow() {
    for modes in /sys/class/drm/card*-*/modes; do
        want=$(head -n1 "$modes")
        [ -n "$want" ] || continue                     # nothing connected there
        output=${modes%/modes}
        output=${output##*/}
        output=${output#card*-}
        have=$(current "$output")
        [ -n "$have" ] || continue                     # not one of sway's outputs
        [ "$have" = "$want" ] && continue
        # Judged by reading the mode back, not by its status: a refusal here is what the
        # custom mode below is for, and under `set -e` it would end the loop first.
        swaymsg -q -- output "$output" mode "$want" || :
        [ "$(current "$output")" = "$want" ] ||
            swaymsg -q -- output "$output" mode --custom "$want" ||
            echo "follow-display: sway refused $want for $output" >&2
        echo "follow-display: $output $have -> $(current "$output")"
    done
}

udevadm monitor --udev --subsystem-match=drm | while read -r _ _ action _; do
    [ "$action" = change ] && follow
done
