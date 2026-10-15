"""Turns an API error cut off, resumed without the user.

Claude Code ends a turn an API error cut off with `StopFailure` rather than `Stop` (`rai
agent-activity failed`), so the tab is idle with the error on `agent.idle`. The tabs nobody
sits in front of — the machine tab, the janitor, and every tab the machine tab manages — are
told to continue once what cut them off is over:

- **the usage limit** (`rate_limit`) is the account's, not the tab's, since every tab spends the
  one credential. The proxy reads when it resets from the 429 that said so
  (`credproxy.limited`); until then `account.limited` holds every channel's pushes, which
  would only fail (`channel.py`), and at the reset each cut tab is told to continue. The reset
  is the latest the limit can end, not the only way: an upgraded plan or a lifted limit ends it
  early. So while it holds, one cut tab is told to continue every `PROBE_SECONDS`, and the first
  answer the API gives as allowed — a turn that ends without an error, or `credproxy.plan`
  reading `allowed` — is the account answering again, which resumes the rest.

**The plan's usage** is every API answer's (`credproxy.plan`): kept in `Paths.plan` and read by
`status` through `plan_view`, which is how a tab paces its work to what the plan has left.
- **a transient failure** (`TRANSIENT`) is the tab's own, resumed after `RETRY_SECONDS`, and
  each that follows it with no clean turn between after twice as long, up to `RETRY_CAP_SECONDS`.
  Claude Code 2.1.295 reports a connection that fails, times out, is refused or reset, a host
  name that does not resolve, a 5xx, repeated 529s and the proxy's 502 for an upstream it cannot
  reach as `server_error`, and an HTTP status or error it does not classify as `unknown`: the
  network going down is one of these, and a run outlives it.
- **any other** (a credential, billing, a model or request refused) fails the same way until
  the user acts, so it is said to them (`agent.turn_failed`) and not resumed — except a refusal
  while the claude.ai sign-in is set, which its renewal fixes (`remotecontrol.py`).

A cut tab is one whose last report was the failure (`activity.Activity.failed`), which is how a
daemon that starts finds the ones cut off while it was down. It is read as this is constructed,
before the start reopens any tab: a reopened session's start clears it.

**A turn its session ended is continued too** (`agent.reopened` with `mid_turn`, `Session._reopen`):
a reboot, or an agent that crashed, leaves the reopened conversation idle where the turn stopped.
Not a tab held for the user, which is theirs, nor the machine tab while it manages a run, which
the coordinator tells of the restart (`coordinator.py`).
"""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any

from . import activity, claude_login
from .events import Event, EventLog
from .intent import save_json

if TYPE_CHECKING:
    from .session import Session

LIMIT = "rate_limit"
TRANSIENT = frozenset({"overloaded", "server_error", "unknown", "max_output_tokens"})
# Claude Code has already retried a transient error before giving the turn up, so these only
# set how soon a cut tab is tried again; none decides whether it is.
RETRY_SECONDS = 120.0
# A failure that repeats is an outage, which can last hours: each retry of one waits twice the
# last, up to this, so a long outage costs a turn per half hour rather than one per two minutes.
RETRY_CAP_SECONDS = 1800.0
PROBE_SECONDS = 600.0
# The reset the header names is when the window opens; a request in its first moments can
# still be refused, and would be probed again.
GRACE_SECONDS = 30.0
# The account's longest usage window is a week.
LONGEST_WINDOW = 8 * 24 * 3600.0


# An answer reporting one of these is the API serving the account (`credproxy.PLAN_PREFIX`).
ALLOWED = frozenset({"allowed", "allowed_warning"})
_UNIT_SECONDS = {"h": 3600.0, "d": 24 * 3600.0}


def plan_view(reading: dict[str, Any] | None, now: float) -> dict[str, Any] | None:
    """The plan's usage as a tab reads it: for each window, the share used, when it resets,
    the share of the window passed, and `on_course_for` — the share it ends on if it goes on
    being used as it has been. Over 1 the limit cuts the work off before the reset; under 1
    part of the allowance goes unused. A window past its reset is said to be, its use unknown."""
    if reading is None:
        return None
    windows = {}
    for name, window in sorted(reading["windows"].items()):
        length = float(name[:-1]) * _UNIT_SECONDS[name[-1]]
        resets_at = window["resets_at"]
        if now >= resets_at:
            windows[name] = {"resets_at": resets_at, "reset_since_read": True}
            continue
        passed = 1.0 - (resets_at - now) / length
        windows[name] = {"used": window["used"], "resets_at": resets_at,
                         "passed": round(passed, 3),
                         "on_course_for": round(window["used"] / passed, 2) if passed > 0 else None}
    return {"status": reading["status"], "read_at": reading["read_at"], "windows": windows}


# What `continue_message` names for a turn whose session ended under it.
REOPENED = "reopened"


def continue_message(error: str) -> str:
    if error == REOPENED:
        return ("Your last turn was cut off: your session ended under it (the machine "
                "restarted, or your agent stopped) and was reopened on this conversation. A tool "
                "call it was in may not have finished. Continue the work from where it stopped.")
    cause = "the account's usage limit" if error == LIMIT else f"an API error ({error})"
    return (f"Your last turn was cut off by {cause}, which is over. Continue the work from "
            "where it stopped.")


