"""The face compositor's lifecycle.

Two of these assert the rules that are the reason this file
exists rather than a smoke test: a socket file is not a listening socket, and `swaymsg`
answering `{"success":true}` is not evidence that anything happened.
"""
from __future__ import annotations

import dataclasses
import os
import socket
from collections.abc import Callable
from pathlib import Path

import pytest

from raigolmid import faces as face_module
from raigolmid import labels, naming, settings
from raigolmid.definitions import Face, FaceDesktop, FaceEditor
from raigolmid.events import EventLog
from raigolmid.faces import (HOST_WAYLAND, FaceError, Faces, HostCompositor,
                            listening_unix_sockets)
from raigolmid.paths import Paths
from raigolmid.runtime.base import ContainerSpec, Mount
from tests.fakeruntime import FakeRuntime

from tests.facedisplay import NestedSway, UNSHARE_NET, back_faces, holder_process
from tests.fakes import ClosureCopies
from ui import hostipc


class ScriptedHost(HostCompositor):
    """The host compositor with only its transport replaced.

    The tree walk, the read-back and the fullscreen verdict are the real ones — a double
    that answered `fullscreen()` directly would be testing itself. `_ipc` is scripted
    because the alternative is a running Sway, and what is under test is what this module
    concludes from what Sway says.
    """

    def __init__(self, tree: dict | Callable[[], dict], *, fullscreen_takes: bool = True,
                 maps_after: int = 0) -> None:
        super().__init__(swaysock="/nonexistent/scripted.sock")
        self.tree_doc = tree if callable(tree) else (lambda: tree)
        self.fullscreen_takes = fullscreen_takes
        # How many tree reads go by before the clients' windows appear: a nested
        # compositor's window maps on the host after its display is already bound.
        self.maps_after = maps_after
        self.calls: list[str] = []

    @property
    def available(self) -> bool:
        return True

    def _ipc(self, call, *args):
        if call is hostipc.tree:
            if self.maps_after > 0:
                self.maps_after -= 1
                return {"id": 1, "type": "root", "pid": None, "nodes": []}
            return self.tree_doc()
        if call is not hostipc.run_command:
            raise AssertionError(f"the scripted host answers no {call.__name__}")
        (line,) = args
        self.calls.append(line)
        criteria = line.split(" ", 1)[0]
        matched = [n for n in hostipc.nodes(self.tree_doc()) if f'[pid="{n.get("pid")}"]' == criteria]
        if criteria.startswith("[") and (not matched or self.maps_after > 0):
            # What sway does with criteria that match nothing, through the real translation.
            return super()._ipc(_refused, line)
        # Sway answers success for a fullscreen it did not perform, which is the whole
        # point of the read-back. So does this.
        if self.fullscreen_takes:
            for node in matched:
                node["fullscreen_mode"] = 1
        return None


def _refused(line: str, swaysock: str) -> None:
    raise hostipc.HostIpcError(f"the host compositor refused '{line}': No matching node.")


# A face's nvim with no editor config of its own.
NVIM = ("foot", "--app-id=raigolmi-editor", "nvim", "--listen", "{socket}",
        "--cmd", "set rtp^={glue}", "-u", "NONE")
NVIM_OPEN = ("nvim", "--server", "{socket}", "--remote-send",
             '<C-\\><C-N><Cmd>lua require("raigolmi").show("{request}")<CR>')


def windows_of_faces(runtime: FakeRuntime) -> Callable[[], dict]:
    """The host's tree as sway keeps it: a window for each face compositor running, by its
    host pid, keeping its state across reads."""
    windows: dict[int, dict] = {}

    def tree() -> dict:
        faces = runtime.list(labels.managed_filter(**{labels.ROLE: str(labels.Role.FACE)}))
        return {"id": 1, "type": "root", "pid": None, "nodes": [
            {"id": 2, "type": "output", "pid": None, "nodes": [
                windows.setdefault(info.pid, {"id": info.pid, "type": "con", "pid": info.pid,
                                              "app_id": None, "fullscreen_mode": 0,
                                              "nodes": []})
                for info in faces if info.status == "running" and info.pid is not None]},
        ]}
    return tree


