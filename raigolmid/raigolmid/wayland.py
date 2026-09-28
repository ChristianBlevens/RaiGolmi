"""The Wayland wire, as far as this daemon's own clients speak it (`presence.py`,
`virtualseat.py`). Each needs a few objects and events of one protocol, and the host's
`/usr` cannot gain a binding library, so they write the messages themselves.

A message is the sender's object id, then the size and opcode in one word, then the
arguments, each padded to four bytes. `wl_display` is object 1 by the protocol.
"""
from __future__ import annotations

import struct

DISPLAY = 1


class WaylandError(RuntimeError):
    pass


def string(text: str) -> bytes:
    raw = text.encode() + b"\0"
    return struct.pack("=I", len(raw)) + raw + b"\0" * (-len(raw) % 4)


def request(sender: int, opcode: int, body: bytes = b"") -> bytes:
    return struct.pack("=II", sender, (8 + len(body)) << 16 | opcode) + body


def read_string(body: bytes, at: int) -> tuple[str, int]:
    (length,) = struct.unpack_from("=I", body, at)
    text = body[at + 4:at + 4 + length - 1].decode()
    return text, at + 4 + length + (-length % 4)


def get_registry(registry: int, sync: int) -> bytes:
    """`wl_display.get_registry`, then a `sync` whose `done` marks the end of the globals."""
    return (request(DISPLAY, 1, struct.pack("=I", registry))
            + request(DISPLAY, 0, struct.pack("=I", sync)))


def bind(registry: int, globals_: dict[str, tuple[int, int]], interface: str,
         new_id: int, version: int = 1) -> bytes:
    """`wl_registry.bind` of a global the compositor announced, refused by name if it did not."""
    if interface not in globals_:
        raise WaylandError(f"the compositor offers no {interface}")
    name, _ = globals_[interface]
    return request(registry, 0, struct.pack("=I", name) + string(interface)
                   + struct.pack("=II", version, new_id))


def split(buffer: bytes) -> tuple[list[tuple[int, int, bytes]], bytes]:
    """The whole messages in `buffer` as (sender, opcode, body), and what is left over."""
    messages = []
    while len(buffer) >= 8:
        sender, word = struct.unpack_from("=II", buffer)
        size = word >> 16
        if len(buffer) < size:
            break
        messages.append((sender, word & 0xFFFF, buffer[8:size]))
        buffer = buffer[size:]
    return messages, buffer


def check(sender: int, opcode: int, body: bytes) -> None:
    """`wl_display.error` (object, code, message) raised as what the compositor said."""
    if sender == DISPLAY and opcode == 0:
        message, _ = read_string(body, 8)
        raise WaylandError(f"the compositor refused: {message}")


def registry_global(body: bytes, globals_: dict[str, tuple[int, int]]) -> None:
    """`wl_registry.global` (name, interface, version) recorded by interface."""
    (name,) = struct.unpack_from("=I", body)
    interface, at = read_string(body, 4)
    (version,) = struct.unpack_from("=I", body, at)
    globals_[interface] = (name, version)
