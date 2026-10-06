"""A face asks the host to show the AI terminal, keeps the user's things, and sees the
machine read-only — through its own socket, and nothing else of the host's runtime dir."""
from __future__ import annotations

import threading

import pytest

from raigolmid import hostsurfaces
from raigolmid.client import ApiClient, ApiError
from raigolmid.faces import FACE_HOME, FACE_SOCKET_MOUNT, HOST_WAYLAND, Faces
from raigolmid.runtime.base import Mount
from raigolmid.scopes import FACE, TRIAL_TERMINAL, FaceSockets, build_face_methods
from raigolmid.session import SessionError
from tests.harness import answering
from tests.test_faces import ScriptedHost, a_face, rig, windows_of_faces  # noqa: F401


def _spec_of(runtime, monkeypatch, start):
    """The face container's spec, of everything `start` runs."""
    specs = []
    original = runtime.run
    monkeypatch.setattr(runtime, "run", lambda spec: specs.append(spec) or original(spec))
    state = start()
    spec, = (spec for spec in specs if spec.name == state.container)
    return spec


def test_the_users_face_is_given_the_hosts_display_and_nothing_else_of_its_runtime_dir(
        rig, tmp_path, monkeypatch):
    """The user's runtime dir holds the host compositor's IPC socket, D-Bus, the user's
    systemd and the daemon's unscoped socket, each of which runs host commands."""
    runtime, paths, events = rig
    faces = Faces(runtime, paths, events, host=ScriptedHost(windows_of_faces(runtime)))
    spec = _spec_of(runtime, monkeypatch, lambda: faces.start(a_face(tmp_path)))

    from_runtime = [m for m in spec.mounts
                    if m.source == str(paths.runtime)
                    or m.source.startswith(f"{paths.runtime}/")]
    assert {m.source for m in from_runtime} == {
        str(paths.runtime / "wayland-0"), str(paths.face_runtime),
        str(paths.face_socket_dir(trial=False))}
    assert spec.environment["XDG_RUNTIME_DIR"] == str(paths.face_runtime)
    assert Mount(source=str(paths.face_socket_dir(trial=False)), target=FACE_SOCKET_MOUNT,
                 read_only=True) in spec.mounts
    assert spec.environment["RAIGOLMID_SOCKET"] == f"{FACE_SOCKET_MOUNT}/raigolmid.sock"
    assert spec.environment["RAIGOLMID_FACE"] == "1"
    # The user's things outlive the face, and every face on their screen has them.
    assert Mount(source=str(paths.face_home), target=FACE_HOME) in spec.mounts
    # And what they drop from Windows.
    assert Mount(source=str(paths.transfer), target=f"{FACE_HOME}/Transfer") in spec.mounts


def test_a_trial_has_its_own_socket_and_none_of_the_users_things(rig, tmp_path, monkeypatch):
    runtime, paths, events = rig
    faces = Faces(runtime, paths, events, host=ScriptedHost(windows_of_faces(runtime)))
    monkeypatch.setattr(faces, "_screen_size", lambda: None)
    spec = _spec_of(runtime, monkeypatch, lambda: faces.start_trial(a_face(tmp_path)))

    assert Mount(source=str(paths.face_socket_dir(trial=True)), target=FACE_SOCKET_MOUNT,
                 read_only=True) in spec.mounts
    assert not any(m.target.startswith(FACE_HOME) or m.target == HOST_WAYLAND
                   for m in spec.mounts)
    # It shows no instance, so no launcher of one is within its reach.
    assert not any(m.source == str(paths.view_sockets)
                   for m in spec.mounts)


def test_a_trial_asking_for_the_terminal_is_answered_and_the_users_screen_is_not_touched(
        rig, monkeypatch):
    runtime, paths, events = rig
    monkeypatch.setattr(hostsurfaces, "show_ai_terminal_for_face",
                        lambda: pytest.fail("a trial reached the user's screen"))

    assert build_face_methods({n: lambda: n for n in FACE}, events,
                              trial=True)["show_ai_terminal"]() == TRIAL_TERMINAL


