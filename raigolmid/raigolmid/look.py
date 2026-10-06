"""The user's `[look]` where the host surfaces read it (`ui/theme.py`).

A surface runs in a container that has only the runtime dir, so the look is a JSON file there
(`Paths.look`). It is written by `hostsurfaces._wayland_spec`, which every surface is started
through, so no surface can start before its look exists; the container is labelled with the
look's digest, which is how a running surface drawn with an older look is told from a current
one (`hostsurfaces.selector_current`, `daemon._apply_look`).
"""
from __future__ import annotations

import hashlib
import json
import os

from . import settings
from .paths import Paths


def render(paths: Paths) -> str:
    return json.dumps(settings.in_force(paths).look, indent=1, sort_keys=True) + "\n"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def current(paths: Paths) -> str:
    """The digest of the look the user's settings now make."""
    return digest(render(paths))


def write(paths: Paths) -> str:
    """Write the look the user's settings make, replacing the file whole so a surface
    starting meanwhile never reads half of one, and return its digest."""
    text = render(paths)
    path = paths.look
    if not (path.is_file() and path.read_text(encoding="utf-8") == text):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    return digest(text)
