"""The daemon process: startup, the single-instance lock, watching, and shutdown.

Startup: take the lock, increment the epoch, emit
`raigolmid.started` with it, load intent, list every managed container, reconcile.

Shutdown is the part that is easy to get wrong: on SIGTERM the daemon stops accepting new
work and **leaves running containers running**. Views and bodies outlive the daemon by
design, and reconciliation adopts them on the next start. It does not tear down the user's
environment because its own process is restarting.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import signal
import threading
import time
import traceback
from pathlib import Path

from . import credential, hostsurfaces, keyboard, labels, look, naming, settings
from .api import ApiServer, build_methods
from .definitions import SearchPaths
from .events import EventLog
from .history import History
from .hostkeys import HostKeyError, HostKeys
from .maintenance import Maintenance
from .manager import Manager
from .channel import Channels
from .clipboard import ClipboardBridge
from .coordinator import Coordinator
from .credproxy import CredentialProxy
from .permissions import Permissions
from .judge import Judge
from .questions import Questions
from .viewing import Viewing
from . import hostimages
from .hostimages import HostImageError
from .hostsurfaces import HostSurfaceError, selector_current, start_at_rest
from ui.hostipc import HostIpcError
from .paths import Paths
from .runtime import ContainerRuntime
from .runtime.base import RuntimeError_
from .scopes import AgentSockets, FaceSockets
from .session import Session
from .supervisor import Unit, unit_of

logger = logging.getLogger(__name__)

# What a first start shows while the surfaces are being built (`rai boot`). A host `foot`,
# because the only toolkit on the host is a terminal — GTK lives in the images this is waiting
# for, which is the whole reason the wait exists.
BOOT_SCREEN_COMMAND = "foot --app-id=raigolmi-boot rai boot"


class AlreadyRunning(Exception):
    """A second daemon would fight the first over the same containers."""


class Lock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None

    def acquire(self) -> int:
        """Returns the new epoch. The epoch lives in the lock file so it survives a crash:
        it is what tells a later reader which raigolmid run created an object."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._fh.close()
            self._fh = None
            raise AlreadyRunning(
                f"another raigolmid holds {self.path}. Two daemons would fight over the "
                "same containers, so this one will not start."
            ) from exc
        self._fh.seek(0)
        raw = self._fh.read().strip()
        try:
            epoch = int(json.loads(raw)["epoch"]) + 1 if raw else 1
        except (json.JSONDecodeError, KeyError, ValueError):
            epoch = 1
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(json.dumps({"epoch": epoch, "pid": os.getpid()}))
        self._fh.flush()
        os.fsync(self._fh.fileno())
        return epoch

    def release(self) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None