def tree_with(pid: int, fullscreen: int = 0) -> dict:
    return {"id": 1, "type": "root", "pid": None, "nodes": [
        {"id": 2, "type": "output", "pid": None, "nodes": [
            {"id": 3, "type": "con", "pid": pid, "app_id": None,
             "fullscreen_mode": fullscreen, "nodes": []},
        ]},
    ]}




@pytest.fixture
def rig(tmp_path, monkeypatch):
    runtime_dir = tmp_path / "run"
    runtime_dir.mkdir()
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    paths = Paths(state=tmp_path / "state", data=tmp_path / "data",
                  config=tmp_path / "config", runtime=runtime_dir)
    paths.state.mkdir(parents=True, exist_ok=True)
    # What the daemon's start writes before anything reads a setting.
    settings.install(paths.settings)
    paths.face_runtime.mkdir()
    (runtime_dir / "wayland-0").touch()
    runtime = FakeRuntime()
    runtime.one_shot[naming.closure_copy()] = ClosureCopies(runtime)
    back_faces(runtime)
    monkeypatch.setattr(Faces, "_nested_compositor", lambda _self, _s: NestedSway(runtime))
    return runtime, paths, EventLog(paths.events)


def a_face(tmp_path: Path, *, desktop: bool = True, compositor: str = "sway",
           packaged: bool = True, editor: bool = False) -> Face:
    config_dir = tmp_path / "facedir" / "desktop"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / f"{compositor}.conf").write_text("output * bg #202430 solid_color\n")
    if packaged:
        # The compositor's image definition sits beside the faces, not inside one.
        package = tmp_path / "_compositors" / compositor
        package.mkdir(parents=True, exist_ok=True)
        (package / "Containerfile").write_text(
            f"FROM fedora:42\nCOPY {compositor}.conf /etc/face/{compositor}.conf\n")
        (package / f"{compositor}.conf").write_text("# the compositor's shipped config\n")
    return Face(
        id="writing", name="Writing", requires_toolbelt_capabilities=(),
        directory=tmp_path / "facedir",
        editor=FaceEditor(package="neovim", config_dir=None, command=NVIM, open=NVIM_OPEN)
        if editor else None,
        desktop=FaceDesktop(compositor=compositor, config_dir=config_dir, apps=())
        if desktop else None,
    )


# --- the socket rules ------------------------------------------------------------------

@pytest.mark.skipif(UNSHARE_NET is None,
                    reason="needs CAP_SYS_ADMIN to put a holder in its own netns, which is "
                           "the only thing that makes this question meaningful")
def test_the_display_is_read_from_the_faces_own_network_namespace(rig, tmp_path):
    """The discriminating case, and the one that cost a run on the VM to find.

    AF_UNIX pathname sockets are registered **per network namespace**. A face container has
    its own, so its display is absent from the host's `/proc/net/unix` even though the
    socket file is right there in the shared runtime directory. Code that read the host's
    view found nothing and called a running face dead; this asserts the namespace is what
    is asked.
    """
    runtime, paths, events = rig
    faces = Faces(runtime, paths, events)

    # The host's display, which the face connects to as an ordinary client. Its endpoint in
    # the face's namespace carries this path too and is NOT listening — so a reader that
    # ignored SO_ACCEPTCON would see two wayland names and be unable to name either.
    host_display = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    host_display.bind(str(paths.runtime / "wayland-1"))
    host_display.listen(1)

    face = holder_process(paths.face_runtime / "wayland-2", own_netns=True,
                           connect_to=paths.runtime / "wayland-1")
    try:
        assert faces.display_of(face.pid) == "wayland-2"
        # The proof that the namespace is doing the work: from this process's own view the
        # face's socket is not listening at all.
        assert str(paths.face_runtime / "wayland-2") not in listening_unix_sockets()
    finally:
        host_display.close()
        face.terminate()
        face.wait(timeout=5)


def test_the_sweep_takes_dead_sockets_and_never_a_live_one(rig):
    runtime, paths, events = rig
    faces = Faces(runtime, paths, events)

    (paths.face_runtime / "wayland-3").write_text("")
    (paths.face_runtime / "sway-ipc.1000.1.sock").write_text("")
    (paths.face_runtime / "raigolmid.sock").write_text("")   # not ours to sweep

    live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    live.bind(str(paths.face_runtime / "wayland-7"))
    live.listen(1)
    try:
        swept = faces.sweep_dead_sockets()
    finally:
        live.close()

    assert set(swept) == {"wayland-3", "sway-ipc.1000.1.sock"}
    assert (paths.face_runtime / "wayland-7").exists()
    assert (paths.face_runtime / "raigolmid.sock").exists()


