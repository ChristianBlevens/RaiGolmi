"""The machine tab coordinating the body tabs the user hands it.

Which tabs are managed is the intent's (`TabIntent.managed`, set by `Session.manage`). This
system reads that mark and the events, and reaches the machine tab only by events whose data
carries `deliver`, which its channel holds until its turn ends (`channel.py`):

- a managed tab's turn ending, with its context use against the budget;
- a question the judge referred from a managed tab (`question.referred`), which the machine
  tab answers with `answer_question`; the same for a tab handed over while asking, and, when a
  machine tab opens, everything it has not heard because it was not there to hear it.

The machine tab's own verbs (`methods`) are refused to every other tab: the views of a
managed tab, a directive pushed into one, and a fresh conversation for it.

**A fresh conversation is a new tab** (`Session.succeed_tab`): the old one closes, its
conversation and thought doc archived together as that conversation's record, and a new tab on
the same body takes over the work, still managed. It starts from `SESSION-START.md` alone —
the one document written for the next conversation; a thought doc is never where one starts.
**A handover takes two steps** (`TabIntent.handover`), because the next conversation has only
that document: `restart_fresh` first pushes the wrap-up (`wrap_up_message`); once that turn has
started and ended the tab is ready, the machine tab is told, reads its `SESSION-START.md`, and
the next `restart_fresh` hands the work on. **The machine tab hands itself on the same way**
while it manages tabs, since nobody else is there to close it: at its own budget the daemon
asks it to make its `SESSION-START.md` ready, it says so with `ready_to_restart`, and when that
turn ends a new machine tab takes over.

**A managed tab stops where the user said** (`TabIntent.stop_when`, given as they hand it
over, and in every message about it): at that goal or decision the machine tab `hold`s it on
the situation, and the tab is resumed on Remote Control to put it to them on their phone
(`Session.hold`). Nothing of the machine tab's reaches a held tab; the user's own words in it
release it (`Session.release`), and the machine tab is told.

**And stops when the user's time for it runs out** (`TabIntent.until`): the daemon, not the
machine tab, pushes the wrap-up then (`time_up_message`, `tick`), and gives the tab back once
that turn has ended — a held one as it stands.

**A run ends with a report** (`Intent.run`): once the last tab is given back the machine tab
is asked for the user's report (`report_message`), carrying what the daemon itself recorded of
the run and read of each tab, since a tab given back is out of its reach. **Each machine-tab
handover is a checkpoint**, so nothing about a run grows with its length: the outgoing machine
tab's `ready_to_restart` files its progress report on its stretch with that stretch's
`documents.RUN_RECORD` and the daemon's record of it (`Session.checkpoint_run`), and the next
machine tab keeps a new record. The final report covers the last stretch and where the run
ended, beside the progress reports (`Session.report_run`).

Context use is the input the tab's latest main-conversation answer took — input, cache-read
and cache-created tokens, as Claude Code's own status line counts it — read from its newest
transcript. The daemon reads every tab's home; no tab can read another's.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from . import api, claude_login, documents, history, limits, settings
from .channel import Channels
from .events import Event, EventLog
from .intent import Run, TabIntent
from .questions import Questions
from .session import SessionError

if TYPE_CHECKING:
    from .session import Session

# How much of a managed tab a view shows by default.
TAIL_TURNS = 20
TAIL_LINES = 60
TURN_CHARS = 2000


def latest_transcript(home: Path) -> Path | None:
    transcripts = list((home / ".claude" / "projects" / "-work").glob("*.jsonl"))
    return max(transcripts, key=lambda p: p.stat().st_mtime) if transcripts else None


def main_rows(transcript: Path) -> list[dict[str, Any]]:
    """The main conversation's user and assistant rows, in order; a subagent's are not it."""
    rows = []
    with transcript.open(encoding="utf-8") as lines:
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # a line being written as it is read
            if row.get("type") in ("user", "assistant") and not row.get("isSidechain"):
                rows.append(row)
    return rows


def context_tokens(home: Path) -> int | None:
    """The input the latest main-conversation answer took; None before the first answer."""
    transcript = latest_transcript(home)
    if transcript is None:
        return None
    for row in reversed(main_rows(transcript)):
        usage = (row.get("message") or {}).get("usage")
        if row["type"] == "assistant" and usage:
            return (usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
                    + usage.get("cache_read_input_tokens", 0))
    return None


def _said(row: dict[str, Any]) -> str:
    content = (row.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    parts = []
    for part in content or ():
        if part.get("type") == "text":
            parts.append(part["text"])
        elif part.get("type") == "tool_use":
            parts.append(f"[tool {part.get('name')}]")
        elif part.get("type") == "tool_result":
            parts.append("[tool result]")
    return "\n".join(parts)


def transcript_tail(home: Path, turns: int) -> list[dict[str, str]]:
    """The last `turns` turns, each cut to `TURN_CHARS` but the last: that is the tab's
    report at its turn's end, which the machine tab and the run's report read whole."""
    transcript = latest_transcript(home)
    if transcript is None:
        return []
    tail = []
    for row in main_rows(transcript):
        said = _said(row).strip()
        if said:
            tail.append({"role": row["type"], "said": said})
    tail = tail[-turns:] if turns > 0 else []
    for turn in tail[:-1]:
        turn["said"] = turn["said"][:TURN_CHARS]
    return tail


def thoughts_tail(home: Path, lines: int) -> str | None:
    doc = home / documents.THOUGHTS
    if not doc.is_file():
        return None
    return "\n".join(doc.read_text(encoding="utf-8").splitlines()[-lines:])


def _context_line(tokens: int | None, budget: int) -> str:
    if tokens is None:
        return "It has not answered yet, so its context use is unknown."
    said = f"Its context use is {tokens // 1000}k of the {budget // 1000}k budget."
    if tokens >= budget:
        said += (" It is at the budget: `restart_fresh` it, which first has it make its "
                 "documents ready for its next conversation.")
    return said


def clock(t: float) -> str:
    """The user's time, zone named: the agents read it beside their own clocks and logs."""
    return time.strftime("%H:%M %Z", time.localtime(t))


