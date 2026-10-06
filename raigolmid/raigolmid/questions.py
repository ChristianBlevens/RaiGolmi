"""What agents ask the user and how each was settled, and what each tab
is waiting on. The menu shows them in the history (`history.py`), which reads them here.

Two kinds of item. A **question** is put by an agent through its `ask_user` tool: a message,
optional choices, and an answer in words, which goes back to the asker and to no one else. The
user answers it by typing in the asker's tab: a prompt they type there is that tab's next
message, so it withdraws the tab's questions and nothing is sent (`answered_in_terminal`). A
**permission** is the daemon's, raised by a gated action a tab's tool call makes: yes or no,
answered in the tab's own terminal window (`answer`), and carried out or not by
`permissions.py`, never through `ask_user` and never withdrawn by the asker's typing. An item
is `pending` until it is settled, and a settled item is kept with its outcome.

**A question does not block its asker.** `ask` registers it and returns, and the agent ends
its turn waiting on the user. The outcome is sent into the asker's session as its next
message, on its channel (`channel.py`): the event that settles it carries it as `deliver`. An
item left the user's settings' `lapse_minutes` unanswered lapses: "no answer", and the asker
decides without them. **A question is withdrawn when its asker's conversation ends** — a
restart that does not resume it, or the tab closing — and outlives a restart or crash-reopen
that resumes it, whose answer still belongs to that conversation. **A permission is withdrawn
when its asker's agent stops**: the process that waited on it is gone, whatever conversation
follows.

**The preferences judge answers first** (`judge.py`, which this meets only through events). A
question is `judging` until the judge settles it from the user's preferences
(`by: preferences`, with the line it came from) or refers it to them, when it is `pending`. A
judge that fails refers the question. **Every answer the user gives is learned** — typed in the tab,
or by overturning the judge's (`overturn`) — and an overturn reaches the asker only after the
judge has rewritten the doc with it (before the tab goes on); the tab is *waiting* until then.
Their typing has already started the tab's turn, so it is learned without being held.

**A tab the machine tab manages** has what the judge refers answered by the machine
tab (`answer_by_machine`, `by: machine`) — which tabs are managed is the intent's, and the
machine tab is handed the question by `coordinator.py`. Its answer is not learned, and the
user overturns it as they do the judge's.

**A permission answered *always* is remembered**, per action, for the project it was asked in
(the sandbox's body) or everywhere, and the same action asked again there is settled with
that answer, `by: always`, without being put to the user; the history still shows it, so they
see what was done in their name. **The *always* answers are a document of the user's**,
`Paths.permissions`, one line each (`PERMISSIONS_ABSENT` says the form); it is read at every
ask, so their edit in the catalog is what the next ask sees, and a line it cannot read refuses
their save naming the line.

The items are written to `Paths.questions` on every change, so they outlive a daemon restart; so does a pending item, because the asker's session outlives
the daemon too. `forget_absent_tabs` withdraws what was asked by a tab that is gone.
"""
from __future__ import annotations

import itertools
import re
import threading
import time
from dataclasses import asdict, dataclass

from . import docwrite, settings
from .docwrite import DocumentError
from .events import Event, EventLog
from .intent import BrokenState, load_json, save_json
from .paths import Paths

NO_ANSWER = "no answer"
PERMISSION_CHOICES = ("yes", "no")
ALWAYS_SCOPES = ("project", "everywhere")

PERMISSIONS_ABSENT = """\
# Permissions answered always. The machine reads this each time a permission is asked, and
# settles one that a line here matches without asking you. One answer per line:
#
#   - yes toolbelt_swap everywhere
#   - no toolbelt_swap in <project>
#
# Delete a line to be asked again.
"""
_ALWAYS_LINE = re.compile(r"^- (yes|no) ([a-z_]+) (?:everywhere|in (\S+))$")

# The process a permission waits in is gone after any of these.
PROCESS_GONE = frozenset({"agent.stopped", "agent.crashed"})

SETTLED_AS = {"question.answered": "answered", "question.lapsed": "lapsed",
              "question.withdrawn": "withdrawn"}


class QuestionError(Exception):
    """An answer that names no pending item, or is not one the item takes."""


