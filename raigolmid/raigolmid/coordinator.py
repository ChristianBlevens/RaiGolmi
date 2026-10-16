"""The machine tab coordinating the body tabs the user hands it.

Which tabs are managed is the intent's (`TabIntent.managed`, set by `Session.manage`). This
system reads that mark and the events, and reaches the machine tab only by events whose data
carries `deliver`, which its channel holds until its turn ends (`channel.py`):

- a managed tab's turn ending, with its context use against the budget, said as it stands when
  it is pushed (`Channels.restate_at_push`): withdrawn once the tab has closed, started another
  turn or been given back;
- a question the judge referred from a managed tab (`question.referred`), which the machine
  tab answers with `answer_question`; the same for a tab handed over while asking, and, when a
  machine tab opens, everything it has not heard because it was not there to hear it;
- the keeper's answer to a fault in RaiGolmi the machine tab filed with `report_fault`
  (`faults.py`), so it resumes the tabs it paused for it.

The machine tab's own verbs (`methods`) are refused to every other tab: the views of a
managed tab, a directive sent to one, and a fresh conversation for it.

**A fresh conversation is a new tab** (`Session.succeed_tab`): the old one closes, its
conversation and thought doc archived together as that conversation's record, and a new tab on
the same body takes over the work, still managed. It starts from `SESSION-START.md` alone —
the one document written for the next conversation; a thought doc is never where one starts.
**A handover takes two steps** (`TabIntent.handover`), because the next conversation has only
that document: `restart_fresh` first pushes the wrap-up (`wrap_up_message`); once that turn has
started and ended the tab is ready, the machine tab is told, reads its `SESSION-START.md`, and
the next `restart_fresh` hands the work on. **Every tab can hand itself on**
(`end_conversation`, `ENDING`): it says its `SESSION-START.md` is ready, and when that turn
ends a new tab takes over from it; a managed tab's machine tab is told, with what that last
turn ended on, since a closed tab cannot be viewed. The machine tab is
asked to at its own budget while it manages tabs, since nobody else is there to close it.
The janitor has no such verb: the daemon gives it a fresh conversation per failure
(`janitor.py`).

**A managed tab stops where the user said** (`TabIntent.stop_when`, given as they hand it
over, and in every message about it): at that goal or decision the machine tab `hold`s it on
the situation, and the tab puts it to them in its conversation (`Session.hold`). Nothing of
the machine tab's reaches a held tab; the user's own words in it release it
(`Session.release`), and the machine tab is told.

**And stops when the user's time for it runs out** (`TabIntent.until`): the daemon, not the
machine tab, pushes the wrap-up then (`time_up_message`, `tick`), and gives the tab back once
that turn has ended — a held one as it stands.

**A run that stops moving is woken** (`tick`): the machine tab and every tab it manages idle
for `IDLE_SECONDS`, with nothing that waits on the user — a question to them, a tab held or
paused for them, the account's usage limit — is a notice that never arrived, so the machine
tab is told where each tab's last turn ended; as it is when it sits idle that long with a
question or message from a tab waiting on it. A wake whose own turn ends with the run still
idle wakes it again after twice the wait, up to `IDLE_CEILING_SECONDS`; a managed tab's turn,
or a machine-tab turn other than the wake's own, starting ends the stretch and its back-off.
**A restart is said to it** (`tabs.reopened`, and `agent.reopened` for its own agent restarted
mid-turn): which tabs were reopened and which of them were mid-turn — its own turn it carries
on, the others `limits.py` continues — and that the run goes on.

**A run ends with a report** (`Intent.run`): once the last tab is given back the machine tab
is asked for the user's report (`report_message`), carrying what the daemon itself recorded of
the run and read of each tab, since a tab given back is out of its reach. **Each machine-tab
handover is a checkpoint**, so nothing about a run grows with its length: the outgoing machine
tab's `end_conversation` files its progress report on its stretch with that stretch's
`documents.RUN_RECORD` and the daemon's record of it (`Session.checkpoint_run`), and the next
machine tab keeps a new record. The final report covers the last stretch and where the run
ended, beside the progress reports (`Session.report_run`).

Context use is the input the tab's latest main-conversation answer took — input, cache-read
and cache-created tokens, as Claude Code's own status line counts it — read from its newest
transcript. The daemon reads every tab's home; no tab can read another's.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from . import api, documents, history, limits, machinecopy
from .channel import DIRECTED, Channels, HandedOn, Message
from .events import Event, EventLog
from .faults import Faults, answer_message, stranded_message
from .intent import Run, TabIntent
from .library import Library, run_id, save_from_tab
from .questions import PERMISSIONS_ABSENT, Questions, parse_permissions
from .session import SessionError
from .transcript import context_tokens, latest_transcript, main_rows

if TYPE_CHECKING:
    from .session import Session

# How much of a managed tab a view shows by default.
TAIL_TURNS = 20
TAIL_LINES = 60
TURN_CHARS = 2000
# How much of each direction a handover's notice quotes: enough to tell which it is.
DIRECTION_EXCERPT = 300
# A moving run starts or ends a turn every few minutes; this long with none is a notice lost.
# It sets how soon a stalled run is recovered, never whether it is.
IDLE_SECONDS = 20 * 60
# A wake whose turn leaves the run idle is repeated after twice its wait, since each costs a
# machine-tab turn; up to this, so a stall a wake could move waits at most a few hours of a run
# that lasts months.
IDLE_CEILING_SECONDS = 4 * 60 * 60
DIRECTION = "From the machine tab, which manages this tab for the user:\n\n"
# What a turn-end notice calls the messages on their way to a tab; any other is named by its
# event, which `managed_tab`'s `on_its_way` lists the same way.
ON_ITS_WAY = {DIRECTED: "your direction", "message.sent": "a message from another tab",
              "message.answered": "the answer to its message", "face.asked": "the user's words"}


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
            "conversation: where the work stands, the choice or the next step that is theirs, "
            "the options and what you recommend. Then end your turn; their answer is your next "
            "message. Do nothing more of the work until it comes.")


def restart_message(tab: str, continues: str, opened: str) -> str:
    """What the user would type to start a session: the work's direction is in the document."""
    return (f"You are tab {tab}, taking over tab {continues}'s work in a fresh conversation, "
            f"{opened}. Read /work/{documents.SESSION_START} and continue the work from it. "
            f"~/{documents.THOUGHTS} is this conversation's own record; {continues}'s is "
            "archived with its conversation.")


