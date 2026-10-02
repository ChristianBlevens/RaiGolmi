"""The history the menu at the top centre shows the user: what the agents
and the machine did, so they can understand them.

An entry is written for each event `SAYS` names, or `NOTICED` makes a notice of, in the order
they came; the menu shows them newest first. Each says its tab, when, and what happened, in
words fixed when it is written. A **question or permission** is the one exception: its entry
holds only the item's id, because how it stands moves (pending, then settled, overturned), and
`questions.py` is the one authority on that — whoever serves the history reads the item there
(`api.with_items`). Its entry is written when the item is first the user's to see: a permission
when it is asked, a question when the judge refers it to them or, never referred, when it settles.

A **notice** is a failure that is the user's (`NOTICED`). It stands until a later event shows the
failure over (`OVER`), and then settles itself. Nothing in the history asks for anything.

**The handle is lit by anything new**: an entry after the newest one the open menu has shown
the user (`seen`); ids only grow, so that one id is the whole of what they have seen. An entry
that is the visible result of their own action is **born seen** and never lights it, since a
mark says only what they may not know: a tab opened by their selection or close, and their
close, which name them (`by: "user"`); a tab done while they are viewing it
(`terminal.viewing`); and a question they answered by typing before it was ever put to them.

The entries and `seen` are written to `Paths.history` on every change, so they outlive a
daemon restart; an entry older than the user's settings' `kept_days` is deleted. Events this
subscriber dropped are read back from the event log, which writes each before offering it, so
none is missing.
"""
from __future__ import annotations

import itertools
import threading
import time
from dataclasses import asdict, dataclass
from typing import Callable

from . import settings
from .events import Event, EventLog
from .intent import BrokenState, load_json, save_json
from .paths import Paths


class HistoryError(Exception):
    """A `seen` that names no entry."""


def _closed(e: Event) -> str:
    if e.data.get("reason") == "continued":
        return (f"its work handed on to {e.data['continued_by']} by the {_BY[e.data['by']]}; "
                "this conversation and its thought doc archived")
    if "reason" in e.data:
        return f"closed: {e.data['reason']}"
    who = {"user": "closed by you"}[e.data["by"]]
    # No archive: the tab had no home to keep; one that could not be kept says so itself
    # (`agent.home_archive_failed`).
    return who if e.data["archive"] is None else f"{who}; its conversation archived"


def _rebuilt(e: Event) -> str | None:
    report, why = e.data["report"], e.data["why"]
    result = report["result"]
    if result == "already_current":
        return None
    said = {"rebuilt": "rebuilt", "build_failed": "not rebuilt: its build failed"}.get(
        result, f"not rebuilt: {result}")
    reason = f" ({report['reason']})" if report.get("reason") else ""
    return f"{e.instance or report['instance']} {said}{reason} — {why}"


def _build_failed(e: Event) -> str:
    last = next((line for line in reversed(e.data["log"].splitlines()) if line.strip()), "")
    return f"the build of body {e.data['body']} failed" + (f": {last}" if last else "")




def _clock(t: float) -> str:
    return time.strftime("%H:%M", time.localtime(t))


_BY = {"machine": "machine tab", "daemon": "daemon"}


def _opened(e: Event) -> str:
    said = f"opened for {e.data['body']}" if e.data.get("body") else "opened as the machine tab"
    if e.data.get("continues"):
        said += (f", continuing {e.data['continues']}'s work in a fresh conversation from "
                 "its SESSION-START.md")
    return said


def _handed(e: Event) -> str:
    said = "handed to the machine tab to manage"
    if e.data.get("stop_when"):
        said += f", stopping for you at {e.data['stop_when']!r}"
    if e.data.get("until"):
        said += f", until {_clock(e.data['until'])}"
    return said