def stop_line(tab: TabIntent) -> str:
    said = ""
    if tab.stop_when is not None:
        said += (f" The user stops it at: {tab.stop_when!r}. When that is reached, `hold` it "
                 "for them; when you are unsure whether it is, go on, and have it note the "
                 "doubt in its thought doc and commit, so they can return to that point.")
    if tab.until is not None:
        said += (f" Their time for it runs out at {clock(tab.until)}; the daemon then has it "
                 "make its documents ready and gives it back to them.")
    return said


def question_message(item: dict[str, Any], tab: TabIntent) -> str:
    choices = f" Its choices: {', '.join(item['choices'])}." if item["choices"] else ""
    return (f"Tab {item['tab']} ({tab.body}), which you manage, asked the user {item['id']}: "
            f"{item['message']!r}.{choices} Their preferences did not answer it, so it is "
            f"yours: answer it with `answer_question`. Never put it to them."
            f"{stop_line(tab)}")


def hold_message(stop_when: str | None, situation: str) -> str:
    stop = f" at {stop_when!r}" if stop_when else ""
    return (f"From the machine tab: you are held for the user{stop}. The situation, as the "
            f"machine tab puts it:\n\n{situation}\n\nPut it to them now, in this "
            "conversation — they reach it from their phone through Remote Control, or at the "
            "screen: where the work stands, the choice or the next step that is theirs, the "
            "options and what you recommend. Then end your turn; their answer is your next "
            "message. Do nothing more of the work until it comes.")


def restart_message(tab: str, continues: str) -> str:
    """What the user would type to start a session: the run's direction is in the document."""
    return (f"You are tab {tab}, taking over tab {continues}'s work in a fresh conversation, "
            "opened by the machine tab, which manages this work for the user. Read "
            f"/work/{documents.SESSION_START} and continue the work from it. "
            f"~/{documents.THOUGHTS} is this conversation's own record; {continues}'s is "
            "archived with its conversation.")


WRAP_UP = "coordinator.wrap_up"
TIME_UP = "coordinator.time_up"
RESTART_ASKED = "coordinator.restart_asked"
REPORT_ASKED = "coordinator.report_asked"


