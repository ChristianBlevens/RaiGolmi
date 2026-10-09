"""One restart, then the janitor, for every container the daemon keeps running but the host.

A supervised container that exits on its own is restarted once, by its kind's restarter. If
the container that restart started exits on its own too, it is not restarted: its evidence
is written down and it goes to the janitor as `container.unfixable`, because restarting it
again would repeat the exit. A restart that fails goes there the same way.

**The budget is the container id.** Per unit — an agent's tab, a host surface's role, a
sandbox's body, view or anchor, the user's face, the door — this records the id of the
container its restart started, in `Paths.supervisor`, so a daemon restart keeps it. A dead
container with that id is spent. Any other was started by someone else — the user's restart,
the janitor's repairs, a rebuild, a swap, a selection, reconcile — and gets its one restart, so
nothing resets the budget.

**Exited on its own** is the runtime's answer, not the event's: every stop or replacement the
daemon makes removes the container or puts a new one under its name, so a `die` whose
container is still there, under the same id and `exited`, is one nobody asked for (a
force-remove's finds it `removing` or gone). That holds
only while the check is serialised with the unit's own starts and stops — its sandbox's queue,
or the session lock — which the caller arranges.

The evidence, the exit code and the container's last output, is written to `Paths.crashes` on
every exit on its own, before a restart removes the container.
"""
from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import labels
from .events import EventLog
from .intent import load_json, save_json
from .paths import Paths
from .runtime import ContainerInfo, ContainerRuntime

# Enough of a dead container's output to hold the error a process prints as it dies, under
# the screen of TUI redraws before it.
EVIDENCE_LINES = 400

HOST_SURFACES = frozenset({labels.Role.SELECTOR, labels.Role.CONTROL, labels.Role.NOTIFY,
                           labels.Role.CATALOG, labels.Role.WELCOME})
SANDBOX_PARTS = frozenset({labels.Role.BODY, labels.Role.VIEW, labels.Role.ANCHOR})
# Not the one-shots, which their caller runs to completion, nor the face tried off the user's
# screen, which is the machine tab's experiment (`Session.try_face`).
SUPERVISED = frozenset({labels.Role.AGENT, labels.Role.FACE, labels.Role.DOOR,
                        *HOST_SURFACES, *SANDBOX_PARTS})


class SupervisorError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Unit:
    """What is kept running: `kind` is its containers' role, and `name` the tab, host
    surface, sandbox or face it is that role of."""
    kind: str
    name: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.name}"

    @property
    def queue(self) -> str:
        """Where its exits are handled: a sandbox's with that sandbox's other container work,
        every other unit's on a queue of its own that nothing waits on."""
        return self.name if self.kind in SANDBOX_PARTS else self.key

    def scope(self) -> dict[str, str]:
        """What its events are about, as the event log files them."""
        if self.kind == labels.Role.AGENT:
            return {"tab": self.name}
        if self.kind in SANDBOX_PARTS:
            return {"instance": self.name}
        return {}


def unit_of(role: str, attributes: dict[str, str]) -> Unit | None:
    """The unit a runtime event's container is, from its labels; None for a role that is not
    kept running."""
    if role not in SUPERVISED:
        return None
    label = {labels.Role.AGENT: labels.TAB, labels.Role.FACE: labels.FACE,
             **{r: labels.INSTANCE for r in SANDBOX_PARTS}}.get(labels.Role(role))
    if label is None:
        return Unit(role, role)
    name = attributes.get(label)
    if not name:
        raise SupervisorError(f"a {role} container {attributes.get('name')!r} carries no "
                              f"{label} label, so there is no unit to keep running")
    return Unit(role, name)


@dataclass(frozen=True, slots=True)
class Exit:
    """A container that exited on its own, with its crash log's name in `Paths.crashes`: what
    `crash_logs` reads, and never a host path, which no container could open."""
    unit: Unit
    container: ContainerInfo
    evidence: str


