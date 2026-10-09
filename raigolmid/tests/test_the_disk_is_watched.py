"""The disk is read by holder, and its growth and shortage go to the janitor (`disk.py`):
growth past the mark is said once with what grew, a body's ignored output named with its
tab; freed space lowers the mark; a shortage is said once per episode; a holder that cannot
be read is reported unread, never as empty."""
from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from raigolmid import disk
from raigolmid.janitor import TAKEN
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
    (root / "target" / "CACHEDIR.TAG").write_text(
        "Signature: 8a477f597d28d172789f06886806bc55\n"
        "# This file is a cache directory tag created by cargo.\n")
    (root / "target" / "inner").mkdir(exist_ok=True)
    (root / "target" / "inner" / "CACHEDIR.TAG").write_text(
        "Signature: 8a477f597d28d172789f06886806bc55\n")
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
    assert body["caches_bytes"] >= 3 * 1024 * 1024 and body["output"] < 1024 * 1024, \
        "a cache's bytes are its own, not counted again as output"
    assert grown.data["changes"]["body:myapi"] >= 3 * 1024 * 1024
    [cache] = body["caches"]
    assert (cache["path"], cache["tool"]) == ("target", "cargo")
    assert cache["largest"][0] == {"entry": "out3", "bytes": cache["largest"][0]["bytes"]}
    assert cache["largest"][0]["bytes"] >= 3 * 1024 * 1024

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


def _budget(h, text: str) -> None:
    toml = h.session.catalogue.bodies["myapi"].directory / "body.toml"
    toml.write_text(toml.read_text().split("\n[budget]\n")[0] + "\n[budget]\n" + text)
    h.session.rediscover()


def test_a_project_holding_output_with_no_budget_has_its_tab_asked_once(h, monkeypatch):
    watch = disk.Disk(h.session, h.events)
    _filesystem(monkeypatch, 20 * GB)
    _body_output(h, 1)
    watch.tick()
    watch.tick()
    [asked] = h.events_of("budget.unset")
    assert asked.tab == h.tab("myapi") and "[budget]" in asked.data["deliver"]["content"]
    assert disk.Disk(h.session, h.events).tick() is None and len(h.events_of("budget.unset")) == 1, \
        "asked once, not at every start"


def test_a_budget_passed_goes_to_its_tab_to_check_once_until_it_is_kept_again(h, monkeypatch):
    assert "budget.exceeded" not in TAKEN and "budget.untended" in TAKEN
    _budget(h, 'caches = "1M"\noutput = "8G"\n')
    watch = disk.Disk(h.session, h.events)
    _filesystem(monkeypatch, 20 * GB)
    _body_output(h, 2)
    watch.tick()
    watch.tick()
    [over] = h.events_of("budget.exceeded")
    assert (over.data["body"], over.data["kind"]) == ("myapi", "caches")
    assert over.data["caches"][0]["path"] == "target"
    assert over.tab == h.tab("myapi") and "new range" in over.data["deliver"]["content"]
    assert not h.events_of("budget.unset"), "a project with a budget is not asked for one"
    _budget(h, "")      # an empty table keeps nothing, so nothing is past it
    watch.tick()
    _budget(h, 'caches = "1M"\n')
    watch.tick()
    assert len(h.events_of("budget.exceeded")) == 2


def test_a_budget_that_is_not_a_size_is_a_definition_error(h):
    _budget(h, 'memory = "lots"\n')
    assert "myapi" not in h.session.catalogue.bodies


def test_a_project_with_no_tab_past_its_budget_is_the_janitors(h, monkeypatch):
    _budget(h, 'caches = "1M"\n')
    tab = h.tab("myapi")
    h.session.deselect("body")
    h.session.close_tab(tab)
    assert h.session.intent.body_tab("myapi") is None
    _filesystem(monkeypatch, 20 * GB)
    _body_output(h, 2)
    disk.Disk(h.session, h.events).tick()
    assert not h.events_of("budget.exceeded")
    [untended] = h.events_of("budget.untended")
    assert untended.data["body"] == "myapi"


def test_the_ask_says_a_budget_is_a_regular_run_not_a_ceiling():
    from raigolmid.budgets import unset_message
    said = unset_message("myapi", {"caches": GB}, "/definitions/bodies/myapi/body.toml")
    assert "regular run" in said and "not a" in said and "room to spare" in said


def test_growth_across_what_the_machine_built_for_a_chosen_layer_is_the_new_mark(h, monkeypatch):
    """A first face builds gigabytes of image and closure: the work going as it should, so the
    janitor is not sent to it. Growth after it is measured from there."""
    watch = disk.Disk(h.session, h.events)
    _filesystem(monkeypatch, 20 * GB)
    watch.tick()
    h.events.emit("face.trial_started", face="writing")
    _filesystem(monkeypatch, 25 * GB)
    watch.tick()
    assert "disk.grown" not in h.event_types()
    _filesystem(monkeypatch, 28 * GB)
    watch.tick()
    assert len(h.events_of("disk.grown")) == 1
