"""Every tab is on Remote Control while the user's claude.ai sign-in is set, and on the agent
credential while it is not.

A tab takes the sign-in when its container starts (`Agents.start`), so a change in the sign-in
reaches a running tab only by restarting it, resumed:

- **given** (`claude_login.stored`, or a daemon that starts with it): each idle tab started
  without it is restarted onto it, and each busy one once its turn ends (`agent.idle`).
- **lost** (`claude_login.lost`): the proxy refuses every request a tab makes on it, so every
  tab is restarted onto the agent credential at once, and one whose turn that cut short is
  told to continue.

Whether a tab is on it is what `Agents.start` wrote into its home (`Agents.signed_in`).
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from . import claude_login
from .events import Event, EventLog

if TYPE_CHECKING:
    from .session import Session


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

    def run(self, stop: threading.Event) -> None:
        self._sign_in_idle()
        while not stop.is_set():
            for event in self._sub.drain(timeout=1.0):
                self.on_event(event)

    def on_event(self, event: Event) -> None:
        if event.type == "claude_login.stored":
            self._sign_in_idle()
        elif event.type == "claude_login.lost":
            self._sign_out_all()
        elif event.type == "agent.idle" and event.tab is not None:
            tab = self.session.intent.tabs.get(event.tab)
            if tab is not None and self._behind(tab.tab_id, self._login_set()):
                self._restart(tab.tab_id, cut=False)

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

    def _restart(self, tab_id: str, cut: bool) -> None:
        try:
            self.session.restart_agent(tab_id, resume=True)
        except Exception as exc:                       # noqa: BLE001
            # Said rather than raised: this thread ending would end the daemon.
            self.events.emit("remote_control.restart_failed", tab=tab_id,
                             error=f"{type(exc).__name__}: {exc}")
            return
        if cut:
            self.events.emit("remote_control.continued", tab=tab_id, deliver={
                "content": continue_message(), "meta": {"from": "daemon"}})
