"""Persisted intent.

Two kinds of state, two homes. This file is the first kind: the selection, the tabs and the
bodies they are on, the tab→instance mapping, the next tab id. It cannot be derived from anything, so it is
written to disk. Everything about what is *actually running* is the other kind and is
never persisted — it is re-derived from the container runtime at startup, because after a
crash the runtime is the only thing that knows the truth (principle 9).

Getting that split wrong is the failure this module exists to prevent: a stored inventory
of containers goes stale the instant the daemon dies.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

from . import naming
from .compatibility import Selection

AgentStatus = Literal["running", "crashed", "exited", "starting"]

# The manager tab: scoped to the machine rather than a sandbox, so it is neither the
# machine tab nor a body's, and never one of the tabs the user works in.
MANAGER = "manager"


@dataclass(slots=True)
class TabIntent:
    """One AI-terminal tab. `body` is the body a body tab is equipped to until it
    closes; None is the machine tab (and the manager, told apart by its id)."""
    tab_id: str
    body: str | None = None
    status: AgentStatus = "starting"
    # Between a prompt and the agent's answer to it, as its own hooks report.
    busy: bool = False
    # Restarted, and its session has not yet reported up (SessionStart): its channel is not
    # heard until it has (`channel.py`).
    awaiting_session: bool = False
    # A body tab the user handed the machine tab to coordinate.
    managed: bool = False
    # Its handover to a fresh conversation (`coordinator.py`): None, or "asked" (the push
    # asking it to make its documents ready is on its way), "heard" (that turn started),
    # "ready" (the documents are ready for the next conversation), or, for the machine tab,
    # "unanswered" (that turn ended without saying ready).
    handover: str | None = None
    # The user's words for where a managed tab stops for them — a goal, or a decision that is
    # theirs — given when they hand it over; and, once the machine tab finds it reached, the
    # situation it is held on until they answer in it.
    stop_when: str | None = None
    held: str | None = None
    # When the user's time for a managed tab runs out (epoch seconds): the daemon then has it
    # make its documents ready and gives it back (`coordinator.py`).
    until: float | None = None
    # The tab whose work this one took over in a fresh conversation (`Session.succeed_tab`).
    continues: str | None = None

    @property
    def manager(self) -> bool:
        return self.tab_id == MANAGER

    @property
    def machine(self) -> bool:
        return self.body is None and not self.manager


@dataclass(slots=True)
class InstanceIntent:
    """Why an instance exists. `refs` is the whole lifetime rule: an instance runs
    while something references it, and reference counting gives the same answers after a
    restart because this is on disk."""
    instance_id: str
    body: str
    toolbelt: str | None
    working_copy: str
    branch: str
    refs: list[str] = field(default_factory=list)

    def referenced(self) -> bool:
        return bool(self.refs)


@dataclass(slots=True)
class StopRecord:
    """The agents running when the daemon last stopped, and the boot they ran in. One
    recorded here and not running in a later boot was ended by the machine going down, not
    by a crash."""
    boot: str
    running_tabs: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Run:
    """The machine tab managing tabs for the user, from the first tab handed to it until it
    reports on the run; `ended` is when the last was given back, None while any is managed.
    `asked` is when the machine tab heard the daemon's request for the report, which carries
    the daemon's own record of the run; a report is taken only after it."""
    started: float
    ended: float | None = None
    asked: float | None = None


