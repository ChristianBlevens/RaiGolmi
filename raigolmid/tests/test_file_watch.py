"""The daemon's dependency-file watch, on real `watchfiles`.

The session is a stand-in because the question is the loop's: which paths it is watching
when a file changes. The watch itself is the real library on real files.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from raigolmid import claude_login, credential
from raigolmid.daemon import Daemon
from raigolmid.paths import Paths

pytest.importorskip("watchfiles")


class _Session:
    def __init__(self, watched: dict[str, list[Path]], definitions: list[Path] = ()) -> None:
        self.watched = watched
        self.definitions = list(definitions)
        self.triggered: list[str] = []
        self.fired = threading.Event()
        self.rediscovered = threading.Event()
        self.order: list[str] = []

    def definition_roots(self) -> list[Path]:
        return list(self.definitions)

    def rediscover(self) -> None:
        self.order.append("rediscover")
        self.rediscovered.set()

    def watched_paths(self) -> dict[str, list[Path]]:
        return dict(self.watched)

    def on_watched_change(self, instance: str) -> None:
        self.triggered.append(instance)
        self.fired.set()


class _Events:
    def __init__(self) -> None:
        self.emitted: list[str] = []
        self.stored = threading.Event()
        self.login_stored = threading.Event()

    def emit(self, type_: str, **data) -> None:
        self.emitted.append(type_)
        if type_ == "credential.stored":
            self.stored.set()
        if type_ == "claude_login.stored":
            self.login_stored.set()


def _daemon(tmp_path: Path, session: _Session) -> Daemon:
    daemon = Daemon.__new__(Daemon)
    daemon.session, daemon.events, daemon._stop = session, _Events(), threading.Event()
    daemon.paths = Paths(state=tmp_path / "state", data=tmp_path / "data",
                         config=tmp_path / "config" / "raigolmid", runtime=tmp_path / "run")
    return daemon


def test_an_instance_started_after_the_watch_began_is_watched(tmp_path):
    """A body tab's sandbox opened while the daemon runs builds from its body's working copy,
    a directory the watch was not started on. Its agent's dependency change must still
    rebuild it."""
    first_copy = tmp_path / "project"
    later = tmp_path / "other-project"
    for d in (first_copy, later):
        d.mkdir()
        (d / "requirements.txt").write_text("requests==2.32.3\n")
    session = _Session({"api@tab-2": [first_copy / "requirements.txt"]})

    daemon = _daemon(tmp_path, session)
    thread = threading.Thread(target=daemon._watch_files, daemon=True)
    thread.start()
    try:
        threading.Event().wait(1.0)    # the watch is running on the first copy alone
        session.watched["other@tab-3"] = [later / "requirements.txt"]
        threading.Event().wait(7.0)    # past one `rust_timeout`, when the loop re-reads
        (later / "requirements.txt").write_text("requests==2.32.3\nhttpx==0.27.2\n")

        assert session.fired.wait(15), "a change in the later copy never reached the session"
        assert session.triggered == ["other@tab-3"]
    finally:
        daemon._stop.set()
        thread.join(15)


def test_a_definition_written_while_the_daemon_runs_is_discovered(tmp_path):
    """Asking the agent for a new face, toolbelt or body is part of choosing one, so what
    it writes has to reach the selector without restarting raigolmid. `definitions.changed`
    follows the re-read: maintenance sweeps the catalogue on it, and a stale one sends the
    janitor to document a layer that was just deleted."""
    faces = tmp_path / "faces"
    faces.mkdir()
    session = _Session({}, definitions=[faces])

    daemon = _daemon(tmp_path, session)
    daemon.events.emit = lambda type_, **data: session.order.append(type_)
    thread = threading.Thread(target=daemon._watch_files, daemon=True)
    thread.start()
    try:
        threading.Event().wait(1.0)
        (faces / "new").mkdir()
        (faces / "new" / "face.toml").write_text('id = "new"\n')

        assert session.rediscovered.wait(15), "a new definition was never discovered"
        for _ in range(50):
            if "definitions.changed" in session.order:
                break
            threading.Event().wait(0.1)
        changed = [e for e in session.order if e in ("rediscover", "definitions.changed")]
        assert changed[:2] == ["rediscover", "definitions.changed"], changed
    finally:
        daemon._stop.set()


def test_a_projects_own_output_is_not_a_definition_change(tmp_path):
    """A body with no `working_copy` is its own working copy, so its project builds inside
    the definition root; what its git ignores is output, not the layer, and an edit to its
    `body.toml` still is."""
    import subprocess
    bodies = tmp_path / "bodies"
    body = bodies / "game"
    (body / "target").mkdir(parents=True)
    (body / "body.toml").write_text('id = "game"\n')
    (body / ".gitignore").write_text("target/\n")
    subprocess.run(["git", "init", "-q", str(body)], check=True)
    session = _Session({}, definitions=[bodies])
    seen: list[list[str]] = []
    daemon = _daemon(tmp_path, session)
    daemon.events.emit = lambda type_, **data: (
        seen.append(data["files"]) if type_ == "definitions.changed" else None)
    thread = threading.Thread(target=daemon._watch_files, daemon=True)
    thread.start()
    try:
        threading.Event().wait(1.0)
        for n in range(20):
            (body / "target" / f"run-{n}.log").write_text("built\n")
        (body / ".git" / "touched").write_text("x")
        threading.Event().wait(1.5)
        assert not session.rediscovered.is_set(), seen
        (body / "body.toml").write_text('id = "game"\nname = "Game"\n')
        assert session.rediscovered.wait(15), "an edit to body.toml was never discovered"
        threading.Event().wait(0.5)
        assert seen == [[str(body / "body.toml")]], seen
    finally:
        daemon._stop.set()


def test_a_credential_stored_while_the_daemon_runs_is_said(tmp_path):
    """The janitor holds a fresh machine's failures until a credential exists, and its
    directory does not exist until the first one is stored."""
    faces = tmp_path / "faces"
    faces.mkdir()
    daemon = _daemon(tmp_path, _Session({}, definitions=[faces]))
    thread = threading.Thread(target=daemon._watch_files, daemon=True)
    thread.start()
    try:
        threading.Event().wait(1.0)
        assert not daemon.paths.agent_credentials.parent.exists()
        credential.write(daemon.paths.agent_credentials, "CLAUDE_CODE_OAUTH_TOKEN", "t")
        assert daemon.events.stored.wait(15), "the first credential was never said"
        daemon.events.stored.clear()
        threading.Event().wait(1.0)
        assert not daemon.events.stored.is_set(), "said once, not on every wake"
        credential.write(daemon.paths.agent_credentials, "CLAUDE_CODE_OAUTH_TOKEN", "u")
        assert daemon.events.stored.wait(15), "a replaced credential was never said"
    finally:
        daemon._stop.set()
        thread.join(15)


def test_a_claude_login_given_while_the_daemon_runs_is_said_and_its_renewal_is_not(tmp_path):
    """A sign-in given again after one ended settles the notice that asked for it."""
    faces = tmp_path / "faces"
    faces.mkdir()
    daemon = _daemon(tmp_path, _Session({}, definitions=[faces]))
    login = {"claudeAiOauth": {"accessToken": "a", "refreshToken": "r", "expiresAt": 1,
                               "scopes": [claude_login.SESSIONS_SCOPE]},
             "oauthAccount": {"organizationUuid": "org-1"}}
    thread = threading.Thread(target=daemon._watch_files, daemon=True)
    thread.start()
    try:
        threading.Event().wait(1.0)
        claude_login.write(daemon.paths.claude_login, login)
        assert daemon.events.login_stored.wait(15), "the sign-in was never said"
        daemon.events.login_stored.clear()
        claude_login.write(daemon.paths.claude_login,
                           {**login, "claudeAiOauth": {**login["claudeAiOauth"], "accessToken": "b"}})
        threading.Event().wait(2.0)
        assert not daemon.events.login_stored.is_set(), "a renewal is not a sign-in"
        daemon.paths.claude_login.unlink()
        threading.Event().wait(2.0)
        claude_login.write(daemon.paths.claude_login, login)
        assert daemon.events.login_stored.wait(15), "a sign-in after one ended was never said"
    finally:
        daemon._stop.set()
        thread.join(15)
