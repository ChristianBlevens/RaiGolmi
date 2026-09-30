"""The machine tab coordinates the body tabs the user hands it (`coordinator.py`):
it is told when their turns end and what they ask that the user's preferences cannot answer,
it answers and directs them, and it restarts one fresh at the context budget."""
from __future__ import annotations

import json

import pytest

from raigolmid import claude_login, naming
from raigolmid.api import with_marks, with_tab_states
from raigolmid.channel import Channels
from raigolmid.coordinator import Coordinator
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
    assert m.tab(MACHINE)["manage"](tab=BODY) == {"tab": BODY, "managed": True}
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
    m.tab(MACHINE)["direct"](tab=BODY, content="run the tests")
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


def test_a_fresh_restart_first_has_the_tab_ready_its_documents_then_starts_it_on_them(h):
    m = Machine(h)
    m.tab(MACHINE)["manage"](tab=BODY)
    home = h.session.agents.home(BODY)
    converse(home)

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

    restarted = m.tab(MACHINE)["restart_fresh"](tab=BODY, brief="the tests are next")
    assert restarted["resumed"] is False
    assert h.session.intent.tabs[BODY].handover is None
    assert not h.session.agents.has_conversation(BODY), \
        "a crash before the new session's first turn must not --continue the old one"
    [archived] = [r for r in h.paths.agent_archive.glob("*.json")
                  if json.loads(r.read_text()).get("fresh_restart")]
    assert (archived.with_suffix("") / "s1.jsonl").is_file()
    [brief] = m.queued(BODY)
    assert "/work/SESSION-START.md" in brief["content"] and "~/thoughts.md" in brief["content"]
    assert "the tests are next" in brief["content"]


def test_the_machine_tab_at_its_budget_while_managing_confirms_then_restarts_on_its_thoughts(h):
    m = Machine(h)
    settingsdoc.write(h.session.paths.settings, agents={"context_budget_tokens": 100000})
    home = h.session.agents.home(MACHINE)
    _usage(home, 105_000)
    (home / "thoughts.md").write_text("tab-2 is porting the parser\n")

    # Not managing, the user is there: theirs to close.
    m.tab(MACHINE)["agent_activity"](busy=True)
    m.tab(MACHINE)["agent_activity"](busy=False)
    assert m.queued(MACHINE) == []
    with pytest.raises(SessionError, match="nothing asked you"):
        m.tab(MACHINE)["ready_to_restart"]()

    m.tab(MACHINE)["manage"](tab=BODY)
    m.tab(MACHINE)["agent_activity"](busy=True)
    m.tab(MACHINE)["agent_activity"](busy=False)
    m.pump()
    assert "`ready_to_restart`" in m.queued(MACHINE)[-1]["content"]

    # A turn that heard the ask and did not say ready is asked again.
    _hear(m, MACHINE)
    assert "`ready_to_restart`" in m.queued(MACHINE)[-1]["content"]
    item = m.channels.take(MACHINE)
    m.tab(MACHINE)["agent_activity"](busy=True, channel_seq=item["seq"])
    m.tab(MACHINE)["ready_to_restart"]()
    m.tab(MACHINE)["agent_activity"](busy=False)
    m.pump()

    assert h.session.intent.tabs[MACHINE].handover is None
    assert not (home / "thoughts.md").exists()
    assert (home / "previous-thoughts.md").read_text() == "tab-2 is porting the parser\n"
    [archived] = [r for r in h.paths.agent_archive.glob("*.json")
                  if json.loads(r.read_text()).get("fresh_restart")]
    assert (archived.with_suffix("") / "thoughts.md").is_file(), "archived with its conversation"
    assert "~/previous-thoughts.md" in m.queued(MACHINE)[-1]["content"]


def test_a_tab_stops_where_the_user_said_and_is_held_on_remote_control_until_they_answer(h):
    m = Machine(h)
    m.tab(MACHINE)["manage"](tab=BODY, stop_when="the parser passes its suite")
    converse(h.session.agents.home(BODY))
    m.tab(BODY)["agent_activity"](busy=True)
    m.tab(BODY)["agent_activity"](busy=False)
    assert "the parser passes its suite" in m.queued(MACHINE)[-1]["content"]

    with pytest.raises(SessionError, match="rai claude-login"):
        m.tab(MACHINE)["hold"](tab=BODY, situation="green; next is the grammar or the CLI")
    claude_login.write(h.session.paths.claude_login, {
        "claudeAiOauth": {"accessToken": "sk-ant-oat01-the-real-login", "refreshToken": "r",
                          "expiresAt": 1, "scopes": [claude_login.SESSIONS_SCOPE]},
        "oauthAccount": {"organizationUuid": "org-1"}})
    held = m.tab(MACHINE)["hold"](tab=BODY, situation="green; next is the grammar or the CLI")
    assert held["resumed"] is True

    spec = h.runtime._containers[naming.agent(BODY)]["spec"]
    assert spec.command[-2:] == ("--remote-control", "myapi: held")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in spec.environment
    home = h.session.agents.home(BODY)
    signed_in = (home / ".claude" / ".credentials.json").read_text()
    assert "the-real-login" not in signed_in and "sk-ant-oat01-rail-" in signed_in
    assert json.loads((home / ".claude.json").read_text())["oauthAccount"] == {
        "organizationUuid": "org-1"}
    assert "the grammar or the CLI" in m.queued(BODY)[-1]["content"]

    # The user's until they answer: the machine tab neither hears its turns nor reaches it.
    m.channels._tabs[MACHINE].queue.clear()
    h.session.agent_session_started(BODY)
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