def _documents_ready() -> str:
    return (
        f"1. /work/{documents.SESSION_START}: the whole start for the next conversation — "
        "where the work stands, what it takes next and what it reads, and every direction "
        "from the machine tab that holds past this conversation, as the user's would — as "
        "short as it can be, within any size cap the project sets, with nothing stale in it: "
        "no history, nothing past-tense that nothing turns on.\n"
        f"2. /home/agent/{documents.THOUGHTS}: finished as this conversation's record — what "
        "it set out to do, found, decided and left. It is archived with this conversation; "
        "the next one does not start from it.\n"
        "3. What outlives this work, in the permanent doc it belongs to.\n"
        "4. What should be committed, committed.\n"
        f"5. This conversation held against the rules {documents.SESSION_START} gives the "
        "work: each one it broke or kept only when told, said in your last words; and a "
        "rule this conversation showed the work needs, added there.\n")


def wrap_up_message() -> str:
    return (
        "From the machine tab, which manages this tab for the user: once this turn ends your "
        "work is handed to a new tab, a fresh conversation that starts from "
        f"{documents.SESSION_START} alone. Make your documents ready for it now:\n"
        + _documents_ready()
        + f"Then end your turn saying the next conversation can continue from "
          f"{documents.SESSION_START}.")


def time_up_message(until: float) -> str:
    return (
        f"From the daemon: the user's time for this work ran out at {clock(until)}. Once this "
        "turn ends you are given back to them, and nothing more comes from the machine tab. "
        "Make your documents ready for their return now:\n" + _documents_ready()
        + "Then end your turn saying where the work stands.")


def restart_asked_message(tokens: int, budget: int) -> str:
    return (
        f"From the daemon: your context is at {tokens // 1000}k of the {budget // 1000}k "
        "budget, and the user is away, so a new machine tab takes over in a fresh "
        f"conversation that starts from /work/{documents.SESSION_START}. Make it that "
        "conversation's whole start now: every tab you manage, what each is working toward, "
        "the last direction you gave each and what is on its way to or from it, whose "
        "handover stands where, and what you were in the middle of. Finish "
        f"~/{documents.THOUGHTS} as this conversation's record, and bring "
        f"/work/{documents.RUN_RECORD}, the run's record, current with what this one did. "
        "This handover is a checkpoint of the run: write the user's progress report on this "
        "stretch of it — for each tab what it worked toward, what it got done and where it "
        "stands; what you decided for them; what went wrong — and hand it over with "
        "`ready_to_restart`, last of all: it files the report in their catalog with "
        f"{documents.RUN_RECORD} and the daemon's own record of the stretch. Then end your "
        "turn; the new tab takes over once it ends.")


def machine_restart_message(continues: str) -> str:
    return (
        f"You are the machine tab, taking over from {continues} in a fresh conversation the "
        "daemon opened at its context budget, while the user is away. Read "
        f"/work/{documents.SESSION_START}, call `managed` for where each tab you manage "
        f"stands now, and carry on managing them. Keep /work/{documents.RUN_RECORD}, the "
        f"record of this stretch of the run: {continues}'s stretch is filed with its progress "
        f"report. ~/{documents.THOUGHTS} is this conversation's own record.")


def report_message(run: Run, record: list[str], tabs: dict[str, dict[str, Any]]) -> str:
    said = [
        f"From the daemon: the run you managed from {clock(run.started)} to "
        f"{clock(run.ended or run.started)} is over; every tab is given back to the user. "
        "Write their report on it now and hand it over with `report_run`: it is what they read "
        "when they return. For each tab: what it worked toward, what it got done, where it "
        "stands and what is theirs to decide next. Then what you decided for them — the "
        "questions you answered — and what went wrong or is unfinished. Draw on "
        f"/work/{documents.RUN_RECORD}, your record of this stretch, and on what the daemon "
        "recorded and read below: a tab given back is out of your reach."]
    if run.checkpoints:
        said.append(
            f"The run's first {run.checkpoints} stretch(es) are filed with their progress "
            f"reports, from {clock(run.started)} to {clock(run.since)}, and the user reads them "
            "beside this one: report where the whole run ended and what is theirs next, and "
            "this last stretch in full.")
    said.append("\nThe daemon's record of this stretch:\n" + ("\n".join(record) or "(nothing)"))
    for tab_id, seen in tabs.items():
        said.append(f"\nTab {tab_id} ({seen['body']}), the tail of its thought doc:\n"
                    f"{seen['thoughts'] or '(it has none)'}\n\nIts last words:\n"
                    f"{seen['said'] or '(none)'}")
    return "\n".join(said)


