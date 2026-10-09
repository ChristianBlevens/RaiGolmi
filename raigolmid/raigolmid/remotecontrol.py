"""Every tab is on Remote Control while the user's claude.ai sign-in is set, and on the agent
credential while it is not.

A tab takes the sign-in when its container starts (`Agents.start`), so a change in the sign-in
reaches a running tab only by restarting it, resumed:

- **given** (`claude_login.stored`, or a daemon that starts with it): each idle tab started
  without it is restarted onto it, and each busy one once its turn ends (`agent.idle`).
- **lost** (`claude_login.lost`): the proxy refuses every request a tab makes on it, so every
  tab is restarted onto the agent credential at once, and one whose turn that cut short is
  told to continue.
- **refused** (a turn ended on `authentication_failed` while it is set): Claude Code empties the
  tab's own copy, so the tab cannot recover by itself, and every tab is refused alike — the
  machine tab and the janitor included, so no agent is left to restart the rest. The refusal
  is said (`claude_login.refused`, which renews it at once), and at the renewal
  (`claude_login.refreshed`) each refused tab is restarted onto it and told to continue. A tab
  refused again on that renewal is not the sign-in's to fix, and waits for the user
  (`agent.turn_failed`). A daemon that starts takes the tabs whose last turn was refused the
  same way.

Whether a tab is on it is what `Agents.start` wrote into its home (`Agents.signed_in`).
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from . import activity, claude_login
from .events import Event, EventLog

if TYPE_CHECKING:
    from .session import Session


def renewed_message() -> str:
    return ("Your last turn was cut off when the API refused the user's claude.ai sign-in; it "
            "is renewed and you are restarted on it. Continue the work from where it stopped.")


def continue_message() -> str:
    return ("Your last turn was cut off when the user's claude.ai sign-in ended; you are "
            "restarted on the machine's agent credential. Continue the work from where it "
            "stopped.")


class RemoteControl:
    """Subscribed at construction; tabs a stopped daemon left behind the sign-in are read in
    `run`."""

    def __init__(self, session: "Session", events: EventLog) -> None:
        self.session = session
        self.events = events
        self._sub = events.subscribe()
        # Tabs refused on the sign-in, waiting for its renewal; and tabs restarted on a
        # renewal that have not yet ended a turn on it.
        self._refused: set[str] = set()
        self._renewed: set[str] = set()

    def run(self, stop: threading.Event) -> None:
        self._sign_in_idle()
        self._read_refused()
        while not stop.is_set():
            for event in self._sub.drain(timeout=1.0):
                self.on_event(event)

    def on_event(self, event: Event) -> None:
        if event.type == "claude_login.stored":
            self._sign_in_idle()
        elif event.type == "claude_login.refreshed":
            self._restart_refused()
        elif event.type == "claude_login.lost":
            self._refused.clear()
            self._renewed.clear()
            self._sign_out_all()
        elif event.type == "tab.closed" and event.tab is not None:
            self._refused.discard(event.tab)
            self._renewed.discard(event.tab)
        elif event.type == "agent.idle" and event.tab is not None:
            tab = self.session.intent.tabs.get(event.tab)
            if tab is None:
                return
            login = self._login_set()
            if event.data.get("error") == claude_login.TURN_REFUSED and login:
                self._was_refused(tab.tab_id)
            else:
                self._renewed.discard(tab.tab_id)
                if self._behind(tab.tab_id, login):
                    self._restart(tab.tab_id, cut=False)

    def _was_refused(self, tab_id: str) -> None:
        if tab_id in self._renewed:
            self._renewed.discard(tab_id)
            self.events.emit("agent.turn_failed", tab=tab_id,
                             error=claude_login.TURN_REFUSED,
                             message=f"Tab {tab_id} was refused again on a renewed claude.ai "
                                     "sign-in, which renewing will not fix; it waits for you.")
            return
        self._refused.add(tab_id)
        self.events.emit("claude_login.refused", tab=tab_id)

    def _read_refused(self) -> None:
        if not self._login_set():
            return
        for tab in self._running():
            failed = activity.read(self.session.agents.home(tab.tab_id)).failed
            if failed == claude_login.TURN_REFUSED:
                self._was_refused(tab.tab_id)

    def _restart_refused(self) -> None:
        refused, self._refused = self._refused, set()
        for tab_id in sorted(refused):
            if tab_id in self.session.intent.tabs and self._restart(
                    tab_id, cut=False):
                self._renewed.add(tab_id)
                self.events.emit("remote_control.continued", tab=tab_id, deliver={
                    "content": renewed_message(), "meta": {"from": "daemon"}})

    def _login_set(self) -> bool:
        return claude_login.is_set(self.session.paths.claude_login)

    def _running(self):
        return [tab for tab in list(self.session.intent.tabs.values())
                if tab.status == "running"]

    def _behind(self, tab_id: str, login: bool) -> bool:
        return self.session.agents.signed_in(tab_id) != login

    def _sign_in_idle(self) -> None:
        if not self._login_set():
            return
        for tab in self._running():
            if not tab.busy and self._behind(tab.tab_id, True):
                self._restart(tab.tab_id, cut=False)

    def _sign_out_all(self) -> None:
        # Every running tab, not only the ones whose home still holds the sign-in: Claude Code
        # removes it there itself once the proxy's refusal signs it out.
        for tab in self._running():
            self._restart(tab.tab_id, cut=tab.busy)

    def _restart(self, tab_id: str, cut: bool) -> bool:
        try:
            self.session.restart_agent(tab_id, resume=True)
        except Exception as exc:                       # noqa: BLE001
            # Said rather than raised: this thread ending would end the daemon.
            self.events.emit("remote_control.restart_failed", tab=tab_id,
                             error=f"{type(exc).__name__}: {exc}")
            return False
        if cut:
            self.events.emit("remote_control.continued", tab=tab_id, deliver={
                "content": continue_message(), "meta": {"from": "daemon"}})
        return True
