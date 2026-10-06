"""A tab past its context budget is told so, by the janitor, and never cut off.

Every tab keeps to the user's context budget (`settings.toml`, the primer's budget line) by
making its documents ready and ending its conversation before it is reached. A tab that misses
that goes on working: stopping it would lose the work it is in the middle of. So it is
noticed instead: each `TICK_SECONDS` every running tab's context — the input its latest answer
took (`transcript.context_tokens`) — is read against the budget, and a conversation past it is
`tab.over_budget`, once, which the janitor takes and tells the tab of (`janitor.py`).
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from . import settings
from .events import EventLog
from .intent import JANITOR
from .transcript import context_tokens, latest_transcript

if TYPE_CHECKING:
    from .session import Session

# How soon a conversation past the budget is noticed, never whether.
TICK_SECONDS = 60.0


class ContextWatch:
    def __init__(self, session: "Session", events: EventLog) -> None:
        self.session = session
        self.events = events
        self._said: set[str] = set()        # the transcripts already said

    def run(self, stop: threading.Event) -> None:
        while not stop.wait(TICK_SECONDS):
            self.tick()

    def tick(self) -> None:
        budget = settings.load(self.session.paths.settings).budget_tokens
        for tab_id, tab in dict(self.session.intent.tabs).items():
            # The janitor's own conversation is one failure long and starts fresh each time.
            if tab_id == JANITOR or tab.status != "running":
                continue
            home = self.session.agents.home(tab_id)
            transcript = latest_transcript(home)
            tokens = context_tokens(home)
            if transcript is None or tokens is None or tokens <= budget:
                continue
            if str(transcript) in self._said:
                continue
            self._said.add(str(transcript))
            self.events.emit("tab.over_budget", tab=tab_id, tokens=tokens, budget=budget,
                             managed=tab.managed, body=tab.body,
                             message=(f"Tab {tab_id} is at {tokens // 1000}k tokens of context, "
                                      f"past the {budget // 1000}k budget"))
