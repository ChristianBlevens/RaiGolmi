"""The agent's documents: where each lives, what it is for,
and whether it is current.

The index is generated here at each call and never stored, so it cannot go stale. It names
the documents that exist for a tab and the ones its work expects but nobody has written, so
an agent knows to write them. A layer's description doc is **stale** when a file of the layer
was written after it: modification time is the evidence, because a layer need not be a git
repository.

The manager's documents are the machine's, not a tab's, so they live in the daemon's state and
outlive every manager session (`Paths.manager_documents`, mounted at `/manager`). The daemon
opens an incident's doc as it hands the manager the failure, so there is one per incident
whether or not the manager remembers to write it; the manager moves it to `fixed/` when it is
repaired, and keeps `patterns.md`, the failures this machine has shown and what fixed them.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import docwrite, git
from .events import Event

LAYER_DOC = "LAYER.md"
SESSION_START = "SESSION-START.md"
THOUGHTS = "thoughts.md"
# The machine tab's record of a run, in its `/work`, kept across every machine tab's
# conversation until the run is reported.
RUN_RECORD = "run.md"
INCIDENTS = "incidents"
FIXED = "fixed"
PATTERNS = "patterns.md"
# Written by the daemon when a toolbelt resolves, so it moves without the layer changing.
_NOT_THE_LAYERS = {LAYER_DOC, "toolbelt.lock"}
_SINGULAR = {"faces": "face", "toolbelts": "toolbelt", "bodies": "body"}


@dataclass(frozen=True, slots=True)
class Layer:
    kind: str           # faces, toolbelts or bodies
    id: str
    directory: Path


def _layer_files(directory: Path) -> list[Path]:
    """What the layer is made of. When the layer is its own repository, git's own answer, so
    a build's ignored output is not the layer; otherwise every file not hidden. Only the
    layer's own `.git` is asked, never one above it: the definitions are every agent's to
    write."""
    if git.is_repo(directory):
        listed = git.run(["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                         directory).stdout
        return [directory / name for name in listed.split("\0") if name]
    files = []
    for root, dirs, names in os.walk(directory):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        files.extend(Path(root) / n for n in names if not n.startswith("."))
    return files


def changed_after(doc: Path, directory: Path) -> list[str]:
    """The layer's files written after its doc, relative to the layer. The doc's own
    temporaries (`LAYER.md.tmp.*`, an editor's atomic write) are the doc, not the layer."""
    written = doc.stat().st_mtime
    return sorted(str(f.relative_to(directory)) for f in _layer_files(directory)
                  if f.name not in _NOT_THE_LAYERS and not f.name.startswith(LAYER_DOC)
                  and f.is_file() and f.stat().st_mtime > written)


def index(working_copy: Path, home: Path, layers: list[Layer]) -> dict[str, Any]:
    """`documents` exist; `missing` are expected and unwritten. A layer doc's
    `changed_after` lists the layer's files written since it, and is empty when it is current."""
    return _route([
        (working_copy / SESSION_START, None,
         "where the work in this working copy stands, rewritten each session: the one document "
         "the next conversation starts from. Read it before acting; the user's words outrank "
         "it. When their instruction changes what the next session should do, rewrite it "
         "first, then work."),
        (home / THOUGHTS, None,
         "this conversation's thought doc: the goal, what you found, what you decided, written "
         "as you work — its record, archived with it when the tab closes, never what a "
         "conversation starts from."),
        *_layer_rows(layers),
    ])


def _route(expected: list[tuple[Path, Path | None, str]]) -> dict[str, Any]:
    documents: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for path, layer_directory, what in expected:
        if not path.is_file():
            missing.append({"path": str(path), "what": what})
            continue
        row: dict[str, Any] = {"path": str(path), "what": what}
        if layer_directory is not None:
            row["changed_after"] = changed_after(path, layer_directory)
        documents.append(row)
    return {"documents": documents, "missing": missing}

def _layer_rows(layers: list[Layer]) -> list[tuple[Path, Path | None, str]]:
    return [(layer.directory / LAYER_DOC, layer.directory,
             f"the {_SINGULAR[layer.kind]} {layer.id}: its design, its goals, and a general "
             "account of how it is built, pointing at the real files") for layer in layers]


def incident_key(event: Event) -> str:
    """An incident is what failed: every failure of the same instance, tab, surface, unit or
    document while its doc is open is an occurrence in it, since one failure is often said
    twice (a sandbox part's failed restart degrades the sandbox and spends its restart). A
    failure with no subject is its own."""
    subject = (event.instance or event.tab or event.data.get("role")
               or event.data.get("unit") or event.data.get("document") or "")
    return re.sub(r"[^A-Za-z0-9.@_-]", "_", subject or event.type)


def record_incident(root: Path, event: Event) -> Path:
    """The open incident's doc, with this occurrence in it: a new doc for a new incident,
    or the occurrence added to the one still open."""
    incidents = root / INCIDENTS
    (incidents / FIXED).mkdir(parents=True, exist_ok=True)
    key = incident_key(event)
    at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event.ts))
    occurrence = f"```json\n{event.to_json()}\n```\n"
    # `<stamp>-<key>.md`, and the stamp holds no hyphen.
    open_ = sorted(d for d in incidents.glob("*.md") if d.name.split("-", 1)[1] == f"{key}.md")
    if open_:
        docwrite.update(open_[-1], lambda text: f"{text or ''}\n## Again at {at}\n\n{occurrence}")
        return open_[-1]
    doc = incidents / f"{time.strftime('%Y%m%dT%H%M%S', time.localtime(event.ts))}-{key}.md"
    docwrite.update(doc, lambda _: (
        f"# `{event.type}` at {at}\n\n{occurrence}\n## Diagnosis\n\n## Repair\n\n"
        f"When it is repaired, move this file to `{FIXED}/` and add the pattern to "
        f"`{PATTERNS}`.\n"))
    return doc


