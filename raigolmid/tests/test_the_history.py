"""The history the menu shows (`raigolmid/history.py`): what the agents
and the machine did, each entry lighting the handle until the open menu has shown it, except
the visible result of the user's own action.
"""
from __future__ import annotations

import pytest

from raigolmid.api import with_items
from raigolmid import settings
from raigolmid.paths import Paths
from raigolmid.events import EventLog
from raigolmid.history import History, HistoryError

from tests.harness import Harness, judge_says, settle_judging, standalone


def heard(history: History) -> None:
    for event in history._sub.drain(timeout=0.1):
        history.on_event(event)


def machine(tmp_path) -> Paths:
    paths = Paths(state=tmp_path, data=tmp_path, config=tmp_path / "raigolmid", runtime=tmp_path)
    # What the daemon's start writes before anything reads a setting.
    settings.install(paths.settings)
    return paths


def said(history: History) -> list[tuple[str | None, str | None, bool]]:
    """Each entry's tab, words and whether it is new, oldest first."""
    return [(e["tab"], e["text"], e["new"]) for e in history.entries()]


@pytest.fixture()
def log(tmp_path):
    events = EventLog(tmp_path / "events.jsonl", epoch=1)
    return events, History(events, machine(tmp_path))


@pytest.mark.parametrize("event, data, says", [
    ("janitor.unfixable", {"failure": "container.unfixable", "message": "Direct a session at it."},
     "Direct a session at it."),
    ("channel.unheard", {"seq": 3, "cause": "x", "pushes": 3, "message": "Restart it."},
     "Restart it."),
    ("janitor.open_failed", {"error": "AgentError: no credential"},
     "The janitor tab could not open: AgentError: no credential"),
    ("judge.failed", {"id": "q1", "stage": "learn", "error": "exited 1"},
     "The preferences judge failed on q1; your answer was sent without the preferences "
     "learning it: exited 1"),
])
def test_the_failures_that_are_the_users_are_notices(log, event, data, says):
    events, history = log
    events.emit(event, **data)
    heard(history)
    (entry,) = history.entries()
    assert (entry["notice"], entry["text"], entry["over"], entry["new"]) == (
        True, says, None, True)


def test_a_notice_whose_failure_is_over_settles_itself(log):
    """The janitor that could not open has opened."""
    events, history = log
    events.emit("janitor.open_failed", error="CredentialError: no credential")
    events.emit("channel.unheard", seq=1, cause="x", pushes=3, message="Restart it.")
    events.emit("janitor.opened")
    heard(history)
    assert [(e["kind"], e["over"]) for e in history.entries()] == [
        ("janitor.open_failed", "janitor.opened"), ("channel.unheard", None)]


def test_a_new_entry_lights_the_handle_until_the_open_menu_has_shown_it(tmp_path, log):
    """Whatever enters lights it, and opening the menu puts it out;
    one arriving after the menu drew stays lit."""
    events, history = log
    events.emit("tab.opened", tab="tab-1", body=None, by="daemon")
    events.emit("agent.crashed", tab="tab-1", instance=None, message="It died.")
    heard(history)
    first, second = [e["id"] for e in history.entries()]
    history.seen(first)
    assert [e.type for e in events.tail(1)] == ["history.seen"]
    assert [e["new"] for e in history.entries()] == [False, True]
    history.seen(second)
    history.seen(first)
    assert [e["new"] for e in history.entries()] == [False, False], "seen never goes back"
    events.emit("agent.idle", tab="tab-1", done=True)
    heard(history)
    reloaded = History(EventLog(tmp_path / "events.jsonl", epoch=2),
                       machine(tmp_path))
    assert said(reloaded) == [("tab-1", "opened as the machine tab", False),
                              ("tab-1", "It died.", False),
                              ("tab-1", "done", True)], "outlives a restart"
    with pytest.raises(HistoryError, match="no history entry h99"):
        history.seen("h99")