def run_record(session: "Session", events: EventLog, questions: Questions,
               run: Run) -> tuple[list[str], dict[str, str | None]]:
    """What the daemon recorded of the run's current stretch — since its last checkpoint, or
    its start — in the history's words, for the tabs it managed and the machine tab; and
    every tab managed in the run, with its body. A stretch is one machine tab's
    conversation, so its record is bounded however long the run goes on."""
    machine = session.intent.machine_tab()
    read = list(events.read())
    # The machine tabs that took over from one another, newest back to the run's first.
    machines = set()
    continued = {e.tab: e.data["continues"] for e in read
                 if e.type == "tab.opened" and e.data.get("continues")}
    tab_id = machine.tab_id if machine is not None else None
    while tab_id is not None and tab_id not in machines:
        machines.add(tab_id)
        tab_id = continued.get(tab_id)
    items = questions.items()
    since = run.since or run.started
    managed: dict[str, str | None] = {}
    lines = []
    for event in read:
        if event.type == "run.ended" and event.data["started"] == run.started:
            break       # the giving back that ended it is emitted just before, and is in it
        if event.ts < run.started:
            continue
        if event.type == "tab.managed" and event.data["on"]:
            managed.setdefault(event.tab, event.data["body"])
        if event.ts < since or not (
                event.tab is None or event.tab in managed or event.tab in machines):
            continue
        if event.type == "question.answered" and event.data.get("by") == "machine":
            item = items.get(event.data["id"])
            asked = f" {item['message']!r}" if item else ""
            said = f"you answered {event.data['id']}{asked}: {event.data['answer']!r}"
        elif event.type in history.SAYS:
            said = history.SAYS[event.type](event)
        elif event.type in history.NOTICED:
            said = history.NOTICED[event.type](event)
        else:
            continue
        if said:
            lines.append(f"{clock(event.ts)} {event.tab or 'the machine'}: {said}")
    return lines, managed


