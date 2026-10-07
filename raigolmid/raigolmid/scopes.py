"""What an agent tab's own socket answers.

An agent container is given one directory of the runtime dir, holding one socket
(`Paths.agent_socket_dir`), and the daemon serves it with a table bound to that tab. The tab
is never a parameter — the socket a call arrived on is the tab — and the sandbox a call acts
on is resolved here at call time, since a tab opens and closes its sandbox as it works.
So nothing in a container can name another tab, reach
another sandbox, or read another tab's question. Every socket also carries its tab's channel
(`channel.py`). The janitor's socket answers the machine table instead.

A face has a socket of its own too (`FaceSockets`): the machine's state
read-only, asking the host to show the AI terminal, and any sandbox's launcher by name — the
only way a face reaches a toolbelt, so it never learns where a launcher's socket is.

`AgentSockets` serves one per open tab: opened on `tab.opened`, closed on `tab.closed`, and
at the daemon's start one for every tab the intent holds, so running containers find a
restarted daemon in the directory they already mount. A container starts as soon as its tab
opens, so its entrypoint waits for its socket to answer before the agent starts.
"""
from __future__ import annotations

import shutil
import threading
from typing import Any, Callable

from . import api, hostsurfaces, jobs, naming, permissions
from .api import ApiServer
from .channel import Channels
from . import coordinator
from .events import EventLog
from .intent import JANITOR
from .paths import Paths
from .questions import Questions
from .session import Session, SessionError

SOCKET = "raigolmid.sock"

# Of the full table, what the janitor's machine scope reaches: the machine's state
# read-only, its channel, and the repairs the daemon already has — never a shell in a sandbox.
MACHINE = ("version", "status", "list_items", "events", "container_logs", "journal",
           "crash_logs", "restart_agent", "repair", "reconcile", "rediscover", "unstick",
           "tell", "disk", "memory")

# Of the full table, what a face reaches: a read-only view of the machine, and every sandbox's
# toolbelt by name (a face works with every body). Beyond it, what the user does themselves,
# which a face does for them: their words to an agent tab
# (`ask`), their selection (`select`, `deselect`), and a command in a sandbox (`exec`).
FACE = ("version", "status", "list_items", "events", "open_launcher")

TRIAL_TERMINAL = ("not shown: this face is tried off the user's screen, and the AI terminal "
                  "is on theirs")

TRIAL_SANDBOX = ("this face is tried off the user's screen, and their sandboxes are not "
                 "reached from it")

TRIAL_USERS = ("this face is tried off the user's screen, and their tabs and their selection "
               "are not reached from it")

NO_SANDBOX = ("this tab has no sandbox open, so there is nothing to run this in. Open one with "
              "`sandbox_open` and the toolbelt the work needs; `status` lists the toolbelts "
              "this body can run. Your files are at /work either way.")


def instance_of(session: Session, tab_id: str) -> str:
    """The sandbox this tab acts on now: the one it opened."""
    tab = session.intent.tabs.get(tab_id)
    if tab is None:
        raise SessionError(f"tab '{tab_id}' is not open")
    opened = session._open_sandbox_of(tab)
    if opened is None:
        raise SessionError(NO_SANDBOX)
    return opened


def tab_status(session: Session, questions: Questions, channels: Channels,
               tab_id: str) -> dict[str, Any]:
    """`status` as one tab sees it: the selection, its own tab and sandbox, and the
    definitions. The bare host state is a state rather than a failure, and it is the
    one an agent is asked to repair from, so it answers with a note instead of an error."""
    full = api.with_tab_states(session.status(), questions, channels)
    try:
        instance, note = instance_of(session, tab_id), None
    except SessionError as exc:
        instance, note = None, str(exc)
    out: dict[str, Any] = {
        "instance": instance,
        "tab": next((a for a in full["agents"] if a["tab"] == tab_id), None),
        # No other tab's sandbox is named: whether the face is on this tab's is what it may know.
        "session": {**{k: v for k, v in full["session"].items() if k != "focused_instance"},
                    "instances": [i for i in full["session"]["instances"] if i == instance],
                    "on_face": instance is not None
                    and full["session"]["focused_instance"] == instance},
        "definition_errors": full.get("definition_errors", []),
        "builds": full.get("builds", {}),
    }
    if tab_id != JANITOR:
        out["toolbelts"] = session.toolbelts_for(tab_id)
    tab = session.intent.tabs.get(tab_id)
    if tab is not None and tab.machine:
        # The face it is trying off the user's screen (`try_face`), which only it drives.
        out["trial"] = full["face_runtime"]["trial"]
    if instance is None:
        out["scope_note"] = note
    else:
        out["detail"] = full["instances"].get(instance, {})
    return out


