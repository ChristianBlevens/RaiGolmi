"""Images a newer build of the same repository replaced: the host's own (`hostimages.py`) and a
body's (`instances.py`). Each is tagged by what it was built from, so the newest tag is the one
its definition names and every older one is abandoned. An image with no name at all is
abandoned too: a load or build that puts another image under its tag leaves it, and nothing
can name it to remove it. Docker refuses an image a container was created from; that one
waits for the container to go, which the daemon hears as its `destroy`.
Every removal is followed by pruning the build cache no image holds.
"""
from __future__ import annotations

import logging
import threading
import weakref
from dataclasses import dataclass, field

from .runtime.base import ContainerRuntime, ImageInUse

logger = logging.getLogger(__name__)


@dataclass
class _Superseded:
    """Per engine: the current tags whose older ones were dropped (`drop_older`, once each,
    whether the tag was built, loaded or already here), and the older ones Docker refused
    while a container held them, which wait for that container to go (`release`)."""
    swept: set[str] = field(default_factory=set)
    held: set[str] = field(default_factory=set)


_SUPERSEDED: weakref.WeakKeyDictionary[ContainerRuntime, _Superseded] = \
    weakref.WeakKeyDictionary()
_DROP = threading.Lock()


def drop_older(runtime: ContainerRuntime, tag: str) -> None:
    """Every other tag of this image's repository, once this one is here: each change to what
    it is built from makes a new one, and nothing else ever removes the last (gigabytes
    within an hour of edits) — and every image left with no name. One a container was
    created from is refused by Docker — a surface, tab or body still runs it while its
    successor is built — and is held for `release`."""
    with _DROP:
        superseded = _SUPERSEDED.setdefault(runtime, _Superseded())
        if tag in superseded.swept:
            return
        superseded.swept.add(tag)
        repository = tag.rsplit(":", 1)[0]
        removed = False
        for info in runtime.list_images():
            if info.tags or info.repo_digests:
                stale = [old for old in info.tags
                         if old.rsplit(":", 1)[0] == repository and old != tag]
            else:
                stale = [info.id]
            for old in stale:
                try:
                    runtime.remove_image(old)
                except ImageInUse:
                    superseded.held.add(old)
                    logger.info("holding %s until its container goes", old)
                    continue
                superseded.held.discard(old)
                removed = True
                logger.info("removed %s, replaced by %s", old, tag)
        if removed:
            _prune_build_cache(runtime)


def release(runtime: ContainerRuntime) -> None:
    """A container went: each superseded image Docker refused is asked for again, and one no
    container holds any more goes."""
    with _DROP:
        superseded = _SUPERSEDED.setdefault(runtime, _Superseded())
        removed = False
        for old in sorted(superseded.held):
            try:
                runtime.remove_image(old)
            except ImageInUse:
                continue
            superseded.held.discard(old)
            removed = True
            logger.info("removed %s, which its last container held", old)
        if removed:
            _prune_build_cache(runtime)


def held(runtime: ContainerRuntime) -> bool:
    """Whether any superseded image waits on a container (`release`)."""
    superseded = _SUPERSEDED.get(runtime)
    return superseded is not None and bool(superseded.held)


def _prune_build_cache(runtime: ContainerRuntime) -> None:
    reclaimed = runtime.prune_build_cache()
    logger.info("pruned %d bytes of build cache no image holds", reclaimed)
