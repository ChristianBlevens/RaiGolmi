"""Face compositors — the desktop half of a face.

A face is a complete desktop for a kind of work, and its compositor is **never installed
into the host**: it ships as a container image and runs nested inside the host
compositor as an ordinary Wayland client, displayed fullscreen. `raigolmid` owns that
lifecycle, which is what makes switching a first-class
operation rather than a logout.

The flags a face container needs are not arbitrary: each one has a failure it prevents. This
module is the one way a face starts.

Two rules bind everything here:

- **A status code is not evidence; the effect is.** sway answers `{"success":true}` for
  a binding it never installed and for a reload with a config error outstanding, so every
  compositor operation below is confirmed by reading the tree back, never by the answer.
- **A socket file is not a listening socket**, and which namespace you ask matters more
  than that. wlroots names the nested display with `wl_display_add_socket_auto`, so the
  name is discovered rather than chosen; liveness is read from `/proc/net/unix`, never
  inferred from the file being there; and because AF_UNIX pathname sockets are registered
  per network namespace, a face's sockets are read through its own view. See
  `listening_unix_sockets`.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from . import hostimages, keyboard, labels, localtime, naming, settings
from .closures import Closures
from .definitions import Face, FaceDesktop
from .events import EventLog
from .paths import APPS_READY, Paths
from .runtime.base import ContainerRuntime, ContainerSpec, Mount, RuntimeError_
from .toolbelts import nixery_reference
from ui import hostipc
from ui.hostipc import HostIpcError

DRI = Path("/dev/dri")

# The face's own config directory is mounted here, and the image's entrypoint reads
# `<compositor>.conf` from it. Mounting the directory rather than the single file is what
# lets a face ship a theme, a bar config and includes alongside its compositor config.
CONFIG_MOUNT = "/etc/face"

# Covers pulling `grim` from Nixery on a machine's first capture.
SCREENSHOT_TIMEOUT = 180.0
FACE_PRIVATE = "/.raigolmid"
APPS_BIN = f"{FACE_PRIVATE}/apps/bin"
_MODIFIERS = ("ctrl", "shift", "alt", "logo")
_BUTTONS = {"left": "button1", "middle": "button2", "right": "button3"}
CODE_MOUNT = f"{FACE_PRIVATE}/code"
# The host's display, the one file of the host's runtime dir the user's face is given:
# its compositor is a client of it, and its apps are clients of the face's own.
HOST_WAYLAND = f"{FACE_PRIVATE}/host-wayland"
# The face's own socket (`scopes.FaceSockets`), where `rai` in the face finds it, and
# beside it `focused` (`Paths.focused_view`).
FACE_SOCKET_MOUNT = "/run/raigolmid"
FACE_HOME = "/home/face"
# Each face's apps keep their config and state in `<id>/config` and `<id>/state` here, so two
# faces' versions of one app never share settings while the rest of the user's home is shared.
FACE_SETTINGS = ".faces"
# `rai` itself is Python, so every face's closure carries it.
REQUIRED_FACE_PACKAGES = ("python3",)
# Where `facemounts.py` attaches what the face shows of the focused instance. Each needs a
# directory that exists in every face image, and each is empty with nothing focused.
SHOWN = ("/body", "/work", "/nix/store")
def _rai_packages() -> tuple[Path, Path]:
    """`rai` and the `raigolmid` package it imports: siblings on the host, so both are mounted."""
    raigolmid = Path(__file__).resolve().parent
    return raigolmid, raigolmid.parent / "rai"


_IMAGE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# The editor window is a face app running what the face's `[editor] command` names,
# with its editor config mounted here and raigolmid's nvim glue for its servers at `{glue}`.
EDITOR_CONFIG_MOUNT = "/etc/face-editor"
EDITOR_GLUE = f"{CODE_MOUNT}/raigolmid/nvim"


def _placed(argv: tuple[str, ...], **places: str) -> list[str]:
    """`argv` with each `{place}` it names filled in (`definitions._argv` checked them)."""
    out = []
    for arg in argv:
        for name, value in places.items():
            arg = arg.replace(f"{{{name}}}", value)
        out.append(arg)
    return out

# A face that has not drawn anything yet is indistinguishable from one that failed to bind a
# socket, and only the second is a fault. The wait is for the nested display to appear.
START_TIMEOUT = 20.0
POLL = 0.25

_WAYLAND_SOCKET = re.compile(r"^wayland-[0-9]+$")
_SWAY_IPC_SOCKET = re.compile(r"^sway-ipc\.[0-9]+\.[0-9]+\.sock$")


class FaceError(RuntimeError):
    """A face's desktop could not be brought up."""


@dataclass(frozen=True, slots=True)
class FaceRuntimeState:
    """What is actually on the screen, read from the runtime and the host compositor."""

    face_id: str
    container: str
    pid: int
    wayland_display: str
    fullscreen: bool
    # Where its display and its sway's IPC socket are: `Paths.face_runtime` for the
    # user's face, `Paths.face_trial_runtime` for a trial.
    runtime_dir: Path

    def to_dict(self) -> dict[str, object]:
        return {
            "face": self.face_id,
            "container": self.container,
            "pid": self.pid,
            "wayland_display": self.wayland_display,
            "fullscreen": self.fullscreen,
        }


