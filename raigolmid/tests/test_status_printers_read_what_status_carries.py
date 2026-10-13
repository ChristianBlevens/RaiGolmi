"""`rai status` and the AI terminal's tab list print the daemon's own `status`.

Both read per-agent keys, and a key the daemon stops carrying is a KeyError the first time
either runs with a tab open. The status here is the real one, from the host's method table
over a session with tabs open, not a hand-written dict."""
from __future__ import annotations

import argparse
import time

import pytest

import rai.__main__ as rai_main
import raigolmid.client as client_module
from raigolmid.api import build_methods
from raigolmid.channel import Channels
from raigolmid.history import History
from raigolmid.questions import Questions
from raigolmid.viewing import Viewing
from ui.ai_terminal import terminal

from tests.harness import Harness


def _answering(h):
    return h.served(build_methods(h.session, h.events, Questions(h.events, h.paths),
                                  Channels(h.session, h.events),
                                  Viewing(h.events, h.paths.viewing),
                                  History(h.events, h.paths)))


@pytest.fixture()
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.session.select("body", "myapi")
    return harness


def _agents(out: str) -> dict[str, list[str]]:
    return {line.split()[0]: line.split() for line in out.splitlines()
            if line.split()[:1] and line.split()[0].startswith("tab-")}


def test_rai_status_prints_each_tab_with_its_scope_and_state(h, monkeypatch, capsys):
    monkeypatch.setattr(rai_main, "_client", lambda: _answering(h))
    assert rai_main.cmd_status(argparse.Namespace(json=False)) == 0
    agents = _agents(capsys.readouterr().out.replace("●", " "))
    assert agents[h.tab(None)][1:] == ["machine", "[running,", "idle]"]
    assert agents[h.tab("myapi")][1:] == ["myapi", "[running,", "idle]"]


def test_rai_status_names_the_active_sandbox(h, monkeypatch, capsys):
    sandbox = h.open_sandbox("myapi", "python-dev")
    monkeypatch.setattr(rai_main, "_client", lambda: _answering(h))
    assert rai_main.cmd_status(argparse.Namespace(json=False)) == 0
    line = next(l for l in capsys.readouterr().out.splitlines() if sandbox in l.split()[:1])
    assert line.rstrip().endswith("←active")


def test_the_ai_terminal_lists_every_tab_by_its_window_name(h, monkeypatch, capsys):
    # The module's name, not the class's `__new__`: undoing a patched `__new__` leaves the
    # class unconstructible with arguments for every later test in the run.
    monkeypatch.setattr(client_module, "ApiClient", lambda *a, **k: _answering(h))
    assert terminal.main(argparse.Namespace(action="list")) == 0
    out = capsys.readouterr().out
    assert f"{h.tab(None)} machine" in out
    assert f"{h.tab('myapi')} myapi" in out


def test_rai_status_says_when_each_sign_in_ends_and_never_its_secret(h, monkeypatch, capsys):
    from raigolmid import claude_login, credential
    credential.write(h.paths.registry_token, "GITHUB_TOKEN", "gho_secretgithub",
                     credential.REGISTRY_KEYS)
    ends = 1_790_000_000
    claude_login.write(h.paths.claude_login, {
        "claudeAiOauth": {"accessToken": "secret-access", "refreshToken": "secret-refresh",
                          "expiresAt": ends * 1000, "refreshTokenExpiresAt": None,
                          "scopes": [claude_login.SESSIONS_SCOPE]},
        "oauthAccount": {"organizationUuid": "org-1"}})
    monkeypatch.setattr(rai_main, "_client", lambda: _answering(h))
    assert rai_main.cmd_status(argparse.Namespace(json=False)) == 0
    out = capsys.readouterr().out
    assert "setup-token, no expiry stated" in out
    assert "OAuth token, does not expire" in out
    assert (f"token until {time.strftime('%Y-%m-%d %H:%M', time.localtime(ends))}, "
            "renewable until not stated") in out
    assert "secret" not in out and "test-token" not in out