@dataclass
class Item:
    id: str
    kind: str                       # "question" | "permission"
    message: str
    tab: str
    choices: tuple[str, ...] = ()
    asked_at: float = 0.0
    action: dict | None = None      # a permission's gated action, as `permissions.py` reads it
    project: str | None = None      # a permission's project, which an *always* is kept for
    # | "judging" (a question with the preferences judge) | "answered" | "lapsed" | "withdrawn"
    state: str = "pending"
    outcome: str | None = None      # the answer, NO_ANSWER, or why it was withdrawn
    settled_at: float | None = None
    # who settled it: "terminal" | "always" | "preferences" | "machine" | "overturn" | None
    by: str | None = None
    quote: str | None = None        # the preference a `by: preferences` answer came from
    overturned: str | None = None   # the preferences' or machine tab's answer the user overturned
    learning: bool = False          # the user's overturn, held from the asker until it is learned
    asker_gone: bool = False        # its tab closed: nothing more is sent to it


def _number(id: str) -> int:
    return int(id[1:])


@dataclass(frozen=True)
class Always:
    do: str
    project: str | None             # None: everywhere
    answer: str

    def line(self) -> str:
        where = f"in {self.project}" if self.project else "everywhere"
        return f"- {self.answer} {self.do} {where}"


def parse_permissions(text: str) -> dict[tuple[str, str | None], Always]:
    """The *always* answers in the user's document; a line that is not one refuses, named."""
    found: dict[tuple[str, str | None], Always] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ALWAYS_LINE.match(line)
        if match is None:
            raise DocumentError(f"line {number} is not an answer: {raw!r}. Each is "
                                "`- yes|no <action> everywhere` or `… in <project>`")
        answer, do, project = match.groups()
        if (do, project) in found:
            raise DocumentError(f"line {number} answers {do} "
                                f"{'in ' + project if project else 'everywhere'} a second time")
        found[(do, project)] = Always(do=do, project=project, answer=answer)
    return found


def outcome_message(item: Item, answer: str | None) -> str:
    """What the asker reads as its next message."""
    asked = f"Your question {item.id} to the user — {item.message!r} —"
    if item.by == "preferences":
        return (f"{asked} was answered from their preferences, which say: {item.quote!r}. "
                f"They see this answer and may overturn it. The answer:\n\n{answer}")
    if item.by == "machine":
        return (f"{asked} was answered by the machine tab, which manages this tab for them. "
                f"They see this answer and may overturn it. The answer:\n\n{answer}")
    if item.by == "overturn":
        return (f"{asked} was answered with {item.overturned!r}, and they have overturned "
                f"that. Redo whatever that answer led you to do. Their answer:\n\n{answer}")
    return (f"{asked} got no answer: they left it unanswered. Decide without their choice, and "
            "say in this tab what you decided.")