def _talking(session: Session, questions: Questions, channels: Channels,
             tab_id: str) -> dict[str, Callable[..., Any]]:
    """One tab's questions, its channel, and its Claude Code hooks.

    A prompt taken that carries no channel `seq` is one the user typed, and it answers
    whatever the tab asked. A turn ending with nothing asked, nothing on its way and no
    message to another tab open is done."""
    def ask(message: str, choices: list[str] | None = None) -> str:
        tab = session.intent.tabs.get(tab_id)
        if tab is not None and tab.machine and session.intent.hands_off(tab_id):
            raise SessionError("the user handed you tabs to manage and is away: nothing is put "
                               "to them until they take the tabs back, so decide it yourself")
        return questions.ask(tab_id, message, choices)

    def swap(toolbelt: str) -> dict:
        # A tab the user handed over has their yes to everything it does.
        if (session.intent.focused_instance != session.intent.sandbox_of(tab_id)
                or session.intent.hands_off(tab_id)):
            return session.toolbelt_swap(tab_id, toolbelt)
        # The active sandbox is the one the user works in: refused now if it could not
        # be done at all, and otherwise done on their yes.
        if not session.check_toolbelt_swap(tab_id, toolbelt):
            return {"status": "unchanged",
                    "next": f"The sandbox already runs toolbelt '{toolbelt}'."}
        body, _ = naming.split(instance_of(session, tab_id))
        id, always = questions.ask_permission(
            tab_id, permissions.swap_message(toolbelt),
            {"do": permissions.TOOLBELT_SWAP, "toolbelt": toolbelt},
            project=body or naming.WORK)
        return {"status": "answered_always" if always else "asked", "permission": id,
                "next": "End your turn now. Whether the user allowed it, and what came of it, is "
                        "your next message."}

    def activity(busy: bool, channel_seq: int | None = None,
                 prompt: str | None = None, error: str | None = None) -> dict:
        # Only a prompt taken carries one; a stop that stays busy does not, and is not the user's.
        if busy and channel_seq is None and prompt is not None:
            questions.answered_in_terminal(tab_id, prompt)
            session.release(tab_id)
        # A turn an API error cut off is not done: it is resumed (`limits.py`).
        done = not busy and error is None and channels.turn_done(
            tab_id, questions.tab_state(tab_id) is None)
        return session.agent_activity(tab_id, busy, channel_seq, done, error)

    return {
        "ask": ask,
        "sandbox_open": lambda toolbelt: session.sandbox_open(tab_id, toolbelt),
        "toolbelt_swap": swap,
        "channel_take": lambda: channels.take(tab_id),
        "channel_state": lambda: channels.state(tab_id),
        "midturn": lambda: channels.take_midturn(tab_id),
        "agent_activity": activity,
        "agent_session_started": lambda: session.agent_session_started(tab_id),
    }


def build_tab_methods(session: Session, questions: Questions, channels: Channels,
                      tab_id: str) -> dict[str, Callable[..., Any]]:
    here = lambda: instance_of(session, tab_id)                     # noqa: E731
    return {
        "version": lambda: {"protocol": api.PROTOCOL_VERSION, "epoch": session.epoch},
        "status": lambda: tab_status(session, questions, channels, tab_id),
        "list_items": lambda kind=None: session.list_items(kind),
        "index": lambda: session.document_index(tab_id),
        # The session answers with the machine's whole status; a tab is answered with its own.
        "select": lambda kind, id: (session.select(kind, id, tab_id),
                                    tab_status(session, questions, channels, tab_id))[1],
        "deselect": lambda kind: (session.deselect(kind, tab_id),
                                  tab_status(session, questions, channels, tab_id))[1],
        "exec": lambda cmd, cwd="/work", timeout=300.0: session.exec(here(), cmd, cwd,
                                                                     timeout),
        "job_start": lambda name, cmd, cwd="/work": session.jobs.start(tab_id, here(), name,
                                                                       cmd, cwd),
        "job_wait": lambda name, timeout=jobs.WAIT_MOST: session.jobs.wait(tab_id, name,
                                                                           timeout),
        "jobs": lambda: session.jobs.list(tab_id),
        "rebuild_body": lambda: session.rebuild_body(
            here(), why=f"{tab_id}'s request").to_dict(),
        "restart_body": lambda: session.restart_body(here()),
        "show_file": lambda path, line=1: session.show_file(here(), path, line),
        "show_url": lambda url: session.show_url(
            session._open_sandbox_of(session.intent.tabs[tab_id]), url),
        "screenshot": lambda trial=False: session.screenshot(tab_id, trial),
        "face_input": lambda action, text=None, x=None, y=None, button="left", trial=False:
            session.face_input(tab_id, action, text, x, y, button, trial),
        "try_face": lambda face: session.try_face(tab_id, face),
        "stop_trial": lambda: session.stop_trial(tab_id),
        "face_windows": lambda: session.face_windows(tab_id),
        "restart_face": lambda: session.restart_face(tab_id),
        "seed_face_settings": lambda face, source: session.seed_face_settings(
            tab_id, face, source),
        "history": lambda n=50: session.history(here(), n),
        "logs": lambda tail=100: api._logs(session, here(), tail),
        "search_packages": lambda query, limit=20: session.search_packages(query, limit),
        # Tabs message each other; the janitor does not (`messages.py`).
        "message": lambda to, content: channels.messages.send(tab_id, to, content),
        "reply": lambda message, content: channels.messages.reply(tab_id, message, content),
        # The machine tab's, refused to every other.
        **coordinator.methods(session, questions, channels, tab_id),
        **_talking(session, questions, channels, tab_id),
    }


