"""A tab's Claude Code transcript, as the daemon and the tab's own hooks read it."""
from __future__ import annotations

import json
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


def _parts(row: dict[str, Any]) -> list[dict[str, Any]]:
    content = row.get("message", {}).get("content")
    return content if isinstance(content, list) else []


def undeclared_documents(rows: list[dict[str, Any]]) -> list[str]:
    """The documents (`.md`) the conversation read with the Read tool after it last declared
    them with `declare_documents`, in the order first read. A call that failed is neither a
    read nor a declaration."""
    failed = {part.get("tool_use_id") for row in rows if row["type"] == "user"
              for part in _parts(row)
              if part.get("type") == "tool_result" and part.get("is_error")}
    read: dict[str, int] = {}
    declared: dict[str, int] = {}
    for at, row in enumerate(rows):
        if row["type"] != "assistant":
            continue
        for part in _parts(row):
            if part.get("type") != "tool_use" or part.get("id") in failed:
                continue
            given = part.get("input") or {}
            if part.get("name") == "Read" and str(given.get("file_path", "")).endswith(".md"):
                read[given["file_path"]] = at
            elif str(part.get("name", "")).endswith(DECLARE):
                for doc in given.get("documents") or []:
                    declared[doc["path"]] = at
    return [path for path, at in read.items() if at > declared.get(path, -1)]
