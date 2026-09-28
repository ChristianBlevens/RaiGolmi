"""What agents ask the user, daemon side.

Over a real socket, driven by the real client and the MCP tool's own function: a question does
not block its asker, is answered by a prompt typed in its tab, and an outcome that is not the
user's typing reaches the asker as a message on its own channel — the same `channel_take` its MCP
server polls.
Every question passes the real preferences judge first; with no preferences it is put to the
user without a run, and a run that learns from their answer is the fake runtime's scripted one.
"""
from __future__ import annotations

import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from raigolmid import settings
from raigolmid.paths import Paths
from raigolmid.api import ApiServer, build_methods, with_marks
from raigolmid.channel import Channels
from raigolmid.credproxy import Broker
from raigolmid.client import ApiClient, ApiError
from raigolmid.events import EventLog
from raigolmid.judge import Judge
from raigolmid.mcp_server import ask_user
from raigolmid.intent import BrokenState
from raigolmid.questions import NO_ANSWER, Questions
from raigolmid.history import History
from raigolmid.viewing import Viewing
from raigolmid.scopes import SOCKET, build_tab_methods

from tests.harness import Harness, answering, judge_says, settle_judging, standalone


@dataclass
class Api:
    host: ApiClient           # the host's socket, which the menu and the terminal call
    tab: ApiClient            # tab-1's own socket (the machine tab's), which its agent asks on
    other: ApiClient          # tab-2's, myapi's tab
    questions: Questions
    judge: Judge
    h: Harness
    viewing: Viewing

    def ask(self, client: ApiClient, message: str, choices=None) -> str:
        """Asked, and put to the user: there are no preferences yet."""
        question = ask_user(client, message, choices)["question"]
        settle_judging(self.judge, self.questions)
        return question


