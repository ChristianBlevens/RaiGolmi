"""Document maintenance on facts.

Documents are sent to the manager for maintenance when a fact about them says so — no purpose
header, grown past its last audit, over its kind's size budget, a layer doc older than its
layer's files, a path or symbol it names that no longer exists, or a layer with no doc at all —
never on a count or a schedule. The facts are read again whenever something a document describes
may have moved: the daemon's start, a definition written, a watched file changed, a tab closed or
gone idle.

Every `.md` the manager can reach is swept: each layer's and each working copy's under the
definitions, and its own `patterns.md`. A working copy elsewhere is its own tab's to keep, and is
not checked rather than reported as current. Incident docs are not swept. What a doc names is
checked only in a layer doc and a working copy's `SESSION-START.md`, the two written to cite
from their own directory: a project's other docs name files by bare name and from wherever
they were written, and `patterns.md` records failures, dead paths included.

A document is judged when the tab that owns it is not in the middle of changing it: a body's
docs are its tab's and are judged while that tab is idle, since a tab declares every doc it read
before its turn ends; every other definition is the machine tab's, and is judged while the
machine tab is not working. A layer with no doc counts only while no tab is working: a layer is
written file by file, and the tab making it may be about to write its doc, so the manager takes
up what a tab left undone rather than racing it.

One job covers one owner — a body, a face or toolbelt, or the manager's own documents — and names
each of its documents with new facts, so a project with many docs is one fresh session, not one
per doc. A job is sent as the `documents.maintenance` event, which the manager takes
(`manager.py`); this module never calls it. A doc's facts are sent again only when they change —
a stale doc is one fact whichever files moved, a growing one one fact however far it grew — so
the same facts never page the manager twice. Like the manager's queue, what was sent is held in
memory: a restarted daemon says every standing fact once more.
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

        def seen(path: Path) -> str:
            host, inside = next((h, i) for h, i in as_manager_sees if path.is_relative_to(h))
            return f"{inside}/{path.relative_to(host)}".rstrip("/")

        tabs = [tab for tab in self.session.intent.tabs.values() if not tab.manager]
        working = any(tab.busy for tab in tabs)
        machine = self.session.intent.machine_tab()
        machine_working = machine is not None and machine.busy
        jobs: dict[Path, dict[str, list[str]]] = {}
        for owner, doc, budget, mounts, layer, body in self._documents(definitions, root):
            if body is not None:
                tab = self.session.intent.body_tab(body)
                if tab is not None and tab.busy:
                    continue
            if owner != root and machine_working:
                continue
            if not doc.is_file():
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
            if reasons:
                jobs.setdefault(owner, {})[seen(doc)] = reasons
        for owner, docs in jobs.items():
            where = seen(owner)
            self.events.emit(
                "documents.maintenance", document=where, documents=docs,
                message=f"{len(docs)} document{'s' if len(docs) > 1 else ''} under {where} "
                        "need maintenance")

    def _documents(self, definitions: Path, root: Path):
        """Each doc the manager keeps: the owner its job is grouped under, its path, its kind's
        budget if it has one, the container paths it may cite, the layer it describes if it is
        a layer doc, and the body it belongs to if any."""
        cites = {"/definitions": definitions}
        for kind, table in (("faces", self.session.catalogue.faces),
                            ("toolbelts", self.session.catalogue.toolbelts),
                            ("bodies", self.session.catalogue.bodies)):
            for item_id, item in table.items():
                body = item_id if kind == "bodies" else None
                directories = [item.directory]
                if body is not None and item.source_root != item.directory:
                    directories.append(item.source_root)
                directories = [d for d in directories
                               if d is not None and d.is_relative_to(definitions)]
                if not directories:
                    continue
                owner = directories[0]
                layer_doc = owner / documents.LAYER_DOC
                yield (owner, layer_doc, documents.BUDGET[documents.LAYER_DOC], cites, owner,
                       body)
                for directory in directories:
                    for doc in documents.markdown(directory):
                        if doc == layer_doc:
                            continue
                        if body is not None and doc == item.source_root / documents.SESSION_START:
                            yield (owner, doc, documents.BUDGET[documents.SESSION_START], cites,
                                   None, body)
                        else:
                            yield owner, doc, None, None, None, body
        patterns = root / documents.PATTERNS
        if patterns.is_file():
            yield (root, patterns, documents.BUDGET[documents.PATTERNS], None, None, None)
