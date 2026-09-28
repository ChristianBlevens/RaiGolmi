"""Every tab drives the face, one at a time; the user's switch refuses an agent's
input outright, and otherwise it waits until they have paused for 2.5 s — read from the host compositor's
`ext-idle-notify-v1`."""
from __future__ import annotations

import socket
import struct
import threading
import time

import pytest

from raigolmid.presence import IDLE_MS, Presence, PresenceError
from raigolmid.session import SessionError
from tests.harness import Harness


@pytest.fixture()
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.session.ensure_tabs()
    return harness


def _typed(h):
    sent = []
    h.session.faces.input = lambda action, **kw: sent.append((action, kw))
    return sent


def _away(h):
    h.session.presence._unknown = None
    h.session.presence._present = False


def test_one_tab_drives_at_a_time_and_the_face_is_free_when_its_turn_ends(h, monkeypatch):
    """Every tab uses the face, a body tab too; the first input of a turn holds it, and
    another tab's waits for that turn to end, or is refused past a tool call's patience."""
    sent = _typed(h)
    _away(h)
    h.session.select("body", "myapi")
    body = h.tab("myapi")
    machine = h.session.intent.machine_tab()
    machine.busy = True
    h.session.face_input(machine.tab_id, "type", text="a")
    monkeypatch.setattr("raigolmid.session.INPUT_PATIENCE", 0.05)
    with pytest.raises(SessionError, match=f"tab {machine.tab_id} is driving"):
        h.session.face_input(body, "type", text="b")
    monkeypatch.setattr("raigolmid.session.INPUT_PATIENCE", 5.0)
    threading.Timer(0.05, h.session.agent_activity,
                    args=(machine.tab_id, False)).start()
    h.session.face_input(body, "type", text="c")
    assert [kw["text"] for _, kw in sent] == ["a", "c"]
    assert h.session._driver == body


def test_the_users_switch_refuses_it_and_is_kept(h):
    sent = _typed(h)
    _away(h)
    machine = h.session.intent.machine_tab().tab_id
    h.session.set_face_driving(False)
    with pytest.raises(SessionError, match="turned off"):
        h.session.face_input(machine, "key", text="ctrl+s")
    assert h.session.store.load().face_driving is False
    h.session.set_face_driving(True)
    h.session.face_input(machine, "key", text="ctrl+s")
    assert sent == [("key", {"text": "ctrl+s", "x": None, "y": None, "button": "left"})]


def test_input_waits_for_the_users_pause_and_unknown_presence_is_not_absence(h, monkeypatch):
    sent = _typed(h)
    machine = h.session.intent.machine_tab().tab_id
    presence = h.session.presence
    presence._unknown = "the user's presence could not be read: gone"
    monkeypatch.setattr("raigolmid.session.INPUT_PATIENCE", 0.05)
    with pytest.raises(SessionError, match="could not be read"):
        h.session.face_input(machine, "click", x=1, y=2)
    presence._unknown = None
    presence._present = True
    threading.Timer(0.02, presence._set, kwargs={"present": False}).start()
    monkeypatch.setattr("raigolmid.session.INPUT_PATIENCE", 2.0)
    h.session.face_input(machine, "click", x=1, y=2)
    assert [a for a, _ in sent] == ["click"]


def _event(sender, opcode, body=b""):
    return struct.pack("=II", sender, (8 + len(body)) << 16 | opcode) + body


def _string(text):
    raw = text.encode() + b"\0"
    return struct.pack("=I", len(raw)) + raw + b"\0" * (-len(raw) % 4)


