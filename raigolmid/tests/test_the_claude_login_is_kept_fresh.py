"""The claude.ai sign-in is the daemon's to renew (`claude_login.py`): renewed before it
expires with Claude Code's own request, kept 0600, and a renewal that fails is said."""
from __future__ import annotations

import io
import json
import stat
import urllib.error

import pytest

from raigolmid import claude_login
from raigolmid.events import EventLog

NOW = 1_790_000_000.0


def _login(expires_in: float) -> dict:
    return {"claudeAiOauth": {"accessToken": "old-access", "refreshToken": "old-refresh",
                              "expiresAt": int((NOW + expires_in) * 1000),
                              "scopes": ["user:inference", claude_login.SESSIONS_SCOPE]},
            "oauthAccount": {"organizationUuid": "org-1"}}


class _Sent(list):
    """What reached the token endpoint, and what it answers next."""
    answer: object


@pytest.fixture()
def token_endpoint(monkeypatch):
    sent = _Sent()

    def urlopen(request, timeout):
        sent.append((request.full_url, json.loads(request.data)))
        answer = sent.answer
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(json.dumps(answer).encode())

    sent.answer = {"access_token": "new-access", "refresh_token": "new-refresh",
                   "expires_in": 28800, "scope": "user:inference user:sessions:claude_code"}
    monkeypatch.setattr(claude_login.urllib.request, "urlopen", urlopen)
    return sent


def test_it_is_renewed_before_it_expires_and_not_before(tmp_path, token_endpoint):
    path, events = tmp_path / "claude-login.json", EventLog(tmp_path / "events.jsonl")
    claude_login.write(path, _login(expires_in=2 * claude_login.REFRESH_AHEAD_SECONDS))
    refresher = claude_login.Refresher(path, events)
    refresher.tick(NOW)
    assert token_endpoint == []

    claude_login.write(path, _login(expires_in=60))
    refresher.tick(NOW)
    [(url, body)] = token_endpoint
    assert url == claude_login.TOKEN_URL
    assert body == {"grant_type": "refresh_token", "refresh_token": "old-refresh",
                    "client_id": claude_login.CLIENT_ID,
                    "scope": "user:inference user:sessions:claude_code"}
    oauth = claude_login.read(path)["claudeAiOauth"]
    assert (oauth["accessToken"], oauth["refreshToken"]) == ("new-access", "new-refresh")
    assert oauth["expiresAt"] == int((NOW + 28800) * 1000)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_renewal_refused_is_said_and_tried_again_later(tmp_path, token_endpoint):
    path, events = tmp_path / "claude-login.json", EventLog(tmp_path / "events.jsonl")
    claude_login.write(path, _login(expires_in=60))
    token_endpoint.answer = urllib.error.HTTPError(
        claude_login.TOKEN_URL, 400, "Bad Request", {}, io.BytesIO(b'{"error":"invalid_grant"}'))
    refresher = claude_login.Refresher(path, events)
    refresher.tick(NOW)
    [failed] = [e for e in events.tail(10) if e.type == "claude_login.refresh_failed"]
    assert "invalid_grant" in failed.data["error"]
    refresher.tick(NOW + 1)
    assert len(token_endpoint) == 1, "not hammered"
    assert claude_login.read(path)["claudeAiOauth"]["accessToken"] == "old-access"
