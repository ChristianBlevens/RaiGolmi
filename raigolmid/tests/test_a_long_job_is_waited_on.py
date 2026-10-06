"""A tab's long job (`jobs.py`): started under a name, it outlives the call; `job_wait` ends
when it exits, with its code and the end of what it printed, says it gone when its toolbelt
container was recreated under it, and otherwise returns at its deadline with it running; while
it prints, the stall watch reads the tab's work as moving."""
from __future__ import annotations

import pytest

from raigolmid import jobs
from raigolmid.jobs import JobError
from raigolmid.scopes import instance_of
from raigolmid.stalls import SPIN_SECONDS, Stalls, Watch

from tests.harness import Harness


@pytest.fixture()
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.session.select("body", "myapi")
    monkeypatch.setattr(jobs, "POLL_SECONDS", 0.01)
    return harness


def _instance(h) -> str:
    h.open_sandbox("myapi")
    return instance_of(h.session, h.tab("myapi"))


def test_a_job_is_waited_on_until_it_exits_and_says_how(h):
    tab, instance = h.tab("myapi"), _instance(h)
    assert h.session.jobs.start(tab, instance, "suite", ["pytest", "-q"])["state"] == "running"
    with pytest.raises(JobError, match="still running"):
        h.session.jobs.start(tab, instance, "suite", ["pytest"])

    h.launchers.print_job(instance, 1, "collected 40 items\n")
    waited = h.session.jobs.wait(tab, "suite", timeout=0.05)
    assert waited["state"] == "running" and "collected 40" in waited["tail"]

    h.launchers.print_job(instance, 1, "1 failed, 39 passed\n")
    h.launchers.end_job(instance, 1, 1)
    waited = h.session.jobs.wait(tab, "suite")
    assert (waited["state"], waited["exit_code"]) == ("exited", 1)
    assert waited["tail"].endswith("1 failed, 39 passed\n")
    [ended] = h.events_of("job.ended")
    assert ended.data["exit_code"] == 1
    assert h.session.jobs.list(tab)[0]["state"] == "exited"


def test_a_job_whose_toolbelt_was_recreated_is_gone_not_running(h):
    tab, instance = h.tab("myapi"), _instance(h)
    h.session.jobs.start(tab, instance, "y50", ["myproject", "--years", "50"])
    h.session.repair(instance)
    waited = h.session.jobs.wait(tab, "y50")
    assert waited["state"] == "gone" and "recreated" in waited["why"]


def test_a_job_printing_is_the_tabs_work_moving(h, monkeypatch):
    tab, instance = h.tab("myapi"), _instance(h)
    watch = Stalls(h.session, h.events)
    watch._watches[tab] = Watch(size=1, moved_at=0.0, tree=(), changed_at=0.0)
    monkeypatch.setattr(jobs, "PROGRESS_SECONDS", 0.0)
    h.session.jobs.start(tab, instance, "build", ["cargo", "build"])
    h.launchers.print_job(instance, 1, "Compiling a\n")

    def more(_seconds):
        h.launchers.print_job(instance, 1, "Compiling b\n")
    monkeypatch.setattr(jobs.time, "sleep", more)
    h.session.jobs.wait(tab, "build", timeout=0.05)
    for event in h.events.tail(50):
        watch.on_event(event)
    assert watch._watches[tab].changed_at > SPIN_SECONDS, "the spin clock started again"
