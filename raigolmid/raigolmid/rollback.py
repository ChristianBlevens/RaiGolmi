"""A newer Claude Code that fails its first session is gone back from without anyone asking.

An update to a newer Claude Code is on trial (`claudecode.on_trial`) until a tab's first turn on
the image it made says whether it works. The first session on it **fails** when, in a tab whose
agent runs that image, the agent does not come up (`agent.crashed`: its container would not
start, or exited on its own) or a turn ends on an API error that continuing will not fix
(`agent.turn_failed`: a refusal, billing, a model). It **works** when a turn on it ends without
an error (`agent.idle`). A tab still on the image before it decides nothing: its turn is the old
version's. A crash whose container is already gone was a start, and every start after the
update is on the new image. What fails goes back to the version before it
(`Session.roll_back_claude_code`), which tells the machine tab.
"""
from __future__ import annotations

import logging
import threading
import traceback
from typing import TYPE_CHECKING

from . import claudecode, naming
from .events import Event, EventLog

if TYPE_CHECKING:
    from .session import Session

logger = logging.getLogger(__name__)

DECIDING = frozenset({"agent.idle", "agent.turn_failed", "agent.crashed"})


class Rollback:
    """Subscribed at construction, so a first turn between the daemon's start and `run` is
    heard. The trial is read from the choice at each deciding event, so it outlives a restart."""

    def __init__(self, session: "Session", events: EventLog) -> None:
        self.session = session
        self.events = events
        self._sub = events.subscribe()

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            for event in self._sub.drain(timeout=1.0):
                self.on_event(event)

    def on_event(self, event: Event) -> None:
        if event.type not in DECIDING or event.tab is None:
            return
        trial = claudecode.on_trial(self.session.paths.claude_code)
        if trial is None:
            return
        container = self.session.runtime.inspect(naming.agent(event.tab))
        if event.type == "agent.crashed":
            if container is not None and container.image != trial["image"]:
                return
            self._roll_back(event.tab, event.data.get("message") or "the agent did not start")
        elif container is None or container.image != trial["image"]:
            return
        elif event.type == "agent.turn_failed":
            self._roll_back(event.tab, f"its first turn was refused ({event.data['error']})")
        elif event.data.get("error") is None:
            self.session.claude_code_proven(event.tab)

    def _roll_back(self, tab_id: str, why: str) -> None:
        """A rollback that fails is the janitor's (`claude_code.rollback_failed`), with its
        traceback, and never ends this thread."""
        try:
            self.session.roll_back_claude_code(tab_id, why)
        except Exception as exc:                       # noqa: BLE001
            self.events.emit("claude_code.rollback_failed", tab=tab_id, why=why,
                             error=f"{type(exc).__name__}: {exc}",
                             traceback=traceback.format_exc())
            logger.exception("going back from a Claude Code that failed its first session")
