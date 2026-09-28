"""What the machine keeps of closed tabs and of containers that exited on their own:
the archived homes and the crash evidence (`supervisor.py`) share one
budget on disk, and the oldest go first when it is exceeded.

An entry is an archived home with its `.json` record, or one crash log. The newest entry is
never dropped, so a close cannot remove the archive it has just made, however large.
`kept.pruned` names what went.
"""
from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from .events import EventLog
from .paths import Paths

# About 3% of a 60 GB disk; a closed tab's home is a few MB.
BUDGET_BYTES = 2 * 1024 ** 3


class KeepError(Exception):
    pass


@dataclass(frozen=True)
class _Entry:
    name: str
    kind: str                # "archive" | "crash_log"
    paths: tuple[Path, ...]
    since: float
    size: int


def _size(path: Path) -> int:
    if not path.is_dir() or path.is_symlink():
        return path.lstat().st_size
    total = 0
    for root, dirs, files in os.walk(path, onerror=_raise):
        for name in (*dirs, *files):
            total += os.lstat(os.path.join(root, name)).st_size
    return total


def _raise(exc: OSError) -> None:
    raise exc


def _entries(paths: Paths) -> list[_Entry]:
    entries = []
    for record in paths.agent_archive.glob("*.json"):
        home = record.with_suffix("")
        entries.append(_Entry(home.name, "archive", (home, record), record.stat().st_mtime,
                              _size(home) + _size(record)))
    for log in paths.crashes.glob("*.log"):
        entries.append(_Entry(log.name, "crash_log", (log,), log.stat().st_mtime, _size(log)))
    return sorted(entries, key=lambda e: e.since)


def _remove(path: Path) -> None:
    """An agent's home can hold directories it made read-only, which rmtree cannot empty."""
    if not path.is_dir() or path.is_symlink():
        path.unlink()
        return
    os.chmod(path, path.lstat().st_mode | stat.S_IRWXU)
    # Top-down: each directory is opened before os.walk lists it.
    for root, dirs, _files in os.walk(path, onerror=_raise):
        for name in dirs:
            d = os.path.join(root, name)
            if not os.path.islink(d):
                os.chmod(d, os.lstat(d).st_mode | stat.S_IRWXU)
    shutil.rmtree(path)


def prune(paths: Paths, events: EventLog, budget: int = BUDGET_BYTES) -> list[str]:
    """Raises KeepError on a removal that fails, having said what went before it."""
    dropped: list[_Entry] = []
    total = 0
    try:
        entries = _entries(paths)
        total = sum(e.size for e in entries)
        while total > budget and len(entries) > 1:
            entry = entries.pop(0)
            for path in entry.paths:
                _remove(path)
            total -= entry.size
            dropped.append(entry)
    except OSError as exc:
        raise KeepError(f"pruning {paths.agent_archive} and {paths.crashes}: {exc}") \
            from exc
    finally:
        if dropped:
            events.emit("kept.pruned",
                        archives=[e.name for e in dropped if e.kind == "archive"],
                        crash_logs=[e.name for e in dropped if e.kind == "crash_log"],
                        freed=sum(e.size for e in dropped), kept=total)
    return [e.name for e in dropped]
