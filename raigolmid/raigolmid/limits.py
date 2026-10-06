"""Turns an API error cut off, resumed without the user.

Claude Code ends a turn an API error cut off with `StopFailure` rather than `Stop` (`rai
agent-activity failed`), so the tab is idle with the error on `agent.idle`. The tabs nobody
sits in front of — the machine tab, the janitor, and every tab the machine tab manages — are
told to continue once what cut them off is over:

- **the usage limit** (`rate_limit`) is the account's, not the tab's, since every tab spends the
  one credential. The proxy reads when it resets from the 429 that said so
  (`credproxy.limited`); until then `account.limited` holds every channel's pushes, which
  would only fail (`channel.py`), and at the reset each cut tab is told to continue. A limit
  whose reset nobody said — a 429 without the header, or a daemon started during one — is
  probed: one cut tab is told to continue every `PROBE_SECONDS`, and the first turn that ends
  without an error is the account answering again, which resumes the rest.
- **a transient failure** (`TRANSIENT`) is the tab's own, resumed after `RETRY_SECONDS`.
- **any other** (a credential, billing, a model or request refused) fails the same way until
  the user acts, so it is said to them (`agent.turn_failed`) and not resumed.

A cut tab is one whose last report was the failure (`activity.Activity.failed`), which is how a
daemon that starts finds the ones cut off while it was down.
"""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from . import activity
from .events import Event, EventLog

if TYPE_CHECKING:
    from .session import Session

LIMIT = "rate_limit"
TRANSIENT = frozenset({"overloaded", "server_error", "unknown", "max_output_tokens"})
# Claude Code has already retried a transient error before giving the turn up, so these only
# set how soon a cut tab is tried again; none decides whether it is.
RETRY_SECONDS = 120.0
PROBE_SECONDS = 600.0
# The reset the header names is when the window opens; a request in its first moments can
# still be refused, and would be probed again.
GRACE_SECONDS = 30.0
# The account's longest usage window is a week.
LONGEST_WINDOW = 8 * 24 * 3600.0


def continue_message(error: str) -> str:
    cause = "the account's usage limit" if error == LIMIT else f"an API error ({error})"
    return (f"Your last turn was cut off by {cause}, which is over. Continue the work from "
            "where it stopped.")


class Limits:
    """Subscribed at construction; the cut tabs a stopped daemon missed are read in `run`."""

    def __init__(self, session: "Session", events: EventLog) -> None:
        self.session = session
        self.events = events
        self._sub = events.subscribe()
        self._cut: dict[str, tuple[str, float]] = {}    # tab -> (error, when)
        # Whether the account is limited, and when it resets (None: nobody said).
        self._limited = False
        self._resets_at: float | None = None
        self._probe_at = 0.0

    def run(self, stop: threading.Event) -> None:
        self._read_cut()
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
        elif event.tab is None:
            return
        elif event.type in ("agent.busy", "tab.closed"):
            self._cut.pop(event.tab, None)
        elif event.type == "agent.idle":
            error = event.data.get("error")
            if error is None:
                self._cut.pop(event.tab, None)
                if self._limited and self._resets_at is None:
                    # A turn the API answered: the limit nobody timed is over.
                    self._resume_limited(now)
                return
            self._cut[event.tab] = (error, now)
            if error == LIMIT:
                self._limit(None, now)
            elif error not in TRANSIENT:
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
        if resets_at is None:
            self._probe_at = now + PROBE_SECONDS
        if first or resets_at is not None:
            self.events.emit("account.limited", resets_at=resets_at, hold_until=(
                None if resets_at is None else resets_at + GRACE_SECONDS))

    def tick(self, now: float) -> None:
        if self._limited:
            if self._resets_at is not None:
                if now >= self._resets_at + GRACE_SECONDS:
                    self._resume_limited(now)
            elif now >= self._probe_at:
                self._probe(now)
        for tab_id, (error, when) in list(self._cut.items()):
            if error in TRANSIENT and now >= when + RETRY_SECONDS:
                self._continue(tab_id, error)

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
        self._cut.pop(tab_id)
        self.events.emit("limits.continued", tab=tab_id, error=error, deliver={
            "content": continue_message(error), "meta": {"from": "daemon"}})