class HostCompositor:
    """The host's Sway, addressed over its IPC socket.

    `SWAYSOCK` is required rather than discovered: a face's sway is pid 1 in its own pid
    namespace, so its `sway-ipc.<uid>.1.sock` is indistinguishable by name from the host's,
    and a glob picks by luck. The host config imports the real value into the user manager
    (`host/sway/config:56`).
    """

    def __init__(self, swaysock: str | None = None) -> None:
        self.swaysock = swaysock or os.environ.get("SWAYSOCK") or ""

    @property
    def available(self) -> bool:
        return bool(self.swaysock) and Path(self.swaysock).exists()

    def _require(self) -> str:
        if not self.available:
            raise FaceError(
                "SWAYSOCK is unset or names nothing, so the host compositor cannot be "
                "addressed. It is imported into the user manager by host/sway/config; a "
                "daemon started outside the seat does not have it, and a glob of the "
                "runtime directory is not a substitute."
            )
        return self.swaysock

    def _ipc(self, call, *args):
        """Every exchange with the compositor, over its IPC socket (`ui.hostipc`). A refusal
        carries sway's own reason."""
        try:
            return call(*args, swaysock=self._require())
        except HostIpcError as exc:
            raise FaceError(str(exc)) from exc

    def tree(self) -> dict:
        return self._ipc(hostipc.tree)

    def outputs(self) -> list[dict]:
        return self._ipc(hostipc.outputs)

    def seats(self) -> list[dict]:
        return self._ipc(hostipc.seats)

    def command(self, *words: str) -> None:
        self._ipc(hostipc.run_command, " ".join(words))

    def exec(self, command: str) -> None:
        """Run a command as the host compositor's own child, so it outlives a daemon restart
        rather than dying with the daemon's cgroup. sway answers before the command runs;
        what it did is only visible as whatever it draws."""
        self.command("exec", command)

    def node_for_pid(self, pid: int) -> dict | None:
        """The host's view of a client, which is the only place fullscreen state is true."""
        return hostipc.find(self.tree(), lambda n: n.get("pid") == pid)

    def fullscreen(self, pid: int, timeout: float = START_TIMEOUT) -> bool:
        """Ask for fullscreen, then read the tree back.

        The read-back is the check. sway reports success for operations it did not
        perform, and a face drawn in a tile looks like a face that never started to
        anyone reasoning from the return code.

        The window is waited for first: a nested compositor binds its own display before
        its window maps on the host, and `[pid=…]` matching nothing is refused.
        """
        deadline = time.monotonic() + timeout
        while self.node_for_pid(pid) is None:
            if time.monotonic() >= deadline:
                raise FaceError(
                    f"the face compositor (pid {pid}) opened its display, but no window of "
                    f"it appeared on the host within {timeout:g}s")
            time.sleep(POLL)
        self.command(f'[pid="{pid}"]', "fullscreen", "enable")
        node = self.node_for_pid(pid)
        return bool(node) and node.get("fullscreen_mode", 0) != 0


# `Flags` in /proc/net/unix, which is SO_ACCEPTCON for a socket that is listening. The
# face's own display and the host socket it merely connected to are both pathname sockets
# on the same path column, and this bit is what tells them apart.
SO_ACCEPTCON = 0x10000


def _views(tree: dict) -> set[int]:
    """Every window in a sway tree, by node id."""
    return {n["id"] for n in hostipc.nodes(tree) if n.get("pid")}


def _output_sizes(compositor: HostCompositor) -> list[tuple[int, int]]:
    """Each active output's current mode, as a sway answers it."""
    return [(o["current_mode"]["width"], o["current_mode"]["height"])
            for o in compositor.outputs() if o.get("active") and o.get("current_mode")]


def listening_unix_sockets(pid: int | None = None) -> set[str]:
    """Pathname sockets something is **listening** on, in the network namespace of `pid`.

    ⚠ The namespace is not a detail. **AF_UNIX pathname sockets are registered per network namespace**, and
    `/proc/net/unix` is a per-namespace view. A face container has its own network
    namespace, so the display it opens is absent from the host's view even though the
    socket *file* sits in a directory both mount and is connectable by path — which is
    exactly why the face can reach the host's compositor and the host can reach the face's.
    Read from the host's view, a live face looks like residue.

    So a face's sockets are read through `/proc/<host pid>/net/unix`, which is that
    container's view and is readable by the uid the face runs as — the same uid as the
    daemon. `/proc/<pid>/fd` is *not* readable across the container boundary, so
    attribution is by namespace rather than by descriptor.
    """
    path = "/proc/net/unix" if pid is None else f"/proc/{pid}/net/unix"
    live: set[str] = set()
    try:
        fh = open(path, encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError):
        # The process is gone, and a gone process listens on nothing.
        return live
    except PermissionError as exc:
        # A namespace not ours to read (`/proc` mounted `hidepid`, say) is not one with no
        # sockets: taken as empty, the sweep would unlink a live face's display.
        raise FaceError(f"cannot read which sockets pid {pid} listens on: {exc}") from exc
    with fh:
        next(fh, None)  # header
        for line in fh:
            fields = line.split()
            if len(fields) >= 8 and fields[7].startswith("/"):
                if int(fields[3], 16) & SO_ACCEPTCON:
                    live.add(fields[7])
    return live


