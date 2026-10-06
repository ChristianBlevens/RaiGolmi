"""A tab that is working and getting nothing done goes to the manager, whatever the cause.

Every way a tab hangs looks the same from outside: it is busy, and nothing it does changes.
A turn waiting on a tool that never returns or on a background job that has died, a session
interrupted without its `Stop` hook, a hung Claude Code: its transcript stops growing
(**stalled**). A tab retrying the same failure: its transcript grows and its working copy —
HEAD and the files that differ from it — does not (**spinning**). Neither reading knows a
cause, so a cause nobody has met yet is caught the same way. Only a body tab has a working
copy of its own, so only a body tab is read for spinning; a job it runs printing
(`job.progressed`, `jobs.py`) is its work moving too.

Each is said once per episode as `tab.stalled` or `tab.spinning`, which the manager takes
(`manager.py`) with the evidence: the transcript's last calls, the background tasks the tab
named as waited on (`agent_stop.py`), and the processes in its agent's container and its
sandbox's toolbelt — read from the host, since the manager has no shell in a sandbox. A wait that is real — a long test the tab is rightly
waiting on — is said too, and the manager's look leaves it alone: one look is the price of
never missing a hang. The manager's own stall has nobody to take it, so it is unstuck here.

`unstick` is the manager's repair: the tab restarted on its conversation, which ends its turn
and every background task in its container, and a note queued on its channel (`channel.py`),
pushed when the new session comes up, saying what was stopped and what to do instead.

Nothing is said while the account's usage limit holds: no turn moves then, and nobody could
act. The readings are in memory; a daemon restart starts every clock again.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import git, naming
from .agent_stop import Memory
from .transcript import latest_transcript, main_rows
from .events import Event, EventLog
from .intent import MANAGER
from .runtime.base import RuntimeError_
from .session import SessionError

if TYPE_CHECKING:
    from .session import Session

# How long a busy tab's transcript may stand still, and how long its working copy may while the
# transcript moves. Both set how soon a hang is looked at, never whether it is.
STALL_SECONDS = 600.0
SPIN_SECONDS = 1800.0
# Reading a working copy runs git, so the readings are taken this often rather than every second.
TICK_SECONDS = 30.0
# How much of the transcript the manager is handed.
LAST_CALLS = 8
CALL_CHARS = 400


@dataclass(slots=True)
class Watch:
    """One busy tab's readings: what was last seen, and since when."""
    size: int | None                  # the transcript's, None before it has one
    moved_at: float
    tree: tuple | None                # the working copy's, None where there is none to read
    changed_at: float
    stalled: bool = False             # said this episode
    spinning: bool = False


def working_copy_reading(repo: Path) -> tuple | None:
    """HEAD and every file that differs from it, by size and modification time: anything
    written into the work changes it. None for a working copy that is not a repository."""
    if not git.is_repo(repo):
        return None
    head = git.run(["rev-parse", "--verify", "-q", "HEAD"], repo, check=False).stdout.strip()
    listed = git.run(["ls-files", "-z", "-m", "-o", "--exclude-standard"], repo).stdout
    files = []
    for name in sorted(set(filter(None, listed.split("\0")))):
        try:
            st = (repo / name).stat()
            files.append((name, st.st_size, st.st_mtime_ns))
        except FileNotFoundError:
            files.append((name, None, None))      # deleted from the tree
    return (head, tuple(files))


