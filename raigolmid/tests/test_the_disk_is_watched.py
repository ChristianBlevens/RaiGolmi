"""The disk is read by holder, and its growth and shortage go to the manager (`disk.py`):
growth past the mark is said once with what grew, a body's ignored output named with its
tab; freed space lowers the mark; a shortage is said once per episode; a holder that cannot
be read is reported unread, never as empty."""
from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from raigolmid import disk
from raigolmid.manager import TAKEN
from raigolmid.runtime.base import DiskUsage

from tests.harness import Harness

GB = 1024 ** 3


@pytest.fixture()
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.session.select("body", "myapi")
    harness.runtime.disk = DiskUsage(images=5 * GB, images_unused=GB, build_cache=GB,
                                     build_cache_unused=0, containers=0)
    monkeypatch.setattr(disk, "JOURNAL", tmp_path / "journal")
    return harness


def _filesystem(monkeypatch, used: int, size: int = 60 * GB) -> None:
    monkeypatch.setattr(disk.os, "statvfs", lambda _p: SimpleNamespace(
        f_blocks=size, f_frsize=1, f_bfree=size - used, f_bavail=size - used))


def _body_output(h, mib: int) -> None:
    """A body whose git ignores `target/`, holding `mib` of written build output."""
    root = h.session.catalogue.bodies["myapi"].directory
    if not (root / ".git").exists():
        subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".git" / "info").mkdir(exist_ok=True)
    (root / ".git" / "info" / "exclude").write_text("target/\n")
    (root / "target").mkdir(exist_ok=True)
    with open(root / "target" / f"out{mib}", "wb") as f:
        f.write(os.urandom(mib * 1024 * 1024))


def test_growth_past_the_mark_is_said_once_naming_the_body_and_its_tab(h, monkeypatch):
    assert {"disk.grown", "disk.short"} <= TAKEN
    watch = disk.Disk(h.session, h.events)
    _filesystem(monkeypatch, 20 * GB)
    watch.tick()
    assert "disk.grown" not in h.event_types()

    _body_output(h, 3)
    _filesystem(monkeypatch, 23 * GB)
    watch.tick()
    [grown] = h.events_of("disk.grown")
    body = grown.data["bodies"]["myapi"]
    assert body["tab"] == h.tab("myapi")
    assert body["ignored"] >= 3 * 1024 * 1024
    assert body["largest_ignored"][0]["path"] == "target/"
    assert grown.data["changes"]["body:myapi"] >= 3 * 1024 * 1024

    watch.tick()
    assert len(h.events_of("disk.grown")) == 1


def test_freed_space_lowers_the_mark(h, monkeypatch):
    watch = disk.Disk(h.session, h.events)
    _filesystem(monkeypatch, 20 * GB)
    watch.tick()
    _filesystem(monkeypatch, 15 * GB)
    watch.tick()
    _filesystem(monkeypatch, 18 * GB)
    watch.tick()
    assert len(h.events_of("disk.grown")) == 1


def test_a_shortage_is_said_once_per_episode(h, monkeypatch):
    watch = disk.Disk(h.session, h.events)
    _filesystem(monkeypatch, 55 * GB)
    watch.tick()
    watch.tick()
    _filesystem(monkeypatch, 30 * GB)
    watch.tick()
    _filesystem(monkeypatch, 55 * GB)
    watch.tick()
    assert len(h.events_of("disk.short")) == 2


def test_a_runtime_that_cannot_answer_is_unread_not_empty(h, monkeypatch):
    h.runtime.disk = None
    _filesystem(monkeypatch, 20 * GB)
    reading = disk.accounted(h.session)
    assert "images" not in reading["holders"]
    assert any("container runtime" in u for u in reading["unread"])
