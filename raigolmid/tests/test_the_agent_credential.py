"""The one secret the machine keeps, and the door a new user meets it through.

Nobody is standing beside a fresh machine to put a token on it, so the AI terminal opens on
the question when there is no credential. The file it writes is the file `Agents.credentials`
reads — one module owns both, and these tests drive the pair rather than either alone.
"""
from __future__ import annotations

import stat
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from raigolmid import claude_login, credential                                    # noqa: E402
from ui.ai_terminal.terminal import _first_command                   # noqa: E402


def test_a_credential_is_never_readable_by_anyone_else(tmp_path):
    """Written 0600 from the first byte: this machine's whole job is running other people's
    code, so a window where the token is world-readable is a window where it leaves."""
    path = tmp_path / "agent-credentials"
    path.write_text("CLAUDE_CODE_OAUTH_TOKEN=old\n")
    path.chmod(0o644)
    credential.write(path, "CLAUDE_CODE_OAUTH_TOKEN", "new")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert credential.read(path) == {"CLAUDE_CODE_OAUTH_TOKEN": "new"}


def test_the_ai_terminal_opens_on_the_question_only_when_it_is_open(tmp_path, monkeypatch):
    """And a token given opens the agent's tab at once: only if the question was answered.
    Signing in to GitHub and to claude.ai follow it, each asked on every opening until done,
    and each numbered among those asked."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert _first_command() == ("rai credential --set --step 1/3 && { rai registry-token "
                                "--login --step 2/3; rai claude-login --login --step 3/3; "
                                "rai ai ready; }")

    from raigolmid.paths import Paths
    paths = Paths.from_env()
    credential.write(paths.agent_credentials, "CLAUDE_CODE_OAUTH_TOKEN", "token")
    assert _first_command() == ("rai registry-token --login --step 1/2; "
                                "rai claude-login --login --step 2/2"), "asked until signed in"

    credential.write(paths.registry_token, "GITHUB_TOKEN", "gho_x", credential.REGISTRY_KEYS)
    assert _first_command() == "rai claude-login --login --step 1/1"
    claude_login.write(paths.claude_login, {
        "claudeAiOauth": {"accessToken": "a", "refreshToken": "r", "expiresAt": 1,
                          "scopes": [claude_login.SESSIONS_SCOPE]},
        "oauthAccount": {"organizationUuid": "org-1"}})
    assert _first_command() == ""
