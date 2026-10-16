"""A tab's long job (`jobs.py`): started under a name, it outlives the call; `job_wait` ends
when it exits, with its code and the end of what it printed, says it gone when its toolbelt
container was recreated under it, and otherwise returns at its deadline with it running; while
it prints, the stall watch reads the tab's work as moving. Past its prediction it is paused
and said as OVERRUN until a defence names a new cause, and three overruns end it; its end is
a row in its body's ledger, handed back to the next start of the same command, and said as it
was first seen. A job ends with its tab, and the tab taking its work over is told how."""
from __future__ import annotations

import signal

import pytest

from raigolmid import jobs, naming
from raigolmid.channel import Channels
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
    assert h.session.jobs.start(tab, instance, "suite", ["pytest", "-q"], 600,
                                  "a guess")["state"] == "running"
    with pytest.raises(JobError, match="still running"):
        h.session.jobs.start(tab, instance, "suite", ["pytest"], 600, "a guess")

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
    h.session.jobs.start(tab, instance, "y50", ["myproject", "--years", "50"], 600, "a guess")
    h.session.repair(instance)
    waited = h.session.jobs.wait(tab, "y50")
    assert waited["state"] == "gone" and "recreated" in waited["why"]


def test_a_job_seen_exited_stays_exited_when_its_toolbelt_is_recreated(h):
    tab, instance = h.tab("myapi"), _instance(h)
    h.session.jobs.start(tab, instance, "suite", ["pytest"], 600, "a guess")
    h.launchers.print_job(instance, 1, "40 passed\n")
    h.launchers.end_job(instance, 1, 0)
    assert h.session.jobs.list(tab)[0]["state"] == "exited"
    h.session.repair(instance)
    waited = h.session.jobs.wait(tab, "suite")
    assert (waited["state"], waited["exit_code"], waited["tail"]) == ("exited", 0,
                                                                     "40 passed\n")


def test_a_job_ends_with_its_tab_and_the_successor_is_told_how(h):
    tab, instance = h.tab("myapi"), _instance(h)
    h.session.jobs.start(tab, instance, "carry", ["sleep", "900"], 1000, "a guess")
    launcher = h.launchers.launcher(instance)
    successor = h.session.succeed_tab(tab, by="self").tab_id
    assert h.runtime.inspect(naming.view(instance)) is None, "its sandbox stopped with it"
    assert h.launchers.jobs[launcher][1]["signals"] == ["end"]
    [told] = h.events_of("job.ended_with_tab")
    assert (told.tab, told.data["closed"]) == (successor, tab)
    assert "'carry' (sleep 900): ended" in told.data["deliver"]["content"]
    assert h.session.jobs.list(successor) == [] and tab not in h.session.jobs._jobs
    [row] = h.session.jobs.similar("myapi", ["sleep", "900"])
    assert row["ended"] == "handed on" and successor in row["why"]


def test_a_job_defended_or_ended_withdraws_its_waiting_overrun_notice(h):
    channels = Channels(h.session, h.events)
    tab, instance = h.tab("myapi"), _instance(h)
    h.session.jobs.start(tab, instance, "build", ["cargo", "build"], 300, "a guess")
    _past_due(h, tab, "build")
    h.session.jobs.tick()

    def overruns():
        return [m for m in channels.state(tab)["queued"] if m["cause"] == "job.overrun"]
    assert overruns()
    h.session.jobs.defend(tab, "build", "a cold cache", "compiling bevy", 600)
    assert not overruns()

    _past_due(h, tab, "build")
    h.session.jobs.tick()
    h.launchers.end_job(instance, 1, 0)
    h.session.jobs.list(tab)
    assert not overruns()
    # Said on the sandbox, so its `history` has how each of its jobs went.
    assert {"job.started", "job.overrun", "job.defended", "job.ended"} <= {
        e.type for e in h.events.history(instance)}


def test_a_stopped_job_has_ended_when_stop_answers(h):
    """`stop` goes through the launcher's `end`, which answers once nothing the job started
    runs (`test_launcher`), so the job's own state already says it."""
    tab, instance = h.tab("myapi"), _instance(h)
    h.session.jobs.start(tab, instance, "serve", ["sh", "-c", "setsid srv &"], 60, "a guess")
    assert h.session.jobs.stop(tab, "serve", "wrong port")["state"] == "stopped"
    assert _signals(h, instance) == ["end"]
    state = h.session.jobs.list(tab)[0]
    assert (state["state"], state["exit_code"], state["stopped"]) == ("exited", 143,
                                                                      "wrong port")


