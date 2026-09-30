"""Names for instances and the containers under them.

Instance ids are `<body>@<tab>` — `myapi@tab-2`, the sandbox of that body's tab — and every
container name is derived from one. The instance with no body, a toolbelt on `Paths.work`, is
`work`: the one id with no `@`, so no body id can take it, and only ever the machine tab's.
Derivation rather than storage is the point: after a crash the
daemon finds its containers by label, and the names it expects are recomputed from intent
rather than read from an inventory that may be stale (principle 9).
"""
from __future__ import annotations

import re

PREFIX = "raigolmid"

_SAFE = re.compile(r"[^a-zA-Z0-9_.-]")


def _safe(part: str) -> str:
    return _SAFE.sub("-", part)


WORK = "work"


def instance_id(body_id: str, owner: str) -> str:
    return f"{body_id}@{owner}"


def split(instance: str) -> tuple[str | None, str]:
    if instance == WORK:
        return None, WORK
    body, _, owner = instance.rpartition("@")
    if not body:
        raise ValueError(
            f"'{instance}' is not a sandbox id (expected '<body>@<owner>' or '{WORK}')")
    return body, owner


def has_body(instance: str) -> bool:
    return split(instance)[0] is not None


def _body_of(instance: str) -> tuple[str, str]:
    body, owner = split(instance)
    if body is None:
        raise ValueError(f"'{instance}' has no body, so it has no body container or project")
    return body, owner


def _parts(instance: str) -> str:
    """The container-name stem: `<body>-<owner>`, or `work` — which no body's stem can equal,
    since every one carries an owner segment."""
    body, owner = split(instance)
    return WORK if body is None else f"{_safe(body)}-{_safe(owner)}"


def tab_ref(tab_id: str) -> str:
    """The reference a tab holds on the sandbox it opened."""
    return f"tab:{tab_id}"


def anchor(instance: str) -> str:
    return f"{PREFIX}-anchor-{_parts(instance)}"


def view(instance: str) -> str:
    return f"{PREFIX}-view-{_parts(instance)}"


def network() -> str:
    """The machine's network: every sandbox's anchor and the user's face, each by name, so a
    face reaches every body and every body the face."""
    return PREFIX


FACE_HOST = "face"
_DNS = re.compile(r"[^a-z0-9-]")


def host(instance: str) -> str:
    """A sandbox's name on `network()`: `<body>.<tab>` (`myapi.tab-2`), or `work`. DNS takes
    letters, digits and hyphens per label, so anything else in a body id becomes a hyphen;
    the tab is its own label, so no two sandboxes' names meet."""
    body, owner = split(instance)
    if body is None:
        return WORK
    return f"{_DNS.sub('-', body.lower())[:63]}.{_DNS.sub('-', owner.lower())[:63]}"


def door() -> str:
    """The door (`doors.py`). One name: only the active sandbox's ports reach the host."""
    return f"{PREFIX}-door"


def face_mount() -> str:
    """The face-mount helper (`facemounts.py`). One name, so two can never run at once."""
    return f"{PREFIX}-face-mount"


def face_input() -> str:
    """The helper that types into the face's display (`Faces.input`). One name: keys sent
    twice at once would interleave."""
    return f"{PREFIX}-face-input"


def screenshot() -> str:
    """The helper that captures the face's display (`Faces.screenshot`). One name: a second
    capture waits for the first rather than racing it."""
    return f"{PREFIX}-screenshot"


def claude_refresh() -> str:
    """The claude.ai sign-in's one-shot refresh (`claude_login.py`). One name: the daemon has
    one refresher."""
    return f"{PREFIX}-claude-refresh"


def judge() -> str:
    """The preferences judge's one-shot run (`judge.py`). One name: it runs one job at a time."""
    return f"{PREFIX}-judge"


def flake_build() -> str:
    """The nix builder (`flakes.py`). One name, so two builds never share its store at once."""
    return f"{PREFIX}-flake-build"


def closure_copy() -> str:
    """The helper that copies a closure out of its image (`closures.py`). One name, so two can
    never run at once."""
    return f"{PREFIX}-closure-copy"


def face_trial() -> str:
    """The face tried off the user's screen (`Faces.start_trial`). One name: there is one trial."""
    return f"{PREFIX}-face-trial"


def face(face_id: str) -> str:
    """The face compositor container.

    Named from the face id rather than from an instance: a face belongs to the session, not
    to a body instance, and only one is on the screen at a time.
    """
    return f"{PREFIX}-face-{_safe(face_id)}"


def compose_project(instance: str) -> str:
    body, owner = _body_of(instance)
    # Compose lowercases project names and rejects most punctuation.
    return f"{PREFIX}-{_safe(body)}-{_safe(owner)}".lower()


def body_service() -> str:
    return "body"


def body_container(instance: str) -> str:
    """The name Compose gives the body service in this instance's project."""
    return f"{compose_project(instance)}-{body_service()}-1"


def agent(tab_id: str) -> str:
    return f"{PREFIX}-agent-{_safe(tab_id)}"


BODY_REPOSITORIES = f"{PREFIX}/body-"


def body_repository(body_id: str) -> str:
    return f"{BODY_REPOSITORIES}{_safe(body_id).lower()}"


def build_tag(body_id: str, digest: str) -> str:
    """Images are tagged by definition digest, so an image that is already current is
    recognised without a build."""
    short = digest.split(":")[-1][:12]
    return f"{body_repository(body_id)}:{short}"


def host_image_tag(name: str, digest: str) -> str:
    """The host's own images carry the body's rule: tagged by source digest."""
    short = digest.split(":")[-1][:12]
    return f"raigolmi/{_safe(name).lower()}:{short}"