class _Compositor:
    """The host compositor's side of the conversation, as sway 1.10 held it on the VM:
    globals, the sync's done, then whatever idle events a test sends."""

    def __init__(self, path, offers=("wl_seat", "ext_idle_notifier_v1")):
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen(1)
        self.offers = offers
        self.requests = []
        self.conn = None
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        self.conn, _ = self.server.accept()
        buf = b""
        while True:
            chunk = self.conn.recv(4096)
            if not chunk:
                return
            buf += chunk
            while len(buf) >= 8:
                sender, word = struct.unpack_from("=II", buf)
                size = word >> 16
                if len(buf) < size:
                    break
                body, buf = buf[8:size], buf[size:]
                self.requests.append((sender, word & 0xFFFF, body))
                if (sender, word & 0xFFFF) == (1, 1):          # get_registry
                    (registry,) = struct.unpack("=I", body)
                    for name, interface in enumerate(self.offers, 1):
                        self.conn.sendall(_event(registry, 0, struct.pack("=I", name)
                                                 + _string(interface)
                                                 + struct.pack("=I", 1)))
                elif (sender, word & 0xFFFF) == (1, 0):        # sync
                    (callback,) = struct.unpack("=I", body)
                    self.conn.sendall(_event(callback, 0, struct.pack("=I", 0)))

    def notification_id(self):
        (_, _, body) = next(r for r in self.requests if r[:2] == (4, 1))
        new_id, timeout, seat = struct.unpack("=III", body)
        return new_id, timeout, seat


class _Events:
    def __init__(self):
        self.emitted = []

    def emit(self, type_, **data):
        self.emitted.append((type_, data))


def _wait(predicate):
    for _ in range(200):
        if predicate():
            return
        threading.Event().wait(0.01)
    raise AssertionError("never happened")


def test_presence_is_read_from_the_compositors_idle_notification(tmp_path):
    compositor = _Compositor(tmp_path / "wayland-9")
    presence = Presence(_Events(), tmp_path, "wayland-9")
    stop = threading.Event()
    threading.Thread(target=presence.run, args=(stop,), daemon=True).start()
    _wait(lambda: presence.state()["unknown"] is None)
    _wait(lambda: any(r[:2] == (4, 1) for r in compositor.requests))
    notification, timeout, seat = compositor.notification_id()
    assert timeout == IDLE_MS and seat == 5
    assert presence.state()["present"] is True, "present until the compositor says idle"
    compositor.conn.sendall(_event(notification, 0))          # idled
    _wait(lambda: presence.state()["present"] is False)
    presence.wait_until_away(0.1)
    compositor.conn.sendall(_event(notification, 1))          # resumed
    _wait(lambda: presence.state()["present"] is True)
    with pytest.raises(PresenceError, match="using the machine"):
        presence.wait_until_away(0.05)
    stop.set()


def test_a_compositor_without_the_idle_protocol_leaves_presence_unknown(tmp_path):
    _Compositor(tmp_path / "wayland-9", offers=("wl_seat",))
    events = _Events()
    presence = Presence(events, tmp_path, "wayland-9")
    stop = threading.Event()
    watcher = threading.Thread(target=presence.run, args=(stop,), daemon=True)
    watcher.start()
    deadline = time.monotonic() + 5
    while not events.emitted and time.monotonic() < deadline:
        time.sleep(0.01)
    assert "no ext_idle_notifier_v1" in presence.state()["unknown"]
    assert events.emitted[0][0] == "presence.failed"
    with pytest.raises(PresenceError, match="no ext_idle_notifier_v1"):
        presence.wait_until_away(0.01)
    # It reads again rather than giving up for the daemon's life, and ends when stopped.
    assert watcher.is_alive()
    stop.set()
    watcher.join(10)
    assert not watcher.is_alive()


def test_the_tab_driving_the_users_face_is_said_while_its_turn_goes_on(h):
    """The control marks the user's screen from this: an agent's hands on it are
    never unmarked, and the mark goes when the turn that drove it ends."""
    _typed(h)
    _away(h)
    machine = h.session.intent.machine_tab()
    machine.busy = True
    h.session.face_input(machine.tab_id, "move", x=3, y=4)
    assert h.session.status()["face_runtime"]["driving"]["by"] == machine.tab_id
    machine.busy = False
    assert h.session.status()["face_runtime"]["driving"]["by"] is None


def test_the_users_switch_turned_off_ends_the_mark_while_the_turn_goes_on(h):
    """Nothing drives the user's face once they have said no, so nothing is said to be
    driving it."""
    _typed(h)
    _away(h)
    machine = h.session.intent.machine_tab()
    machine.busy = True
    h.session.face_input(machine.tab_id, "move", x=3, y=4)
    h.session.set_face_driving(False)
    assert h.session.status()["face_runtime"]["driving"]["by"] is None