def build_machine_methods(full: dict[str, Callable[..., Any]], session: Session,
                          questions: Questions,
                          channels: Channels) -> dict[str, Callable[..., Any]]:
    return {**{name: full[name] for name in MACHINE},
            "index": session.machine_index,
            # Any sandbox's body, which is the janitor's to repair too.
            "restart_body": lambda instance: session.restart_body(instance),
            "rebuild_body": lambda instance: session.rebuild_body(
                instance, why="the janitor's request").to_dict(),
            **_talking(session, questions, channels, JANITOR)}


class AgentSockets:
    def __init__(self, paths: Paths, session: Session, events: EventLog,
                 questions: Questions, channels: Channels,
                 full: dict[str, Callable[..., Any]], ready: threading.Event) -> None:
        self.paths = paths
        self.session = session
        self.events = events
        self.questions = questions
        self.channels = channels
        self.full = full
        self.ready = ready
        self._sub = events.subscribe()
        self._lock = threading.Lock()
        self._servers: dict[str, tuple[ApiServer, threading.Thread]] = {}

    def start(self) -> None:
        """Every open tab's socket, before the reconcile restarts its container, so the
        container's first call waits on `ready` rather than finding no socket."""
        for tab_id in list(self.session.intent.tabs):
            self.open(tab_id)

    def run(self, stop: threading.Event) -> None:
        """Ends, and so ends the daemon, when an open tab's socket stops serving: its agent is
        cut off from the machine, and a fresh start serves every open tab's again."""
        while not stop.is_set():
            _require_serving(self._serving(), stop)
            for event in self._sub.drain(timeout=1.0):
                if event.type == "tab.opened" and event.tab is not None:
                    self.open(event.tab)
                elif event.type == "tab.closed" and event.tab is not None:
                    self.close(event.tab)
            if self._sub.dropped:
                # A dropped open leaves a container waiting on a socket that never comes;
                # its entrypoint says so. A dropped close leaves a socket to the next start.
                count, self._sub.dropped = self._sub.dropped, 0
                self.events.emit("agent_sockets.events_dropped", count=count)

    def open(self, tab_id: str) -> None:
        with self._lock:
            if tab_id in self._servers:
                return
            directory = self.paths.agent_socket_dir(tab_id)
            try:
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                directory.chmod(0o700)
                methods = (build_machine_methods(self.full, self.session, self.questions,
                                                 self.channels)
                           if tab_id == JANITOR
                           else build_tab_methods(self.session, self.questions,
                                                  self.channels, tab_id))
                server = ApiServer(directory / SOCKET, methods, self.events, ready=self.ready,
                                   subscribe=False)
            except OSError as exc:
                self.events.emit("agent_socket.failed", tab=tab_id, error=str(exc))
                return
            self._servers[tab_id] = (server, _served(server, f"api-{tab_id}"))
        self.events.emit("agent_socket.opened", tab=tab_id, socket=str(server.socket_path))

    def close(self, tab_id: str) -> None:
        with self._lock:
            served = self._servers.pop(tab_id, None)
        if served is None:
            return
        server, _ = served
        server.shutdown()
        server.server_close()
        # The janitor's directory stays: its id is fixed, and a janitor opened under it at
        # once has already bind-mounted it. Every other tab's id is never reused.
        if tab_id != JANITOR:
            shutil.rmtree(self.paths.agent_socket_dir(tab_id))

    def close_all(self) -> None:
        """The daemon stopping: sockets go, directories stay, so running containers keep
        the mount the next start serves again."""
        with self._lock:
            servers, self._servers = list(self._servers.values()), {}
        for server, _ in servers:
            server.shutdown()
            server.server_close()

    def _serving(self) -> list[threading.Thread]:
        with self._lock:
            return [thread for _, thread in self._servers.values()]


