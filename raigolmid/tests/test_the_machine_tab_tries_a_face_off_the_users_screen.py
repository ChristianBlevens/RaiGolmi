"""The machine tab runs a face off the user's screen and drives it."""
from __future__ import annotations

import stat
import struct
import threading

import pytest

from raigolmid import labels, naming, virtualseat, wayland
from raigolmid.channel import Channels
from raigolmid.questions import Questions
from raigolmid.scopes import build_tab_methods
from raigolmid.faces import FaceError, Faces
from raigolmid.session import SessionError
from tests.harness import Harness
from tests.test_faces import ScriptedHost, a_face, rig, windows_of_faces  # noqa: F401
from tests.test_the_users_presence_gates_an_agents_input import _Compositor, _typed


def test_a_trial_is_its_own_headless_compositor_and_never_touches_the_users_face(
        rig, tmp_path, monkeypatch):
    runtime, paths, events = rig
    host = ScriptedHost(windows_of_faces(runtime))
    faces = Faces(runtime, paths, events, host=host)
    monkeypatch.setattr(faces, "_screen_size", lambda: None)
    face = a_face(tmp_path)
    faces.start(face)
    host.calls.clear()
    specs = []
    original = runtime.run
    monkeypatch.setattr(runtime, "run", lambda spec: specs.append(spec) or original(spec))

    state = faces.start_trial(face)

    spec, = specs
    trial_dir = paths.face_trial_runtime
    assert spec.name == state.container == naming.face_trial()
    assert spec.labels[labels.ROLE] == str(labels.Role.FACE_TRIAL)
    assert spec.environment["XDG_RUNTIME_DIR"] == str(trial_dir)
    assert spec.environment["WLR_BACKENDS"] == "headless"
    assert "WAYLAND_DISPLAY" not in spec.environment
    assert [m.source for m in spec.mounts if m.target.startswith(str(paths.runtime))] == [
        str(trial_dir)]
    assert stat.S_IMODE(trial_dir.stat().st_mode) == 0o700
    assert not host.calls, "the user's compositor was asked about the trial"
    assert runtime.inspect(naming.face("writing")).status == "running"

    faces.stop_trial()
    assert runtime.inspect(naming.face_trial()) is None and not trial_dir.exists()
    assert runtime.inspect(naming.face("writing")).status == "running"


def _holding(tmp_path, offers):
    display = tmp_path / "wayland-1"
    compositor = _Compositor(display, offers=offers)
    failure = []

    def run():
        try:
            virtualseat.hold(str(display))
        except wayland.WaylandError as exc:
            failure.append(str(exc))

    threading.Thread(target=run, daemon=True).start()
    return compositor, failure


def _wait(predicate):
    for _ in range(300):
        if predicate():
            return
        threading.Event().wait(0.01)
    raise AssertionError("never happened")


def test_the_pointer_and_keyboard_are_created_on_the_seat_it_bound(tmp_path):
    """Devices on the trial's seat are what make its input reach a client:
    created on the seat, and the keyboard given its keymap."""
    compositor, failure = _holding(
        tmp_path, ("wl_seat", virtualseat.POINTERS, virtualseat.KEYBOARDS))
    _wait(lambda: any(r[:2] == (8, 0) for r in compositor.requests))
    bound = {struct.unpack_from("=I", b, len(b) - 4)[0]: b for s, o, b in compositor.requests
             if (s, o) == (2, 0)}
    (_, _, pointer), = [r for r in compositor.requests if r[:2] == (5, 0)]
    (_, _, keyboard), = [r for r in compositor.requests if r[:2] == (6, 0)]
    for body in (pointer, keyboard):
        seat, new_id = struct.unpack("=II", body)
        assert b"wl_seat" in bound[seat] and new_id not in bound
    (_, _, keymap), = [r for r in compositor.requests if r[:2] == (8, 0)]
    assert struct.unpack("=II", keymap) == (virtualseat.XKB_V1, len(virtualseat.KEYMAP))
    assert not failure


@pytest.fixture()
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.session.ensure_tabs()
    return harness


def test_only_the_machine_tab_tries_and_drives_a_trial(h):
    h.session.select("body", "myapi")
    body = h.tab("myapi")
    for call in (lambda: h.session.try_face(body, "minimal"),
                 lambda: h.session.stop_trial(body),
                 lambda: h.session.screenshot(body, trial=True),
                 lambda: h.session.face_input(body, "click", x=1, y=1, trial=True)):
        with pytest.raises(SessionError, match="only the machine tab"):
            call()


def test_the_trial_is_driven_past_the_users_switch_and_their_presence(h):
    """The user is not using a face off their screen, so neither their switch nor their presence
    holds the machine tab's hands on it."""
    sent = _typed(h)
    h.session.set_face_driving(False)
    h.session.presence._unknown = "not read"
    machine = h.session.intent.machine_tab().tab_id
    h.session.face_input(machine, "click", x=3, y=4, trial=True)
    assert sent == [("click", {"text": None, "x": 3, "y": 4, "button": "left",
                               "trial": True})]


def test_a_trial_that_does_not_come_up_is_removed(h, monkeypatch):
    stopped = []
    faces = h.session.faces
    monkeypatch.setattr(faces, "start_trial", lambda face: (_ for _ in ()).throw(
        FaceError("the compositor exited before opening a display")))
    monkeypatch.setattr(faces, "stop_trial", lambda: stopped.append(True))
    machine = h.session.intent.machine_tab().tab_id
    face = next(iter(h.session.catalogue.faces))
    with pytest.raises(SessionError, match="did not come up off the user's screen"):
        h.session.try_face(machine, face)
    assert stopped == [True]


def test_the_machine_tab_sees_its_trial_in_its_status_and_a_body_tab_does_not(h):
    h.session.select("body", "myapi")
    machine = h.session.intent.machine_tab().tab_id

    def methods(tab):
        return build_tab_methods(h.session, Questions(h.events, h.paths),
                                 Channels(h.session, h.events), tab)

    assert methods(machine)["status"]()["trial"] is None
    assert "trial" not in methods(h.tab("myapi"))["status"]()