def handed_on_line(handed: HandedOn, successor: str) -> str:
    """The directions a handover moved, for the machine tab: those still on their way, which
    `successor` now has first, and those the closed tab took in its last turn, which only its
    work shows were carried out. Empty when there are none."""
    def quoted(directions: list) -> str:
        texts = [m.content.removeprefix(DIRECTION) for m in directions]
        return "; ".join('"' + (t if len(t) <= DIRECTION_EXCERPT
                                else t[:DIRECTION_EXCERPT] + "…") + '"' for t in texts)
    carried = [m for m in handed.carried if m.cause == DIRECTED]
    line = ""
    if carried:
        line += (f" Your directions still on their way to it go to {successor}, as its first "
                 f"messages after its opening: {quoted(carried)}.")
    if handed.last_turn:
        line += (f" It took these directions in its last turn; check its work and "
                 f"{documents.SESSION_START} carry them out, and `direct` {successor} with any "
                 f"they do not: {quoted(handed.last_turn)}.")
    return line


# A managed tab whose restart the machine tab asked for while its turn ran (`restart_fresh`).
RESTART_ON_IDLE = "restart"
# A tab that said it is ending its conversation (`end_conversation`): handed on as its turn ends.
ENDING = "ending"
WRAP_UP = "coordinator.wrap_up"
TIME_UP = "coordinator.time_up"
RESTART_ASKED = "coordinator.restart_asked"
REPORT_ASKED = "coordinator.report_asked"
IDLE_WOKEN = "coordinator.idle_woken"
# A managed tab's turn end, to the machine tab.
TURN_ENDED = "coordinator.idle"


def on_its_way(channels: Channels, questions: Questions, tab_id: str) -> list[str]:
    """What a tab whose turn ended is not done for, read now: what is on its way to it,
    the question or permission it put, the reply its own message waits on."""
    channel = channels.state(tab_id)
    causes = [m["cause"] for m in channel["queued"]]
    if channel["pushed"] is not None and not channel["pushed"]["heard"]:
        causes.insert(0, channel["pushed"]["cause"])
    said = [f"{causes.count(c)} × {ON_ITS_WAY.get(c, 'the notice ' + c)}"
            for c in dict.fromkeys(causes)]
    if (asked := questions.tab_state(tab_id)) is not None:
        said.append(f"its question or permission ({asked})")
    said += [f"a reply to its message {m['id']} to {m['to']}"
             for m in channels.messages.items()
             if m["state"] == "open" and m["sender"] == tab_id]
    return said


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
        "`end_conversation`, last of all: it files the report in their catalog with "
        f"{documents.RUN_RECORD} and the daemon's own record of the stretch. Then end your "
        "turn; the new tab takes over once it ends.")


