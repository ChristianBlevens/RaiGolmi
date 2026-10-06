"""The machine tab coordinates the body tabs the user hands it (`coordinator.py`):
it is told when their turns end and what they ask that the user's preferences cannot answer,
it answers and directs them, and it restarts one fresh at the context budget."""
from __future__ import annotations

import json

import pytest

from raigolmid import naming
from raigolmid.api import with_marks, with_tab_states
from raigolmid.channel import Channels
from raigolmid.coordinator import Coordinator
from raigolmid.janitor import TAKEN
from raigolmid.questions import Questions
from raigolmid.scopes import build_tab_methods
from raigolmid.session import SessionError
from raigolmid.viewing import Viewing
from tests import settingsdoc
from tests.harness import Harness, converse

MACHINE, BODY = "tab-1", "tab-2"


@pytest.fixture()
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.session.select("body", "myapi")
    assert (harness.tab(None), harness.tab("myapi")) == (MACHINE, BODY)
    return harness


class Machine:
    """The daemon's questions, channels and coordinator, pumped in this thread."""

    def __init__(self, h) -> None:
        self.h = h
        self.questions = Questions(h.events, h.paths)
        self.channels = Channels(h.session, h.events)
        self.coordinator = Coordinator(h.session, h.events, self.questions)
        for tab in (MACHINE, BODY):
            h.session.agent_session_started(tab)

    def tab(self, tab: str):
        return build_tab_methods(self.h.session, self.questions, self.channels, tab)

    def pump(self) -> None:
        while True:
            heard = self.questions._sub.drain(timeout=0.05)
            for event in heard:
                self.questions.on_event(event)
            told = self.coordinator._sub.drain(timeout=0.05)
            for event in told:
                self.coordinator.on_event(event)
            if not heard and not told:
                self.channels._catch_up()
                return

    def queued(self, tab: str) -> list[dict]:
        self.pump()
        return self.channels.state(tab)["queued"]

    def referred(self, asker: str, message: str = "which port?") -> str:
        """A question the preferences judge referred."""
        id = self.tab(asker)["ask"](message=message, choices=["8080", "9090"])
        self.h.events.emit("judge.referred", tab=asker, id=id)
        self.pump()
        return id


def _usage(home, tokens: int) -> None:
    converse(home)
    transcript = home / ".claude" / "projects" / "-work" / "s1.jsonl"
    with transcript.open("a") as rows:
        rows.write(json.dumps({"type": "assistant", "sessionId": "s1", "message": {
            "role": "assistant", "content": [{"type": "text", "text": "halfway"}],
            "usage": {"input_tokens": 10, "cache_creation_input_tokens": 1000,
                      "cache_read_input_tokens": tokens - 1010}}}) + "\n")
        # A subagent's turn is not the tab's context.
        rows.write(json.dumps({"type": "assistant", "isSidechain": True, "message": {
            "usage": {"input_tokens": 5}}}) + "\n")


def test_only_the_machine_tab_manages_and_only_body_tabs_are_managed(h):
    m = Machine(h)
    with pytest.raises(SessionError, match="only the machine tab"):
        m.tab(BODY)["managed"]()
    with pytest.raises(SessionError, match="not a body tab"):
        m.tab(MACHINE)["manage"](tab=MACHINE)
    managed = m.tab(MACHINE)["manage"](tab=BODY)
    assert (managed["managed"], managed["until"]) == (True, None)
    assert "/work/run.md" in managed["next"]
    assert h.session.intent.tabs[BODY].managed
    assert [e.data["on"] for e in h.events_of("tab.managed")] == [True]


def test_a_managed_tabs_turn_ending_wakes_the_machine_tab_with_its_context_use(h):
    m = Machine(h)
    m.tab(MACHINE)["manage"](tab=BODY)
    settingsdoc.write(h.session.paths.settings, agents={"context_budget_tokens": 100000})
    _usage(h.session.agents.home(BODY), 105_000)
    m.tab(BODY)["agent_activity"](busy=False)
    [woken] = m.queued(MACHINE)
    assert woken["meta"] == {"tab": BODY, "why": "idle"}
    assert "105k of the 100k budget" in woken["content"], "the user's settings' budget"
    assert "restart_fresh" in woken["content"], "at the budget it is told what to do"
    [row] = m.tab(MACHINE)["managed"]()
    assert (row["context_tokens"], row["budget_tokens"]) == (105_000, 100_000)
    detail = m.tab(MACHINE)["managed_tab"](tab=BODY)
    assert detail["transcript_tail"][-1] == {"role": "assistant", "said": "halfway"}


