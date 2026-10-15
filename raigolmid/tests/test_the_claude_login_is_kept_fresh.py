"""The claude.ai sign-in is the daemon's to renew (`claude_login.py`): renewed halfway through
its life, or at once when a tab is refused on it, by Claude Code's own `auth login` in a scratch agent container, kept 0600, and a
renewal that fails is said."""
from __future__ import annotations

import dataclasses
import json
import os
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


def _written(path: Path, login: dict, at: float = NOW) -> None:
    """Written `at`, which is where the halfway mark is counted from."""
    claude_login.write(path, login)
    os.utime(path, (at, at))


class _Runs(list):
    """The refresh token and scopes each `auth login` was handed, and what it does next:
    a status the token endpoint refuses with, or None to write a renewed login."""
    refuse: int | None = None


def _image(runtime):
    return lambda: hostimages.ensure(runtime, hostimages.agent(None))


@pytest.fixture()
def auth_login(tmp_path, monkeypatch):
    sources = tmp_path / "sources"
    (sources / "agents" / "claude").mkdir(parents=True)
    (sources / "agents" / "claude" / "Dockerfile").write_text("FROM scratch\n")
    monkeypatch.setenv(hostimages.SOURCE_ENV, str(sources))
    runtime = FakeRuntime()
    runtime.add_image(hostimages.agent(None).tag())
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


def test_it_is_renewed_halfway_through_its_life_and_not_before(tmp_path, auth_login):
    runtime, runs = auth_login
    path, events = tmp_path / "claude-login.json", EventLog(tmp_path / "events.jsonl")
    _written(path, _login(expires_in=28800))
    refresher = claude_login.Refresher(path, events, runtime, epoch=1, agent_image=_image(runtime))
    refresher.tick(NOW + 14399)
    assert runs == []

    refresher.tick(NOW + 14400)
    assert runs == [("old-refresh", "user:inference user:sessions:claude_code")]
    login = claude_login.read(path)
    oauth = login["claudeAiOauth"]
    assert (oauth["accessToken"], oauth["refreshToken"]) == ("new-access", "new-refresh")
    assert oauth["expiresAt"] == int((NOW + 28800) * 1000)
    assert login["oauthAccount"] == {"organizationUuid": "org-1"}, "the account is kept"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".claude-refresh-")] == []
    assert runtime.inspect(naming.claude_refresh()) is None


def test_a_tab_refused_on_it_has_it_renewed_at_once(tmp_path, auth_login):
    """The API can refuse a token before its stated expiry; a tab refused is the evidence."""
    runtime, runs = auth_login
    path, events = tmp_path / "claude-login.json", EventLog(tmp_path / "events.jsonl")
    _written(path, _login(expires_in=28800))
    refresher = claude_login.Refresher(path, events, runtime, epoch=1, agent_image=_image(runtime))
    refresher.tick(NOW + 60)
    assert runs == [], "nothing refused yet"
    first = events.emit("claude_login.refused", tab="tab-1")
    refresher.on_event(first)
    refresher.tick(NOW + 60)
    assert len(runs) == 1
    assert claude_login.read(path)["claudeAiOauth"]["accessToken"] == "new-access"
    # Refused in the same moment, and seen only once the renewal was done.
    refresher.on_event(dataclasses.replace(first, tab="tab-2"))
    refresher.tick(NOW + 120)
    assert len(runs) == 1, "one renewal answers every refusal made before it"


def test_a_renewal_that_fails_is_said_and_tried_again_later(tmp_path, auth_login):
    runtime, runs = auth_login
    path, events = tmp_path / "claude-login.json", EventLog(tmp_path / "events.jsonl")
    _written(path, _login(expires_in=60), at=NOW - 28740)
    runs.refuse = 403
    refresher = claude_login.Refresher(path, events, runtime, epoch=1, agent_image=_image(runtime))
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
    _written(refused, _login(expires_in=60), at=NOW - 28740)
    runs.refuse = 400
    claude_login.Refresher(refused, events, runtime, epoch=1, agent_image=_image(runtime)).tick(NOW)
    login = _login(expires_in=28800)
    login["claudeAiOauth"]["refreshTokenExpiresAt"] = int(NOW * 1000)
    _written(expired, login)
    claude_login.Refresher(expired, events, runtime, epoch=1, agent_image=_image(runtime)).tick(NOW)

    assert not refused.exists() and not expired.exists()
    assert len(runs) == 1, "an expired refresh token is not sent"
    lost = [e.data["error"] for e in events.tail(10) if e.type == "claude_login.lost"]
    assert len(lost) == 2 and "status code 400" in lost[0] and "expired" in lost[1]
    assert not any(e.type == "claude_login.refresh_failed" for e in events.tail(10))


def _line_out(tmp_path, monkeypatch):
    from raigolmid import lineout
    from tests.harness import Harness
    h = Harness(tmp_path, monkeypatch)
    handed_in = tmp_path / "ntfy-topic"
    handed_in.write_text("rai-line-test")
    monkeypatch.setattr(lineout, "TOPIC_FILE", handed_in)
    sent: list[str] = []
    monkeypatch.setattr(lineout, "send", lambda _topic, title, _message: sent.append(title))
    line = lineout.LineOut(h.paths, h.events)

    def hear() -> None:
        for event in line._sub.drain(timeout=0):
            line.on_event(event)
    return h, line, hear, sent


