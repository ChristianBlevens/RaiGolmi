"""A pointer and a keyboard plugged into a face that has none: `python3 -m
raigolmid.virtualseat`, run inside a face tried off the user's screen for as long as it runs.

A headless compositor's seat has no devices. With no pointer its clients never bind one, and
a press through the compositor's own seat (`Faces.input`) reaches nobody while reporting
success. With no keyboard, each `wtype` brings one and takes it away again, and keys sent
before a client has bound the new one are lost. The user's face's seat has the host's two for as
long as it runs, so this client creates a `zwlr_virtual_pointer_v1` and a
`zwp_virtual_keyboard_v1` and holds its connection, which is what keeps them plugged in.
It sends no input of its own.

It reads `XDG_RUNTIME_DIR` and `WAYLAND_DISPLAY` as any client does, and exits non-zero with
the reason when the compositor refuses it or goes away.
"""
from __future__ import annotations

import os
import socket
import struct
import sys

from . import wayland

POINTERS = "zwlr_virtual_pointer_manager_v1"
KEYBOARDS = "zwp_virtual_keyboard_manager_v1"
SEAT = "wl_seat"

# A keyboard needs a keymap before the compositor takes it as one; this one has no keys,
# because the keyboard never sends any — `wtype` brings its own.
KEYMAP = b"""xkb_keymap {
xkb_keycodes "(unnamed)" { minimum = 8; maximum = 255; };
xkb_types "(unnamed)" { include "complete" };
xkb_compatibility "(unnamed)" { include "complete" };
xkb_symbols "(unnamed)" { };
};
\0"""
XKB_V1 = 1

(_REGISTRY, _SYNC, _SEAT, _POINTERS, _KEYBOARDS, _POINTER, _KEYBOARD,
 _CREATED) = range(2, 10)


def _keymap_fd() -> int:
    fd = os.memfd_create("keymap")
    os.write(fd, KEYMAP)
    return fd


def hold(path: str) -> None:
    """Plug both devices into the compositor at `path`, then hold them until the
    connection ends, which is only ever a failure."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.connect(path)
        conn.sendall(wayland.get_registry(_REGISTRY, _SYNC))
        globals_: dict[str, tuple[int, int]] = {}
        buffer = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                raise wayland.WaylandError("the compositor closed the connection")
            messages, buffer = wayland.split(buffer + chunk)
            for sender, opcode, body in messages:
                wayland.check(sender, opcode, body)
                if sender == _REGISTRY and opcode == 0:
                    wayland.registry_global(body, globals_)
                elif sender == _SYNC:
                    conn.sendall(wayland.bind(_REGISTRY, globals_, SEAT, _SEAT)
                                 + wayland.bind(_REGISTRY, globals_, POINTERS, _POINTERS)
                                 + wayland.bind(_REGISTRY, globals_, KEYBOARDS, _KEYBOARDS)
                                 # create_virtual_pointer(seat, id)
                                 + wayland.request(_POINTERS, 0,
                                                   struct.pack("=II", _SEAT, _POINTER))
                                 # create_virtual_keyboard(seat, id)
                                 + wayland.request(_KEYBOARDS, 0,
                                                   struct.pack("=II", _SEAT, _KEYBOARD)))
                    # keymap(format, fd, size): the fd travels beside the message.
                    fd = _keymap_fd()
                    try:
                        socket.send_fds(conn, [wayland.request(
                            _KEYBOARD, 0, struct.pack("=II", XKB_V1, len(KEYMAP)))], [fd])
                    finally:
                        os.close(fd)
                    # A sync after, to hear a refusal by.
                    conn.sendall(wayland.request(wayland.DISPLAY, 0,
                                                 struct.pack("=I", _CREATED)))
                elif sender == _CREATED:
                    print(f"holding a pointer and a keyboard on {path}", flush=True)


def main() -> int:
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    display = os.environ.get("WAYLAND_DISPLAY", "")
    if not runtime or not display:
        print("XDG_RUNTIME_DIR and WAYLAND_DISPLAY name the compositor; one is unset",
              file=sys.stderr)
        return 2
    try:
        hold(os.path.join(runtime, display))
    except (OSError, wayland.WaylandError, struct.error, UnicodeDecodeError) as exc:
        print(f"virtual seat: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
