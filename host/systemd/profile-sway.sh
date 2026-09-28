# Start the host compositor on the auto-login tty and nowhere else.
# Guarded on tty1 so that an SSH session or a second VT gets a plain shell, which is the
# way in when Sway itself is what is broken.
if [ -z "$WAYLAND_DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
    export XDG_CURRENT_DESKTOP=sway
    # Nothing about rendering or the cursor is set: wlroots detects the GPU here as on bare
    # metal, and pixman would black the screen, because the window on Windows can only show
    # a GL scanout (QEMU's D-Bus display).
    exec sway
fi
