"""The actions the daemon gates on the user's yes.

A tab's tool call that would change something the user uses does not act: it raises a
permission (`Questions.ask_permission`) and returns, and the tab ends its turn. This carries
the gated action out when they answer yes, and in every case tells the tab what came of it
on its channel, as its next message, including when a remembered *always* answered for them
(`questions.py`). No answer — dismissed, or left 30 minutes — is a no. The first gated
action is a tab swapping the toolbelt of the sandbox the user's face is on, which ends their
language servers and their face terminals' shells.
"""
from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Callable

from .events import Event, EventLog

if TYPE_CHECKING:
    from .session import Session

logger = logging.getLogger(__name__)

TOOLBELT_SWAP = "toolbelt_swap"


def swap_message(toolbelt: str) -> str:
    return (f"The agent wants to swap this sandbox's toolbelt to '{toolbelt}'. Its language "
            "servers and your terminals' shells in the sandbox end, and the body keeps "
            "running.")


class Permissions:
    def __init__(self, session: "Session", events: EventLog) -> None:
        self.session = session
        self.events = events
        self._sub = events.subscribe()
        self._actions: dict[str, Callable[[str, dict], str]] = {
            TOOLBELT_SWAP: self._swap,
        }

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            for event in self._sub.drain(timeout=1.0):
                self.on_event(event)
            if self._sub.dropped:
                # A dropped answer is an action nobody carries out and a tab never told.
                count, self._sub.dropped = self._sub.dropped, 0
                self.events.emit("permissions.events_dropped", count=count)

    def on_event(self, event: Event) -> None:
        if event.data.get("kind") != "permission" or event.type not in (
                "question.answered", "question.lapsed"):
            return
        action, tab = event.data["action"], event.tab
        who = ("The user's standing answer (always)" if event.data.get("by") == "always"
               else "The user")
        if event.data["answer"] != "yes":
            self._tell(event, tab, f"{who} did not allow it ({event.data['answer']}): "
                                   f"nothing was done. {self._what(action)}")
            return
        try:
            done = self._actions[action["do"]](tab, action)
        except Exception as exc:                        # noqa: BLE001
            # Said to the tab that asked, whose work it is, and logged: nothing else waits.
            logger.error("permission %s: %s", event.data["id"], exc)
            self._tell(event, tab, f"{who} allowed it, and it failed: {exc}. "
                                   f"{self._what(action)}", failed=True)
            return
        self._tell(event, tab, f"{who} allowed it, and it is done: {done}")

    def _swap(self, tab: str, action: dict) -> str:
        self.session.toolbelt_swap(tab, action["toolbelt"])
        return f"the sandbox's toolbelt is now '{action['toolbelt']}'."

    @staticmethod
    def _what(action: dict) -> str:
        return f"(asked: {action['do']} {action.get('toolbelt', '')})".rstrip()

    def _tell(self, event: Event, tab: str, content: str, failed: bool = False) -> None:
        self.events.emit("permission.failed" if failed else "permission.settled", tab=tab,
                         id=event.data["id"], action=event.data["action"],
                         answer=event.data["answer"], deliver={
                             "content": f"Permission {event.data['id']}: {content}",
                             "meta": {"permission": event.data["id"]}})