class Coordinator:
    """Subscribed at construction, so no referral falls between the daemon's start and `run`."""

    def __init__(self, session: "Session", events: EventLog, questions: Questions) -> None:
        self.session = session
        self.events = events
        self.questions = questions
        self._sub = events.subscribe()
        # (machine tab, question) handed over: a referral and a hand-over read in one batch
        # both find the question pending, and a machine tab hears each once.
        self._handed: set[tuple[str, str]] = set()

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            for event in self._sub.drain(timeout=1.0):
                self.on_event(event)
            self.tick(time.time())
            if self._sub.dropped:
                # A dropped referral is a question nobody answers: said, and offered again.
                count, self._sub.dropped = self._sub.dropped, 0
                self.events.emit("coordinator.events_dropped", count=count)
                self.announce()

    def on_event(self, event: Event) -> None:
        if event.type == "run.ended":
            self._ask_report()
            return
        if event.type == "channel.heard" and event.data["cause"] == REPORT_ASKED:
            self.session.report_asked()
            return
        tab = self.session.intent.tabs.get(event.tab) if event.tab else None
        if tab is None:
            return
        if event.type == "channel.heard":
            if (event.data["cause"] in (WRAP_UP, TIME_UP, RESTART_ASKED)
                    and tab.handover == "asked"):
                self.session.hand_over(tab.tab_id, "heard")
        elif event.type == "tab.opened" and tab.machine:
            self.announce()
        elif event.type == "agent.idle" and tab.machine:
            self._machine_idle(tab.tab_id, tab.handover, event.data.get("error"))
        elif not tab.managed:
            return
        elif event.type == "tab.released":
            self._to_machine(event.tab, "coordinator.released", (
                f"The user answered tab {event.tab} ({tab.body}), which you held for them. It "
                "goes on with their direction; it is yours again." + stop_line(tab)),
                why="released")
        elif tab.held is not None:
            return      # the user's until they answer in it
        elif event.type == "agent.idle":
            error = event.data.get("error")
            if error == limits.LIMIT or error in limits.TRANSIENT:
                return      # resumed by the daemon, and heard of when that turn ends
            if error is None and tab.handover == "heard":
                self.session.hand_over(tab.tab_id, "ready")
            if error is None and tab.handover == "ready" and self._time_up(tab, time.time()):
                self._give_back(tab)
                return
            self._to_machine(event.tab, "coordinator.idle", self._idle_message(
                event.tab, tab.body, event.data["done"], error), why="idle")
        elif event.type == "question.referred" and event.data["kind"] == "question":
            self._question(event.data["id"])
        elif event.type == "tab.managed":
            for item in self._pending_of({event.tab}):
                self._question(item["id"])

    def announce(self) -> None:
        """Every managed tab's pending question, and a run's report still to write, to the
        machine tab that is open now: at the daemon's start, whose channels hold nothing, and
        when a machine tab opens."""
        for item in self._pending_of(self.session.managed_tabs()):
            self._question(item["id"])
        run = self.session.intent.run
        if run is not None and run.ended is not None:
            self._ask_report()

    def tick(self, now: float) -> None:
        """Each managed tab whose time has run out: its wrap-up pushed, unless a handover
        already has it making its documents ready; given back once they are, or at once
        while it is held for the user, who has it on Remote Control as it stands."""
        for tab in list(self.session.intent.tabs.values()):
            if not tab.managed or not self._time_up(tab, now):
                continue
            if tab.held is not None or (tab.handover == "ready" and not tab.busy):
                self._give_back(tab)
            elif tab.handover is None:
                self.session.hand_over(tab.tab_id, "asked")
                self.events.emit(TIME_UP, tab=tab.tab_id, deliver={
                    "content": time_up_message(tab.until), "meta": {"from": "daemon"}})

    @staticmethod
    def _time_up(tab: TabIntent, now: float) -> bool:
        return tab.until is not None and tab.until <= now

    def _give_back(self, tab: TabIntent) -> None:
        until, held = tab.until, tab.held
        self.session.manage(tab.tab_id, False, why="time")
        run = self.session.intent.run
        if run is not None and run.ended is not None:
            return      # the last tab: the report request, which follows, says it
        state = ("was held for them, and stays on Remote Control as it stands" if held
                 else "made its documents ready")
        self._to_machine(tab.tab_id, "coordinator.given_back", (
            f"The user's time for tab {tab.tab_id} ({tab.body}) ran out at {clock(until)}; it "
            f"{state}, and is given back to them."), why="time")

    def _ask_report(self) -> None:
        run = self.session.intent.run
        machine = self.session.intent.machine_tab()
        if run is None or run.ended is None:
            return
        if machine is None:
            self.events.emit("coordinator.unheard", about="run", cause=REPORT_ASKED)
            return
        if (machine.tab_id, f"run@{run.started}") in self._handed:
            return
        self._handed.add((machine.tab_id, f"run@{run.started}"))
        lines, managed = run_record(self.session, self.events, self.questions, run)
        tabs = {}
        for tab_id, body in managed.items():
            if tab_id not in self.session.intent.tabs:
                continue    # closed, or handed on: its record is in the archive
            home = self.session.agents.home(tab_id)
            last = transcript_tail(home, 1)
            tabs[tab_id] = {"body": body, "thoughts": thoughts_tail(home, TAIL_LINES),
                            "said": last[0]["said"] if last else None}
        self.events.emit(REPORT_ASKED, tab=machine.tab_id, deliver={
            "content": report_message(run, lines, tabs), "meta": {"from": "daemon"}})

    def _machine_idle(self, tab_id: str, handover: str | None, error: str | None) -> None:
        """The machine tab's own handover, only while the user is away (`Intent.hands_off`):
        asked at its budget and restarted after the turn that said ready. A turn that heard it
        and ended without saying so is the tab failing, not a reason to ask again (the owner,
        2026-09-29): it is said as `coordinator.unanswered`, which the manager takes, and
        nothing more is asked until it says ready."""
        if not self.session.intent.hands_off(tab_id):
            if handover is not None:
                self.session.hand_over(tab_id, None)    # the user is back: theirs to close
            return
        if error is not None:
            return
        if handover == "ready":
            if self.questions.tab_state(tab_id) is not None:
                return      # a restart would withdraw what it asked; its next idle retries
            try:
                successor = self.session.succeed_tab(tab_id, by="daemon")
            except Exception as exc:  # noqa: BLE001 — said, and this thread must not end
                self.events.emit("coordinator.restart_failed", tab=tab_id, error=str(exc))
                return
            self.events.emit("coordinator.machine_restarted", tab=successor.tab_id, deliver={
                "content": machine_restart_message(tab_id), "meta": {"from": "daemon"}})
            return
        if handover in ("asked", "unanswered"):
            return          # its push is on its way, or the manager has it
        if handover == "heard":
            self.session.hand_over(tab_id, "unanswered")
            reply = transcript_tail(self.session.agents.home(tab_id), 1)
            self.events.emit("coordinator.unanswered", tab=tab_id, message=(
                f"The machine tab {tab_id} was asked to make its SESSION-START.md ready and call "
                "`ready_to_restart` at its context budget, and ended its turn without doing "
                "so; nothing more is asked of it. It restarts once it calls "
                "`ready_to_restart`. Its last words: "
                + (reply[0]["said"] if reply else "(none)")))
            return
        tokens = context_tokens(self.session.agents.home(tab_id))
        budget = settings.load(self.session.paths.settings).budget_tokens
        if tokens is None or tokens < budget:
            return
        self.session.hand_over(tab_id, "asked")
        self.events.emit(RESTART_ASKED, tab=tab_id, deliver={
            "content": restart_asked_message(tokens or budget, budget),
            "meta": {"from": "daemon"}})

    def _pending_of(self, tabs: set[str]) -> list[dict[str, Any]]:
        return [i for i in self.questions.pending()
                if i["kind"] == "question" and i["tab"] in tabs]

    def _question(self, id: str) -> None:
        item = self.questions.items()[id]
        tab = self.session.intent.tabs.get(item["tab"])
        machine = self.session.intent.machine_tab()
        if item["state"] != "pending" or tab is None or (
                machine is not None and (machine.tab_id, id) in self._handed):
            return
        if machine is not None:
            self._handed.add((machine.tab_id, id))
        self._to_machine(item["tab"], "coordinator.question",
                         question_message(item, tab), why="question", question=id)

    def _idle_message(self, tab_id: str, body: str | None, done: bool,
                      error: str | None = None) -> str:
        ended = ("is done" if done else "ended its turn and waits on something on its way"
                 if error is None else f"had its turn cut off by an API error ({error}) that "
                 "continuing will not fix; the user is told")
        if self.session.intent.tabs[tab_id].handover == "ready":
            start = self.session.session_start(tab_id)
            size = (f"It is {len(start.encode())} bytes; hold it to any cap the project sets."
                    if start is not None else "It has none yet.")
            return (f"Tab {tab_id} ({body}), which you manage, has made its documents ready for "
                    f"its next conversation. Read its {documents.SESSION_START}, which is all "
                    f"that conversation starts from, with `managed_tab`. {size} `direct` it to "
                    "fix what is stale or missing, or `restart_fresh` it to hand the work to "
                    "that conversation.")
        budget = settings.load(self.session.paths.settings).budget_tokens
        return (f"Tab {tab_id} ({body}), which you manage, {ended}. "
                f"{_context_line(context_tokens(self.session.agents.home(tab_id)), budget)} "
                "`managed_tab` shows its thought doc and latest turns."
                f"{stop_line(self.session.intent.tabs[tab_id])}")

    def _to_machine(self, about: str, event: str, content: str, **meta: str) -> None:
        machine = self.session.intent.machine_tab()
        if machine is None:
            # Nothing open to hear it; the next machine tab is told what is still pending.
            self.events.emit("coordinator.unheard", about=about, cause=event)
            return
        self.events.emit(event, tab=machine.tab_id, about=about, deliver={
            "content": content, "meta": {"tab": about, **meta}})


