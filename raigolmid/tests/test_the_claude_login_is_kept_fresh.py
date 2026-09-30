"""The claude.ai sign-in is the daemon's to renew (`claude_login.py`): renewed before it
expires by Claude Code's own `auth login` in a scratch agent container, kept 0600, and a
renewal that fails is said."""
from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from raigolmid import claude_login, hostimages, naming
from raigolmid.events import EventLog
from raigolmid.runtime.base import ExecResult

from tests.fakeruntime import FakeRuntime

NOW = 1_790_000_000.0


def _login(expires_in: float) -> dict:
    return {"claudeAiOauth": {"accessToken": "old-access", "refreshToken": "old-refresh",
                              "expiresAt": int((NOW + expires_in) * 1000),
                              "scopes": ["user:inference", claude_login.SESSIONS_SCOPE]},
            "oauthAccount": {"organizationUuid": "org-1"}}


class _Runs(list):
    """The refresh token and scopes each `auth login` was handed, and what it does next:
    a status the token endpoint refuses with, or None to write a renewed login."""
    refuse: int | None = None


@pytest.fixture()
def auth_login(tmp_path, monkeypatch):
    sources = tmp_path / "sources"
    (sources / "agents" / "claude").mkdir(parents=True)
    (sources / "agents" / "claude" / "Dockerfile").write_text("FROM scratch\n")
    monkeypatch.setenv(hostimages.SOURCE_ENV, str(sources))
    runtime = FakeRuntime()
    runtime.add_image(hostimages.agent().tag())
    runs = _Runs()

    def login(spec) -> ExecResult:
        # What 2.1.283's `auth login` does with a refresh token in its environment: refreshes
        # without a browser, writes the credentials into $HOME, and says a refusal by its status.
        assert spec.entrypoint == ("claude",) and spec.command == ("auth", "login")
        [home] = [Path(m.source) for m in spec.mounts
                  if m.target == spec.environment["HOME"] and not m.read_only]
        assert list(home.iterdir()) == [], "a home holding a login would refresh on its own"
        env = spec.environment
        runs.append((env[claude_login.REFRESH_TOKEN_ENV], env[claude_login.SCOPES_ENV]))
        if runs.refuse is not None:
            return ExecResult(1, "Login failed: Request failed with status code "
                                 f"{runs.refuse}\n")
        (home / ".claude").mkdir()
        (home / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "new-access", "refreshToken": "new-refresh",
            "expiresAt": int((NOW + 28800) * 1000), "refreshTokenExpiresAt": None,
            "scopes": env[claude_login.SCOPES_ENV].split(), "subscriptionType": "max"}}))
        return ExecResult(0, "Login successful.\n")

    runtime.one_shot[naming.claude_refresh()] = login
    return runtime, runs


def test_it_is_renewed_before_it_expires_and_not_before(tmp_path, auth_login):
    runtime, runs = auth_login
    path, events = tmp_path / "claude-login.json", EventLog(tmp_path / "events.jsonl")
    claude_login.write(path, _login(expires_in=2 * claude_login.REFRESH_AHEAD_SECONDS))
    refresher = claude_login.Refresher(path, events, runtime, epoch=1)
    refresher.tick(NOW)
    assert runs == []

    claude_login.write(path, _login(expires_in=60))
    refresher.tick(NOW)
    assert runs == [("old-refresh", "user:inference user:sessions:claude_code")]
    login = claude_login.read(path)
    oauth = login["claudeAiOauth"]
    assert (oauth["accessToken"], oauth["refreshToken"]) == ("new-access", "new-refresh")
    assert oauth["expiresAt"] == int((NOW + 28800) * 1000)
    assert login["oauthAccount"] == {"organizationUuid": "org-1"}, "the account is kept"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".claude-refresh-")] == []
    assert runtime.inspect(naming.claude_refresh()) is None


def test_a_renewal_that_fails_is_said_and_tried_again_later(tmp_path, auth_login):
    runtime, runs = auth_login
    path, events = tmp_path / "claude-login.json", EventLog(tmp_path / "events.jsonl")
    claude_login.write(path, _login(expires_in=60))
    runs.refuse = 403
    refresher = claude_login.Refresher(path, events, runtime, epoch=1)
    refresher.tick(NOW)
    [failed] = [e for e in events.tail(10) if e.type == "claude_login.refresh_failed"]
    assert "status code 403" in failed.data["error"]
    refresher.tick(NOW + 1)
    assert len(runs) == 1, "not hammered"
    assert claude_login.read(path)["claudeAiOauth"]["accessToken"] == "old-access"
    refresher.tick(NOW + claude_login.RETRY_SECONDS)
    assert len(runs) == 2, "tried again"


def test_a_sign_in_that_has_ended_is_removed_so_it_is_asked_again(tmp_path, auth_login):
    """Only a new sign-in brings back a refresh token the OAuth server refuses or one past
    its expiry, so it is removed and said, and nothing retries it."""
    runtime, runs = auth_login
    events = EventLog(tmp_path / "events.jsonl")
    refused, expired = tmp_path / "refused.json", tmp_path / "expired.json"
    claude_login.write(refused, _login(expires_in=60))
    runs.refuse = 400
    claude_login.Refresher(refused, events, runtime, epoch=1).tick(NOW)
    login = _login(expires_in=2 * claude_login.REFRESH_AHEAD_SECONDS)
    login["claudeAiOauth"]["refreshTokenExpiresAt"] = int(NOW * 1000)
    claude_login.write(expired, login)
    claude_login.Refresher(expired, events, runtime, epoch=1).tick(NOW)

    assert not refused.exists() and not expired.exists()
    assert len(runs) == 1, "an expired refresh token is not sent"
    lost = [e.data["error"] for e in events.tail(10) if e.type == "claude_login.lost"]
    assert len(lost) == 2 and "status code 400" in lost[0] and "expired" in lost[1]
    assert not any(e.type == "claude_login.refresh_failed" for e in events.tail(10))