# What each event this records says. None: this one is not recorded.
SAYS: dict[str, Callable[[Event], str | None]] = {
    "tab.opened": _opened,
    "tab.closed": _closed,
    "agent.idle": lambda e: "done" if e.data["done"] else None,
    "agent.crashed": lambda e: e.data["message"],
    "tab.stalled": lambda e: e.data["message"],
    "tab.spinning": lambda e: e.data["message"],
    "tab.unstuck": lambda e: "its turn stopped by the manager, which said why",
    "tab.managed": lambda e: (None if e.data.get("why") == "continued"
                              else _handed(e) if e.data["on"]
                              else "given back by the daemon: your time for it ran out"
                              if e.data.get("why") == "time"
                              else "taken back from the machine tab"),
    "tab.held": lambda e: f"held for you: {e.data['situation']}",
    "run.ended": lambda e: (f"the machine tab's run from {_clock(e.data['started'])} ended: "
                            "every tab is given back, and it is asked for its report"),
    "run.checkpoint": lambda e: (f"its progress report on the run from "
                                 f"{_clock(e.data['started'])}, stretch {e.data['stretch']}, "
                                 "is in the catalog, under Documents, Runs"),
    "run.reported": lambda e: (f"its report on the run from {_clock(e.data['started'])} to "
                               f"{_clock(e.data['ended'])} is in the catalog, under Documents, "
                               "Runs"),
    "agent.restarted": lambda e: ("restarted, resuming its conversation" if e.data["resumed"]
                                  else "restarted"),
    "agent.home_archive_failed": lambda e: (f"its conversation could not be archived: "
                                            f"{e.data['reason']}"),
    "sandbox.opened": lambda e: f"opened {e.instance} with toolbelt {e.data['toolbelt']}",
    "toolbelt.swapped": lambda e: f"swapped {e.instance}'s toolbelt to {e.data['toolbelt']}",
    "rebuild.finished": _rebuilt,
    "rebuild.refused": lambda e: f"{e.instance} not rebuilt ({e.data['why']}): {e.data['error']}",
    "build.failed": _build_failed,
    "permission.failed": lambda e: e.data["deliver"]["content"],
    "judge.learned": lambda e: (f"your answer to {e.data['id']} learned into your preferences"
                                if e.data["changed"] else
                                f"your answer to {e.data['id']}: your preferences already "
                                "said so"),
    # A failure this records itself is said once, not again for being handed on.
    "manager.queued": lambda e: (None if e.data["failure"] in SAYS.keys() | NOTICED.keys() else
                                 f"handed {e.data['failure']}. {e.data['incident']}"),
    "manager.incident_fixed": lambda e: f"fixed: {e.data['incident']}",
    "body.base_local": lambda e: (f"body {e.data['body']} used the machine's copy of "
                                  f"{e.data['image']}: the registry did not answer "
                                  f"({e.data['reason']})"),
    "clipboard.unbridged": lambda e: (f"face {e.data['face']}'s clipboard is its own again, not "
                                      f"the machine's: {e.data['reason']}"),
    "clipboard.copy_failed": lambda e: (f"a copy did not reach the {e.data['to']} clipboard: "
                                        f"{e.data['reason']}"),
    "documents.maintenance": lambda e: e.data["message"],
    "account.limited": lambda e: (
        "the account's usage limit was reached; "
        + ("its reset was not said, so a tab is tried again every few minutes"
           if e.data["hold_until"] is None else "the tabs it cut off continue at "
           + time.strftime("%H:%M", time.localtime(e.data["hold_until"])))),
    "account.resumed": lambda e: "the account's usage limit is over",
    "documents.maintenance_failed": lambda e: (f"document maintenance could not run: "
                                               f"{e.data['error']}"),
}

# The failures that are the user's, and what the notice says for each.
NOTICED: dict[str, Callable[[Event], str]] = {
    "hostkeys.failed": lambda e: (f"Your host keys in settings.toml were not applied: "
                                  f"{e.data['error']}"),
    "keyboard.failed": lambda e: (f"Your keyboard or display settings were not applied: "
                                  f"{e.data['error']}"),
    "look.failed": lambda e: f"Your look settings were not applied: {e.data['error']}",
    "manager.unfixable": lambda e: e.data["message"],
    "channel.unheard": lambda e: e.data["message"],
    "coordinator.unanswered": lambda e: e.data["message"],
    "manager.open_failed": lambda e: f"The manager tab could not open: {e.data['error']}",
    "agent.turn_failed": lambda e: e.data["message"],
    "stalls.unstick_failed": lambda e: (f"The manager tab was stuck and could not be "
                                        f"restarted: {e.data['error']}"),
    "claude_login.lost": lambda e: (
        f"Your claude.ai sign-in has ended ({e.data['error']}), so a held tab cannot reach your "
        f"phone until you sign in again, in the AI terminal's first window"),
    "claude_login.refresh_failed": lambda e: (
        f"Your claude.ai sign-in could not be renewed, so a held tab cannot reach your phone: "
        f"{e.data['error']}"),
    "judge.failed": lambda e: (
        f"The preferences judge failed on {e.data['id']}"
        + (", so it is put to you" if e.data["stage"] == "judge" else
           "; your answer was sent without the preferences learning it")
        + f": {e.data['error']}"),
}

# A notice's failure that a later event shows is over: the manager that
# could not open has opened; a setting the compositor refused has since been applied.
OVER = {"manager.opened": frozenset({"manager.open_failed"}),
        "hostkeys.applied": frozenset({"hostkeys.failed"}),
        "keyboard.applied": frozenset({"keyboard.failed"}),
        "look.applied": frozenset({"look.failed"}),
        "credential.stored": frozenset({"agent.turn_failed"}),
        "claude_login.refreshed": frozenset({"claude_login.refresh_failed"}),
        "claude_login.stored": frozenset({"claude_login.refresh_failed", "claude_login.lost"})}

# The user's own actions, when the event names them.
BY_THE_USER = frozenset({"tab.opened", "tab.closed"})

# The events that settle a question or permission.
SETTLES = frozenset({"question.answered", "question.lapsed", "question.withdrawn"})


