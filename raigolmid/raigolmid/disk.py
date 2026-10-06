"""The machine's disk, and who holds it.

The disk is one file on the user's machine that grows with what the guest writes and shrinks
when the guest frees it (the ten-minute trim, `host/systemd/fstrim-*.conf`). So the file is as
large as what is live here, and every live byte has a holder: the container images, their build
cache and the containers' own layers, the nix closures, the archives of closed tabs and the
crash logs, the agents' and the user's homes, the journal, and each body's directory — what its
git tracks, what it ignores (build and run output, which nothing else on the machine sees), and
its caches: directories tagged `CACHEDIR.TAG` (every cargo `target/`), whose tool recreates all
they hold and which grow with every build until something empties them.
A reading names each holder and sets their total against what the filesystem reports used; the
rest is **unnamed**: the OS's deployments and anything nothing here accounts for, which is
where a leak shows.

The used bytes rising past the last reported reading by `GROWTH_BYTES` is said as
`disk.grown`, and free space under `SHORT_FRACTION` of the filesystem as `disk.short`, once
per episode; the janitor takes both (`janitor.py`) with each holder's change since that
reading. What is the machine's the janitor repairs; what a body holds is the project's to
judge, so the janitor `tell`s that body's tab what it holds and never deletes it from here.

A holder that cannot be read is reported as unread, never as empty. The last reported reading
is kept in `Paths.disk`, and lowered whenever less is held, so growth across a daemon restart
or a boot is measured from the least the disk has held since it was said.
"""
from __future__ import annotations

import os
import re
import stat
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import git
from .budgets import Budgets
from .events import EventLog
from .intent import load_json, save_json
from .runtime.base import RuntimeError_

if TYPE_CHECKING:
    from .session import Session

# How far the used bytes rise past the last reported reading, and how little may be free,
# before the janitor looks. Both set how soon disk use is looked at, never whether.
GROWTH_BYTES = 2 * 1024 ** 3
SHORT_FRACTION = 0.15
# Sizing walks every file a body holds, so it is read this often rather than continuously.
TICK_SECONDS = 600.0
# The system journal, bounded by `host/systemd/journald.conf`.
JOURNAL = Path("/var/log/journal")
# How many of a body's largest ignored paths, and of a cache's largest entries, a reading names.
LARGEST = 8
# A cache directory says it is one with this file, starting with this line
# (https://bford.info/cachedir/): everything in it is its tool's to recreate. Cargo tags
# every `target/`.
CACHE_TAG = "CACHEDIR.TAG"
CACHE_SIGNATURE = b"Signature: 8a477f597d28d172789f06886806bc55"


def tree_bytes(root: Path, seen: set[tuple[int, int]], unread: list[str],
               caches: list[Path] | None = None) -> int:
    """The bytes allocated under `root`, each inode once across one reading (`seen`), on
    `root`'s own filesystem, never following a link. A directory that cannot be read is
    added to `unread`; each cache directory met is added to `caches` when it is given."""
    try:
        top = root.lstat()
    except FileNotFoundError:
        return 0
    except OSError as exc:
        unread.append(f"{root}: {exc.strerror}")
        return 0
    total, stack = 0, [root]
    if (top.st_dev, top.st_ino) in seen:
        return 0
    seen.add((top.st_dev, top.st_ino))
    total += top.st_blocks * 512
    if not stat.S_ISDIR(top.st_mode):
        return total
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            unread.append(f"{directory}: {exc.strerror}")
            continue
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                unread.append(f"{entry.path}: {exc.strerror}")
                continue
            if st.st_dev != top.st_dev or (st.st_dev, st.st_ino) in seen:
                continue
            seen.add((st.st_dev, st.st_ino))
            total += st.st_blocks * 512
            if stat.S_ISDIR(st.st_mode):
                stack.append(Path(entry.path))
            elif caches is not None and entry.name == CACHE_TAG and _is_cache_tag(entry.path):
                caches.append(Path(directory))
    return total


def _is_cache_tag(path: str) -> bool:
    try:
        with open(path, "rb") as tag:
            return tag.read(len(CACHE_SIGNATURE)) == CACHE_SIGNATURE
    except OSError:
        return False


def cache_reading(directory: Path, cache: Path, own: set[tuple[int, int]],
                  unread: list[str]) -> dict[str, Any]:
    """A cache in a body: its size, the tool its tag names, and its largest entries — where
    what the project put in it, which its tool cannot recreate, shows."""
    try:
        said = (cache / CACHE_TAG).read_text(errors="replace")
    except OSError as exc:
        unread.append(f"{cache / CACHE_TAG}: {exc.strerror}")
        said = ""
    found = re.search(r"created by (\S+?)\.?\s*$", said, re.MULTILINE)
    entries = sorted(((e.name, tree_bytes(Path(e.path), own, unread))
                      for e in os.scandir(cache)), key=lambda x: -x[1])
    return {"path": str(cache.relative_to(directory)), "tool": found[1] if found else None,
            "bytes": sum(b for _, b in entries),
            "largest": [{"entry": n, "bytes": b} for n, b in entries[:LARGEST]]}


