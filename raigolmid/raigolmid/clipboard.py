"""The machine's clipboard and the user's face's are one clipboard.

A face's nested compositor keeps its own selection, so without this, text copied on Windows reaches the host and stops there, and a face
app's copy never leaves the face. The bridge watches both selections with `wl-paste --watch`
and gives each change to the other side with `wl-copy`. Text only: an offer with no text type
runs no watch command at all.

It follows whichever of the user's faces is up — the one found running when the daemon starts,
which covers a face adopted across a restart, then `face.started` and `face.stopped`. A trial
face is never joined: it is off the user's screen and its clipboard is nobody's. The host
compositor is followed the same way: one that goes (`clipboard.host_gone`, the machine shutting
down) is waited for, never reported as the bridge failing. A face that
joins is given the host's text before its own watch starts, so the host's selection wins and
the two never cross over each other.

Echoes are suppressed the way the launcher's `Bridge` does it (`windows/RaiGolmi.cs`): each
side remembers the texts it was given and has not yet reported back, and the text it is known
to hold.
"""
from __future__ import annotations

import base64
import os
import queue
import signal
import socket
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import IO, Callable

from .events import Event, EventLog
from .faces import FaceRuntimeState
from .paths import Paths

# One line of base64 per change, and an empty line for an empty selection. `--watch` reports
# the selection it finds as it starts, empty or not, so the host's first line always comes.
WATCH = ("wl-paste", "--no-newline", "--type", "text", "--watch", "sh", "-c",
         '[ "$CLIPBOARD_STATE" = data ] && base64 -w0; echo')
# The host's first report is immediate; one that never comes is a watch that is not working.
FIRST_REPORT_TIMEOUT = 10.0
# Named rather than sniffed from the content, which could offer text as something else.
OFFERED = "text/plain;charset=utf-8"
# A text `wl-copy` took and the side never reported back would otherwise be kept forever.
ECHOES = 16
COPY_TIMEOUT = 10.0
# How often a gone host compositor is asked whether it is back.
HOST_RETURN_POLL = 1.0


class ClipboardError(RuntimeError):
    """The host's selection can no longer be watched."""


