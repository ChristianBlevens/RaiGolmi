"""What the daemon sends into a tab's session (`channel.py`): one
message at a time into an idle tab, heard only on the turn it starts, repeated until then, and
said when it cannot be heard."""
from __future__ import annotations

import pytest

from raigolmid import channel as channel_module, naming
from raigolmid.channel import Channels, pushed_seq

from tests.harness import Harness

# The harness opens the machine tab first, so selecting myapi opens tab-2.
TAB = "tab-2"


@pytest.fixture()
def h(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


@pytest.fixture()
def c(h):
    channels = Channels(h.session, h.events)
    h.session.select("body", "myapi")
    assert h.tab("myapi") == TAB
    return channels


def _send(h, text: str) -> None:
    h.events.emit("question.answered", tab=TAB, deliver={"content": text, "meta": {"question": "q1"}})


def _up(h) -> None:
    h.session.agent_session_started(TAB)


def test_every_tab_starts_with_its_channel(h, c):
    spec = h.runtime._containers[naming.agent(TAB)]["spec"]
    assert "--channels" in spec.command
    assert spec.command[spec.command.index("--channels") + 1] == "plugin:raigolmi@raigolmi"
    assert spec.environment["RAIGOLMI_SCOPE"] == "tab"


def test_a_message_goes_only_to_an_idle_tab_whose_session_is_up(h, c):
    _send(h, "keep it")
    assert c.take(TAB) is None, "its session has not come up"
    _up(h)
    h.session.agent_activity(TAB, True)
    assert c.take(TAB) is None, "a push into a busy turn is refused as untrusted"
    h.session.agent_activity(TAB, False)
    item = c.take(TAB)
    assert item["content"] == "keep it"
    assert item["meta"] == {"question": "q1", "seq": str(item["seq"])}


def test_only_the_turn_the_push_starts_hears_it(h, c, monkeypatch):
    """A prompt the user typed carries no seq: it is not the push, which is still owed."""
    _send(h, "keep it")
    _up(h)
    first = c.take(TAB)
    h.session.agent_activity(TAB, True, None)
    assert not c.state(TAB)["pushed"]["heard"]
    h.session.agent_activity(TAB, False)
    monkeypatch.setattr(channel_module, "REPUSH_SECONDS", -1.0)
    again = c.take(TAB)
    assert again["seq"] == first["seq"]
    h.session.agent_activity(TAB, True, first["seq"])
    assert c.state(TAB)["pushed"]["heard"]
    assert c.take(TAB) is None
    assert not h.events_of("channel.unheard")


def test_a_push_no_turn_starts_on_is_pushed_again_then_said_as_unheard(h, c, monkeypatch):
    """Claude Code registers a channel after connecting and tells the server nothing, so a
    push can land before it and be dropped (9 ms early, on the VM)."""
    _send(h, "first")
    _send(h, "second")
    _up(h)
    first = c.take(TAB)
    assert c.take(TAB) is None, "too soon to call it lost"
    monkeypatch.setattr(channel_module, "REPUSH_SECONDS", -1.0)
    assert c.take(TAB)["seq"] == first["seq"]
    monkeypatch.setattr(channel_module, "PUSHES_BEFORE_DEAF", 2)
    assert c.take(TAB) is None
    (unheard,) = h.events_of("channel.unheard")
    assert unheard.tab == TAB and f"rai ai restart {TAB}" in unheard.data["message"]
    assert c.take(TAB) is None, "nothing more goes into a channel that is not listening"
    _up(h)
    assert c.take(TAB)["content"] == "first", "a new session gets what the old did not hear"


def test_a_message_on_its_way_is_mail_until_heard(h, c):
    assert not c.has_mail(TAB)
    _send(h, "keep it")
    assert c.has_mail(TAB), "read synchronously: an event already emitted is counted"
    _up(h)
    item = c.take(TAB)
    assert c.has_mail(TAB)
    h.session.agent_activity(TAB, True, item["seq"])
    assert not c.has_mail(TAB)


def test_a_tab_running_before_the_daemon_started_is_already_listening(h, c):
    _send(h, "keep it")
    _up(h)
    restarted = Channels(h.session, h.events)
    h.events.emit("question.answered", tab=TAB, deliver={"content": "after", "meta": {}})
    assert restarted.take(TAB)["content"] == "after"


@pytest.mark.parametrize("prompt, seq", [
    ('<channel source="plugin:raigolmi:raigolmi" question="q1" seq="7">\nYes', 7),
    ('<channel source="plugin:raigolmi:raigolmi" seq="12" failure="x">\n…', 12),
    ("yes, replace it", None),
    ('please read <channel source="plugin:raigolmi:raigolmi" seq="3">', None),
    ('<channel source="plugin:other:raigolmi" seq="3">', None),
])
def test_a_prompt_says_which_push_it_is(prompt, seq):
    assert pushed_seq(prompt) == seq


def test_a_channel_that_stops_asking_is_said_once_as_silent(h, c, monkeypatch):
    """A tab's MCP server that died asks for nothing again; `take` is the only place a
    channel shows it is alive, so the daemon notices the silence. Said once, while a
    message waits for an idle tab, and not again until the channel asks."""
    _up(h)
    c.take(TAB)
    monkeypatch.setattr(channel_module, "SILENT_SECONDS", -1.0)
    c._catch_up(); c._say_silent()
    assert not h.events_of("channel.silent"), "nothing waits, so a quiet channel is fine"
    _send(h, "keep it")
    h.session.agent_activity(TAB, True)
    c._catch_up(); c._say_silent()
    assert not h.events_of("channel.silent"), "a busy tab's poller rightly takes nothing"
    h.session.agent_activity(TAB, False)
    c._catch_up(); c._say_silent(); c._say_silent()
    (silent,) = h.events_of("channel.silent")
    assert silent.tab == TAB and f"rai ai restart {TAB}" in silent.data["message"]
    c.take(TAB)
    monkeypatch.setattr(channel_module, "SILENT_SECONDS", 3600.0)
    c._say_silent()
    assert len(h.events_of("channel.silent")) == 1


def test_a_push_a_restarted_agent_did_not_hear_waits_for_its_new_session(h, c, monkeypatch):
    """Pushed into a session that is coming up, a push is not heard and says nothing about the
    tab: it waits, however often it would be pushed again, and goes first once the session is."""
    _send(h, "first")
    _up(h)
    c.take(TAB)
    h.events.emit("agent.started", tab=TAB)
    monkeypatch.setattr(channel_module, "REPUSH_SECONDS", -1.0)
    for _ in range(channel_module.PUSHES_BEFORE_DEAF + 2):
        assert c.take(TAB) is None
    assert not h.events_of("channel.unheard")
    _up(h)
    assert c.take(TAB)["content"] == "first"


def test_a_daemon_starting_while_a_session_comes_up_does_not_count_it_up(h, c):
    """Whether a running container's session is up is its own to say: the SessionStart hook
    records its container, and a daemon starting reads that (`activity.py`)."""
    from raigolmid import activity
    home = h.session.agents.home(TAB)
    container = h.runtime.inspect(naming.agent(TAB))
    activity.record(home, busy=False, session="an-earlier-container")
    h.session._read_agent_activity()
    fresh = Channels(h.session, h.events)
    _send(h, "keep it")
    assert fresh.take(TAB) is None, "its session has not said it is up"

    activity.record(home, busy=False, session=container.id[:12])
    h.session._read_agent_activity()
    assert Channels(h.session, h.events).take(TAB)["content"] == "keep it"


def test_a_working_tab_reads_a_direction_at_its_next_tool_call_and_nothing_else(h, c):
    _up(h)
    h.session.agent_activity(TAB, busy=True)
    h.events.emit("coordinator.directed", tab=TAB, deliver={
        "content": "From the machine tab: park the parser", "meta": {"from": "machine"}})
    h.events.emit("janitor.told", tab=TAB, deliver={
        "content": "target/ holds 30 GB", "meta": {"from": "janitor"}})
    _send(h, "the answer to q1")
    h.events.emit("coordinator.directed", tab=TAB, deliver={
        "content": "after the answer", "meta": {"from": "machine"}})
    assert c.take(TAB) is None, "nothing is pushed into a busy session"
    assert c.take_midturn(TAB) == ["From the machine tab: park the parser", "target/ holds 30 GB"]
    assert c.take_midturn(TAB) == [], "an answer waits for a turn of its own, and what is behind it"
    h.session.agent_activity(TAB, busy=False)
    assert c.take(TAB)["content"] == "the answer to q1"
