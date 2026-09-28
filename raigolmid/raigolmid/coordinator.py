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
and tells the new one to continue from its thought doc.

Context use is the input the tab's latest main-conversation answer took — input, cache-read
and cache-created tokens, as Claude Code's own status line counts it — read from its newest
transcript. The daemon reads every tab's home; no tab can read another's.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from . import api, documents, settings
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
        said += (" It is at the budget: have it bring its thought doc up to date (`direct`), "
                 "and once it is idle, `restart_fresh` it.")
    return said


def question_message(item: dict[str, Any], body: str | None) -> str:
    choices = f" Its choices: {', '.join(item['choices'])}." if item["choices"] else ""
    return (f"Tab {item['tab']} ({body}), which you manage, asked the user {item['id']}: "
            f"{item['message']!r}.{choices} Their preferences did not answer it, so it is "
            f"yours: answer it with `answer_question`. Never put it to them.")


def restart_message(tab: str, brief: str | None) -> str:
    said = (f"You are tab {tab}, restarted in a fresh conversation by the machine tab, which "
            "manages this tab for the user. Your previous conversation is archived. Read your "
            f"thought doc, ~/{documents.THOUGHTS}, and continue the work from it.")
    return said + (f"\n\nThe machine tab adds:\n\n{brief}" if brief else "")


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
        if event.type == "tab.opened" and tab.machine:
            self.announce()
        elif not tab.managed:
            return
        elif event.type == "agent.idle":
            self._to_machine(event.tab, "coordinator.idle", self._idle_message(
                event.tab, tab.body, event.data["done"]), why="idle")
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
                         question_message(item, tab.body), why="question", question=id)

    def _idle_message(self, tab_id: str, body: str | None, done: bool) -> str:
        ended = "is done" if done else "ended its turn and waits on something on its way"
        budget = settings.load(self.session.paths.settings).budget_tokens
        return (f"Tab {tab_id} ({body}), which you manage, {ended}. "
                f"{_context_line(context_tokens(self.session.agents.home(tab_id)), budget)} "
                "`managed_tab` shows its thought doc and latest turns.")

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

    return {name: only_machine(getattr(verbs, name)) for name in (
        "manage", "managed", "managed_tab", "direct", "answer_question", "restart_fresh")}


class Verbs:
    """What the machine tab does to the tabs it manages."""

    def __init__(self, session: "Session", questions: Questions, channels: Channels) -> None:
        self.session = session
        self.events = session.events
        self.questions = questions
        self.channels = channels

    def manage(self, tab: str, on: bool = True) -> dict[str, Any]:
        return self.session.manage(tab, on)

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
            out.append({
                **agent,
                "body": agent["scope"]["body"],
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
        self._managed(tab)
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
        """The tab in a new conversation that continues from its thought doc, its old one
        archived. Refused while it works, since that would cut its turn off, and while
        anything is asked of the user or the machine tab, since a restart withdraws it."""
        agent = self._managed(tab)
        if agent.busy:
            raise SessionError(f"{tab} is working; restart it once it is idle")
        if (waiting := self.questions.tab_state(tab)) is not None:
            raise SessionError(f"{tab} is {waiting} on a question or permission, and a "
                               "restart would withdraw it; settle it first")
        restarted = self.session.restart_agent(tab, resume=False)
        self.events.emit("coordinator.restarted", tab=tab, deliver={
            "content": restart_message(tab, brief), "meta": {"from": "machine"}})
        return restarted

    def _managed(self, tab: str):
        agent = self.session.intent.tabs.get(tab)
        if agent is None or not agent.managed:
            raise SessionError(f"{tab} is not a tab you manage; the user hands tabs over, and "
                               "`manage` marks one")
        return agent
