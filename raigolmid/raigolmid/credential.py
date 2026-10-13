"""The host-managed agent credential: the one secret this machine keeps.

It is a file on the host rather than a login inside each agent, because a tab is a container
that comes and goes — an agent that opened on a login screen would be asking a question nobody
can answer from a tab, and would ask it again on the next tab (`agents.py`).

Reading and writing live together so the two cannot disagree about what a credential is: one
`KEY=value` line, exactly one of `CREDENTIAL_KEYS`, mode 0600.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import claude_login

if TYPE_CHECKING:
    from .paths import Paths

# Claude Code prefers ANTHROPIC_API_KEY over the subscription token, so a file setting both
# would bill a subscription user per token without telling them. Exactly one is allowed.
CREDENTIAL_KEYS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")


class CredentialError(RuntimeError):
    pass


# The machine's other secret: the token a catalog upload opens a pull request with.
REGISTRY_KEYS = ("GITHUB_TOKEN",)


def read(path: Path, keys: tuple[str, ...] = CREDENTIAL_KEYS) -> dict[str, str]:
    """The one credential in `path`, or a refusal saying what is wrong with it."""
    if not path.is_file():
        if keys == REGISTRY_KEYS:
            raise CredentialError(
                f"not signed in to GitHub ({path} is absent). Run `rai registry-token "
                "--login` in the AI terminal.")
        raise CredentialError(
            f"no agent credential at {path}. Open the AI terminal and run `rai credential "
            f"--set`, or run `claude setup-token` on any machine signed in to Claude and put "
            f"CLAUDE_CODE_OAUTH_TOKEN=<token> in that file with mode 0600.")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise CredentialError(f"{path} is mode {mode:04o}; a credential must be 0600")
    found: dict[str, str] = {}
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or key not in keys or not value:
            raise CredentialError(
                f"{path}:{number} is not KEY=value with KEY one of {', '.join(keys)}")
        found[key] = value
    if len(found) != 1:
        raise CredentialError(f"{path} must set exactly one of {', '.join(keys)}; "
                              f"it sets {', '.join(found) or 'none'}")
    return found


def write(path: Path, key: str, value: str,
          keys: tuple[str, ...] = CREDENTIAL_KEYS) -> None:
    """Replace the credential with `key=value`.

    ⚠ Created 0600 by `os.open`, never by writing and then chmod: between the two the token is
    a world-readable file on a machine whose whole job is running other people's code."""
    if key not in keys:
        raise CredentialError(f"{key} is not one of {', '.join(keys)}")
    value = value.strip()
    if not value or "\n" in value:
        raise CredentialError("a credential is one line and cannot be empty")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(f"{key}={value}\n")
    # An existing file keeps its old mode through O_CREAT, so it is set either way.
    os.chmod(path, 0o600)


def is_set(path: Path, keys: tuple[str, ...] = CREDENTIAL_KEYS) -> bool:
    """Whether the credential is there and well-formed, without saying anything about the
    secret itself."""
    try:
        read(path, keys)
    except CredentialError:
        return False
    return True


# What each agent credential is; neither states an expiry anywhere the machine can read.
AGENT_KINDS = {"CLAUDE_CODE_OAUTH_TOKEN": "setup-token", "ANTHROPIC_API_KEY": "API key"}
# GitHub's token prefixes (docs.github.com, "About authentication to GitHub"), longest first.
GITHUB_KINDS = (("github_pat_", "fine-grained personal access token"),
                ("ghp_", "personal access token"), ("gho_", "OAuth token"),
                ("ghu_", "GitHub App user token"), ("ghs_", "GitHub App installation token"))


def standing(paths: "Paths") -> dict[str, Any]:
    """Each credential the machine keeps: whether it is set, what kind it is, and when it
    ends where that is written down — never a secret itself."""
    try:
        oauth = claude_login.read(paths.claude_login)["claudeAiOauth"]
    except claude_login.LoginError as exc:
        login: dict[str, Any] = {"set": False, "why": str(exc)}
    else:
        ends = oauth.get("refreshTokenExpiresAt")
        login = {"set": True, "expires_at": oauth["expiresAt"] / 1000,
                 # Claude Code writes null when the token endpoint names none.
                 "refresh_token_expires_at": ends / 1000 if ends is not None else None}
    try:
        [key] = read(paths.agent_credentials)
    except CredentialError as exc:
        agent: dict[str, Any] = {"set": False, "why": str(exc)}
    else:
        agent = {"set": True, "kind": AGENT_KINDS[key], "expiry": "no expiry stated"}
    try:
        token = read(paths.registry_token, REGISTRY_KEYS)["GITHUB_TOKEN"]
    except CredentialError as exc:
        github: dict[str, Any] = {"set": False, "why": str(exc)}
    else:
        kind = next((k for prefix, k in GITHUB_KINDS if token.startswith(prefix)),
                    "token of no kind GitHub names")
        github = {"set": True, "kind": kind,
                  "expiry": ("does not expire" if kind == "OAuth token"
                             else "expiry not known here")}
    return {"claude_login": login, "agent": agent, "github": github}
