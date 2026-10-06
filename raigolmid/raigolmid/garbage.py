"""Old toolbelt images and closures go while the machine runs, not only at its next start.

Editing a toolbelt's package list is ordinary work, and each edit makes a new image and
closure (gigabytes for a compiler) the moment a sandbox runs on it. A machine left open for
weeks would hold every one until it restarted. So the toolbelts' half of the daemon's own
collection (`Session.collect_garbage(bodies=False)`) runs whenever what a toolbelt is changes
in use — a new image locked as the one a sandbox works on (`toolbelt.locked`) — and when a
layer is deleted from the catalog (`catalog.deleted`). A body's images keep their own
supersession (`superseded.py`) and the collection at the start.
"""
from __future__ import annotations

import logging
import threading
import traceback
from typing import TYPE_CHECKING

from .events import EventLog

if TYPE_CHECKING:
    from .session import Session

logger = logging.getLogger(__name__)

AFTER = frozenset({"toolbelt.locked", "catalog.deleted"})


class Garbage:
    """Subscribed at construction, so no lock between the daemon's start and `run` is missed."""

    def __init__(self, session: "Session", events: EventLog) -> None:
        self.session = session
        self.events = events
        self._sub = events.subscribe()

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            if any(event.type in AFTER for event in self._sub.drain(timeout=1.0)):
                self.collect()

    def collect(self) -> None:
        """One collection for however many changes arrived together. A failure is the
        janitor's (`garbage.failed`), with its traceback, and never ends this thread."""
        try:
            self.session.collect_garbage(bodies=False)
        except Exception as exc:                       # noqa: BLE001
            self.events.emit("garbage.failed", error=f"{type(exc).__name__}: {exc}",
                             traceback=traceback.format_exc())
            logger.exception("collecting old toolbelt images and closures")