class _Side:
    """One compositor's selection: how to reach it, and what it was given and holds."""

    def __init__(self, name: str, runtime_dir: Path, display: str,
                 lines: queue.Queue[tuple[_Side, bytes | None]]) -> None:
        self.name = name
        self.env = dict(os.environ, XDG_RUNTIME_DIR=str(runtime_dir), WAYLAND_DISPLAY=display)
        self.display = runtime_dir / display
        self.given: deque[bytes] = deque(maxlen=ECHOES)
        self.held: bytes | None = None
        self._lines = lines
        self._process: subprocess.Popen[bytes] | None = None
        self._stderr: IO[bytes] | None = None

    def watch(self) -> None:
        self._stderr = tempfile.TemporaryFile()
        # Its own process group, so a watch command in flight goes with it.
        self._process = subprocess.Popen(WATCH, env=self.env, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=self._stderr,
                                         start_new_session=True)
        threading.Thread(target=self._read, name=f"clipboard-{self.name}", daemon=True).start()

    def _read(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for line in self._process.stdout:
            self._lines.put((self, line.strip()))
        self._lines.put((self, None))

    def ended(self) -> str:
        """How its watch ended: the exit status and everything it wrote to stderr."""
        assert self._process is not None and self._stderr is not None
        code = self._process.wait()
        self._stderr.seek(0)
        said = self._stderr.read().decode(errors="replace").strip()
        return f"wl-paste exited {code}" + (f": {said}" if said else " and said nothing")

    def answering(self) -> bool:
        """Whether its compositor is still up: the display socket accepts a connection."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            try:
                probe.connect(str(self.display))
            except (FileNotFoundError, ConnectionRefusedError):
                return False
        return True

    def unwatch(self) -> None:
        if self._process is None:
            return
        try:
            os.killpg(self._process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass  # it had already ended, which its reader reports
        self._process.wait()

    def give(self, text: bytes) -> str | None:
        """Sets this side's clipboard and primary selection to `text`; why it could not, or
        None. The primary too, because a terminal's right-click pastes the primary (foot has
        no clipboard paste on a button), and it must paste the text copied on the other side."""
        for selection in ((), ("--primary",)):
            failed = self._copy(text, selection)
            if failed is not None:
                return failed
        self.given.append(text)
        self.held = text
        return None

    def _copy(self, text: bytes, selection: tuple[str, ...]) -> str | None:
        # The forked `wl-copy` that serves the selection outlives the call and would hold a
        # pipe open, so what it says goes to a file.
        with tempfile.TemporaryFile() as stderr:
            try:
                done = subprocess.run(("wl-copy", *selection, "--type", OFFERED), input=text,
                                      env=self.env, stdout=subprocess.DEVNULL, stderr=stderr,
                                      timeout=COPY_TIMEOUT)
            except subprocess.TimeoutExpired:
                return f"wl-copy did not return within {COPY_TIMEOUT:g} s"
            if done.returncode != 0:
                stderr.seek(0)
                said = stderr.read().decode(errors="replace").strip()
                return f"wl-copy exited {done.returncode}" + (f": {said}" if said else "")
        return None


class ClipboardBridge:
    """Subscribed at construction, so a face started while the daemon starts is not missed."""

    def __init__(self, events: EventLog, paths: Paths,
                 current_face: Callable[[], FaceRuntimeState | None]) -> None:
        self.events = events
        self.paths = paths
        self.current_face = current_face
        self._sub = events.subscribe()
        self._lines: queue.Queue[tuple[_Side, bytes | None]] = queue.Queue()
        self._host: _Side | None = None
        self._face: _Side | None = None
        self._face_container: str | None = None

    def run(self, stop: threading.Event) -> None:
        display = os.environ.get("WAYLAND_DISPLAY", "")
        if not display:
            # No host compositor: nothing to join, and a thread that ends is a dead part.
            stop.wait()
            return
        while not stop.is_set():
            self._host = _Side("host", self.paths.runtime, display, self._lines)
            self._host.watch()
            try:
                self._bridge(stop)
            finally:
                self._leave()
                self._host.unwatch()
            # The host compositor went away: the machine shutting down, or the compositor
            # restarting. The bridge resumes when it answers again.
            while not stop.is_set() and not self._host.answering():
                stop.wait(HOST_RETURN_POLL)

    def _bridge(self, stop: threading.Event) -> None:
        """Until stopped, or until the host compositor is gone."""
        assert self._host is not None
        # What the host holds is known before any face joins, so a face adopted across a
        # restart is given the host's text rather than racing it.
        try:
            first = self._lines.get(timeout=FIRST_REPORT_TIMEOUT)
        except queue.Empty:
            raise ClipboardError(f"the host's clipboard watch reported nothing within "
                                 f"{FIRST_REPORT_TIMEOUT:g} s") from None
        if not self._take(*first):
            return
        face = self.current_face()
        if face is not None and face.wayland_display:
            self._join(face.container, face.face_id, face.wayland_display)
        while not stop.is_set():
            try:
                if not self._take(*self._lines.get(timeout=0.25)):
                    return
                while True:
                    if not self._take(*self._lines.get_nowait()):
                        return
            except queue.Empty:
                pass
            for event in self._sub.drain(timeout=0):
                self._on_event(event)

    def _on_event(self, event: Event) -> None:
        if event.type == "face.started" and event.data["wayland_display"]:
            self._join(event.data["container"], event.data["face"],
                       event.data["wayland_display"])
        elif event.type == "face.stopped" and event.data["container"] == self._face_container:
            self._leave()

    def _join(self, container: str, face_id: str, display: str) -> None:
        if container == self._face_container:
            return
        self._leave()
        assert self._host is not None
        face = _Side(f"face {face_id}", self.paths.face_runtime, display, self._lines)
        if self._host.held is not None:
            self._carry(self._host.held, face)
        face.watch()
        self._face, self._face_container = face, container
        self.events.emit("clipboard.joined", face=face_id, display=display)

    def _leave(self) -> None:
        if self._face is None:
            return
        face, self._face, self._face_container = self._face, None, None
        face.unwatch()

    def _take(self, side: _Side, line: bytes | None) -> bool:
        """Whether the host compositor is still there to bridge."""
        if side is not self._host and side is not self._face:
            return True  # a side that has left, whose last lines were still queued
        if line is None:
            if side is self._host:
                reason = side.ended()
                # A compositor still answering refused or dropped the watch; one that is gone
                # took it with it, which is the machine shutting down or the compositor
                # restarting, not the bridge failing. An exiting compositor drops its clients
                # before it closes its socket, so it is asked once it has had time to finish.
                time.sleep(HOST_RETURN_POLL)
                if side.answering():
                    raise ClipboardError(f"the host's clipboard is no longer watched: {reason}")
                self.events.emit("clipboard.host_gone", reason=reason)
                return False
            self._face, self._face_container = None, None
            reason = side.ended()
            # A face being stopped loses its compositor before `face.stopped` is emitted, and
            # that ending is the face's own to report. A compositor still answering refused the
            # watch — one without the data-control protocol does — or dropped it.
            if side.answering():
                self.events.emit("clipboard.unbridged", face=side.name.removeprefix("face "),
                                 reason=reason)
            return True
        if not line:
            return True  # an empty selection is never carried: clearing one side keeps the other's
        text = base64.b64decode(line)
        if text in side.given:
            while side.given.popleft() != text:
                pass
            side.held = text
            return True
        if text == side.held:
            return True
        side.held = text
        other = self._face if side is self._host else self._host
        if other is not None:
            self._carry(text, other)
        return True

    def _carry(self, text: bytes, to: _Side) -> None:
        refused = to.give(text)
        if refused is not None:
            self.events.emit("clipboard.copy_failed", to=to.name, reason=refused)
