"""The host's own surfaces: the native selector, the always-on-top control that is the AI
terminal's surface, and the history menu.

Each is a layer-shell client of the host compositor, and both ship as images because the
host `/` is a read-only ostree overlay and cannot gain GTK — the same move a face's
compositor already makes. `hostimages` builds them on this machine.

⚠ **Nothing here goes through the socket API, and that is the point.** Recovery is a
designed feature: the selector and the AI terminal are what the user has when everything else
is broken, and the daemon is the likeliest thing to be broken. A selector you can only open by
asking `raigolmid` to open it is not reachable in the state it exists for — so `rai` drives this
directly, and a running daemon is something the surfaces *display*, not something they need.

The selector **toggles**: the reserved key opens it and the same key closes it, because
a key that only opens leaves the user holding a surface with no way back that they were not
told about.

Once running, a surface is moved by asking it through its socket (`ui/surfaces.py`), never
through Docker: the surface answers with what it did, and the key's path imports nothing it
does not need. Docker is asked only to start one that is not running.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Callable

from ui import hostipc, surfaces

from . import hostimages, labels, localtime, look, settings
from .paths import Paths
from .runtime.base import ContainerRuntime, Mount, ContainerSpec, RuntimeError_

SELECTOR_CONTAINER = "raigolmid-selector"
CONTROL_CONTAINER = "raigolmid-host-control"
NOTIFY_CONTAINER = "raigolmid-notify"
CATALOG_CONTAINER = "raigolmid-catalog"
# Each resident surface's container, by the role its label carries.
CONTAINERS = {labels.Role.SELECTOR: SELECTOR_CONTAINER, labels.Role.CONTROL: CONTROL_CONTAINER,
              labels.Role.NOTIFY: NOTIFY_CONTAINER, labels.Role.CATALOG: CATALOG_CONTAINER}


class HostSurfaceError(RuntimeError):
    pass


def _wayland_spec(name: str, image: str, role: str, paths: Paths,
                  command: tuple[str, ...] = ()) -> ContainerSpec:
    """A Wayland client of the *host* compositor, with the host's identity.

    The socket belongs to the desktop user, so the client has to *be* that uid rather than
    chown anything — the same reason `faces.py` gives.
    """
    wayland_display = os.environ.get("WAYLAND_DISPLAY", "")
    if not wayland_display:
        raise HostSurfaceError(
            "WAYLAND_DISPLAY is unset, so there is no host compositor to draw on. The host "
            "config imports it into the user manager; over ssh, export it from "
            "`systemctl --user show-environment`."
        )
    runtime_dir = str(paths.runtime)
    environment = {"XDG_RUNTIME_DIR": runtime_dir, "WAYLAND_DISPLAY": wayland_display}
    swaysock = os.environ.get("SWAYSOCK", "")
    if swaysock:
        # The control moves the terminal's window and starts it, so it needs the host's IPC
        # socket — the host's own, never a glob, since a face's sway leaves an
        # identically-named dead socket in the same directory.
        environment["SWAYSOCK"] = swaysock
    return ContainerSpec(
        name=name,
        image=image,
        command=command or None,
        labels={labels.MANAGED: "true", labels.ROLE: role, labels.LOOK: look.write(paths)},
        environment={**environment, **localtime.environment()},
        mounts=(Mount(source=runtime_dir, target=runtime_dir), *localtime.mounts()),
        user=f"{os.getuid()}:{os.getgid()}",
    )


def _image(runtime: ContainerRuntime, image: hostimages.HostImage) -> str:
    try:
        return hostimages.ensure(runtime, image)
    except hostimages.HostImageError as exc:
        raise HostSurfaceError(f"this surface cannot be drawn: {exc}") from exc


def selector_running(runtime: ContainerRuntime) -> bool:
    info = runtime.inspect(SELECTOR_CONTAINER)
    return info is not None and info.running


def selector_current(runtime: ContainerRuntime, paths: Paths) -> bool:
    """Running, on the image its sources now make and with the look the user's settings now
    make: one left from before a code change, a new disk or a look saved while the daemon was down
    would draw the old selector for as long as it runs."""
    info = runtime.inspect(SELECTOR_CONTAINER)
    return (info is not None and info.running and info.image == hostimages.selector().tag()
            and info.labels.get(labels.LOOK) == look.current(paths))


def start_at_rest(runtime: ContainerRuntime, paths: Paths, role: labels.Role) -> None:
    """One resident surface as the machine keeps it at rest, replacing any previous one. The
    selector is collapsed: at rest the screen is the face and two tabs, and a drawer that
    opened itself would take the keyboard."""
    if role == labels.Role.SELECTOR:
        start_selector(runtime, paths, shown=False)
    elif role == labels.Role.CONTROL:
        start_control(runtime, paths)
    elif role == labels.Role.NOTIFY:
        start_notify(runtime, paths)
    elif role == labels.Role.CATALOG:
        start_catalog(runtime, paths)
    else:
        raise HostSurfaceError(f"{role} is not a resident host surface")


def restore_resident(runtime: ContainerRuntime, paths: Paths,
                     failed: Callable[[str, str], None]) -> dict[str, str]:
    """Bring back each resident surface that is not running, as the machine keeps it at rest
    (the janitor's reconcile is how a surface that exited comes back). One already
    running is left as it is. A surface that will not start is handed to `failed` with its
    role and reason, and the others are still tried."""
    if not os.environ.get("WAYLAND_DISPLAY"):
        return {"unasked": "WAYLAND_DISPLAY is unset, so there is no host compositor"}
    outcome: dict[str, str] = {}
    for role, name in CONTAINERS.items():
        info = runtime.inspect(name)
        if info is not None and info.running:
            outcome[str(role)] = "running"
            continue
        try:
            start_at_rest(runtime, paths, role)
        except (HostSurfaceError, RuntimeError_) as exc:
            outcome[str(role)] = f"failed: {exc}"
            failed(str(role), str(exc))
            continue
        outcome[str(role)] = "restored"
    return outcome


def start_selector(runtime: ContainerRuntime, paths: Paths, shown: bool) -> None:
    """Start the resident selector, on screen or hidden, replacing any previous one."""
    if runtime.inspect(SELECTOR_CONTAINER) is not None:
        # Residue from a crash or an incomplete stop. Removing it rather than refusing keeps
        # the selector openable without an operator, which is the same call `faces.py` makes.
        runtime.remove(SELECTOR_CONTAINER, force=True)
    image = _image(runtime, hostimages.selector())
    command = () if shown else ("--hidden",)
    runtime.run(_wayland_spec(SELECTOR_CONTAINER, image, str(labels.Role.SELECTOR), paths,
                              command=command))


def toggle_selector(runtime: Callable[[], ContainerRuntime], paths: Paths) -> str:
    """Open the selector, or close it if it is open; returns the state the selector answers
    with.

    The selector stays resident and a toggle is a request to it: a press then shows a surface
    already drawn, where starting a container and GTK per press put a second or two between
    the key and the rows. One that is not running is started open, so the key works whether
    or not the daemon ever brought it up — and only then is `runtime` called, since importing
    Docker's SDK is most of a press.
    """
    try:
        return surfaces.ask(surfaces.SELECTOR, "toggle", paths.runtime)
    except surfaces.SurfaceAbsent:
        start_selector(runtime(), paths, shown=True)
        return "open"
    except surfaces.SurfaceError as exc:
        raise HostSurfaceError(str(exc)) from exc


def start_control(runtime: ContainerRuntime, paths: Paths) -> str:
    """Bring the always-on-top control up, replacing any previous one.

    It is the AI terminal's surface: it starts the terminal's window with the command given
    here, and moves it (`ui/host_control/control.py`).
    """
    info = runtime.inspect(CONTROL_CONTAINER)
    if info is not None:
        runtime.remove(CONTROL_CONTAINER, force=True)
    image = _image(runtime, hostimages.control())
    runtime.run(_wayland_spec(
        CONTROL_CONTAINER, image, str(labels.Role.CONTROL), paths,
        command=("--terminal-command", ai_terminal_command(paths))))
    return "started"


def start_notify(runtime: ContainerRuntime, paths: Paths) -> str:
    """Bring the notification menu up, replacing any previous one. It is resident, a handle
    at top centre at rest; unlike the other two it reads the daemon, because what it shows is
    the daemon's, so with no daemon it has nothing to say."""
    if runtime.inspect(NOTIFY_CONTAINER) is not None:
        runtime.remove(NOTIFY_CONTAINER, force=True)
    image = _image(runtime, hostimages.notify())
    runtime.run(_wayland_spec(NOTIFY_CONTAINER, image, str(labels.Role.NOTIFY), paths))
    return "started"


def start_catalog(runtime: ContainerRuntime, paths: Paths) -> str:
    """Bring the catalog window up hidden, replacing any previous one: resident, so the
    selector's Catalog button shows a window already drawn."""
    if runtime.inspect(CATALOG_CONTAINER) is not None:
        runtime.remove(CATALOG_CONTAINER, force=True)
    image = _image(runtime, hostimages.catalog())
    runtime.run(_wayland_spec(CATALOG_CONTAINER, image, str(labels.Role.CATALOG), paths,
                              command=("--hidden",)))
    return "started"


# --- the AI terminal, toggled by the reserved host key ------------------------------------

# foot's colours that are the user's palette's (`host/foot/foot.ini` keeps the rest), given on the
# terminal's command line because foot cannot read the look's file.
FOOT_COLOURS = {"background": "bg", "foreground": "text", "selection-background": "accent_bg",
                "selection-foreground": "text", "regular0": "surface", "regular1": "bad",
                "regular2": "ok", "regular3": "warn", "regular4": "accent", "regular7": "muted",
                "bright0": "dim", "bright1": "bad", "bright2": "ok", "bright3": "warn",
                "bright4": "accent", "bright7": "text"}


# Ctrl+Enter sends what Alt+Enter does (ESC CR), which Claude Code reads as a new line; left
# alone it is a bare CR, Enter. The AI terminal's alone: in an editor ESC CR is not a new line.
NEWLINE_BINDING = r"-o 'text-bindings.\x1b\x0d=Control+Return'"
# The size the window is given, to the pixel: foot otherwise rounds a floating window down to
# whole cells, and the few pixels short at the sides and the bottom show the face behind and
# take the pointer, which closes the terminal (`ui/host_control/control.py`).
WHOLE_SIZE = "-o resize-by-cells=no"


def ai_terminal_command(paths: Paths) -> str:
    palette = settings.in_force(paths).look
    colours = " ".join(f"-o colors.{key}={str(palette[name]).lstrip('#')}"
                       for key, name in FOOT_COLOURS.items())
    return (f"foot {colours} {NEWLINE_BINDING} {WHOLE_SIZE} "
            f"--app-id={surfaces.TERMINAL_APP_ID} rai ai")


def close_ai_terminal() -> bool:
    """Close the terminal's window, so the control's next show starts it with the colours
    now in `ai_terminal_command`; whether there was one to close."""
    node = hostipc.find_app(hostipc.tree(), surfaces.TERMINAL_APP_ID)
    if node is None:
        return False
    hostipc.run_command(f"[con_id={node['id']}] kill")
    return True


def move_ai_terminal(verb: str, paths: Paths) -> str:
    """Ask the terminal's surface to open, close or toggle it, and return its answer:
    `started`, `shown`, `hidden`, or `already` either.

    ⚠ **`"started"` and `"shown"` are different answers and the caller acts on the
    difference.** A window that had to be started runs its own `rai ai`, which picks the tmux
    window it opens on; one brought back from the scratchpad runs nothing new. Told only
    "shown", the caller did that work as well, and two of them racing through the same tmux
    session is what flickered between the base window and the agent's.
    """
    try:
        return surfaces.ask(surfaces.TERMINAL, verb, paths.runtime)
    except surfaces.SurfaceAbsent as exc:
        raise HostSurfaceError(
            f"{exc}. The control is the terminal's surface; the daemon restarts it, and the "
            f"floor terminal (the host config's own binding) needs neither") from exc
    except surfaces.SurfaceError as exc:
        raise HostSurfaceError(str(exc)) from exc


def show_ai_terminal_for_face() -> str:
    """A face asking for the AI terminal: the terminal's surface asked as the
    key asks it, and its answer is the face's.

    Landing on the agent's tab is left running rather than awaited: on a machine's first
    start it waits minutes for the agent image, the terminal says so on screen, and the face
    asked only for the terminal. Its stdout is that on-screen progress and is dropped; its
    stderr is the daemon's, the journal."""
    answer = move_ai_terminal("open", Paths.from_env())
    if answer == "shown":
        # A window this started runs its own `rai ai`, which lands itself (`move_ai_terminal`).
        land = subprocess.Popen([sys.executable, "-m", "rai", "ai", "ready"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                start_new_session=True)
        threading.Thread(target=land.wait, name="ai-ready", daemon=True).start()
    return answer
