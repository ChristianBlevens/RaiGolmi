"""A project's peak working memory is held to the budget its tab set, a process killed for
want of memory and a machine short of it are said (`memory.py`); a tab past its context budget
is said once, never stopped (`contextwatch.py`). All are the janitor's."""
from __future__ import annotations

import json

import pytest

from raigolmid import labels, memory, naming
from raigolmid.contextwatch import ContextWatch
from raigolmid.janitor import TAKEN
from raigolmid.runtime.base import MemoryUse

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
    h.runtime.memory_use[view] = MemoryUse(working=GB, oom_kills=0)
    _window(watch)
    h.runtime.memory_use[view] = MemoryUse(working=3 * GB, oom_kills=0)
    _window(watch)
    _window(watch)
    [over] = h.events_of("budget.exceeded")
    assert (over.data["kind"], over.data["held"]) == ("memory", 3 * GB)
    assert watch.accounted()["peaks"]["myapi"] == 3 * GB
    h.runtime.memory_use[view] = MemoryUse(working=GB, oom_kills=0)
    _window(watch)
    h.runtime.memory_use[view] = MemoryUse(working=3 * GB, oom_kills=0)
    _window(watch)
    assert len(h.events_of("budget.exceeded")) == 2


def test_a_kill_for_want_of_memory_and_a_short_machine_are_said(h, monkeypatch):
    watch = memory.Memory(h.session, h.events)
    view = _view(h)
    h.runtime.memory_use[view] = MemoryUse(working=GB, oom_kills=2)
    watch.sample()
    assert not h.events_of("memory.oom_killed"), "kills before the daemon looked are not news"
    h.runtime.memory_use[view] = MemoryUse(working=GB, oom_kills=3)
    watch.sample()
    [killed] = h.events_of("memory.oom_killed")
    assert (killed.data["body"], killed.data["kills"]) == ("myapi", 1)
    monkeypatch.setattr(memory, "machine", lambda: {"total": 8 * GB, "available": GB // 2})
    watch.sample()
    watch.sample()
    assert len(h.events_of("memory.short")) == 1


def test_a_conversation_past_the_context_budget_is_said_once(h):
    tab = h.tab("myapi")
    home = h.session.agents.home(tab)
    converse(home)
    with (home / ".claude" / "projects" / "-work" / "s1.jsonl").open("a") as out:
        out.write(json.dumps({"type": "assistant", "message": {"content": [], "usage": {
            "input_tokens": 10, "cache_read_input_tokens": 500_000}},
            "entrypoint": "cli", "sessionId": "s1", "cwd": "/work"}) + "\n")
    watch = ContextWatch(h.session, h.events)
    watch.tick()
    watch.tick()
    [over] = h.events_of("tab.over_budget")
    assert over.tab == tab and over.data["tokens"] == 500_010
    assert h.session.intent.tabs[tab].status == "running", "never stopped"