@pytest.fixture()
def api(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    h.session.select("body", "myapi")
    questions, channels = Questions(h.events, h.paths), Channels(h.session, h.events)
    judge = Judge(h.events, h.runtime, h.paths.preferences, h.session.agents.broker, 1)
    viewing = Viewing(h.events, h.paths.viewing)
    servers = [ApiServer(h.paths.api_socket,
                         build_methods(h.session, h.events, questions, channels, viewing,
                                       History(h.events, h.paths)),
                         h.events, ready=answering())]
    # Real sockets under a short directory: a Unix socket path is limited to 108 bytes.
    short = Path(tempfile.mkdtemp(prefix="rai-", dir="/tmp"))
    assert (h.tab(None), h.tab("myapi")) == ("tab-1", "tab-2")
    for tab in ("tab-1", "tab-2"):
        servers.append(ApiServer(short / tab / SOCKET,
                                 build_tab_methods(h.session, questions, channels, tab),
                                 h.events, ready=answering(), subscribe=False))
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    clients = [ApiClient(s.socket_path, timeout=30) for s in servers]
    for client in clients[1:]:
        client.call("agent_session_started")
    # What the daemon's questions thread would long since have heard: the setup's own events.
    settle_judging(judge, questions)
    try:
        yield Api(*clients, questions, judge, h, viewing)
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        shutil.rmtree(short)


def the_one_pending(client: ApiClient) -> dict:
    (item,) = client.call("questions")
    return item


def agent_of(api: Api, tab: str) -> dict:
    return next(a for a in api.host.call("status")["agents"] if a["tab"] == tab)


def test_asking_returns_at_once_and_the_tab_waits(api):
    out = ask_user(api.tab, "Keep the old face?", ["keep", "replace"])
    assert out["status"] == "asked" and "End your turn" in out["next"]
    assert api.host.call("questions") == [], "the judge has it first"
    assert agent_of(api, "tab-1")["state"] == "waiting"
    settle_judging(api.judge, api.questions)
    item = the_one_pending(api.host)
    assert (item["id"], item["kind"], item["tab"], item["choices"]) == (
        out["question"], "question", "tab-1", ["keep", "replace"])
    assert agent_of(api, "tab-1")["state"] == "asking", "put to the user"
    assert agent_of(api, "tab-2")["state"] == "idle"


def test_a_prompt_typed_in_the_tab_answers_and_withdraws_it(api):
    """The user answers in the terminal, where the question is."""
    question = api.ask(api.tab, "Which body?")
    prompts = judge_says(api.h.runtime, "- Bodies: requests.")
    api.tab.call("agent_activity", busy=True, prompt="use requests")
    assert api.host.call("questions") == []
    (event,) = api.h.events_of("question.withdrawn")
    assert (event.data["id"], event.data["reason"]) == (question, "answered_in_terminal")
    assert api.tab.call("channel_take") is None, "nothing is sent: the user's prompt was the answer"
    settle_judging(api.judge, api.questions)
    assert "use requests" in prompts[0], "what the user typed is learned"
    assert api.h.paths.preferences.read_text() == "- Bodies: requests.\n"


def test_a_stop_that_stays_busy_is_not_an_answer(api):
    """A turn ending with a command the agent waits on reports busy with no prompt."""
    api.ask(api.tab, "Which body?")
    api.tab.call("agent_activity", busy=True)
    assert len(api.host.call("questions")) == 1


def test_a_question_left_thirty_minutes_lapses_to_no_answer(api):
    question = api.ask(api.other, "Which body?")
    api.questions.lapse(now=api.questions._items[question].asked_at
                        + settings.load(api.h.paths.settings).lapse_seconds)
    assert api.host.call("questions") == []
    (event,) = api.h.events_of("question.lapsed")
    assert event.data["answer"] == NO_ANSWER
    assert "got no answer" in api.other.call("channel_take")["content"]


def test_a_tab_cannot_ask_as_another(api):
    """The tab is the socket's, never a parameter: one naming another tab is refused."""
    with pytest.raises(ApiError, match="bad_params|unexpected keyword"):
        api.tab.call("ask", tab="tab-2", message="Now?", choices=[])
    api.other.call("ask", message="Later?", choices=[])
    settle_judging(api.judge, api.questions)
    assert [i["tab"] for i in api.host.call("questions")] == ["tab-2"]


def test_a_question_is_not_answered_from_the_host(api):
    """Only by typing in its tab: what the user types there is the tab's next message."""
    question = api.ask(api.tab, "Now?")
    with pytest.raises(ApiError, match="typing in tab-1"):
        api.host.call("answer", id=question, text="yes")
    with pytest.raises(ApiError, match="unknown method"):
        api.host.call("dismiss", id=question)


# --- no tab closes itself ---------------------------------------------------------
def test_a_tab_that_asked_is_waiting_until_the_answer_reaches_it(api):
    api.other.call("agent_activity", busy=True)
    question = api.other.call("ask", message="Merge it?", choices=[])
    api.other.call("agent_activity", busy=False)
    assert agent_of(api, "tab-2")["state"] == "waiting", "with the judge is not done"
    settle_judging(api.judge, api.questions)

    assert agent_of(api, "tab-2")["state"] == "asking"
    judge_says(api.h.runtime, "- Merges: yes.")
    api.other.call("agent_activity", busy=True, prompt="yes, merge it")
    assert api.h.events_of("question.withdrawn")[-1].data["id"] == question
    api.other.call("agent_activity", busy=False)
    assert agent_of(api, "tab-2")["state"] == "idle"
    assert "tab-2" in api.h.session.intent.tabs


def test_a_question_outlives_a_resuming_restart_and_goes_with_its_conversation(
        tmp_path, monkeypatch):
    """A restart or crash-reopen that resumes the conversation keeps its question,
    whose answer belongs to it; a restart that does not resume it withdraws it. A permission
    goes when its agent stops, because the process that waited on it is gone."""
    events, questions, judge, _ = standalone(tmp_path, monkeypatch)
    question = questions.ask("tab-1", "Now?")
    permission, _ = questions.ask_permission("tab-1", "Swap?", {"do": "x"}, project=None)
    settle_judging(judge, questions)

    def happen(*emitted) -> list[str]:
        for type_, data in emitted:
            events.emit(type_, tab="tab-1", **data)
        for event in questions._sub.drain(timeout=0.1):
            questions.on_event(event)
        return [i["id"] for i in questions.pending()]

    assert happen(("agent.crashed", {}), ("agent.stopped", {}),
                  ("agent.restarted", {"resumed": True})) == [question]
    assert happen(("agent.stopped", {}), ("agent.restarted", {"resumed": False})) == []


# --- what is kept -----------------------------------------------------------------------
def test_what_is_asked_and_an_overturn_held_outlive_the_daemon(tmp_path, monkeypatch):
    events, first, judge, runtime = standalone(tmp_path, monkeypatch)
    store = tmp_path / "questions.json"
    done = first.ask("tab-1", "Colour?")
    settle_judging(judge, first)
    judge_says(runtime, "- Colours: blue.\n- Fonts: serif.")
    first.answered_in_terminal("tab-1", "blue")
    settle_judging(judge, first)
    judge_says(runtime, "ANSWER: serif\nQUOTE: - Fonts: serif.")
    held = first.ask("tab-1", "Font?")
    settle_judging(judge, first)
    first.overturn(held, "sans")          # the daemon stops before the judge learns it
    judge_says(runtime, "UNCLEAR")
    waiting = first.ask("tab-1", "Size?")
    settle_judging(judge, first)

    restarted = Questions(events, Paths(state=tmp_path, data=tmp_path, config=tmp_path / "raigolmid", runtime=tmp_path))
    judge = Judge(events, runtime, tmp_path / "preferences.md",
                  Broker(tmp_path / "agent-credentials", tmp_path / "proxy-secret",
                         tmp_path / "proxy-ca", runtime, tmp_path / "registry-token"), 2)
    assert list(restarted.items()) == [done, held, waiting]
    assert restarted.tab_state("tab-1") == "asking"
    assert restarted.ask("tab-2", "Next?") not in (done, held, waiting)
    judge_says(runtime, "UNCLEAR", "- Fonts: sans.")
    restarted.offer_to_judge()
    settle_judging(judge, restarted)
    sent = [e.data["deliver"]["content"] for e in events.tail(50)
            if e.type == "question.delivered" and e.data.get("deliver")]
    assert sent[-1].endswith("sans")


def test_what_a_gone_tab_asked_is_withdrawn_at_start(tmp_path, monkeypatch):
    events, first, judge, _ = standalone(tmp_path, monkeypatch)
    store = tmp_path / "questions.json"
    gone, kept = first.ask("tab-2", "Still there?"), first.ask("tab-1", "Here?")
    settle_judging(judge, first)

    restarted = Questions(events, Paths(state=tmp_path, data=tmp_path, config=tmp_path / "raigolmid", runtime=tmp_path))
    restarted.forget_absent_tabs({"tab-1"})
    assert [i["id"] for i in restarted.pending()] == [kept]
    assert restarted._items[gone].outcome == "tab_gone"
    assert restarted.tab_state("tab-2") is None


def test_a_store_that_does_not_parse_is_moved_aside_and_refused(tmp_path):
    events = EventLog(tmp_path / "events.jsonl", epoch=1)
    store = tmp_path / "questions.json"
    store.write_text('{"items": [{"id": "q1"}], "always": []}')
    with pytest.raises(BrokenState, match="not readable questions and permissions"):
        Questions(events, Paths(state=tmp_path, data=tmp_path, config=tmp_path / "raigolmid", runtime=tmp_path))
    assert not store.exists() and store.with_suffix(".json.broken").exists()


# --- the terminal marks a tab that needs the user ----------------------------------
def _heard(viewing: Viewing) -> None:
    for event in viewing._sub.drain(timeout=0.1):
        viewing.on_event(event)


def _marks(api: Api) -> tuple[dict, bool]:
    status = api.host.call("status")
    return {a["tab"]: a["marked"] for a in status["agents"]}, status["terminal"]["lit"]


def test_an_idle_tab_is_marked_until_he_views_it(api):
    api.host.call("terminal_viewing", shown=False, window="tab-1 machine")
    _heard(api.viewing)
    api.other.call("agent_activity", busy=True)
    api.other.call("agent_activity", busy=False)
    _heard(api.viewing)
    assert _marks(api) == ({"tab-1": False, "tab-2": True}, True)

    api.host.call("terminal_viewing", window="tab-2 myapi")
    assert _marks(api)[1], "a window changed behind a hidden terminal is not viewed"
    assert api.host.call("terminal_viewing", shown=True) == {"viewing": "tab-2"}
    _heard(api.viewing)
    assert _marks(api) == ({"tab-1": False, "tab-2": False}, False)
    assert agent_of(api, "tab-2")["state"] == "idle", "seen, and still idle"


def test_a_tab_that_goes_idle_while_he_watches_it_is_not_marked(api):
    api.host.call("terminal_viewing", shown=True, window="tab-2 myapi")
    _heard(api.viewing)
    api.other.call("agent_activity", busy=True)
    api.other.call("agent_activity", busy=False)
    _heard(api.viewing)
    assert _marks(api) == ({"tab-1": False, "tab-2": False}, False)


def test_a_question_or_permission_is_marked_until_settled_even_in_view(api):
    api.host.call("terminal_viewing", shown=True, window="tab-1 machine")
    _heard(api.viewing)
    question = api.ask(api.tab, "Which colour?")
    assert agent_of(api, "tab-1")["state"] == "asking"
    assert _marks(api) == ({"tab-1": True, "tab-2": False}, True)
    api.tab.call("agent_activity", busy=True, prompt="blue")
    assert _marks(api) == ({"tab-1": False, "tab-2": False}, False)

    permission, _ = api.questions.ask_permission("tab-2", "Swap?", {"do": "x"}, project="myapi")
    assert agent_of(api, "tab-2")["state"] == "permission"
    assert _marks(api)[0]["tab-2"]


def test_the_manager_lights_the_collapsed_tab_only_while_it_asks(tmp_path):
    """The user answers the manager in its terminal tab, as any tab; its idle ends each job."""
    events = EventLog(tmp_path / "events.jsonl", epoch=1)
    viewing = Viewing(events, tmp_path / "viewing.json")
    events.emit("agent.idle", tab="manager", done=True)
    _heard(viewing)
    for state, lit in (("asking", True), ("idle", False)):
        status = {"agents": [{"tab": "manager", "state": state, "managed": False},
                             {"tab": "tab-1", "state": "working", "managed": False}]}
        out = with_marks(status, viewing)
        assert [a["marked"] for a in out["agents"]] == [True, False]
        assert out["terminal"] == {"viewing": None, "lit": lit}


def test_what_he_has_seen_outlives_the_daemon(tmp_path):
    events = EventLog(tmp_path / "events.jsonl", epoch=1)
    first = Viewing(events, tmp_path / "viewing.json")
    first.report(shown=True, window="tab-1 machine")
    events.emit("agent.idle", tab="tab-2", done=True)
    _heard(first)
    after = EventLog(tmp_path / "events.jsonl", epoch=2)
    restarted = Viewing(after, tmp_path / "viewing.json")
    assert (restarted.viewed(), restarted.unseen("tab-2")) == ("tab-1", True)
    restarted.report(window="raigolmi")
    assert restarted.viewed() == "raigolmi", "a window that is no tab's"
    after.emit("tab.closed", tab="tab-2")
    _heard(restarted)
    assert not restarted.unseen("tab-2")