def methods(session: "Session", questions: Questions, channels: Channels,
            caller: str) -> dict[str, Callable[..., Any]]:
    """The machine tab's verbs on its own socket, refused on any other tab's."""
    verbs = Verbs(session, questions, channels)

    def only_machine(verb: Callable[..., Any]) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            tab = session.intent.tabs.get(caller)
            if tab is None or not tab.machine:
                raise SessionError("only the machine tab coordinates other tabs; message it "
                                   "(`to` \"machine\") instead")
            return verb(*args, **kwargs)
        return call

    def record() -> list[str]:
        run = session.intent.run
        if run is None:
            raise SessionError("there is no run to report on: one starts when the user "
                               "hands you a tab")
        return run_record(session, session.events, questions, run)[0]

    def ready_to_restart(report: str) -> dict[str, Any]:
        tab = session.intent.tabs[caller]
        if tab.handover not in ("asked", "heard", "unanswered"):
            raise SessionError("nothing asked you to restart: the daemon asks at your context "
                               "budget while you manage tabs")
        read_at = time.time()
        filed = session.checkpoint_run(caller, report, record(), read_at)
        session.hand_over(caller, "ready")
        return {**filed, "status": "ready",
                "next": "End your turn; you are restarted once it ends."}

    def report_run(report: str) -> dict[str, Any]:
        return session.report_run(caller, report, record())

    return {name: only_machine(verb) for name, verb in (
        *((n, getattr(verbs, n)) for n in ("manage", "managed", "managed_tab", "direct",
                                           "answer_question", "restart_fresh", "hold")),
        ("ready_to_restart", ready_to_restart), ("report_run", report_run))}