# --- what a face refuses ----------------------------------------------------------------

def test_the_compositor_image_is_built_from_the_definitions_beside_the_face(rig, tmp_path):
    """Nothing ships a built image, so a face's first start builds it — once, and
    again only when its definition changes."""
    runtime, paths, events = rig
    face = a_face(tmp_path)
    faces = Faces(runtime, paths, events, host=ScriptedHost(tree_with(0)))

    first = faces.image_for(face, face.desktop)
    assert first.startswith("raigolmi/face-sway:") and runtime.image(first) is not None
    assert faces.image_for(face, face.desktop) == first and runtime.build_count == 1, \
        "a current image was rebuilt"

    (tmp_path / "_compositors" / "sway" / "sway.conf").write_text("# edited\n")
    assert faces.image_for(face, face.desktop) != first, "an edited compositor kept its image"


# --- the container the face runs as -------------------------------------------------------

def test_the_face_runs_as_the_hosts_own_uid(rig, tmp_path, monkeypatch):
    """The identity comes from the host and is never minted inside the image. The
    image's own `USER 1000` happening to match is a coincidence, not the mechanism."""
    runtime, paths, events = rig
    captured = {}
    original = runtime.run

    def spy(spec):
        captured["spec"] = spec
        return original(spec)

    monkeypatch.setattr(runtime, "run", spy)
    faces = Faces(runtime, paths, events, host=ScriptedHost(windows_of_faces(runtime)))
    faces.start(a_face(tmp_path))

    spec = captured["spec"]
    assert spec.user == f"{os.getuid()}:{os.getgid()}"
    assert "SYS_NICE" in spec.cap_add
    assert spec.labels[labels.ROLE] == str(labels.Role.FACE)
    assert spec.labels[labels.FACE] == "writing"
    assert spec.environment["WAYLAND_DISPLAY"] == HOST_WAYLAND
    assert Mount(source=str(paths.runtime / "wayland-0"), target=HOST_WAYLAND) in spec.mounts
    assert any(m.target == "/etc/face" and m.read_only for m in spec.mounts)


@pytest.mark.parametrize("gpu", [True, False])
def test_the_face_is_given_the_hosts_gpu_when_there_is_one(rig, tmp_path, monkeypatch, gpu):
    """With the host's render nodes wlroots renders with GL; without, `docker run` would
    fail on the missing node, so none is named."""
    runtime, paths, events = rig
    captured = {}
    original = runtime.run

    def spy(spec):
        captured["spec"] = spec
        return original(spec)

    dri = tmp_path / "dri"
    if gpu:
        dri.mkdir()
    monkeypatch.setattr(face_module, "DRI", dri)
    monkeypatch.setattr(runtime, "run", spy)
    faces = Faces(runtime, paths, events, host=ScriptedHost(windows_of_faces(runtime)))
    faces.start(a_face(tmp_path))

    assert captured["spec"].devices == ((str(dri),) if gpu else ())
    assert "WLR_RENDERER" not in captured["spec"].environment