def test_the_users_own_action_is_born_seen_and_what_follows_from_it_is_not(tmp_path, monkeypatch):
    """Rulings: a mark says only what the user may not know. The tab their selection opened,
    their close and the fresh tab it opened are on their screen; the machine tab the daemon opened,
    and a tab another tab's selection opened, are not."""
    h = Harness(tmp_path, monkeypatch)
    history = History(h.events, h.paths)
    h.session.ensure_tabs()
    h.session.select("body", "myapi")
    h.session.select("body", "webui", by_tab="tab-2")
    h.session.select("body", "myapi")
    h.session.close_tab("tab-2")
    heard(history)
    assert said(history) == [
        ("tab-1", "opened as the machine tab", True),
        ("tab-2", "opened for myapi", False),
        ("tab-3", "opened for webui", True),
        ("tab-2", "closed by you; its conversation archived", False),
        ("tab-4", "opened for myapi", False)]


def test_a_tab_done_while_he_views_it_is_born_seen(log):
    events, history = log
    events.emit("terminal.viewing", tab="tab-1")
    events.emit("agent.idle", tab="tab-1", done=True)
    events.emit("agent.idle", tab="tab-2", done=True)
    events.emit("agent.idle", tab="tab-2", done=False)
    heard(history)
    assert said(history) == [("tab-1", "done", False), ("tab-2", "done", True)]


def test_a_question_is_the_users_to_see_once_referred_and_is_read_where_it_is_kept(
        tmp_path, monkeypatch):
    events, questions, judge, runtime = standalone(tmp_path, monkeypatch)
    history = History(events, machine(tmp_path))
    judge.doc.write_text("- Fonts: serif.\n")
    judge_says(runtime, "UNCLEAR", "ANSWER: serif\nQUOTE: - Fonts: serif.", "UNCLEAR",
               "- Colours: blue.")
    referred = questions.ask("tab-1", "Colour?")
    answered = questions.ask("tab-2", "Font?")
    settle_judging(judge, questions)
    typed = questions.ask("tab-3", "Size?")
    heard(history)
    assert all(e["question"] != typed for e in history.entries()), "the judge has it"
    questions.answered_in_terminal("tab-3", "large")
    permission, _ = questions.ask_permission("tab-4", "Swap?", {"do": "x"}, project="p")
    settle_judging(judge, questions)
    heard(history)

    *shown, learned = with_items(history.entries(), questions, set())
    assert [(e["question"], e["new"]) for e in shown] == [
        (referred, True), (answered, True), (typed, False), (permission, True)]
    assert [e["item"]["state"] for e in shown] == ["pending", "answered", "withdrawn",
                                                    "pending"]
    assert learned["text"] == f"your answer to {typed} learned into your preferences"
    questions.answer(permission, "yes")
    assert with_items(history.entries(), questions, set())[-2]["item"]["by"] == "terminal", \
        "read as it stands now, not as it was when the entry was written"


def test_dropped_events_are_read_back_from_the_log_once(log):
    events, history = log
    events.emit("agent.idle", tab="tab-1", done=True)
    heard(history)
    events.emit("agent.idle", tab="tab-2", done=True)
    events.emit("agent.idle", tab="tab-3", done=True)
    history._sub.drain(timeout=0.1)                  # lost from the queue
    history._replay(before=None)
    history._replay(before=None)
    assert [e["tab"] for e in history.entries()] == ["tab-1", "tab-2", "tab-3"]


def test_every_event_it_records_is_one_the_daemon_emits():
    """A kind named here and emitted nowhere is an entry that never comes."""
    import re
    from pathlib import Path

    from raigolmid import history as history_module

    source = "\n".join(p.read_text() for p in
                       (Path(history_module.__file__).parent).glob("*.py")
                       if p.name != "history.py")
    emitted = set(re.findall(r'emit\(\s*"([a-z_.]+)"', source))
    from raigolmid.questions import SETTLED_AS

    named = (set(history_module.SAYS) | set(history_module.NOTICED)
             | set(history_module.OVER)
             | {"question.asked", "question.referred", "terminal.viewing"})
    assert named - emitted == set()
    assert history_module.SETTLES == set(SETTLED_AS), "settled through `Questions._settle`"
