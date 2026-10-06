"""What the catalog window shows, without a screen.

The daemon says what each layer is (`raigolmid/catalog.py`); this decides what of it is on
screen: three sections, each a grid of entries, filtered by the search and by whether the
server is shown. A collapsed section is not part of a search. With the server shown and
nothing searched, a section holds its most-downloaded server entries after the machine's own;
a search looks through all of them.

Below the layers, every document an agent reads (`raigolmid/library.py`): the Documents section
by group, and the Thoughts section, which is shown and never edited. A search matches a
document by its title and group.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

SECTIONS = (("face", "Faces"), ("body", "Bodies"), ("toolbelt", "Toolbelts"))
DOCUMENTS, THOUGHTS = "documents", "thoughts"
# How many server entries a section shows before anything is searched.
POPULAR = 12
STATES = {"server": "On the server", "downloaded": "Downloaded", "installed": "Installed"}
BUSY = {"installing": "Installing…", "uploading": "Uploading…"}


def matches(entry: dict[str, Any], query: str) -> bool:
    words = query.casefold().split()
    text = " ".join(str(entry.get(k) or "") for k in ("name", "id", "description", "author"))
    return all(word in text.casefold() for word in words)


def buttons(entry: dict[str, Any]) -> tuple[str, ...]:
    """The entry's actions: download becomes install once downloaded, delete while anything
    of it is here, upload on one the user authored."""
    if (entry.get("activity") or {}).get("what") in BUSY:
        return ()
    state = entry["state"]
    if state == "server":
        return ("download",)
    out = ("install", "delete") if state == "downloaded" else ("delete",)
    return out + (("upload",) if entry.get("authored") else ())


def note(entry: dict[str, Any]) -> tuple[str, bool] | None:
    """The one line an entry says about itself beyond its state, and whether it is a problem."""
    activity = entry.get("activity") or {}
    what = activity.get("what")
    if what in BUSY:
        return BUSY[what], False
    if what in ("install_failed", "upload_failed"):
        return activity["error"], True
    if what == "uploaded":
        return f"pull request: {activity['pull_request']}", False
    if entry.get("problem"):
        return entry["problem"], True
    if entry.get("in_use"):
        return f"in use: {entry['in_use']}", False
    if entry["state"] != "installed" and not entry.get("authored"):
        return "someone else's code: installed, it runs on your machine as yours does", False
    return None


def documents(docs: list[dict[str, Any]], query: str,
              collapsed: set[str]) -> dict[str, list[tuple[str, list[dict[str, Any]]]]]:
    """Each documents section's groups in the daemon's order, with what the search leaves."""
    words = query.casefold().split()
    out: dict[str, list[tuple[str, list[dict[str, Any]]]]] = {DOCUMENTS: [], THOUGHTS: []}
    for doc in docs:
        section = THOUGHTS if doc["group"] == "Thoughts" else DOCUMENTS
        text = f"{doc['group']} {doc['title']}".casefold()
        if section in collapsed or not all(word in text for word in words):
            continue
        groups = out[section]
        if not groups or groups[-1][0] != doc["group"]:
            groups.append((doc["group"], []))
        groups[-1][1].append(doc)
    return out


def sections(entries: list[dict[str, Any]], query: str, server: bool,
             collapsed: set[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for kind, _ in SECTIONS:
        if kind in collapsed:
            out[kind] = []
            continue
        mine = sorted((e for e in entries if e["kind"] == kind and e["state"] != "server"),
                      key=lambda e: e["name"].casefold())
        theirs = sorted((e for e in entries if e["kind"] == kind and e["state"] == "server"),
                        key=lambda e: -(e.get("downloads") or 0)) if server else []
        if query.strip():
            mine = [e for e in mine if matches(e, query)]
            theirs = [e for e in theirs if matches(e, query)]
        else:
            theirs = theirs[:POPULAR]
        out[kind] = mine + theirs
    return out


@dataclass
class CatalogModel:
    call: Callable[..., Any]
    entries: list[dict[str, Any]] = field(default_factory=list)
    server_error: str | None = None
    documents: list[dict[str, Any]] = field(default_factory=list)

    def fetch(self, server: bool) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        return self.call("catalog", server=server), self.call("documents")

    def apply(self, fetched) -> None:
        listing, self.documents = fetched
        self.entries, self.server_error = listing["entries"], listing["server_error"]

    @staticmethod
    def request(action: str, entry: dict[str, Any]) -> tuple[str, tuple[str, dict[str, Any]]]:
        """What an entry's button asks the daemon, and the line said once it answers."""
        params = {"kind": entry["kind"], "id": entry["id"]}
        said = {"download": "downloaded", "install": "installing", "delete": "deleted"}[action]
        return f"{entry['name']}: {said}", (f"catalog_{action}", params)