class Faces:
    """Starts, stops and switches the nested compositor for the selected face."""

    def __init__(self, runtime: ContainerRuntime, paths: Paths, events: EventLog,
                 host: HostCompositor | None = None, epoch: int = 0,
                 closures: Closures | None = None) -> None:
        self.runtime = runtime
        self.paths = paths
        self.events = events
        self.host = host or HostCompositor()
        self.epoch = epoch
        # The machine's one, shared with the views' (`Session`): its copies are serialised.
        self.closures = closures or Closures(runtime, paths, events, epoch)

    # --- packaging ---------------------------------------------------------------------
    def image_for(self, face: Face, desktop: FaceDesktop) -> str:
        """The compositor ships as an image, built from the definitions beside the face.
        A face naming a compositor nobody has packaged fails here, naming the missing
        Containerfile, rather than on a config error deep in a container that did start."""
        try:
            return hostimages.ensure(self.runtime, hostimages.face_compositor(
                face.directory.parent, desktop.compositor))
        except hostimages.HostImageError as exc:
            raise FaceError(f"face '{face.id}' cannot start its desktop: {exc}") from exc

    # --- state -------------------------------------------------------------------------
    def current(self) -> FaceRuntimeState | None:
        """The running face, found by label rather than from a stored inventory.

        A face container that is present but not running is reported as absent: it is
        residue from a crash, and `start` removes it. Saying "a face is up" because a name
        resolves is the defect this project pays for most.
        """
        for info in self.runtime.list(labels.managed_filter(**{labels.ROLE: str(labels.Role.FACE)})):
            if info.status != "running" or info.pid is None:
                continue
            node = self.host.node_for_pid(info.pid) if self.host.available else None
            return FaceRuntimeState(
                face_id=info.labels.get(labels.FACE, ""),
                container=info.name,
                pid=info.pid,
                wayland_display=self.display_of(info.pid, self.paths.face_runtime),
                fullscreen=bool(node) and node.get("fullscreen_mode", 0) != 0,
                runtime_dir=self.paths.face_runtime,
            )
        return None

    def trial(self) -> FaceRuntimeState | None:
        """The face tried off the user's screen, found by label as `current` finds theirs."""
        info = self.runtime.inspect(naming.face_trial())
        if info is None or info.status != "running" or info.pid is None:
            return None
        runtime_dir = self.paths.face_trial_runtime
        return FaceRuntimeState(face_id=info.labels.get(labels.FACE, ""), container=info.name,
                                pid=info.pid,
                                wayland_display=self.display_of(info.pid, runtime_dir),
                                fullscreen=False, runtime_dir=runtime_dir)

    def _target(self, trial: bool) -> FaceRuntimeState:
        state = self.trial() if trial else self.current()
        if state is None or not state.wayland_display:
            raise FaceError("no face is being tried off the user's screen; `try_face` starts one"
                            if trial else "no face is running on the user's screen")
        return state

    @staticmethod
    def apps_reference(face: Face) -> str:
        """The image carrying the face's apps: its editor, its `apps`, and what `rai`
        needs."""
        packages = [*REQUIRED_FACE_PACKAGES]
        if face.editor is not None:
            packages.append(face.editor.package)
        if face.desktop is not None:
            packages.extend(face.desktop.apps)
        return nixery_reference(tuple(dict.fromkeys(packages)))

    def apps_closure(self, face: Face) -> tuple[str, Path]:
        """The face's apps as one Nix closure. Returns the image id and the host directory it
        was copied to."""
        return self.closures.of_image(self.apps_reference(face))

    # --- lifecycle ---------------------------------------------------------------------
    def start(self, face: Face) -> FaceRuntimeState:
        """The user's face: nested in the host compositor and fullscreen on their screen."""
        image = self._image(face)
        wayland_display = os.environ.get("WAYLAND_DISPLAY", "")
        if not wayland_display:
            raise FaceError(
                "WAYLAND_DISPLAY is unset, so there is no host compositor to nest in. The "
                "host config imports it into the user manager (host/sway/config:56)."
            )
        name = naming.face(face.id)
        if self.runtime.inspect(name) is not None:
            # Residue from a crash or from a stop that did not complete. Removing it here
            # rather than refusing keeps a face recoverable without an operator.
            self.runtime.remove(name, force=True)
        runtime_dir = self.paths.face_runtime
        runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.sweep_dead_sockets()
        host_display = (Path(wayland_display) if wayland_display.startswith("/")
                        else self.paths.runtime / wayland_display)
        self.paths.face_home.mkdir(parents=True, exist_ok=True)
        self.paths.transfer.mkdir(parents=True, exist_ok=True)
        display, pid = self._launch(
            face, image, name, labels.Role.FACE, runtime_dir,
            {"WAYLAND_DISPLAY": HOST_WAYLAND},
            (Mount(source=str(host_display), target=HOST_WAYLAND),
             Mount(source=str(self.paths.face_home), target=FACE_HOME),
             Mount(source=str(self.paths.transfer), target=f"{FACE_HOME}/Transfer")),
            trial=False)
        fullscreen = self.host.fullscreen(pid) if self.host.available else False
        state = FaceRuntimeState(face_id=face.id, container=name, pid=pid,
                                 wayland_display=display, fullscreen=fullscreen,
                                 runtime_dir=runtime_dir)
        try:
            self.apply_keyboard(state)
        except (keyboard.KeyboardError, FaceError) as exc:
            # The face is the user's screen; a keyboard its sway refuses, or a sway not yet
            # answering, leaves it on its own keymap and is theirs to see, as on the host.
            self.events.emit("keyboard.failed", error=str(exc))
        self.events.emit("face.started", **state.to_dict())
        return state

    def apply_keyboard(self, state: FaceRuntimeState) -> None:
        """The user's keyboard on the face's own sway, which keeps its own keymap
        (`keyboard.py`)."""
        nested = self._nested_compositor(state)
        for line in keyboard.input_commands(settings.current(self.paths, self.events)):
            try:
                nested.command(line)
            except FaceError as exc:
                raise keyboard.KeyboardError(f"face '{state.face_id}' refused '{line}' from "
                                             f"settings.toml: {exc}") from exc

    def start_trial(self, face: Face) -> FaceRuntimeState:
        """`face` tried off the user's screen, replacing any trial before it. Its own
        compositor on a headless output the size of their face's, in a runtime dir of its own,
        so it is a definition tried whole — its config, its apps, its compositor — and
        nothing of their screen is touched. Its pointer and keyboard are plugged in by
        `ready_trial`, once its apps are in its store."""
        image = self._image(face)
        self.stop_trial()
        runtime_dir = self.paths.face_trial_runtime
        runtime_dir.mkdir(mode=0o700, parents=True)
        display, pid = self._launch(face, image, naming.face_trial(),
                                    labels.Role.FACE_TRIAL, runtime_dir, {
                                        "WLR_BACKENDS": "headless",
                                        "WLR_LIBINPUT_NO_DEVICES": "1",
                                    }, (), trial=True)
        state = FaceRuntimeState(face_id=face.id, container=naming.face_trial(), pid=pid,
                                 wayland_display=display, fullscreen=False,
                                 runtime_dir=runtime_dir)
        size = self._screen_size()
        if size is not None:
            nested = self._nested_compositor(state)
            nested.command(f"output * mode --custom {size[0]}x{size[1]}")
            if _output_sizes(nested) != [size]:
                raise FaceError(f"the trial's output did not take the user's screen size "
                                f"{size[0]}x{size[1]}: {_output_sizes(nested)}")
        self.events.emit("face.trial_started", **state.to_dict())
        return state

    def ready_trial(self, face: Face) -> None:
        """Plug a pointer and a keyboard into the trial and open its editor window: both are
        its apps' (`virtualseat.py` runs on its `python3`), so they wait for its store as the
        user's editor window does. The devices are confirmed by the seat saying it has both."""
        state = self._target(trial=True)
        log = state.runtime_dir / "raigolmid-virtual-seat.log"
        self.runtime.spawn(state.container, [
            "sh", "-c", f"exec python3 -m raigolmid.virtualseat >{shlex.quote(str(log))} 2>&1"],
            environment={"WAYLAND_DISPLAY": state.wayland_display})
        nested = self._nested_compositor(state)
        deadline = time.monotonic() + START_TIMEOUT
        # wl_seat's capabilities: pointer 1, keyboard 2.
        while not any(seat.get("capabilities", 0) & 3 == 3 for seat in nested.seats()):
            if time.monotonic() > deadline:
                output = log.read_text(errors="replace")[-2000:] if log.exists() else ""
                raise FaceError(f"the trial's seat lacks a pointer or a keyboard "
                                f"{START_TIMEOUT:g}s after both were plugged in. Its "
                                f"output:\n{output.strip() or '<none>'}")
            time.sleep(POLL)
        if face.editor is not None:
            self.open_editor_window(face, trial=True)

    def stop_trial(self) -> None:
        """Remove the trial and its runtime dir, the only place its sockets are."""
        name = naming.face_trial()
        info = self.runtime.inspect(name)
        if info is not None:
            self.runtime.remove(name, force=True)
        if self.paths.face_trial_runtime.exists():
            shutil.rmtree(self.paths.face_trial_runtime)
        if info is not None:
            self.events.emit("face.trial_stopped", face=info.labels.get(labels.FACE, ""))

    def remove_ended_trial(self) -> None:
        """Remove a trial that is no longer running. `trial` already reads it as gone, but its
        container outlives it — the machine going down, or its compositor exiting — and holds
        its face's images until removed."""
        info = self.runtime.inspect(naming.face_trial())
        if info is not None and info.status != "running":
            self.stop_trial()

    def settings(self, face_id: str) -> Path:
        """Where the face's apps keep their config and state, in the user's home."""
        return self.paths.face_home / FACE_SETTINGS / face_id

    def seed_settings(self, face_id: str, source_id: str) -> None:
        """Replace `face_id`'s app settings with a copy of `source_id`'s. The face must not be
        on the user's screen, whose apps hold those files; the caller checks."""
        source = self.settings(source_id)
        if not source.is_dir():
            raise FaceError(f"face {source_id!r} has no settings to seed from: {source} "
                            "does not exist")
        target = self.settings(face_id)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target, symlinks=True)
        self.events.emit("face.settings_seeded", face=face_id, source=source_id)

    def _screen_size(self) -> tuple[int, int] | None:
        """What the user's face is drawn at, or with none running the host's output: the size
        a trial is drawn at, so its screenshots are the pixels they would see. None without a
        host compositor to ask."""
        state = self.current()
        if state is not None and state.wayland_display:
            sizes = _output_sizes(self._nested_compositor(state))
        elif self.host.available:
            sizes = _output_sizes(self.host)
        else:
            return None
        return sizes[0] if sizes else None

    def _image(self, face: Face) -> str:
        """The compositor image, before anything is stopped or started for it."""
        if face.desktop is None:
            raise FaceError(
                f"face '{face.id}' declares no [desktop], so it has no compositor to start "
                "and nothing of it runs."
            )
        return self.image_for(face, face.desktop)

    def _launch(self, face: Face, image: str, name: str, role: labels.Role,
                runtime_dir: Path, display_environment: dict[str, str],
                own_mounts: tuple[Mount, ...], trial: bool) -> tuple[str, int]:
        """Run `face`'s compositor as container `name` on `runtime_dir`, and wait for its
        display. Returns the display and the container's host pid. What the face reaches
        of the machine is its own socket (`Paths.face_socket_dir`); `own_mounts` is what
        one kind of face has and the other does not."""
        desktop = face.desktop
        closure_id, closure = self.apps_closure(face)
        # Docker creates a bind source that is missing, as root; the daemon serves it.
        self.paths.face_socket_dir(trial).mkdir(parents=True, exist_ok=True, mode=0o700)
        # Its apps are not in until its first sync marks them (`FaceMounts._mount`).
        (runtime_dir / APPS_READY).unlink(missing_ok=True)
        mounts = [Mount(source=str(runtime_dir), target=str(runtime_dir)),
                  Mount(source=str(closure / "bin"), target=APPS_BIN, read_only=True),
                  *(Mount(source=str(package), target=f"{CODE_MOUNT}/{package.name}",
                          read_only=True) for package in _rai_packages()),
                  # Read-only: connecting to a socket needs no write to its filesystem, and
                  # a face that could write here could rewrite `focused`. A sandbox's
                  # launcher is reached by naming it on this socket, never by a mount.
                  Mount(source=str(self.paths.face_socket_dir(trial)),
                        target=FACE_SOCKET_MOUNT, read_only=True),
                  *own_mounts]
        if desktop.config_dir is not None:
            mounts.append(Mount(source=str(desktop.config_dir), target=CONFIG_MOUNT,
                                read_only=True))
        if face.editor is not None and face.editor.config_dir is not None:
            mounts.append(Mount(source=str(face.editor.config_dir),
                                target=EDITOR_CONFIG_MOUNT, read_only=True))

        spec = ContainerSpec(
            name=name,
            image=image,
            labels={
                labels.MANAGED: "true",
                labels.ROLE: str(role),
                labels.FACE: face.id,
                labels.FACE_CLOSURE: closure_id,
                labels.EPOCH: str(self.epoch),
            },
            environment={
                "XDG_RUNTIME_DIR": str(runtime_dir),
                "XDG_CONFIG_HOME": f"{FACE_HOME}/{FACE_SETTINGS}/{face.id}/config",
                "XDG_STATE_HOME": f"{FACE_HOME}/{FACE_SETTINGS}/{face.id}/state",
                **display_environment,
                # The face's apps first: they are what the face is for, and the image's own
                # tools are the compositor's.
                "PATH": f"{APPS_BIN}:{_IMAGE_PATH}",
                "PYTHONPATH": CODE_MOUNT,
                "PYTHONDONTWRITEBYTECODE": "1",
                "RAIGOLMID_SOCKET": f"{FACE_SOCKET_MOUNT}/raigolmid.sock",
                "RAIGOLMID_FOCUSED": f"{FACE_SOCKET_MOUNT}/focused",
                # `rai ai --show` asks this socket rather than the face's own sway.
                "RAIGOLMID_FACE": "1",
                **localtime.environment(),
            },
            mounts=(*mounts, *localtime.mounts()),
            # The user's face is on the machine's network as `face`, where every sandbox is by
            # its name; a trial reaches none of their things.
            network=None if trial else naming.network(),
            aliases=() if trial else (naming.FACE_HOST,),
            # The host's Wayland socket belongs to the desktop user, so the client has to
            # *be* that uid; the identity comes from the host and is never minted inside the
            # container.
            user=f"{os.getuid()}:{os.getgid()}",
            # Fedora's sway carries cap_sys_nice=ep, an *effective* file capability, and the
            # exec fails with EPERM when the bounding set lacks it — an error that names the
            # exec rather than the capability and reads like a corrupt image.
            cap_add=("SYS_NICE",),
            tmpfs={path: "mode=0755" for path in SHOWN},
            # The GPU: with a render node wlroots renders
            # with GL, nested or headless; without one it renders in software, as the host
            # does on a machine with no GPU, and naming a missing node fails the run.
            devices=(str(DRI),) if DRI.is_dir() else (),
        )
        if not trial:
            self.runtime.ensure_network(naming.network(), {labels.MANAGED: "true"})
        self.runtime.run(spec)
        display = self._await_display(name, runtime_dir)
        return display, self._running_pid(name)

    def open_editor_window(self, face: Face, trial: bool = False) -> None:
        """The face shows its editor, a face app on the face's own display.
        Called once the face's `/nix/store` holds its apps (`Session._show`): before that
        the editor on its PATH is a link to nothing.

        Its output goes to a log in the runtime dir, the same path on the host. Not the
        face's own log: sway's file capability makes pid 1 non-dumpable, so its
        `/proc/1/fd/2` refuses every other process."""
        state = self._target(trial)
        editor = shlex.join(_placed(face.editor.command,
                                    socket=str(state.runtime_dir / self.paths.editor_socket.name),
                                    config=EDITOR_CONFIG_MOUNT, glue=EDITOR_GLUE))
        log = state.runtime_dir / self.paths.editor_window_log.name
        try:
            exec_id = self._open_window(state, editor, log, append=True, what="the editor")
        except (RuntimeError_, FaceError) as exc:
            # The desktop is up and usable without it; the reason is reported, not raised.
            self.events.emit("face.editor_window.failed", face=face.id, trial=trial,
                             error=str(exc))
            return
        self.events.emit("face.editor_window.opened", face=face.id, trial=trial,
                         exec_id=exec_id, log=str(log))

    def _open_window(self, state: FaceRuntimeState, command: str, log: Path, append: bool,
                     what: str) -> str:
        """`command` run in the face on its own display, and the exec's id once it has a new
        window there. A status code is not evidence (module docstring): an app that dies at
        start never makes one, and its output is the reason."""
        nested = self._nested_compositor(state)
        before = _views(nested.tree())
        exec_id = self.runtime.spawn(
            state.container,
            ["sh", "-c", f"exec {command} {'>>' if append else '>'}{shlex.quote(str(log))} 2>&1"],
            environment={"WAYLAND_DISPLAY": state.wayland_display})
        deadline = time.monotonic() + START_TIMEOUT
        while not _views(nested.tree()) - before:
            if time.monotonic() > deadline:
                output = log.read_text(errors="replace")[-2000:] if log.exists() else ""
                raise FaceError(f"{what} opened no window on the face within "
                                f"{START_TIMEOUT:g}s. Its output:\n{output.strip() or '<none>'}")
            time.sleep(POLL)
        return exec_id

    def show_file(self, face: Face, path: str, line: int) -> None:
        """`path`, a path as the face sees it, opened at `line` in the editor window
        by what the face names (`[editor] open`), run in the face. `{request}` is the pair as
        JSON in a file, for an editor whose own quoting a path could break out of."""
        if face.editor is None or face.editor.open is None:
            raise FaceError(f"face '{face.id}' names no way to show its editor a file "
                            "([editor] open)")
        state = self.current()
        if state is None:
            raise FaceError("no face is running, so there is no editor to show a file in")
        uses_socket = any("{socket}" in arg for arg in face.editor.open)
        if uses_socket and not self.paths.editor_socket.exists():
            raise FaceError("the face's editor window is not open; quitting it closes it "
                            "until the face is started again")
        request = self.paths.editor_request
        partial = request.with_name(request.name + ".partial")
        partial.write_text(json.dumps({"path": path, "line": line}), encoding="utf-8")
        partial.replace(request)
        result = self.runtime.exec(state.container, _placed(
            face.editor.open, path=path, line=str(line),
            socket=str(self.paths.editor_socket), request=str(request)))
        if result.exit_code != 0:
            raise FaceError(f"the editor did not take the file (exit {result.exit_code}): "
                            f"{result.output.strip()}")

    def show_url(self, face: Face, url: str) -> None:
        """`url`, as the face reaches it, opened in the face's browser, a command
        from its apps that the face names (`[desktop] browser`)."""
        if face.desktop is None or face.desktop.browser is None:
            raise FaceError(f"face '{face.id}' names no browser ([desktop] browser), so it "
                            "has nothing to show a page in")
        state = self.current()
        if state is None or not state.wayland_display:
            raise FaceError("no face is running, so there is no browser to show a page in")
        self._open_window(state, shlex.join([face.desktop.browser, url]),
                          self.paths.browser_log, append=False, what=face.desktop.browser)

    def _nested_compositor(self, state: FaceRuntimeState) -> HostCompositor:
        """The face's own sway. It is pid 1 in the face, so its socket's name is one every
        dead face left too; the one this face is listening on is the live one."""
        socket = str(state.runtime_dir / f"sway-ipc.{os.getuid()}.1.sock")
        if socket not in listening_unix_sockets(state.pid):
            raise FaceError(f"the face's compositor is not listening on {socket}")
        return HostCompositor(socket)

    def input(self, action: str, text: str | None = None, x: int | None = None,
              y: int | None = None, button: str = "left", trial: bool = False) -> None:
        """An agent's input on the face. Keys go through `wtype` on the
        nested display, from Nixery like `grim`; the pointer through the face sway's own seat,
        whose pointer is the host's, so a press is delivered and not only a move. `x`, `y` are
        the face output's pixels, the screenshot's. `key` takes `ctrl+shift+t`: modifiers
        (`ctrl`, `shift`, `alt`, `logo`) and one xkb key name. `trial` is the face tried off
        the user's screen, whose devices are `virtualseat.py`'s."""
        state = self._target(trial)
        if action in ("type", "key"):
            if not text:
                raise FaceError(f"`{action}` needs text")
            if action == "type":
                args = ("wtype", "--", text)
            else:
                *mods, key = text.split("+")
                unknown = [m for m in mods if m not in _MODIFIERS]
                if unknown or not key:
                    raise FaceError(f"`{text}` is not a key: modifiers are "
                                    f"{', '.join(_MODIFIERS)}, then one key name")
                args = ("wtype", *(a for m in mods for a in ("-M", m)), "-k", key,
                        *(a for m in reversed(mods) for a in ("-m", m)))
            self._wtype(state, args)
        elif action in ("move", "click"):
            if x is None or y is None:
                raise FaceError(f"`{action}` needs x and y")
            seat = self._nested_compositor(state)
            seat.command("seat", "-", "cursor", "set", str(x), str(y))
            if action == "click":
                if button not in _BUTTONS:
                    raise FaceError(f"`{button}` is not a button: {', '.join(_BUTTONS)}")
                seat.command("seat", "-", "cursor", "press", _BUTTONS[button])
                seat.command("seat", "-", "cursor", "release", _BUTTONS[button])
        else:
            raise FaceError(f"`{action}` is not an input: type, key, move or click")

    def _wtype(self, state: FaceRuntimeState, args: tuple[str, ...]) -> None:
        helper = naming.face_input()
        if self.runtime.inspect(helper) is not None:
            self.runtime.remove(helper, force=True)
        runtime_dir = str(state.runtime_dir)
        result = self.runtime.run_to_completion(ContainerSpec(
            name=helper,
            image=nixery_reference(("wtype",)),
            command=args,
            labels={labels.MANAGED: "true", labels.ROLE: str(labels.Role.FACE_INPUT),
                    labels.EPOCH: str(self.epoch)},
            environment={"XDG_RUNTIME_DIR": runtime_dir,
                         "WAYLAND_DISPLAY": state.wayland_display},
            mounts=(Mount(source=runtime_dir, target=runtime_dir),),
            # wtype writes its keymap to /tmp, and Docker's own tmpfs is root's 0755.
            tmpfs={"/tmp": "mode=1777"},
            user=f"{os.getuid()}:{os.getgid()}",
        ), SCREENSHOT_TIMEOUT)
        if result.exit_code != 0:
            raise FaceError(f"wtype exited {result.exit_code}: {result.output.strip()}")

    def screenshot(self, into: Path, trial: bool = False) -> Path:
        """The face's own display as a PNG in `into`, for an agent to read. The
        nested display, so it is the face alone, never the AI terminal slid over it. `grim`
        comes from Nixery like any toolbelt package, so it does not depend on what the
        user's face image carries. `trial` is the face tried off their screen."""
        state = self._target(trial)
        into.mkdir(parents=True, exist_ok=True)
        name = f"{'trial' if trial else 'face'}-{time.strftime('%Y%m%dT%H%M%S')}.png"
        helper = naming.screenshot()
        if self.runtime.inspect(helper) is not None:
            self.runtime.remove(helper, force=True)
        runtime_dir = str(state.runtime_dir)
        result = self.runtime.run_to_completion(ContainerSpec(
            name=helper,
            image=nixery_reference(("grim",)),
            command=("grim", f"/out/{name}"),
            labels={labels.MANAGED: "true", labels.ROLE: str(labels.Role.SCREENSHOT),
                    labels.EPOCH: str(self.epoch)},
            environment={"XDG_RUNTIME_DIR": runtime_dir,
                         "WAYLAND_DISPLAY": state.wayland_display},
            mounts=(Mount(source=runtime_dir, target=runtime_dir),
                    Mount(source=str(into), target="/out")),
            user=f"{os.getuid()}:{os.getgid()}",
        ), SCREENSHOT_TIMEOUT)
        shot = into / name
        if result.exit_code != 0 or not shot.is_file():
            raise FaceError(f"grim exited {result.exit_code} without a capture: "
                            f"{result.output.strip()}")
        return shot

    def stop(self, face_id: str | None = None) -> None:
        """Remove the running face and the sockets it leaves in its runtime dir."""
        state = self.current()
        if state is None:
            return
        if face_id is not None and state.face_id != face_id:
            return
        self.runtime.remove(state.container, force=True)
        if state.wayland_display:
            self._unlink_socket(state.wayland_display)
        self.paths.editor_socket.unlink(missing_ok=True)
        self.sweep_dead_sockets()
        self.events.emit("face.stopped", face=state.face_id, container=state.container)

    def switch(self, face: Face) -> FaceRuntimeState:
        """Stop the current nested compositor and start the new one, no logout.

        Sequential rather than overlapped, because two faces nested at once are two clients
        competing for the screen and the second one's fullscreen would be decided by
        whichever the host mapped last. The new face's image and apps are built first, so
        the old face stays on the user's screen through a build rather than leaving it blank; a
        build that fails still stops it, for the bare host state.
        """
        try:
            self._image(face)
            self.apps_closure(face)
        except BaseException:
            self.stop()
            raise
        self.stop()
        return self.start(face)

    # --- the nested display ------------------------------------------------------------
    def display_of(self, pid: int, runtime_dir: Path | None = None) -> str:
        """The nested display this face is listening on, read from its own namespace.

        wlroots picks the name with `wl_display_add_socket_auto`, so it is not ours to
        choose and must not be guessed — and it must not be taken by diffing the runtime
        directory either. A diff attributes to the face whatever appeared while it was
        starting, which during a switch is whichever socket won the race, and it cannot
        answer at all after a daemon restart, when there is no "before" to subtract.

        A face's namespace contains exactly one listening `wayland-N`, because it is one
        compositor. More than one means this is not the namespace of a single face, and
        that is raised rather than resolved by picking: a display named by a coin toss is
        the wrong answer reported as the right one.
        """
        found = sorted(
            name for path in listening_unix_sockets(pid)
            if (name := Path(path).name)
            and _WAYLAND_SOCKET.match(name)
            and Path(path).parent == (runtime_dir or self.paths.face_runtime)
        )
        if len(found) > 1:
            raise FaceError(
                f"pid {pid} is listening on more than one Wayland display "
                f"({', '.join(found)}), so none of them can be called the face's"
            )
        return found[0] if found else ""

    def _await_display(self, container: str, runtime_dir: Path) -> str:
        """Wait for the nested compositor to open its display.

        A face that exits during the wait is reported with its own log, because the cause is
        in it and nowhere else. A face still running with no display after the timeout is a
        different failure and says so — it started and did not bind, which is the case
        `.State.Running` alone would have called healthy.
        """
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            info = self.runtime.inspect(container)
            if info is None or info.status != "running":
                log = self.runtime.diagnostic_log(container, tail=40) if info is not None else ""
                raise FaceError(
                    f"the face compositor exited before opening a display "
                    f"(exit {info.exit_code if info else 'unknown'}):\n{log}"
                )
            if info.pid is not None:
                display = self.display_of(info.pid, runtime_dir)
                if display:
                    return display
            time.sleep(POLL)
        raise FaceError(
            f"the face compositor is running but opened no Wayland display within "
            f"{START_TIMEOUT:g}s. Its log:\n{self.runtime.diagnostic_log(container, tail=40)}"
        )

    def _running_pid(self, container: str) -> int:
        info = self.runtime.inspect(container)
        if info is None or info.status != "running" or info.pid is None:
            raise FaceError(f"the face container {container} is no longer running")
        # The host-namespace pid, which is what the host compositor reports for the client.
        return info.pid

    def _unlink_socket(self, name: str) -> None:
        for path in (self.paths.face_runtime / name, self.paths.face_runtime / f"{name}.lock"):
            path.unlink(missing_ok=True)

    def sweep_dead_sockets(self) -> list[str]:
        """Remove sockets nothing is listening on, by liveness and never by name.

        wlroots takes the lowest free name, so a dead `wayland-N` left by an earlier face is
        taken again, and a face's sway is pid 1 in its own namespace, so every face leaves
        the same `sway-ipc.<uid>.1.sock` in the face's runtime dir.

        ⚠ Liveness is the union of the host's namespace **and every running face's own**,
        for the reason `listening_unix_sockets` gives: a live face's display is invisible
        from the host's view, so a sweep that consulted only the host would unlink the
        socket of a face that is on the screen.
        """
        live = listening_unix_sockets()
        for info in self.runtime.list(
                labels.managed_filter(**{labels.ROLE: str(labels.Role.FACE)})):
            if info.status == "running" and info.pid is not None:
                live |= listening_unix_sockets(info.pid)

        swept: list[str] = []
        if not self.paths.face_runtime.is_dir():
            return []
        for path in self.paths.face_runtime.iterdir():
            if not (_WAYLAND_SOCKET.match(path.name) or _SWAY_IPC_SOCKET.match(path.name)):
                continue
            if str(path) in live:
                continue
            path.unlink(missing_ok=True)
            Path(f"{path}.lock").unlink(missing_ok=True)
            swept.append(path.name)
        return swept
