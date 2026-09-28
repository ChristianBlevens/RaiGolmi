"""Which boot of the machine this is: the kernel's id, new on every boot. What tells a
daemon restart (the same boot, containers still running) from a reboot (every container
stopped with the machine)."""
from __future__ import annotations

from pathlib import Path

BOOT_ID = Path("/proc/sys/kernel/random/boot_id")


def current() -> str:
    return BOOT_ID.read_text().strip()
