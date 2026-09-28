"""What the running face shows of the focused instance (`facemounts.py`).

The helper's work is kernel work.
What is checked here is the daemon's half: which body, working copy and store paths, into
which face, when — against a helper double that refuses what the kernel refuses: no host pids,
a missing capability, a pid that is not a running container, or a source directory that was
never mounted into the helper.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from raigolmid import facemounts, hostimages, labels, naming
from raigolmid.closures import Closures
from raigolmid.events import EventLog
from raigolmid.facemounts import FaceMounts, FaceMountError
from raigolmid.paths import APPS_READY, Paths
from raigolmid.runtime.base import ContainerSpec, ExecResult
from tests.fakeruntime import FakeRuntime

from tests.fakes import ClosureCopies

FACE_APPS = "nixery.dev/shell/python3/neovim"
TOOLBELT = "nixery.dev/shell/libcap/python3/coreutils/util-linux/pyright"


class Kernel:
    """What each face holds, as `facemount.py sync` would leave it."""

    def __init__(self, runtime: FakeRuntime) -> None:
        self.runtime = runtime
        self.shown: dict[int, dict] = {}
        self.read_only: dict[int, set[str]] = {}
        self.pinned: dict[int, set[str]] = {}
        self.runs = 0

    def running_pid(self, pid: str) -> int:
        if int(pid) not in {c.pid for c in self.runtime.list() if c.running}:
            raise LookupError(f"/proc/{pid}/ns/mnt")
        return int(pid)

    def __call__(self, spec: ContainerSpec) -> ExecResult:
        self.runs += 1
        if spec.pid_mode != "host":
            return ExecResult(1, f"facemount: /proc/{spec.command[3]}/ns/mnt: No such file")
        if not {"SYS_ADMIN", "SYS_CHROOT", "SYS_PTRACE"} <= set(spec.cap_add):
            return ExecResult(1, "facemount: setns: Operation not permitted (errno 1)")
        mounts = {m.target: m for m in spec.mounts}
        script = mounts[facemounts.SCRIPT_IN_HELPER]
        assert script.read_only and Path(script.source).name == "facemount.py"
        args = spec.command[2:]
        assert args[0] == "sync"
        try:
            face = self.running_pid(args[1])
            flags = list(zip(args[2::2], args[3::2]))
            body = next((self.running_pid(v) for f, v in flags if f == "--body"), None)
            sources = {f: [] for f in ("--work", "--store", "--closure")}
            binds = [(v, f == "--protect") for f, v in flags if f in ("--protect", "--pin")]
            for flag, value in flags:
                if flag not in (*sources, "--body", "--protect", "--pin"):
                    return ExecResult(1, "usage: facemount.py sync …")
                if flag in sources:
                    if value not in mounts or not Path(mounts[value].source).exists():
                        raise LookupError(value)
                    sources[flag].append(mounts[value])
            store: set[str] = set()
            for listing in sources["--closure"]:
                assert listing.read_only and sources["--store"][0].read_only
                for name in Path(listing.source).read_text().split():
                    # What open_tree meets: a listed path the store does not have.
                    if not (Path(sources["--store"][0].source) / name).exists():
                        raise LookupError(f"open_tree of /sources/store/{name}")
                    store.add(name)
            work = sources["--work"]
            if binds and not work:
                return ExecResult(1, "usage: facemount.py sync …")
            for relative, _ in binds:
                # What open_tree meets: a path to protect that /work does not have.
                if not (Path(work[0].source) / relative).exists():
                    raise LookupError(f"open_tree of {facemounts.WORK_IN_HELPER}/{relative}")
        except LookupError as missing:
            return ExecResult(1, f"facemount: {missing}: No such file or directory")
        self.shown[face] = {"body": body, "work": work[0].source if work else None,
                            "store": store}
        # A bind over a directory hides the binds already under it.
        read_only: set[str] = set()
        pinned: set[str] = set()
        for relative, is_read_only in binds:
            read_only = {r for r in read_only if not r.startswith(f"{relative}/")}
            (read_only if is_read_only else pinned).add(relative)
        self.read_only[face] = read_only
        self.pinned[face] = pinned
        return ExecResult(0, "facemount: ok")


@pytest.fixture()
def world(tmp_path, monkeypatch):
    sources = tmp_path / "sources"
    (sources / "host" / "face-mount").mkdir(parents=True)
    (sources / "host" / "face-mount" / "Containerfile").write_text("FROM scratch\n")
    monkeypatch.setenv(hostimages.SOURCE_ENV, str(sources))
    paths = Paths(state=tmp_path / "state", data=tmp_path / "data",
                  config=tmp_path / "config", runtime=tmp_path / "run")
    runtime = FakeRuntime()
    kernel = Kernel(runtime)
    copies = ClosureCopies(runtime)
    runtime.one_shot[naming.face_mount()] = kernel
    runtime.one_shot[naming.closure_copy()] = copies
    events = EventLog(tmp_path / "events.jsonl")
    closures = Closures(runtime, paths, events, epoch=1)
    working_copies: dict[str, str] = {}
    paths.ensure()
    for runtime_dir in paths.face_runtimes().values():
        runtime_dir.mkdir(parents=True)             # as `Faces.start` makes it
    face_mounts = FaceMounts(runtime, events, 1, closures, working_copy=working_copies.get,
                             focused_view=paths.focused_view,
                             face_runtimes=paths.face_runtimes())
    return runtime, kernel, face_mounts, closures, copies, working_copies, tmp_path


def start(runtime: FakeRuntime, name: str, role: labels.Role, image: str = "busybox",
          **extra: str) -> int:
    return runtime.run(ContainerSpec(name=name, image=image, labels={
        labels.MANAGED: "true", labels.ROLE: str(role), **extra})).pid


def start_face(runtime: FakeRuntime, closures: Closures) -> int:
    image_id, _ = closures.of_image(FACE_APPS)
    return start(runtime, naming.face("minimal"), labels.Role.FACE,
                 **{labels.FACE_CLOSURE: image_id})


def instance(world, iid: str, toolbelt: str | None = TOOLBELT) -> int:
    runtime, *_, working_copies, tmp_path = world
    work = tmp_path / "work" / iid
    work.mkdir(parents=True)
    working_copies[iid] = str(work)
    if toolbelt is not None:
        start(runtime, naming.view(iid), labels.Role.VIEW, image=toolbelt)
    return start(runtime, naming.body_container(iid), labels.Role.BODY)


def store_of(copies: ClosureCopies, *packages: str) -> set[str]:
    return {copies.store_name(p) for p in ("bash", "coreutils", *packages)}


def test_the_focused_instance_is_shown_in_the_running_face(world):
    runtime, kernel, face_mounts, closures, copies, working_copies, _ = world
    face = start_face(runtime, closures)
    body = instance(world, "app@tab-2")
    face_mounts.show("app@tab-2")
    assert kernel.shown[face] == {
        "body": body, "work": working_copies["app@tab-2"],
        # One closure's copy of each path: python3 and coreutils are in both.
        "store": store_of(copies, "python3", "neovim", "libcap", "util-linux", "pyright")}


def test_a_face_is_told_its_apps_are_in_once_its_store_is(world):
    """Its startup runs before its first sync, with every app a link to nothing; the mark is
    what it waits on (agents/guide/faces.md)."""
    runtime, _, face_mounts, closures, _, _, root = world
    start_face(runtime, closures)
    mark = Paths(state=root / "state", data=root / "data", config=root / "config",
                 runtime=root / "run").face_runtime / APPS_READY
    assert not mark.exists()
    face_mounts.show(None)
    assert mark.exists()


def test_the_toolbelt_store_is_the_image_the_view_runs(world):
    runtime, kernel, face_mounts, closures, copies, _, _ = world
    start_face(runtime, closures)
    instance(world, "app@tab-2")
    face_mounts.show("app@tab-2")
    assert copies.copies == [runtime.image(FACE_APPS).id,
                             runtime.inspect(naming.view("app@tab-2")).image_id]


def test_a_toolbelt_one_package_larger_copies_one_store_path(world):
    runtime, kernel, face_mounts, closures, copies, _, _ = world
    start_face(runtime, closures)
    instance(world, "app@tab-2")
    face_mounts.show("app@tab-2")
    before = list(copies.paths_copied)
    runtime.remove(naming.view("app@tab-2"), force=True)
    start(runtime, naming.view("app@tab-2"), labels.Role.VIEW, image=TOOLBELT + "/ripgrep")
    face_mounts.refresh("app@tab-2")
    assert copies.paths_copied[len(before):] == [copies.store_name("ripgrep")]


def test_with_no_view_the_face_shows_only_its_own_store(world):
    runtime, kernel, face_mounts, closures, copies, _, _ = world
    face = start_face(runtime, closures)
    instance(world, "app@tab-2", toolbelt=None)
    face_mounts.show("app@tab-2")
    assert kernel.shown[face]["store"] == store_of(copies, "python3", "neovim")


def test_a_new_focus_replaces_what_is_shown_and_none_empties_it(world):
    runtime, kernel, face_mounts, closures, copies, working_copies, _ = world
    face = start_face(runtime, closures)
    instance(world, "a@tab-2")
    b = instance(world, "b@tab-3")
    face_mounts.show("a@tab-2")
    face_mounts.show("b@tab-3")
    assert (kernel.shown[face]["body"], kernel.shown[face]["work"]) == (
        b, working_copies["b@tab-3"])
    face_mounts.show(None)
    assert kernel.shown[face] == {"body": None, "work": None,
                                  "store": store_of(copies, "python3", "neovim")}


def test_release_withholds_only_the_body_that_is_shown(world):
    runtime, kernel, face_mounts, closures, _, working_copies, _ = world
    face = start_face(runtime, closures)
    a = instance(world, "a@tab-2")
    instance(world, "b@tab-3")
    face_mounts.show("a@tab-2")
    face_mounts.release("b@tab-3")
    assert kernel.shown[face]["body"] == a
    face_mounts.release("a@tab-2")
    assert kernel.shown[face]["body"] is None
    # The working copy and the store belong to no container, so nothing holds them back.
    assert kernel.shown[face]["work"] == working_copies["a@tab-2"]


def test_refresh_shows_the_replaced_body(world):
    runtime, kernel, face_mounts, closures, _, _, _ = world
    face = start_face(runtime, closures)
    instance(world, "app@tab-2")
    name = naming.body_container("app@tab-2")
    face_mounts.show("app@tab-2")
    face_mounts.release("app@tab-2")
    runtime.remove(name, force=True)
    new = start(runtime, name, labels.Role.BODY)
    face_mounts.refresh("app@tab-2")
    assert kernel.shown[face]["body"] == new


def test_a_released_instance_that_was_stopped_is_shown_when_selected_again(world):
    runtime, kernel, face_mounts, closures, _, _, _ = world
    face = start_face(runtime, closures)
    body = instance(world, "app@tab-2")
    face_mounts.show("app@tab-2")
    face_mounts.release("app@tab-2")
    face_mounts.show("app@tab-2")
    assert kernel.shown[face]["body"] == body


def focused(face_mounts: FaceMounts) -> str:
    return face_mounts.focused_view.read_text()


def test_the_apps_are_pointed_at_a_new_view_and_only_a_new_one(world):
    """A change of this line restarts the editor's servers, so a sync that finds
    the same view — a release, a refresh with the body replaced — must leave it alone."""
    runtime, _, face_mounts, closures, *_ = world
    start_face(runtime, closures)
    instance(world, "app@tab-2")
    face_mounts.show("app@tab-2")
    first = runtime.inspect(naming.view("app@tab-2")).id
    assert focused(face_mounts) == f"app@tab-2 {first}\n"

    stamp = face_mounts.focused_view.stat().st_ino
    face_mounts.release("app@tab-2")
    face_mounts.refresh("app@tab-2")
    assert face_mounts.focused_view.stat().st_ino == stamp

    runtime.remove(naming.view("app@tab-2"), force=True)
    start(runtime, naming.view("app@tab-2"), labels.Role.VIEW, image=TOOLBELT)
    face_mounts.refresh("app@tab-2")
    second = runtime.inspect(naming.view("app@tab-2")).id
    assert second != first and focused(face_mounts) == f"app@tab-2 {second}\n"


def test_a_focus_on_another_instance_points_the_apps_at_its_view(world):
    """The old instance's view stays up while its tab holds it, so no server's stream ends:
    this line is the only thing that tells the editor its servers are the wrong ones."""
    runtime, _, face_mounts, closures, *_ = world
    start_face(runtime, closures)
    instance(world, "a@tab-2")
    instance(world, "b@tab-3")
    face_mounts.show("a@tab-2")
    face_mounts.show("b@tab-3")
    assert focused(face_mounts).split()[0] == "b@tab-3"


def test_with_no_view_the_apps_are_told_there_is_none(world):
    runtime, _, face_mounts, closures, *_ = world
    start_face(runtime, closures)
    instance(world, "app@tab-2", toolbelt=None)
    face_mounts.show("app@tab-2")
    assert focused(face_mounts) == ""


def test_the_apps_are_not_pointed_at_a_view_the_face_could_not_be_shown(world, monkeypatch):
    runtime, _, face_mounts, closures, *_ = world
    start_face(runtime, closures)
    instance(world, "app@tab-2")
    monkeypatch.setattr(facemounts, "CAPS", ("SYS_ADMIN",))
    with pytest.raises(FaceMountError):
        face_mounts.show("app@tab-2")
    assert not face_mounts.focused_view.exists()


def test_residue_of_a_dead_run_does_not_block_the_next(world):
    runtime, kernel, face_mounts, closures, _, _, _ = world
    face = start_face(runtime, closures)
    body = instance(world, "app@tab-2")
    start(runtime, naming.face_mount(), labels.Role.FACE_MOUNT)
    face_mounts.show("app@tab-2")
    assert kernel.shown[face]["body"] == body


def test_the_real_runtime_implements_every_operation():
    """Docker is not reachable from the suite, so nothing else here constructs DockerRuntime:
    an operation added to the interface and missed there fails only at the daemon's start."""
    from raigolmid.runtime.docker_runtime import DockerRuntime
    assert DockerRuntime.__abstractmethods__ == frozenset()


def test_the_users_git_hooks_and_config_are_read_only_in_the_face(world):
    """Nothing the face runs may make the host's git execute code. A repository
    with no hooks directory or commondir gets them, or the face could make and fill one."""
    runtime, kernel, face_mounts, closures, *_, working_copies, tmp_path = world
    face = start_face(runtime, closures)
    instance(world, "body@s")
    git_dir = Path(working_copies["body@s"]) / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\n")

    face_mounts.show("body@s")

    assert (git_dir / "hooks").is_dir()
    assert (git_dir / "commondir").read_text().strip() == "."
    assert kernel.read_only[face] == {".git/hooks", ".git/config", ".git/commondir"}
    assert kernel.pinned[face] == {".git"}, "a .git that could be renamed takes its binds away"