def test_the_agent_credential_refused_with_no_sign_in_goes_out_on_the_line(
        tmp_path, monkeypatch):
    h, line, hear, sent = _line_out(tmp_path, monkeypatch)
    _written(h.paths.claude_login, _login(3600))
    h.events.emit("agent.turn_failed", tab="tab-1", error=claude_login.TURN_REFUSED)
    hear()
    assert not sent, "a sign-in still set is renewed by the daemon"

    h.paths.claude_login.unlink()
    h.events.emit("agent.turn_failed", tab="tab-1", error=claude_login.TURN_REFUSED)
    hear()
    assert len(sent) == 1 and "sign in" in sent[0]


def _seen(line, start: float, seconds: float, step: float = 60.0) -> float:
    """`seconds` of the daemon ticking from `start`, as its run loop does; the time reached."""
    now = start
    line.tick(now)
    while now < start + seconds:
        now += step
        line.tick(now)
    return now


def test_every_tab_refused_for_a_day_goes_out_and_an_answered_turn_ends_it(
        tmp_path, monkeypatch):
    from raigolmid import lineout
    h, line, hear, sent = _line_out(tmp_path, monkeypatch)
    h.events.emit("agent.turn_failed", tab="tab-1", error="billing_error")
    hear()
    started = line._refused_at
    h.events.emit("agent.idle", tab="tab-2", done=True, error=None)
    hear()
    line.tick(started + lineout.REFUSED_FOR + 1)
    assert not sent

    h.events.emit("agent.turn_failed", tab="tab-1", error="billing_error")
    hear()
    failed = h.events_of("agent.turn_failed")[-1]
    now = failed.ts
    line.tick(now)
    # Refused again every hour, as a stalled run's wakes are.
    while now < failed.ts + lineout.REFUSED_FOR - 120:
        now += 60
        if now - line._refused_at >= 3600:
            line.on_event(dataclasses.replace(failed, ts=now))
        line.tick(now)
    assert not sent
    _seen(line, now, 180)
    assert len(sent) == 1 and "refuses" in sent[0]


def test_the_refusal_clock_runs_only_on_seen_unheld_time_with_a_recent_refusal(
        tmp_path, monkeypatch):
    """A boot after a day off, a usage-limit hold, and an old refusal never push."""
    from raigolmid import lineout
    h, line, hear, sent = _line_out(tmp_path, monkeypatch)
    h.events.emit("agent.turn_failed", tab="tab-1", error="invalid_request")
    hear()
    t = line._refused_at

    # A day off: the daemon stops and starts again a day later, refused at once.
    again = lineout.LineOut(h.paths, h.events)
    again._refused_at = t + lineout.DAY + 7200
    again.tick(t + lineout.DAY + 7200)
    assert not sent and again._refused_for < lineout.REFUSED_FOR, "downtime is not counted"

    # The account held at its usage limit for days: the clock stands still.
    h.events.emit("account.limited", resets_at=None, hold_until=None)
    hear()
    line._refused_at = t
    now = t
    line.tick(now)
    for _ in range(3 * 24):
        now += 60
        line._refused_at = now
        line.tick(now)
    assert line._refused_for == 0.0 and not sent

    # One refusal, then nothing tried for a day: not the API refusing now.
    h.events.emit("account.resumed")
    hear()
    _seen(line, now + 1, lineout.REFUSED_FOR + 3600)
    assert not sent and line._refused_for <= lineout.REFUSED_RECENTLY + 120

    # A gap longer than a tick is a suspended VM, not seen time.
    before = line._refused_for
    line._refused_at = now + 2 * lineout.DAY
    line.tick(now + 2 * lineout.DAY)
    assert line._refused_for == before


def test_the_sign_in_ending_goes_out_once_ahead_of_its_end(tmp_path, monkeypatch):
    from raigolmid import lineout
    h, line, hear, sent = _line_out(tmp_path, monkeypatch)
    messages: list[str] = []
    monkeypatch.setattr(lineout, "send",
                        lambda _topic, title, message: (sent.append(title),
                                                        messages.append(message)))
    login = _login(28800)
    ends = NOW + lineout.LOGIN_LEAD + 3600
    login["claudeAiOauth"]["refreshTokenExpiresAt"] = int(ends * 1000)
    _written(h.paths.claude_login, login)

    line.tick(ends - lineout.LOGIN_LEAD - 60)
    assert not sent, "not yet within the lead"
    line.tick(ends - lineout.LOGIN_LEAD + 60)
    assert len(sent) == 1 and "sign in to Claude again" in sent[0]
    assert "{ends}" not in messages[0] and "phone" in messages[0]
    line.tick(ends - lineout.LOGIN_LEAD + 60 + lineout.DAY + 1)
    again = lineout.LineOut(h.paths, h.events)
    again.tick(ends - 60)
    assert len(sent) == 1, "said once per end, across a restart"

    # A new sign-in has a new end, which is said in its turn.
    login["claudeAiOauth"]["refreshTokenExpiresAt"] = int((ends + lineout.DAY * 14) * 1000)
    _written(h.paths.claude_login, login)
    again.tick(ends + lineout.DAY * 14 - 3600)
    assert len(sent) == 2
