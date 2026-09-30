"""What the daemon sends into an agent tab's session.

Every tab's MCP server is also its **channel**: it polls `take` on its tab's socket and pushes what it is handed as
`notifications/claude/channel`, which starts a turn in an idle session. Three things travel
on it: a failure the manager takes (`manager.py`), the outcome of a question a tab put to the
user (`questions.py`), and a message between tabs or its answer (`messages.py`, held here as
`messages`). No producer calls this module: an event whose data carries `deliver`
(`{"content", "meta"}`) is queued on the channel of the tab it names.

A push to a busy session is refused as untrusted, so a tab is handed one message at a time and
only while idle. A channel Claude Code has not registered drops a push silently, and nothing
tells the server when it is (a push even 9 ms early is lost). So a push counts as heard
only on positive evidence: the turn it starts, whose prompt hook reports the push's `seq`
(`agent.busy` with `channel_seq`). A turn the user typed carries none, so it never hears a
push. One not heard within `REPUSH_SECONDS` is pushed again, same seq; after
`PUSHES_BEFORE_DEAF` unheard pushes it is said as `channel.unheard`, and nothing more goes
into that channel until the tab's session comes up again, which puts it back at the front.
Nothing new is pushed while the account's usage limit holds (`account.limited`, `limits.py`).

Its callers read it synchronously — a turn ending asks whether a message is on its way before
the tab is closed as done — so every read first catches up on the events already emitted, and
an answer given a moment before cannot be missed.

**The manager takes each failure in a fresh conversation**: its memory is its documents,
and every failure it is handed is whole. So a failure due into a manager session that has
already heard one is held, and `manager.fresh_conversation` asks for a new session
(`manager.py`), into which it is pushed once that session is up. Not while a question the
manager asked is unanswered: the answer belongs to the conversation that asked. A failure whose
incident already has a message waiting is not queued again: that message sends the manager to
the incident's doc, which holds every occurrence (`documents.record_incident`).

**What is on its way outlives a daemon restart**, as the tabs' sessions do: each tab's
queue, its push not yet heard, and the manager's open questions are written to
`Paths.channels` on every change and read back at the start. A push the old daemon had not
heard is pushed again marked `redelivered`: its turn may have started while nothing was
listening for the hook that says so, and the agent is the one that can tell a repeat.
"""
from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .events import Event, EventLog
from .intent import load_json, save_json
from .messages import Messages

if TYPE_CHECKING:
    from .session import Session

# A delivered push starts its turn within a second, so these only set how soon a lost push is
# repeated and how soon a deaf channel is said; neither decides whether delivery works.
REPUSH_SECONDS = 10.0
# The event that queues a failure for the manager (`manager.py`).
FAILURE = "manager.queued"
PUSHES_BEFORE_DEAF = 3
# The channel asks every second, so this only sets how soon a poller that stopped is said.
SILENT_SECONDS = 30.0


@dataclass(slots=True)
class Message:
    content: str
    meta: dict[str, str]
    cause: str                      # the event type that queued it


@dataclass(slots=True)
class Pushed:
    seq: int
    message: Message
    at: float
    heard: bool = False
    pushes: int = 1


@dataclass
class Tab:
    queue: deque[Message] = field(default_factory=deque)
    pushed: Pushed | None = None
    deaf: bool = False
    # Whether the tab's current session has reported up. Its MCP server pushes from
    # `notifications/initialized`, which Claude Code answers before it registers the channel,
    # so a push made on that alone lands before anything listens.
    session_up: bool = False
    # The manager's: whether this session has heard a failure, the questions it asked still
    # unanswered, and whether a fresh session has been asked for.
    took_failure: bool = False
    open_questions: set[str] = field(default_factory=set)
    fresh_asked: bool = False
    # When the tab's poller last asked, or its session came up: a channel is known alive
    # only by asking, so one that stops asking with something waiting is said (`silent`).
    asked_at: float = field(default_factory=time.time)
    silent: bool = False


# How Claude Code hands a push from this plugin to the session and to its prompt hook: the
# content wrapped in a tag carrying the source and every meta key.
CHANNEL_SOURCE = "plugin:raigolmi:raigolmi"
_PUSHED = re.compile(r'\A\s*<channel source="' + re.escape(CHANNEL_SOURCE)
                     + r'"[^>]*?\sseq="(\d+)"')


def pushed_seq(prompt: str) -> int | None:
    """The push a prompt is, or None for one the user typed."""
    found = _PUSHED.match(prompt)
    return None if found is None else int(found.group(1))


def silent_message(tab: str) -> str:
    return (f"Tab {tab} has a message waiting and its channel has not asked for one in "
            f"{SILENT_SECONDS:.0f} s: the tab's MCP server is not running. Restart the tab "
            f"(`rai ai restart {tab}`).")


def unheard_message(tab: str) -> str:
    return (f"Tab {tab}'s channel took a message and no turn started on it: Claude Code has "
            f"not registered the channel. Restart the tab (`rai ai restart {tab}`).")


def _saved(message: Message) -> dict[str, Any]:
    return {"content": message.content, "meta": message.meta, "cause": message.cause}


