"""The machine tab coordinating the body tabs the user hands it.

Which tabs are managed is the intent's (`TabIntent.managed`, set by `Session.manage`). This
system reads that mark and the events, and reaches the machine tab only by events whose data
carries `deliver`, which its channel holds until its turn ends (`channel.py`):

- a managed tab's turn ending, with its context use against the budget;
- a question the judge referred from a managed tab (`question.referred`), which the machine
  tab answers with `answer_question`; the same for a tab handed over while asking, and, when a
  machine tab opens, everything it has not heard because it was not there to hear it.

The machine tab's own verbs (`methods`) are refused to every other tab: the views of a
managed tab, a directive pushed into one, and a fresh restart that archives its conversation
and tells the new one to continue from `SESSION-START.md` and its thought doc.

**A handover takes two steps** (`TabIntent.handover`), because the next conversation has only
the documents: `restart_fresh` first pushes the wrap-up (`wrap_up_message`); once that turn has
started and ended the tab is ready, the machine tab is told, reads what it wrote, and the next
`restart_fresh` restarts it. **The machine tab hands itself over the same way** while it manages
tabs, since nobody else is there to close it: at its own budget the daemon asks it to make its
thought doc ready, it says so with `ready_to_restart`, and when that turn ends it is restarted
fresh, its thought doc passed on as `documents.PREVIOUS_THOUGHTS` for a new one of its own.

**A managed tab stops where the user said** (`TabIntent.stop_when`, given as they hand it
over, and in every message about it): at that goal or decision the machine tab `hold`s it on
the situation, and the tab is resumed on Remote Control to put it to them on their phone
(`Session.hold`). Nothing of the machine tab's reaches a held tab; the user's own words in it
release it (`Session.release`), and the machine tab is told.

Context use is the input the tab's latest main-conversation answer took — input, cache-read
and cache-created tokens, as Claude Code's own status line counts it — read from its newest
transcript. The daemon reads every tab's home; no tab can read another's.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from . import api, claude_login, documents, limits, settings
from .channel import Channels
from .events import Event, EventLog
from .questions import Questions
from .session import SessionError

if TYPE_CHECKING:
    from .session import Session

# How much of a managed tab a view shows by default.
TAIL_TURNS = 20
TAIL_LINES = 60
TURN_CHARS = 2000


def _latest_transcript(home: Path) -> Path | None:
    transcripts = list((home / ".claude" / "projects" / "-work").glob("*.jsonl"))
    return max(transcripts, key=lambda p: p.stat().st_mtime) if transcripts else None


def _main_rows(transcript: Path) -> list[dict[str, Any]]:
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
    transcript = _latest_transcript(home)
    if transcript is None:
        return None
    for row in reversed(_main_rows(transcript)):
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
    transcript = _latest_transcript(home)
    if transcript is None:
        return []
    tail = []
    for row in _main_rows(transcript):
        said = _said(row).strip()
        if said:
            tail.append({"role": row["type"], "said": said[:TURN_CHARS]})
    return tail[-turns:]


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


def stop_line(stop_when: str | None) -> str:
    if stop_when is None:
        return ""
    return (f" The user stops it at: {stop_when!r}. When that is reached, `hold` it for them; "
            "when you are unsure whether it is, go on, and have it note the doubt in its "
            "thought doc and commit, so they can return to that point.")


def question_message(item: dict[str, Any], body: str | None,
                     stop_when: str | None = None) -> str:
    choices = f" Its choices: {', '.join(item['choices'])}." if item["choices"] else ""
    return (f"Tab {item['tab']} ({body}), which you manage, asked the user {item['id']}: "
            f"{item['message']!r}.{choices} Their preferences did not answer it, so it is "
            f"yours: answer it with `answer_question`. Never put it to them."
            f"{stop_line(stop_when)}")


def hold_message(stop_when: str | None, situation: str) -> str:
    stop = f" at {stop_when!r}" if stop_when else ""
    return (f"From the machine tab: you are held for the user{stop}. The situation, as the "
            f"machine tab puts it:\n\n{situation}\n\nPut it to them now, in this "
            "conversation — they reach it from their phone through Remote Control, or at the "
            "screen: where the work stands, the choice or the next step that is theirs, the "
            "options and what you recommend. Then end your turn; their answer is your next "
            "message. Do nothing more of the work until it comes.")


def restart_message(tab: str, brief: str | None) -> str:
    said = (f"You are tab {tab}, restarted in a fresh conversation by the machine tab, which "
            "manages this tab for the user. Your previous conversation is archived. Read "
            f"/work/{documents.SESSION_START}, then your thought doc, ~/{documents.THOUGHTS}, "
            "and continue the work from them.")
    return said + (f"\n\nThe machine tab adds:\n\n{brief}" if brief else "")


WRAP_UP = "coordinator.wrap_up"
RESTART_ASKED = "coordinator.restart_asked"


def wrap_up_message() -> str:
    return (
        "From the machine tab, which manages this tab for the user: this conversation is at "
        "its context budget, and once this turn ends you are restarted in a fresh one that has "
        "only your documents. Make them ready for it now:\n"
        f"1. /work/{documents.SESSION_START}: where the work stands, what the next session "
        "takes and what it reads — with nothing stale in it: no history, nothing past-tense "
        "that nothing turns on.\n"
        f"2. ~/{documents.THOUGHTS}: the goal, what you found and decided, the state now.\n"
        "3. What outlives this work, in the permanent doc it belongs to.\n"
        "4. What should be committed, committed.\n"
        "Then end your turn saying the next session can continue from them.")


def restart_asked_message(tokens: int, budget: int) -> str:
    return (
        f"From the daemon: your context is at {tokens // 1000}k of the {budget // 1000}k "
        "budget, and the user is away, so you are restarted in a fresh conversation that has "
        f"only ~/{documents.THOUGHTS} to go on. Make it ready now: every tab you manage, what "
        "each is working toward, what you told each and what is on its way to or from it, "
        "whose handover stands where, and what you were in the middle of. Then call "
        "`ready_to_restart` and end your turn; you are restarted once it ends.")


def machine_restart_message() -> str:
    return (
        "You are the machine tab, restarted in a fresh conversation by the daemon at your "
        "context budget, while the user is away; your previous conversation is archived. "
        f"~/{documents.PREVIOUS_THOUGHTS} is its thought doc: read it, call `managed` for "
        "where each tab you manage stands now, and carry on managing them. Keep "
        f"~/{documents.THOUGHTS} afresh for this conversation.")


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
            if self._sub.dropped:
                # A dropped referral is a question nobody answers: said, and offered again.
                count, self._sub.dropped = self._sub.dropped, 0
                self.events.emit("coordinator.events_dropped", count=count)
                self.announce()

    def on_event(self, event: Event) -> None:
        tab = self.session.intent.tabs.get(event.tab) if event.tab else None
        if tab is None:
            return
        if event.type == "channel.heard":
            if event.data["cause"] in (WRAP_UP, RESTART_ASKED) and tab.handover == "asked":
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
                "goes on with their direction; it is yours again." + stop_line(tab.stop_when)),
                why="released")
        elif tab.held is not None:
            return      # the user's until they answer in it
        elif event.type == "agent.idle":
            error = event.data.get("error")
            if error == limits.LIMIT or error in limits.TRANSIENT:
                return      # resumed by the daemon, and heard of when that turn ends
            if error is None and tab.handover == "heard":
                self.session.hand_over(tab.tab_id, "ready")
            self._to_machine(event.tab, "coordinator.idle", self._idle_message(
                event.tab, tab.body, event.data["done"], error), why="idle")
        elif event.type == "question.referred" and event.data["kind"] == "question":
            self._question(event.data["id"])
        elif event.type == "tab.managed":
            for item in self._pending_of({event.tab}):
                self._question(item["id"])

    def announce(self) -> None:
        """Every managed tab's pending question, to the machine tab that is open now: at the
        daemon's start, whose channels hold nothing, and when a machine tab opens."""
        for item in self._pending_of(self.session.managed_tabs()):
            self._question(item["id"])

    def _machine_idle(self, tab_id: str, handover: str | None, error: str | None) -> None:
        """The machine tab's own handover, only while the user is away (`Intent.hands_off`):
        asked at its budget, asked again after a turn that heard it and did not say ready, and
        restarted after the turn that did."""
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
                self.session.restart_agent(tab_id, resume=False, new_thoughts=True)
            except Exception as exc:  # noqa: BLE001 — said, and this thread must not end
                self.events.emit("coordinator.restart_failed", tab=tab_id, error=str(exc))
                return
            self.events.emit("coordinator.machine_restarted", tab=tab_id, deliver={
                "content": machine_restart_message(), "meta": {"from": "daemon"}})
            return
        if handover == "asked":
            return          # its push is on its way
        tokens = context_tokens(self.session.agents.home(tab_id))
        budget = settings.load(self.session.paths.settings).budget_tokens
        if handover is None and (tokens is None or tokens < budget):
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
                         question_message(item, tab.body, tab.stop_when), why="question",
                         question=id)

    def _idle_message(self, tab_id: str, body: str | None, done: bool,
                      error: str | None = None) -> str:
        ended = ("is done" if done else "ended its turn and waits on something on its way"
                 if error is None else f"had its turn cut off by an API error ({error}) that "
                 "continuing will not fix; the user is told")
        if self.session.intent.tabs[tab_id].handover == "ready":
            return (f"Tab {tab_id} ({body}), which you manage, has made its documents ready for "
                    "its next conversation. Read them with `managed_tab` (and its "
                    f"{documents.SESSION_START} in the body); `direct` it to fix what is stale, "
                    "or `restart_fresh` it to start that conversation.")
        budget = settings.load(self.session.paths.settings).budget_tokens
        return (f"Tab {tab_id} ({body}), which you manage, {ended}. "
                f"{_context_line(context_tokens(self.session.agents.home(tab_id)), budget)} "
                "`managed_tab` shows its thought doc and latest turns."
                f"{stop_line(self.session.intent.tabs[tab_id].stop_when)}")

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

    def ready_to_restart() -> dict[str, Any]:
        tab = session.intent.tabs[caller]
        if tab.handover not in ("asked", "heard"):
            raise SessionError("nothing asked you to restart: the daemon asks at your context "
                               "budget while you manage tabs")
        session.hand_over(caller, "ready")
        return {"status": "ready", "next": "End your turn; you are restarted once it ends."}

    return {name: only_machine(verb) for name, verb in (
        *((n, getattr(verbs, n)) for n in ("manage", "managed", "managed_tab", "direct",
                                           "answer_question", "restart_fresh", "hold")),
        ("ready_to_restart", ready_to_restart))}


