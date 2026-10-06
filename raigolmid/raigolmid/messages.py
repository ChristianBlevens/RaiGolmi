"""What agent tabs ask each other.

A tab messages another for what needs that agent's judgement — a body tab asking the machine
tab for a change to a face, which only the machine tab edits — and the answer returns to the
tab that asked. The machine tab and body tabs message each other; the janitor does neither,
since it takes failures and asks the user. A tab is addressed as `machine`, by the body it
works on, or by its id; the first two outlive a closed tab's id.

**A message does not block its sender**, as a question does not (`questions.py`): `send`
returns its id, and the sender ends its turn *waiting*, not done, while any message it sent is
open. The message reaches the recipient on its channel (`channel.py`) as a `deliver`, and its
`reply` reaches the sender on the sender's. **A recipient's turn that ends done with a message
still open did not answer it**, and the sender is told so. "Done" is decided here, under the
same lock `send` takes, so a message sent while a turn ends is either mail that keeps the turn
from being done or arrives after the decision — never settled unheard. A recipient's tab
closing settles its messages the same way; a sender's closing withdraws what it sent, and a
reply to one is refused.

Messages are written to `Paths.messages` on every change, so they outlive a daemon restart as
the tabs' sessions do, and so does the channel that carries them (`channel.py`).
"""
from __future__ import annotations

import itertools
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .events import Event, EventLog
from .intent import JANITOR, BrokenState, load_json, save_json

if TYPE_CHECKING:
    from .session import Session

MACHINE = "machine"


class MessageError(Exception):
    """A message to no tab that can take one, or a reply to no open message of this tab's."""


@dataclass
class Message:
    id: str
    sender: str
    to: str
    content: str
    sent_at: float
    state: str = "open"             # | "answered" | "unanswered" | "withdrawn"
    heard: bool = False             # its push started a turn in the recipient
    answer: str | None = None
    settled_at: float | None = None


def received_message(m: Message) -> str:
    return (f"Tab {m.sender} asks you, as message {m.id}:\n\n{m.content}\n\nAnswer with the "
            f"`reply` tool and message {m.id}. Ending your turn without replying tells "
            f"{m.sender} you did not answer.")


def answer_message(m: Message) -> str:
    asked = f"Tab {m.to} answered your message {m.id} — {m.content!r} —"
    if m.state == "answered":
        return f"{asked[:-2]}:\n\n{m.answer}"
    return (f"Your message {m.id} to tab {m.to} — {m.content!r} — was not answered: "
            f"{m.answer}. Decide without it, or ask again.")


