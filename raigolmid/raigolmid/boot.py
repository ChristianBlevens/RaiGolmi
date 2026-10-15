"""Which boot of the machine this is: the kernel's id, new on every boot. What tells a
daemon restart (the same boot, containers still running) from a reboot (every container
stopped with the machine)."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
STAT = Path("/proc/stat")


def current() -> str:
    return BOOT_ID.read_text().strip()


def started() -> datetime:
    """When this boot began: the kernel's `btime`, seconds since the epoch."""
    for line in STAT.read_text().splitlines():
        if line.startswith("btime "):
            return datetime.fromtimestamp(int(line.split()[1]), UTC)
    raise RuntimeError(f"{STAT} has no btime line")


def before_this_boot(started_at: str | None) -> bool:
    """Whether a container's last start (Docker's `State.StartedAt`) was in an earlier boot.
    A container never started carries Docker's zero time, which is no start at all."""
    if not started_at or started_at.startswith("0001-"):
        return False
    return datetime.fromisoformat(started_at) < started()
