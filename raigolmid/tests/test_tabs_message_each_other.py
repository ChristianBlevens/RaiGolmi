"""Tabs message each other for what needs another agent's judgement, and the answer returns
to the tab that asked (`messages.py`)."""
from __future__ import annotations

import pytest

from raigolmid.channel import Channels
from raigolmid.intent import JANITOR
from raigolmid.messages import MessageError
from raigolmid.questions import Questions
from raigolmid.scopes import build_tab_methods
from tests.harness import Harness

MACHINE, BODY = "tab-1", "tab-2"


@pytest.fixture()
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.session.select("body", "myapi")
    assert (harness.tab(None), harness.tab("myapi")) == (MACHINE, BODY)
    return harness


@pytest.fixture()
def c(h):
    channels = Channels(h.session, h.events)
    for tab in (MACHINE, BODY):
        h.session.agent_session_started(tab)
    return channels


def _tab(h, c, tab):
    return build_tab_methods(h.session, Questions(h.events, h.paths), c, tab)


def _heard(h, c, tab):
    """The channel pushes into the idle tab and the turn it starts hears it."""
    item = c.take(tab)
    _tab(h, c, tab)["agent_activity"](busy=True, channel_seq=item["seq"])
    c._catch_up()               # what the daemon's channel thread does: the push is heard
    for event in c.messages._sub.drain(timeout=0):
        c.messages.on_event(event)
    return item


def _ends(h, c, tab) -> bool:
    _tab(h, c, tab)["agent_activity"](busy=False)
    return [e for e in h.events_of("agent.idle") if e.tab == tab][-1].data["done"]


def test_a_body_tab_asks_the_machine_tab_and_the_answer_comes_back(h, c):
    body, machine = _tab(h, c, BODY), _tab(h, c, MACHINE)
    body["agent_activity"](busy=True)
    sent = body["message"](to="machine", content="make the face's bar taller")
    assert sent["to"] == MACHINE
    assert not _ends(h, c, BODY), "a tab with a message out is waiting, not done"
    got = _heard(h, c, MACHINE)
    assert got["meta"]["from"] == BODY and got["meta"]["message"] == sent["message"]
    assert "make the face's bar taller" in got["content"]
    machine["reply"](message=sent["message"], content="done: 32px")
    assert _ends(h, c, MACHINE)
    answer = _heard(h, c, BODY)
    assert "done: 32px" in answer["content"]
    assert _ends(h, c, BODY)


def test_a_turn_that_ends_done_without_replying_did_not_answer(h, c):
    sent = _tab(h, c, BODY)["message"](to=MACHINE, content="which toolbelt?")
    _heard(h, c, MACHINE)
    assert _ends(h, c, MACHINE)
    told = _heard(h, c, BODY)
    assert "was not answered" in told["content"]
    with pytest.raises(MessageError, match="already unanswered"):
        _tab(h, c, MACHINE)["reply"](message=sent["message"], content="late")


def test_a_message_sent_while_the_recipient_works_keeps_its_turn_from_being_done(h, c):
    _tab(h, c, MACHINE)["agent_activity"](busy=True)
    sent = _tab(h, c, BODY)["message"](to="machine", content="a question")
    assert not _ends(h, c, MACHINE), "the message is its mail"
    assert [m["state"] for m in c.messages.items() if m["id"] == sent["message"]] == ["open"]


def test_a_closed_recipient_is_said_and_a_closed_sender_withdraws(h, c):
    first = _tab(h, c, BODY)["message"](to=MACHINE, content="one")
    h.session.close_tab(MACHINE)
    for event in c.messages._sub.drain(timeout=0):
        c.messages.on_event(event)
    assert f"tab {MACHINE} closed" in _heard(h, c, BODY)["content"]
    machine = h.tab(None)
    h.session.agent_session_started(machine)
    second = _tab(h, c, machine)["message"](to="myapi", content="two")
    h.session.close_tab(machine)
    for event in c.messages._sub.drain(timeout=0):
        c.messages.on_event(event)
    with pytest.raises(MessageError, match="nobody is waiting"):
        _tab(h, c, BODY)["reply"](message=second["message"], content="x")
    assert first["message"] != second["message"]


def test_the_janitor_takes_no_messages_and_a_tab_cannot_answer_for_another(h, c):
    body = _tab(h, c, BODY)
    with pytest.raises(MessageError, match="janitor"):
        body["message"](to=JANITOR, content="help")
    with pytest.raises(MessageError, match="this tab"):
        body["message"](to=BODY, content="me")
    sent = body["message"](to="machine", content="q")
    with pytest.raises(MessageError, match="no message"):
        body["reply"](message=sent["message"], content="answering myself")


def test_what_is_on_its_way_into_a_tab_outlives_a_daemon_restart(h, c):
    """The daemon holds what is pending across its own restart. A push heard is not
    said again; one pushed and not heard comes first, marked `redelivered`, because its turn
    may have started while no daemon heard the hook; one queued follows as it was."""
    body = _tab(h, c, BODY)
    body["message"](to="machine", content="heard")
    _heard(h, c, MACHINE)
    _ends(h, c, MACHINE)
    body["message"](to="machine", content="pushed")
    c.take(MACHINE)
    body["message"](to="machine", content="queued")
    c._catch_up()               # what the old daemon's channel thread did before it stopped

    again = Channels(h.session, h.events)
    first = again.take(MACHINE)
    assert "pushed" in first["content"] and first["meta"]["redelivered"] == "true"
    _tab(h, again, MACHINE)["agent_activity"](busy=True, channel_seq=first["seq"])
    _ends(h, again, MACHINE)
    second = again.take(MACHINE)
    assert "queued" in second["content"] and "redelivered" not in second["meta"]
    assert second["seq"] > first["seq"]
