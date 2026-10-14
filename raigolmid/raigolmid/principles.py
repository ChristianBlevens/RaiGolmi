"""Whether a body's work keeps to the principles its project names.

A body names the doc its work is judged against in body.toml (`[principles] doc`); one that
names none is never asked. A rule only written down drifts, so the judging is asked for at the
moment work lands: when a body tab's turn ends with its working copy's HEAD moved past what was
last declared, its Stop hook asks it (`owed`, folded into `agent_stop.decide`'s question) to
declare each new commit — what it was for, what it was judged by, and whether that holds — and
any work set aside or reverted, with why (`declare`). HEAD is read on the host, as `stalls.py`
reads it. A body's first reading is where its declarations start: nothing is owed for the
history before it.

The rows go to the body's ledger (`Paths.principles_ledger`) beside every direction the machine
tab sends that body's tabs, so the machine tab reads its own steering next to the work across
its own handovers. Every `CHECK_TURNS` managed turn-ends with declarations since the last check,
the machine tab is asked to read them against the doc (`principles.check_asked`) and to say what
it found (`checked`); every `AUDIT_SESSIONS` managed handovers with declarations since the last
audit, it is asked for a deeper one (`principles.audit_asked`). When nothing moved, nothing is
asked. History shows a declaration that does not hold and every check.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import git
from .events import Event, EventLog
from .intent import load_json, save_json

if TYPE_CHECKING:
    from .session import Session

CHECK_TURNS = 5
AUDIT_SESSIONS = 10
# Commits asked about at one turn's end; the rest are asked at the next.
ASKED_MOST = 20
DECLARATION = ("commit", "for", "judged_by", "holds", "note")
SET_ASIDE = ("what", "why")
DIRECTED = "coordinator.directed"


class PrinciplesError(Exception):
    pass


class Principles:
    def __init__(self, session: "Session", events: EventLog) -> None:
        self.session = session
        self.events = events
        self.paths = session.paths
        self._lock = threading.Lock()
        saved = load_json(self.paths.principles, "the principles checks") or {
            "bodies": {}, "turns": 0, "sessions": 0, "unchecked": {}, "unaudited": {}}
        self._bodies: dict[str, dict[str, Any]] = saved["bodies"]
        self._turns: int = saved["turns"]
        self._sessions: int = saved["sessions"]
        # Declarations since the machine tab last checked, and last audited, by body.
        self._unchecked: dict[str, int] = saved["unchecked"]
        self._unaudited: dict[str, int] = saved["unaudited"]
        # Bodies whose working copy could not be read, said once until it can be again.
        self._unread: set[str] = set()

    def _save(self) -> None:
        """Under `_lock`."""
        save_json(self.paths.principles, {
            "bodies": self._bodies, "turns": self._turns, "sessions": self._sessions,
            "unchecked": self._unchecked, "unaudited": self._unaudited})

    def _append(self, body: str, rows: list[dict[str, Any]]) -> None:
        path = self.paths.principles_ledger(body)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            for row in rows:
                f.write(json.dumps({"at": time.time(), **row}) + "\n")

    def _rows(self, body: str) -> list[dict[str, Any]]:
        path = self.paths.principles_ledger(body)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def _doc(self, body: str) -> Path | None:
        definition = self.session.catalogue.bodies.get(body)
        if definition is None or definition.principles is None:
            return None
        return definition.source_root / definition.principles

    # --- a body tab's ----------------------------------------------------------------
    def _owing(self, tab_id: str) -> tuple[str, str, Path, list[tuple[str, str]], list[str]] | None:
        """The body, its HEAD, its doc, the commits since what was declared that are not yet
        declared (oldest first, with subjects), and the commits HEAD no longer holds."""
        tab = self.session.intent.tabs.get(tab_id)
        if tab is None or tab.body is None or tab.machine:
            return None
        body = tab.body
        doc = self._doc(body)
        if doc is None:
            return None
        repo = self.session.working_copy(tab_id)
        try:
            if repo is None or not git.is_repo(repo):
                raise git.GitError(f"{repo} is not a git repository")
            head = git.run(["rev-parse", "--verify", "HEAD"], repo).stdout.strip()
            with self._lock:
                state = self._bodies.get(body)
                if state is None:
                    self._bodies[body] = {"checked": head, "declared": []}
                    self._save()
                    return None
                checked, declared = state["checked"], set(state["declared"])
            if head == checked:
                self._unread.discard(body)
                return None
            logged = git.run(["log", "--reverse", "--format=%H %s", f"{checked}..{head}"],
                             repo).stdout.splitlines()
            left = git.run(["rev-list", f"{head}..{checked}"], repo).stdout.split()
        except git.GitError as exc:
            # Said, never raised: this is asked as a turn ends, which must still end.
            if body not in self._unread:
                self._unread.add(body)
                self.events.emit("principles.unread", tab=tab_id, body=body, error=str(exc))
            return None
        self._unread.discard(body)
        new = [(sha, subject) for sha, _, subject in (line.partition(" ") for line in logged)
               if sha not in declared]
        if not new and not left:
            with self._lock:
                self._bodies[body] = {"checked": head, "declared": []}
                self._save()
            return None
        return body, head, doc, new, left

    def owed(self, tab_id: str) -> dict[str, Any] | None:
        """What the tab's Stop hook asks it to declare, or None."""
        owing = self._owing(tab_id)
        if owing is None:
            return None
        _, _, doc, new, left = owing
        return {"doc": str(doc),
                "commits": [{"commit": sha, "subject": subject}
                            for sha, subject in new[:ASKED_MOST]],
                "more": max(0, len(new) - ASKED_MOST), "left": left[:ASKED_MOST]}

    def declare(self, tab_id: str, commits: list[dict[str, Any]],
                set_aside: list[dict[str, str]] | None = None) -> dict[str, Any]:
        set_aside = set_aside or []
        tab = self.session.intent.tabs[tab_id]
        if tab.body is None or self._doc(tab.body) is None:
            raise PrinciplesError("this tab's body names no principles doc ([principles] doc in "
                               "its body.toml), so nothing is declared against one")
        owing = self._owing(tab_id)
        if owing is None and not set_aside:
            raise PrinciplesError("nothing of yours is owed a declaration: HEAD has not moved "
                               "since your last one")
        new = [] if owing is None else owing[3]
        left = [] if owing is None else owing[4]
        if left and not set_aside:
            raise PrinciplesError(f"HEAD no longer holds {len(left)} commit(s) it had "
                               f"({', '.join(s[:10] for s in left[:5])}): say in `set_aside` "
                               "what was set aside or reverted, and why")
        rows: list[dict[str, Any]] = []
        drift: list[dict[str, str]] = []
        for entry in commits:
            if set(entry) - set(DECLARATION) or set(DECLARATION[:4]) - set(entry):
                raise PrinciplesError(f"a commit is declared as {{{', '.join(DECLARATION)}}} "
                                   f"(`note` optional when it holds): got {sorted(entry)}")
            if not isinstance(entry["holds"], bool):
                raise PrinciplesError("`holds` is true or false")
            if not str(entry["for"]).strip() or not str(entry["judged_by"]).strip():
                raise PrinciplesError("`for` and `judged_by` each need words: what the commit was "
                                   "for, and what it was judged by")
            note = str(entry.get("note") or "").strip()
            if not entry["holds"] and not note:
                raise PrinciplesError("a commit that does not hold says in `note` what comes next")
            matched = [sha for sha, _ in new if len(str(entry["commit"])) >= 7
                       and sha.startswith(str(entry["commit"]))]
            if len(matched) != 1:
                raise PrinciplesError(f"{entry['commit']!r} is not one commit owed a declaration; "
                                   "owed: " + (", ".join(sha[:10] for sha, _ in new) or "none"))
            rows.append({"kind": "commit", "tab": tab_id, "commit": matched[0],
                         "for": entry["for"], "judged_by": entry["judged_by"],
                         "holds": entry["holds"], "note": note})
            if not entry["holds"]:
                drift.append({"commit": matched[0][:10], "note": note})
        for entry in set_aside:
            if set(entry) != set(SET_ASIDE) or not all(str(v).strip() for v in entry.values()):
                raise PrinciplesError("work set aside is declared as {what, why}, each with words")
            rows.append({"kind": "set_aside", "tab": tab_id, **entry})
        body = tab.body
        with self._lock:
            state = self._bodies[body]
            declared = set(state["declared"]) | {r["commit"] for r in rows if r["kind"] == "commit"}
            remaining = [sha for sha, _ in new if sha not in declared]
            if owing is not None and not remaining:
                self._bodies[body] = {"checked": owing[1], "declared": []}
            else:
                state["declared"] = sorted(declared)
            self._unchecked[body] = self._unchecked.get(body, 0) + len(rows)
            self._unaudited[body] = self._unaudited.get(body, 0) + len(rows)
            self._append(body, rows)
            self._save()
        self.events.emit("principles.declared", tab=tab_id, body=body,
                         commits=len(rows) - len(set_aside), set_aside=len(set_aside),
                         drift=drift)
        return {"declared": len(rows), "still_owed": [sha[:10] for sha in remaining]}

    # --- the machine tab's -----------------------------------------------------------
    def ledger(self, body: str, rows: int = 60) -> dict[str, Any]:
        """The body's doc, and its ledger since the machine tab's last check."""
        doc = self._doc(body)
        if doc is None:
            raise PrinciplesError(f"{body} names no principles doc ([principles] doc in its "
                               "body.toml)")
        every = self._rows(body)
        last = max((i for i, r in enumerate(every) if r["kind"] in ("check", "audit")),
                   default=-1)
        since = every[last + 1:]
        return {"body": body, "doc_path": str(doc), "doc": doc.read_text(),
                "last_check": every[last] if last >= 0 else None,
                "since_check": since[-rows:], "omitted": max(0, len(since) - rows)}

    def checked(self, body: str, tabs: str, drift: str, direction: str = "",
                deep: bool = False) -> dict[str, Any]:
        if self._doc(body) is None:
            raise PrinciplesError(f"{body} names no principles doc ([principles] doc in its "
                               "body.toml)")
        if not drift.strip():
            raise PrinciplesError("`drift` is \"none\" or names what drifted")
        if drift.strip().lower() != "none" and not direction.strip():
            raise PrinciplesError("drift found says in `direction` what you sent to correct it")
        row = {"kind": "audit" if deep else "check", "tabs": tabs, "drift": drift.strip(),
               "direction": direction.strip()}
        with self._lock:
            self._unchecked[body] = 0
            if deep:
                self._unaudited[body] = 0
            self._append(body, [row])
            self._save()
        self.events.emit("principles.checked", body=body, tabs=tabs, drift=row["drift"],
                         direction=row["direction"], deep=deep)
        return {"recorded": row["kind"]}

    # --- watching --------------------------------------------------------------------
    def on_event(self, event: Event) -> None:
        if event.tab is None:
            return
        intent = self.session.intent
        if event.type == DIRECTED:
            tab = intent.tabs.get(event.tab)
            if tab is not None and tab.body is not None and self._doc(tab.body) is not None:
                with self._lock:
                    self._append(tab.body, [{"kind": "direction", "tab": event.tab,
                                             "content": event.data["deliver"]["content"]}])
            return
        if event.type == "agent.idle":
            tab = intent.tabs.get(event.tab)
            if tab is None or not tab.managed or tab.machine:
                return
            with self._lock:
                self._turns += 1
                due = self._turns >= CHECK_TURNS
                if due:
                    self._turns = 0
                bodies = sorted(b for b, n in self._unchecked.items() if n) if due else []
                self._save()
            if bodies:
                self._ask("principles.check_asked", bodies, check_message(bodies))
            return
        if event.type == "tab.closed" and (successor := event.data.get("continued_by")):
            tab = intent.tabs.get(successor)
            if tab is None or not tab.managed:
                return
            with self._lock:
                self._sessions += 1
                due = self._sessions >= AUDIT_SESSIONS
                if due:
                    self._sessions = 0
                bodies = sorted(b for b, n in self._unaudited.items() if n) if due else []
                self._save()
            if bodies:
                self._ask("principles.audit_asked", bodies, audit_message(bodies))

    def _ask(self, event: str, bodies: list[str], content: str) -> None:
        machine = self.session.intent.machine_tab()
        if machine is None:
            return
        self.events.emit(event, tab=machine.tab_id, bodies=bodies, deliver={
            "content": content, "meta": {"from": "daemon"}})

    def run(self, stop: threading.Event) -> None:
        sub = self.events.subscribe()
        try:
            while not stop.is_set():
                for event in sub.drain(timeout=1.0):
                    try:
                        self.on_event(event)
                    except Exception as exc:               # noqa: BLE001
                        # Said rather than raised: this thread ending would end the daemon.
                        self.events.emit("principles.watch_failed", tab=event.tab,
                                         error=f"{type(exc).__name__}: {exc}")
        finally:
            self.events.unsubscribe(sub)


def check_message(bodies: list[str]) -> str:
    return (f"From the daemon: {CHECK_TURNS} turns of the tabs you manage have ended since your "
            f"last principles check, with declarations to read for {', '.join(bodies)}. For "
            "each, call `principles` for its doc and what its tabs declared and you directed "
            "since the last check; read them against the doc — was each thing judged by what it "
            "was for, and did your own directions keep to it? Then call `principles_checked` "
            "with the body, the tabs and sessions read, `drift` (\"none\" or what drifted) and "
            "the `direction` you sent to correct it.")


def audit_message(bodies: list[str]) -> str:
    return (f"From the daemon: {AUDIT_SESSIONS} sessions of the tabs you manage have ended since "
            f"the last principles audit of {', '.join(bodies)}. Audit each more deeply than a "
            "check: have a reader go over its merges (its git log), the work set aside and the "
            "readings since the last audit against its principles doc (`principles` gives the "
            "doc and the ledger), correct what drifted — in its documents, or by `direct` — and "
            "call `principles_checked` with `deep` true.")
