"""The user's claude.ai sign-in, which Remote Control needs and the agent credential cannot give.

The agent credential is a long-lived token, which Claude Code limits to inference; Remote
Control asks a full-scope login (`user:sessions:claude_code`). It is got once, at a first
start, by Claude Code's own `claude auth login` in a scratch agent container (`rai
claude-login --login`), and kept here as that login wrote it: its `claudeAiOauth` and the
account's `oauthAccount`. Like the agent credential it never enters a tab — every tab gets
placeholders the proxy swaps (`credproxy.py`) — so the daemon is its one refresher: a login
refreshed in two places would have each refresh spend the other's refresh token.

The refresh is Claude Code's too: its `auth login`, handed the refresh token and scopes in
place of a browser (`REFRESH_TOKEN_ENV`, `SCOPES_ENV`), refreshes, writes the result to its home
as a login does, and exits 1 on any refusal, naming the token endpoint's HTTP status. It runs
the way the login does, in a scratch agent container with an empty home: a home holding a
login would start Claude Code's own background refresh, spending the same refresh token.
No other command waits for a refresh before it exits.

It is renewed halfway through the life its token was issued with, counted from when this file
was written, so a token the API refuses early, or a renewal that fails and is retried, still has
hours in hand. A tab refused on it (`claude_login.refused`, `remotecontrol.py`) is evidence it is
spent whatever its expiry says, and it is renewed at once.

It is asked again only when it has ended: its refresh token refused by the OAuth server
itself (400 or 401, which a retry is refused again) or past `refreshTokenExpiresAt`. The file is then
removed, so every place that asks whether it is set asks for it again (`claude_login.lost`,
answered in the AI terminal's base window); any other failed renewal is retried.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from . import hostimages, labels, naming
from .events import Event, EventLog
from .runtime.base import ContainerRuntime, ContainerSpec, Mount, RuntimeError_

REFRESH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_REFRESH_TOKEN"
SCOPES_ENV = "CLAUDE_CODE_OAUTH_SCOPES"
# How `auth login` says the token endpoint refused it: axios's own message.
REFUSED = re.compile(r"status code (\d{3})")
# How a tab's turn ends when the API refuses the token it was sent with (`agent.idle`'s error).
TURN_REFUSED = "authentication_failed"
# The OAuth server's refusal of the grant itself; Cloudflare's is 403, a fault 5xx.
ENDED_STATUSES = frozenset({"400", "401"})
HOME_IN_CONTAINER = "/login"
RUN_TIMEOUT = 120.0
# Remote Control's scope; a login without it is the agent credential over again.
SESSIONS_SCOPE = "user:sessions:claude_code"
# How soon a failed renewal is tried again; it does not decide whether one is.
RETRY_SECONDS = 300.0
# How often the file is looked at when no tab has been refused.
TICK_SECONDS = 30.0


class LoginError(RuntimeError):
    pass


class LoginEnded(LoginError):
    """Only a new sign-in brings it back."""


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
    check(login, str(path))
    return login


def check(login: dict[str, Any], where: str) -> None:
    """Refuses a login that is not a full-scope claude.ai sign-in, naming `where` it came from."""
    oauth = login.get("claudeAiOauth") or {}
    missing = [k for k in ("accessToken", "refreshToken", "expiresAt", "scopes") if k not in oauth]
    if missing or "organizationUuid" not in (login.get("oauthAccount") or {}):
        raise LoginError(f"{where} is not a claude.ai sign-in: it lacks "
                         f"{', '.join(missing) or 'the account'}")
    if SESSIONS_SCOPE not in oauth["scopes"]:
        raise LoginError(f"{where}'s sign-in lacks {SESSIONS_SCOPE}, which Remote Control needs")


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


def from_claude_home(home: Path, with_account: bool = True) -> dict[str, Any]:
    """What `claude auth login` left in a home: its credentials and, asked, the account."""
    try:
        login = {"claudeAiOauth": json.loads(
            (home / ".claude" / ".credentials.json").read_text())["claudeAiOauth"]}
        if with_account:
            login["oauthAccount"] = json.loads((home / ".claude.json").read_text())["oauthAccount"]
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        raise LoginError(f"`claude auth login` left no sign-in in {home}: {exc}") from exc
    return login


def refreshed(login: dict[str, Any], runtime: ContainerRuntime, epoch: int,
              beside: Path, agent_image: Callable[[], str]) -> dict[str, Any]:
    """The login renewed by Claude Code's `auth login` in a scratch agent container whose home
    is a directory made in `beside`. The account is the login's: a refresh does not change
    whose it is."""
    oauth = login["claudeAiOauth"]
    try:
        image = agent_image()
        # Left by a daemon that stopped mid-run, it would refuse every run after it.
        runtime.remove(naming.claude_refresh(), force=True)
        with tempfile.TemporaryDirectory(dir=beside, prefix=".claude-refresh-") as home:
            result = runtime.run_to_completion(ContainerSpec(
                name=naming.claude_refresh(),
                image=image,
                entrypoint=("claude",),
                command=("auth", "login"),
                labels={labels.MANAGED: "true", labels.ROLE: str(labels.Role.CLAUDE_REFRESH),
                        labels.EPOCH: str(epoch)},
                environment={"HOME": HOME_IN_CONTAINER,
                             REFRESH_TOKEN_ENV: oauth["refreshToken"],
                             SCOPES_ENV: " ".join(oauth["scopes"])},
                mounts=(Mount(source=home, target=HOME_IN_CONTAINER),),
                # The daemon's own user, so what it writes in the home is the daemon's to read.
                user=f"{os.getuid()}:{os.getgid()}",
                cap_drop=("ALL",),
                security_opt=("no-new-privileges:true",),
            ), timeout=RUN_TIMEOUT)
            if result.exit_code != 0:
                said = result.output.strip()[-500:]
                status = REFUSED.search(said)
                raise (LoginEnded if status and status.group(1) in ENDED_STATUSES else LoginError)(
                    f"claude auth login could not refresh the sign-in: exited "
                    f"{result.exit_code}: {said!r}")
            new = from_claude_home(Path(home), with_account=False)["claudeAiOauth"]
    except (hostimages.HostImageError, RuntimeError_) as exc:
        raise LoginError(f"the refresh's container could not run: {exc}") from exc
    renewed = {**login, "claudeAiOauth": new}
    check(renewed, "the refreshed sign-in")
    return renewed


class Renewal:
    """Whether the sign-in is being renewed, and how many renewals have ended: what the
    refresher and the proxy share. The old access token can be refused from the moment the
    token endpoint issues the new one, a moment before the new one is written here, so a
    request sent on the sign-in in between can be refused — and a tab refused there refreshes with its placeholder,
    which is refused too and signs it out for good. So the proxy holds a request on the sign-in
    while a renewal runs, and sends one again that a renewal overlapped (`credproxy.py`)."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._running = False
        self._ended = 0

    @contextlib.contextmanager
    def running(self) -> Iterator[None]:
        with self._cond:
            self._running = True
        try:
            yield
        finally:
            with self._cond:
                self._running = False
                self._ended += 1
                self._cond.notify_all()

    def settled(self) -> int:
        """Once no renewal runs: how many have ended. A renewal is bounded by its
        container's `RUN_TIMEOUT`, so this wait is too."""
        with self._cond:
            self._cond.wait_for(lambda: not self._running, timeout=RUN_TIMEOUT + 30.0)
            return self._ended

    def overlapped(self, settled: int) -> bool:
        """Whether a renewal ran since `settled` was answered."""
        with self._cond:
            return self._running or self._ended != settled


