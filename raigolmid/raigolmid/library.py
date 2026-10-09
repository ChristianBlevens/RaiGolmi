"""Every document an agent reads, listed for the catalog's Documents and Thoughts sections;
how a save is written is `docwrite.py`.

**Ids are the daemon's**: the catalog names a document by the id `Library.list` gave it and the
daemon resolves the path from its own enumeration, never from the window. A document the design
says is shown and never edited — a tab's thoughts, the shipped guide, the generated index — is
listed with `editable` false and refused on save. A document whose reader has rules (the
settings, the permissions, the primers) is checked by that reader's own parser before it is
written, and a document that does not exist yet opens on the text its reader would use without
it, so nothing the user has not written is hidden from them.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import documents, hostimages, settings
from .agents import DEFAULT_TEMPLATE, GUIDE, JANITOR_TEMPLATE, AgentError, render_template
from . import docwrite
from .docwrite import DocumentError
from .paths import Paths


@dataclass(frozen=True)
class Document:
    id: str
    group: str
    title: str
    path: Path | None                            # None: generated, never a file
    editable: bool
    absent: str = ""                             # what its reader uses while there is no file
    check: Callable[[str], None] | None = None   # its reader's parser; raises on what it refuses
    generate: Callable[[], str] | None = None




def _md_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.md") if p.is_file()) if root.is_dir() else []


class Library:
    """What the catalog's Documents and Thoughts sections list, read and save."""

    def __init__(self, paths: Paths, session: Any, check_permissions: Callable[[str], None],
                 absent_permissions: str) -> None:
        self.paths = paths
        self.session = session
        self.check_permissions = check_permissions
        self.absent_permissions = absent_permissions

    def _documents(self) -> list[Document]:
        p, docs = self.paths, []
        add = docs.append

        def check_settings(text: str) -> None:
            try:
                settings.parse(text, "settings.toml")
            except settings.SettingsError as exc:
                raise DocumentError(str(exc)) from exc

        add(Document("settings", "Settings", "Settings", p.settings, True,
                     check=check_settings))
        add(Document("permissions", "Settings", "Permissions answered always", p.permissions,
                     True, absent=self.absent_permissions, check=self.check_permissions))

        for name, default, title in (("claude.md", DEFAULT_TEMPLATE, "Tab primer"),
                                     ("janitor.md", JANITOR_TEMPLATE, "Janitor primer")):
            path = p.agent_templates / name

            def check_template(text: str, path: Path = path) -> None:
                try:
                    render_template(text, path, settings.in_force(p))
                except AgentError as exc:
                    raise DocumentError(str(exc)) from exc
            add(Document(f"primer/{name}", "Machine", title, path, True, absent=default,
                         check=check_template))
        add(Document("preferences", "Machine", "Preferences", p.preferences, True))
        add(Document("work/session-start", "Machine", f"{documents.SESSION_START} (no body)",
                     p.work / documents.SESSION_START, True))
        for path in _md_files(p.agent_plugins):
            rel = path.relative_to(p.agent_plugins)
            add(Document(f"plugin/{rel}", "Machine", f"Plugin {rel}", path, True))

        for kind, rows in self.session.list_items().items():
            for row in rows:
                if row["directory"]:
                    one = documents._SINGULAR[kind]
                    add(Document(f"layer/{one}/{row['id']}", "Layers",
                                 f"{one} {row['id']} — {documents.LAYER_DOC}",
                                 Path(row["directory"]) / documents.LAYER_DOC, True))
        for body_id, body in sorted(self.session.catalogue.bodies.items()):
            if body.source_root is not None:
                add(Document(f"project/{body_id}", "Projects",
                             f"{body_id} — {documents.SESSION_START}",
                             body.source_root / documents.SESSION_START, True))

        root = p.janitor_documents
        add(Document("janitor/patterns", "Janitor", documents.PATTERNS,
                     root / documents.PATTERNS, True))
        incidents = root / documents.INCIDENTS
        for path in sorted(incidents.glob("*.md")):
            add(Document(f"janitor/incident/{path.name}", "Janitor", f"Open: {path.stem}",
                         path, True))
        for path in sorted((incidents / documents.FIXED).glob("*.md")):
            add(Document(f"janitor/fixed/{path.name}", "Janitor", f"Fixed: {path.stem}",
                         path, True))

        # A run's report and record are what happened while the user was away, kept as written.
        if p.runs.is_dir():
            for path in sorted(p.runs.glob("*.md"), reverse=True):
                add(Document(f"run/{path.name}", "Runs", path.stem, path, False))

        # A thought is what an AI was thinking; editing one would make history unreliable.
        for tab_id in sorted(self.session.intent.tabs):
            path = p.agent_homes / tab_id / documents.THOUGHTS
            if path.is_file():
                add(Document(f"thought/{tab_id}", "Thoughts", f"{tab_id} (open)", path, False))
        if p.agent_archive.is_dir():
            for home in sorted(d for d in p.agent_archive.iterdir() if d.is_dir()):
                path = home / documents.THOUGHTS
                if path.is_file():
                    add(Document(f"thought/archive/{home.name}", "Thoughts", home.name,
                                 path, False))

        guide = hostimages.source_root() / GUIDE
        for path in _md_files(guide):
            rel = path.relative_to(guide)
            add(Document(f"guide/{rel}", "Shipped", f"Guide: {rel}", path, False))
        add(Document("index", "Shipped", "The janitor's index (generated)", None, False,
                     generate=lambda: json.dumps(self.session.machine_index(), indent=2)))
        return docs

    def _find(self, id: str) -> Document:
        for doc in self._documents():
            if doc.id == id:
                return doc
        raise DocumentError(f"no document {id!r}")

    def list(self) -> list[dict[str, Any]]:
        return [{"id": d.id, "group": d.group, "title": d.title, "editable": d.editable,
                 "exists": d.path is None or d.path.is_file()} for d in self._documents()]

    def read(self, id: str) -> dict[str, Any]:
        doc = self._find(id)
        if doc.generate is not None:
            text, ver, exists = doc.generate(), None, True
        else:
            assert doc.path is not None
            text, ver = docwrite.read(doc.path)
            exists = text is not None
            text = doc.absent if text is None else text
        return {"id": doc.id, "title": doc.title, "path": str(doc.path or ""), "text": text,
                "version": ver, "editable": doc.editable, "exists": exists}

    def write(self, id: str, text: str, version: str | None) -> dict[str, Any]:
        doc = self._find(id)
        if not doc.editable or doc.path is None:
            raise DocumentError(f"{doc.title} is shown, never edited")
        if doc.check is not None:
            doc.check(text)
        docwrite.write(doc.path, text, version)
        return self.read(id)