def test_the_face_socket_answers_its_table_and_refuses_the_rest(rig):
    runtime, paths, events = rig
    full = {name: (lambda name=name, **params: name) for name in (*FACE, "repair")}
    sockets = FaceSockets(paths, events, full, answering())
    sockets.start()
    try:
        client = ApiClient(paths.face_socket_dir(trial=False) / "raigolmid.sock", timeout=5)
        assert client.call("status") == "status"
        with pytest.raises(ApiError, match="unknown method 'repair'"):
            client.call("repair")
        # The read-only view includes the event stream.
        subscribed = threading.Event()
        stream = client.subscribe(subscribed)
        threading.Thread(target=lambda: next(stream, None), daemon=True).start()
        assert subscribed.wait(5)
    finally:
        sockets.close_all()
    assert paths.face_socket_dir(trial=True).stat().st_mode & 0o777 == 0o700


def test_rai_ai_in_a_face_asks_its_socket_and_only_to_show(rig, monkeypatch, capsys):
    """In a face `SWAYSOCK` is the face's own sway, so the key's path would drive the wrong
    compositor; `rai ai --show` asks the face's socket instead."""
    from rai.__main__ import _move_ai_terminal

    runtime, paths, events = rig
    monkeypatch.setattr(hostsurfaces, "show_ai_terminal_for_face", lambda: "started")
    sockets = FaceSockets(paths, events, {n: lambda: n for n in FACE}, answering())
    sockets.start()
    monkeypatch.setenv("RAIGOLMID_FACE", "1")
    monkeypatch.setenv("RAIGOLMID_SOCKET",
                       str(paths.face_socket_dir(trial=False) / "raigolmid.sock"))
    try:
        assert _move_ai_terminal("show") == 0
        assert capsys.readouterr().out.strip() == "started"
        assert _move_ai_terminal("hide") == 1
        assert "may only ask for the AI terminal to be shown" in capsys.readouterr().err
    finally:
        sockets.close_all()


def _the_users(scopes, viewing=None):
    status = {"agents": [{"tab": t, "scope": s} for t, s in scopes.items()],
              "terminal": {"viewing": viewing}}
    return {**{n: lambda: n for n in FACE}, "status": lambda: status,
            "select": lambda kind, id: (kind, id)}


def test_the_users_words_from_a_face_reach_the_tab_they_view_else_the_machine_tab(rig):
    """A face's ask: delivered as `direct` delivers, from the user's face."""
    _, _, events = rig
    scopes = {"tab-1": "machine", "tab-2": {"body": "app"}, "janitor": "janitor"}
    ask = build_face_methods(_the_users(scopes, viewing="tab-2"), events, trial=False)["ask"]
    assert ask("what failed?")["tab"] == "tab-2"
    ask = build_face_methods(_the_users(scopes, viewing="raigolmi"), events, trial=False)["ask"]
    assert ask("hello")["tab"] == "tab-1"
    asked = [e for e in events.tail(10) if e.type == "face.asked"]
    assert [e.tab for e in asked] == ["tab-2", "tab-1"]
    assert asked[0].data["deliver"] == {"content": "From the user's face:\n\nwhat failed?",
                                        "meta": {"from": "face"}}
    with pytest.raises(SessionError, match="janitor"):
        ask("x", tab="janitor")


def test_a_trial_reaches_neither_the_users_tabs_nor_their_selection(rig):
    _, _, events = rig
    methods = build_face_methods(_the_users({"tab-1": "machine"}), events, trial=True)
    for call in (lambda: methods["ask"]("hi"), lambda: methods["select"]("body", "app"),
                 lambda: methods["exec"]("app@tab-2", ["ls"])):
        with pytest.raises(SessionError, match="tried off the user's screen"):
            call()