def test_the_machine_tab_is_not_marked_for_the_user_while_it_manages(h):
    m = Machine(h)
    viewing = Viewing(h.events, h.paths.viewing)
    h.events.emit("agent.idle", tab=MACHINE, done=True)
    for event in viewing._sub.drain(timeout=0.1):
        viewing.on_event(event)

    def machine() -> dict:
        status = with_tab_states(h.session.status(), m.questions, m.channels)
        return next(a for a in with_marks(status, viewing)["agents"] if a["tab"] == MACHINE)

    assert machine()["state"] == "idle" and machine()["marked"]
    m.tab(MACHINE)["manage"](tab=BODY)
    assert not machine()["marked"], "the user who handed tabs over is away"


def test_an_unmanaged_tab_wakes_nothing(h):
    m = Machine(h)
    m.tab(BODY)["agent_activity"](busy=False)
    m.referred(BODY)
    assert m.queued(MACHINE) == []


def test_what_the_judge_refers_goes_to_the_machine_tab_and_its_answer_to_the_asker(h):
    m = Machine(h)
    m.tab(MACHINE)["manage"](tab=BODY)
    id = m.referred(BODY)
    [asked] = m.queued(MACHINE)
    assert asked["meta"] == {"tab": BODY, "why": "question", "question": id}
    assert "which port?" in asked["content"] and "8080" in asked["content"]

    status = with_tab_states(h.session.status(), m.questions, m.channels)
    marks = with_marks(status, Viewing(h.events, h.paths.viewing))
    assert not next(a for a in marks["agents"] if a["tab"] == BODY)["marked"]
    assert not marks["terminal"]["lit"], "a managed tab's question is not the user's"

    m.tab(MACHINE)["answer_question"](id=id, answer="9090")
    [answered] = m.queued(BODY)
    assert "answered by the machine tab" in answered["content"] and "9090" in answered["content"]
    assert m.questions.items()[id]["by"] == "machine"
    assert not h.events_of("question.learn"), "the machine tab's answer is never learned"

    m.questions.overturn(id, "8080")
    assert h.events_of("question.learn")[-1].data["answer"] == "8080"


def test_a_tab_handed_over_while_asking_puts_its_question_to_the_machine_tab(h):
    m = Machine(h)
    id = m.referred(BODY)
    assert m.queued(MACHINE) == []
    m.tab(MACHINE)["manage"](tab=BODY)
    [asked] = m.queued(MACHINE)
    assert asked["meta"]["question"] == id


def test_a_new_machine_tab_is_told_what_is_still_pending(h):
    m = Machine(h)
    m.tab(MACHINE)["manage"](tab=BODY)
    m.referred(BODY)
    h.session.close_tab(MACHINE)
    new = h.session.intent.machine_tab().tab_id
    assert new != MACHINE
    [asked] = m.queued(new)
    assert asked["meta"]["tab"] == BODY


def test_a_directive_is_the_managed_tabs_next_message(h):
    m = Machine(h)
    with pytest.raises(SessionError, match="not a tab you manage"):
        m.tab(MACHINE)["direct"](tab=BODY, content="run the tests")
    m.tab(MACHINE)["manage"](tab=BODY)
    assert m.tab(MACHINE)["direct"](tab=BODY, content="run the tests")["status"] == "pushed", \
        "an idle tab starts on it at once"
    [directed] = m.queued(BODY)
    assert directed["meta"] == {"from": "machine"}
    assert directed["content"].startswith("From the machine tab")


def _hear(m, tab: str) -> str:
    """The tab's next push taken and its turn run, as its session would; what it said."""
    item = m.channels.take(tab)
    m.tab(tab)["agent_activity"](busy=True, channel_seq=item["seq"])
    m.tab(tab)["agent_activity"](busy=False)
    m.pump()
    return item["content"]


