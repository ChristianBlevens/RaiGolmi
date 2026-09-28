"""The user's face's clipboard and the host's are one (`clipboard.py`).

`wl-copy` and `wl-paste` are doubles on PATH, and they keep each display's selection in a file
beside its socket. They refuse what the real tools refuse: no socket, no clipboard; a watch reports the selection it finds as it
starts, runs its command only for a text offer, and ends when its compositor goes; a
compositor without data-control refuses the watch.
"""
from __future__ import annotations

import os
import socket
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from raigolmid.clipboard import ClipboardBridge, ClipboardError
from raigolmid.events import EventLog
from raigolmid.faces import FaceRuntimeState
from raigolmid.history import SAYS
from raigolmid.paths import Paths

_COMMON = r'''
import os, socket as sockets, subprocess, sys, time
from pathlib import Path
runtime, display = os.environ["XDG_RUNTIME_DIR"], os.environ["WAYLAND_DISPLAY"]
socket = Path(display if display.startswith("/") else os.path.join(runtime, display))
selection = Path(f"{socket}.selection")
def compositor_up():
    with sockets.socket(sockets.AF_UNIX) as probe:
        try:
            probe.connect(str(socket))
        except OSError:
            return False
    return True
if not compositor_up():
    print("Failed to connect to a Wayland server: No such file or directory", file=sys.stderr)
    sys.exit(1)
with open(os.environ["FAKE_WL_LOG"], "a") as log:
    log.write(f"{Path(sys.argv[0]).name} {socket}"
              + (" primary" if "--primary" in sys.argv else "") + "\n")
'''

_WL_COPY = _COMMON + r'''
args = sys.argv[1:]
if args == ["--clear"]:
    selection.unlink(missing_ok=True)
    sys.exit(0)
if args[0] == "--primary":   # its own selection, which no clipboard watch sees
    selection, args = Path(f"{socket}.primary"), args[1:]
assert args[0] == "--type", args
Path(f"{socket}.offered").write_text(args[1])
tmp = Path(f"{selection}.tmp")
tmp.write_bytes(sys.stdin.buffer.read())
tmp.rename(selection)   # a new inode: a copy of the same text is still a new selection
'''

_WL_PASTE = _COMMON + r'''
args = sys.argv[1:]
assert args[:5] == ["--no-newline", "--type", "text", "--watch", "sh"], args
if Path(f"{socket}.no-data-control").exists():
    print("Watch mode requires a compositor that supports the wlroots data-control protocol",
          file=sys.stderr)
    sys.exit(1)
command, seen = args[4:], None
while True:
    if not compositor_up():
        print("Error reading from the Wayland display: Broken pipe", file=sys.stderr)
        sys.exit(1)
    try:
        now = selection.stat().st_ino
    except FileNotFoundError:
        now = 0
    if now != seen:
        seen = now
        if now and Path(f"{socket}.image-only").exists():
            pass   # no text type offered: the command is not run
        elif now:
            with open(selection, "rb") as data:
                subprocess.run(command, stdin=data, env=dict(os.environ, CLIPBOARD_STATE="data"))
        else:
            subprocess.run(command, stdin=subprocess.DEVNULL,
                           env=dict(os.environ, CLIPBOARD_STATE="nil"))
        sys.stdout.flush()
    time.sleep(0.01)
'''


@pytest.fixture
def rig(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("wl-copy", _WL_COPY), ("wl-paste", _WL_PASTE)):
        path = bin_dir / name
        path.write_text(f"#!{sys.executable}\n{body}")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_WL_LOG", str(tmp_path / "wl.log"))
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    runtime = tmp_path / "run"
    runtime.mkdir()
    paths = Paths(state=tmp_path / "state", data=tmp_path / "data",
                  config=tmp_path / "config", runtime=runtime)
    paths.state.mkdir(parents=True)
    paths.face_runtime.mkdir()
    compositors: list[socket.socket] = []
    _compositor(runtime / "wayland-1", compositors)
    yield paths, EventLog(paths.events), tmp_path / "wl.log", compositors
    for listener in compositors:
        listener.close()


