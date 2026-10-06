"""The Unix socket JSON API.

The full table is served at `$XDG_RUNTIME_DIR/raigolmid.sock`, to `rai` and the host
surfaces; each agent container reaches only its own tab's socket, which serves a table
bound to that tab (`scopes.py`). Newline-delimited JSON: a request is `{"method": …, "params": {…}, "id": …}`
and the answer is `{"id": …, "ok": true, "result": …}` or `{"id": …, "ok": false,
"error": …}`.

The method table below is the whole API, including which methods queue. Mutating
methods go through the per-instance queues inside `Session`; read methods do not queue
behind them, so `status` still answers while a build is running — which is the whole
reason a user can watch a rebuild happen.
"""
from __future__ import annotations

import inspect
import json
import logging
import socket
import socketserver
import subprocess
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import disk, stalls
from .events import EventLog
from .channel import Channels
from .intent import JANITOR
from .catalog import CatalogError
from .history import History, HistoryError
from .messages import MessageError
from .docwrite import DocumentError
from .library import Library
from .questions import PERMISSIONS_ABSENT, QuestionError, Questions, parse_permissions
from .registry import RegistryError
from .jobs import JobError
from .session import Session, SessionError
from .viewing import Viewing

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1


@dataclass(frozen=True)
class Handoff:
    """An answer that carries a connected socket: sent to the caller with the answer over
    SCM_RIGHTS and closed on this side, so the pipe is the caller's and raigolmid relays no
    byte of it."""
    result: Any
    sock: socket.socket


def build_methods(session: Session, events: EventLog, questions: Questions,
                  channels: Channels, viewing: Viewing,
                  history: History) -> dict[str, Callable[..., Any]]:
    """Everything the host's own callers can ask for. What an agent asks, and what
    only an agent reports, is its tab's socket's (`scopes.py`)."""
    library = Library(session.paths, session, parse_permissions, PERMISSIONS_ABSENT)
    return {
        "list_items": lambda kind=None: session.list_items(kind),
        "select": lambda kind, id, by_tab=None: session.select(kind, id, by_tab),
        "deselect": lambda kind, by_tab=None: session.deselect(kind, by_tab),
        "status": lambda: with_marks(with_tab_states(session.status(), questions, channels),
                                     viewing),
        "open_launcher": lambda instance: Handoff({"instance": instance},
                                                  session.open_launcher(instance)),
        "ensure_tabs": lambda: session.ensure_tabs(),
        "restart_agent": lambda tab_id, resume=True: session.restart_agent(tab_id, resume),
        "close_tab": lambda tab_id: session.close_tab(tab_id),
        "unstick": lambda tab_id, note: stalls.unstick(session, events, tab_id, note),
        "tell": lambda tab_id, note: _tell(session, events, tab_id, note),
        "disk": lambda: disk.accounted(session),
        "exec": lambda target, cmd, cwd="/work", timeout=300.0: session.exec(
            target, cmd, cwd, timeout),
        "rebuild_body": lambda instance_id: session.rebuild_body(
            instance_id, why="your request").to_dict(),
        # The agent closes its own loop and shows the user what it did.
        "restart_body": lambda instance_id: session.restart_body(instance_id),
        "show_file": lambda instance_id, path, line=1: session.show_file(instance_id, path,
                                                                          line),
        "screenshot": lambda tab: session.screenshot(tab),
        "set_face_driving": lambda allowed: session.set_face_driving(allowed),
        "show_url": lambda instance_id, url: session.show_url(instance_id, url),
        "history": lambda instance_id, n=50: session.history(instance_id, n),
        "search_packages": lambda query, limit=20: session.search_packages(query, limit),
        "reconcile": lambda: _reconcile(session, events),
        "repair": lambda instance: session.repair(instance),
        "rediscover": lambda: {"errors": session.rediscover().errors},
        "logs": lambda instance_id, tail=100: _logs(session, instance_id, tail),
        "events": lambda n=50: [json.loads(e.to_json()) for e in events.tail(n)],
        # The janitor's machine scope: what it reads to diagnose.
        "container_logs": lambda container, tail=100: _container_logs(session, container,
                                                                      tail),
        "journal": lambda n=200: _journal(n),
        # What agents ask the user; a permission is answered in its tab's window.
        "questions": questions.pending,
        "answer": questions.answer,
        # The history the menu shows, and the one action on it.
        "menu": lambda: with_items(history.entries(), questions, session.managed_tabs()),
        "seen": history.seen,
        "overturn": questions.overturn,
        "crash_logs": lambda name=None: _crash_logs(session, name),
        "version": lambda: {"protocol": PROTOCOL_VERSION, "epoch": session.epoch},
        "terminal_viewing": viewing.report,
        # The catalog window's whole vocabulary.
        "catalog": lambda server=False: session.catalog.listing(server),
        "catalog_thumbnail": lambda kind, id: session.catalog.thumbnail(kind, id),
        "catalog_download": lambda kind, id: session.catalog.download(kind, id),
        "catalog_install": lambda kind, id: session.catalog.install(kind, id),
        "catalog_delete": lambda kind, id: session.catalog.delete(kind, id),
        "catalog_upload_files": lambda kind, id: session.catalog.upload_files(kind, id),
        "catalog_upload": lambda kind, id, excluded=None: session.catalog.upload(kind, id,
                                                                                 excluded),
        # Every document an agent reads, the user's to open and save (`library.py`).
        "documents": library.list,
        "document": library.read,
        "document_save": library.write,
    }


