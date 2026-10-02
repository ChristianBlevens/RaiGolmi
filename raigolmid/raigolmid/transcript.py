"""A tab's Claude Code transcript, as the daemon and the tab's own hooks read it."""
from __future__ import annotations

import glob
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any


def latest_transcript(home: Path) -> Path | None:
    transcripts = list((home / ".claude" / "projects" / "-work").glob("*.jsonl"))
    return max(transcripts, key=lambda p: p.stat().st_mtime) if transcripts else None


def main_rows(transcript: Path) -> list[dict[str, Any]]:
    """The main conversation's user and assistant rows, in order; a subagent's are not it."""
    rows = []
    with transcript.open(encoding="utf-8") as lines:
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # a line being written as it is read
            if row.get("type") in ("user", "assistant") and not row.get("isSidechain"):
                rows.append(row)
    return rows


DECLARE = "declare_documents"

# A shell command whose first word is one of these shows the agent what the files it names say.
# `sed -i` edits, and a redirect's target is written, so neither is a read.
_READERS = frozenset({"cat", "head", "tail", "sed", "grep", "rg", "awk", "nl", "less", "more",
                      "diff", "cut", "sort", "uniq", "bat", "tac"})
_SEGMENT = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")
_HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")
_PORTION_CHARS = 120


def _parts(row: dict[str, Any]) -> list[dict[str, Any]]:
    content = row.get("message", {}).get("content")
    return content if isinstance(content, list) else []


def _segments(command: str) -> list[str]:
    """The simple commands in a shell command line, heredoc bodies left out."""
    lines, kept, marker = command.splitlines(), [], None
    for line in lines:
        if marker is not None:
            if line.strip() == marker:
                marker = None
            continue
        kept.append(line)
        found = _HEREDOC.search(line)
        if found:
            marker = found[1]
    return [seg for seg in _SEGMENT.split("\n".join(kept)) if seg]


def _words(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _place(name: str, cwd: str, home: Path) -> list[str]:
    """The documents a name in a command means, from the directory it ran in. A name that
    matches nothing here (a path inside the sandbox only) is not one this tab can declare."""
    if name.startswith("~/"):
        name = str(home / name[2:])
    path = name if name.startswith("/") else os.path.normpath(os.path.join(cwd, name))
    if any(c in path for c in "*?["):
        return sorted(glob.glob(path))
    return [path] if os.path.isfile(path) else []


def _shell_reads(command: str, cwd: str, home: Path) -> tuple[list[tuple[str, str]], str]:
    """(document, portion) for each `.md` a reading command names, and the directory the
    shell is left in. The portion is the command itself: what it showed is what was read."""
    reads = []
    for segment in _segments(command):
        words = _words(segment)
        while words and "=" in words[0] and not words[0].startswith("-"):
            words = words[1:]               # an environment assignment before the command
        if not words:
            continue
        if words[0] == "cd" and len(words) > 1:
            cwd = (words[1] if words[1].startswith("/")
                   else os.path.normpath(os.path.join(cwd, words[1])))
            continue
        if words[0] not in _READERS or (words[0] == "sed" and any(
                w.startswith("-i") for w in words)):
            continue
        written = {words[i + 1] for i, w in enumerate(words[:-1]) if w in (">", ">>")}
        for word in words[1:]:
            if word.startswith((">", "-")) or word in written or ".md" not in word:
                continue
            portion = segment if len(segment) <= _PORTION_CHARS else (
                segment[:_PORTION_CHARS] + "…")
            reads.extend((doc, f"`{portion}`") for doc in _place(word, cwd, home))
    return reads, cwd


def _read_portion(given: dict[str, Any]) -> str:
    offset, limit = given.get("offset"), given.get("limit")
    if offset and limit:
        return f"lines {offset}-{int(offset) + int(limit) - 1}"
    if offset:
        return f"from line {offset}"
    if limit:
        return f"lines 1-{limit}"
    return "the whole file"


def undeclared_documents(rows: list[dict[str, Any]], home: Path,
                         cwd: str = "/work") -> dict[str, list[str]]:
    """Each document (`.md`) the conversation read after it last declared it with
    `declare_documents`, with the portions it read — by the Read tool, or by a reading shell
    command, in the agent's own shell or its sandbox's `exec` — in the order first read. A
    declaration answers for what was read before it, and only that. A call that failed is
    neither a read nor a declaration."""
    failed = {part.get("tool_use_id") for row in rows if row["type"] == "user"
              for part in _parts(row)
              if part.get("type") == "tool_result" and part.get("is_error")}
    read: dict[str, list[str]] = {}
    for row in rows:
        if row["type"] != "assistant":
            continue
        for part in _parts(row):
            if part.get("type") != "tool_use" or part.get("id") in failed:
                continue
            name, given = str(part.get("name", "")), part.get("input") or {}
            if name == "Read" and str(given.get("file_path", "")).endswith(".md"):
                read.setdefault(given["file_path"], []).append(_read_portion(given))
            elif name == "Bash":
                found, cwd = _shell_reads(str(given.get("command", "")), cwd, home)
                for doc, portion in found:
                    read.setdefault(doc, []).append(portion)
            elif name.endswith("__exec"):
                cmd = given.get("cmd") or []
                line = " ".join(shlex.quote(w) for w in cmd) if isinstance(cmd, list) else str(cmd)
                if len(cmd) >= 3 and cmd[0] in ("sh", "bash") and cmd[1] in ("-c", "-lc"):
                    line = cmd[2]
                found, _ = _shell_reads(line, str(given.get("cwd", "/work")), home)
                for doc, portion in found:
                    read.setdefault(doc, []).append(portion)
            elif name.endswith(DECLARE):
                for doc in given.get("documents") or []:
                    read.pop(doc.get("path", ""), None)
    return {doc: list(dict.fromkeys(portions)) for doc, portions in read.items()}
