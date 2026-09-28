"""How every document the user can edit is written.

**A save over a newer write is refused.** A document's version is the sha256 of its bytes
(`None` when it does not exist); a save names the version it was edited from and is refused
with `StaleDocument` when the file has moved on, so the catalog shows the user the newer text rather
than overwriting what an agent or the judge wrote meanwhile. Every write the daemon makes to a
document goes through `write` or `update`, under one lock, so the check and the replace are one
step against the daemon's own writers; an agent writes through its mount, and the version check
is what catches that.

**A document is replaced whole**: a temp file beside it, fsynced, renamed over it with the old
file's mode. The temp is `<name>.tmp.<hex>`, the name `documents.changed_after` already skips,
so a save in progress never makes a layer look stale.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import threading
from pathlib import Path
from typing import Callable


class DocumentError(Exception):
    pass


class StaleDocument(DocumentError):
    """Its message is what the window shows; the newer text is a `read` away."""


_LOCK = threading.RLock()


def version(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def _replace(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o7777
    except FileNotFoundError:
        mode = 0o644
    tmp = path.with_name(f"{path.name}.tmp.{secrets.token_hex(4)}")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.chmod(mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def read(path: Path) -> tuple[str | None, str | None]:
    """Its text and version, together; both None when it does not exist."""
    with _LOCK:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None, None
        return data.decode("utf-8"), hashlib.sha256(data).hexdigest()


def write(path: Path, text: str, expected: str | None) -> str:
    """Replace `path` with `text` if it is still at `expected`; the new version."""
    with _LOCK:
        if version(path) != expected:
            raise StaleDocument(f"{path.name} was written after you opened it; this is the "
                                "newer text. Your edit was not saved.")
        _replace(path, text)
        return version(path)


def update(path: Path, change: Callable[[str | None], str]) -> None:
    """The daemon's own read-modify-write, under the same lock as the user's saves."""
    with _LOCK:
        text = path.read_text(encoding="utf-8") if path.exists() else None
        _replace(path, change(text))