class Messages:
    """Subscribed at construction, so a push heard before `run` is not missed."""

    def __init__(self, session: "Session", events: EventLog, store: Path) -> None:
        self.session = session
        self.events = events
        self.store = store
        self._sub = events.subscribe()
        # Reentrant: `end_turn`'s check reads the channel, which may emit while it is held.
        self._lock = threading.RLock()
        self._messages = self._load()
        last = max((int(i[1:]) for i in self._messages), default=0)
        self._ids = itertools.count(last + 1)

    # --- routing ----------------------------------------------------------------------
    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            for event in self._sub.drain(timeout=1.0):
                self.on_event(event)
            if self._sub.dropped:
                count, self._sub.dropped = self._sub.dropped, 0
                self.events.emit("messages.events_dropped", count=count)

    def on_event(self, event: Event) -> None:
        if event.type == "channel.heard" and "message" in event.data.get("meta", {}):
            with self._lock:
                m = self._messages.get(event.data["meta"]["message"])
                if m is not None and not m.heard:
                    m.heard = True
                    self._save()
        elif event.type == "tab.closed" and event.tab is not None:
            self._tab_gone(event.tab)

    def forget_absent_tabs(self, open_tabs: set[str]) -> None:
        """At the daemon's start: what a tab that is gone sent or was sent is settled."""
        with self._lock:
            gone = {t for m in self._open() for t in (m.sender, m.to) if t not in open_tabs}
        for tab in gone:
            self._tab_gone(tab)

    # --- the tabs' side ---------------------------------------------------------------
    def send(self, sender: str, to: str, content: str) -> dict:
        if not content.strip():
            raise MessageError("a message needs content")
        with self._lock:
            recipient = self._recipient(to)
            if recipient == sender:
                raise MessageError("that is this tab; a message goes to another tab")
            m = Message(id=f"m{next(self._ids)}", sender=sender, to=recipient,
                        content=content, sent_at=time.time())
            self._messages[m.id] = m
            self._save()
            self._deliver(m)
        return {"message": m.id, "to": recipient,
                "next": "End your turn when you have nothing else to do; the answer is your "
                        "next message."}

    def reply(self, tab: str, id: str, content: str) -> dict:
        with self._lock:
            m = self._messages.get(id)
            if m is None or m.to != tab:
                raise MessageError(
                    f"no message {id} was sent to this tab. `reply` answers a tab's `message`; "
                    "a direction from the machine tab, the janitor or the user is answered in "
                    "your turn's own text, which its sender reads when the turn ends")
            if m.state == "withdrawn":
                raise MessageError(f"tab {m.sender} closed; nobody is waiting on {id}")
            if m.state != "open":
                raise MessageError(f"message {id} is already {m.state}")
            self._settle(m, "answered", content)
        return {"answered": id, "to": m.sender}

    def waiting(self, tab: str) -> bool:
        """Whether this tab sent a message that is still open."""
        with self._lock:
            return any(m.sender == tab for m in self._open())

    def end_turn(self, tab: str, quiet: Callable[[], bool]) -> bool:
        """Whether the tab's ended turn is done: `quiet` (nothing asked of the user, nothing
        on its way to it) and no message of its own open. A done turn did not answer what it
        was sent."""
        with self._lock:
            done = quiet() and not any(m.sender == tab for m in self._open())
            if done:
                for m in [m for m in self._open() if m.to == tab]:
                    self._settle(m, "unanswered", "its turn ended without a reply")
            return done

    def items(self) -> list[dict]:
        with self._lock:
            return [asdict(m) for m in self._messages.values()]

    # --- inside -----------------------------------------------------------------------
    def _recipient(self, to: str) -> str:
        """Under the lock."""
        intent = self.session.intent
        tab = (intent.machine_tab() if to == MACHINE
               else intent.tabs.get(to) or intent.body_tab(to))
        if tab is None:
            raise MessageError(f"no open tab is {to!r}: name `machine`, a body with a tab, or "
                               "a tab id; `status` lists the tabs")
        if tab.tab_id == JANITOR:
            raise MessageError("the janitor takes the machine's failures, not messages")
        return tab.tab_id

    def _open(self) -> list[Message]:
        return [m for m in self._messages.values() if m.state == "open"]

    def _deliver(self, m: Message) -> None:
        """Under the lock, so `end_turn` sees it as the recipient's mail or not at all."""
        self.events.emit("message.sent", tab=m.to, id=m.id, sender=m.sender, deliver={
            "content": received_message(m), "meta": {"message": m.id, "from": m.sender}})

    def _settle(self, m: Message, state: str, answer: str) -> None:
        """Under the lock."""
        m.state, m.answer, m.settled_at = state, answer, time.time()
        self._save()
        self.events.emit(f"message.{state}", tab=m.sender, id=m.id, to=m.to, deliver={
            "content": answer_message(m), "meta": {"message": m.id, "from": m.to}})

    def _tab_gone(self, tab: str) -> None:
        with self._lock:
            for m in self._open():
                if m.to == tab:
                    self._settle(m, "unanswered", f"tab {tab} closed")
                elif m.sender == tab:
                    m.state, m.settled_at = "withdrawn", time.time()
                    self._save()
                    self.events.emit("message.withdrawn", tab=m.to, id=m.id, sender=tab)

    def _save(self) -> None:
        """Under the lock."""
        save_json(self.store, [asdict(m) for m in self._messages.values()])

    def _load(self) -> dict[str, Message]:
        raw = load_json(self.store, "messages between tabs")
        if raw is None:
            return {}
        try:
            return {d["id"]: Message(**d) for d in raw}
        except (TypeError, KeyError) as exc:
            raise BrokenState(self.store, "messages between tabs", exc) from exc