@dataclass
class Entry:
    id: str
    at: float
    kind: str                       # the event it records
    tab: str | None
    text: str | None = None         # None for a question's entry, read from `questions.py`
    question: str | None = None     # the question or permission it shows
    notice: bool = False
    over: str | None = None         # the event that showed a notice's failure over
    born_seen: bool = False


def _number(id: str) -> int:
    return int(id[1:])


class History:
    """Subscribed at construction, like the manager, so a failure of the daemon's own start
    is in it."""

    def __init__(self, events: EventLog, paths: Paths) -> None:
        self.events = events
        self.paths = paths
        self.store = paths.history
        self._sub = events.subscribe()
        self._lock = threading.Lock()
        self._entries, self._seen = self._load()
        last = max((_number(i) for i in self._entries), default=0)
        self._ids = itertools.count(last + 1)
        self._questions = {e.question for e in self._entries.values() if e.question}
        self._viewing: str | None = None
        # The newest event heard, where a replay starts: nothing before it subscribed is its.
        self._last = time.time()

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            batch = self._sub.drain(timeout=1.0)
            if self._sub.dropped:
                self._sub.dropped = 0
                self._replay(before=batch[0].ts if batch else None)
            for event in batch:
                self.on_event(event)

    def _replay(self, before: float | None) -> None:
        """The events dropped from the queue, which are the oldest: those after the last one
        heard and before the first still queued."""
        missed = [e for e in self.events.read()
                  if e.ts > self._last and (before is None or e.ts < before)]
        for event in missed:
            self.on_event(event)
        self.events.emit("history.replayed", count=len(missed))

    def on_event(self, event: Event) -> None:
        self._last = max(self._last, event.ts)
        if event.type == "terminal.viewing":
            self._viewing = event.tab
        elif event.type in NOTICED:
            self._add(event, NOTICED[event.type](event), notice=True)
        elif event.type in OVER:
            self._over(event)
        elif event.type == "question.asked" and event.data["kind"] == "permission":
            self._add_question(event)
        elif event.type == "question.referred" or event.type in SETTLES:
            self._add_question(event, born_seen=event.data.get("by") == "terminal")
        elif event.type in SAYS:
            text = SAYS[event.type](event)
            if text is not None:
                self._add(event, text, born_seen=self._by_the_user(event))

    def entries(self) -> list[dict]:
        """Every entry, oldest first, each `new` until the menu has shown it to the user
        (`seen`)."""
        with self._lock:
            return [{**asdict(e), "new": _number(e.id) > self._seen and not e.born_seen}
                    for e in sorted(self._entries.values(), key=lambda e: _number(e.id))]

    def seen(self, through: str) -> None:
        """The open menu has shown the user every entry up to `through`, its newest: none of
        them lights the handle any more. One that arrived after the menu drew is not seen."""
        with self._lock:
            if through not in self._entries:
                raise HistoryError(f"no history entry {through}")
            if _number(through) <= self._seen:
                return
            self._seen = _number(through)
            self._save()
        self.events.emit("history.seen", through=through)

    # --- internals --------------------------------------------------------------------
    def _by_the_user(self, event: Event) -> bool:
        if event.type in BY_THE_USER:
            return event.data.get("by") == "user"
        return event.type == "agent.idle" and event.tab == self._viewing

    def _add_question(self, event: Event, born_seen: bool = False) -> None:
        id = event.data["id"]
        with self._lock:
            if id in self._questions:
                return
            self._questions.add(id)
        self._add(event, None, question=id, born_seen=born_seen)

    def _add(self, event: Event, text: str | None, **fields) -> None:
        with self._lock:
            entry = Entry(id=f"h{next(self._ids)}", at=event.ts, kind=event.type,
                          tab=event.tab, text=text, **fields)
            self._entries[entry.id] = entry
            old = time.time() - settings.load(self.paths.settings).kept_seconds
            for id in [id for id, e in self._entries.items() if e.at < old]:
                del self._entries[id]
            self._save()
        self.events.emit("history.added", id=entry.id, new=not entry.born_seen)

    def _over(self, event: Event) -> None:
        with self._lock:
            over = [e for e in self._entries.values()
                    if e.notice and e.over is None and e.kind in OVER[event.type]]
            for entry in over:
                entry.over = event.type
            if over:
                self._save()
        for entry in over:
            self.events.emit("history.over", id=entry.id, by=event.type)

    def _save(self) -> None:
        """Under the lock."""
        save_json(self.store, {"entries": [asdict(e) for e in self._entries.values()],
                               "seen": self._seen})

    def _load(self) -> tuple[dict[str, Entry], int]:
        raw = load_json(self.store, "history")
        if raw is None:
            return {}, 0
        try:
            entries = {d["id"]: Entry(**d) for d in raw["entries"]}
            seen = int(raw["seen"])
        except (TypeError, KeyError, ValueError) as exc:
            raise BrokenState(self.store, "history", exc) from exc
        return entries, seen
