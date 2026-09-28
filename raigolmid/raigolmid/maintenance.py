"""Document maintenance on facts.

A document is sent to the manager for maintenance when a fact about it says so — over its
size budget, a layer doc older than its layer's files, a path or symbol it names that no
longer exists, or a layer with no doc at all — never on a count or a schedule. The facts are
read again whenever something a document describes may have moved: the daemon's start, a
definition written, a watched file changed, a tab closed or gone idle.

A document is judged when the tab that owns it is not in the middle of changing it: a
body with a tab is that tab's, its layer doc and `SESSION-START.md` included, and is not swept
until the tab closes; every other definition is the machine tab's, and is judged while the
machine tab is not working. A layer with no doc counts only while no tab is working: a layer
is written file by file, and the tab making it may be about to write its doc, so the manager
takes up what a tab left undone rather than racing it. A job is sent as the `documents.maintenance` event, which the manager
takes (`manager.py`); this module never calls it.

Only what the manager can reach is swept: the layer docs and working copies under the
definitions, and its own `patterns.md`. A working copy elsewhere is its own tab's to keep, and
is not checked rather than reported as current. Incident docs are not swept, and `patterns.md`
is held to its budget alone: both record failures, dead paths included, and a job about one
would open an incident about itself.

A job is sent again only when the document's facts change — a stale doc is one fact whichever
files moved — so the same facts never page the manager twice. Like the manager's queue, what was sent is held in memory: a restarted daemon
says every standing fact once more.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from . import documents
from .events import EventLog

if TYPE_CHECKING:
    from .session import Session

TRIGGERS = frozenset({"raigolmid.started", "definitions.changed", "watch.triggered",
                      "tab.closed", "agent.idle"})


class Maintenance:
    def __init__(self, session: "Session", events: EventLog) -> None:
        self.session = session
        self.events = events
        self._sub = events.subscribe()
        self._sent: dict[str, list[str]] = {}

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            batch = self._sub.drain(timeout=1.0)
            if any(event.type in TRIGGERS for event in batch):
                try:
                    self.sweep()
                except Exception as exc:               # noqa: BLE001
                    # The manager takes this too: a sweep that cannot run keeps every doc
                    # unchecked, which is not the same as every doc current.
                    self.events.emit("documents.maintenance_failed",
                                     error=f"{type(exc).__name__}: {exc}")

    def sweep(self) -> None:
        definitions = self.session.definitions_root()
        root = self.session.paths.manager_documents
        # Where the manager finds each doc, so the job names what it can open.
        as_manager_sees = ((root, "/manager"), (definitions, "/definitions"))
        tabs = [tab for tab in self.session.intent.tabs.values() if not tab.manager]
        working = any(tab.busy for tab in tabs)
        machine = self.session.intent.machine_tab()
        machine_working = machine is not None and machine.busy
        for doc, budget, mounts, layer, body in self._documents(definitions, root):
            if body is not None and self.session.intent.body_tab(body) is not None:
                continue
            if mounts is not None and machine_working:
                continue
            if not doc.is_file():
                if layer is None:
                    self._sent.pop(str(doc), None)
                    continue
                if working:
                    continue
                reasons = [f"missing: the layer at {layer.name} has no {documents.LAYER_DOC}; "
                           "write one from its files"]
            else:
                try:
                    reasons = documents.maintenance(doc, budget, mounts, layer)
                except (OSError, UnicodeDecodeError) as exc:
                    reasons = [f"could not be read: {exc}"]
            facts = [documents.fact(reason) for reason in reasons]
            if facts == self._sent.get(str(doc)):
                continue
            self._sent[str(doc)] = facts
            if not reasons:
                continue
            host, inside = next((h, i) for h, i in as_manager_sees if doc.is_relative_to(h))
            seen = f"{inside}/{doc.relative_to(host)}"
            self.events.emit(
                "documents.maintenance", document=seen, reasons=reasons,
                message=f"{seen} needs maintenance: bring it up to date and within its "
                        "budget, naming only what exists, each path relative to the doc's "
                        "own directory or under `/definitions`.")

    def _documents(self, definitions: Path, root: Path):
        """Each doc the manager keeps: its path, budget, the container paths it may cite, the
        layer it describes if it is a layer doc, and the body it belongs to if any."""
        for kind, table in (("faces", self.session.catalogue.faces),
                            ("toolbelts", self.session.catalogue.toolbelts),
                            ("bodies", self.session.catalogue.bodies)):
            for item_id, item in table.items():
                directory = item.directory
                if directory is None or not directory.is_relative_to(definitions):
                    continue
                yield (directory / documents.LAYER_DOC, documents.BUDGET[documents.LAYER_DOC],
                       {"/definitions": definitions}, directory,
                       item_id if kind == "bodies" else None)
        for body_id, body in self.session.catalogue.bodies.items():
            copy = body.source_root
            if copy is not None and copy.is_relative_to(definitions):
                yield (copy / documents.SESSION_START,
                       documents.BUDGET[documents.SESSION_START],
                       {"/definitions": definitions}, None, body_id)
        yield (root / documents.PATTERNS, documents.BUDGET[documents.PATTERNS], None, None, None)