def test_a_fresh_restart_readies_the_tabs_documents_then_hands_its_work_to_a_new_tab(h):
    m = Machine(h)
    m.tab(MACHINE)["manage"](tab=BODY, stop_when="the parser ships", hours=4)
    home = h.session.agents.home(BODY)
    converse(home)
    (home / "thoughts.md").write_text("s1 found the lexer slow\n")

    m.tab(BODY)["agent_activity"](busy=True)
    with pytest.raises(SessionError, match="working"):
        m.tab(MACHINE)["restart_fresh"](tab=BODY)
    m.tab(BODY)["agent_activity"](busy=False)
    id = m.referred(BODY)
    with pytest.raises(SessionError, match="withdraw"):
        m.tab(MACHINE)["restart_fresh"](tab=BODY)
    m.tab(MACHINE)["answer_question"](id=id, answer="9090")
    m.pump()
    m.channels._tabs[BODY].queue.clear()        # heard, as the tab's turn would
    m.channels._tabs[MACHINE].queue.clear()

    assert m.tab(MACHINE)["restart_fresh"](tab=BODY)["status"] == "wrapping_up"
    # Asked again before that turn has run, it is still on its way, and nothing restarts.
    assert m.tab(MACHINE)["restart_fresh"](tab=BODY)["status"] == "wrapping_up"
    assert "SESSION-START.md" in _hear(m, BODY)
    assert not any(r for r in h.paths.agent_archive.glob("*.json"))
    [told] = m.queued(MACHINE)
    assert "made its documents ready" in told["content"]

    until = h.session.intent.tabs[BODY].until
    restarted = m.tab(MACHINE)["restart_fresh"](tab=BODY)
    new = restarted["tab"]
    assert (restarted["continues"], new != BODY) == (BODY, True)
    assert BODY not in h.session.intent.tabs
    assert h.session.intent.run.ended is None, "the run goes on across the handing-on"
    successor = h.session.intent.tabs[new]
    assert (successor.body, successor.managed, successor.stop_when, successor.until,
            successor.continues, successor.handover) == (
        "myapi", True, "the parser ships", until, BODY, None)
    # The old conversation and its thought doc are archived together, its record.
    [archived] = [r for r in h.paths.agent_archive.glob("*.json")
                  if json.loads(r.read_text())["tab"] == BODY]
    kept = archived.with_suffix("")
    assert (kept / "thoughts.md").read_text() == "s1 found the lexer slow\n"
    assert (kept / ".claude" / "projects" / "-work" / "s1.jsonl").is_file()
    new_home = h.session.agents.home(new)
    assert not (new_home / "thoughts.md").exists()
    assert not h.session.agents.has_conversation(new)
    # It starts from SESSION-START.md alone, as for the user, never the old thought doc.
    [start] = m.queued(new)
    assert "/work/SESSION-START.md" in start["content"]
    assert "lexer" not in start["content"]
    [row] = m.tab(MACHINE)["managed"]()
    assert (row["tab"], row["continues"]) == (new, BODY)
    # The machine tab hears of the new tab's turns without managing it again.
    m.channels._tabs[MACHINE].queue.clear()
    h.session.agent_session_started(new)
    m.tab(new)["agent_activity"](busy=True)
    m.tab(new)["agent_activity"](busy=False)
    [told] = m.queued(MACHINE)
    assert told["meta"]["tab"] == new