def test_the_face_carries_its_apps_closure_and_what_rai_needs(rig, tmp_path):
    """The face's apps are a Nix closure — its editor, its `apps`, and python3 for `rai
    lsp` — copied out of their image and on the face's PATH; the store paths themselves are
    `facemounts.py`'s, attached into the tmpfs made here."""
    runtime, paths, events = rig
    faces = Faces(runtime, paths, events, host=ScriptedHost(windows_of_faces(runtime)))
    face = a_face(tmp_path, editor=True)
    face = dataclasses.replace(face, desktop=dataclasses.replace(face.desktop, apps=("ripgrep",)))
    faces.start(face)

    spec = runtime.spec_of(naming.face("writing"))
    image = runtime.image("nixery.dev/shell/python3/neovim/ripgrep")
    assert spec.labels[labels.FACE_CLOSURE] == image.id
    mounts = {m.target: m for m in spec.mounts}
    apps_bin = mounts[face_module.APPS_BIN]
    assert apps_bin.read_only
    assert Path(apps_bin.source) == paths.closures / image.id.removeprefix("sha256:") / "bin"
    assert (Path(apps_bin.source) / "neovim").is_symlink()
    assert spec.environment["PATH"].startswith(face_module.APPS_BIN + ":")
    for package in ("raigolmid", "rai"):
        code = mounts[f"{face_module.CODE_MOUNT}/{package}"]
        assert code.read_only and (Path(code.source) / "__init__.py").is_file()
    # A sandbox's launcher is reached by naming it on the face's socket: no launcher socket
    # is mounted, and `focused` is read-only beside the face's socket.
    assert not any(m.source == str(paths.view_sockets)
                   for m in spec.mounts)
    assert "RAIGOLMID_VIEW_SOCKET_DIR" not in spec.environment
    own = mounts[face_module.FACE_SOCKET_MOUNT]
    assert own.read_only and own.source == str(paths.face_socket_dir(False))
    assert spec.environment["RAIGOLMID_FOCUSED"] == f"{face_module.FACE_SOCKET_MOUNT}/focused"
    assert paths.focused_view.parent == paths.face_socket_dir(False)
    assert set(spec.tmpfs) == {"/body", "/work", "/nix/store"}


# --- the rule that cost this project the most ---------------------------------------------

def test_fullscreen_is_read_back_from_the_tree_and_not_taken_from_the_return_code(rig):
    """Sway answers `{"success":true}` for a binding it never installed and for a
    reload with a config error outstanding. A face drawn in a tile and a face that never
    started are indistinguishable to anything reasoning from the exit status."""
    runtime, paths, events = rig
    refusing = ScriptedHost(tree_with(4242), fullscreen_takes=False)
    assert refusing.fullscreen(4242) is False
    assert '[pid="4242"] fullscreen enable' in refusing.calls

    taking = ScriptedHost(tree_with(4242))
    assert taking.fullscreen(4242) is True


def test_fullscreen_waits_for_the_window_to_map(rig):
    """The nested compositor binds its display before its window is on the host; asked in
    that gap, `[pid=…]` matches nothing and sway refuses the fullscreen outright."""
    host = ScriptedHost(tree_with(4242), maps_after=3)
    assert host.fullscreen(4242) is True


@pytest.mark.parametrize("break_it, why", [
    (lambda rt, name: rt.kill(name), "a face that died on its own"),
    (lambda rt, name: rt.pause(name), "a face that is paused, and so still has a pid"),
])
def test_a_face_container_that_is_not_running_is_reported_as_no_face(
        rig, tmp_path, break_it, why):
    """A name that resolves is not a face that is up.

    The paused case is the one that makes this a real check: every other way of stopping a
    container drops its pid, so a `current()` that looked only at the pid would pass. A
    paused compositor keeps its pid and draws nothing.
    """
    runtime, paths, events = rig
    faces = Faces(runtime, paths, events, host=ScriptedHost(windows_of_faces(runtime)))
    faces.start(a_face(tmp_path))
    assert faces.current() is not None

    break_it(runtime, naming.face("writing"))
    assert faces.current() is None, why


# --- a face that will not start must not take the session with it ---------------------

def test_a_face_whose_desktop_cannot_start_still_completes_the_selection(tmp_path, monkeypatch):
    """The bare host state is a designed destination, not an error state: it is
    where the user lands when a face will not come up, and it has to be reachable in order
    to repair the face. So the selection completes and the reason is carried in `status()`.

    The compositor here is packaged nowhere, which is the most ordinary way a face fails.
    """
    from tests.harness import Harness

    h = Harness(tmp_path, monkeypatch)
    monkeypatch.setattr(type(h.session.faces.host), "available", property(lambda self: True))

    status = h.session.select("face", "writing")

    assert status["session"]["face"] == "writing", "the selection was lost with the desktop"
    assert status["face_runtime"]["desktop"] is None
    assert "_compositors/sway" in status["face_runtime"]["desktop_error"]
    assert h.events_of("face.failed"), "the reason never reached the event log"


# --- what a face definition must say to have a desktop at all -------------------------------

def _face_dir(tmp_path: Path, body: str, files: dict[str, str]) -> Path:
    d = tmp_path / "f"
    (d / "desktop").mkdir(parents=True, exist_ok=True)
    (d / "face.toml").write_text(body)
    for name, text in files.items():
        (d / "desktop" / name).write_text(text)
    return d


