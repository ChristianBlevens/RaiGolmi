"""The preferences judge: a question an agent asks the user is answered
from their preferences when those settle it, and every answer they give rewrites them so the
question would have been obvious.

It is its own system and meets `questions.py` only through events. A question to judge
arrives as `question.judging`; the judge answers `judge.answered` (the answer and the line of
the doc it came from), `judge.referred` (the doc does not settle it: it goes to the user), or
`judge.failed`. An answer to learn from arrives as `question.learn`, and the rewrite ends in
`judge.learned` or `judge.failed`.

**It is the doc's only agent writer; the user edits it too** (in the catalog). One worker takes
every job in the order it came, so no two of its writes meet and a question asked after an
answer is judged against the doc that answer rewrote; a rewrite is saved only over the text it
was made from (`docwrite.write`), so the user's edit is never lost to it. Events are taken on
their own thread and only queued there: a run takes seconds, and a subscription left unread
drops events (`events.Subscriber`).

A run is a one-shot `claude -p` in the agent image, with a placeholder for the credential (`credproxy.py`), no tools and
nothing mounted: the doc is in the prompt, and the new doc is the run's `result`. What the
run says is checked before it is believed — an answer's quote must be in the doc word for
word, and among the choices when there are any — and anything else is a failure with what it
printed, never a guess at what it meant. No doc, or an empty one, settles nothing, so
nothing is run.
"""
from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
from typing import Callable

from . import docwrite, labels, naming
from .credproxy import Broker
from .docwrite import StaleDocument
from .events import Event, EventLog
from .runtime.base import ContainerRuntime, ContainerSpec

# How long a hung run may keep a question from the user; a run takes 3–8 s.
RUN_TIMEOUT = 60.0

UNCLEAR = "UNCLEAR"

JUDGE_PROMPT = """\
You are the preferences judge for one user of RaiGolmi. An agent working for them has asked \
them the question below. Decide whether their answer is settled by their preferences document \
alone.

<preferences>
{doc}
</preferences>

<question>{message}</question>
<choices>{choices}</choices>

If the preferences settle the answer, reply with exactly two lines:
ANSWER: <the answer{choice_rule}>
QUOTE: <the one line of the preferences that settles it, copied exactly>
If they do not settle it — if you would be guessing, generalising past what they say, or \
weighing one preference against another — reply with exactly: UNCLEAR"""

LEARN_PROMPT = """\
You keep the preferences document for one user of RaiGolmi: how they want the agents that \
work for them to decide things, written so that a question like the one below can be answered \
from it without asking them. An agent asked them this question, and they answered it.

<preferences>
{doc}
</preferences>

<question>{message}</question>
<choices>{choices}</choices>
{overturned}<user-answer>{answer}</user-answer>

Rewrite the whole document so that this question's answer would have been clear from it. It \
states the user's preferences; it is never a log, so do not record that they were asked. \
Merge with and correct what is there, keep one preference per line as "- " bullets under \
short headings, keep everything that still holds, and keep it short. If their answer carries \
no preference beyond this one case, return the document unchanged. Reply with the document \
only: no commentary and no code fence."""


class JudgeError(Exception):
    """A run that did not give an answer the judge can believe."""


def parse_result(output: str) -> str:
    """The `result` of `claude -p --output-format json`: the one line of the run's output that
    is its result object. Other lines are what else the run printed, kept for the refusal."""
    for line in reversed(output.splitlines()):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            if obj.get("is_error") or not isinstance(obj.get("result"), str):
                raise JudgeError(f"the run reported an error: {line}")
            return obj["result"].strip()
    raise JudgeError(f"the run printed no result: {output.strip()[-2000:]!r}")


def parse_verdict(result: str, doc: str, choices: list[str]) -> tuple[str, str] | None:
    """(answer, quote), or None for UNCLEAR."""
    if result == UNCLEAR:
        return None
    lines = [line.strip() for line in result.splitlines() if line.strip()]
    if (len(lines) != 2 or not lines[0].startswith("ANSWER:")
            or not lines[1].startswith("QUOTE:")):
        raise JudgeError(f"the verdict is neither ANSWER/QUOTE nor {UNCLEAR}: {result!r}")
    answer = lines[0].removeprefix("ANSWER:").strip()
    quote = lines[1].removeprefix("QUOTE:").strip()
    if not answer or not quote:
        raise JudgeError(f"the verdict has an empty answer or quote: {result!r}")
    if quote not in doc:
        raise JudgeError(f"the quote is not in the preferences: {quote!r}")
    if choices and answer not in choices:
        raise JudgeError(f"the answer {answer!r} is not one of the choices {choices}")
    return answer, quote


def _choices(choices: list[str]) -> str:
    return ", ".join(choices) if choices else "none: the user answers in words"


