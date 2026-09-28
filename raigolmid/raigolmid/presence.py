"""The user's presence: they are present until 2.5 s pass with no
key or pointer event on the host, and an agent's input on their face waits until they are not.

The host compositor says it through `ext-idle-notify-v1`, which takes its timeout in
milliseconds and counts pointer motion as activity; `swayidle` counts whole seconds. The
daemon speaks the Wayland wire protocol for it directly, since it needs two objects and two
events and the host's `/usr` cannot gain a binding library.

Presence that cannot be read is unknown, never absent: input waits on it as on the user, and the
reason is what the refusal says.
"""
from __future__ import annotations

import socket
import struct
import threading
import time
from pathlib import Path

from . import wayland
from .events import EventLog
from .wayland import WaylandError

IDLE_MS = 2500
# Only how soon presence is read again after the host compositor's connection fails.
RETRY_SECONDS = 5.0
NOTIFIER = "ext_idle_notifier_v1"
SEAT = "wl_seat"

# The ids this client allocates, in order: wl_display is 1 by the protocol.
_REGISTRY, _SYNC, _NOTIFIER, _SEAT, _NOTIFICATION = 2, 3, 4, 5, 6


class PresenceError(RuntimeError):
    pass


class Presence:
    def __init__(self, events: EventLog, runtime_dir: Path, display: str | None,
                 idle_ms: int = IDLE_MS) -> None:
        self.events = events
        self.idle_ms = idle_ms
        self._path = None if not display else (
            Path(display) if display.startswith("/") else runtime_dir / display)
        self._changed = threading.Condition()
        self._present = True
        self._unknown: str | None = ("WAYLAND_DISPLAY is unset, so the host compositor "
                                     "cannot say whether the user is at the machine"
                                     if self._path is None else "not yet read")

    def run(self, stop: threading.Event) -> None:
        """Watches until stopped. A connection that fails leaves presence unknown — every
        agent's input on the user's face waits on it — so it is read again rather than given up on
        for the daemon's life; each new reason is said once."""
        if self._path is None:
            stop.wait()
            return
        said = None
        while not stop.is_set():
            try:
                self._watch(stop)
            except (OSError, PresenceError, WaylandError, struct.error,
                    UnicodeDecodeError) as exc:
                with self._changed:
                    self._unknown = f"the user's presence could not be read: {exc}"
                    self._changed.notify_all()
                if str(exc) != said:
                    self.events.emit("presence.failed", error=str(exc))
                    said = str(exc)
                stop.wait(RETRY_SECONDS)

    def _watch(self, stop: threading.Event) -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.connect(str(self._path))
            conn.settimeout(1.0)
            conn.sendall(wayland.get_registry(_REGISTRY, _SYNC))
            globals_: dict[str, tuple[int, int]] = {}
            buffer = b""
            bound = False
            while not stop.is_set():
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    raise PresenceError("the host compositor closed the connection")
                messages, buffer = wayland.split(buffer + chunk)
                for sender, opcode, body in messages:
                    wayland.check(sender, opcode, body)
                    if sender == _REGISTRY and opcode == 0:
                        wayland.registry_global(body, globals_)
                    elif sender == _SYNC and not bound:
                        conn.sendall(self._bind(globals_))
                        bound = True
                        with self._changed:
                            self._unknown = None
                            self._changed.notify_all()
                    elif sender == _NOTIFICATION:
                        self._set(present=opcode == 1)

    def _bind(self, globals_: dict[str, tuple[int, int]]) -> bytes:
        return (wayland.bind(_REGISTRY, globals_, NOTIFIER, _NOTIFIER)
                + wayland.bind(_REGISTRY, globals_, SEAT, _SEAT)
                + wayland.request(_NOTIFIER, 1, struct.pack("=III", _NOTIFICATION,
                                                            self.idle_ms, _SEAT)))

    def _set(self, present: bool) -> None:
        with self._changed:
            self._present = present
            self._changed.notify_all()

    def state(self) -> dict[str, object]:
        with self._changed:
            return {"present": self._present, "unknown": self._unknown}

    def wait_until_away(self, patience: float) -> None:
        """Returns once the user has been still for the idle time; raises with why when they
        are still at it after `patience` seconds, or their presence cannot be read."""
        deadline = time.monotonic() + patience
        with self._changed:
            while self._unknown is not None or self._present:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise PresenceError(
                        self._unknown or f"the user is using the machine: no pause of "
                        f"{self.idle_ms / 1000:g}s in their keys or pointer for {patience:g}s")
                self._changed.wait(left)