def test_a_job_printing_is_the_tabs_work_moving(h, monkeypatch):
    tab, instance = h.tab("myapi"), _instance(h)
    watch = Stalls(h.session, h.events)
    watch._watches[tab] = Watch(size=1, moved_at=0.0, tree=(), changed_at=0.0)
    monkeypatch.setattr(jobs, "PROGRESS_SECONDS", 0.0)
    h.session.jobs.start(tab, instance, "build", ["cargo", "build"], 600, "a guess")
    h.launchers.print_job(instance, 1, "Compiling a\n")

    def more(_seconds):
        h.launchers.print_job(instance, 1, "Compiling b\n")
    monkeypatch.setattr(jobs.time, "sleep", more)
    h.session.jobs.wait(tab, "build", timeout=0.05)
    for event in h.events.tail(50):
        watch.on_event(event)
    assert watch._watches[tab].changed_at > SPIN_SECONDS, "the spin clock started again"


def _past_due(h, tab, name):
    h.session.jobs._jobs[tab][name]["until"] = 0.0


def _signals(h, instance):
    return h.launchers.jobs[h.launchers.launcher(instance)][1]["signals"]


def test_a_job_past_its_prediction_is_paused_until_a_cause_is_given(h):
    tab, instance = h.tab("myapi"), _instance(h)
    with pytest.raises(JobError, match="expect"):
        h.session.jobs.start(tab, instance, "build", ["cargo", "build"], 0, "a guess")
    h.session.jobs.start(tab, instance, "build", ["cargo", "build"], 300, "last runs 3-6 min")
    h.launchers.print_job(instance, 1, "Compiling bevy\n")
    h.launchers.jobs[h.launchers.launcher(instance)][1]["cpu"] = 290.0
    _past_due(h, tab, "build")

    waited = h.session.jobs.wait(tab, "build")
    assert waited["state"] == "OVERRUN" and _signals(h, instance) == [signal.SIGSTOP]
    assert waited["daemon_reading"] == {"cpu_seconds": 290.0, "bytes_printed": 15}
    assert h.session.jobs.list(tab)[0]["state"] == "OVERRUN"
    [overrun] = h.events_of("job.overrun")
    assert "paused" in overrun.data["deliver"]["content"]

    with pytest.raises(JobError, match="cause"):
        h.session.jobs.defend(tab, "build", "still running", "it prints", 600)
    done = h.session.jobs.defend(tab, "build", "the registry path changed, so bevy rebuilds",
                                 "compiling bevy_render", 900)
    assert done["state"] == "running" and _signals(h, instance)[-1] == signal.SIGCONT

    _past_due(h, tab, "build")
    assert h.session.jobs.wait(tab, "build")["state"] == "OVERRUN"
    with pytest.raises(JobError, match="given before"):
        h.session.jobs.defend(tab, "build", "The registry path changed, so Bevy rebuilds!",
                              "compiling bevy_pbr", 600)
    h.session.jobs.defend(tab, "build", "confirmed: the registry path changed, bevy rebuilds",
                          "bevy_pbr now; target/ grew 2 GB", 600)


def test_a_job_wrong_three_times_is_struck_out(h):
    tab, instance = h.tab("myapi"), _instance(h)
    h.session.jobs.start(tab, instance, "y50", ["myproject", "--years", "50"], 60, "a guess")
    for cause in ("first cause", "second cause"):
        _past_due(h, tab, "y50")
        h.session.jobs.wait(tab, "y50")
        h.session.jobs.defend(tab, "y50", cause, "year 20", 60)
    _past_due(h, tab, "y50")
    h.session.jobs.tick()
    assert _signals(h, instance)[-1] == "end"
    [*_, struck] = h.events_of("job.overrun")
    assert struck.data["struck"] and "not the task" in struck.data["deliver"]["content"]
    with pytest.raises(JobError, match="nothing to defend"):
        h.session.jobs.defend(tab, "y50", "third cause", "year 40", 60)
    waited = h.session.jobs.wait(tab, "y50")
    assert waited["state"] == "exited" and "struck out" in waited["stopped"]


def test_a_jobs_end_is_a_ledger_row_handed_to_the_next_start(h):
    tab, instance = h.tab("myapi"), _instance(h)
    h.session.jobs.start(tab, instance, "build", ["bash", "-lc", "cargo build -p game"], 900,
                         "a guess")
    h.launchers.end_job(instance, 1, 0)
    h.session.jobs.wait(tab, "build")
    h.session.jobs.wait(tab, "build")
    started = h.session.jobs.start(tab, instance, "again", ["cargo", "build"], 60, "the row")
    [row] = started["history"]
    assert (row["expect"], row["ended"], row["exit_code"], row["under"]) == (900, "exited", 0,
                                                                            True)