def last_calls(home: Path, n: int = LAST_CALLS) -> list[dict[str, str]]:
    """The transcript's last `n` tool calls with what each was given and what came back, or
    `pending` for one with no result: a hang is usually the last of them."""
    transcript = latest_transcript(home)
    if transcript is None:
        return []
    calls: list[dict[str, str]] = []
    by_id: dict[str, dict[str, str]] = {}
    for row in main_rows(transcript):
        content = (row.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if part.get("type") == "tool_use":
                call = {"tool": str(part.get("name")),
                        "input": json.dumps(part.get("input"))[:CALL_CHARS],
                        "result": "pending"}
                calls.append(call)
                by_id[part.get("id")] = call
            elif part.get("type") == "tool_result" and part.get("tool_use_id") in by_id:
                result = part.get("content")
                if isinstance(result, list):
                    result = " ".join(p.get("text", "") for p in result if isinstance(p, dict))
                by_id[part["tool_use_id"]]["result"] = str(result)[:CALL_CHARS]
    return calls[-n:]


def waited_on(home: Path) -> list[str]:
    """The background tasks the tab last named as waited on (`agent_stop.py`)."""
    store = home / ".raigolmi" / "stop-memory.json"
    if not store.is_file():
        return []
    return sorted(Memory.from_json(json.loads(store.read_text())).waiting)


def stalled_message(tab_id: str, minutes: int) -> str:
    return (f"Tab {tab_id} has been working for {minutes} minutes with nothing in its "
            "conversation moving; the manager is looking at it.")


def spinning_message(tab_id: str, minutes: int) -> str:
    return (f"Tab {tab_id}'s conversation has moved for {minutes} minutes without anything in "
            "its working copy changing; the manager is looking at it.")


def unstuck_message(note: str) -> str:
    return ("Your last turn was stopped by the manager tab: it had gone on without getting "
            "anything done. Every background task in your session was stopped with it. "
            f"The manager says:\n\n{note}")


class Stalls:
    """Subscribed at construction, for the account's limit."""

    def __init__(self, session: "Session", events: EventLog) -> None:
        self.session = session
        self.events = events
        self._sub = events.subscribe()
        self._watches: dict[str, Watch] = {}
        self._unread: dict[str, str] = {}      # tab -> why its working copy was last unreadable
        self._limited = False
        self._next_tick = 0.0

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            for event in self._sub.drain(timeout=1.0):
                self.on_event(event)
            now = time.time()
            if now >= self._next_tick:
                self._next_tick = now + TICK_SECONDS
                self.tick(now)

    def on_event(self, event: Event) -> None:
        if event.type == "account.limited":
            self._limited = True
        elif event.type == "account.resumed":
            # Every clock stood still with the account: they start again from now.
            self._limited = False
            self._watches.clear()
        elif event.type == "agent.restarted" and event.tab is not None:
            self._watches.pop(event.tab, None)
        elif event.type in ("job.started", "job.progressed") and event.tab in self._watches:
            # A job the tab runs printing is its work moving, whatever its working copy does.
            watch = self._watches[event.tab]
            watch.changed_at, watch.spinning = event.ts, False

    def tick(self, now: float) -> None:
        if self._limited:
            return
        tabs = dict(self.session.intent.tabs)
        for tab_id in list(self._watches):
            tab = tabs.get(tab_id)
            if tab is None or tab.status != "running" or not tab.busy:
                del self._watches[tab_id]
        for tab_id, tab in tabs.items():
            if tab.status == "running" and tab.busy:
                self._read(tab_id, tab.body, now)

    def _read(self, tab_id: str, body: str | None, now: float) -> None:
        home = self.session.agents.home(tab_id)
        transcript = latest_transcript(home)
        size = None if transcript is None else transcript.stat().st_size
        tree = self._tree(tab_id) if body is not None else None
        watch = self._watches.get(tab_id)
        if watch is None:
            self._watches[tab_id] = Watch(size, now, tree, now)
            return
        if size != watch.size:
            watch.size, watch.moved_at, watch.stalled = size, now, False
        if tree != watch.tree:
            watch.tree, watch.changed_at, watch.spinning = tree, now, False
        if not watch.stalled and now - watch.moved_at >= STALL_SECONDS:
            watch.stalled = True
            message = stalled_message(tab_id, int((now - watch.moved_at) // 60))
            evidence = self._evidence(body, tab_id, home, watch.moved_at)
            self.events.emit("tab.stalled", tab=tab_id, message=message, **evidence)
            self._unstick_manager(tab_id, message, evidence)
        elif (not watch.spinning and watch.tree is not None
              and now - watch.moved_at < STALL_SECONDS
              and now - watch.changed_at >= SPIN_SECONDS):
            watch.spinning = True
            message = spinning_message(tab_id, int((now - watch.changed_at) // 60))
            evidence = self._evidence(body, tab_id, home, watch.changed_at)
            self.events.emit("tab.spinning", tab=tab_id, message=message, **evidence)
            self._unstick_manager(tab_id, message, evidence)

    def _tree(self, tab_id: str) -> tuple | None:
        """Unreadable — a body no longer defined, a git that fails — is reported as unasked:
        the transcript is still read, and the reason is said once per reading."""
        try:
            repo = self.session.working_copy(tab_id)
            reading = None if repo is None else working_copy_reading(repo)
        except (SessionError, git.GitError) as exc:
            if self._unread.get(tab_id) != str(exc):
                self._unread[tab_id] = str(exc)
                self.events.emit("stalls.unread", tab=tab_id, error=str(exc))
            return None
        self._unread.pop(tab_id, None)
        return reading

    def _evidence(self, body: str | None, tab_id: str, home: Path,
                  since: float) -> dict[str, Any]:
        """What the manager judges by, read-only: what the tab last called and waits on, and
        what runs in its agent's container and its sandbox's toolbelt, where those calls ran."""
        sandbox = None if body is None else naming.instance_id(body, tab_id)
        containers = [naming.agent(tab_id)] + ([] if sandbox is None else [naming.view(sandbox)])
        processes = {}
        for container in containers:
            try:
                processes[container] = self.session.runtime.processes(container)
            except RuntimeError_ as exc:
                # The refusal is the answer: a toolbelt that is down runs nothing.
                processes[container] = f"(not listed: {exc})"
        return {"since": since, "sandbox": sandbox, "waiting_on": waited_on(home),
                "last_calls": last_calls(home), "processes": processes}

    def _unstick_manager(self, tab_id: str, message: str, evidence: dict[str, Any]) -> None:
        """Nobody takes the manager's own: it is unstuck here, told what it was doing."""
        if tab_id != MANAGER:
            return
        calls = "\n".join(f"- {c['tool']} {c['input']} -> {c['result']}"
                          for c in evidence["last_calls"])
        try:
            unstick(self.session, self.events, MANAGER,
                    f"{message}\nIts last calls:\n{calls}\nStart the failure you were on "
                    "again from its incident doc.")
        except Exception as exc:                       # noqa: BLE001
            # Said rather than raised: this thread ending would end the daemon.
            self.events.emit("stalls.unstick_failed", tab=MANAGER,
                             error=f"{type(exc).__name__}: {exc}")




def unstick(session: "Session", events: EventLog, tab_id: str, note: str) -> dict[str, Any]:
    """The manager's repair: end a stuck tab's turn and every background task in its
    container, and start its next turn on `note` — what had stopped, and what to do instead."""
    if not note.strip():
        raise SessionError("unstick says what had stopped and what to do instead")
    session.restart_agent(tab_id, resume=True)
    events.emit("tab.unstuck", tab=tab_id, deliver={
        "content": unstuck_message(note), "meta": {"from": "manager"}})
    return {"tab": tab_id, "status": "restarted", "note": "queued for its next session"}
