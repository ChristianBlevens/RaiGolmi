"""The user's claude.ai sign-in, which Remote Control needs and the agent credential cannot give.

The agent credential is a long-lived token, which Claude Code limits to inference; Remote
Control asks a full-scope login (`user:sessions:claude_code`). It is got once, at a first
start, by Claude Code's own `claude auth login` in a scratch agent container (`rai
claude-login --login`), and kept here as that login wrote it: its `claudeAiOauth` and the
account's `oauthAccount`. Like the agent credential it never enters a tab — a held tab gets
placeholders the proxy swaps (`credproxy.py`) — so the daemon is its one refresher: a login
refreshed in two places would have each refresh spend the other's refresh token.

The refresh is Claude Code's own (2.1.283): a JSON POST of the refresh token, the client id
and the scopes to `TOKEN_URL`, answered with `access_token`, `expires_in` and, when it turns,
`refresh_token`.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .events import EventLog

TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
# Remote Control's scope; a login without it is the agent credential over again.
SESSIONS_SCOPE = "user:sessions:claude_code"
# An access token lives hours; these only set how early it is renewed and how soon a failed
# renewal is tried again, not whether renewal works.
REFRESH_AHEAD_SECONDS = 3600.0
RETRY_SECONDS = 300.0


class LoginError(RuntimeError):
    pass


def read(path: Path) -> dict[str, Any]:
    """The login as `claude auth login` wrote it, or a refusal saying what is wrong."""
    try:
        login = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise LoginError(f"no claude.ai sign-in at {path}. Run `rai claude-login --login` in "
                         "the AI terminal.") from None
    except json.JSONDecodeError as exc:
        raise LoginError(f"{path} does not parse: {exc}") from exc
    if os.stat(path).st_mode & 0o077:
        raise LoginError(f"{path} must be readable by its owner alone (0600)")
    oauth = login.get("claudeAiOauth") or {}
    missing = [k for k in ("accessToken", "refreshToken", "expiresAt", "scopes") if k not in oauth]
    if missing or "organizationUuid" not in (login.get("oauthAccount") or {}):
        raise LoginError(f"{path} is not a claude.ai sign-in: it lacks "
                         f"{', '.join(missing) or 'the account'}")
    if SESSIONS_SCOPE not in oauth["scopes"]:
        raise LoginError(f"{path}'s sign-in lacks {SESSIONS_SCOPE}, which Remote Control needs")
    return login


def is_set(path: Path) -> bool:
    try:
        read(path)
    except LoginError:
        return False
    return True


def write(path: Path, login: dict[str, Any]) -> None:
    """Replaced whole, 0600 from the moment it exists (`credential.write`)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(login, handle)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def from_claude_home(home: Path) -> dict[str, Any]:
    """What `claude auth login` left in a home: its credentials and the account."""
    try:
        oauth = json.loads((home / ".claude" / ".credentials.json").read_text())["claudeAiOauth"]
        account = json.loads((home / ".claude.json").read_text())["oauthAccount"]
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        raise LoginError(f"`claude auth login` left no sign-in in {home}: {exc}") from exc
    return {"claudeAiOauth": oauth, "oauthAccount": account}


def refreshed(login: dict[str, Any], now: float) -> dict[str, Any]:
    oauth = login["claudeAiOauth"]
    body = json.dumps({"grant_type": "refresh_token", "refresh_token": oauth["refreshToken"],
                       "client_id": CLIENT_ID, "scope": " ".join(oauth["scopes"])}).encode()
    request = urllib.request.Request(TOKEN_URL, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as answer:
            said = json.loads(answer.read())
    except urllib.error.HTTPError as exc:
        raise LoginError(f"{TOKEN_URL} refused the refresh: {exc.code} "
                         f"{exc.read()[:300].decode(errors='replace')}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise LoginError(f"{TOKEN_URL} could not refresh the sign-in: {exc}") from exc
    new = {**oauth, "accessToken": said["access_token"],
           "refreshToken": said.get("refresh_token", oauth["refreshToken"]),
           "expiresAt": int((now + said["expires_in"]) * 1000)}
    if "refresh_token_expires_in" in said:
        new["refreshTokenExpiresAt"] = int((now + said["refresh_token_expires_in"]) * 1000)
    if "scope" in said:
        new["scopes"] = said["scope"].split()
    return {**login, "claudeAiOauth": new}


class Refresher:
    """Renews the sign-in before it expires, and says so when it cannot."""

    def __init__(self, path: Path, events: EventLog) -> None:
        self.path = path
        self.events = events
        self._retry_at = 0.0

    def run(self, stop: threading.Event) -> None:
        while not stop.wait(30.0):
            self.tick(time.time())

    def tick(self, now: float) -> None:
        if not self.path.exists() or now < self._retry_at:
            return
        try:
            login = read(self.path)
            if login["claudeAiOauth"]["expiresAt"] / 1000 - now > REFRESH_AHEAD_SECONDS:
                return
            write(self.path, refreshed(login, now))
        except LoginError as exc:
            self._retry_at = now + RETRY_SECONDS
            self.events.emit("claude_login.refresh_failed", error=str(exc))
            return
        self.events.emit("claude_login.refreshed")