class Verbs:
    """What the machine tab does to the tabs it manages."""

    def __init__(self, session: "Session", questions: Questions, channels: Channels) -> None:
        self.session = session
        self.events = session.events
        self.questions = questions
        self.channels = channels

    def manage(self, tab: str, on: bool = True,
               stop_when: str | None = None) -> dict[str, Any]:
        return self.session.manage(tab, on, stop_when)

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
                "stop_when": intent.stop_when,
                "held": intent.held,
                "context_tokens": context_tokens(self.session.agents.home(tab)),
                "budget_tokens": budget,
                "questions": [{"id": i["id"], "message": i["message"],
                               "choices": list(i["choices"])}
                              for i in pending if i["tab"] == tab and i["kind"] == "question"],
            })
        return out

    def managed_tab(self, tab: str, turns: int = TAIL_TURNS,
                    lines: int = TAIL_LINES) -> dict[str, Any]:
        """One managed tab closer: the tail of its thought doc and of its conversation."""
        row = next((t for t in self.managed() if t["tab"] == tab), None)
        if row is None:
            raise SessionError(f"{tab} is not a tab you manage")
        home = self.session.agents.home(tab)
        return {**row, "thoughts_tail": thoughts_tail(home, lines),
                "transcript_tail": transcript_tail(home, turns)}

    def direct(self, tab: str, content: str) -> dict[str, Any]:
        """One way: it is the tab's next message once its turn ends; nothing comes back."""
        self._unheld(tab)
        if not content.strip():
            raise SessionError("a directive needs words")
        self.events.emit("coordinator.directed", tab=tab, deliver={
            "content": f"From the machine tab, which manages this tab for the user:\n\n"
                       f"{content}", "meta": {"from": "machine"}})
        return {"tab": tab, "status": "queued",
                "next": "It is pushed when the tab's turn ends; you are told when it is idle."}

    def answer_question(self, id: str, answer: str) -> dict[str, Any]:
        item = self.questions.items().get(id)
        if item is None:
            raise SessionError(f"no question {id}")
        self._managed(item["tab"])
        self.questions.answer_by_machine(id, answer)
        return {"question": id, "status": "answered",
                "next": "The user sees this answer in the history and may overturn it."}

    def restart_fresh(self, tab: str, brief: str | None = None) -> dict[str, Any]:
        """The tab's handover: first its wrap-up pushed, then, once that turn has ended, the
        tab in a new conversation that continues from its documents, its old one archived.
        Refused while it works, since that would cut its turn off, and while anything is
        asked of the user or the machine tab, since a restart withdraws it."""
        agent = self._unheld(tab)
        if agent.busy:
            raise SessionError(f"{tab} is working; restart it once it is idle")
        if (waiting := self.questions.tab_state(tab)) is not None:
            raise SessionError(f"{tab} is {waiting} on a question or permission, and a "
                               "restart would withdraw it; settle it first")
        if agent.handover != "ready":
            if agent.handover is None:
                self.session.hand_over(tab, "asked")
                self.events.emit(WRAP_UP, tab=tab, deliver={
                    "content": wrap_up_message(), "meta": {"from": "machine"}})
            return {"tab": tab, "status": "wrapping_up",
                    "next": "It is making its documents ready for its next conversation; you "
                            "are told when that turn ends, and `restart_fresh` then restarts "
                            "it."}
        restarted = self.session.restart_agent(tab, resume=False)
        self.events.emit("coordinator.restarted", tab=tab, deliver={
            "content": restart_message(tab, brief), "meta": {"from": "machine"}})
        return restarted

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
        return agent

    def _managed(self, tab: str):
        agent = self.session.intent.tabs.get(tab)
        if agent is None or not agent.managed:
            raise SessionError(f"{tab} is not a tab you manage; the user hands tabs over, and "
                               "`manage` marks one")
        return agent
