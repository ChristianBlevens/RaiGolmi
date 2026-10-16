"""A project's peak working memory is held to the budget its tab set, a process killed for
want of memory and a machine short of it are said (`memory.py`); a tab past its context budget
is said once, never stopped (`contextwatch.py`). All are the janitor's."""
from __future__ import annotations

import json

import pytest

from raigolmid import labels, memory
from raigolmid.contextwatch import ContextWatch
from raigolmid.janitor import TAKEN
from raigolmid.runtime.base import CgroupUse

from tests.harness import Harness, converse

GB = 1024 ** 3


@pytest.fixture()
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.session.select("body", "myapi")
    harness.open_sandbox("myapi")
    monkeypatch.setattr(memory, "machine", lambda: {"total": 8 * GB, "available": 4 * GB})
    return harness


def _view(h) -> str:
    return next(c.name for c in h.runtime.list(labels.managed_filter())
                if c.labels.get(labels.ROLE) == labels.Role.VIEW)


def _window(watch) -> None:
    for _ in range(memory.WINDOW_SAMPLES):
        watch.sample()


def test_a_peak_past_the_memory_budget_is_said_once_per_episode(h):
    assert {"memory.oom_killed", "memory.short", "tab.over_budget"} <= TAKEN
    toml = h.session.catalogue.bodies["myapi"].directory / "body.toml"
    toml.write_text(toml.read_text() + '\n[budget]\nmemory = "2G"\n')
    h.session.rediscover()
    watch = memory.Memory(h.session, h.events)
    view = _view(h)
    h.runtime.cgroup_use[view] = CgroupUse(working=GB, oom_kills=0, cpu_usec=0)
    _window(watch)
    h.runtime.cgroup_use[view] = CgroupUse(working=3 * GB, oom_kills=0, cpu_usec=0)
    _window(watch)
    _window(watch)
    [over] = h.events_of("budget.exceeded")
    assert (over.data["kind"], over.data["held"]) == ("memory", 3 * GB)
    assert watch.accounted()["peaks"]["myapi"] == 3 * GB
    h.runtime.cgroup_use[view] = CgroupUse(working=GB, oom_kills=0, cpu_usec=0)
    _window(watch)
    h.runtime.cgroup_use[view] = CgroupUse(working=3 * GB, oom_kills=0, cpu_usec=0)
    _window(watch)
    assert len(h.events_of("budget.exceeded")) == 2


def test_a_kill_for_want_of_memory_and_a_short_machine_are_said(h, monkeypatch):
    watch = memory.Memory(h.session, h.events)
    view = _view(h)
    h.runtime.cgroup_use[view] = CgroupUse(working=GB, oom_kills=2, cpu_usec=0)
    watch.sample()
    assert not h.events_of("memory.oom_killed"), "kills before the daemon looked are not news"
    h.runtime.cgroup_use[view] = CgroupUse(working=GB, oom_kills=3, cpu_usec=0)
    watch.sample()
    [killed] = h.events_of("memory.oom_killed")
    assert (killed.data["body"], killed.data["kills"]) == ("myapi", 1)
    monkeypatch.setattr(memory, "machine", lambda: {"total": 8 * GB, "available": GB // 2})
    watch.sample()
    watch.sample()
    assert len(h.events_of("memory.short")) == 1


def _past_budget(h, tab: str) -> None:
    home = h.session.agents.home(tab)
    converse(home)
    with (home / ".claude" / "projects" / "-work" / "s1.jsonl").open("a") as out:
        out.write(json.dumps({"type": "assistant", "message": {"content": [], "usage": {
            "input_tokens": 10, "cache_read_input_tokens": 500_000}},
            "entrypoint": "cli", "sessionId": "s1", "cwd": "/work"}) + "\n")


def test_a_conversation_past_the_context_budget_is_said_once(h):
    tab = h.tab("myapi")
    _past_budget(h, tab)
    watch = ContextWatch(h.session, h.events)
    watch.tick()
    watch.tick()
    [over] = h.events_of("tab.over_budget")
    assert over.tab == tab and over.data["tokens"] == 500_010
    assert h.session.intent.tabs[tab].status == "running", "never stopped"


def test_a_hands_off_tab_past_its_budget_is_left_to_the_coordinator(h):
    """The janitor's word would end a managed tab before the machine tab reviewed its docs."""
    tab = h.tab("myapi")
    h.session.manage(tab, True)
    _past_budget(h, tab)
    ContextWatch(h.session, h.events).tick()
    assert h.events_of("tab.over_budget") == []


def test_a_projects_cpu_is_the_whole_machine_unless_its_tab_says_less(h, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(memory.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(memory.os, "cpu_count", lambda: 4)
    watch = memory.Memory(h.session, h.events)
    view, used = _view(h), [0]

    def busy(cores: int) -> None:
        for _ in range(memory.WINDOW_SAMPLES):
            clock[0] += memory.SAMPLE_SECONDS
            used[0] += int(cores * memory.SAMPLE_SECONDS * 1e6)
            h.runtime.cgroup_use[view] = CgroupUse(working=GB, oom_kills=0, cpu_usec=used[0])
            watch.sample()

    busy(4)
    busy(4)
    assert watch.accounted()["cpu"]["myapi"] == 100
    assert watch.accounted()["cpu_budgets"]["myapi"] == 100
    assert not h.events_of("budget.exceeded"), "the whole machine is never passed"
    toml = h.session.catalogue.bodies["myapi"].directory / "body.toml"
    toml.write_text(toml.read_text() + '\n[budget]\ncpu = "25%"\n')
    h.session.rediscover()
    busy(2)
    [over] = h.events_of("budget.exceeded")
    assert (over.data["kind"], over.data["budget"], over.data["held"]) == ("cpu", 25, 50)
    assert "50% over a minute" in over.data["deliver"]["content"]
