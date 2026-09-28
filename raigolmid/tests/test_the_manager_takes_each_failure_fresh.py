"""A failure after the first is handed to a fresh manager session — its memory is its
documents — unless a question it asked is still unanswered."""
from __future__ import annotations

import pytest

from raigolmid.channel import FAILURE, Channels
from raigolmid.intent import MANAGER
from raigolmid.manager import Manager

from tests.harness import Harness


@pytest.fixture()
def h(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


@pytest.fixture()
def c(h):
    channels = Channels(h.session, h.events)
    h.session.open_manager()
    h.session.agent_session_started(MANAGER)
    return channels


def _failure(h, name: str) -> None:
    h.events.emit(FAILURE, tab=MANAGER, failure=name,
                  deliver={"content": f"failure {name}", "meta": {"failure": name}})


def _hear(h, c) -> dict:
    item = c.take(MANAGER)
    h.session.agent_activity(MANAGER, True, item["seq"])
    h.session.agent_activity(MANAGER, False)
    return item


def _fresh_asked(h) -> list[dict]:
    return [e.data for e in h.events.read() if e.type == "manager.fresh_conversation"]


def test_a_second_failure_waits_for_a_fresh_session(h, c):
    _failure(h, "one")
    assert _hear(h, c)["content"] == "failure one"
    _failure(h, "two")

    assert c.take(MANAGER) is None
    assert c.take(MANAGER) is None
    assert _fresh_asked(h) == [{"failure": "two"}], "asked for once, not per poll"

    h.session.agent_session_started(MANAGER)
    assert c.take(MANAGER)["content"] == "failure two"


def test_an_unanswered_question_keeps_the_conversation(h, c):
    _failure(h, "one")
    _hear(h, c)
    h.events.emit("question.asked", tab=MANAGER, id="q7", kind="question")
    _failure(h, "two")

    assert c.take(MANAGER)["content"] == "failure two"
    assert _fresh_asked(h) == []


def test_once_its_answer_is_on_the_channel_the_next_failure_is_fresh(h, c):
    _failure(h, "one")
    _hear(h, c)
    h.events.emit("question.asked", tab=MANAGER, id="q7", kind="question")
    h.events.emit("question.answered", tab=MANAGER, id="q7",
                  deliver={"content": "yes", "meta": {"question": "q7"}})
    _failure(h, "two")

    assert _hear(h, c)["content"] == "yes", "the answer reaches the session that asked"
    assert c.take(MANAGER) is None
    assert _fresh_asked(h) == [{"failure": "two"}]


def test_a_fresh_session_is_the_manager_restarted_without_its_conversation(h, c, monkeypatch):
    manager = Manager(h.session, h.events)
    restarts = []
    monkeypatch.setattr(h.session, "restart_agent",
                        lambda tab_id, resume=True: restarts.append((tab_id, resume)))
    manager.on_event(h.events.emit("manager.fresh_conversation", tab=MANAGER, failure="two"))
    assert restarts == [(MANAGER, False)]