def test_the_machine_tab_at_its_budget_while_managing_confirms_then_hands_on_to_a_new_one(h):
    m = Machine(h)
    settingsdoc.write(h.session.paths.settings, agents={"context_budget_tokens": 100000})
    home = h.session.agents.home(MACHINE)
    _usage(home, 105_000)
    (home / "thoughts.md").write_text("tab-2 is porting the parser\n")
    record = h.session.paths.work / "run.md"
    record.write_text("23:00 handed tab-2\n")

    # Not managing, the user is there: theirs to close.
    m.tab(MACHINE)["agent_activity"](busy=True)
    m.tab(MACHINE)["agent_activity"](busy=False)
    assert m.queued(MACHINE) == []
    with pytest.raises(SessionError, match="nothing asked you"):
        m.tab(MACHINE)["ready_to_restart"](report="early")

    m.tab(MACHINE)["manage"](tab=BODY)
    m.tab(MACHINE)["agent_activity"](busy=True)
    m.tab(MACHINE)["agent_activity"](busy=False)
    m.pump()
    assert "`ready_to_restart`" in m.queued(MACHINE)[-1]["content"]

    # A turn that heard the ask and did not say ready is the tab failing: said once, for the
    # janitor, and not asked again however often it goes idle.
    _hear(m, MACHINE)
    for _ in range(3):
        m.tab(MACHINE)["agent_activity"](busy=True)
        m.tab(MACHINE)["agent_activity"](busy=False)
        m.pump()
    assert m.queued(MACHINE) == []
    [unanswered] = h.events_of("coordinator.unanswered")
    assert unanswered.tab == MACHINE and "ready_to_restart" in unanswered.data["message"]
    assert "coordinator.unanswered" in TAKEN

    # Repaired, it says ready with its progress report, a checkpoint of the run: filed with
    # this stretch's record and the daemon's, so the next stretch starts a record of its own.
    m.tab(MACHINE)["agent_activity"](busy=True)
    with pytest.raises(SessionError, match="needs words"):
        m.tab(MACHINE)["ready_to_restart"](report=" ")
    filed = m.tab(MACHINE)["ready_to_restart"](report="tab-2 ported the lexer")
    m.tab(MACHINE)["agent_activity"](busy=False)
    m.pump()
    run = h.session.intent.run
    assert (run.checkpoints, run.since is not None) == (1, True)
    stem = h.paths.runs / filed["report"].rsplit("/", 1)[-1]
    assert stem.name.endswith("-progress-01.md")
    assert stem.read_text().endswith("tab-2 ported the lexer\n")
    assert not record.exists()
    assert (h.paths.runs / filed["record"].rsplit("/", 1)[-1]).read_text() == \
        "23:00 handed tab-2\n"
    assert "handed to the machine tab" in \
        (h.paths.runs / filed["daemon_record"].rsplit("/", 1)[-1]).read_text()
    [checkpoint] = h.events_of("run.checkpoint")
    assert checkpoint.data["stretch"] == 1

    machine = h.session.intent.machine_tab()
    assert (machine.tab_id != MACHINE, machine.continues) == (True, MACHINE)
    assert MACHINE not in h.session.intent.tabs and h.session.intent.tabs[BODY].managed
    [archived] = [r for r in h.paths.agent_archive.glob("*.json")
                  if json.loads(r.read_text())["tab"] == MACHINE]
    assert (archived.with_suffix("") / "thoughts.md").read_text() == \
        "tab-2 is porting the parser\n", "archived with its conversation"
    new_home = h.session.agents.home(machine.tab_id)
    assert not (new_home / "thoughts.md").exists()
    told = m.queued(machine.tab_id)[-1]["content"]
    assert "/work/SESSION-START.md" in told and "parser" not in told


def test_a_tab_stops_where_the_user_said_and_is_held_until_they_answer(h):
    m = Machine(h)
    m.tab(MACHINE)["manage"](tab=BODY, stop_when="the parser passes its suite")
    converse(h.session.agents.home(BODY))
    m.tab(BODY)["agent_activity"](busy=True)
    m.tab(BODY)["agent_activity"](busy=False)
    assert "the parser passes its suite" in m.queued(MACHINE)[-1]["content"]

    container = h.runtime._containers[naming.agent(BODY)]["spec"]
    m.tab(MACHINE)["hold"](tab=BODY, situation="green; next is the grammar or the CLI")
    assert h.runtime._containers[naming.agent(BODY)]["spec"] is container, "not restarted"
    assert "the grammar or the CLI" in m.queued(BODY)[-1]["content"]

    # The user's until they answer: the machine tab neither hears its turns nor reaches it.
    m.channels._tabs[MACHINE].queue.clear()
    m.tab(BODY)["agent_activity"](busy=True)
    m.tab(BODY)["agent_activity"](busy=False)
    assert m.queued(MACHINE) == []
    with pytest.raises(SessionError, match="held"):
        m.tab(MACHINE)["direct"](tab=BODY, content="do the CLI")

    m.tab(BODY)["agent_activity"](busy=True, prompt="the grammar first")
    m.pump()
    assert h.session.intent.tabs[BODY].held is None
    assert "The user answered" in m.queued(MACHINE)[-1]["content"]
    m.tab(MACHINE)["direct"](tab=BODY, content="carry on")


