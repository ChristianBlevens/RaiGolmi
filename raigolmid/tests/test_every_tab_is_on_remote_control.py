"""Every tab is on Remote Control while the claude.ai sign-in is set, and on the agent credential
while it is not (`agents.Agents.start`, `remotecontrol.py`)."""
from __future__ import annotations

import json

import pytest

from raigolmid import claude_login, naming
from raigolmid.remotecontrol import RemoteControl
from tests.harness import Harness

MACHINE, BODY = "tab-1", "tab-2"


@pytest.fixture()
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.session.select("body", "myapi")
    assert (harness.tab(None), harness.tab("myapi")) == (MACHINE, BODY)
    return harness


def sign_in(h) -> None:
    claude_login.write(h.session.paths.claude_login, {
        "claudeAiOauth": {"accessToken": "sk-ant-oat01-the-real-login", "refreshToken": "r",
                          "expiresAt": 1, "scopes": [claude_login.SESSIONS_SCOPE]},
        "oauthAccount": {"organizationUuid": "org-1"}})


def spec(h, tab: str):
    return h.runtime._containers[naming.agent(tab)]["spec"]


def pump(rc: RemoteControl) -> None:
    for event in rc._sub.drain(timeout=0.05):
        rc.on_event(event)


def test_a_sign_in_puts_every_tab_on_remote_control_and_a_busy_one_once_its_turn_ends(h):
    rc = RemoteControl(h.session, h.events)
    assert "--remote-control" not in spec(h, BODY).command
    assert "CLAUDE_CODE_OAUTH_TOKEN" in spec(h, BODY).environment

    h.session.intent.tabs[BODY].busy = True
    sign_in(h)
    h.events.emit("claude_login.stored")
    pump(rc)
    assert spec(h, MACHINE).command[-2:] == ("--remote-control", f"machine tab ({MACHINE})")
    assert "--remote-control" not in spec(h, BODY).command, "its turn is not cut"

    h.session.intent.tabs[BODY].busy = False
    h.events.emit("agent.idle", tab=BODY)
    pump(rc)
    assert spec(h, BODY).command[-2:] == ("--remote-control", f"myapi ({BODY})")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in spec(h, BODY).environment
    home = h.session.agents.home(BODY)
    signed_in = (home / ".claude" / ".credentials.json").read_text()
    assert "the-real-login" not in signed_in and "sk-ant-oat01-rail-" in signed_in
    assert json.loads((home / ".claude.json").read_text())["oauthAccount"] == {
        "organizationUuid": "org-1"}


def test_a_lost_sign_in_puts_every_tab_back_on_the_agent_credential_and_continues_a_cut_turn(h):
    sign_in(h)
    rc = RemoteControl(h.session, h.events)
    rc._sign_in_idle()
    assert "--remote-control" in spec(h, BODY).command

    h.session.intent.tabs[BODY].busy = True
    h.session.paths.claude_login.unlink()
    h.events.emit("claude_login.lost", error="its refresh token has expired")
    pump(rc)
    for tab in (MACHINE, BODY):
        assert "--remote-control" not in spec(h, tab).command
        assert "CLAUDE_CODE_OAUTH_TOKEN" in spec(h, tab).environment
        assert not (h.session.agents.home(tab) / ".claude" / ".credentials.json").exists()
    continued = h.events_of("remote_control.continued")
    assert [e.tab for e in continued] == [BODY], "only the tab whose turn was cut"
