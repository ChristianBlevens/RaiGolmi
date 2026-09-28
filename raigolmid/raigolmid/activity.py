"""What an agent last said about itself, kept where it outlives the daemon.

Whether an agent is working is a fact only its own hooks know; Docker cannot re-derive it. So
each hook writes its report here, in the agent's home — its own, on the host — before it tells
the daemon, and a daemon that starts reads it from here rather than from a report it may have
missed while it was down.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

FILE = Path(".raigolmi") / "activity.json"


@dataclass(frozen=True, slots=True)
class Activity:
    busy: bool = False
    # The container whose Claude Code session last started, by its hostname — Docker's
    # short container id. A running container that is not this one has a session still
    # coming up, which does not hear its channel yet (`channel.py`).
    session: str = ""


def read(home: Path) -> Activity:
    try:
        raw = json.loads((home / FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Activity()
    return Activity(busy=raw["busy"], session=raw.get("session", ""))


def record(home: Path, *, busy: bool, session: str | None = None) -> None:
    """By the agent's hook, in its container, before the daemon is told. `session` is said
    by the SessionStart hook alone; every other report keeps the one before it."""
    path = home / FILE
    path.parent.mkdir(exist_ok=True)
    if session is None:
        session = read(home).session
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"busy": busy, "session": session}))
    os.replace(tmp, path)