DESKTOP_FACE = '''id = "f"
name = "F"

[desktop]
compositor = "{compositor}"
config_dir = "desktop/"
'''


def test_a_face_shipping_another_compositors_config_is_refused(tmp_path):
    """`backend-focus` declared sway and shipped `hyprland.conf`, and nothing noticed —
    a desktop half that could never have run. Hyprland cannot be a face at all under
    software rendering, so the config was dead twice over."""
    from raigolmid.definitions import DefinitionError, load_face

    path = _face_dir(tmp_path, DESKTOP_FACE.format(compositor="sway"),
                     {"hyprland.conf": "# not sway\n"})
    with pytest.raises(DefinitionError, match="sway.conf"):
        load_face(path)


def test_the_sweep_does_not_unlink_a_live_faces_socket(rig, tmp_path):
    """The destructive half of the namespace mistake.

    A running face's display is invisible in the host's `/proc/net/unix`, so a sweep that
    consulted only the host would call it residue and unlink the socket of the face on the
    screen. Liveness is therefore the union of the host's view and every running face's own.
    """
    runtime, paths, events = rig
    faces = Faces(runtime, paths, events, host=ScriptedHost(windows_of_faces(runtime)))
    faces.start(a_face(tmp_path))

    (paths.face_runtime / "wayland-6").write_text("")        # genuine residue
    swept = faces.sweep_dead_sockets()

    assert (paths.face_runtime / "wayland-2").exists(), "the live face's display was unlinked"
    assert swept == ["wayland-6"]