class Verbs:
    """What the machine tab does to the tabs it manages."""

    def __init__(self, session: "Session", questions: Questions, channels: Channels) -> None:
        self.session = session
        self.events = session.events
        self.questions = questions
        self.channels = channels

    def manage(self, tab: str, on: bool = True, stop_when: str | None = None,
               hours: float | None = None) -> dict[str, Any]:
        if hours is not None and hours <= 0:
            raise SessionError("`hours` is how long the user gives the tab, more than none")
        until = time.time() + hours * 3600 if hours is not None else None
        managed = self.session.manage(tab, on, stop_when, until)
        if not on:
            run = self.session.intent.run
            if run is not None and run.ended is not None:
                return {**managed, "next": "That was the last tab, so the run is over: the "
                                           "daemon's request for the user's report reaches "
                                           "you when this turn ends."}
            return managed
        return {**managed, "until": self.session.intent.tabs[tab].until,
                "next": f"Keep /work/{documents.RUN_RECORD}, the record of your stretch of "
                        "the run: what you directed, decided and saw, as it happens. When the "
                        "last tab is given back you are asked for the user's report."}

    def managed(self) -> list[dict[str, Any]]:
        """Each managed tab: its state, its questions waiting on the machine tab, and its
        context use."""
        status = api.with_tab_states(self.session.status(), self.questions, self.channels)
        pending = self.questions.pending()
        budget = settings.load(self.session.paths.settings).budget_tokens
        out = []
        for agent in status["agents"]:
            if not agent["managed"]:
                continue
            tab = agent["tab"]
            intent = self.session.intent.tabs[tab]
            out.append({
                **agent,
                "body": agent["scope"]["body"],
                "continues": intent.continues,
                "stop_when": intent.stop_when,
                "until": intent.until,
                "held": intent.held,
                "context_tokens": context_tokens(self.session.agents.home(tab)),
                "budget_tokens": budget,
                "questions": [{"id": i["id"], "message": i["message"],
                               "choices": list(i["choices"])}
                              for i in pending if i["tab"] == tab and i["kind"] == "question"],
            })
        return out

    def managed_tab(self, tab: str, turns: int = TAIL_TURNS, lines: int = TAIL_LINES,
                    session_start: bool = True) -> dict[str, Any]:
        """One managed tab closer: its `SESSION-START.md`, which its next conversation starts
        from — its size always, its text unless `session_start` is false — and the tails of
        this conversation's thought doc and transcript."""
        row = next((t for t in self.managed() if t["tab"] == tab), None)
        if row is None:
            raise SessionError(f"{tab} is not a tab you manage")
        home = self.session.agents.home(tab)
        start = self.session.session_start(tab)
        return {**row, "session_start": start if session_start else None,
                "session_start_bytes": len(start.encode()) if start is not None else None,
                "thoughts_tail": thoughts_tail(home, lines),
                "transcript_tail": transcript_tail(home, turns)}

    def direct(self, tab: str, content: str) -> dict[str, Any]:
        """One way: it is the tab's next message once its turn ends; nothing comes back."""
        agent = self._unheld(tab)
        if not content.strip():
            raise SessionError("a directive needs words")
        self.events.emit("coordinator.directed", tab=tab, deliver={
            "content": f"From the machine tab, which manages this tab for the user:\n\n"
                       f"{content}", "meta": {"from": "machine"}})
        if agent.busy:
            return {"tab": tab, "status": "queued",
                    "next": "It is pushed when the tab's turn ends; you are told when it is "
                            "idle."}
        return {"tab": tab, "status": "pushed",
                "next": "The tab is idle, so it starts on this now, after anything already on "
                        "its way to it; you are told when that turn ends."}

    def answer_question(self, id: str, answer: str) -> dict[str, Any]:
        item = self.questions.items().get(id)
        if item is None:
            raise SessionError(f"no question {id}")
        self._managed(item["tab"])
        self.questions.answer_by_machine(id, answer)
        return {"question": id, "status": "answered",
                "next": "The user sees this answer in the history and may overturn it."}

    def restart_fresh(self, tab: str, documents_ready: bool = False) -> dict[str, Any]:
        """The tab's handover: first its wrap-up pushed, then, once that turn has ended, a new
        tab that takes the work over in a fresh conversation from its `SESSION-START.md`.
        `documents_ready` skips the wrap-up for a tab whose turn already ended with its
        documents ready, which the machine tab has read. Refused while it works, since that
        would cut its turn off, and while anything is asked of the user or the machine tab,
        since a restart withdraws it."""
        agent = self._unheld(tab)
        if agent.busy:
            raise SessionError(f"{tab} is working; restart it once it is idle")
        if (waiting := self.questions.tab_state(tab)) is not None:
            raise SessionError(f"{tab} is {waiting} on a question or permission, and a "
                               "restart would withdraw it; settle it first")
        if agent.handover != "ready" and not (documents_ready and agent.handover is None):
            if agent.handover is None:
                self.session.hand_over(tab, "asked")
                self.events.emit(WRAP_UP, tab=tab, deliver={
                    "content": wrap_up_message(), "meta": {"from": "machine"}})
            return {"tab": tab, "status": "wrapping_up",
                    "next": "It is making its documents ready for its next conversation; you "
                            "are told when that turn ends, and `restart_fresh` then restarts "
                            "it."}
        successor = self.session.succeed_tab(tab, by="machine")
        self.events.emit("coordinator.restarted", tab=successor.tab_id, deliver={
            "content": restart_message(successor.tab_id, tab),
            "meta": {"from": "machine"}})
        return {"tab": successor.tab_id, "continues": tab, "status": "started",
                "next": f"{successor.tab_id} takes over {tab}'s work, which is closed and "
                        f"archived; you manage {successor.tab_id} now, and are told when its "
                        "first turn ends."}

    def hold(self, tab: str, situation: str) -> dict[str, Any]:
        """The tab stopped for the user where they said, resumed on Remote Control and told to
        put `situation` to them. Refused while it works, since the restart would cut its turn
        off, and without the claude.ai sign-in Remote Control needs."""
        agent = self._unheld(tab)
        if not situation.strip():
            raise SessionError("a hold needs the situation: where the work stands and what is "
                               "the user's to decide")
        if agent.busy:
            raise SessionError(f"{tab} is working; hold it once its turn ends")
        if not claude_login.is_set(self.session.paths.claude_login):
            raise SessionError(
                "the user has not signed in to claude.ai (`rai claude-login --login`), so a held "
                "tab cannot reach their phone: `direct` the tab to stop and write the situation "
                "at the top of its SESSION-START.md, where they will see it when they return")
        held = self.session.hold(tab, situation)
        self.events.emit("coordinator.held", tab=tab, deliver={
            "content": hold_message(agent.stop_when, situation), "meta": {"from": "machine"}})
        return {**held, "next": "It is the user's until they answer in it; you are told then."}

    def _unheld(self, tab: str):
        agent = self._managed(tab)
        if agent.held is not None:
            raise SessionError(f"{tab} is held for the user; nothing reaches it until they "
                               "answer in it, and you are told then")
        if agent.until is not None and agent.until <= time.time():
            raise SessionError(f"the user's time for {tab} ran out at {clock(agent.until)}; "
                               "it is making its documents ready and is given back to them "
                               "once that turn ends")
        return agent

    def _managed(self, tab: str):
        agent = self.session.intent.tabs.get(tab)
        if agent is None or not agent.managed:
            raise SessionError(f"{tab} is not a tab you manage; the user hands tabs over, and "
                               "`manage` marks one")
        return agent