def manager_index(root: Path, layers: list[Layer]) -> dict[str, Any]:
    incidents = root / INCIDENTS
    expected: list[tuple[Path, Path | None, str]] = [
        *((doc, None, "an open incident: the failure, and your diagnosis and repair as you "
                      "make them. Move it to fixed/ when it is repaired.")
          for doc in sorted(incidents.glob("*.md"))),
        (root / PATTERNS, None,
         "the failures this machine has shown and what fixed them, one entry per pattern. "
         "Read it before diagnosing; add to it when an incident is fixed."),
        *_layer_rows(layers),
    ]
    found = _route(expected)
    found["fixed"] = sorted(str(d) for d in (incidents / FIXED).glob("*.md"))
    return found


# A document is kept small so it is read whole; its budget says how small for
# its kind. Past it, the manager is sent to cut it down.
BUDGET = {LAYER_DOC: 8 * 1024, SESSION_START: 16 * 1024, PATTERNS: 32 * 1024}

# What reads as a file in backticks: a path with a slash, a name with one of these
# extensions, or one of the bare names. Anything else in backticks — a tool, a word, a
# command — is not a claim that something exists, so it is never checked.
_FILE_EXTENSIONS = {"md", "py", "toml", "lock", "json", "yaml", "yml", "txt", "sh", "lua",
                    "js", "ts", "rs", "go", "c", "h", "nix", "cfg", "ini", "conf", "html",
                    "css", "sql"}
_BARE_FILES = {"Dockerfile", "Makefile", "Containerfile"}
_CITED = re.compile(r"`([^`\s]+)`")
_REFERENCE = re.compile(r"^(?P<path>[^:]+?)(?::(?P<at>\d+(?:-\d+)?|[A-Za-z_][\w.]*))?$")


