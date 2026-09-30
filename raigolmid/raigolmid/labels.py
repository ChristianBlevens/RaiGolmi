"""The label schema.

Everything raigolmid creates is labelled so it can be found again with no stored
inventory. The prefix is owned by the project because it leaks into other people's
images.
"""
from __future__ import annotations

from enum import StrEnum

PREFIX = "io.raigolmi"

MANAGED = f"{PREFIX}.managed"
ROLE = f"{PREFIX}.role"
INSTANCE = f"{PREFIX}.instance"
TAB = f"{PREFIX}.tab"
BODY_CONTAINER = f"{PREFIX}.body_container"
FACE = f"{PREFIX}.face"
# The image id of a face's apps closure (`closures.py`), whose store paths it shows.
FACE_CLOSURE = f"{PREFIX}.face_closure"
VIEW_GENERATION = f"{PREFIX}.view_generation"
DEFINITION_DIGEST = f"{PREFIX}.definition_digest"
# On an agent: the definition repositories' git protections it was made with (`agents.py`).
GIT_PROTECTED = f"{PREFIX}.git_protected"
# On a host surface: the digest of the look it was drawn with (`look.py`).
LOOK = f"{PREFIX}.look"
EPOCH = f"{PREFIX}.epoch"
# On a body: the ports it listens on inside its anchor. A rebuild replaces the body, so this
# is always the running definition's; the door publishes the active sandbox's.
BODY_PORTS = f"{PREFIX}.body_ports"
# On the door: the sandbox, anchor address and ports it forwards (`doors.py`).
DOOR = f"{PREFIX}.door"


class Role(StrEnum):
    ANCHOR = "anchor"
    BODY = "body"
    VIEW = "view"
    FACE = "face"
    # A face tried off the user's screen by the machine tab (`Faces.start_trial`): one at a time,
    # headless, never the face they see.
    FACE_TRIAL = "face-trial"
    AGENT = "agent"
    # The host's own surfaces. They carry no instance, because they belong to the
    # host rather than to a session — a reconcile must not treat them as a session's leftovers.
    SELECTOR = "selector"
    CONTROL = "control"
    NOTIFY = "notify"
    CATALOG = "catalog"
    # The one-shot helper that mounts what a face shows (`facemounts.py`). It exits
    # before its caller returns, so one still present is residue, swept as an orphan.
    FACE_MOUNT = "face-mount"
    # The one-shot helper that copies a Nix closure out of its image (`closures.py`); residue
    # the same way.
    CLOSURE_COPY = "closure-copy"
    # The one container publishing the active sandbox's ports (`doors.py`). It
    # belongs to the session, not to an instance: the face moving replaces it.
    DOOR = "door"
    # The one-shot `grim` that captures the face's display for an agent (`Faces.screenshot`);
    # residue the same way.
    SCREENSHOT = "screenshot"
    # The one-shot `wtype` that types into the face's display for an agent (`Faces.input`).
    FACE_INPUT = "face-input"
    # The preferences judge's one-shot `claude -p` (`judge.py`).
    JUDGE = "judge"
    # The one-shot nix builder a toolbelt Nixery refuses is built in (`flakes.py`).
    FLAKE_BUILD = "flake-build"
    # The one-shot `claude auth login` that refreshes the claude.ai sign-in (`claude_login.py`).
    CLAUDE_REFRESH = "claude-refresh"


# Run once for a caller and removed by it. One running under the daemon run that started it
# belongs to a caller still waiting; any other is residue, swept as an orphan.
ONE_SHOT = frozenset({Role.FACE_MOUNT, Role.CLOSURE_COPY, Role.SCREENSHOT, Role.FACE_INPUT,
                      Role.JUDGE, Role.FLAKE_BUILD, Role.CLAUDE_REFRESH})


class Kind(StrEnum):
    FACE = "face"
    TOOLBELT = "toolbelt"
    BODY = "body"


def managed_filter(**extra: str) -> dict[str, str]:
    """A Docker label filter that matches only what this project owns."""
    f = {MANAGED: "true"}
    f.update(extra)
    return f