def with_tab_states(status: dict[str, Any], questions: Questions,
                    channels: Channels) -> dict[str, Any]:
    """Each tab working, or needing the user — idle, asking, or asking permission — or waiting
    on what is on its way to it: a question with the judge, an answer held for learning or
    pushed on its channel, or another tab's answer to its message (`questions.tab_state`,
    `messages.py`)."""
    for agent in status["agents"]:
        tab = agent["tab"]
        agent["state"] = ("working" if agent["busy"] else
                          questions.tab_state(tab)
                          or ("waiting" if channels.has_mail(tab)
                              or channels.messages.waiting(tab) else "idle"))
    return status


def with_items(entries: list[dict[str, Any]], questions: Questions,
               managed: set[str]) -> list[dict[str, Any]]:
    """Each question's or permission's entry with the item as it stands now (`history.py`),
    and whether its asker is a tab the machine tab manages, which a pending question
    is with instead of the user."""
    items = questions.items()
    return [{**e, "item": {**items[e["question"]], "managed": items[e["question"]]["tab"]
                           in managed}} if e["question"] else e for e in entries]


def with_marks(status: dict[str, Any], viewing: Viewing) -> dict[str, Any]:
    """A tab is marked while it needs the user — an idle one until they have viewed it, a
    question or permission until it is settled — and the terminal's collapsed tab is lit
    while any is marked, the janitor only while it asks: its idle ends each job and is
    shown in the history. A tab the user handed over, and the machine tab while it manages
    any, never needs them (`Intent.hands_off`). Asked of `with_tab_states`' answer."""
    for agent in status["agents"]:
        state = agent["state"]
        agent["marked"] = not agent["hands_off"] and (
            state in ("asking", "permission") or state == "idle" and viewing.unseen(agent["tab"]))
    status["terminal"] = {"viewing": viewing.viewed(),
                          "lit": any(a["marked"] and (a["tab"] != JANITOR or
                                                      a["state"] in ("asking", "permission"))
                                     for a in status["agents"])}
    return status


def _reconcile(session: Session, events: EventLog) -> dict[str, Any]:
    """The sandboxes, then the resident host surfaces: a surface that exited is part of
    what runs not matching what is intended. Boot draws them itself (`Daemon`)."""
    from .hostsurfaces import restore_resident

    report = session.reconcile().to_dict()
    report["host_surfaces"] = restore_resident(
        session.runtime, session.paths,
        lambda role, error: events.emit(f"{role}.failed", error=error))
    return report


def _logs(session: Session, instance_id: str, tail: int) -> dict[str, Any]:
    from . import naming
    logs = {"view": session.runtime.logs(naming.view(instance_id), tail)}
    if naming.has_body(instance_id):
        logs["body"] = session.runtime.logs(naming.body_container(instance_id), tail)
    return logs


def _container_logs(session: Session, container: str, tail: int) -> dict[str, Any]:
    """Any container raigolmid manages, and only those: the janitor diagnoses the machine's
    parts, not whatever else runs on the host."""
    from . import labels
    info = session.runtime.inspect(container)
    if info is None:
        raise SessionError(f"no container {container}")
    if info.labels.get(labels.MANAGED) != "true":
        raise SessionError(f"{container} is not one raigolmid manages")
    return {"container": container, "running": info.running, "exit_code": info.exit_code,
            "logs": session.runtime.logs(info.id, tail)}


def _tell(session: Session, events: EventLog, tab_id: str, note: str) -> dict[str, Any]:
    """The janitor's word to a tab, without stopping it: queued on its channel and pushed
    when the tab is next idle (`channel.py`)."""
    if tab_id not in session.intent.tabs:
        raise SessionError(f"no tab {tab_id!r}")
    if not note.strip():
        raise SessionError("tell says something")
    events.emit("janitor.told", tab=tab_id, deliver={
        "content": note, "meta": {"from": "janitor"}})
    return {"tab": tab_id, "note": "queued for when it is next idle"}


