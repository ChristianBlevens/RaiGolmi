"""The janitor tab's failures.

Every failure that interrupts regular use, with no agent already handling it, is put in front
of one agent tab, `janitor`, so the user is never the one who has to bring it to an AI. This
module decides which events are such failures, opens the janitor for the first one, and holds
the rest until the janitor can take them.

Delivery is the janitor's channel, the same as every tab's (`channel.py`): a failure is
queued there by the `janitor.queued` event that carries it, and the channel pushes it into the
janitor's session while it is idle, one at a time, and says it when it could not be heard.
A failure is queued after the janitor is opened for it, so a janitor that could not open
closes no queue: the failure waits for the next open. That is tried again when a credential is
stored (`credential.stored`), and not at all while there is none, because no agent can start
without one. Why the janitor cannot open is said once per reason, not once per failure held.

An incident is fixed when the janitor moves its doc to `incidents/fixed/`, as its template
says; the end of each of its turns says the ones moved since (`janitor.incident_fixed`).

The queue is in memory: a daemon restart re-derives the machine's state, and a
failure that still holds is said again by what finds it.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from . import credential, documents
from .events import Event, EventLog
from .intent import JANITOR

if TYPE_CHECKING:
    from .session import Session

logger = logging.getLogger(__name__)

# What the janitor takes. Everything else is either development failing back to the
# agent that asked (a build, an API error to its caller) or interrupts nothing.
TAKEN = frozenset({
    "container.unfixable",        # one restart spent or failed (`supervisor.py`)
    "container.exit_failed",      # a container exit the daemon could not handle
    "instance.degraded",
    "reconcile.failed",
    "face.failed",
    "face.sync_failed",
    "face.editor_window.failed",
    "selector.failed",            # `Daemon._try`: a host surface or image that does not come up
    "control.failed",
    "notify.failed",
    "hostimages.failed",
    "agent.failed",               # the ready agent not starting
    "runtime.event_stream_failed",
    "hostkeys.watch_failed",
    "clipboard.unbridged",        # the user's face's clipboard left the machine's
    "garbage.failed",
    "watch.failed",
    "channel.unheard",            # another tab's session not hearing its channel
    "channel.silent",             # another tab's channel no longer asking
    "coordinator.unanswered",     # the machine tab not confirming its own handover
    "tab.stalled",                # a working tab whose conversation stands still (`stalls.py`)
    "tab.spinning",               # a working tab whose working copy does not change
    "agent.turn_failed",          # a turn cut off by an API error continuing will not fix
    "disk.grown",                 # the disk holding more than when last looked at (`disk.py`)
    "disk.short",                 # little of the disk left free
    "documents.maintenance",      # an owner's docs: no header, grown, over budget, stale, dead refs
    "documents.maintenance_failed",
})

def message(event: Event, incident: str) -> str:
    """What the janitor reads: the failure as the event log has it, and where its incident
    is written. Its job is its template's (`agents.JANITOR_TEMPLATE`), said once there."""
    return (f"RaiGolmi failure `{event.type}` at "
            f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(event.ts))}:\n"
            f"{event.to_json()}\n\n{incident}")


class Janitor:
    """Subscribed at construction, so the failures of the daemon's own start are not missed:
    the reconcile and the host surfaces run before any thread would otherwise be listening."""

    def __init__(self, session: "Session", events: EventLog) -> None:
        self.session = session
        self.events = events
        self._sub = events.subscribe()
        # Whether failures are queued for a janitor that could not open, and the reason last said.
        self._held = False
        self._unopened: str | None = None
        self._fixed = self._fixed_now()

    # --- routing ----------------------------------------------------------------------
    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            for event in self._sub.drain(timeout=1.0):
                self.on_event(event)
            if self._sub.dropped:
                # Events lost here are failures nobody is told about; the loss is itself one.
                count, self._sub.dropped = self._sub.dropped, 0
                self.events.emit("janitor.events_dropped", count=count)

    def on_event(self, event: Event) -> None:
        if event.type == "credential.stored":
            if self._held:
                self._ensure_open()
            return
        if event.tab == JANITOR:
            self._on_janitor_event(event)
            return
        if event.type not in TAKEN:
            return
        self._ensure_open()
        root = self.session.paths.janitor_documents
        meta = {"failure": event.type}
        try:
            doc = documents.record_incident(root, event)
            meta["incident"] = f"/janitor/{doc.relative_to(root)}"
            incident = f"Its incident is {meta['incident']}: write your diagnosis and repair there."
        except OSError as exc:
            # The failure still reaches the janitor; it may be the reason the write failed.
            incident = f"Its incident doc could not be written in {root}: {exc}"
        self.events.emit("janitor.queued", tab=JANITOR, failure=event.type, incident=incident,
                         deliver={"content": message(event, incident), "meta": meta})

    def _on_janitor_event(self, event: Event) -> None:
        if event.type == "agent.idle":
            self._say_fixed()
        if event.type == "janitor.fresh_conversation":
            # The channel holds the failure until the new session is up (`channel.py`).
            try:
                self.session.restart_agent(JANITOR, resume=False)
            except Exception as exc:                   # noqa: BLE001
                # Reported, and the failure stays held: a janitor that cannot restart is
                # past what it could be handed anyway.
                self.events.emit("janitor.fresh_failed", tab=JANITOR,
                                 error=f"{type(exc).__name__}: {exc}")
        if event.type in ("container.unfixable", "container.exit_failed"):
            # The janitor past its restart is the user's: a janitor for the janitor
            # would loop on the same failure.
            self.events.emit(
                "janitor.unfixable", failure=event.type,
                message="The janitor tab is past reopening. Direct an agent session at it: "
                        f"its evidence is in {self.session.paths.crashes}.")

    def _fixed_now(self) -> set[str]:
        fixed = self.session.paths.janitor_documents / documents.INCIDENTS / documents.FIXED
        return {d.name for d in fixed.glob("*.md")}

    def _say_fixed(self) -> None:
        """An incident is fixed when the janitor moves its doc to `fixed/` (its template),
        which it does in a turn: each turn's end says the ones moved since."""
        now = self._fixed_now()
        for name in sorted(now - self._fixed):
            self.events.emit("janitor.incident_fixed", tab=JANITOR,
                             incident=f"/janitor/{documents.INCIDENTS}/{documents.FIXED}/{name}")
        self._fixed = now

    def _ensure_open(self) -> None:
        try:
            credential.read(self.session.paths.agent_credentials)
            opened = self.session.open_janitor()
        except Exception as exc:                       # noqa: BLE001
            # Not routed back into the queue: the janitor failing to open is the one failure
            # it cannot take, and the queue keeps what it would have been given.
            self._held = True
            error = f"{type(exc).__name__}: {exc}"
            if error != self._unopened:
                self._unopened = error
                self.events.emit("janitor.open_failed", error=error)
                logger.error("janitor tab: %s", exc)
            return
        self._held, self._unopened = False, None
        if opened:
            self.events.emit("janitor.opened")