def body_reading(directory: Path, seen: set[tuple[int, int]],
                 unread: list[str]) -> dict[str, Any]:
    """A body's project: everything; its build caches; and its output — what its git ignores
    outside those caches — with the largest of it."""
    found: list[Path] = []
    total = tree_bytes(directory, seen, unread, found)
    # Sized again with a reading of their own, since `seen` holds every inode already; the
    # caches first, so the output is what is left.
    own: set[tuple[int, int]] = set()
    caches = [cache_reading(directory, c, own, unread) for c in found
              if not any(o != c and c.is_relative_to(o) for o in found)]   # inside another
    out = {"bytes": total, "caches": caches, "caches_bytes": sum(c["bytes"] for c in caches),
           "output": None, "largest_output": []}
    if not git.is_repo(directory):
        return out
    try:
        listed = git.run(["ls-files", "--others", "--ignored", "--exclude-standard",
                          "--directory", "-z"], directory).stdout
    except git.GitError as exc:
        unread.append(f"{directory} (what git ignores): {exc}")
        return out
    sizes = sorted(((p, tree_bytes(directory / p, own, unread))
                    for p in listed.split("\0") if p), key=lambda x: -x[1])
    return {**out, "output": sum(b for _, b in sizes),
            "largest_output": [{"path": p, "bytes": b} for p, b in sizes[:LARGEST] if b]}


def reading(session: "Session") -> dict[str, Any]:
    """What the disk holds now, by holder."""
    paths = session.paths
    fs = os.statvfs(paths.data)
    size, free = fs.f_blocks * fs.f_frsize, fs.f_bavail * fs.f_frsize
    used = size - fs.f_bfree * fs.f_frsize
    seen: set[tuple[int, int]] = set()
    unread: list[str] = []
    holders: dict[str, int] = {}
    reclaimable: dict[str, int] = {}
    try:
        usage = session.runtime.disk_usage()
    except RuntimeError_ as exc:
        unread.append(f"the container runtime: {exc}")
    else:
        holders.update(images=usage.images, build_cache=usage.build_cache,
                       containers=usage.containers)
        reclaimable.update(images=usage.images_unused, build_cache=usage.build_cache_unused)
    for name, root in (("closures", paths.closures), ("agent_archive", paths.agent_archive),
                       ("crash_logs", paths.crashes), ("agent_homes", paths.agent_homes),
                       ("face_home", paths.face_home), ("journal", JOURNAL)):
        holders[name] = tree_bytes(root, seen, unread)
    bodies = {}
    for body in session.catalogue.bodies.values():
        if body.source_root is None:
            continue
        bodies[body.id] = body_reading(body.source_root, seen, unread)
        tab = session.intent.body_tab(body.id)
        bodies[body.id]["tab"] = tab.tab_id if tab is not None else None
        holders[f"body:{body.id}"] = bodies[body.id]["bytes"]
    named = sum(holders.values())
    return {"at": time.time(), "filesystem": {"size": size, "used": used, "free": free},
            "holders": holders, "named": named, "unnamed": used - named,
            "reclaimable": reclaimable, "bodies": bodies, "unread": unread}


def changes(now: dict[str, Any], then: dict[str, Any] | None) -> dict[str, int]:
    """Each holder's change since `then`, the unnamed rest's among them, largest first."""
    if then is None:
        return {}
    before = {**then["holders"], "unnamed": then["unnamed"]}
    after = {**now["holders"], "unnamed": now["unnamed"]}
    moved = {k: after.get(k, 0) - before.get(k, 0) for k in before.keys() | after.keys()}
    return dict(sorted(((k, v) for k, v in moved.items() if v), key=lambda x: -abs(x[1])))


class Disk:
    """Reads the disk every `TICK_SECONDS` and says growth and shortage to the janitor."""

    def __init__(self, session: "Session", events: EventLog) -> None:
        self.session = session
        self.events = events
        self.path = session.paths.disk
        self.budgets = Budgets(session, events)
        self._short = False
        self._over: set[tuple[str, str]] = set()       # (body, kind) said this episode

    def run(self, stop: threading.Event) -> None:
        while not stop.wait(TICK_SECONDS):
            self.tick()

    def _reported(self) -> dict[str, Any] | None:
        return load_json(self.path, "the disk's last reading")

    def tick(self) -> None:
        now = reading(self.session)
        then = self._reported()
        fs = now["filesystem"]
        if then is None or fs["used"] < then["filesystem"]["used"]:
            # Freed space lowers the mark, so growth is always measured from the least held.
            self._report(now)
        elif fs["used"] - then["filesystem"]["used"] >= GROWTH_BYTES:
            self.events.emit("disk.grown", used=fs["used"], free=fs["free"],
                             since=then["at"], changes=changes(now, then),
                             bodies=now["bodies"], reclaimable=now["reclaimable"],
                             unread=now["unread"])
            self._report(now)
        short = fs["free"] < SHORT_FRACTION * fs["size"]
        if short and not self._short:
            self.events.emit("disk.short", used=fs["used"], free=fs["free"], size=fs["size"],
                             changes=changes(now, then), bodies=now["bodies"],
                             reclaimable=now["reclaimable"], unread=now["unread"])
        self._short = short
        for body, read in now["bodies"].items():
            self._hold_to_budget(body, read)

    def _hold_to_budget(self, body: str, read: dict[str, Any]) -> None:
        held = {"caches": read["caches_bytes"], "output": read["output"] or 0}
        if any(held.values()):
            self.budgets.ask_once(body, {k: v for k, v in held.items() if v})
        over = self.budgets.over(body, held)
        for kind in held:
            if kind not in over:
                self._over.discard((body, kind))
            elif (body, kind) not in self._over:
                self._over.add((body, kind))
                budget, holding = over[kind]
                self.budgets.say_over(body, kind, budget, holding, caches=read["caches"],
                                      largest_output=read["largest_output"])

    def _report(self, now: dict[str, Any]) -> None:
        """`now` becomes what the next growth is measured from."""
        save_json(self.path, now)


def accounted(session: "Session") -> dict[str, Any]:
    """The janitor's `disk`: the reading now, and each holder's change since the mark."""
    now = reading(session)
    then = load_json(session.paths.disk, "the disk's last reading")
    return {**now, "since": then["at"] if then else None, "changes": changes(now, then)}