class Questions:
    """Subscribed at construction, like the janitor, so no judge's verdict falls between the
    daemon's start and `run`."""

    def __init__(self, events: EventLog, paths: Paths) -> None:
        self.events = events
        self.paths = paths
        self.store = paths.questions
        self._sub = events.subscribe()
        self._lock = threading.Lock()
        self._items = self._load()
        last = max((_number(i) for i in self._items), default=0)
        self._ids = itertools.count(last + 1)

    # --- routing ----------------------------------------------------------------------
    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            for event in self._sub.drain(timeout=1.0):
                self.on_event(event)
            if self._sub.dropped:
                count, self._sub.dropped = self._sub.dropped, 0
                self.events.emit("questions.events_dropped", count=count)
            self.lapse()

    def on_event(self, event: Event) -> None:
        if event.type == "judge.answered":
            self._settle(event.data["id"], "question.answered", by="preferences",
                         deliver=event.data["answer"], quote=event.data["quote"])
        elif event.type == "judge.referred" or (
                event.type == "judge.failed" and event.data["stage"] == "judge"):
            self._refer(event.data["id"])
        elif event.type == "judge.learned" or event.type == "judge.failed":
            self._learned(event.data["id"])
        elif event.type == "judge.events_dropped":
            self.offer_to_judge()
        if event.tab is None:
            return
        if event.type in PROCESS_GONE:
            self._withdraw(event.tab, event.type, kinds=("permission",))
        elif event.type == "agent.restarted" and not event.data["resumed"]:
            self._withdraw(event.tab, "conversation_ended")
        elif event.type == "tab.closed":
            self._withdraw(event.tab, event.type)
            self._asker_gone(event.tab)

    def lapse(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            lapse = settings.current(self.paths, self.events).lapse_seconds
            old = [i.id for i in self._pending() if now - i.asked_at >= lapse]
        for id in old:
            self._settle(id, "question.lapsed", deliver=None)

    def forget_absent_tabs(self, open_tabs: set[str]) -> None:
        """At the daemon's start: what a tab that is gone asked, nothing waits on any more."""
        with self._lock:
            gone = {i.tab for i in self._items.values() if i.tab not in open_tabs}
        for tab in gone:
            self._withdraw(tab, "tab_gone")
            self._asker_gone(tab)

    def offer_to_judge(self) -> None:
        """What is still with the judge, offered again: at the daemon's start, whose judge
        heard nothing before it, and when the judge's events were dropped."""
        with self._lock:
            items = [i for i in self._items.values() if i.state == "judging" or i.learning]
        for item in items:
            if item.state == "judging":
                self._to_judge(item)
            else:
                self._to_learn(item, item.outcome)

    # --- the asker's side -------------------------------------------------------------
    def ask(self, tab: str, message: str, choices: list[str] | None = None) -> str:
        if not message.strip():
            raise QuestionError("a question needs a message")
        item = Item(id=self._next(), kind="question", message=message, tab=tab,
                    choices=tuple(choices or ()), asked_at=time.time(), state="judging")
        self._add(item)
        self._to_judge(item)
        return item.id

    def ask_permission(self, tab: str, message: str, action: dict,
                       project: str | None) -> tuple[str, Always | None]:
        """The item's id, and the *always* that answered it, if one did."""
        item = Item(id=self._next(), kind="permission", message=message, tab=tab,
                    choices=PERMISSION_CHOICES, asked_at=time.time(), action=action,
                    project=project)
        self._add(item)
        text, _ = docwrite.read(self.paths.permissions)
        answers = parse_permissions(text or "")
        always = answers.get((action["do"], project)) or answers.get((action["do"], None))
        if always is not None:
            self._settle(item.id, "question.answered", deliver=always.answer, by="always")
        return item.id, always

    def answered_in_terminal(self, tab: str, prompt: str) -> None:
        """A prompt the user typed into this tab: it is the answer to whatever it asked."""
        self._withdraw(tab, "answered_in_terminal", answer=prompt, kinds=("question",))

    def tab_state(self, tab: str) -> str | None:
        """What the tab waits on: "permission" or "asking" while one is put to the user,
        and "waiting" while a question is with the judge or their answer is held for learning."""
        with self._lock:
            mine = [i for i in self._items.values() if i.tab == tab]
            for kind, state in (("permission", "permission"), ("question", "asking")):
                if any(i.kind == kind and i.state == "pending" for i in mine):
                    return state
            if any(i.state == "judging" or i.learning and not i.asker_gone for i in mine):
                return "waiting"
            return None

    # --- the user's side --------------------------------------------------------------
    def pending(self) -> list[dict]:
        with self._lock:
            return [asdict(i) for i in self._pending()]

    def items(self) -> dict[str, dict]:
        """Every item by id, settled or not."""
        with self._lock:
            return {id: asdict(i) for id, i in self._items.items()}

    def answer(self, id: str, text: str, always: str | None = None) -> None:
        """A permission answered in its tab's window, yes or no, perhaps kept *always*."""
        item = self._get(id)
        if item.kind != "permission":
            raise QuestionError(f"{id} is a question; it is answered by typing in "
                                f"{item.tab}")
        if text not in PERMISSION_CHOICES:
            raise QuestionError(f"{id} is a permission; it is answered yes or no")
        if always is not None:
            if always not in ALWAYS_SCOPES:
                raise QuestionError(f"always is one of {ALWAYS_SCOPES}, not {always!r}")
            self._remember(Always(do=item.action["do"], answer=text,
                                  project=item.project if always == "project" else None))
        self._settle(id, "question.answered", deliver=text, by="terminal")

    def answer_by_machine(self, id: str, text: str) -> None:
        """The machine tab's answer to a question the judge referred; whether the
        asker is one it manages is the caller's to check."""
        if not text.strip():
            raise QuestionError("an answer needs words")
        item = self._get(id)
        if item.kind != "question":
            raise QuestionError(f"{id} is a permission; only the user answers those")
        self._settle(id, "question.answered", deliver=text, by="machine")

    def overturn(self, id: str, text: str) -> None:
        """The user's answer in place of the one the preferences or the machine tab gave."""
        if not text.strip():
            raise QuestionError("an overturn needs the user's answer")
        with self._lock:
            item = self._items.get(id)
            if item is None or item.by not in ("preferences", "machine"):
                raise QuestionError(f"{id} was not answered from the preferences or by the "
                                    "machine tab")
            item.overturned, item.outcome, item.by = item.outcome, text, "overturn"
            item.settled_at, item.learning = time.time(), True
            self._save()
        self.events.emit("question.overturned", tab=item.tab, id=id, kind=item.kind,
                         by="overturn", answer=text, overturned=item.overturned)
        self._to_learn(item, text)

    # --- internals --------------------------------------------------------------------
    def _next(self) -> str:
        return f"q{next(self._ids)}"

    def _pending(self) -> list[Item]:
        return [i for i in self._items.values() if i.state == "pending"]

    def _get(self, id: str) -> Item:
        with self._lock:
            item = self._items.get(id)
            if item is None or item.state != "pending":
                raise QuestionError(f"no pending question or permission {id}")
            return item

    def _add(self, item: Item) -> None:
        with self._lock:
            self._items[item.id] = item
            self._save()
        self.events.emit("question.asked", tab=item.tab, **{
            k: v for k, v in asdict(item).items() if k != "tab"})

    def _remember(self, always: Always) -> None:
        """The user's answer replaces any line for the same action and place; their other lines
        and comments stay as they wrote them."""
        def change(text: str | None) -> str:
            lines = (PERMISSIONS_ABSENT if text is None else text).splitlines()
            kept = [line for line in lines
                    if (m := _ALWAYS_LINE.match(line.strip())) is None
                    or (m.group(2), m.group(3)) != (always.do, always.project)]
            return "\n".join([*kept, always.line()]) + "\n"
        docwrite.update(self.paths.permissions, change)
        self.events.emit("permission.always", **asdict(always))

    def _withdraw(self, tab: str, reason: str, answer: str | None = None,
                  kinds: tuple[str, ...] = ("question", "permission")) -> None:
        with self._lock:
            gone = [i for i in self._items.values()
                    if i.state in ("pending", "judging") and i.tab == tab and i.kind in kinds]
        for item in gone:
            self._settle(item.id, "question.withdrawn", reason=reason,
                         by="terminal" if reason == "answered_in_terminal" else None)
            if answer is not None:
                self._to_learn(item, answer)

    def _to_judge(self, item: Item) -> None:
        self.events.emit("question.judging", tab=item.tab, id=item.id, message=item.message,
                         choices=list(item.choices))

    def _to_learn(self, item: Item, answer: str) -> None:
        self.events.emit("question.learn", tab=item.tab, id=item.id, message=item.message,
                         choices=list(item.choices), answer=answer,
                         overturned=item.overturned, held=item.learning)

    def _refer(self, id: str) -> None:
        with self._lock:
            item = self._items.get(id)
            if item is None or item.state != "judging":
                return
            item.state = "pending"
            self._save()
        self.events.emit("question.referred", tab=item.tab, id=id, kind=item.kind)

    def _learned(self, id: str) -> None:
        """The judge is done with the user's answer: the asker may go on."""
        with self._lock:
            item = self._items.get(id)
            if item is None or not item.learning:
                return
            item.learning = False
            self._save()
        data = {} if item.asker_gone else {"deliver": {
            "content": outcome_message(item, item.outcome), "meta": {"question": id}}}
        self.events.emit("question.delivered", tab=item.tab, id=id, kind=item.kind,
                         by=item.by, **data)

    def _asker_gone(self, tab: str) -> None:
        with self._lock:
            marked = [i for i in self._items.values() if i.tab == tab and not i.asker_gone]
            for item in marked:
                item.asker_gone = True
            if marked:
                self._save()

    def _settle(self, id: str, event: str, by: str | None = None, **data) -> None:
        """`deliver` given, answer text or None for "no answer", sends the outcome to the
        asker; left out, nothing is sent."""
        with self._lock:
            item = self._items.get(id)
            if item is None or item.state not in ("pending", "judging"):
                return
            item.state = SETTLED_AS[event]
            item.settled_at, item.by = time.time(), by
            item.quote = data.pop("quote", None)
            if "deliver" in data:
                answer = data["deliver"]
                item.outcome = NO_ANSWER if answer is None else answer
            else:
                item.outcome = data.get("reason")
            self._save()
        if "deliver" in data:
            answer = data.pop("deliver")
            data["answer"] = NO_ANSWER if answer is None else answer
            if item.kind == "question":
                data["deliver"] = {"content": outcome_message(item, answer),
                                   "meta": {"question": id}}
            else:
                # Carried out, or not, by `permissions.py`, which says what came of it.
                data["action"] = item.action
                data["project"] = item.project
        self.events.emit(event, tab=item.tab, id=id, kind=item.kind, by=by,
                         **({"quote": item.quote} if item.quote else {}), **data)

    def _save(self) -> None:
        """Under the lock."""
        save_json(self.store, {"items": [asdict(i) for i in self._items.values()]})

    def _load(self) -> dict[str, Item]:
        raw = load_json(self.store, "questions and permissions")
        if raw is None:
            return {}
        try:
            return {d["id"]: Item(**{**d, "choices": tuple(d["choices"])})
                    for d in raw["items"]}
        except (TypeError, KeyError, ValueError) as exc:
            raise BrokenState(self.store, "questions and permissions", exc) from exc
