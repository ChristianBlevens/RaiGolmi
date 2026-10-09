"""`rai claude-update`: the agent image takes a newer Claude Code than the release pins, every
tab follows it, and a build that fails leaves the machine on the one it had. The machine tab
can ask for it too, and no other tab can."""
from __future__ import annotations

import time

import pytest

from raigolmid import claudecode, hostimages, naming
from raigolmid.channel import Channels
from raigolmid.hostimages import HostImageError
from raigolmid.questions import Questions
from raigolmid.scopes import build_tab_methods
from raigolmid.session import SessionError

from tests.harness import Harness

PIN = "2.1.283"


@pytest.fixture()
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    hostimages.agent(None).containerfile.write_text(
        f"FROM scratch\nARG CLAUDE_CODE_VERSION={PIN}\n")
    harness.session.select("body", "myapi")
    monkeypatch.setattr(claudecode, "_fetch", lambda url: b'{"version": "2.1.295"}')
    return harness


def _image_of(h, tab_id: str) -> str:
    return h.runtime._containers[naming.agent(tab_id)]["spec"].image


def _settles(check) -> None:
    deadline = time.monotonic() + 5
    while not check():
        assert time.monotonic() < deadline, "never settled"
        time.sleep(0.05)


def test_a_newer_choice_is_another_image_and_one_no_newer_is_the_releases(h):
    choice = h.paths.claude_code
    containerfile = hostimages.agent(None).containerfile
    release = hostimages.agent(None).tag()
    assert hostimages.agent(choice).tag() == release

    claudecode.choose(choice, "2.1.295", containerfile)
    assert hostimages.agent(choice).buildargs == (("CLAUDE_CODE_VERSION", "2.1.295"),)
    assert hostimages.agent(choice).tag() != release

    claudecode.choose(choice, "2.1.200", containerfile)
    assert not choice.exists()
    assert hostimages.agent(choice).tag() == release


def test_an_update_moves_an_idle_tab_now_and_a_working_one_as_its_turn_ends(h):
    idle, working = h.tab(None), h.tab("myapi")
    old = _image_of(h, idle)
    h.session.agent_activity(working, busy=True)

    report = h.session.update_claude_code()

    assert report["version"] == "2.1.295" and report["pinned"] == PIN
    assert {"CLAUDE_CODE_VERSION": "2.1.295"} in h.runtime.builds_buildargs
    assert report["moving"] == [idle] and report["working"] == [working]
    _settles(lambda: _image_of(h, idle) == report["image"])
    assert _image_of(h, working) == old
    h.session.agent_activity(working, busy=False)
    _settles(lambda: _image_of(h, working) == report["image"])

    back = h.session.update_claude_code(pinned=True)
    assert back["version"] == PIN and not h.paths.claude_code.exists()
    _settles(lambda: _image_of(h, idle) == old)


def test_a_failed_build_keeps_the_claude_code_the_machine_had(h):
    h.runtime.build_should_fail = True
    with pytest.raises(HostImageError):
        h.session.update_claude_code()
    assert not h.paths.claude_code.exists()


def test_the_machine_tab_updates_and_moves_as_its_turn_ends_and_no_other_tab_can(h):
    machine, body = h.tab(None), h.tab("myapi")
    questions, channels = Questions(h.events, h.paths), Channels(h.session, h.events)
    with pytest.raises(SessionError):
        build_tab_methods(h.session, questions, channels, body)["update_claude_code"]()
    assert not h.paths.claude_code.exists()

    h.session.agent_activity(machine, busy=True)
    report = build_tab_methods(h.session, questions, channels, machine)["update_claude_code"]()
    assert report["version"] == "2.1.295" and machine in report["working"]
    h.session.agent_activity(machine, busy=False)
    _settles(lambda: _image_of(h, machine) == report["image"])
