"""What the running face shows of the focused instance.

The face's apps work on the layers below it without running in them: the editor opens the
files a language server in the toolbelt's container names, and those paths must mean the same
bytes in the face. So whenever a face is up it holds:

- `/body` — the focused instance's body, live and read-only, attached without touching it;
- `/work` — that instance's working copy, the same path as in the toolbelt and the agent;
- `/nix/store` — the face's own apps closure and the focused toolbelt's, both read-only. The
  toolbelt's is copied from the image the view is *running* (`closures.py`), so the store
  paths its server answers with are the ones the face has.

And it names the view the face's apps reach (`Paths.focused_view`): a new view there — a
rebuild, a toolbelt change, another instance focused — is what restarts the editor's servers.

The mounting itself is `facemount.py sync`, run to completion in a helper container with host
pids and the mount capabilities: this daemon has neither, and Docker is already where
privilege is granted. The script and every source directory are mounted from the host.

The face holds a reference to the body's filesystem, so the body cannot be replaced or removed
under it: `release` is called before, `refresh` after — the view's teardown order, extended to the face. Nothing else the face holds belongs to a container.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from . import git, hostimages, labels, naming
from .closures import Closures
from .events import EventLog
from .paths import APPS_READY
from .runtime.base import ContainerInfo, ContainerRuntime, ContainerSpec, Mount

SCRIPT_IN_HELPER = "/facemount.py"
WORK_IN_HELPER = "/sources/work"
STORE_IN_HELPER = "/sources/store"
CLOSURES_IN_HELPER = "/sources/closures"
# setns into a mount namespace needs SYS_ADMIN and SYS_CHROOT; opening the namespace of a face
# that runs as the user, from a helper that runs as root, needs SYS_PTRACE.
CAPS = ("SYS_ADMIN", "SYS_CHROOT", "SYS_PTRACE")
TIMEOUT = 60.0


class FaceMountError(RuntimeError):
    """The face could not be made to show the focused instance."""


class FaceMounts:
    def __init__(self, runtime: ContainerRuntime, events: EventLog, epoch: int,
                 closures: Closures, working_copy: Callable[[str | None], str | None],
                 focused_view: Path, face_runtimes: dict[str, Path]) -> None:
        self.runtime = runtime
        self.face_runtimes = face_runtimes
        self.events = events
        self.epoch = epoch
        self.closures = closures
        self.working_copy = working_copy
        self.focused_view = focused_view
        # Why the face is not showing what it should, until a sync succeeds; `status` shows it.
        self.failure: str | None = None
        self._instance: str | None = None
        # The instance whose body is between `release` and `refresh`.
        self._released: str | None = None
        self._lock = threading.Lock()

    def show(self, instance: str | None) -> None:
        """The face shows this instance from now on, or nothing below it."""
        with self._lock:
            self._instance = instance
            # A statement of what the face shows, made with every body up: an instance released
            # and then stopped never sees its `refresh`.
            self._released = None
            self._sync()

    def stock_trial(self, face: ContainerInfo) -> None:
        """A face tried off the user's screen holds its own apps and nothing of an
        instance: no body, no toolbelt, and no `/work`, so the tab trying it cannot write
        into the user's working copy through it."""
        with self._lock:
            self._mount(face, None, None, None)

    def release(self, instance: str) -> None:
        """Before `instance`'s body is replaced or removed."""
        with self._lock:
            self._released = instance
            if instance == self._instance:
                self._sync()

    def refresh(self, instance: str) -> None:
        """After `instance`'s body or view is running again."""
        with self._lock:
            if self._released == instance:
                self._released = None
            if instance == self._instance:
                self._sync()

    def _sync(self) -> None:
        instance = self._instance
        view = self._running(naming.view(instance)) if instance else None
        face = self._face()
        if face is not None:
            try:
                self._mount(face, instance, view, self.working_copy(instance))
            except FaceMountError as exc:
                self.failure = str(exc)
                self.events.emit("face.sync_failed", instance=instance, error=str(exc))
                raise
        self.failure = None
        # After the mounts: a server started on this view answers with its store paths.
        self._point_apps_at(instance, view)

    def _point_apps_at(self, instance: str | None, view: ContainerInfo | None) -> None:
        """The face's apps restart their servers when this line changes, so it is
        rewritten only when the view is another one."""
        line = f"{instance} {view.id}\n" if view is not None else ""
        if self.focused_view.is_file() and self.focused_view.read_text() == line:
            return
        staged = self.focused_view.with_name(f".{self.focused_view.name}.new")
        staged.write_text(line)
        staged.replace(self.focused_view)
        self.events.emit("face.view_focused", instance=instance,
                         view=view.id if view is not None else None)

    def _mount(self, face: ContainerInfo, instance: str | None,
               view: ContainerInfo | None, work: str | None) -> None:
        args = ["sync", str(face.pid)]
        mounts: list[Mount] = []
        if labels.FACE_CLOSURE not in face.labels:
            raise FaceMountError(
                f"the running face {face.name} carries no apps closure label, so it was "
                f"started by an raigolmid from before its apps were a closure. "
                f"Select the face again to restart it.")
        stores = [self.closures.of_image_id(face.labels[labels.FACE_CLOSURE])]

        body = (self._running(naming.body_container(instance))
                if instance and naming.has_body(instance) else None)
        if body is not None and instance != self._released:
            args += ["--body", str(body.pid)]
        if work is not None:
            args += ["--work", WORK_IN_HELPER]
            mounts.append(Mount(source=work, target=WORK_IN_HELPER))
            for path, read_only in git.protected_paths(Path(work)).binds():
                args += ["--protect" if read_only else "--pin", str(path.relative_to(work))]
        if view is not None:
            stores.append(self.closures.of_image_id(view.image_id))

        args += ["--store", STORE_IN_HELPER]
        mounts.append(Mount(source=str(self.closures.store), target=STORE_IN_HELPER,
                            read_only=True))
        for n, closure in enumerate(stores):
            target = f"{CLOSURES_IN_HELPER}/{n}"
            args += ["--closure", target]
            mounts.append(Mount(source=str(closure / "paths"), target=target, read_only=True))
        output = self._helper(args, mounts)
        (self.face_runtimes[face.labels[labels.ROLE]] / APPS_READY).touch()
        self.events.emit("face.synced", instance=instance, face=face.name,
                         body="--body" in args, work=work is not None,
                         toolbelt=view.image_id if view is not None else None, detail=output)

    def _running(self, name: str) -> ContainerInfo | None:
        info = self.runtime.inspect(name)
        return info if info is not None and info.running and info.pid is not None else None

    def _face(self) -> ContainerInfo | None:
        """The running face, found by label, as `Faces.current` finds it."""
        for info in self.runtime.list(labels.managed_filter(**{labels.ROLE: str(labels.Role.FACE)})):
            if info.running and info.pid is not None:
                return info
        return None

    def _helper(self, args: list[str], mounts: list[Mount]) -> str:
        name = naming.face_mount()
        if self.runtime.inspect(name) is not None:
            # Residue of a daemon that died mid-run: the name is the lock.
            self.runtime.remove(name, force=True)
        spec = ContainerSpec(
            name=name,
            image=hostimages.ensure(self.runtime, hostimages.face_mount()),
            command=("python3", SCRIPT_IN_HELPER, *args),
            labels={labels.MANAGED: "true", labels.ROLE: str(labels.Role.FACE_MOUNT),
                    labels.EPOCH: str(self.epoch)},
            mounts=(Mount(source=str(Path(__file__).resolve().parent / "facemount.py"),
                          target=SCRIPT_IN_HELPER, read_only=True), *mounts),
            pid_mode="host",
            cap_add=CAPS,
            security_opt=("apparmor=unconfined", "seccomp=unconfined"),
        )
        result = self.runtime.run_to_completion(spec, TIMEOUT)
        if result.exit_code != 0:
            raise FaceMountError(f"facemount {' '.join(args)} exited {result.exit_code}: "
                                f"{result.output.strip()}")
        return result.output.strip()
