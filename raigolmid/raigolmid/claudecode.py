"""Which Claude Code the agent image installs: the release's, or a newer one the user asked for.

The release pins one (`agents/claude/Dockerfile`'s `CLAUDE_CODE_VERSION`), because the channel
rests on how that version speaks to `mcp`. A machine whose release has not moved in a while can
still take the newest: `rai claude-update` records it in `Paths.claude_code`, and the agent image
is then built with it as a build argument, so its tag names the version too (`hostimages.agent`).
A choice no newer than the pin is no choice — a later release that pins past it wins — and
`rai claude-update --pinned` removes it.

A move to a newer one is on trial until a tab's first turn on its image says whether it works:
the choice holds the version it replaced and the image it made (`trial`), and the version goes
back to that one when the first session on it fails (`rollback.py`). The pin is the release's
own, never on trial.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from .intent import load_json, save_json

ARG = "CLAUDE_CODE_VERSION"
PACKAGE = "@anthropic-ai/claude-code"
# The registry's `latest` dist-tag: the newest stable release, never a prerelease.
LATEST_URL = "https://registry.npmjs.org/@anthropic-ai%2Fclaude-code/latest"

_PIN = re.compile(rf"^ARG {ARG}=(\S+)$", re.MULTILINE)
_VERSION = re.compile(r"^\d+(\.\d+)*$")


class ClaudeCodeError(RuntimeError):
    pass


def _parsed(version: str, where: str) -> tuple[int, ...]:
    if not _VERSION.match(version):
        raise ClaudeCodeError(f"{where} names Claude Code {version!r}, which is not a version")
    return tuple(int(part) for part in version.split("."))


def pinned(containerfile: Path) -> str:
    """The release's version: the agent Dockerfile's default."""
    found = _PIN.search(containerfile.read_text(encoding="utf-8"))
    if found is None:
        raise ClaudeCodeError(f"{containerfile} declares no `ARG {ARG}=<version>`")
    _parsed(found.group(1), str(containerfile))
    return found.group(1)


def chosen(choice: Path) -> str | None:
    """The version the user asked for, or None when they have not."""
    data = load_json(choice, "Claude Code choice")
    if data is None:
        return None
    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(version, str):
        raise ClaudeCodeError(f"{choice} holds no version: {data!r}")
    _parsed(version, str(choice))
    return version


def newer_than_pin(choice: Path, containerfile: Path) -> str | None:
    """The version to build with in place of the pin, or None for the pin itself."""
    version = chosen(choice)
    if version is None:
        return None
    if _parsed(version, str(choice)) <= _parsed(pinned(containerfile), str(containerfile)):
        return None
    return version


def choose(choice: Path, version: str | None, containerfile: Path,
           trial: dict[str, str | None] | None = None) -> None:
    """Record `version`, on `trial` when given; None, or one no newer than the pin, goes back
    to the release's."""
    if version is None or (_parsed(version, "the choice")
                           <= _parsed(pinned(containerfile), str(containerfile))):
        choice.unlink(missing_ok=True)
        return
    save_json(choice, {"version": version, **({"trial": trial} if trial is not None else {})})


def on_trial(choice: Path) -> dict[str, str | None] | None:
    """The chosen version's trial: `from`, the version it replaced (None for the pin), and
    `image`, the agent image it made; None once it has proven itself or when nothing is
    chosen."""
    data = load_json(choice, "Claude Code choice")
    if data is None or "trial" not in data:
        return None
    trial = data["trial"]
    if (not isinstance(trial, dict) or not isinstance(trial.get("image"), str)
            or not isinstance(trial.get("from"), (str, type(None)))):
        raise ClaudeCodeError(f"{choice} holds a trial that is not one: {trial!r}")
    return trial


def latest(fetch: Callable[[str], bytes] | None = None) -> str:
    """The newest stable Claude Code, asked of the npm registry."""
    raw = (fetch or _fetch)(LATEST_URL)
    try:
        version = json.loads(raw)["version"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ClaudeCodeError(f"{LATEST_URL} answered without a version: {raw[:200]!r}") from exc
    if not isinstance(version, str):
        raise ClaudeCodeError(f"{LATEST_URL} answered version {version!r}")
    _parsed(version, LATEST_URL)
    return version


def _fetch(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read()
    except urllib.error.URLError as exc:
        raise ClaudeCodeError(f"{url} did not answer: {exc}") from exc
