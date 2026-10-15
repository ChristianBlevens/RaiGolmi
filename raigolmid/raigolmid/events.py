"""The event log.

Every state change lands here as one JSON line. This is the one thing an agent cannot
reconstruct from a repository: what actually ran on this machine and what happened to it.
It is also the daemon's own black box: a user with a broken `raigolmid` can still read this
file from the AI terminal and find the cause.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_BYTES = 8 * 1024 * 1024
RETAIN = 3


@dataclass(slots=True)
class Event:
    type: str
    ts: float
    epoch: int
    instance: str | None = None
    tab: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        d: dict[str, Any] = {"ts": self.ts, "type": self.type, "epoch": self.epoch}
        if self.instance:
            d["instance"] = self.instance
        if self.tab:
            d["tab"] = self.tab
        d.update(self.data)
        return json.dumps(d, default=str)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Event":
        return cls(
            type=d.pop("type"),
            ts=d.pop("ts", 0.0),
            epoch=d.pop("epoch", 0),
            instance=d.pop("instance", None),
            tab=d.pop("tab", None),
            data=d,
        )


def read_lines(path: Path) -> Iterator[Any]:
    """Each row of an append-only JSON-lines file. A line that does not parse is a write torn
    by a power cut or a killed process, and is not a reason to hide the rest."""
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield row


def append_lines(path: Path, lines: Iterable[str]) -> None:
    """`lines` appended to a JSON-lines file, after ending a torn last line, so that the
    torn line costs only itself and not the first row written after it."""
    with path.open("a+b") as fh:
        end = fh.seek(0, os.SEEK_END)
        if end:
            fh.seek(end - 1)
        torn = end > 0 and fh.read(1) != b"\n"
        text = "".join(f"{line}\n" for line in lines)
        fh.write((("\n" if torn else "") + text).encode("utf-8"))


class Subscriber:
    """One `subscribe()` caller. Events are dropped for a subscriber that stops reading
    rather than blocking the daemon behind it; the drop is visible in `dropped`."""

    def __init__(self, maxsize: int = 512) -> None:
        self._q: deque[Event] = deque(maxlen=maxsize)
        self._wake = threading.Condition()
        self.dropped = 0
        self.closed = False

    def offer(self, event: Event) -> None:
        with self._wake:
            if len(self._q) == self._q.maxlen:
                self.dropped += 1
            self._q.append(event)
            self._wake.notify_all()

    def wait(self, timeout: float | None = None) -> None:
        """Until there is an event to drain, or `timeout`; takes none."""
        with self._wake:
            if not self._q and not self.closed:
                self._wake.wait(timeout)

    def drain(self, timeout: float | None = None) -> list[Event]:
        with self._wake:
            if not self._q and not self.closed:
                self._wake.wait(timeout)
            out = list(self._q)
            self._q.clear()
            return out

    def close(self) -> None:
        with self._wake:
            self.closed = True
            self._wake.notify_all()


class EventLog:
    def __init__(self, path: Path, epoch: int = 0, max_bytes: int = MAX_BYTES,
                 retain: int = RETAIN) -> None:
        self.path = path
        self.epoch = epoch
        self.max_bytes = max_bytes
        self.retain = retain
        self._lock = threading.Lock()
        self._subscribers: list[Subscriber] = []
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, type: str, *, instance: str | None = None, tab: str | None = None,
             **data: Any) -> Event:
        event = Event(type=type, ts=time.time(), epoch=self.epoch,
                      instance=instance, tab=tab, data=data)
        line = event.to_json()
        with self._lock:
            self._rotate_if_needed(len(line) + 1)
            append_lines(self.path, [line])
            subscribers = list(self._subscribers)
        for sub in subscribers:
            sub.offer(event)
        return event

    def _rotate_if_needed(self, incoming: int) -> None:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return
        if size + incoming <= self.max_bytes:
            return
        for n in range(self.retain - 1, 0, -1):
            src = self.path.with_suffix(f".jsonl.{n}")
            if src.exists():
                src.rename(self.path.with_suffix(f".jsonl.{n + 1}"))
        self.path.rename(self.path.with_suffix(".jsonl.1"))

    # --- reading -------------------------------------------------------------------
    def _files_for_history(self) -> list[Path]:
        """`history` reads the current file plus one rotation."""
        rotated = self.path.with_suffix(".jsonl.1")
        return [p for p in (rotated, self.path) if p.exists()]

    def read(self) -> Iterator[Event]:
        for f in self._files_for_history():
            for row in read_lines(f):
                try:
                    event = Event.from_dict(row)
                except KeyError:
                    continue  # a row with no type is not an event
                yield event

    def history(self, instance_id: str, n: int = 50,
                working_copy: str | None = None) -> list[Event]:
        """A sandbox's events, and the builds of `working_copy`, the copy its body builds
        from: a build names the copy it builds, never a sandbox."""
        keep: deque[Event] = deque(maxlen=n)
        for event in self.read():
            if event.instance == instance_id or (
                    working_copy is not None and event.type.startswith("build.")
                    and event.data["working_copy"] == working_copy):
                keep.append(event)
        return list(keep)

    def tail(self, n: int = 50) -> list[Event]:
        keep: deque[Event] = deque(maxlen=n)
        keep.extend(self.read())
        return list(keep)

    # --- streaming -----------------------------------------------------------------
    def subscribe(self) -> Subscriber:
        sub = Subscriber()
        with self._lock:
            self._subscribers.append(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        with self._lock:
            if sub in self._subscribers:
                self._subscribers.remove(sub)
        sub.close()