class Judge:
    """Subscribed at construction, so no question asked after the daemon starts is missed."""

    def __init__(self, events: EventLog, runtime: ContainerRuntime, doc: Path,
                 broker: Broker, epoch: int, image: Callable[[], str]) -> None:
        self.events = events
        self.runtime = runtime
        self.doc = doc
        self.broker = broker
        self.epoch = epoch
        # The agent image's tag, built if it is not here (`Agents.image`).
        self.image = image
        self._sub = events.subscribe()
        self._jobs: queue.Queue[Event] = queue.Queue()

    def run(self, stop: threading.Event) -> None:
        worker = threading.Thread(target=self._work, args=(stop,), name="judge-worker",
                                  daemon=True)
        worker.start()
        while not stop.is_set():
            if not worker.is_alive():
                # Ending the daemon with it: every later question would stay `judging`.
                raise RuntimeError("the judge's worker ended while the daemon runs")
            for event in self._sub.drain(timeout=1.0):
                if event.type in ("question.judging", "question.learn"):
                    self._jobs.put(event)
            if self._sub.dropped:
                count, self._sub.dropped = self._sub.dropped, 0
                # `questions.py` offers again what it still has with the judge.
                self.events.emit("judge.events_dropped", count=count)
        worker.join()

    def _work(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                event = self._jobs.get(timeout=1.0)
            except queue.Empty:
                continue
            self.take(event)

    def take(self, event: Event) -> None:
        """One job, start to finish."""
        d = event.data
        stage = "judge" if event.type == "question.judging" else "learn"
        try:
            if stage == "judge":
                self._judge(event.tab, d["id"], d["message"], list(d["choices"]))
            else:
                self._learn(event.tab, d)
        except Exception as exc:                       # noqa: BLE001 - said as the job's end
            # Every failure ends the job the same way, and the worker lives on: a thread that
            # died on an unlisted one (Docker's own errors, a write refused) left every later
            # question `judging` for good, where nothing lapses it.
            self.events.emit("judge.failed", tab=event.tab, id=d["id"], stage=stage,
                             error=f"{type(exc).__name__}: {exc}")

    def _judge(self, tab: str | None, id: str, message: str, choices: list[str]) -> None:
        doc = self._read()
        if not doc.strip():
            self.events.emit("judge.referred", tab=tab, id=id, reason="no preferences yet")
            return
        choice_rule = ", which is one of the choices" if choices else ""
        verdict = parse_verdict(self._ask(JUDGE_PROMPT.format(
            doc=doc, message=message, choices=_choices(choices), choice_rule=choice_rule)),
            doc, choices)
        if verdict is None:
            self.events.emit("judge.referred", tab=tab, id=id, reason="not settled by them")
        else:
            self.events.emit("judge.answered", tab=tab, id=id, answer=verdict[0],
                             quote=verdict[1])

    def _learn(self, tab: str | None, d: dict) -> None:
        """The user edits the doc too, so the rewrite is saved only over the text it was made
        from; an edit of theirs during the run is learned from on a second run, never
        overwritten."""
        overturned = (f"<overturned>You had answered it from the preferences as "
                      f"{d['overturned']!r}; the user overturned that.</overturned>\n"
                      if d.get("overturned") else "")
        for _ in range(2):
            doc, version = docwrite.read(self.doc)
            doc = doc or ""
            new = self._ask(LEARN_PROMPT.format(
                doc=doc or "(empty: nothing is known about the user yet)", message=d["message"],
                choices=_choices(list(d["choices"])), overturned=overturned,
                answer=d["answer"]))
            if not new or new.startswith("```"):
                raise JudgeError(f"the rewrite is not a document: {new[:200]!r}")
            changed = new != doc.strip()
            if not changed:
                break
            try:
                docwrite.write(self.doc, new + "\n", version)
                break
            except StaleDocument:
                continue
        else:
            raise JudgeError(f"{self.doc} changed under both rewrites; {d['id']} is not learned")
        self.events.emit("judge.learned", tab=tab, id=d["id"], changed=changed)

    def _read(self) -> str:
        return docwrite.read(self.doc)[0] or ""

    def _ask(self, prompt: str) -> str:
        image = self.image()
        # Left by a daemon that stopped mid-run, it would refuse every run after it.
        self.runtime.remove(naming.judge(), force=True)
        result = self.runtime.run_to_completion(ContainerSpec(
            name=naming.judge(),
            image=image,
            entrypoint=("claude",),
            command=("-p", prompt, "--tools", "", "--output-format", "json",
                     "--no-session-persistence"),
            labels={labels.MANAGED: "true", labels.ROLE: str(labels.Role.JUDGE),
                    labels.EPOCH: str(self.epoch)},
            environment=self.broker.environment(naming.judge()),
            mounts=self.broker.mounts(),
            # The agent image's user, with nothing it could escalate to.
            cap_drop=("ALL",),
            security_opt=("no-new-privileges:true",),
        ), timeout=RUN_TIMEOUT)
        if result.exit_code != 0:
            raise JudgeError(f"the run exited {result.exit_code}: {result.output.strip()[-2000:]!r}")
        return parse_result(result.output)