def test_a_face_whose_sockets_cannot_be_read_is_not_swept_as_dead(rig, tmp_path, monkeypatch):
    """Under `hidepid` a running face's `/proc/<pid>/net/unix` refuses the daemon; that is no
    evidence its display is dead, so the sweep refuses rather than unlinking it."""
    runtime, paths, events = rig
    faces = Faces(runtime, paths, events, host=ScriptedHost(windows_of_faces(runtime)))
    faces.start(a_face(tmp_path))
    pid = runtime.inspect(naming.face("writing")).pid
    real_open = open

    def hidden(path, *args, **kwargs):
        if path == f"/proc/{pid}/net/unix":
            raise PermissionError(13, "Permission denied", path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(face_module, "open", hidden, raising=False)
    with pytest.raises(FaceError, match=f"cannot read which sockets pid {pid}"):
        faces.sweep_dead_sockets()
    assert (paths.face_runtime / "wayland-2").exists()


# --- the face shows its editor -------------------------------------------------------

def test_a_face_with_an_editor_opens_its_window_on_its_own_display(rig, tmp_path):
    """The window is a face app: started inside the face's container, a client of
    the face's display, with raigolmid's glue for its language servers on the runtimepath."""
    runtime, paths, events = rig
    host = ScriptedHost(windows_of_faces(runtime))
    faces = Faces(runtime, paths, events, host=host)
    face = a_face(tmp_path, editor=True)
    faces.start(face)
    faces.open_editor_window(face)

    (container, command, environment), = runtime.spawn_log
    assert container == naming.face("writing")
    assert environment == {"WAYLAND_DISPLAY": "wayland-2"}
    assert "foot --app-id=raigolmi-editor nvim" in command[-1]
    assert f"'set rtp^={face_module.EDITOR_GLUE}'" in command[-1]
    assert (Path(face_module.__file__).parent / "nvim" / "lua" / "raigolmi.lua").is_file()


def test_starting_a_face_opens_no_window_before_its_store_is_there(rig, tmp_path):
    """The face's `/nix/store` is an empty tmpfs until its first sync, and the editor on its
    PATH a link into it: `foot` said `nvim: failed to execute` on the VM."""
    runtime, paths, events = rig
    host = ScriptedHost(windows_of_faces(runtime))
    faces = Faces(runtime, paths, events, host=host)
    faces.start(a_face(tmp_path, editor=True))
    assert runtime.spawn_log == []



def test_an_editor_that_opens_no_window_is_a_failure_with_its_output(rig, tmp_path,
                                                                     monkeypatch):
    """A spawn's status says the process started, not that a window is on the face."""
    runtime, paths, events = rig
    monkeypatch.setattr(Faces, "_nested_compositor",
                        lambda _self, _s: NestedSway(runtime, maps=lambda c: False))
    monkeypatch.setattr(face_module, "START_TIMEOUT", 0.3)
    faces = Faces(runtime, paths, events, host=ScriptedHost(windows_of_faces(runtime)))
    face = a_face(tmp_path, editor=True)
    faces.start(face)
    paths.editor_window_log.write_text("nvim: failed to execute\n")
    faces.open_editor_window(face)
    (failed,) = [e for e in events.read() if e.type.startswith("face.editor")]
    assert failed.type == "face.editor_window.failed"
    assert "nvim: failed to execute" in failed.data["error"]


def test_the_editor_windows_output_goes_where_the_host_can_read_it(rig, tmp_path):
    """Not the face's `/proc/1/fd/2`: sway's file capability makes pid 1 non-dumpable, and
    the redirect's refusal kills the window before it starts."""
    runtime, paths, events = rig
    faces = Faces(runtime, paths, events, host=ScriptedHost(windows_of_faces(runtime)))
    face = a_face(tmp_path, editor=True)
    faces.start(face)
    faces.open_editor_window(face)
    (_, command, _), = runtime.spawn_log
    assert "/proc/1" not in command[-1]
    assert command[-1].endswith(f">>{paths.editor_window_log} 2>&1")
    assert paths.editor_window_log.parent == paths.face_runtime


def test_the_editor_listens_where_the_host_reaches_it_and_stopping_the_face_removes_it(
        rig, tmp_path):
    """The channel into the editor the user is looking at:
    in the runtime dir, which the face mounts at the same path. nvim listens over a dead
    window's socket, so only `stop` removes it, as it does the display's."""
    runtime, paths, events = rig
    faces = Faces(runtime, paths, events, host=ScriptedHost(windows_of_faces(runtime)))
    face = a_face(tmp_path, editor=True)
    faces.start(face)
    faces.open_editor_window(face)
    (_, command, _), = runtime.spawn_log
    assert f"--listen {paths.editor_socket} " in command[-1]
    paths.editor_socket.write_text("")
    faces.stop()
    assert not paths.editor_socket.exists()


def test_a_switch_builds_the_new_face_while_the_old_one_is_still_on_the_users_screen(
        rig, tmp_path, monkeypatch):
    """A first build takes minutes, and the user's screen keeps the old face through it."""
    runtime, paths, events = rig
    faces = Faces(runtime, paths, events, host=ScriptedHost(windows_of_faces(runtime)))
    face = a_face(tmp_path)
    faces.start(face)
    on_screen_while = {}
    for step in ("_image", "apps_closure"):
        original = getattr(faces, step)
        monkeypatch.setattr(faces, step, lambda f, step=step, original=original: (
            on_screen_while.setdefault(step, faces.current() is not None), original(f))[1])

    faces.switch(face)

    assert on_screen_while == {"_image": True, "apps_closure": True}
    assert faces.current() is not None


def test_a_switch_whose_build_fails_leaves_the_bare_host(rig, tmp_path, monkeypatch):
    """The state the user lands in when a face will not come up is the bare host."""
    runtime, paths, events = rig
    faces = Faces(runtime, paths, events, host=ScriptedHost(windows_of_faces(runtime)))
    face = a_face(tmp_path)
    faces.start(face)

    def refuse(f):
        raise FaceError("no Containerfile for this compositor")
    monkeypatch.setattr(faces, "_image", refuse)

    with pytest.raises(FaceError, match="no Containerfile"):
        faces.switch(face)
    assert faces.current() is None


def test_a_face_docker_is_removing_fails_as_a_face_error_not_a_refused_log(rig, tmp_path):
    """The session's `_bring_up_face` keeps the bare host reachable by catching `FaceError`;
    Docker's refusal of a removing container's logs has to arrive inside one."""
    runtime, paths, events = rig
    faces = Faces(runtime, paths, events)
    container = naming.face("writing")
    runtime.run(ContainerSpec(name=container, image="face"))
    runtime.mark_for_removal(container)

    with pytest.raises(FaceError, match="docker refused this container's logs"):
        faces._await_display(container, paths.face_runtime)