def _loaded(saved: dict[str, Any], session_up: bool) -> Tab:
    """A tab's channel as the daemon before this one left it. Its push not heard goes back to
    the front marked `redelivered`; one heard was taken, and its turn is over or will end."""
    tab = Tab(queue=deque(Message(**m) for m in saved["queue"]), session_up=session_up,
              open_questions=set(saved["open_questions"]),
              took_failure=saved["took_failure"], fresh_asked=saved["fresh_asked"])
    pushed = saved["pushed"]
    if pushed is not None and not pushed["heard"]:
        tab.queue.appendleft(Message(content=pushed["content"],
                                     meta={**pushed["meta"], "redelivered": "true"},
                                     cause=pushed["cause"]))
    return tab


class Channels:
    """Subscribed at construction, so what the daemon's own start queues is not missed."""

    def __init__(self, session: "Session", events: EventLog) -> None:
        self.session = session
        self.events = events
        self._sub = events.subscribe()
        self._lock = threading.Lock()
        # Held while events are taken and applied, so they are applied in order by whichever
        # thread catches up.
        self._pump = threading.Lock()
        # A tab already running when the daemon starts kept its container, its session and
        # the MCP server that polls here, which outlives the daemon; it registered its channel
        # before this daemon existed and will not report up again.
        self._path = session.paths.channels
        saved = load_json(self._path, "the channels' queues") or {"seq": 0, "tabs": {}}
        self._seq = saved["seq"]
        self._tabs: dict[str, Tab] = {}
        for tab_id, tab in session.intent.tabs.items():
            up = tab.status == "running" and not tab.awaiting_session
            if tab_id in saved["tabs"]:
                self._tabs[tab_id] = _loaded(saved["tabs"][tab_id], session_up=up)
            elif up:
                self._tabs[tab_id] = Tab(session_up=True)
        self.messages = Messages(session, events, session.paths.messages)
        self._hold_until = 0.0

    # --- routing ----------------------------------------------------------------------
    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self._sub.wait(timeout=1.0)
            self._catch_up()
            self._say_silent()
            if self._sub.dropped:
                # A dropped event may be a message nobody will hear; the loss is said.
                count, self._sub.dropped = self._sub.dropped, 0
                self.events.emit("channel.events_dropped", count=count)

    def _catch_up(self) -> None:
        with self._pump:
            for event in self._sub.drain(timeout=0):
                self.on_event(event)

    def on_event(self, event: Event) -> None:
        if event.type in ("account.limited", "account.resumed"):
            # A push while the account's usage limit holds would only fail (`limits.py`).
            with self._lock:
                self._hold_until = event.data.get("hold_until") or 0.0
            return
        if event.tab is None:
            return
        deliver = event.data.get("deliver")
        with self._lock:
            if self._apply(event, deliver):
                self._save()

    def _apply(self, event: Event, deliver: dict | None) -> bool:
        """Whether what is written to `Paths.channels` changed."""
        if deliver is not None:
            tab = self._tab(event.tab)
            incident = deliver["meta"].get("incident")
            if incident is not None and any(m.meta.get("incident") == incident
                                            for m in tab.queue):
                self.events.emit("channel.joined", tab=event.tab, incident=incident,
                                 cause=event.type)
                return False
            tab.queue.append(Message(content=deliver["content"],
                                     meta=dict(deliver["meta"]), cause=event.type))
            tab.open_questions.discard(deliver["meta"].get("question"))
            return True
        if event.type == "question.asked" and event.data.get("kind") == "question":
            self._tab(event.tab).open_questions.add(event.data["id"])
            return True
        if event.type in ("question.withdrawn", "question.delivered"):
            self._tab(event.tab).open_questions.discard(event.data.get("id"))
            return True
        if event.type == "tab.closed":
            return self._tabs.pop(event.tab, None) is not None
        if event.type == "agent.started":
            self._tab(event.tab).session_up = False
            return False
        if event.type == "agent.busy":
            tab = self._tab(event.tab)
            if tab.pushed is None or tab.pushed.seq != event.data.get("channel_seq"):
                return False
            tab.pushed.heard = True
            if tab.pushed.message.cause == FAILURE:
                tab.took_failure = True
            self.events.emit("channel.heard", tab=event.tab, seq=tab.pushed.seq,
                             cause=tab.pushed.message.cause, meta=tab.pushed.message.meta)
            return True
        if event.type == "agent.idle":
            tab = self._tab(event.tab)
            if tab.pushed is None or not tab.pushed.heard:
                return False
            tab.pushed = None
            return True
        if event.type == "agent.session_started":
            # A new session registers the channel afresh: whatever it did not hear goes
            # back to the front, to be pushed into the session that can.
            tab = self._tab(event.tab)
            if tab.pushed is not None and not tab.pushed.heard:
                tab.queue.appendleft(tab.pushed.message)
            tab.pushed = None
            tab.deaf = False
            tab.session_up = True
            tab.took_failure = tab.fresh_asked = False
            tab.asked_at, tab.silent = time.time(), False
            return True
        return False

    def _save(self) -> None:
        """Under `_lock`."""
        save_json(self._path, {"seq": self._seq, "tabs": {
            tab_id: {
                "queue": [_saved(m) for m in tab.queue],
                "pushed": None if tab.pushed is None else {
                    **_saved(tab.pushed.message), "seq": tab.pushed.seq,
                    "heard": tab.pushed.heard},
                "open_questions": sorted(tab.open_questions),
                "took_failure": tab.took_failure,
                "fresh_asked": tab.fresh_asked,
            } for tab_id, tab in self._tabs.items()}})

    def _say_silent(self) -> None:
        """A tab whose poller has stopped cannot hear anything, and only the daemon can
        notice: `take` is the one place a channel shows it is alive. Said once, with
        something waiting and the tab idle, which is when a live poller would have asked."""
        now = time.time()
        with self._lock:
            for tab_id, tab in self._tabs.items():
                waiting = bool(tab.queue) or (tab.pushed is not None and not tab.pushed.heard)
                agent = self.session.intent.tabs.get(tab_id)
                idle = agent is not None and agent.status == "running" and not agent.busy
                if (tab.silent or not tab.session_up or not waiting or not idle
                        or now - tab.asked_at <= SILENT_SECONDS):
                    continue
                tab.silent = True
                self.events.emit("channel.silent", tab=tab_id, since=tab.asked_at,
                                 message=silent_message(tab_id))

    def _tab(self, tab_id: str) -> Tab:
        return self._tabs.setdefault(tab_id, Tab())

    # --- delivery (the tab's MCP server asks) ------------------------------------------
    def take(self, tab_id: str) -> dict[str, Any] | None:
        """The next message to push into this tab, or None. Handed out only to an idle tab
        with nothing still unheard, because a push into a busy turn is refused."""
        self._catch_up()
        agent = self.session.intent.tabs.get(tab_id)
        idle = agent is not None and agent.status == "running" and not agent.busy
        with self._lock:
            tab = self._tabs.get(tab_id)
            if tab is None:
                return None
            tab.asked_at, tab.silent = time.time(), False
            pushed = tab.pushed
            if pushed is not None:
                if pushed.heard or tab.deaf or time.time() - pushed.at <= REPUSH_SECONDS:
                    return None
                if pushed.pushes >= PUSHES_BEFORE_DEAF:
                    tab.deaf = True
                    self.events.emit("channel.unheard", tab=tab_id, seq=pushed.seq,
                                     cause=pushed.message.cause, pushes=pushed.pushes,
                                     message=unheard_message(tab_id))
                    return None
                # A session still coming up cannot hear it, so a push into it is no evidence
                # the tab is deaf: it waits, and `session_started` puts it back in front.
                if not idle or not tab.session_up:
                    return None
                pushed.pushes += 1
                pushed.at = time.time()
                self.events.emit("channel.repushed", tab=tab_id, seq=pushed.seq,
                                 cause=pushed.message.cause, pushes=pushed.pushes)
                return self._item(pushed)
            if not idle or not tab.session_up or not tab.queue or time.time() < self._hold_until:
                return None
            if tab.queue[0].cause == FAILURE and tab.took_failure and not tab.open_questions:
                if not tab.fresh_asked:
                    tab.fresh_asked = True
                    self._save()
                    self.events.emit("manager.fresh_conversation", tab=tab_id,
                                     failure=tab.queue[0].meta.get("failure"))
                return None
            self._seq += 1
            tab.pushed = Pushed(seq=self._seq, message=tab.queue.popleft(), at=time.time())
            self._save()
            self.events.emit("channel.pushed", tab=tab_id, seq=self._seq,
                             cause=tab.pushed.message.cause)
            return self._item(tab.pushed)

    @staticmethod
    def _item(pushed: Pushed) -> dict[str, Any]:
        return {"seq": pushed.seq, "content": pushed.message.content,
                "meta": {**pushed.message.meta, "seq": str(pushed.seq)}}

    def has_mail(self, tab_id: str) -> bool:
        """Whether a message is on its way into this tab: queued, or pushed and not heard."""
        self._catch_up()
        with self._lock:
            tab = self._tabs.get(tab_id)
            return tab is not None and (bool(tab.queue) or (
                tab.pushed is not None and not tab.pushed.heard))

    def turn_done(self, tab_id: str, quiet: bool) -> bool:
        """Whether a turn that ended is done: `quiet` (nothing asked of the user), nothing on its
        way to the tab, and nothing it sent another tab still open (`Messages.end_turn`)."""
        return self.messages.end_turn(tab_id, lambda: quiet and not self.has_mail(tab_id))

    def state(self, tab_id: str) -> dict[str, Any]:
        self._catch_up()
        with self._lock:
            tab = self._tabs.get(tab_id) or Tab()
            return {
                "queued": [{"cause": m.cause, "meta": m.meta, "content": m.content}
                           for m in tab.queue],
                "pushed": None if tab.pushed is None else {
                    "seq": tab.pushed.seq, "cause": tab.pushed.message.cause,
                    "heard": tab.pushed.heard},
                "deaf": tab.deaf,
            }