def test_a_tab_whose_time_runs_out_makes_its_documents_ready_and_is_given_back(h):
    m = Machine(h)
    m.tab(MACHINE)["manage"](tab=BODY, hours=4)
    until = h.session.intent.tabs[BODY].until
    converse(h.session.agents.home(BODY))
    m.tab(BODY)["agent_activity"](busy=True)
    m.tab(BODY)["agent_activity"](busy=False)
    assert "runs out at" in m.queued(MACHINE)[-1]["content"]

    m.coordinator.tick(until - 1)
    assert m.queued(BODY) == []
    m.coordinator.tick(until)
    m.coordinator.tick(until + 1)
    [wrap_up] = m.queued(BODY)
    assert wrap_up["meta"] == {"from": "daemon"} and "SESSION-START.md" in wrap_up["content"]
    h.session.intent.tabs[BODY].until = 1.0    # the daemon's clock past it, for the verbs
    with pytest.raises(SessionError, match="ran out"):
        m.tab(MACHINE)["direct"](tab=BODY, content="one more thing")

    _hear(m, BODY)
    assert not h.session.intent.tabs[BODY].managed
    [given_back] = [e for e in h.events_of("tab.managed") if not e.data["on"]]
    assert given_back.data["why"] == "time"
    told = [q["content"] for q in m.queued(MACHINE)]
    assert "report_run" in told[-1] and not any("is given back to them" in t for t in told), \
        "the last tab's give-back is said by the report request alone"
    [ended] = h.events_of("run.ended")
    assert ended.data["ended"] >= ended.data["started"]


def test_a_run_ends_with_the_machine_tabs_report_filed_with_its_record(h):
    m = Machine(h)
    m.tab(MACHINE)["manage"](tab=BODY, stop_when="the parser passes")
    home, body_home = h.session.agents.home(MACHINE), h.session.agents.home(BODY)
    record = h.session.paths.work / "run.md"
    record.write_text("directed tab-2 to the parser\n")
    converse(body_home)
    (body_home / "thoughts.md").write_text("the parser is green\n")
    id = m.referred(BODY)
    m.tab(MACHINE)["answer_question"](id=id, answer="9090")
    with pytest.raises(SessionError, match="not over"):
        m.tab(MACHINE)["report_run"](report="done")

    stopped = m.tab(MACHINE)["manage"](tab=BODY, on=False)        # the user said stop
    assert "reaches you when this turn ends" in stopped["next"]
    with pytest.raises(SessionError, match="when this turn ends"):    # before its record
        m.tab(MACHINE)["report_run"](report="done")
    m.pump()
    [asked] = [q for q in m.queued(MACHINE) if "report_run" in q["content"]]
    assert asked["meta"] == {"from": "daemon"}
    assert "handed to the machine tab to manage, stopping for you at 'the parser passes'" \
        in asked["content"], "what the user gave it is in the record"
    assert f"you answered {id} 'which port?': '9090'" in asked["content"]
    assert "the parser is green" in asked["content"], "a tab given back is out of its reach"
    # A machine tab opening, or the daemon starting, asks again only what it has not heard.
    m.coordinator.announce()
    assert len([q for q in m.queued(MACHINE) if "report_run" in q["content"]]) == 1

    while h.session.intent.run.asked is None:
        _hear(m, MACHINE)
    filed = m.tab(MACHINE)["report_run"](report="tab-2: the parser passes; next is the CLI")
    assert h.session.intent.run is None
    report = h.paths.runs / filed["report"].rsplit("/", 1)[-1]
    assert report.read_text().endswith("tab-2: the parser passes; next is the CLI\n")
    assert not record.exists()
    assert (h.paths.runs / f"{report.stem}-record-01.md").read_text() == \
        "directed tab-2 to the parser\n"
    daemon = (h.paths.runs / f"{report.stem}-daemon-01.md").read_text()
    assert f"you answered {id}" in daemon and "taken back from the machine tab" in daemon
    [reported] = h.events_of("run.reported")
    assert reported.data["report"] == report.name
    with pytest.raises(SessionError, match="no run"):
        m.tab(MACHINE)["report_run"](report="again")