class Supervisor:
    def __init__(self, runtime: ContainerRuntime, paths: Paths, events: EventLog,
                 prune: Callable[[], None]) -> None:
        self.runtime = runtime
        self.paths = paths
        self.events = events
        # Keeps the crash directory within its budget (`keep.py`) after each write.
        self._prune = prune
        self._lock = threading.Lock()
        self._started: dict[str, str] = load_json(paths.supervisor,
                                                  "the supervisor's record") or {}

    def exited(self, unit: Unit, container: str, container_id: str) -> Exit | None:
        """`container_id` is the one that died; None when the container named `container`
        was stopped or replaced rather than exiting on its own."""
        info = self.runtime.inspect(container)
        if info is None or info.id != container_id or info.status != "exited":
            return None
        evidence = self._evidence(unit, info)
        self.events.emit("container.exited", **unit.scope(), kind=unit.kind, unit=unit.name,
                         container=container, exit_code=info.exit_code,
                         evidence=evidence)
        return Exit(unit, info, evidence)

    def settle(self, exit: Exit, restart: Callable[[], str | None]) -> str:
        """The exit's one restart, or the janitor. `restart` starts the unit again and returns
        its new container's id, or None when the unit is no longer wanted; it raises when it
        cannot. Its failure is said here, since nobody waits on an exit to raise to."""
        name = exit.container.name
        if self._started.get(exit.unit.key) == exit.container.id:
            self._unfixable(exit, f"{name} exited on its own again after its restart; "
                                  "restarting it would repeat the exit")
            return "spent"
        try:
            started = restart()
        except Exception as exc:                       # noqa: BLE001 - said as unfixable
            self._unfixable(exit, f"{name} exited on its own and did not restart: "
                                  f"{type(exc).__name__}: {exc}")
            return "failed"
        if started is None:
            return "unwanted"
        with self._lock:
            self._started[exit.unit.key] = started
            save_json(self.paths.supervisor, self._started)
        self.events.emit("container.restarted", **exit.unit.scope(), kind=exit.unit.kind,
                         unit=exit.unit.name, container=name, started=started[:12])
        return "restarted"

    def handle(self, unit: Unit, container: str, container_id: str,
               restart: Callable[[], str | None]) -> str:
        exit = self.exited(unit, container, container_id)
        return "not_an_exit" if exit is None else self.settle(exit, restart)

    def running_id(self, container: str) -> str:
        """The id a restarter returns: the container it started, which is running."""
        info = self.runtime.inspect(container)
        if info is None or not info.running:
            raise SupervisorError(f"{container} is not running after its restart"
                                  + (f" (it is {info.status})" if info is not None else ""))
        return info.id

    def unfixable(self, unit: Unit, container: str, message: str,
                  **evidence: Any) -> None:
        """A unit the daemon could not bring back, for the janitor (`janitor.TAKEN`)."""
        self.events.emit("container.unfixable", **unit.scope(), kind=unit.kind,
                         unit=unit.name, container=container, **evidence, message=message)

    def _unfixable(self, exit: Exit, message: str) -> None:
        self.unfixable(exit.unit, exit.container.name, message,
                       exit_code=exit.container.exit_code, evidence=exit.evidence)

    def _evidence(self, unit: Unit, info: ContainerInfo) -> str:
        """Docker refusing the logs is recorded as the answer (`diagnostic_log`): this exists
        to explain a failure and may not fail on it. Named for the container that died, so
        two exits a second apart keep two logs."""
        output = self.runtime.diagnostic_log(info.id, tail=EVIDENCE_LINES)
        safe = re.sub(r"[^A-Za-z0-9.@_-]", "_", unit.name)
        self.paths.crashes.mkdir(parents=True, exist_ok=True)
        log = (self.paths.crashes / f"{unit.kind}-{safe}-{time.strftime('%Y%m%dT%H%M%S')}"
                                    f"-{info.id[:12]}.log")
        log.write_text(f"exit code: {info.exit_code}\n{output}")
        self._prune()
        return log.name
