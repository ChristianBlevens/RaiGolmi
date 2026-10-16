"""What a regular run of a project holds, said by its own tab: a tripwire that keeps the tab
on top of what its project accumulates.

A budget is not a ceiling with room to spare. It is the range a regular run of the project
stays within when nothing is duplicated or stale — this build's caches, the runs and snapshots
the work still cites, the memory its largest build or run takes — set close to that. Passing it
is never trusted and never an error: it is the moment the tab looks, and either the excess is
what the work now regularly needs, so the tab adjusts the budget to the new range, or it is
leftovers — old builds, superseded runs, copies — and the tab clears back within it.

The tab says it in its body.toml (`[budget]`: `caches`, `output`, `memory`, `cpu`;
`definitions.Budget`) when its project first holds build caches or output: the daemon asks it
once (`budget.unset`), and the asks are kept in `Paths.budget_asks`. The disk and memory
readings (`disk.py`, `memory.py`) hold each project to it; a budget passed is delivered to the
project's own tab to check (`budget.exceeded`), and is the janitor's only when the project has
no tab to check it (`budget.untended`). Neither ever changes a project's files for it, and
nothing holds a sandbox to a budget: no container has a CPU or memory limit (the owner's call).
`cpu` is the whole machine unless the tab says less, which matters only when projects run side
by side.
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from .events import EventLog
from .intent import load_json, save_json

if TYPE_CHECKING:
    from .session import Session

KINDS = {"caches": "build caches", "output": "other git-ignored output",
         "memory": "peak working memory", "cpu": "share of the machine's CPU"}


def gb(n: int) -> str:
    """A size in GB, or in MB under one, so a budget of a few MB never reads as 0.0 GB."""
    return f"{n / 1024 ** 3:.1f} GB" if n >= 1024 ** 3 else f"{n / 1024 ** 2:.1f} MB"


def amount(kind: str, n: int) -> str:
    return f"{n}%" if kind == "cpu" else gb(n)


def unset_message(body: str, held: dict[str, int], toml: str) -> str:
    holding = ", ".join(f"{KINDS[k]} {gb(v)}" for k, v in held.items())
    return (f"From the daemon: this project ({body}) now holds {holding}, and its body.toml "
            "sets no budget. A budget is the range a regular run of this project stays within "
            "with nothing duplicated or stale, so you notice when it accumulates — not a "
            "ceiling with room to spare. Measure what a regular run keeps (this build's caches, "
            "the runs and snapshots the work still cites, the memory its largest build or run "
            "takes), clear what is stale first, and set each close to that in a `[budget]` "
            f"table in {toml}: `caches`, `output`, `memory`, "
            "each a size like \"2G\"; `cpu`, a share of the machine like \"50%\", is the "
            "whole machine unless you set less for projects run side by side. When one is passed you are told, and check: if the excess "
            "is what the work now regularly needs, set the budget to that new range; if it is "
            "leftovers, clear back within it.")


def holding(kind: str, evidence: dict[str, Any]) -> str:
    """What the reading found holding `kind`, largest first, paths within the project: the
    caches with their tools, the output's largest paths. Memory is read per project, so it
    names nothing smaller."""
    if kind == "caches":
        parts = [(f"`{c['path']}`" + (f" ({c['tool']})" if c["tool"] else ""), c["bytes"])
                 for c in sorted(evidence["caches"], key=lambda c: -c["bytes"])]
    elif kind == "output":
        parts = [(f"`{o['path']}`", o["bytes"]) for o in evidence["largest_output"]]
    else:
        return ""
    if not parts:
        return ""
    return " It is held by " + ", ".join(f"{p} {gb(b)}" for p, b in parts) + "."


def exceeded_message(kind: str, budget: int, held: int, toml: str, holders: str) -> str:
    if kind == "cpu":
        return (f"From the daemon: this project's {KINDS[kind]} came to {held}% over a minute, "
                f"past the {budget}% budget set for a regular run. Check what is running "
                f"(`memory` reads it again, `jobs` lists your jobs): if it is what the work now "
                f"regularly needs, set `cpu` in {toml} to that new range; if it is something "
                "left running — a job, a watcher, a loop — stop it. Say which you did.")
    reads = "memory" if kind == "memory" else "disk"
    return (f"From the daemon: this project's {KINDS[kind]} came to {gb(held)}, past the "
            f"{gb(budget)} budget set for a regular run.{holders} Check what grew (`{reads}` "
            f"reads it again): if it is what the work now regularly needs, set `{kind}` in "
            f"{toml} to that new "
            "range; if it is leftovers — old builds, superseded runs, copies — clear them "
            "back within the budget (a build cache with its own tool, once anything the project "
            "needs is moved out of it). Say which you did.")


class Budgets:
    def __init__(self, session: "Session", events: EventLog) -> None:
        self.session = session
        self.events = events
        self.path = session.paths.budget_asks
        self._lock = threading.Lock()

    def ask_once(self, body: str, held: dict[str, int]) -> None:
        """A project holding something and with no budget: its tab asked to set one, once."""
        definition = self.session.catalogue.bodies.get(body)
        tab = self.session.intent.body_tab(body)
        if definition is None or definition.budget is not None or tab is None:
            return
        with self._lock:
            asked = set(load_json(self.path, "the projects asked for a budget") or [])
            if body in asked:
                return
            asked.add(body)
            save_json(self.path, sorted(asked))
        self.events.emit("budget.unset", tab=tab.tab_id, body=body, held=held, deliver={
            "content": unset_message(body, held, self._toml(definition)),
            "meta": {"from": "daemon"}})

    def _toml(self, definition) -> str:
        """The body's body.toml as its tab sees it, under /definitions."""
        within = (definition.directory / "body.toml").relative_to(
            self.session.definitions_root())
        return f"/definitions/{within}"

    def over(self, body: str, held: dict[str, int]) -> dict[str, tuple[int, int]]:
        """Each kind in `held` past the body's budget: (budget, held)."""
        definition = self.session.catalogue.bodies.get(body)
        budget = definition.budget if definition is not None else None
        if budget is None:
            return {}
        return {k: (getattr(budget, k), v) for k, v in held.items()
                if getattr(budget, k) is not None and v > getattr(budget, k)}

    def say_over(self, body: str, kind: str, budget: int, held: int,
                 **evidence: Any) -> None:
        """To the project's tab, which checks it; the janitor's only with no tab to."""
        tab = self.session.intent.body_tab(body)
        said = (f"{body}'s {KINDS[kind]} came to {amount(kind, held)}, past the "
                f"{amount(kind, budget)} budget")
        if tab is None:
            self.events.emit("budget.untended", body=body, kind=kind, budget=budget,
                             held=held, message=said + ", and it has no tab to check it",
                             **evidence)
            return
        self.events.emit("budget.exceeded", tab=tab.tab_id, body=body, kind=kind,
                         budget=budget, held=held, message=said, **evidence, deliver={
                             "content": exceeded_message(
                                 kind, budget, held,
                                 self._toml(self.session.catalogue.bodies[body]),
                                 holding(kind, evidence)),
                             # The newest reading replaces one still queued (`channel.py`).
                             "meta": {"from": "daemon", "standing": f"budget {kind}"}})
