"""The machine's disk, and who holds it.

The disk is one file on the user's machine that grows with what the guest writes and shrinks
when the guest frees it (the ten-minute trim, `host/systemd/fstrim-*.conf`). So the file is as
large as what is live here, and every live byte has a holder: the container images, their build
cache and the containers' own layers, the nix closures, the archives of closed tabs and the
crash logs, the agents' and the user's homes, the journal, and each body's directory — what its
git tracks, and what it ignores (build and run output, which nothing else on the machine sees).
A reading names each holder and sets their total against what the filesystem reports used; the
rest is **unnamed**: the OS's deployments and anything nothing here accounts for, which is
where a leak shows.

The used bytes rising past the last reported reading by `GROWTH_BYTES` is said as
`disk.grown`, and free space under `SHORT_FRACTION` of the filesystem as `disk.short`, once
per episode; the manager takes both (`manager.py`) with each holder's change since that
reading. What is the machine's the manager repairs; what a body holds is the project's to
judge, so the manager `tell`s that body's tab what it holds and never deletes it from here.

A holder that cannot be read is reported as unread, never as empty. The last reported reading
is kept in `Paths.disk`, and lowered whenever less is held, so growth across a daemon restart
or a boot is measured from the least the disk has held since it was said.
"""
from __future__ import annotations

import json
import os
import stat
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import git
from .events import EventLog
from .runtime.base import RuntimeError_

if TYPE_CHECKING:
    from .session import Session

# How far the used bytes rise past the last reported reading, and how little may be free,
# before the manager looks. Both set how soon disk use is looked at, never whether.
GROWTH_BYTES = 2 * 1024 ** 3
SHORT_FRACTION = 0.15
# Sizing walks every file a body holds, so it is read this often rather than continuously.
TICK_SECONDS = 600.0
# The system journal, bounded by `host/systemd/journald-raigolmi.conf`.
JOURNAL = Path("/var/log/journal")
# How many of a body's largest ignored paths a reading names.
LARGEST = 8


def tree_bytes(root: Path, seen: set[tuple[int, int]], unread: list[str]) -> int:
    """The bytes allocated under `root`, each inode once across one reading (`seen`), on
    `root`'s own filesystem, never following a link. A directory that cannot be read is
    added to `unread`."""
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
    return total


def body_reading(directory: Path, seen: set[tuple[int, int]],
                 unread: list[str]) -> dict[str, Any]:
    """A body's directory: everything, what git ignores in it, and the largest of those."""
    total = tree_bytes(directory, seen, unread)
    if not git.is_repo(directory):
        return {"bytes": total, "ignored": None, "largest_ignored": []}
    try:
        listed = git.run(["ls-files", "--others", "--ignored", "--exclude-standard",
                          "--directory", "-z"], directory).stdout
    except git.GitError as exc:
        unread.append(f"{directory} (what git ignores): {exc}")
        return {"bytes": total, "ignored": None, "largest_ignored": []}
    # Sized again with a reading of its own: `seen` already holds every inode under it.
    own: set[tuple[int, int]] = set()
    sizes = sorted(((p, tree_bytes(directory / p, own, unread))
                    for p in listed.split("\0") if p), key=lambda x: -x[1])
    return {"bytes": total, "ignored": sum(b for _, b in sizes),
            "largest_ignored": [{"path": p, "bytes": b} for p, b in sizes[:LARGEST]]}


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
        if body.directory is None:
            continue
        bodies[body.id] = body_reading(body.directory, seen, unread)
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
    """Reads the disk every `TICK_SECONDS` and says growth and shortage to the manager."""

    def __init__(self, session: "Session", events: EventLog) -> None:
        self.session = session
        self.events = events
        self.path = session.paths.disk
        self._short = False

    def run(self, stop: threading.Event) -> None:
        while not stop.wait(TICK_SECONDS):
            self.tick()

    def _reported(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return None

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

    def _report(self, now: dict[str, Any]) -> None:
        """`now` becomes what the next growth is measured from."""
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(now))
        tmp.replace(self.path)


def accounted(session: "Session") -> dict[str, Any]:
    """The manager's `disk`: the reading now, and each holder's change since the mark."""
    now = reading(session)
    try:
        then = json.loads(session.paths.disk.read_text())
    except FileNotFoundError:
        then = None
    return {**now, "since": then["at"] if then else None, "changes": changes(now, then)}
