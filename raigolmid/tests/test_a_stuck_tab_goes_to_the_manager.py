"""A tab working and getting nothing done is put in front of the manager, whatever the cause
(`stalls.py`): stalled when its conversation stands still, spinning when it moves and its
working copy does not; said once per episode, with the evidence; and `unstick` ends the turn and
starts the next on the manager's note."""
from __future__ import annotations

import json

import pytest

from raigolmid import naming
from raigolmid.channel import Channels
from raigolmid.intent import MANAGER
from raigolmid.manager import TAKEN
from raigolmid.stalls import SPIN_SECONDS, STALL_SECONDS, Stalls, unstick

from tests.harness import Harness, converse

TAB = "tab-2"


@pytest.fixture()
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.session.select("body", "myapi")
    assert harness.tab("myapi") == TAB
    return harness


@pytest.fixture()
def s(h):
    return Stalls(h.session, h.events)


def _transcript(h, tab: str = TAB):
    home = h.session.agents.home(tab)
    converse(home)
    return home / ".claude" / "projects" / "-work" / "s1.jsonl"


def _write(transcript, *rows) -> None:
    with transcript.open("a") as out:
        for row in rows:
            out.write(json.dumps(row) + "\n")


def _call(transcript, call_id: str, command: str) -> None:
    _write(transcript, {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": command}}]}})


def test_a_busy_tab_whose_conversation_stands_still_is_said_once_with_its_evidence(h, s):
    transcript = _transcript(h)
    _call(transcript, "t1", "ls")
    _write(transcript, {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "src"}]}})
    _call(transcript, "t2", "until grep -q done log; do sleep 5; done")
    home = h.session.agents.home(TAB)
    (home / ".raigolmi").mkdir()
    (home / ".raigolmi" / "stop-memory.json").write_text(
        json.dumps({"asked": ["b1"], "waiting": ["b1"]}))
    h.runtime.staged_processes[naming.agent(TAB)] = ["7\t1\t50:00\tbash -c until grep"]
    h.session.agent_activity(TAB, True)

    s.tick(0.0)
    s.tick(STALL_SECONDS - 1)
    assert not h.events_of("tab.stalled")
    s.tick(STALL_SECONDS)
    s.tick(STALL_SECONDS + 60)
    (stalled,) = h.events_of("tab.stalled")
    assert stalled.tab == TAB and "tab.stalled" in TAKEN
    assert stalled.data["waiting_on"] == ["b1"]
    assert stalled.data["sandbox"] == naming.instance_id("myapi", TAB)
    assert [c["result"] for c in stalled.data["last_calls"]] == ["src", "pending"]
    assert "until grep" in stalled.data["last_calls"][-1]["input"]
    processes = stalled.data["processes"]
    assert "bash -c until grep" in processes[naming.agent(TAB)]
    view = naming.view(naming.instance_id("myapi", TAB))
    assert processes[view].startswith("(not listed:"), "no sandbox is open, and that is said"

    # Its conversation moving again starts a new episode, which is said again.
    _write(transcript, {"type": "user", "message": {"content": "more"}})
    s.tick(STALL_SECONDS + 90)
    s.tick(2 * STALL_SECONDS + 90)
    assert len(h.events_of("tab.stalled")) == 2


def test_an_idle_tab_and_a_limited_account_are_never_said(h, s):
    _transcript(h)
    s.tick(0.0)
    s.tick(10 * STALL_SECONDS)
    assert not h.events_of("tab.stalled"), "an idle tab is waiting on nobody's work"

    h.session.agent_activity(TAB, True)
    h.events.emit("account.limited", resets_at=None, hold_until=None)
    s.on_event(h.events_of("account.limited")[-1])
    s.tick(0.0)
    s.tick(10 * STALL_SECONDS)
    assert not h.events_of("tab.stalled"), "no turn moves while the account is limited"


def test_a_tab_whose_conversation_moves_and_whose_work_does_not_is_spinning(h, s):
    transcript = _transcript(h)
    h.session.agent_activity(TAB, True)
    repo = h.session.working_copy(TAB)
    now = 0.0
    s.tick(now)
    while now < SPIN_SECONDS - 60:
        now += 60
        _call(transcript, f"c{now}", "cargo test")
        s.tick(now)
    (repo / "notes.txt").write_text("a change")
    while now < 2 * SPIN_SECONDS - 120:
        now += 60
        _call(transcript, f"c{now}", "cargo test")
        s.tick(now)
    assert not h.events_of("tab.spinning"), "a file written into the work is progress"
    while now < 2 * SPIN_SECONDS + 60:
        now += 60
        _call(transcript, f"c{now}", "cargo test")
        s.tick(now)
    (spinning,) = h.events_of("tab.spinning")
    assert spinning.tab == TAB and "tab.spinning" in TAKEN
    assert not h.events_of("tab.stalled")


def test_unstick_ends_the_turn_and_starts_the_next_on_the_managers_note(h, s):
    channels = Channels(h.session, h.events)
    _transcript(h)
    h.session.agent_session_started(TAB)
    h.session.agent_activity(TAB, True)
    unstick(h.session, h.events, TAB, "Your test run was killed at 19:55; rerun it detached, with a deadline.")
    assert h.events_of("agent.restarted")[-1].tab == TAB
    assert h.events_of("agent.restarted")[-1].data["resumed"]
    h.session.agent_session_started(TAB)
    pushed = channels.take(TAB)
    assert pushed is not None and "rerun it detached" in pushed["content"]
    with pytest.raises(Exception):
        unstick(h.session, h.events, TAB, " ")


def test_the_managers_own_stall_is_unstuck_without_anyone(h, s):
    h.session.open_manager()
    _transcript(h, MANAGER)
    h.session.agent_activity(MANAGER, True)
    s.tick(0.0)
    s.tick(STALL_SECONDS)
    assert h.events_of("tab.stalled")[-1].tab == MANAGER
    assert h.events_of("agent.restarted")[-1].tab == MANAGER
    (unstuck,) = h.events_of("tab.unstuck")
    assert "incident doc" in unstuck.data["deliver"]["content"]