@dataclass(slots=True)
class Intent:
    epoch: int = 0
    selection: Selection = field(default_factory=Selection)
    instances: dict[str, InstanceIntent] = field(default_factory=dict)
    tabs: dict[str, TabIntent] = field(default_factory=dict)
    stopped: StopRecord | None = None
    # Tab ids are never reused: an id in the history or `git log` names one tab.
    next_tab: int = 1
    # The user's switch in the drawer: False is "don't drive my current face".
    face_driving: bool = True
    run: Run | None = None

    # --- references -----------------------------------------------------------------
    def add_ref(self, instance_id: str, ref: str) -> None:
        inst = self.instances[instance_id]
        if ref not in inst.refs:
            inst.refs.append(ref)

    def drop_ref(self, instance_id: str, ref: str) -> None:
        inst = self.instances.get(instance_id)
        if inst is not None and ref in inst.refs:
            inst.refs.remove(ref)

    def unreferenced(self) -> list[str]:
        return [i for i, inst in self.instances.items() if not inst.referenced()]

    def tab_for_instance(self, instance_id: str) -> TabIntent | None:
        inst = self.instances.get(instance_id)
        for tab in self.tabs.values():
            if inst is not None and naming.tab_ref(tab.tab_id) in inst.refs:
                return tab
        return None

    def sandbox_of(self, tab_id: str) -> str | None:
        ref = naming.tab_ref(tab_id)
        return next((i for i, inst in self.instances.items() if ref in inst.refs), None)

    def machine_tab(self) -> TabIntent | None:
        return next((t for t in self.tabs.values() if t.machine), None)

    def hands_off(self, tab_id: str) -> bool:
        """Whether the user has handed this tab's work over, so nothing it does waits on them:
        a tab the machine tab manages, or the machine tab while it manages any."""
        tab = self.tabs.get(tab_id)
        return tab is not None and (tab.managed or tab.machine and any(
            t.managed for t in self.tabs.values()))

    def body_tab(self, body: str) -> TabIntent | None:
        return next((t for t in self.tabs.values() if t.body == body), None)

    def face_tab(self) -> TabIntent | None:
        """The tab whose sandbox the face is on: the selected body's, or the machine
        tab's with no body selected."""
        body = self.selection.body
        return self.body_tab(body) if body is not None else self.machine_tab()

    @property
    def focused_instance(self) -> str | None:
        """The active sandbox, derived: the face tab's sandbox, if it has one open."""
        tab = self.face_tab()
        return self.sandbox_of(tab.tab_id) if tab is not None else None

    def new_tab_id(self) -> str:
        tab_id = f"tab-{self.next_tab}"
        self.next_tab += 1
        return tab_id

    # --- persistence ----------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "epoch": self.epoch,
            "selection": asdict(self.selection),
            "instances": {k: asdict(v) for k, v in self.instances.items()},
            # `busy` and `awaiting_session` are the running agent's to say, never stored.
            "tabs": {k: {f: x for f, x in asdict(v).items()
                         if f not in ("busy", "awaiting_session")}
                     for k, v in self.tabs.items()},
            "stopped": asdict(self.stopped) if self.stopped is not None else None,
            "next_tab": self.next_tab,
            "face_driving": self.face_driving,
            "run": asdict(self.run) if self.run is not None else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Intent":
        return cls(
            epoch=d.get("epoch", 0),
            selection=Selection(**d.get("selection", {})),
            instances={k: InstanceIntent(**v) for k, v in d.get("instances", {}).items()},
            tabs={k: TabIntent(**v) for k, v in d.get("tabs", {}).items()},
            stopped=StopRecord(**d["stopped"]) if d.get("stopped") else None,
            next_tab=d.get("next_tab", 1),
            face_driving=d.get("face_driving", True),
            run=Run(**d["run"]) if d.get("run") else None,
        )


def load_json(path: Path, what: str) -> Any | None:
    """A state file the daemon wrote with `save_json`; None when there is none yet. One that
    does not parse is moved aside, never discarded, and refused loudly."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BrokenState(path, what, exc) from exc


class BrokenState(RuntimeError):
    """A state file that is not what it should be, moved to `.broken` for the operator."""

    def __init__(self, path: Path, what: str, exc: Exception) -> None:
        broken = path.with_suffix(path.suffix + ".broken")
        path.replace(broken)
        super().__init__(f"{path} is not readable {what} ({exc}); moved to {broken}. "
                         "Start again without it or repair that file.")


def save_json(path: Path, data: Any) -> None:
    """Atomic because a half-written state file after a power cut loses state that cannot be
    re-derived: a temp file in the same directory, fsynced, renamed over the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


TAB_NAME = re.compile(r"^tab-(\d+)(?:-|$)")


class IntentStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self, spent: Iterable[str] = ()) -> Intent:
        """`spent` names what outlives the intent — agent homes and archives, `tab-N[-…]`. A
        fresh intent numbers its tabs past them, so an id never names two tabs even
        when the intent before it was removed or refused."""
        raw = load_json(self.path, "intent")
        if raw is None:
            used = [int(m.group(1)) for m in map(TAB_NAME.match, spent) if m]
            return Intent(next_tab=max(used, default=0) + 1)
        try:
            return Intent.from_dict(raw)
        except (TypeError, KeyError) as exc:
            raise BrokenState(self.path, "intent", exc) from exc

    def save(self, intent: Intent) -> None:
        save_json(self.path, intent.to_dict())
