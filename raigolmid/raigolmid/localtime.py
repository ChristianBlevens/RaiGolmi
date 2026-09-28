"""The host's timezone in the containers the user reads a clock in: the face, the host's surfaces, the agents and the session views.

The Windows launcher hands the zone in at boot and the host's `/etc/localtime` points at it
(`host/systemd/set-timezone`). A container keeps its image's zone unless told, so each is
given the zone by name in `TZ`, and the host's zone data read-only with `TZDIR` naming it:
glibc and musl read it there whatever their image carries, and Node resolves the name from
its own ICU. A host with no zone of its own passes nothing, and its containers keep UTC as
the host does.
"""
from __future__ import annotations

from pathlib import Path

from .runtime.base import Mount

LOCALTIME = Path("/etc/localtime")
ZONEINFO = Path("/usr/share/zoneinfo")


def zone(localtime: Path = LOCALTIME) -> str | None:
    """The IANA name `/etc/localtime` points at, or None when it names none."""
    if not localtime.is_symlink():
        return None
    target = str(localtime.resolve())
    _, found, name = target.partition(f"{ZONEINFO}/")
    return name if found and name else None


def environment(localtime: Path = LOCALTIME) -> dict[str, str]:
    name = zone(localtime)
    return {} if name is None else {"TZ": name, "TZDIR": str(ZONEINFO)}


def mounts(localtime: Path = LOCALTIME) -> tuple[Mount, ...]:
    if zone(localtime) is None:
        return ()
    return (Mount(source=str(ZONEINFO), target=str(ZONEINFO), read_only=True),)