class Refresher:
    """Renews the sign-in halfway through its life or at once when a tab was refused on it,
    says so when it cannot, and removes it once it has ended. A new one is said by the daemon's
    file watch (`claude_login.stored`)."""

    def __init__(self, path: Path, events: EventLog, runtime: ContainerRuntime,
                 epoch: int, agent_image: Callable[[], str],
                 renewal: Renewal | None = None) -> None:
        self.path = path
        self.events = events
        self.runtime = runtime
        self.epoch = epoch
        # The agent image's tag, built if it is not here (`Agents.image`).
        self.agent_image = agent_image
        self.renewal = renewal if renewal is not None else Renewal()
        self._retry_at = 0.0
        self._sub = events.subscribe()
        # A tab was refused on the sign-in since the last renewal began.
        self._refused = False

    def run(self, stop: threading.Event) -> None:
        ticked = 0.0
        while not stop.is_set():
            for event in self._sub.drain(timeout=TICK_SECONDS):
                self.on_event(event)
            now = time.time()
            if self._refused or now >= ticked + TICK_SECONDS:
                ticked = now
                self.tick(now)

    def on_event(self, event: Event) -> None:
        if event.type == "claude_login.refused":
            self._refused = True

    def tick(self, now: float) -> None:
        if not self.path.exists() or now < self._retry_at:
            return
        try:
            oauth = read(self.path)["claudeAiOauth"]
            # Claude Code writes null when the token endpoint names no expiry.
            ends = oauth.get("refreshTokenExpiresAt")
            if ends is not None and ends / 1000 <= now:
                raise LoginEnded("its refresh token has expired")
            written = self.path.stat().st_mtime
            if not self._refused and now < written + (oauth["expiresAt"] / 1000 - written) / 2:
                return
            with self.renewal.running():
                write(self.path, refreshed(read(self.path), self.runtime, self.epoch,
                                          self.path.parent, self.agent_image))
            self._refused = False
        except LoginEnded as exc:
            self.path.unlink()
            self.events.emit("claude_login.lost", error=str(exc))
            return
        except LoginError as exc:
            self._retry_at = now + RETRY_SECONDS
            self.events.emit("claude_login.refresh_failed", error=str(exc))
            return
        self.events.emit("claude_login.refreshed")
