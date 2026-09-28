"""Which agent tab the user is looking at in the AI terminal, and which idle tabs they have
not seen.

The user views a tab when the terminal is on the screen and that tab's tmux window is
current. Two reporters say so, because no one process knows both: the terminal's
`session-window-changed` hook names the current window (`rai ai viewing`), and the host
control, which watches sway, says whether the terminal is shown. Neither is asked; each
reports a change as it sees it.

A tab that ends a turn with nothing pending (`agent.idle`, `done`) is **unseen** until the user
views it, unless they are viewing it as it ends. What it needs of them is derived where it is
read (`api.with_tab_states`); only what they have seen is kept here, written to
`Paths.viewing` so a daemon restart neither lights nor puts out a mark.
"""
from __future__ import annotations

import threading
from pathlib import Path

from .events import Event, EventLog
from .intent import load_json, save_json


class Viewing:
    """Subscribed at construction, so no idle falls between the daemon's start and `run`."""

    def __init__(self, events: EventLog, store: Path) -> None:
        self.events = events
        self.store = store
        self._sub = events.subscribe()
        self._lock = threading.Lock()
        raw = load_json(store, "what the user has viewed")
        if raw is None:
            self._shown, self._window, self._unseen = False, None, set()
        else:
            self._shown, self._window = bool(raw["shown"]), raw["window"]
            self._unseen = set(raw["unseen"])

    def run(self, stop: threading.Event) -> None:
        # What was viewed before the daemon started still is: the terminal outlives it.
        self.events.emit("terminal.viewing", tab=self.viewed())
        while not stop.is_set():
            for event in self._sub.drain(timeout=1.0):
                self.on_event(event)

    def on_event(self, event: Event) -> None:
        if event.tab is None:
            return
        if event.type == "agent.idle" and event.data["done"]:
            self._mark(event.tab, unseen=event.tab != self.viewed())
        elif event.type in ("agent.busy", "tab.closed"):
            self._mark(event.tab, unseen=False)

    def report(self, shown: bool | None = None, window: str | None = None) -> dict:
        """The terminal shown or hidden, or its current window by name: a tab's window is
        named for its tab first (`terminal.window_name`); any other window names no tab."""
        with self._lock:
            if shown is not None:
                self._shown = shown
            if window is not None:
                self._window = window.split()[0] if window.strip() else None
            self._save()
        viewed = self.viewed()
        self.events.emit("terminal.viewing", tab=viewed)
        if viewed is not None:
            self._mark(viewed, unseen=False)
        return {"viewing": viewed}

    def viewed(self) -> str | None:
        with self._lock:
            return self._window if self._shown else None

    def unseen(self, tab: str) -> bool:
        with self._lock:
            return tab in self._unseen

    def _mark(self, tab: str, unseen: bool) -> None:
        with self._lock:
            if (tab in self._unseen) == unseen:
                return
            (self._unseen.add if unseen else self._unseen.discard)(tab)
            self._save()
        self.events.emit("terminal.unseen", tab=tab, unseen=unseen)

    def _save(self) -> None:
        """Under the lock."""
        save_json(self.store, {"shown": self._shown, "window": self._window,
                               "unseen": sorted(self._unseen)})