def _is_file_reference(path: str, base: Path) -> bool:
    """A file by its name, or a relative path into what lies beside the doc. A slash alone is
    no claim — a branch (`agent/tab-2`), an import path (`runtime/cgo`) and an image
    (`raigolmi/face-sway`) have one — so a relative path with no file name is checked only
    when its first component is there. `toolbelt.lock` is written by the daemon when the
    toolbelt resolves, so a doc may name it before it exists."""
    if "://" in path or any(c in path for c in "*?{}<>$"):
        return False
    name = path.rstrip("/").rsplit("/", 1)[-1]
    if name == "toolbelt.lock":
        return False
    if name in _BARE_FILES or ("." in name and name.rsplit(".", 1)[1] in _FILE_EXTENSIONS):
        return True
    if path.startswith("/"):
        return True
    return "/" in path and (base / path.split("/", 1)[0]).exists()


# `/work` is the definitions to the manager and a working copy to a tab, so a file under it is a
# different file to each reader. A doc cites from its own directory or `/definitions`. `/work`
# alone names the mount point, which a body's own command may quote, and is no file claim.
_AMBIGUOUS = "/work"


def dead_references(doc: Path, mounts: dict[str, Path]) -> list[str]:
    """Every backticked path the doc names that does not exist, and every `path:line` or
    `path:symbol` whose line or symbol is not in the file. A relative path resolves from the
    doc's directory; a container path (`/definitions/…`) through `mounts`, and a file under
    `/work` is named as ambiguous. An absolute path outside the mounts cannot be answered from
    here and is not checked."""
    dead = []
    for cited in dict.fromkeys(_CITED.findall(doc.read_text(encoding="utf-8"))):
        match = _REFERENCE.match(cited)
        if match is None or not _is_file_reference(match["path"], doc.parent):
            continue
        under = match["path"].removeprefix(_AMBIGUOUS + "/")
        if under != match["path"] and under.strip("/"):
            dead.append(f"`{cited}`: `/work` is a different directory to each agent; cite it "
                        "relative to this doc's directory or under `/definitions`")
            continue
        target = _resolve(match["path"], doc.parent, mounts)
        if target is None:
            continue
        if not target.exists():
            dead.append(f"`{cited}`: no such path")
        elif match["at"] and target.is_file():
            text = target.read_text(encoding="utf-8", errors="replace")
            at = match["at"]
            if at[0].isdigit():
                if int(at.split("-")[-1]) > len(text.splitlines()):
                    dead.append(f"`{cited}`: the file has {len(text.splitlines())} lines")
            elif not re.search(rf"\b{re.escape(at.rsplit('.', 1)[-1])}\b", text):
                dead.append(f"`{cited}`: `{at}` is not in the file")
    return dead


def _resolve(path: str, base: Path, mounts: dict[str, Path]) -> Path | None:
    if not path.startswith("/"):
        return base / path
    for inside, host in mounts.items():
        if path == inside or path.startswith(inside + "/"):
            return host / path[len(inside):].lstrip("/")
    return None


def fact(reason: str) -> str:
    """What a reason is a fact of, for telling a new fact from the same one restated: a stale
    doc is one fact whichever of its layer's files moved."""
    return "stale" if reason.startswith(_STALE) else reason


_STALE = "stale: "


def maintenance(doc: Path, budget: int, mounts: dict[str, Path] | None,
                layer_directory: Path | None = None) -> list[str]:
    """Why this doc needs its maintenance job, or nothing.
    `mounts` None is a record of failures, which names what went missing by its nature, so
    its references are not checked."""
    reasons = []
    size = doc.stat().st_size
    if size > budget:
        reasons.append(f"{size} bytes, over its budget of {budget}")
    if layer_directory is not None:
        changed = changed_after(doc, layer_directory)
        if changed:
            reasons.append(f"{_STALE}changed since it was written: " + ", ".join(changed))
    if mounts is not None:
        reasons.extend(dead_references(doc, mounts))
    return reasons
