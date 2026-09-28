"""Closed tabs' archives and crash evidence share one budget, the oldest going first."""
from __future__ import annotations

import os

import pytest

from raigolmid import keep
from tests.harness import Harness


@pytest.fixture
def h(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


def _archive(h, name: str, size: int, at: float) -> None:
    home = h.paths.agent_archive / name
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "t.jsonl").write_bytes(b"x" * size)
    record = h.paths.agent_archive / f"{name}.json"
    record.write_text("{}")
    os.utime(record, (at, at))


def _crash_log(h, name: str, size: int, at: float) -> None:
    log = h.paths.crashes / name
    log.write_bytes(b"x" * size)
    os.utime(log, (at, at))


def test_the_oldest_go_first_whichever_kind_they_are(h):
    _archive(h, "tab-1-a", 1000, 1)
    _crash_log(h, "tab-2-b.log", 1000, 2)
    _archive(h, "tab-3-c", 1000, 3)
    assert keep.prune(h.paths, h.events, budget=1500) == ["tab-1-a", "tab-2-b.log"]
    assert not (h.paths.agent_archive / "tab-1-a").exists()
    assert not (h.paths.agent_archive / "tab-1-a.json").exists()
    assert (h.paths.agent_archive / "tab-3-c").is_dir()
    (pruned,) = h.events_of("kept.pruned")
    assert (pruned.data["archives"], pruned.data["crash_logs"]) == (["tab-1-a"],
                                                                     ["tab-2-b.log"])


def test_the_newest_is_kept_however_large(h):
    _archive(h, "tab-1-a", 5000, 1)
    assert keep.prune(h.paths, h.events, budget=10) == []
    assert (h.paths.agent_archive / "tab-1-a").is_dir()


def test_a_home_with_a_read_only_directory_is_removed_whole(h):
    _archive(h, "tab-1-a", 1000, 1)
    _archive(h, "tab-2-b", 10, 2)
    locked = h.paths.agent_archive / "tab-1-a" / ".claude"
    locked.chmod(0o500)
    keep.prune(h.paths, h.events, budget=100)
    assert not (h.paths.agent_archive / "tab-1-a").exists()
