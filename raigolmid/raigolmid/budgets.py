"""What a project should take, said by its own tab and held to by the janitor.

A body's tab knows what its project needs — how large its build caches and its output may
grow, how much memory its builds and runs take — and nothing else on the machine does. So the
tab says it, in its body.toml (`[budget]`: `caches`, `output`, `memory`; `definitions.Budget`),
when its project first holds build caches or output: the daemon asks it once (`budget.unset`).
The disk and memory readings (`disk.py`, `memory.py`) hold each project to it, and a budget
passed is the janitor's (`budget.exceeded`): it judges whether the budget should grow to fit
what the project rightly holds, or the project should clear what it no longer needs, and tells
the tab which. The janitor never changes a project's files; the tab does.

The bodies asked are kept in `Paths.budget_asks`, so a tab is asked once, not at every start.
"""
from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING, Any

from .events import EventLog

if TYPE_CHECKING:
    from .session import Session

KINDS = {"caches": "its build caches", "output": "the other output its git ignores",
         "memory": "its sandbox's peak working memory"}


def gb(n: int) -> str:
    return f"{n / 1024 ** 3:.1f} GB"


def unset_message(body: str, held: dict[str, int]) -> str:
    holding = ", ".join(f"{KINDS[k]} {gb(v)}" for k, v in held.items())
    return (f"From the daemon: this project ({body}) now holds {holding}, and its body.toml "
            "sets no budget. Add a `[budget]` table to "
            f"/definitions/bodies/{body}/body.toml — `caches`, `output` and `memory`, each a "
            "size like \"4G\" — with what this project should take as it grows: a fresh build's "
            "caches with room for its next ones, the runs and snapshots it keeps, the memory "
            "its largest build or run needs. The janitor holds the project to it, and tells you "
            "when it is passed whether to clear something or to raise it.")


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
            try:
                asked = set(json.loads(self.path.read_text()))
            except FileNotFoundError:
                asked = set()
            if body in asked:
                return
            asked.add(body)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(sorted(asked)))
            tmp.replace(self.path)
        self.events.emit("budget.unset", tab=tab.tab_id, body=body, held=held, deliver={
            "content": unset_message(body, held), "meta": {"from": "daemon"}})

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
        tab = self.session.intent.body_tab(body)
        self.events.emit("budget.exceeded", tab=tab.tab_id if tab else None, body=body,
                         kind=kind, budget=budget, held=held,
                         message=(f"{body}'s {KINDS[kind]} is {gb(held)}, past the "
                                  f"{gb(budget)} its tab set"), **evidence)