class Daemon:
    def __init__(self, runtime: ContainerRuntime, paths: Paths,
                 search: SearchPaths) -> None:
        self.paths = paths
        self.paths.ensure()
        self.lock = Lock(paths.lock)
        self.epoch = self.lock.acquire()
        settings.install(paths.settings)
        # The product ships no layers: a machine starts with these empty, and every agent's
        # `/definitions` is the directory above them (`Session.definitions_root`).
        for directory in (*search.faces, *search.toolbelts, *search.bodies):
            directory.mkdir(parents=True, exist_ok=True)
        self.events = EventLog(paths.events, epoch=self.epoch)
        self.session = Session(runtime, paths, search, self.events, self.epoch)
        # Before anything in `start` can fail: the manager hears the daemon's own start.
        self.manager = Manager(self.session, self.events)
        self.history = History(self.events, paths)
        self.maintenance = Maintenance(self.session, self.events)
        self.questions = Questions(self.events, paths)
        self.judge = Judge(self.events, runtime, paths.preferences, self.session.agents.broker,
                           self.epoch)
        self.channels = Channels(self.session, self.events)
        self.coordinator = Coordinator(self.session, self.events, self.questions)
        # The credential stays here; every agent container is given a placeholder.
        self.credproxy = CredentialProxy(
            self.session.agents.broker, self.events,
            lambda owner: owner == naming.judge() or owner in self.session.intent.tabs,
            host=runtime.bridge_gateway())
        self.permissions = Permissions(self.session, self.events)
        self.viewing = Viewing(self.events, paths.viewing)
        methods = build_methods(self.session, self.events, self.questions, self.channels,
                                self.viewing, self.history)
        # No socket answers until the start has re-derived the machine.
        self._reconciled = threading.Event()
        self.api = ApiServer(paths.api_socket, methods, self.events, ready=self._reconciled)
        # Each agent container reaches only its own tab's socket.
        self.agent_sockets = AgentSockets(paths, self.session, self.events, self.questions,
                                          self.channels, methods, self._reconciled)
        # A face reaches the machine through its own socket alone.
        self.face_sockets = FaceSockets(paths, self.events, methods, self._reconciled)
        # raigolmid owns the reserved host keys *and* the always-on-top control, and
        # one object holds both so the button and the key cannot come to run different
        # commands.
        self.host_keys = HostKeys(paths, self.events)
        # The user's face's clipboard and the host's are one.
        self.clipboard = ClipboardBridge(self.events, paths, self.session.faces.current)
        self._stop = threading.Event()
        # The threads that run until the daemon stops, and only those: one of them ending is
        # a part of the machine that stopped (`dead_threads`).
        self._threads: list[threading.Thread] = []
        # Why each of them ended, when one ended on an exception (`_thread_died`).
        self._died: dict[str, str] = {}

    def start(self) -> None:
        self.events.emit("raigolmid.started", epoch=self.epoch, pid=os.getpid(),
                         socket=str(self.paths.api_socket))
        threading.excepthook = self._thread_died
        # Served from here, so a caller that comes early is accepted and waits, answered only
        # once `_reconciled` is set. Before reconciling, which starts the user's face on the
        # socket it mounts.
        self.face_sockets.start()
        self._run_until_stopped(self.api.serve_forever, "api")
        # The runtime event last handled, in ns (`_watch_runtime`): none yet, so every one
        # from here on is handled, those during the reconcile included.
        self._runtime_seen = time.time_ns()
        errors = self.session.rediscover().errors
        for error in errors:
            logger.warning("definition error: %s", error)
        report = self.session.reconcile()
        logger.info("reconciled: %s", report.to_dict())
        # After reconcile, whose containers are roots, and before any thread can fetch.
        self._try("garbage", "collecting unused images and closures",
                  self.session.collect_garbage)
        self.questions.forget_absent_tabs(set(self.session.intent.tabs))
        self.channels.messages.forget_absent_tabs(set(self.session.intent.tabs))
        self.questions.offer_to_judge()
        self.coordinator.announce()
        self._reconciled.set()
        self._bring_up_host_surfaces()

        for target, name in ((lambda: self.manager.run(self._stop), "manager"),
                             (lambda: self.maintenance.run(self._stop), "maintenance"),
                             (lambda: self.session.presence.run(self._stop), "presence"),
                             (lambda: self.questions.run(self._stop), "questions"),
                             (lambda: self.history.run(self._stop), "history"),
                             (lambda: self.viewing.run(self._stop), "viewing"),
                             (lambda: self.judge.run(self._stop), "judge"),
                             (lambda: self.channels.run(self._stop), "channels"),
                             (lambda: self.channels.messages.run(self._stop), "messages"),
                             (lambda: self.coordinator.run(self._stop), "coordinator"),
                             (self.credproxy.serve, "credproxy"),
                             (lambda: self.permissions.run(self._stop), "permissions"),
                             (lambda: self.agent_sockets.run(self._stop), "agent-sockets"),
                             (lambda: self.face_sockets.run(self._stop), "face-sockets"),
                             (lambda: self.clipboard.run(self._stop), "clipboard"),
                             (self._watch_runtime, "runtime-events"),
                             (self._watch_files, "file-watch"),
                             (self._watch_settings, "settings-watch")):
            self._run_until_stopped(target, name)

    def _run_until_stopped(self, target, name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    # --- the host's own two doors ---------------------------------------
    def _bring_up_host_surfaces(self) -> None:
        """Install the reserved keys, draw the control, and open the selector over an empty
        screen: the machine boots straight into the selector.

        The keys go in first and synchronously: they need no image. The rest waits on
        `hostimages`, and a first boot loads every image from the disk's archive first, which
        the API must not spend unreachable.

        Neither is allowed to stop the daemon. These are the way back to a working system,
        and a daemon that refused to start because it could not draw a button would take the
        session down over the thing that exists to repair it. Both outcomes
        are reported — nothing here is swallowed.
        """
        if not os.environ.get("WAYLAND_DISPLAY"):
            # Not a failure: an unanswerable question, reported as unasked. A host with no
            # compositor has nowhere to draw these surfaces.
            self.events.emit("hostsurfaces.skipped",
                             reason="WAYLAND_DISPLAY is unset, so there is no host "
                                    "compositor to bind keys on or draw the control over")
            return
        self._try("hostkeys", "reserved host keys", self.host_keys.apply)
        self._try("keyboard", "the user's keyboard and display", self._apply_keyboard)
        # A one-off that ends once the surfaces are drawn, so it is not one of `_threads`.
        self._drawing = threading.Thread(target=self._draw_host_surfaces,
                                         name="host-surfaces", daemon=True)
        self._drawing.start()

    def _draw_host_surfaces(self) -> None:
        """⚠ The selector first, because it is what the machine opens into.

        The control is second and serial: where its image is built rather than loaded, it
        installs the same GTK stack from the same base, spelled identically on purpose, so the
        build is a layer-cache hit — two builds at once would each run the install. `test_the_surface_images_share_one_install_line` holds the two files to
        the same line."""
        runtime = self.session.runtime
        self._try("boot-screen", "the first-start screen", self._show_boot_screen)
        self._try("selector", "selector at boot", self._start_resident_selector)
        self._try("control", "host control",
                  lambda: start_at_rest(runtime, self.paths, labels.Role.CONTROL))
        self._try("notify", "notification popup",
                  lambda: start_at_rest(runtime, self.paths, labels.Role.NOTIFY))
        self._try("catalog", "catalog window",
                  lambda: start_at_rest(runtime, self.paths, labels.Role.CATALOG))
        # Built now so the first selection of a face does not wait on its compositor.
        for face in self.session.catalogue.faces.values():
            if face.desktop is not None:
                self._try("hostimages", f"compositor image for face '{face.id}'",
                          lambda face=face: hostimages.ensure(runtime, hostimages.face_compositor(
                              face.directory.parent, face.desktop.compositor)))
        # And the agent's, before the first tab asks for it; the face-mount helper's, before
        # the first face shows a body.
        self._try("hostimages", "agent image",
                  lambda: hostimages.ensure(runtime, hostimages.agent()))
        self._try("hostimages", "face-mount image",
                  lambda: hostimages.ensure(runtime, hostimages.face_mount()))
        self._try("hostimages", "door image",
                  lambda: hostimages.ensure(runtime, hostimages.door()))
        self._try("hostimages", "gh image",
                  lambda: hostimages.ensure(runtime, hostimages.gh()))
        # AI is always ready: the machine tab, and the selected body's, before the terminal
        # is first shown, so showing it only draws their windows. The container only, never
        # the tmux window: a tmux server started here would be in this unit's cgroup and die
        # with every restart of the daemon.
        self._try("agent", "a ready agent", self.session.ensure_tabs)

    def _show_boot_screen(self) -> None:
        """Say what is happening while there is nothing yet to look at.

        Only when the selector's image is actually missing: on every later start it is already
        built, the selector is up in a moment, and a screen that flashed each boot to announce
        that nothing was wrong would be noise. The screen ends itself when the selector is
        running (`rai boot`), so nothing here has to take it down."""
        from .faces import HostCompositor

        if hostimages.present(self.session.runtime, hostimages.selector()):
            return
        HostCompositor().exec(BOOT_SCREEN_COMMAND)

    def _start_resident_selector(self) -> None:
        # Collapsed even with nothing selected: the tab is what says there is a drawer. One
        # already running on the current image is left as it is, on screen or not.
        if not selector_current(self.session.runtime, self.paths):
            start_at_rest(self.session.runtime, self.paths, labels.Role.SELECTOR)

    def _try(self, event: str, what: str, action) -> None:
        """⚠ Every exception, not just the two these raise deliberately. The policy above is
        that neither door can stop the daemon, and a policy that only holds for the failures
        that were anticipated is not one. Nothing is swallowed: the type, the message and the
        traceback all go to the event log and the journal, which is where an agent repairing
        this reads.
        """
        try:
            action()
        except (HostKeyError, HostSurfaceError, HostImageError, keyboard.KeyboardError) as exc:
            self.events.emit(f"{event}.failed", error=str(exc))
            logger.error("%s: %s", what, exc)
        except Exception as exc:                       # noqa: BLE001
            self.events.emit(f"{event}.failed", error=f"{type(exc).__name__}: {exc}",
                             traceback=traceback.format_exc())
            logger.exception("%s: unexpected failure", what)

    def _apply_keyboard(self) -> None:
        s = keyboard.apply_host(self.paths.settings)
        face = self.session.faces.current()
        if face is not None:
            self.session.faces.apply_keyboard(face)
        self.events.emit("keyboard.applied", layout=s.layout, variant=s.variant,
                         repeat_rate=s.repeat_rate, repeat_delay=s.repeat_delay, scale=s.scale)

    def _apply_look(self) -> None:
        """Redraw each running host surface drawn with a look the user's settings no longer make.
        The AI terminal's colours are the control's command, so a redrawn control closes the
        terminal's window and its next show opens one in the new colours."""
        runtime, now = self.session.runtime, look.current(self.paths)
        stale = [role for role, name in hostsurfaces.CONTAINERS.items()
                 if (info := runtime.inspect(name)) is not None and info.running
                 and info.labels.get(labels.LOOK) != now]
        for role in stale:
            start_at_rest(runtime, self.paths, role)
        if labels.Role.CONTROL in stale:
            hostsurfaces.close_ai_terminal()
        if stale:
            self.events.emit("look.applied", surfaces=[str(role) for role in stale])

    def _watch_settings(self) -> None:
        """A rebind takes effect without rebuilding the host image, so the settings
        are watched rather than read once, and a save re-applies the keys, the keyboard and
        the display. The control is not restarted with it — a rebind changes which *key*
        reaches a door, never which command the button runs; a changed look redraws them."""
        # With nothing to watch it waits to be stopped: a thread that ends is a dead part of
        # the daemon (`dead_threads`), and this one is simply not needed here.
        try:
            from watchfiles import watch
        except ImportError:
            self._stop.wait()
            return
        if not os.environ.get("WAYLAND_DISPLAY"):
            self._stop.wait()
            return
        directory = self.paths.settings.parent
        directory.mkdir(parents=True, exist_ok=True)
        while not self._stop.is_set():
            try:
                for changes in watch(str(directory), stop_event=self._stop, debounce=200):
                    if self._stop.is_set():
                        return
                    if not any(Path(p) == self.paths.settings for _, p in changes):
                        continue
                    try:
                        self.host_keys.apply()
                    except HostKeyError as exc:
                        # Rolled back inside `apply`, so the static bindings still hold.
                        self.events.emit("hostkeys.failed", error=str(exc))
                        logger.error("reserved host keys: %s", exc)
                    try:
                        self._apply_keyboard()
                    except keyboard.KeyboardError as exc:
                        # The compositor refused it, so the keymap it had still holds.
                        self.events.emit("keyboard.failed", error=str(exc))
                        logger.error("the user's keyboard and display: %s", exc)
                    try:
                        self._apply_look()
                    except (HostSurfaceError, HostIpcError, RuntimeError_) as exc:
                        self.events.emit("look.failed", error=str(exc))
                        logger.error("the user's look: %s", exc)
            except Exception as exc:                   # noqa: BLE001
                if self._stop.is_set():
                    return
                self.events.emit("hostkeys.watch_failed", error=str(exc))
                time.sleep(2.0)

    def _watch_runtime(self) -> None:
        """Each (re)subscription asks for the events since the last one handled, so none is
        lost between the start's reconcile and the first, or across a reconnect. One at that
        very instant may come again, and is skipped. An event is marked handled before it is,
        so one that fails is not replayed into the same failure."""
        while not self._stop.is_set():
            try:
                for event in self.session.runtime.events(
                        label_filter={labels.MANAGED: "true"}, since=self._runtime_seen):
                    if self._stop.is_set():
                        return
                    if event["timeNano"] <= self._runtime_seen:
                        continue
                    self._runtime_seen = event["timeNano"]
                    self._on_runtime_event(event)
            except Exception as exc:                   # noqa: BLE001
                if self._stop.is_set():
                    return
                self.events.emit("runtime.event_stream_failed", error=str(exc))
                time.sleep(2.0)

    def _on_runtime_event(self, event: dict) -> None:
        """A supervised container's `die` is judged on its unit's own queue (`supervisor.py`)
        — a sandbox's with that sandbox's other container work, never a machine-wide
        reconcile, which takes the session lock every queue caller is already waiting behind.
        Only `die` says a container ended; its `destroy` follows a removal."""
        if event.get("Action") != "die":
            return
        actor = event.get("Actor") or {}
        attrs = actor.get("Attributes") or {}
        unit = unit_of(attrs.get(labels.ROLE, ""), attrs)
        if unit is None:
            return
        self.session.queues.submit(unit.queue,
                                   lambda c=actor.get("ID"): self._exit(unit, c), "exit")

    def _exit(self, unit: Unit, container_id: str) -> None:
        """Nobody waits on the queued job, so its failure is said here or not at all."""
        try:
            self.session.on_exit(unit, container_id)
        except Exception as exc:
            self.events.emit("container.exit_failed", **unit.scope(), kind=unit.kind,
                             unit=unit.name, reason=f"{type(exc).__name__}: {exc}")
            raise

    def _watch_files(self) -> None:
        """Dependency files are watched by raigolmid itself, never by Compose Watch: Watch
        would replace the body without the controlled swap. The definition
        directories are watched too, so a face, toolbelt or body written while the daemon
        runs — by the user or by an agent asked for one — reaches the selector. The
        credential's directory is watched so a credential stored while the daemon runs is
        said (`credential.stored`): the manager waits on it."""
        try:
            from watchfiles import watch
        except ImportError:
            self.events.emit("watch.unavailable",
                             message="watchfiles is not installed; dependency changes "
                                     "will not trigger rebuilds automatically")
            self._stop.wait()
            return
        stored = self.paths.agent_credentials
        # Read on every wake as well as on a change, because the first credential creates its
        # directory, and that directory is not being watched yet.
        was_set = credential.is_set(stored)

        def roots_of(watched: dict[str, list[Path]], definitions: list[Path]) -> set[str]:
            # The credential's directory does not exist until the first credential is stored.
            return ({str(p.parent) for paths in watched.values() for p in paths}
                    | {str(d) for d in definitions}
                    | ({str(stored.parent)} if stored.parent.is_dir() else set()))

        while not self._stop.is_set():
            definitions = self.session.definition_roots()
            roots = roots_of(self.session.watched_paths(), definitions)
            if not roots:
                time.sleep(2.0)
                continue
            try:
                # `yield_on_timeout` wakes the loop with no changes too, because an instance
                # that starts later adds a root this watch is not on.
                for changes in watch(*roots, stop_event=self._stop, debounce=200,
                                     yield_on_timeout=True):
                    changed = {Path(p) for _, p in changes}
                    now_set = credential.is_set(stored)
                    if now_set and (not was_set or stored in changed):
                        self.events.emit("credential.stored")
                    was_set = now_set
                    if any(c.is_relative_to(d) for c in changed for d in definitions):
                        # Emitted after the catalogue is re-read: maintenance sweeps on this
                        # event, and a stale catalogue sends it to document a deleted layer.
                        self.session.rediscover()
                        self.events.emit("definitions.changed",
                                         files=[str(p) for p in sorted(changed)])
                    watched = self.session.watched_paths()
                    for instance, paths in watched.items():
                        if changed & set(paths):
                            self.events.emit("watch.triggered", instance=instance,
                                             files=[str(p) for p in sorted(changed)])
                            self.session.on_watched_change(instance)
                    if roots_of(watched, definitions) != roots:
                        break
            except Exception as exc:                   # noqa: BLE001
                if self._stop.is_set():
                    return
                self.events.emit("watch.failed", error=str(exc))
                time.sleep(2.0)

    def stop(self) -> None:
        self.events.emit("raigolmid.stopping", epoch=self.epoch,
                         message="leaving running containers running; reconciliation "
                                 "will adopt them on the next start")
        self._stop.set()
        self.api.shutdown()
        self.api.server_close()
        self.credproxy.close()
        self.agent_sockets.close_all()
        self.face_sockets.close_all()
        self.session.close()
        self.lock.release()

    def _thread_died(self, args: threading.ExceptHookArgs) -> None:
        """Kept for `dead_threads` to say, and printed to the journal as Python would."""
        if args.thread is not None:
            self._died[args.thread.name] = "".join(traceback.format_exception(
                args.exc_type, args.exc_value, args.exc_traceback))
        threading.__excepthook__(args)

    def dead_threads(self) -> dict[str, str]:
        """Every one of the daemon's threads runs until it is told to stop, so one that has
        ended is a part of the machine that stopped working — its name and why."""
        return {t.name: self._died.get(t.name, "it returned")
                for t in self._threads if not t.is_alive()}

    def run_forever(self) -> int:
        """Returns the exit status. A thread that ended ends the daemon with 1, and systemd's
        `Restart=on-failure` starts a fresh one that re-derives the machine: kept
        running, the daemon would answer `status` while its API, its runtime events or a tab's
        channel had silently stopped."""
        self.start()
        stopping = threading.Event()

        def handle(signum, _frame):
            logger.info("received %s", signal.Signals(signum).name)
            stopping.set()

        signal.signal(signal.SIGTERM, handle)
        signal.signal(signal.SIGINT, handle)
        dead: dict[str, str] = {}
        try:
            while not stopping.wait(1.0):
                if dead := self.dead_threads():
                    for name, why in dead.items():
                        logger.error("thread %s ended: %s", name, why)
                    self.events.emit("raigolmid.thread_died", threads=sorted(dead),
                                     reasons=dead, message="restarting the daemon")
                    break
        finally:
            self.stop()
        return 1 if dead else 0
