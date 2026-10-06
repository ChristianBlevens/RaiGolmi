"""A face works with every body: it names a sandbox on its
own socket and is handed that sandbox's launcher connection (`open_launcher`), focused or not.
The daemon's real tables and socket; the launchers are `FakeLaunchers`, whose connections are
real sockets with the far end kept."""
from __future__ import annotations

import pytest

from raigolmid import naming
from raigolmid.api import build_methods
from raigolmid.channel import Channels
from raigolmid.history import History
from raigolmid.launcher.client import BrokeredLauncherClient, LauncherUnreachable
from raigolmid.faces import Faces
from raigolmid.questions import Questions
from raigolmid.runtime.base import ContainerSpec, RuntimeError_
from tests.fakeruntime import FakeRuntime
from raigolmid.scopes import TRIAL_SANDBOX, FaceSockets
from raigolmid.viewing import Viewing
from tests.harness import Harness, answering
from tests.test_a_face_reaches_the_machine_through_its_own_socket import _spec_of
from tests.test_faces import ScriptedHost, a_face, rig, windows_of_faces  # noqa: F401


@pytest.fixture()
def face(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    methods = build_methods(h.session, h.events, Questions(h.events, h.paths),
                            Channels(h.session, h.events), Viewing(h.events, h.paths.viewing),
                            History(h.events, h.paths))
    sockets = FaceSockets(h.paths, h.events, methods, answering())
    sockets.start()
    try:
        yield h
    finally:
        sockets.close_all()


def _launcher(h, instance: str, trial: bool = False) -> BrokeredLauncherClient:
    return BrokeredLauncherClient(h.paths.face_socket_dir(trial) / "raigolmid.sock", instance)


def test_a_sandbox_the_face_is_not_showing_is_handed_over_all_the_same(face):
    h = face
    unfocused = h.open_sandbox("myapi")
    focused = h.open_sandbox("webui")
    assert h.session.intent.focused_instance == focused != unfocused

    for instance in (unfocused, focused):
        conn = _launcher(h, instance).connect()
        conn.sendall(instance.encode())
        name, far = h.launchers.connections[-1]
        assert name == instance
        assert far.recv(64) == instance.encode()
        conn.close()
        far.close()


def test_a_face_tried_off_the_users_screen_reaches_no_sandbox(face):
    h = face
    instance = h.open_sandbox("myapi")

    with pytest.raises(LauncherUnreachable, match=TRIAL_SANDBOX):
        _launcher(h, instance, trial=True).connect()
    assert h.launchers.connections == []


# --- the machine's network ------------------------------------------------------------------

def test_every_sandbox_is_on_the_machines_network_by_its_name(face):
    h = face
    for body in ("myapi", "webui"):
        instance = h.open_sandbox(body)
        spec = h.runtime.spec_of(naming.anchor(instance))
        assert spec.network == naming.network() and spec.network_mode is None
        assert spec.aliases == (naming.host(instance),)
    assert naming.host("myapi@tab-2") == "myapi.tab-2"
    # The door forwards to the focused anchor's address, which is on that network.
    door = h.runtime.spec_of(naming.door())
    assert door.network == naming.network()
    anchor = h.runtime.inspect(naming.anchor(h.session.intent.focused_instance))
    assert anchor.ip in door.command


def test_the_users_face_is_face_on_the_network_and_a_trial_is_not_on_it(rig, tmp_path, monkeypatch):
    runtime, paths, events = rig
    faces = Faces(runtime, paths, events, host=ScriptedHost(windows_of_faces(runtime)))
    monkeypatch.setattr(faces, "_screen_size", lambda: None)
    theirs = _spec_of(runtime, monkeypatch, lambda: faces.start(a_face(tmp_path)))
    trial = _spec_of(runtime, monkeypatch, lambda: faces.start_trial(a_face(tmp_path)))

    assert theirs.network == naming.network() and theirs.aliases == (naming.FACE_HOST,)
    assert trial.network is None and trial.aliases == ()


def test_the_fake_refuses_what_docker_refuses_of_a_network():
    runtime = FakeRuntime()
    runtime.add_image("img")
    with pytest.raises(RuntimeError_, match="network nope not found"):
        runtime.run(ContainerSpec(name="a", image="img", network="nope"))
    runtime.ensure_network("n", {})
    with pytest.raises(RuntimeError_, match="can not be used together"):
        runtime.run(ContainerSpec(name="b", image="img", network="n",
                                  network_mode="container:x"))
    with pytest.raises(RuntimeError_, match="no network to answer on"):
        runtime.run(ContainerSpec(name="c", image="img", aliases=("x",)))
