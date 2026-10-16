"""Faults in RaiGolmi itself, handed by the machine tab to the keeper that repairs RaiGolmi.

The keeper (`keeper/keeper.py`) is a Claude Code agent outside the VM. The machine tab, meeting
a RaiGolmi bug during a run, pauses the tabs it affects and files it with `report_fault`; each
fault is one JSON file under `Paths.faults`, named by its id, whose `status` is

    open       filed, not yet taken
    taken      a keeper session is working on it
    fixed      the keeper says the fix is in
    deferred   the keeper will take it up itself later, and says when and why in `answer`
    not_fixed  the keeper tried and could not fix it, and says why in `answer`

The keeper reaches the daemon over ssh through `rai keeper`: every probe of its loop says it
looked (`seen`, kept in `Paths.keeper`) and is answered with the faults open or taken; it
`take`s one before a session on it and `answer`s it with that session's verdict. A fault it
cannot work yet (the plan's usage, `keeper.py`) is answered `deferred` and stays waiting, so
nobody files it again. An answer is `fault.answered`, which the coordinator delivers to the
machine tab, or to whichever machine tab opens next when none is open; one handed over is
carried to its successor by the channel.

A keeper is reachable while it looked within `SEEN_SECONDS`, or is in a session it said may last
longer (`busy_seconds`), since its loop does not look while a session runs. `report_fault` says
when none is; and a fault still waiting when its keeper stops being reachable is said to the
machine tab once (`stranded`), since the tabs it paused for it would otherwise wait on an answer
nobody gives. Nobody is there to report to while a run goes on: what the keeper cannot fix the
machine tab works around.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .events import EventLog
from .intent import load_json, save_json
from .limits import plan_view
from .paths import Paths
from .session import SessionError

logger = logging.getLogger(__name__)

# The keeper looks every minute while the launcher runs the VM (keeper/keeper.py
# CHECK_SECONDS); five looks missed is a keeper that is gone, not one that is slow.
SEEN_SECONDS = 5 * 60
WAITING = ("open", "taken", "deferred")
VERDICTS = ("fixed", "not_fixed", "deferred")

# Every Faults reads and writes the same files; one daemon process holds them.
_lock = threading.Lock()


class Faults:
    def __init__(self, paths: Paths, events: EventLog) -> None:
        self.paths = paths
        self.events = events

    def _path(self, id: str) -> Path:
        return self.paths.faults / f"{id}.json"

    def get(self, id: str) -> dict[str, Any]:
        fault = load_json(self._path(id), f"fault {id}")
        if fault is None:
            raise SessionError(f"no fault {id!r}")
        return fault

    def all(self) -> list[dict[str, Any]]:
        if not self.paths.faults.is_dir():
            return []
        return sorted((self.get(p.stem) for p in self.paths.faults.glob("*.json")),
                      key=lambda f: f["reported_at"])

    def _save(self, fault: dict[str, Any]) -> None:
        save_json(self._path(fault["id"]), fault)

    # --- the machine tab ------------------------------------------------------------------
    def report(self, tab: str, title: str, body: str, affects: list[str]) -> dict[str, Any]:
        if not title.strip() or not body.strip():
            raise SessionError("a fault needs a `title` and a `body`: what failed, the error "
                               "as it was seen, and what it stops")
        now = time.time()
        fault = {"id": f"fault-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now))}-"
                       f"{uuid.uuid4().hex[:6]}",
                 "title": title.strip(), "body": body.strip(), "affects": list(affects),
                 "reported_by": tab, "reported_at": now, "status": "open", "taken_at": None,
                 "answered_at": None, "answer": None, "told": None, "stranded": None}
        with _lock:
            self._save(fault)
        self.events.emit("fault.reported", tab=tab, id=fault["id"], title=fault["title"],
                         affects=fault["affects"])
        logger.info("fault %s filed by %s: %s", fault["id"], tab, fault["title"])
        return fault

    def keeper_reachable(self, now: float) -> tuple[bool, str]:
        """Whether a keeper will take a fault, and in words, when it last looked."""
        seen = load_json(self.paths.keeper, "the keeper's last look")
        if seen is None:
            return False, "no keeper has ever looked at this machine"
        if now - seen["seen"] < SEEN_SECONDS or now < seen["busy_until"]:
            return True, f"the keeper last looked {round(now - seen['seen'])} s ago"
        return False, f"the keeper last looked {round((now - seen['seen']) / 60)} minutes ago"

    # --- the keeper -----------------------------------------------------------------------
    def seen(self, busy_seconds: float = 0) -> dict[str, Any]:
        """The keeper looked, and will not look again for up to `busy_seconds` (a session); it
        is answered with the faults waiting and the plan it paces them by."""
        now = time.time()
        with _lock:
            save_json(self.paths.keeper, {"seen": now, "busy_until": now + busy_seconds})
        return {"faults": [f for f in self.all() if f["status"] in WAITING],
                "plan": plan_view(load_json(self.paths.plan, "plan"), now)}

    def take(self, id: str, busy_seconds: float = 0) -> dict[str, Any]:
        with _lock:
            fault = self.get(id)
            if fault["status"] not in WAITING:
                raise SessionError(f"fault {id} is answered already ({fault['status']})")
            fault["status"], fault["taken_at"] = "taken", time.time()
            self._save(fault)
        self.seen(busy_seconds)
        self.events.emit("fault.taken", id=id)
        return fault

    def answer(self, id: str, verdict: str, words: str = "") -> dict[str, Any]:
        """The keeper's verdict, once: the same verdict again (an answer whose reply the keeper
        never got) is the fault as answered."""
        if verdict not in VERDICTS:
            raise SessionError(f"a verdict is one of {', '.join(VERDICTS)}, not {verdict!r}")
        if verdict == "not_fixed" and not words.strip():
            raise SessionError("`not_fixed` says in words what is wrong and what was tried")
        if verdict == "deferred" and not words.strip():
            raise SessionError("`deferred` says in words why and when the keeper takes it up")
        with _lock:
            fault = self.get(id)
            if fault["status"] == verdict:
                return fault
            if fault["status"] not in WAITING:
                raise SessionError(f"fault {id} is answered already ({fault['status']})")
            fault["status"], fault["answered_at"] = verdict, time.time()
            fault["answer"] = words.strip() or None
            fault["told"] = None
            self._save(fault)
        self.events.emit("fault.answered", id=id, verdict=verdict, answer=fault["answer"])
        logger.info("fault %s answered by the keeper: %s", id, verdict)
        return fault

    # --- the coordinator ------------------------------------------------------------------
    def told(self, id: str, machine_tab: str) -> None:
        with _lock:
            fault = self.get(id)
            fault["told"] = machine_tab
            self._save(fault)

    def untold(self) -> list[dict[str, Any]]:
        """Answered while no machine tab was open to hear it."""
        return [f for f in self.all() if f["status"] in VERDICTS and f["told"] is None]

    def stranded(self, now: float) -> tuple[list[dict[str, Any]], str]:
        """The faults still waiting while no keeper is reachable that the machine tab has not
        been told of, and when the keeper last looked."""
        reachable, when = self.keeper_reachable(now)
        if reachable:
            return [], when
        return [f for f in self.all() if f["status"] in WAITING and f["stranded"] is None], when

    def told_stranded(self, id: str, machine_tab: str) -> None:
        with _lock:
            fault = self.get(id)
            fault["stranded"] = machine_tab
            self._save(fault)


def _around(tabs: str) -> str:
    return (f"Work around it: whether the tabs you paused for it ({tabs}) can go on around it is "
            f"yours to decide; `resume` each that can. Record it in your faults log, and file it "
            f"again only with something new about it, since a fault filed again is worked from "
            f"the start.")


def answer_message(fault: dict[str, Any]) -> str:
    """The keeper's answer as the machine tab is told it."""
    tabs = ", ".join(fault["affects"]) or "none named"
    if fault["status"] == "fixed":
        return (f"The keeper fixed fault {fault['id']} ({fault['title']}): the fix is in. "
                f"`resume` the tabs you paused for it ({tabs}).")
    if fault["status"] == "deferred":
        return (f"The keeper deferred fault {fault['id']} ({fault['title']}): "
                f"{fault['answer']}\n\nIt stays filed and the keeper takes it up itself; do not "
                f"file it again. You are told its answer. " + _around(tabs))
    return (f"The keeper tried and could not fix fault {fault['id']} ({fault['title']}): "
            f"{fault['answer']}\n\n" + _around(tabs))


def stranded_message(fault: dict[str, Any], when: str) -> str:
    """A fault still waiting with no keeper reachable, as the machine tab is told it."""
    tabs = ", ".join(fault["affects"]) or "none named"
    return (f"From the daemon: no keeper is reachable for fault {fault['id']} "
            f"({fault['title']}) — {when} — so it is not being fixed. " + _around(tabs)
            + " A keeper that comes back takes it, and you are told its answer.")