class Limits:
    """Subscribed, and the cut tabs a stopped daemon missed read, at construction."""

    def __init__(self, session: "Session", events: EventLog) -> None:
        self.session = session
        self.events = events
        self._sub = events.subscribe()
        self._cut: dict[str, tuple[str, float]] = {}    # tab -> (error, when)
        # Transient failures in a row, by tab: a turn that ends clean, or the tab closing, ends
        # the row; a resumed turn starting does not.
        self._failures: dict[str, int] = {}
        # Whether the account is limited, and when it resets (None: nobody said).
        self._limited = False
        self._resets_at: float | None = None
        self._probe_at = 0.0
        self._read_cut()

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            for event in self._sub.drain(timeout=1.0):
                self.on_event(event)
            self.tick(time.time())

    def _read_cut(self) -> None:
        now = time.time()
        for tab_id in list(self.session.intent.tabs):
            failed = activity.read(self.session.agents.home(tab_id)).failed
            if failed:
                self._cut[tab_id] = (failed, now)
                if failed == LIMIT:
                    self._limit(None, now)

    def on_event(self, event: Event) -> None:
        now = time.time()
        if event.type == "credproxy.limited" and event.data["resets_at"] is not None:
            self._limit(event.data["resets_at"], now)
        elif event.type == "credproxy.plan":
            save_json(self.session.paths.plan, {**event.data, "read_at": now})
            if self._limited and event.data["status"] in ALLOWED:
                self._resume_limited(now)
        elif event.tab is None:
            return
        elif event.type == "agent.reopened":
            tab = self.session.intent.tabs.get(event.tab)
            if (event.data["mid_turn"] and tab is not None and tab.held is None
                    and not (tab.machine and self.session.intent.hands_off(tab.tab_id))):
                self._continue(event.tab, REOPENED)
        elif event.type == "agent.busy":
            self._cut.pop(event.tab, None)
        elif event.type == "tab.closed":
            self._cut.pop(event.tab, None)
            self._failures.pop(event.tab, None)
        elif event.type == "agent.idle":
            error = event.data.get("error")
            if error is None:
                self._cut.pop(event.tab, None)
                self._failures.pop(event.tab, None)
                if self._limited:
                    # A turn the API answered: the limit is over, whenever it said it would be.
                    self._resume_limited(now)
                return
            self._cut[event.tab] = (error, now)
            if error in TRANSIENT:
                self._failures[event.tab] = self._failures.get(event.tab, 0) + 1
            if error == LIMIT:
                self._limit(None, now)
            elif error not in TRANSIENT and not (
                    error == claude_login.TURN_REFUSED
                    and claude_login.is_set(self.session.paths.claude_login)):
                self.events.emit("agent.turn_failed", tab=event.tab, error=error,
                                 message=f"Tab {event.tab}'s turn ended on an API error "
                                         f"({error}) that continuing will not fix; it waits "
                                         "for you.")

    def _limit(self, resets_at: float | None, now: float) -> None:
        if resets_at is not None and not now < resets_at <= now + LONGEST_WINDOW:
            resets_at = None        # not a reset the account's windows can have: probed
        if self._limited and (resets_at is None or resets_at == self._resets_at):
            return
        first = not self._limited
        self._limited, self._resets_at = True, resets_at
        if first:
            self._probe_at = now + PROBE_SECONDS
        if first or resets_at is not None:
            self.events.emit("account.limited", resets_at=resets_at, hold_until=(
                None if resets_at is None else resets_at + GRACE_SECONDS))

    def tick(self, now: float) -> None:
        if self._limited:
            if self._resets_at is not None and now >= self._resets_at + GRACE_SECONDS:
                self._resume_limited(now)
            elif now >= self._probe_at:
                self._probe(now)
        for tab_id, (error, when) in list(self._cut.items()):
            if error in TRANSIENT and now >= when + self.retry_after(tab_id):
                self._continue(tab_id, error)

    def retry_after(self, tab_id: str) -> float:
        """How long a tab cut by a transient failure waits: doubled by each in a row."""
        row = self._failures.get(tab_id, 1)
        return min(RETRY_SECONDS * 2 ** (row - 1), RETRY_CAP_SECONDS)

    def _resume_limited(self, now: float) -> None:
        self._limited, self._resets_at = False, None
        self.events.emit("account.resumed")
        for tab_id, (error, _) in list(self._cut.items()):
            if error == LIMIT:
                self._continue(tab_id, error)

    def _probe(self, now: float) -> None:
        self._probe_at = now + PROBE_SECONDS
        tabs = [t for t, (e, _) in self._cut.items() if e == LIMIT and self._unattended(t)]
        machine = self.session.intent.machine_tab()
        if machine is not None and machine.tab_id in tabs:
            tabs.insert(0, machine.tab_id)
        if not tabs:
            return
        self.events.emit("account.probed", tab=tabs[0])
        self._continue(tabs[0], LIMIT)

    def _unattended(self, tab_id: str) -> bool:
        tab = self.session.intent.tabs.get(tab_id)
        return tab is not None and (tab.machine or tab.janitor or tab.managed)

    def _continue(self, tab_id: str, error: str) -> None:
        """A tab the user sits in front of is theirs to resume; it stays cut until it works."""
        if not self._unattended(tab_id):
            return
        self._cut.pop(tab_id, None)
        self.events.emit("limits.continued", tab=tab_id, error=error, deliver={
            "content": continue_message(error),
            "meta": {"from": "daemon", "standing": "continue"}})