def _said(home, text: str) -> None:
    transcript = home / ".claude" / "projects" / "-work" / "s1.jsonl"
    with transcript.open("a") as rows:
        rows.write(json.dumps({"type": "assistant", "sessionId": "s1", "message": {
            "role": "assistant", "content": [{"type": "text", "text": text}]}}) + "\n")


def test_a_tabs_last_turn_reaches_the_machine_tab_whole(h):
    m = Machine(h)
    m.tab(MACHINE)["manage"](tab=BODY)
    home = h.session.agents.home(BODY)
    converse(home)
    _said(home, "x" * 5000)
    report = "found the leak; built the fix; next the CLI. " * 100
    _said(home, report)
    seen = m.tab(MACHINE)["managed_tab"](tab=BODY)
    assert seen["transcript_tail"][-1]["said"] == report.strip()
    assert len(seen["transcript_tail"][-2]["said"]) < 5000, "earlier turns stay short"
    (h.session._place(h.session.intent.tabs[BODY]).working_copy / "SESSION-START.md") \
        .write_text("# start\n")
    lean = m.tab(MACHINE)["managed_tab"](tab=BODY, session_start=False)
    assert (lean["session_start"], lean["session_start_bytes"]) == (None, 8)
    m.tab(BODY)["agent_activity"](busy=True)
    m.tab(MACHINE)["direct"](tab=BODY, content="park the parser")
    assert any("park the parser" in d for d in
               m.tab(MACHINE)["managed_tab"](tab=BODY)["directions_queued"])


def test_a_tab_already_ready_is_handed_on_in_one_call(h):
    m = Machine(h)
    m.tab(MACHINE)["manage"](tab=BODY)
    started = m.tab(MACHINE)["restart_fresh"](tab=BODY, documents_ready=True)
    assert (started["status"], started["continues"]) == ("started", BODY)
    assert not h.events_of("coordinator.wrap_up"), "no wrap-up turn"


def test_a_ready_tab_still_in_its_turn_is_restarted_as_that_turn_ends(h):
    m = Machine(h)
    m.tab(MACHINE)["manage"](tab=BODY)
    m.tab(BODY)["agent_activity"](busy=True)
    queued = m.tab(MACHINE)["restart_fresh"](tab=BODY, documents_ready=True)
    assert queued["status"] == "restart_queued"
    assert BODY in h.session.intent.tabs, "nothing cut off mid-turn"
    m.tab(BODY)["agent_activity"](busy=False)
    m.pump()
    assert BODY not in h.session.intent.tabs
    [told] = [q["content"] for q in m.queued(MACHINE) if "restarted at its turn's end" in
              q["content"]]
    assert "takes over" in told


def test_a_runs_report_after_a_checkpoint_covers_its_last_stretch(h):
    m = Machine(h)
    m.tab(MACHINE)["manage"](tab=BODY)
    first = m.referred(BODY, "which lexer?")
    m.tab(MACHINE)["answer_question"](id=first, answer="the fast one")
    h.session.hand_over(MACHINE, "asked")
    m.tab(MACHINE)["ready_to_restart"](report="stretch one: the lexer")
    later = m.referred(BODY, "which parser?")
    m.tab(MACHINE)["answer_question"](id=later, answer="pratt")
    m.tab(MACHINE)["manage"](tab=BODY, on=False)
    m.pump()
    [asked] = [q["content"] for q in m.queued(MACHINE) if "report_run" in q["content"]]
    assert "'which parser?'" in asked and "'which lexer?'" not in asked
    assert "first 1 stretch(es) are filed with their progress reports" in asked
    while h.session.intent.run.asked is None:
        _hear(m, MACHINE)
    filed = m.tab(MACHINE)["report_run"](report="the parser and the lexer are in")
    assert filed["daemon_record"].endswith("-daemon-02.md")