def _journal(n: int) -> dict[str, Any]:
    """The daemon's unit journal: what it logged, including tracebacks the event log only
    points at. The agent image has no journalctl, so the daemon reads it."""
    result = subprocess.run(
        ["journalctl", "--user", "--unit", "raigolmid.service", "--lines", str(n),
         "--no-pager", "--output", "short-iso"],
        capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise SessionError(f"journalctl exited {result.returncode}: {result.stderr.strip()}")
    return {"journal": result.stdout}


def _crash_logs(session: Session, name: str | None) -> dict[str, Any]:
    """The evidence each container's exit on its own left (`supervisor.py`): the list, or
    one."""
    directory = session.paths.crashes
    if name is None:
        return {"logs": sorted(p.name for p in directory.glob("*.log"))}
    path = directory / name
    if path.parent != directory or not path.is_file():
        raise SessionError(f"no crash log {name} in {directory}")
    return {"name": name, "log": path.read_text(errors="replace")}


class Handler(socketserver.StreamRequestHandler):
    server: "ApiServer"

    def handle(self) -> None:
        self.server.ready.wait()
        try:
            for line in self.rfile:
                line = line.strip()
                if not line:
                    continue
                try:
                    request = json.loads(line)
                except json.JSONDecodeError as exc:
                    self._send({"ok": False, "error": f"not JSON: {exc}"})
                    continue
                self._dispatch(request)
        except ConnectionError as exc:
            # A client gone mid-request — a follow window closing with its tab, a caller
            # that timed out — is ordinary: one line, not socketserver's traceback.
            logger.info("api client went away: %s", exc)

    def _dispatch(self, request: dict[str, Any]) -> None:
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}

        if method == "subscribe" and self.server.subscribe:
            self._subscribe(request_id)
            return

        fn = self.server.methods.get(method)
        if fn is None:
            self._send({"id": request_id, "ok": False,
                        "error": f"unknown method '{method}'",
                        "known": sorted(self.server.methods)})
            return
        # Only the call's parameters not fitting the method are bad params; a TypeError from
        # inside it is a failure like any other.
        try:
            call = inspect.signature(fn).bind(**params)
        except TypeError as exc:
            self._send({"id": request_id, "ok": False,
                        "error": f"{method}: {exc}", "kind": "bad_params"})
            return
        try:
            result = fn(*call.args, **call.kwargs)
        except (SessionError, QuestionError, MessageError, HistoryError, CatalogError,
                RegistryError, DocumentError, JobError) as exc:
            self._send({"id": request_id, "ok": False, "error": str(exc),
                        "kind": type(exc).__name__})
        except Exception as exc:                       # noqa: BLE001
            # Loud, with the traceback in the event log: a daemon that answers "error"
            # with nothing behind it makes the next question unanswerable.
            self.server.events.emit("api.error", method=method, error=str(exc),
                                    traceback=traceback.format_exc()[-4000:])
            self._send({"id": request_id, "ok": False, "error": str(exc),
                        "kind": type(exc).__name__})
        else:
            if isinstance(result, Handoff):
                self._hand_over(request_id, result)
            else:
                self._send({"id": request_id, "ok": True, "result": result})

    def _hand_over(self, request_id: Any, handoff: Handoff) -> None:
        line = (json.dumps({"id": request_id, "ok": True, "result": handoff.result},
                           default=str) + "\n").encode()
        try:
            self.wfile.flush()
            sent = socket.send_fds(self.connection, [line], [handoff.sock.fileno()])
            self.connection.sendall(line[sent:])
        finally:
            handoff.sock.close()

    def _subscribe(self, request_id: Any) -> None:
        sub = self.server.events.subscribe()
        self._send({"id": request_id, "ok": True, "result": {"subscribed": True}})
        try:
            while True:
                for event in sub.drain(timeout=5.0):
                    self._send({"event": json.loads(event.to_json())})
                if sub.dropped:
                    self._send({"event": {"type": "subscriber.dropped",
                                          "count": sub.dropped}})
                    sub.dropped = 0
        except ConnectionError:
            return
        finally:
            self.server.events.unsubscribe(sub)

    def _send(self, payload: dict[str, Any]) -> None:
        self.wfile.write((json.dumps(payload, default=str) + "\n").encode())
        self.wfile.flush()


class ApiServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, socket_path: Path, methods: dict[str, Callable[..., Any]],
                 events: EventLog, *, ready: threading.Event,
                 subscribe: bool = True) -> None:
        self.socket_path = socket_path
        self.events = events
        self.methods = methods
        # Set once the daemon has re-derived the machine. A connection is accepted
        # at once and answered after it, so an early caller waits for a true answer.
        self.ready = ready
        # The event stream names every tab and sandbox, so a scoped socket does not serve it.
        self.subscribe = subscribe
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        if socket_path.exists():
            socket_path.unlink()
        super().__init__(str(socket_path), Handler)
        socket_path.chmod(0o600)

    def server_close(self) -> None:
        super().server_close()
        try:
            self.socket_path.unlink()
        except OSError:
            pass