def _compositor(path: Path, compositors: list[socket.socket]) -> socket.socket:
    """A display socket that really answers, as a compositor's does."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(16)
    compositors.append(listener)
    threading.Thread(target=_accept, args=(listener,), daemon=True).start()
    return listener


def _accept(listener: socket.socket) -> None:
    while True:
        try:
            client, _ = listener.accept()
        except OSError:
            return  # closed: the compositor is gone
        client.close()


class Clipboards:
    """Both displays as the doubles keep them, and the bridge running between them."""

    def __init__(self, paths: Paths, events: EventLog, log: Path,
                 face: FaceRuntimeState | None = None) -> None:
        self.paths, self.events, self.log = paths, events, log
        self.bridge = ClipboardBridge(events, paths, lambda: face)
        self.stop = threading.Event()
        self.raised: list[BaseException] = []
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            self.bridge.run(self.stop)
        except BaseException as exc:
            self.raised.append(exc)

    def __enter__(self) -> "Clipboards":
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop.set()
        self.thread.join(5)

    def host(self) -> Path:
        return self.paths.runtime / "wayland-1"

    def face(self, display: str = "wayland-1") -> Path:
        return self.paths.face_runtime / display

    @staticmethod
    def held(socket: Path) -> bytes | None:
        try:
            return Path(f"{socket}.selection").read_bytes()
        except FileNotFoundError:
            return None

    @staticmethod
    def primary(socket: Path) -> bytes | None:
        try:
            return Path(f"{socket}.primary").read_bytes()
        except FileNotFoundError:
            return None

    @staticmethod
    def copy(socket: Path, text: bytes) -> None:
        tmp = Path(f"{socket}.selection.tmp")
        tmp.write_bytes(text)
        tmp.rename(Path(f"{socket}.selection"))

    def copies_to(self, socket: Path) -> int:
        if not self.log.exists():
            return 0
        return self.log.read_text().splitlines().count(f"wl-copy {socket}")

    def events_of(self, type_: str) -> list[dict]:
        return [e.data for e in self.events.tail(1000) if e.type == type_]


def _until(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("not within the wait")
        time.sleep(0.02)


def _face_up(events: EventLog, compositors: list[socket.socket], display: Path, *,
             face: str = "minimal", container: str = "raigolmid-face-minimal") -> socket.socket:
    listener = _compositor(display, compositors)
    events.emit("face.started", face=face, container=container, pid=1234,
                wayland_display=display.name, fullscreen=False)
    return listener


def _joined(clips: Clipboards, count: int = 1) -> None:
    _until(lambda: len(clips.events_of("clipboard.joined")) >= count)


def test_text_copied_on_either_side_is_pasted_on_the_other(rig):
    paths, events, log, compositors = rig
    with Clipboards(paths, events, log) as clips:
        _face_up(events, compositors, clips.face())
        _joined(clips)
        clips.copy(clips.host(), b"from windows\r\nsecond line")
        _until(lambda: clips.held(clips.face()) == b"from windows\r\nsecond line")
        # A terminal's right-click pastes the primary.
        _until(lambda: clips.primary(clips.face()) == b"from windows\r\nsecond line")
        assert Path(f"{clips.face()}.offered").read_text() == "text/plain;charset=utf-8"
        clips.copy(clips.face(), b"from a face app")
        _until(lambda: clips.held(clips.host()) == b"from a face app")


def test_a_carried_text_is_not_carried_back(rig):
    paths, events, log, compositors = rig
    with Clipboards(paths, events, log) as clips:
        _face_up(events, compositors, clips.face())
        _joined(clips)
        clips.copy(clips.host(), b"once")
        _until(lambda: clips.held(clips.face()) == b"once")
        time.sleep(0.5)
        assert clips.copies_to(clips.face()) == 1
        assert clips.copies_to(clips.host()) == 0


def test_a_face_that_joins_takes_the_hosts_text_over_its_own(rig):
    paths, events, log, compositors = rig
    face_socket = paths.face_runtime / "wayland-1"
    _compositor(face_socket, compositors)
    Clipboards.copy(face_socket, b"the face's old text")
    Clipboards.copy(paths.runtime / "wayland-1", b"the host's text")
    adopted = FaceRuntimeState(face_id="minimal", container="raigolmid-face-minimal", pid=1234,
                               wayland_display="wayland-1", fullscreen=False,
                               runtime_dir=paths.face_runtime)
    with Clipboards(paths, events, log, face=adopted) as clips:
        _joined(clips)
        _until(lambda: clips.held(face_socket) == b"the host's text")
        time.sleep(0.5)
        assert clips.held(clips.host()) == b"the host's text"


def test_a_cleared_selection_and_an_image_are_not_carried(rig):
    paths, events, log, compositors = rig
    with Clipboards(paths, events, log) as clips:
        _face_up(events, compositors, clips.face())
        _joined(clips)
        clips.copy(clips.host(), b"kept")
        _until(lambda: clips.held(clips.face()) == b"kept")
        Path(f"{clips.host()}.selection").unlink()
        Path(f"{clips.host()}.image-only").touch()
        clips.copy(clips.host(), b"\x89PNG")
        time.sleep(0.5)
        assert clips.held(clips.face()) == b"kept"


def test_a_trial_face_is_never_joined(rig):
    paths, events, log, compositors = rig
    with Clipboards(paths, events, log) as clips:
        trial = paths.face_trial_runtime / "wayland-1"
        trial.parent.mkdir()
        _compositor(trial, compositors)
        events.emit("face.trial_started", face="minimal", container="raigolmid-face-trial",
                    pid=99, wayland_display="wayland-1", fullscreen=False)
        clips.copy(clips.host(), b"not for the trial")
        time.sleep(0.5)
        assert clips.events_of("clipboard.joined") == []
        assert clips.copies_to(trial) == 0


def test_a_face_that_stops_is_left_unsaid_and_the_next_one_is_joined(rig):
    paths, events, log, compositors = rig
    with Clipboards(paths, events, log) as clips:
        listener = _face_up(events, compositors, clips.face())
        _joined(clips)
        # `Faces.stop`'s order: the compositor is gone before `face.stopped` is said.
        listener.close()
        time.sleep(0.3)
        events.emit("face.stopped", face="minimal", container="raigolmid-face-minimal")
        clips.face().unlink()
        clips.copy(clips.host(), b"while no face is up")
        time.sleep(0.3)
        second = clips.face("wayland-2")
        _face_up(events, compositors, second, face="other", container="raigolmid-face-other")
        _joined(clips, 2)
        _until(lambda: clips.held(second) == b"while no face is up")
        assert clips.events_of("clipboard.unbridged") == []


def test_a_face_whose_compositor_refuses_the_watch_is_said(rig):
    paths, events, log, compositors = rig
    with Clipboards(paths, events, log) as clips:
        Path(f"{clips.face()}.no-data-control").touch()
        _face_up(events, compositors, clips.face())
        _until(lambda: clips.events_of("clipboard.unbridged"))
        [said] = clips.events_of("clipboard.unbridged")
        assert said["face"] == "minimal"
        assert "data-control" in said["reason"] and "exited 1" in said["reason"]
        event = next(e for e in events.tail(1000) if e.type == "clipboard.unbridged")
        assert "data-control" in SAYS["clipboard.unbridged"](event)


def test_the_host_watch_ending_ends_the_bridge_with_its_reason(rig):
    paths, events, log, compositors = rig
    with Clipboards(paths, events, log) as clips:
        _until(lambda: clips.log.exists())
        compositors[0].close()
        clips.thread.join(5)
        [raised] = clips.raised
        assert isinstance(raised, ClipboardError)
        assert "Broken pipe" in str(raised)


