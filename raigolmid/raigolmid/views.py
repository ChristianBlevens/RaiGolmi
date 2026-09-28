"""Session views.

The session view is the toolbelt's container, built when a tab opens its sandbox with
the toolbelt it names. The toolbelt's programs run in it: language servers, debuggers,
terminals' shells, and agent `exec` calls. The face reaches them through pipes raigolmid
owns; nothing of the face runs here.

Attachment is done here, directly, not via Compose.

The one ordering rule that must never be broken: **a view's mounts are released before the
old body container is removed**. A view holds references into the body's
filesystem; removing the body underneath it leaves Docker unable to remove the container
("device or resource busy") and leaks its storage.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from . import git, labels, localtime, naming
from .launcher import LauncherClient
from .paths import Paths
from .runtime import ContainerInfo, ContainerRuntime, ContainerSpec, Mount

# SYS_ADMIN for mounts, setns and pivot_root, SYS_PTRACE for reading /proc/<body-pid>/root.
# Both are needed only while the entrypoint builds the root; everything the launcher
# starts is unprivileged.
SETUP_CAPS = ("SYS_ADMIN", "SYS_PTRACE")

VIEW_READY_TIMEOUT = 45.0
VIEW_POLL_INTERVAL = 0.25

# A terminal program's scrollback is raw PTY bytes: cursor moves, colour, and device
# queries whose *replies* the receiving terminal will type back at its shell. Pasting that
# into a log or an error message corrupts the reader's terminal, so a diagnostic carries
# the words and not the control stream.
# ECMA-48, and all of it rather than the CSI sequences alone: the fragments that survive a
# partial strip (`\x1b(B` charset designators, `\x1b=` keypad mode) reach the reader's
# terminal as literal `(B` noise.
_ANSI = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"       # OSC ... BEL|ST
    r"|\x1b[P^_X][^\x1b]*(?:\x1b\\)?"            # DCS/PM/APC/SOS ... ST
    r"|\x1b\[[0-?]*[ -/]*[@-~]"                   # CSI
    r"|\x1b[ -/]+[0-~]"                            # nF, e.g. ESC ( B
    r"|\x1b[0-~]"                                  # Fp/Fe/Fs, e.g. ESC =
)
# A failing program repeats its last error, so the tail is the whole story.
_OUTPUT_LINES = 40


def _readable(raw: str) -> str:
    """Scrollback with the control stream removed and repetition collapsed."""
    text = _ANSI.sub("", raw).replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(c for c in text if c in "\n\t" or " " <= c < "\x7f")
    lines: list[str] = []
    for line in text.split("\n"):
        line = line.rstrip()
        if line.strip() and line != (lines[-1] if lines else None):
            lines.append(line)
    return "\n".join(lines[-_OUTPUT_LINES:])

# The launcher and the entrypoint are mounted into every view rather than baked into an
# image, so they are not a second thing to rebuild when the toolbelt changes, and a
# raigolmid upgrade reaches every view's *next* generation without republishing anything.
# Everything raigolmid puts inside a view lives under one top-level directory the body does
# not own. `/run` would be the obvious home for the sockets and is wrong: it is rbound from
# the body, so creating the mount point there would create a directory *inside the body*,
# and would fail outright on a read-only one.
VIEW_PRIVATE = "/.raigolmid"
LAUNCHER_MOUNT = f"{VIEW_PRIVATE}/code"
SOCKET_MOUNT = f"{VIEW_PRIVATE}/sockets"
ENTRYPOINT_IN_VIEW = f"{VIEW_PRIVATE}/viewinit.py"


def _launcher_source() -> Path:
    """The `raigolmid` package directory on the host. Mounted read-only into the view, with
    `PYTHONPATH` pointing at its parent, so `python3 -m raigolmid.launcher.server` resolves
    inside the view against the same code this daemon is running."""
    return Path(__file__).resolve().parent


class ViewError(Exception):
    """A view could not be built or could not be talked to. Never swallowed: a view that
    cannot be driven is useless to the user, so it is torn down and recreated rather than
    adopted."""


@dataclass(frozen=True, slots=True)
class ViewPlan:
    """Everything a view needs that is not in the definitions."""
    instance: str
    # None for the instance with no body: the view keeps the toolbelt's own root.
    body_container_id: str | None
    anchor_ref: str
    toolbelt_image: str
    working_copy: Path
    generation: int
    tab: str | None = None


class Views:
    def __init__(self, runtime: ContainerRuntime, paths: Paths, epoch: int) -> None:
        self.runtime = runtime
        self.paths = paths
        self.epoch = epoch

    # --- inspection -------------------------------------------------------------------
    def get(self, instance: str) -> ContainerInfo | None:
        return self.runtime.inspect(naming.view(instance))

    def client(self, instance: str) -> LauncherClient:
        return LauncherClient(self.paths.launcher_socket(instance))

    def usable(self, instance: str, timeout: float = 2.0) -> bool:
        """A running container is not a usable view. The launcher answering is."""
        info = self.get(instance)
        if info is None or not info.running:
            return False
        return self.client(instance).alive(timeout)

    def generation_of(self, instance: str) -> int:
        info = self.get(instance)
        if info is None:
            return 0
        raw = info.labels.get(labels.VIEW_GENERATION, "0")
        try:
            return int(raw)
        except ValueError as exc:
            raise ViewError(f"view of {instance} has an unreadable generation label "
                            f"{raw!r}") from exc

    # --- lifecycle ---------------------------------------------------------------------
    def create(self, plan: ViewPlan) -> ContainerInfo:
        name = naming.view(plan.instance)
        if self.runtime.inspect(name) is not None:
            raise ViewError(
                f"a view container named {name} already exists; tear it down before "
                "creating another"
            )

        self.paths.view_sockets.mkdir(parents=True, exist_ok=True)
        self.paths.launcher_socket(plan.instance).unlink(missing_ok=True)

        spec_labels = {
            labels.MANAGED: "true",
            labels.ROLE: str(labels.Role.VIEW),
            labels.INSTANCE: plan.instance,
            labels.VIEW_GENERATION: str(plan.generation),
            labels.EPOCH: str(self.epoch),
        }
        if plan.body_container_id is not None:
            spec_labels[labels.BODY_CONTAINER] = plan.body_container_id
        if plan.tab:
            spec_labels[labels.TAB] = plan.tab

        source = _launcher_source()
        mounts = [
            Mount(source=str(self.paths.view_sockets), target=SOCKET_MOUNT),
            Mount(source=str(plan.working_copy), target="/work"),
            Mount(source=str(source), target=f"{LAUNCHER_MOUNT}/raigolmid", read_only=True),
            Mount(source=str(source / "launcher" / "view" / "viewinit.py"),
                  target=ENTRYPOINT_IN_VIEW, read_only=True),
        ]

        info = self.runtime.run(ContainerSpec(
            name=name,
            image=plan.toolbelt_image,
            entrypoint=("python3", ENTRYPOINT_IN_VIEW),
            labels=spec_labels,
            environment={
                "VIEW_INSTANCE": plan.instance,
                "VIEW_ROOT": "toolbelt" if plan.body_container_id is None else "body",
                "VIEW_GENERATION": str(plan.generation),
                # The socket's *filename* comes from `Paths`, which is the only thing that
                # knows it: an instance id is `body@session` and `@` is a poor filename
                # component, so `Paths` folds it. A view deriving the name from the
                # instance id instead would create `body@session.sock` while raigolmid waited
                # on the folded one — a launcher listening on a path nothing looks at.
                "VIEW_SOCK_NAME": self.paths.launcher_socket(plan.instance).name,
                "VIEW_SOCK_DIR": SOCKET_MOUNT,
                # What the entrypoint binds over /work, in order. Found here, the one
                # place that enumerates them, since a view sees only what it is mounted.
                "VIEW_GIT_BINDS": json.dumps([
                    [str(path.relative_to(plan.working_copy)), read_only]
                    for path, read_only in git.protected_paths(plan.working_copy).binds()]),
                "VIEW_CODE_DIR": LAUNCHER_MOUNT,
                "PYTHONPATH": LAUNCHER_MOUNT,
                "PYTHONDONTWRITEBYTECODE": "1",
                **localtime.environment(),
            },
            mounts=(*mounts, *localtime.mounts()),
            pid_mode=plan.anchor_ref,
            network_mode=plan.anchor_ref,
            cap_add=SETUP_CAPS,
            # Both of the runtime's sandboxes refuse the root assembly, and for different
            # reasons: AppArmor's container profile blocks mount and pivot_root, while the
            # builtin seccomp profile gates mount, setns and open_tree on CAP_SYS_ADMIN --
            # which the view has -- but never lists pivot_root at all, so the pivot alone
            # falls to that profile's default EPERM after every mount has succeeded. The
            # capabilities above are dropped by the entrypoint before anything user-facing
            # runs, which is the boundary that actually matters.
            security_opt=("apparmor=unconfined", "seccomp=unconfined"),
        ))
        return info

    def wait_until_usable(self, instance: str,
                          timeout: float = VIEW_READY_TIMEOUT) -> LauncherClient:
        """Building the tree and pivoting takes a moment. A view that never answers is a
        failure with a cause in its container log, so the log is what the error carries."""
        client = self.client(instance)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            info = self.get(instance)
            if info is None:
                raise ViewError(f"the view container for {instance} disappeared while starting")
            if not info.running:
                raise ViewError(f"the view for {instance} exited before its launcher came up."
                                f"{self._view_log(instance)}")
            if client.alive(timeout=1.0):
                return client
            time.sleep(VIEW_POLL_INTERVAL)
        raise ViewError(f"the view for {instance} did not answer within {timeout:.0f}s."
                        f"{self._view_log(instance)}")

    def launcher_state(self, instance: str) -> str:
        """Whether the launcher is still there, for an error that cannot tell on its own.

        A launcher that answers means one request's handler failed and the view is fine; a
        launcher that does not means the view died under it, and then the container's log
        is the thing that knows.
        """
        info = self.get(instance)
        if info is None:
            return (" The view container is gone — something removed it, which is a "
                    "teardown rather than a crash. Its log went with it.")
        if not info.running:
            return f" The view container has exited.{self._view_log(instance)}"
        if self.client(instance).alive(timeout=2.0):
            return (" The launcher is still answering, so the view is up and this one "
                    "request failed inside it.")
        return (" The view container is running but its launcher no longer answers."
                f"{self._view_log(instance)}")

    def _view_log(self, instance: str) -> str:
        log = _readable(self.runtime.diagnostic_log(naming.view(instance), tail=40))
        return f" Its log:\n{log}" if log else " Its log is empty."

    def teardown(self, instance: str) -> None:
        """The restart sequence's first move, and the one thing reconciliation must do before
        touching a body container.

        Removing the container is what releases its mounts of the body's filesystem. It is
        forced deliberately: this is the one place where leaving a container behind is
        worse than killing it, because everything downstream of it is already going away.
        """
        name = naming.view(instance)
        if self.runtime.inspect(name) is not None:
            self.runtime.remove(name, force=True)
        self.paths.launcher_socket(instance).unlink(missing_ok=True)

    def store_paths(self, instance: str, limit: int = 4096) -> tuple[str, ...]:
        """The closure's actual store paths, read from the view that has it mounted.

        This is what goes in `toolbelt.lock`. Reading them from the running view
        rather than from the image means the lock records what was really mounted, not
        what was expected to be.
        """
        result = self.client(instance).exec(
            ["/.toolbelt/bin/ls", "-1", "/nix/store"], cwd="/", timeout=30)
        if result.exit_code != 0:
            raise ViewError(
                f"could not list /nix/store in {instance}: "
                f"{result.stderr.strip() or f'exit {result.exit_code}'}"
            )
        names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return tuple(f"/nix/store/{n}" for n in sorted(names)[:limit])