def machine_restart_message(continues: str | None, report: str | None, running: bool) -> str:
    """`continues` None: a machine tab opened while a run is on after the one before it was
    closed, not handed on, so the stretch and its record go on."""
    start = (f"You are the machine tab, taking over from {continues} in a fresh conversation "
             f"opened as it ended its own. Read /work/{documents.SESSION_START}"
             if continues is not None else
             "You are the machine tab, opened afresh while a run you manage for the user is on, "
             f"after the machine tab before you was closed. Read /work/{documents.SESSION_START}")
    own = f" ~/{documents.THOUGHTS} is this conversation's own record."
    if not running:
        return start + " and carry on from it." + own
    if continues is None:
        filed = ("the machine tab before you kept it, and its stretch goes on with you"
                 + (f"; the run's latest progress report is `{report}`, which `document` reads"
                    if report is not None else ""))
    elif report is not None:
        filed = (f"{continues}'s stretch is filed with its progress report, `{report}`, which "
                 "`document` reads")
    else:
        filed = f"{continues}'s stretch is filed with its progress report"
    return (start + ", call `managed` for where each tab you manage stands now, and carry on "
            f"managing them. Keep /work/{documents.RUN_RECORD}, the record of this stretch of "
            f"the run: {filed}." + own)


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
    every tab managed in the run, with its body (`Run.tabs`). A stretch is one machine tab's
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
    managed: dict[str, str | None] = dict(run.tabs)
    lines = []
    for event in read:
        if event.type == "run.ended" and event.data["started"] == run.started:
            break       # the giving back that ended it is emitted just before, and is in it
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

    def __init__(self, session: "Session", events: EventLog, questions: Questions,
                 channels: Channels) -> None:
        self.session = session
        self.events = events
        self.questions = questions
        self.channels = channels
        self._sub = events.subscribe()
        # (machine tab, question) handed over: a referral and a hand-over read in one batch
        # both find the question pending, and a machine tab hears each once.
        self._handed: set[tuple[str, str]] = set()
        # The idle watch (`tick`), in memory: a daemon that starts starts its clocks. When a
        # turn of the run's tabs, and of the machine tab, last started or ended; how each
        # managed tab's last turn ended (done, error, when); whether a wake is due to the
        # machine tab now; its own turn, "due" until it starts and "on" until it ends; and how
        # long the next wake waits.
        self._quiet_since = self._machine_quiet_since = time.time()
        self._ended: dict[str, tuple[bool, str | None, float]] = {}
        self._woken = False
        self._wake_turn: str | None = None
        self._wait = IDLE_SECONDS
        self._limited = False
        self.faults = Faults(session.paths, events)
        channels.restate_at_push(TURN_ENDED, self._restated)

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
        if event.type in ("account.limited", "account.resumed"):
            self._limited = event.type == "account.limited"
            self._quiet_since = self._machine_quiet_since = event.ts
            return
        if event.type == "tabs.reopened":
            self._reopened(event.data["tabs"], event.data["mid_turn"], (
                "The machine restarted" if event.data["rebooted"]
                else "The daemon restarted and found agents gone"))
            return
        if (event.type == "agent.reopened" and event.data["mid_turn"]
                and not event.data["at_start"]):
            machine = self.session.intent.machine_tab()
            if machine is not None and machine.tab_id == event.tab:
                # `limits.py` leaves the machine tab's cut turn to this while it manages.
                self._reopened([event.tab], [event.tab], "Your agent exited and was restarted")
            return
        if event.type in ("agent.busy", "agent.idle"):
            self._turn(event)
        if event.type == "run.ended":
            self._ask_report()
            return
        if event.type == "fault.answered":
            self._fault_answered(self.faults.get(event.data["id"]))
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
        elif event.type == "agent.idle" and tab.handover == ENDING:
            self._end(tab, event.data.get("error"))
        elif event.type == "tab.opened" and tab.machine:
            if event.data.get("continues") is None and running(self.session):
                # One handed on is told by `_end`; this one follows a machine tab closed.
                self.events.emit("coordinator.machine_restarted", tab=tab.tab_id, deliver={
                    "content": machine_restart_message(None, self._last_report(), True),
                    "meta": {"from": "daemon"}})
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
            if tab.handover == RESTART_ON_IDLE:
                self.session.hand_over(tab.tab_id, None)
                if error is None:
                    if (waiting := self.questions.tab_state(tab.tab_id)) is not None:
                        content = (f"Tab {tab.tab_id} ({tab.body}) was not restarted at its "
                                   f"turn's end as you asked: it is {waiting} on a question "
                                   "or permission, which a restart would withdraw")
                    else:
                        try:
                            started = hand_on(self.session, self.channels, tab.tab_id)
                        except Exception as exc:  # noqa: BLE001 — said; this thread must not end
                            self.events.emit("coordinator.restart_failed", tab=tab.tab_id,
                                             error=str(exc))
                            content = (f"Tab {tab.tab_id} ({tab.body}) was not restarted at its "
                                       f"turn's end as you asked: {exc}")
                        else:
                            content = (f"Tab {tab.tab_id} ({tab.body}) was restarted at its "
                                       f"turn's end as you asked: {started['next']}")
                    self._to_machine(event.tab, "coordinator.restarted_on_idle", content,
                                     why="restarted")
                    return
            if error is None and tab.handover == "heard":
                self.session.hand_over(tab.tab_id, "ready")
            if error is None and tab.handover == "ready" and self._time_up(tab, time.time()):
                self._give_back(tab)
                return
            # Only the newest turn end of a tab is where it stands (`channel.py`, `standing`).
            self._to_machine(event.tab, TURN_ENDED, self._idle_message(
                event.tab, tab.body, event.data["done"], error, event.ts), why="idle",
                standing=f"{event.tab} turn end")
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
        for fault in self.faults.untold():
            self._fault_answered(fault)

    def _turn(self, event: Event) -> None:
        """A turn of the machine tab's or a managed tab's starting or ending, for the idle
        watch. The wake's own turn does not end the stretch it woke; its end re-arms the wake,
        which the run's quiet since then, at twice the wait, sets off."""
        tab = self.session.intent.tabs.get(event.tab) if event.tab else None
        if tab is None or not (tab.machine or tab.managed):
            return
        self._quiet_since = event.ts
        if tab.machine:
            self._machine_quiet_since = event.ts
        if event.type == "agent.idle":
            self._ended[tab.tab_id] = (event.data["done"], event.data.get("error"), event.ts)
            if tab.machine and self._wake_turn == "on":
                self._woken, self._wake_turn = False, None
                self._wait = min(2 * self._wait, IDLE_CEILING_SECONDS)
        elif tab.machine and self._wake_turn == "due":
            self._wake_turn = "on"
        else:
            self._woken, self._wake_turn, self._wait = False, None, IDLE_SECONDS

    def _wake_if_idle(self, now: float) -> None:
        intent = self.session.intent
        machine = intent.machine_tab()
        if (self._woken or self._limited or not running(self.session) or machine is None
                or machine.status != "running" or machine.busy):
            return
        managed = [t for t in intent.tabs.values() if t.managed]
        ids = {t.tab_id for t in managed}
        pending = self.questions.pending()
        # What has itself waited the span, not what arrived as the machine tab's quiet passed
        # it: a notice built in the second a message lands is pushed after the reply to it.
        waiting = [f"Tab {i['tab']} asked {i['id']}: {i['message']!r}; answer it with "
                   "`answer_question`." for i in pending
                   if i["kind"] == "question" and i["tab"] in ids
                   and now - i["asked_at"] >= IDLE_SECONDS]
        waiting += [f"Tab {m['sender']} asks you, as message {m['id']}: "
                    f"{m['content'][:DIRECTION_EXCERPT]!r}; answer it with `reply`."
                    for m in self.channels.messages.items()
                    if m["state"] == "open" and m["to"] == machine.tab_id
                    and now - m["sent_at"] >= IDLE_SECONDS]
        resumed = {limits.LIMIT, *limits.TRANSIENT}
        run_idle = (
            now - self._quiet_since >= self._wait
            and not any(t.busy for t in managed)
            # Waiting on the user — a question or permission put to them, or every tab held
            # or paused for them — or on the daemon resuming a cut turn, is not stalled.
            and not any(i["tab"] in ids | {machine.tab_id} and not (
                i["kind"] == "question" and i["tab"] in ids) for i in pending)
            and any(t.status != "paused" and t.held is None for t in managed)
            and not any(self._ended.get(t, (None, None, None))[1] in resumed
                        for t in ids | {machine.tab_id}))
        if not (run_idle or waiting and now - self._machine_quiet_since >= self._wait):
            return
        self._woken, self._wake_turn = True, "due"
        if run_idle:
            minutes = int((now - self._quiet_since) // 60)
            said = [f"From the daemon: you and every tab you manage have been idle for "
                    f"{minutes} minutes, and nothing that waits on the user explains it; a "
                    "notice may not have reached you. Where each tab stands:"]
            for tab in managed:
                if tab.status == "paused":
                    said.append(f"- Tab {tab.tab_id} ({tab.body}) is paused for the user.")
                elif tab.held is not None:
                    said.append(f"- Tab {tab.tab_id} ({tab.body}) is held for the user.")
                else:
                    said.append("- " + self._idle_message(
                        tab.tab_id, tab.body, *self._ended.get(tab.tab_id, (None, None, None))))
            if waiting:
                said.append("Waiting on you:\n" + "\n".join(f"- {w}" for w in waiting))
            said.append("Carry the run on: direct each tab, `hold` one at its stop, or give "
                        "back one whose work is done.")
        else:
            minutes = int((now - self._machine_quiet_since) // 60)
            said = [f"From the daemon: you have been idle for {minutes} minutes while this "
                    "waits on you; a notice may not have reached you.",
                    *(f"- {w}" for w in waiting)]
        self.events.emit(IDLE_WOKEN, tab=machine.tab_id, deliver={
            "content": "\n".join(said), "meta": {"from": "daemon"}})

    def _reopened(self, tabs: list[str], mid_turn: list[str], what: str) -> None:
        """The machine tab told, while a run is on, that its tabs' agents were reopened, and
        `what` reopened them: a reopened conversation is idle, and the run goes on only when
        it is told to."""
        machine = self.session.intent.machine_tab()
        if machine is None or not running(self.session):
            return
        intent = self.session.intent
        said = [f"From the daemon: {what}; the agents of {', '.join(tabs)} were reopened on "
                "their conversations."]
        for tab_id in mid_turn:
            tab = intent.tabs.get(tab_id)
            if tab_id == machine.tab_id:
                said.append("Your own turn was cut off by it: carry it on from where it "
                            "stopped.")
            elif tab is None or not tab.managed:
                continue
            elif tab.held is not None:
                said.append(f"Tab {tab_id} ({tab.body}) was mid-turn and is held for the "
                            "user; it is left as it stands.")
            else:
                said.append(f"Tab {tab_id} ({tab.body}) was mid-turn and is told to "
                            "continue its turn; you are told when it ends.")
        said.append("Call `managed` for where each tab you manage stands, and continue the "
                    "run.")
        self.events.emit("coordinator.reopened", tab=machine.tab_id, deliver={
            "content": " ".join(said), "meta": {"from": "daemon"}})

    def tick(self, now: float) -> None:
        """Each managed tab whose time has run out: its wrap-up pushed, unless a handover
        already has it making its documents ready; given back once they are, or at once
        while it is held for the user, who has it as it stands. A run gone idle woken, and a
        fault left waiting by a keeper gone said."""
        self._wake_if_idle(now)
        self._tell_stranded(now)
        for tab in list(self.session.intent.tabs.values()):
            if not tab.managed or tab.status == "paused" or not self._time_up(tab, now):
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
        state = ("was held for them, and is theirs as it stands" if held
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

    def _end(self, tab: TabIntent, error: str | None) -> None:
        """A tab that said it is ending its conversation, handed on as its turn ends: a new
        tab takes over from its `SESSION-START.md`, and a managed tab's machine tab is told.
        A turn ended by an error is resumed and ends again; one with a question or permission
        pending waits, since a restart would withdraw it."""
        if error is not None or self.questions.tab_state(tab.tab_id) is not None:
            return
        # Its last turn's end, read before its home is archived: the machine tab hears it here
        # in place of `coordinator.idle`, and cannot view a closed tab.
        home = self.session.agents.home(tab.tab_id)
        last = transcript_tail(home, 1) if tab.managed else []
        tokens = context_tokens(home) if tab.managed else None
        try:
            successor = self.session.succeed_tab(tab.tab_id, by="self")
        except Exception as exc:  # noqa: BLE001 — said, and this thread must not end
            self.events.emit("coordinator.restart_failed", tab=tab.tab_id, error=str(exc))
            return
        handed = self.channels.handed_on(successor.tab_id)
        if tab.machine:
            # What was on its way to the machine tab went on to its successor, so `announce`,
            # as the successor opens, does not send it twice.
            for message in handed.carried:
                if message.cause == "coordinator.question":
                    self._handed.add((successor.tab_id, message.meta["question"]))
                elif message.cause == REPORT_ASKED:
                    run = self.session.intent.run
                    self._handed.add((successor.tab_id, f"run@{run.started}"))
            self.events.emit("coordinator.machine_restarted", tab=successor.tab_id, deliver={
                "content": machine_restart_message(tab.tab_id, self._last_report(),
                                                   running(self.session)),
                "meta": {"from": "daemon"}})
            return
        opened = f"opened as {tab.tab_id} ended its conversation"
        if tab.managed:
            opened += "; the machine tab manages this work for the user"
        self.events.emit("coordinator.continued", tab=successor.tab_id, deliver={
            "content": restart_message(successor.tab_id, tab.tab_id, opened),
            "meta": {"from": "daemon"}})
        if tab.managed:
            used = (f" at {tokens // 1000}k of the "
                    f"{self.session.context_budget(tab.tab_id) // 1000}k budget"
                    if tokens is not None else "")
            report = (f" Its last turn ended{used}, saying:\n\n{last[0]['said']}"
                      if last and last[0]["role"] == "assistant" else
                      f" Its last turn ended{used} with nothing said.")
            self._to_machine(successor.tab_id, "coordinator.continued_itself", (
                f"Tab {tab.tab_id} ({tab.body}) ended its own conversation; "
                f"{successor.tab_id} takes over its work from its SESSION-START.md, and you "
                "manage it now." + crashed_line(successor)
                + handed_on_line(handed, successor.tab_id) + report),
                why="continued")

    def _machine_idle(self, tab_id: str, handover: str | None, error: str | None) -> None:
        """The machine tab asked to hand itself on, only while the user is away
        (`Intent.hands_off`): asked at its budget, and handed on by `_end` once it says
        ready. A turn that heard it and ended without saying so is the tab failing, and
        asking again would only repeat it: it is said as `coordinator.unanswered`, which the
        janitor takes, and nothing more is asked until it says ready."""
        if not self.session.intent.hands_off(tab_id):
            if handover is not None:
                self.session.hand_over(tab_id, None)    # the user is back: theirs to close
            return
        if error is not None:
            return
        if handover in ("asked", "unanswered"):
            return          # its push is on its way, or the janitor has it
        if handover == "heard":
            self.session.hand_over(tab_id, "unanswered")
            reply = transcript_tail(self.session.agents.home(tab_id), 1)
            self.events.emit("coordinator.unanswered", tab=tab_id, message=(
                f"The machine tab {tab_id} was asked to make its SESSION-START.md ready and call "
                "`end_conversation` at its context budget, and ended its turn without doing "
                "so; nothing more is asked of it. It restarts once it calls "
                "`end_conversation`. Its last words: "
                + (reply[0]["said"] if reply else "(none)")))
            return
        tokens = context_tokens(self.session.agents.home(tab_id))
        budget = self.session.context_budget(tab_id)
        if tokens is None or tokens < budget:
            return
        self.session.hand_over(tab_id, "asked")
        self.events.emit(RESTART_ASKED, tab=tab_id, deliver={
            "content": restart_asked_message(tokens or budget, budget),
            "meta": {"from": "daemon"}})

    def _last_report(self) -> str | None:
        """The catalog id of the run's latest progress report, if it has one."""
        run = self.session.intent.run
        if run is None or run.checkpoints == 0:
            return None
        return run_id(self.session.progress_report(run, run.checkpoints))

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

    def _idle_message(self, tab_id: str, body: str | None, done: bool | None,
                      error: str | None = None, at: float | None = None) -> str:
        """`done` None: how its last turn ended is not known, having ended before the daemon
        started. A turn not done names what it waits on as it stands now; with nothing left
        that it waits on, it only ended."""
        if done is None:
            ended = "is idle; its last turn ended before the daemon started"
        elif error is not None:
            ended = (f"had its turn cut off by an API error ({error}) that continuing will not "
                     "fix; the user is told")
        elif done:
            ended = "is done"
        else:
            waits = on_its_way(self.channels, self.questions, tab_id)
            ended = "ended its turn" + (f" and waits on {'; '.join(waits)}" if waits else "")
        if at is not None:
            ended += f". That turn ended at {clock(at)}"
        if self.session.intent.tabs[tab_id].handover == "ready":
            start = self.session.session_start(tab_id)
            size = (f"It is {len(start.encode())} bytes; hold it to any cap the project sets."
                    if start is not None else "It has none yet.")
            return (f"Tab {tab_id} ({body}), which you manage, has made its documents ready for "
                    f"its next conversation. Read its {documents.SESSION_START}, which is all "
                    f"that conversation starts from, with `managed_tab`. {size} `direct` it to "
                    "fix what is stale or missing, or `restart_fresh` it to hand the work to "
                    "that conversation.")
        budget = self.session.context_budget(tab_id)
        return (f"Tab {tab_id} ({body}), which you manage, {ended}. "
                f"{_context_line(context_tokens(self.session.agents.home(tab_id)), budget)} "
                "`managed_tab` shows its thought doc and latest turns."
                f"{stop_line(self.session.intent.tabs[tab_id])}")

    def _restated(self, message: Message) -> str | None:
        """A turn end as it stands when pushed. None once the tab has closed, started another
        turn, been given back or had a turn cut that the daemon resumes: whatever it said then
        no longer holds, and a newer turn end follows where one is due."""
        tab = self.session.intent.tabs.get(message.meta["tab"])
        if tab is None or tab.busy or not tab.managed:
            return None
        done, error, at = self._ended.get(tab.tab_id, (None, None, None))
        if error == limits.LIMIT or error in limits.TRANSIENT:
            return None
        return self._idle_message(tab.tab_id, tab.body, done, error, at)

    def _to_machine(self, about: str, event: str, content: str, **meta: str) -> None:
        machine = self.session.intent.machine_tab()
        if machine is None:
            # Nothing open to hear it; the next machine tab is told what is still pending.
            self.events.emit("coordinator.unheard", about=about, cause=event)
            return
        self.events.emit(event, tab=machine.tab_id, about=about, deliver={
            "content": content, "meta": {"tab": about, **meta}})

    def _fault_answered(self, fault: dict[str, Any]) -> None:
        """The keeper's answer to a fault the machine tab reported (`faults.py`)."""
        machine = self.session.intent.machine_tab()
        self._to_machine(fault["id"], "coordinator.fault_answered", answer_message(fault),
                         why="fault", verdict=fault["status"])
        if machine is not None:
            self.faults.told(fault["id"], machine.tab_id)

    def _tell_stranded(self, now: float) -> None:
        """A fault still waiting while no keeper is reachable, said once to the machine tab
        that is open, with the fallback a `not_fixed` answer gives (`faults.stranded`)."""
        machine = self.session.intent.machine_tab()
        if machine is None:
            return
        stranded, when = self.faults.stranded(now)
        for fault in stranded:
            self._to_machine(fault["id"], "coordinator.fault_stranded",
                             stranded_message(fault, when), why="fault")
            self.faults.told_stranded(fault["id"], machine.tab_id)


def methods(session: "Session", questions: Questions, channels: Channels,
            caller: str) -> dict[str, Callable[..., Any]]:
    """The machine tab's verbs on its own socket, refused on any other tab's; and every
    tab's own `end_conversation` and `document_save`."""
    verbs = Verbs(session, questions, channels)
    library = Library(session.paths, session, parse_permissions, PERMISSIONS_ABSENT)

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

    def end_conversation(report: str | None = None) -> dict[str, Any]:
        """The caller handed on to a new tab as this turn ends (`Coordinator._end`). The
        machine tab during a run hands over `report`, its progress report on its stretch,
        filed as the run's checkpoint."""
        tab = session.intent.tabs[caller]
        if tab.held is not None:
            raise SessionError("you are held for the user: put the situation to them and end "
                               "your turn; their answer is your next message")
        if tab.handover in (RESTART_ON_IDLE, "ready"):
            raise SessionError("the machine tab is handing you on already; end your turn")
        filed: dict[str, Any] = {}
        if tab.machine and running(session):
            filed = session.checkpoint_run(caller, report or "", record(), time.time())
        elif report is not None:
            raise SessionError("`report` is the machine tab's progress report on a run's "
                               "stretch; there is no run to report on")
        session.hand_over(caller, ENDING)
        return {**filed, "status": "ending",
                "next": f"End your turn; a new tab takes over from "
                        f"/work/{documents.SESSION_START} once it ends."}

    def report_run(report: str) -> dict[str, Any]:
        return session.report_run(caller, report, record())

    def report_fault(title: str, body: str, affects: list[str] | None = None) -> dict[str, Any]:
        """A bug in RaiGolmi itself, filed for the keeper outside the VM (`faults.py`)."""
        for tab in affects or ():
            verbs._managed(tab)
        store = Faults(session.paths, session.events)
        fault = store.report(caller, title, body, affects or [])
        reachable, when = store.keeper_reachable(time.time())
        if reachable:
            return {"fault": fault["id"], "keeper": "reachable", "next": (
                f"Filed; {when}, and takes it at its next look. You are told its answer: "
                "`resume` the tabs you paused for it when it says the fix is in.")}
        store.told_stranded(fault["id"], caller)    # this answer is the telling
        return {"fault": fault["id"], "keeper": "unreachable", "next": (
            f"Filed, but no keeper is reachable ({when}): work around it. A keeper that "
            "comes back takes it, and you are told its answer.")}

    def document_save(id: str, text: str, version: str | None = None) -> dict[str, Any]:
        """Every tab reads the catalog (`library.served_to_tabs`); the machine's documents are
        the machine tab's to change, through the same checks as the user's own save."""
        tab = session.intent.tabs.get(caller)
        if tab is None or not tab.machine:
            raise SessionError("the catalog's documents are the machine tab's to change: "
                               "message it (`to` \"machine\") with the change; your own "
                               "project's documents are in /work")
        return save_from_tab(library, id, text, version)

    return {name: only_machine(verb) for name, verb in (
        *((n, getattr(verbs, n)) for n in ("manage", "managed", "managed_tab", "direct",
                                           "answer_question", "restart_fresh", "hold",
                                           "pause", "resume")),
        ("report_run", report_run),
        ("report_fault", report_fault),
        # `rai claude-update`'s; the caller is mid-turn, so it moves as that turn ends.
        ("update_claude_code", lambda pinned=False: session.update_claude_code(pinned)),
        ("export_machine", lambda: export_machine(session)),
        # What the tabs it manages declared against their principles (`principles.py`).
        ("principles", lambda body, rows=60: session.principles.ledger(body, rows)),
        ("principles_checked", lambda body, tabs, drift, direction="", deep=False:
            session.principles.checked(body, tabs, drift, direction, deep)))
    } | {"document_save": document_save, "end_conversation": end_conversation}


def export_machine(session: "Session") -> dict[str, Any]:
    """`rai export`'s, into the transfer folder's `out`, which the launcher empties to the
    user's Windows transfer folder."""
    made = machinecopy.export(session.paths, session.search, session.paths.transfer / "out")
    return {"file": made.name, "bytes": made.stat().st_size}


def running(session: "Session") -> bool:
    """A run in progress: the machine tab managing for the user while they are away."""
    run = session.intent.run
    return run is not None and run.ended is None


def hand_on(session: "Session", channels: Channels, tab: str) -> dict[str, Any]:
    """A new tab takes over `tab`'s work from its `SESSION-START.md`, and `tab` is closed."""
    successor = session.succeed_tab(tab, by="machine")
    handed = channels.handed_on(successor.tab_id)
    session.events.emit("coordinator.restarted", tab=successor.tab_id, deliver={
        "content": restart_message(successor.tab_id, tab, "opened by the machine tab, which "
                                   "manages this work for the user"),
        "meta": {"from": "machine"}})
    return {"tab": successor.tab_id, "continues": tab, "status": "started",
            "next": f"{successor.tab_id} takes over {tab}'s work, which is closed and "
                    f"archived; you manage {successor.tab_id} now, and are told when its "
                    "first turn ends." + crashed_line(successor)
                    + handed_on_line(handed, successor.tab_id)}


def crashed_line(successor: TabIntent) -> str:
    """A successor whose agent did not start (`Session.succeed_tab`), said; empty otherwise."""
    if successor.status != "crashed":
        return ""
    return (f" Its agent did not start, so {successor.tab_id} is crashed and the janitor has it; "
            "what is on its way to it waits until it is up.")


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
        if not on and (agent := self.session.intent.tabs.get(tab)) is not None \
                and agent.status == "paused":
            raise SessionError(f"{tab} is paused, and only you resume it: `resume` it before "
                               "giving it back")
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
                "budget_tokens": self.session.context_budget(tab),
                "questions": [{"id": i["id"], "message": i["message"],
                               "choices": list(i["choices"])}
                              for i in pending if i["tab"] == tab and i["kind"] == "question"],
            })
        return out

    def managed_tab(self, tab: str, turns: int = TAIL_TURNS, lines: int = TAIL_LINES,
                    session_start: bool = True) -> dict[str, Any]:
        """One managed tab closer: its `SESSION-START.md`, which its next conversation starts
        from — its size always, its text unless `session_start` is false — and the tails of
        this conversation's thought doc and transcript; and the directions from the machine tab
        still on their way to it, so a machine tab that took over never sends one twice, beside
        everything its turn's end waits on (`on_its_way`)."""
        row = next((t for t in self.managed() if t["tab"] == tab), None)
        if row is None:
            raise SessionError(f"{tab} is not a tab you manage")
        home = self.session.agents.home(tab)
        start = self.session.session_start(tab)
        channel = self.channels.state(tab)
        return {**row, "directions_queued": [m["content"] for m in channel["queued"]
                                             if m["cause"] == DIRECTED],
                "on_its_way": on_its_way(self.channels, self.questions, tab),
                "session_start": start if session_start else None,
                "session_start_bytes": len(start.encode()) if start is not None else None,
                "thoughts_tail": thoughts_tail(home, lines),
                "transcript_tail": transcript_tail(home, turns)}

    def direct(self, tab: str, content: str) -> dict[str, Any]:
        """One way: a working tab reads it at its next tool call, an idle one starts on it;
        nothing comes back. One to a tab that has handed on goes to the tab that took its
        work over, as what was already on its way to it did."""
        addressed, tab = tab, self._successor(tab)
        agent = self._unheld(tab)
        if not content.strip():
            raise SessionError("a directive needs words")
        self.events.emit(DIRECTED, tab=tab, deliver={
            "content": DIRECTION + content, "meta": {"from": "machine"}})
        handed = {"ended": addressed} if addressed != tab else {}
        if agent.busy:
            return {"tab": tab, **handed, "status": "queued",
                    "next": "The tab is working: it reads this at its next tool call, or as its "
                            "next message if this turn ends first; you are told when it is idle."}
        return {"tab": tab, **handed, "status": "pushed",
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
        if agent.handover == ENDING:
            raise SessionError(f"{tab} is ending its own conversation: a new tab takes over "
                               "as its turn ends, and you are told")
        if agent.busy and documents_ready and agent.handover in (None, RESTART_ON_IDLE):
            # Its documents are ready and it is finishing a turn: restarted when that ends.
            self.session.hand_over(tab, RESTART_ON_IDLE)
            return {"tab": tab, "status": "restart_queued",
                    "next": "It is restarted when its turn ends, unless that turn ends on an "
                            "error or a question; you are told either way."}
        if agent.busy:
            raise SessionError(f"{tab} is working; restart it once it is idle, or say its "
                               "documents are ready to restart it when its turn ends")
        if (waiting := self.questions.tab_state(tab)) is not None:
            raise SessionError(f"{tab} is {waiting} on a question or permission, and a "
                               "restart would withdraw it; settle it first")
        if agent.handover == RESTART_ON_IDLE:
            self.session.hand_over(tab, None)
            agent.handover = None
        if agent.handover != "ready" and not (documents_ready and agent.handover is None):
            if agent.handover is None:
                self.session.hand_over(tab, "asked")
                self.events.emit(WRAP_UP, tab=tab, deliver={
                    "content": wrap_up_message(), "meta": {"from": "machine"}})
            return {"tab": tab, "status": "wrapping_up",
                    "next": "It is making its documents ready for its next conversation; you "
                            "are told when that turn ends, and `restart_fresh` then restarts "
                            "it."}
        return hand_on(self.session, self.channels, tab)

    def hold(self, tab: str, situation: str) -> dict[str, Any]:
        """The tab stopped for the user where they said, and told to put `situation` to
        them. Only at a `stop_when`: a hold is released only by the user's words in the tab."""
        agent = self._unheld(tab)
        if agent.stop_when is None:
            raise SessionError(f"{tab} has no stop_when, and a held tab is released only by the "
                               "user, who is away: hold a tab only at the stop the user gave "
                               "it, and decide everything else for them")
        if not situation.strip():
            raise SessionError("a hold needs the situation: where the work stands and what is "
                               "the user's to decide")
        held = self.session.hold(tab, situation)
        self.events.emit("coordinator.held", tab=tab, deliver={
            "content": hold_message(agent.stop_when, situation), "meta": {"from": "machine"}})
        return {**held, "next": "It is the user's until they answer in it; you are told then."}

    def pause(self, tab: str) -> dict[str, Any]:
        """At the user's word to you: the tab's work frozen where it stands, and their time
        for it standing still, until they tell you to `resume` it."""
        self._managed(tab)
        paused = self.session.pause(tab)
        return {**paused, "next": "Nothing of it runs and nothing reaches it, and its time "
                                  "stands still; `resume` it when the user says."}

    def resume(self, tab: str) -> dict[str, Any]:
        self._managed(tab)
        resumed = self.session.resume(tab)
        return {**resumed, "next": "It goes on where it stood; you are told when its turn "
                                   "ends, as before."}

    def _unheld(self, tab: str):
        agent = self._managed(tab)
        if agent.status == "paused":
            raise SessionError(f"{tab} is paused for the user; `resume` it first")
        if agent.held is not None:
            raise SessionError(f"{tab} is held for the user; nothing reaches it until they "
                               "answer in it, and you are told then")
        if agent.until is not None and agent.until <= time.time():
            raise SessionError(f"the user's time for {tab} ran out at {clock(agent.until)}; "
                               "it is making its documents ready and is given back to them "
                               "once that turn ends")
        return agent

    def _successor(self, tab: str) -> str:
        """The open tab doing *tab*'s work: itself, or the one at the end of the handovers
        that `continues` it. A closed tab is gone from the intent, so the hops through closed
        ones are read from the history's `tab.opened`."""
        tabs = self.session.intent.tabs
        if tab in tabs:
            return tab
        if not self.session.intent.issued(tab):
            raise SessionError(f"no tab {tab!r} has opened on this machine; `managed` lists "
                               "the tabs you manage")
        continued = {e.data["continues"]: e.tab for e in self.events.read()
                     if e.type == "tab.opened" and e.data.get("continues")}
        continued |= {t.continues: t.tab_id for t in tabs.values() if t.continues}
        at, seen = tab, set()
        while at is not None and at not in seen:
            if at in tabs:
                return at
            seen.add(at)
            at = continued.get(at)
        raise SessionError(f"{tab} has ended and no open tab continues its work")

    def _managed(self, tab: str):
        agent = self.session.intent.tabs.get(tab)
        if agent is None or not agent.managed:
            raise SessionError(f"{tab} is not a tab you manage; the user hands tabs over, and "
                               "`manage` marks one")
        return agent
