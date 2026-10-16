"""The machine's memory and CPU, by project, held to what each project's tab said it needs.

The VM has a fixed memory and no swap, so two heavy builds at once can stall the machine or
have the kernel kill one. Every `SAMPLE_SECONDS` each sandbox's working memory — its toolbelt
and its body, anonymous pages only, never the page cache a build reads through — is read from
its containers' cgroups (`ContainerRuntime.use`), with the machine's available memory and the
CPU time each has used. Over each `WINDOW_SAMPLES` the peak per project is held to its tab's
`[budget] memory`, and its share of the machine's CPU over the window to `[budget] cpu`
(`budgets.py`): passed is `budget.exceeded`, once per episode of each. A process the kernel killed for
want of memory is `memory.oom_killed`, each time; the machine's available memory under
`SHORT_FRACTION` is `memory.short`, once per episode. The janitor takes all three
(`janitor.py`) and reads the last window with `memory`.

The readings are in memory; a daemon restart starts every peak and count again.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import labels, naming
from .budgets import Budgets
from .definitions import Budget
from .events import EventLog
from .runtime.base import RuntimeError_

if TYPE_CHECKING:
    from .session import Session

# How often memory is read, and over how many readings a project's peak is judged. A build's
# peak lasts seconds to minutes; these set how fine a peak is seen, never whether one is.
SAMPLE_SECONDS = 10.0
WINDOW_SAMPLES = 6
# How little of the machine's memory may be available before the janitor looks.
SHORT_FRACTION = 0.10
MEMINFO = Path("/proc/meminfo")
PARTS = (labels.Role.VIEW, labels.Role.BODY)


def machine() -> dict[str, int]:
    """Total and available memory, in bytes."""
    info = {k: int(v.split()[0]) * 1024 for k, _, v in
            (line.partition(":") for line in MEMINFO.read_text().splitlines())
            if k in ("MemTotal", "MemAvailable")}
    return {"total": info["MemTotal"], "available": info["MemAvailable"]}


class Memory:
    def __init__(self, session: "Session", events: EventLog) -> None:
        self.session = session
        self.events = events
        self.budgets = Budgets(session, events)
        self._lock = threading.Lock()
        self._peaks: dict[str, int] = {}             # body -> peak this window
        self._last: dict[str, Any] = {}              # the last window, for `memory`
        self._kills: dict[str, int] = {}             # container id -> OOM kills seen
        self._samples = 0
        self._over: set[tuple[str, str]] = set()     # (body, kind) said past budget this episode
        self._cpu_seen: dict[str, int] = {}          # container id -> CPU µs at its last read
        self._cpu_used: dict[str, int] = {}          # body -> CPU µs used this window
        self._window_start = time.monotonic()
        self._short = False

    def run(self, stop: threading.Event) -> None:
        while not stop.wait(SAMPLE_SECONDS):
            self.sample()

    def sample(self) -> None:
        working: dict[str, int] = {}
        unread: list[str] = []
        for container in self.session.runtime.list(labels.managed_filter()):
            if container.labels.get(labels.ROLE) not in PARTS or not container.running:
                continue
            body, _ = naming.split(container.labels[labels.INSTANCE])
            body = body or naming.WORK          # the machine tab's sandbox
            try:
                use = self.session.runtime.use(container.id)
            except RuntimeError_ as exc:
                unread.append(f"{container.name}: {exc}")
                continue
            working[body] = working.get(body, 0) + use.working
            before = self._cpu_seen.get(container.id)
            self._cpu_seen[container.id] = use.cpu_usec
            if before is not None and use.cpu_usec >= before:
                self._cpu_used[body] = self._cpu_used.get(body, 0) + use.cpu_usec - before
            seen = self._kills.get(container.id)
            self._kills[container.id] = use.oom_kills
            if seen is not None and use.oom_kills > seen:
                tab = self.session.intent.body_tab(body)
                self.events.emit("memory.oom_killed", tab=tab.tab_id if tab else None,
                                 body=body, container=container.name,
                                 kills=use.oom_kills - seen, working_now=use.working)
        host = machine()
        short = host["available"] < SHORT_FRACTION * host["total"]
        if short and not self._short:
            self.events.emit("memory.short", **host, working=working, unread=unread)
        self._short = short
        with self._lock:
            for body, held in working.items():
                self._peaks[body] = max(self._peaks.get(body, 0), held)
            self._samples += 1
            if self._samples < WINDOW_SAMPLES:
                return
            peaks, self._peaks, self._samples = self._peaks, {}, 0
            now = time.monotonic()
            whole = (now - self._window_start) * 1e6 * (os.cpu_count() or 1)
            cpu = {body: round(100 * used / whole) for body, used in self._cpu_used.items()}
            self._cpu_used, self._window_start = {}, now
            self._last = {"peaks": peaks, "cpu": cpu, "machine": host, "unread": unread}
        for kind, reading in (("memory", peaks), ("cpu", cpu)):
            for body, held in reading.items():
                past = self.budgets.over(body, {kind: held})
                if not past:
                    self._over.discard((body, kind))
                elif (body, kind) not in self._over:
                    self._over.add((body, kind))
                    budget, held = past[kind]
                    self.budgets.say_over(body, kind, budget, held, machine=host,
                                          peaks=peaks, cpu=cpu)

    def of_body(self, body: str) -> dict[str, Any]:
        """A body tab's own `memory`: its project's peak and CPU share over the last window,
        its budgets, and the machine's memory now."""
        whole = self.accounted()
        return {"peak": whole.get("peaks", {}).get(body), "budget": whole["budgets"].get(body),
                "cpu": whole.get("cpu", {}).get(body),
                "cpu_budget": whole["cpu_budgets"].get(body),
                "machine": whole["machine"], "window_seconds": whole["window_seconds"]}

    def accounted(self) -> dict[str, Any]:
        """The janitor's `memory`: each project's peak working memory and its share of the
        machine's CPU (in percent) over the last window, their budgets, and the machine's
        memory now."""
        with self._lock:
            last = dict(self._last)
        bodies = self.session.catalogue.bodies.values()
        budgets = {b.id: b.budget.memory if b.budget else None for b in bodies}
        cpu_budgets = {b.id: (b.budget or Budget()).cpu for b in bodies}
        return {**last, "machine": machine(), "budgets": budgets, "cpu_budgets": cpu_budgets,
                "window_seconds": SAMPLE_SECONDS * WINDOW_SAMPLES}