def build_face_methods(full: dict[str, Callable[..., Any]], events: EventLog,
                       trial: bool) -> dict[str, Callable[..., Any]]:
    def show_ai_terminal() -> str:
        answer = TRIAL_TERMINAL if trial else hostsurfaces.show_ai_terminal_for_face()
        events.emit("face.asked_ai_terminal", trial=trial, answer=answer)
        return answer

    def open_launcher(instance: str) -> Any:
        if trial:
            raise SessionError(TRIAL_SANDBOX)
        return full["open_launcher"](instance)

    def exec(target: str, cmd: list[str], cwd: str = "/work", timeout: float = 300.0) -> Any:
        if trial:
            raise SessionError(TRIAL_SANDBOX)
        return full["exec"](target, cmd, cwd, timeout)

    def select(kind: str, id: str) -> Any:
        if trial:
            raise SessionError(TRIAL_USERS)
        return full["select"](kind, id)

    def deselect(kind: str) -> Any:
        if trial:
            raise SessionError(TRIAL_USERS)
        return full["deselect"](kind)

    def ask(content: str, tab: str | None = None) -> dict[str, Any]:
        """The user's words, typed in the face, as the named tab's next message; with no tab
        named, the tab they view, else the machine tab. Pushed when that tab's turn ends."""
        if trial:
            raise SessionError(TRIAL_USERS)
        if not content.strip():
            raise SessionError("nothing to ask: the words are empty")
        status = full["status"]()
        scopes = {agent["tab"]: agent["scope"] for agent in status["agents"]}
        target = tab or status["terminal"]["viewing"]
        if target not in scopes:
            if tab is not None:
                raise SessionError(f"no tab {tab}; the tabs are {sorted(scopes)}")
            target = next(t for t, scope in scopes.items() if scope == "machine")
        if scopes[target] == "janitor":
            raise SessionError("the janitor tab takes the machine's failures, not the user's words")
        events.emit("face.asked", tab=target, deliver={
            "content": f"From the user's face:\n\n{content}", "meta": {"from": "face"}})
        return {"tab": target, "status": "queued",
                "next": "It is pushed when the tab's turn ends; show_ai_terminal shows it."}

    return {**{name: full[name] for name in FACE}, "show_ai_terminal": show_ai_terminal,
            "open_launcher": open_launcher, "exec": exec, "select": select,
            "deselect": deselect, "ask": ask}


class FaceSockets:
    """The user's face's socket and the trial's, served for the daemon's whole life so a face
    started, switched or tried finds its socket already answering. Whatever runs in the face
    may ask: the face is theirs, and the socket is its only way into the machine. Its stream
    of events is part of the read-only view."""

    def __init__(self, paths: Paths, events: EventLog,
                 full: dict[str, Callable[..., Any]], ready: threading.Event) -> None:
        self.paths = paths
        self.events = events
        self.full = full
        self.ready = ready
        self._servers: list[tuple[ApiServer, threading.Thread]] = []

    def start(self) -> None:
        for trial in (False, True):
            directory = self.paths.face_socket_dir(trial)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
            server = ApiServer(directory / SOCKET,
                               build_face_methods(self.full, self.events, trial), self.events,
                               ready=self.ready)
            self._servers.append(
                (server, _served(server, f"api-face{'-trial' if trial else ''}")))
            self.events.emit("face_socket.opened", trial=trial, socket=str(server.socket_path))

    def run(self, stop: threading.Event) -> None:
        """Ends, and so ends the daemon, when a face's socket stops serving."""
        while not stop.wait(1.0):
            _require_serving([thread for _, thread in self._servers], stop)

    def close_all(self) -> None:
        """Sockets go, directories stay, for `AgentSockets.close_all`'s reason."""
        servers, self._servers = self._servers, []
        for server, _ in servers:
            server.shutdown()
            server.server_close()


def _served(server: ApiServer, name: str) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, name=name, daemon=True)
    thread.start()
    return thread


def _require_serving(threads: list[threading.Thread], stop: threading.Event) -> None:
    """Read after the threads, since the daemon's stop is set before its sockets close."""
    ended = [t.name for t in threads if not t.is_alive()]
    if ended and not stop.is_set():
        raise RuntimeError(f"{', '.join(ended)} stopped serving while still wanted")
