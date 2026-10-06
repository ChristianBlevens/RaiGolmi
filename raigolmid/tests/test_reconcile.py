"""Reconciliation, case by case.

The cases: kill raigolmid with a healthy instance and restart — the view
and its processes survive and are re-adopted; kill it mid-rebuild with a stale view —
the stale view is torn down before the old body is removed; leave an unreferenced anchor
behind — it is removed; break a launcher socket — the view is recreated; make a body
unremovable — the instance is marked degraded rather than silently recreated.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from raigolmid import boot, labels, naming
from raigolmid.runtime.base import ContainerSpec

from tests.harness import Harness

# The harness opens the machine tab first, so selecting myapi opens tab-2.
TAB = "tab-2"
SANDBOX = f"myapi@{TAB}"


@pytest.fixture()
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.session.select("face", "backend-focus")
    assert harness.open_sandbox("myapi", "python-dev") == SANDBOX
    return harness


def restart_daemon(h) -> Harness:
    """A raigolmid restart: the process goes, the containers stay. Intent is reloaded from
    disk; runtime facts are re-derived from the runtime, never from a stored inventory."""
    h.session.close()
    from raigolmid.session import Session
    h.session = Session(h.runtime, h.paths, h.search, h.events, epoch=2,
                        compose_cli=h.compose)
    return h


def test_a_healthy_instance_is_adopted_and_nothing_is_restarted(h):
    view_before = h.runtime.inspect(naming.view(SANDBOX)).id
    body_before = h.runtime.inspect(naming.body_container(SANDBOX)).id

    restart_daemon(h)
    report = h.session.reconcile()

    assert SANDBOX in report.adopted
    assert h.runtime.inspect(naming.view(SANDBOX)).id == view_before, \
        "the view was recreated; its language servers would have died"
    assert h.runtime.inspect(naming.body_container(SANDBOX)).id == body_before


def test_an_adopted_sandbox_is_watched_against_what_its_agent_started_with(h):
    """A protected git file changed before the daemon restarts is still said after
    it; a snapshot taken at adoption would take the change as the start."""
    config = Path(h.session.intent.instances[SANDBOX].working_copy) / ".git" / "config"
    config.write_text(config.read_text() + "[core]\n\thooksPath = /tmp/evil\n")
    assert h.session.status()["protected_git_changes"] == {SANDBOX: [str(config)]}

    restart_daemon(h)
    h.session.reconcile()

    assert h.session.status()["protected_git_changes"] == {SANDBOX: [str(config)]}


def test_an_instance_with_no_body_is_adopted_with_its_view(h):
    """`work` is an anchor and a view and nothing else: no body is its healthy
    state, not a stale view over a body that is gone."""
    h.session.close_tab(TAB)
    h.session.deselect("body")
    h.open_sandbox(None, "python-dev")
    view_before = h.runtime.inspect(naming.view(naming.WORK)).id

    restart_daemon(h)
    report = h.session.reconcile()

    assert naming.WORK in report.adopted
    assert h.runtime.inspect(naming.view(naming.WORK)).id == view_before
    assert h.session.instances.all()[naming.WORK].body is None


def test_a_view_over_a_replaced_body_is_torn_down_before_the_body_is_removed(h):
    """Killed mid-rebuild: the new body is in place and the view still points at the old
    one. The view's mounts must be released first, or the old container cannot be removed."""
    old_body = h.runtime.inspect(naming.body_container(SANDBOX))
    h.runtime.remove(naming.body_container(SANDBOX), force=True)
    h.compose.up(naming.compose_project(SANDBOX),
                 h.session.instances.compose_file(SANDBOX))
    new_body = h.runtime.inspect(naming.body_container(SANDBOX))
    assert new_body.id != old_body.id

    restart_daemon(h)
    report = h.session.reconcile()

    stale = h.events_of("reconcile.stale_view")
    assert stale, "the stale view was not recognised"
    assert SANDBOX in report.recreated or SANDBOX in report.adopted
    view = h.runtime.inspect(naming.view(SANDBOX))
    assert view.labels[labels.BODY_CONTAINER] == \
        h.runtime.inspect(naming.body_container(SANDBOX)).id


def test_a_view_whose_launcher_does_not_answer_is_recreated(h):
    before = h.runtime.inspect(naming.view(SANDBOX)).id
    h.launchers.unreachable.add(SANDBOX)

    restart_daemon(h)
    # The replacement answers: the flag is what makes the *old* view unusable, so it is
    # lifted when that view is torn down, which is exactly when the real one goes away.
    original_teardown = h.session.views.teardown

    def teardown(instance):
        h.launchers.unreachable.discard(instance)
        return original_teardown(instance)

    h.session.views.teardown = teardown
    h.session.reconciler.views.teardown = teardown
    h.session.reconcile()

    assert h.events_of("reconcile.unreachable_view"), \
        "a view that cannot be driven must not be adopted"
    after = h.runtime.inspect(naming.view(SANDBOX))
    assert after is None or after.id != before


def test_an_unreferenced_anchor_left_behind_is_removed(h):
    h.runtime.run(ContainerSpec(
        name=naming.anchor("ghost@tab-7"),
        image="registry.k8s.io/pause:3.9",
        labels={labels.MANAGED: "true", labels.ROLE: str(labels.Role.ANCHOR),
                labels.INSTANCE: "ghost@tab-7"},
    ))
    restart_daemon(h)
    report = h.session.reconcile()

    assert naming.anchor("ghost@tab-7") in report.removed
    assert h.runtime.inspect(naming.anchor("ghost@tab-7")) is None


def test_a_one_shot_this_run_started_is_left_to_its_caller_and_an_older_one_swept(h):
    """A reconcile can run while the judge's `claude -p` is still answering; stopping it
    would fail the question it was judging. One from a daemon run that is gone is residue."""
    for name, epoch in (("raigolmid-judge", h.session.epoch), ("stale-judge", 0)):
        h.runtime.run(ContainerSpec(name=name, image="agent", labels={
            labels.MANAGED: "true", labels.ROLE: str(labels.Role.JUDGE),
            labels.EPOCH: str(epoch)}))
    report = h.session.reconcile()

    assert h.runtime.inspect("raigolmid-judge").running
    assert "stale-judge" in report.removed


def test_a_paused_leftover_is_stopped_and_removed(h):
    """Docker refuses to remove a paused container and stops one cleanly, so
    reconciliation stops it first rather than failing on the 409."""
    h.runtime.run(ContainerSpec(
        name=naming.anchor("ghost@tab-7"),
        image="registry.k8s.io/pause:3.9",
        labels={labels.MANAGED: "true", labels.ROLE: str(labels.Role.ANCHOR),
                labels.INSTANCE: "ghost@tab-7"},
    ))
    h.runtime.pause(naming.anchor("ghost@tab-7"))
    restart_daemon(h)
    report = h.session.reconcile()

    assert naming.anchor("ghost@tab-7") in report.removed
    assert h.runtime.inspect(naming.anchor("ghost@tab-7")) is None


def test_a_body_that_cannot_be_removed_marks_the_instance_degraded(h):
    """Nothing is auto-recreated over an unexplained state, and nothing is silently
    discarded. The sandbox's tab is gone, so reconciliation stops it, and its body
    will not be removed."""
    h.session.intent.tabs.pop(TAB)
    h.session.store.save(h.session.intent)
    h.runtime.busy_on_remove.add(naming.body_container(SANDBOX))

    restart_daemon(h)
    report = h.session.reconcile()

    assert SANDBOX in report.degraded
    assert "busy" in report.degraded[SANDBOX].lower()
    assert h.events_of("body.remove_busy"), \
        "the busy removal must be surfaced, not retried silently"


def test_a_body_tabs_sandbox_survives_a_daemon_restart_with_another_body_selected(h):
    """A sandbox runs while its tab holds it, whatever is selected."""
    h.session.select("body", "webui")

    restart_daemon(h)
    report = h.session.reconcile()

    assert h.session.intent.instances[SANDBOX].refs == [naming.tab_ref(TAB)]
    assert SANDBOX in report.adopted


def test_a_sandbox_held_only_by_a_gone_tab_is_stopped_and_forgotten(h):
    """A ref names an open tab or holds nothing. Left in intent, the sandbox would run
    for good. The body's tab that opens in its place is a new one, with a sandbox of its own."""
    h.session.intent.tabs.pop(TAB)
    h.session.store.save(h.session.intent)

    restart_daemon(h)
    h.session.reconcile()

    assert SANDBOX not in h.session.intent.instances
    assert h.runtime.inspect(naming.anchor(SANDBOX)) is None
    assert [e.data["ref"] for e in h.events_of("reconcile.dangling_ref")] == [
        naming.tab_ref(TAB)]
    h.session.ensure_tabs()
    fresh = h.tab("myapi")
    assert fresh != TAB
    h.session.sandbox_open(fresh, "python-dev")
    assert h.session.intent.instances[f"myapi@{fresh}"].refs == [naming.tab_ref(fresh)]


def test_a_sandbox_whose_toolbelt_is_no_longer_defined_is_degraded_not_run_bare(h):
    """A sandbox runs only with its toolbelt. Recreated without one, the body would run
    with no view and be marked ok — success the sandbox had not earned."""
    import shutil
    for c in h.runtime.list():
        if c.labels.get(labels.ROLE) != str(labels.Role.AGENT):
            h.runtime.remove(c.name, force=True)
    shutil.rmtree(h.session.catalogue.toolbelts["python-dev"].directory)

    restart_daemon(h)
    report = h.session.reconcile()

    assert "not defined" in report.degraded[SANDBOX]
    assert h.session.instances.get(SANDBOX).health == "degraded"
    assert h.runtime.inspect(naming.body_container(SANDBOX)) is None, \
        "the body was started with no view"


def test_an_agent_container_whose_tab_is_gone_is_stopped(h):
    # The image is here because something built it: it is published nowhere, so a
    # container running from it could not exist otherwise.
    h.runtime.add_image("raigolmi/claude")
    h.runtime.run(ContainerSpec(
        name=naming.agent("tab-9"), image="raigolmi/claude",
        labels={labels.MANAGED: "true", labels.ROLE: str(labels.Role.AGENT),
                labels.TAB: "tab-9"}))
    restart_daemon(h)
    report = h.session.reconcile()

    assert naming.agent("tab-9") in report.removed
    assert h.events_of("agent.orphaned")


def test_a_tab_whose_agent_is_gone_is_announced_and_reopened(h):
    """A crash found at the daemon's start is said, and the tab comes back."""
    h.runtime.remove(naming.agent(TAB), force=True)

    restart_daemon(h)
    report = h.session.reconcile()

    assert TAB in report.crashed_tabs
    crashed = h.events_of("agent.crashed")
    assert crashed and "crashed" in crashed[-1].data["message"]
    assert h.session.intent.tabs[TAB].status == "running"
    assert h.runtime.inspect(naming.agent(TAB)).running


def test_what_an_agent_last_said_is_read_back_at_the_start_not_the_intent(h):
    """`busy` is the agent's to say (`activity.py`): a turn that ended while the daemon was
    down leaves the tab idle."""
    from raigolmid import activity
    h.session.agent_activity(TAB, busy=True)
    h.session.store.save(h.session.intent)
    activity.record(h.session.agents.home(TAB), busy=False)

    restart_daemon(h)

    assert h.session.intent.tabs[TAB].busy is False


def test_the_one_restart_is_spent_across_a_daemon_restart(h):
    """The budget is the container id, kept on disk (`supervisor.py`): the agent its restart
    started, exiting on its own while the daemon is down, is the janitor's at the next
    start, not reopened — and stays crashed at the one after."""
    list(h.runtime.events())
    h.runtime.kill(naming.agent(TAB), exit_code=1)
    h.deliver_runtime_events()
    assert h.events_of("container.restarted")
    h.runtime.kill(naming.agent(TAB), exit_code=3)

    restart_daemon(h)
    h.session.reconcile()

    assert h.session.intent.tabs[TAB].status == "crashed"
    assert not h.runtime.inspect(naming.agent(TAB)).running
    [unfixable] = h.events_of("container.unfixable")
    assert (unfixable.tab, unfixable.data["exit_code"]) == (TAB, 3)

    restart_daemon(h)
    h.session.reconcile()
    assert len(h.events_of("container.unfixable")) == 1


def _machine_restarted(h, monkeypatch, *, reboot: bool) -> Harness:
    """The daemon stops in one boot; its agents stop with the machine; it starts again in
    another boot — or, with `reboot` False, in the same one, where nothing stopped them."""
    monkeypatch.setattr(boot, "current", lambda: "boot-1")
    h.session.close()
    h.runtime.stop(naming.agent(TAB))
    monkeypatch.setattr(boot, "current", lambda: "boot-2" if reboot else "boot-1")
    from raigolmid.session import Session
    h.session = Session(h.runtime, h.paths, h.search, h.events, epoch=2,
                        compose_cli=h.compose)
    return h


def test_an_agent_the_machine_shut_down_resumes_rather_than_crashing(h, monkeypatch):
    _machine_restarted(h, monkeypatch, reboot=True)

    report = h.session.reconcile()

    assert report.resumable_tabs == [TAB] and TAB not in report.crashed_tabs
    agent = h.runtime.inspect(naming.agent(TAB))
    assert agent is not None and agent.running
    assert h.events_of("agent.restarted")
    assert h.session.intent.stopped is None, "a stop record is evidence for one start only"


def test_an_agent_that_died_within_one_boot_is_still_a_crash(h, monkeypatch):
    _machine_restarted(h, monkeypatch, reboot=False)

    report = h.session.reconcile()

    assert TAB in report.crashed_tabs and not report.resumable_tabs
    assert h.events_of("agent.crashed")[-1].tab == TAB


def test_a_face_that_cannot_show_the_instance_does_not_stop_the_daemon_starting(
        h, monkeypatch):
    """A daemon that does not start leaves nothing to repair the face from. The
    reason is in `status` and the events instead, and it clears when a sync succeeds."""
    from raigolmid.facemounts import FaceMountError

    h.runtime.run(ContainerSpec(name=naming.face("backend-focus"), image="busybox", labels={
        labels.MANAGED: "true", labels.ROLE: str(labels.Role.FACE)},
        environment={"XDG_RUNTIME_DIR": str(h.paths.face_runtime)}))
    restart_daemon(h)

    def refused(*_args):
        raise FaceMountError("facemount sync exited 1: Operation not permitted")

    monkeypatch.setattr(h.session.face_mounts, "_mount", refused)
    h.session.reconcile()
    assert "Operation not permitted" in h.session.status()["face_runtime"]["shown_error"]
    assert h.events_of("face.sync_failed")

    monkeypatch.setattr(h.session.face_mounts, "_mount", lambda *_args: None)
    h.session.reconcile()
    assert h.session.status()["face_runtime"]["shown_error"] is None


def test_an_adopted_sandbox_still_knows_its_bodys_ports(h):
    """Read off the body's label: without it, show_url refuses every port after a restart,
    and the door has nothing to publish."""
    assert h.runtime.inspect(naming.body_container(SANDBOX)).labels[labels.BODY_PORTS] == "8000"
    restart_daemon(h)
    h.session.reconcile()
    assert h.session.instances.get(SANDBOX).ports == (8000,)




def test_the_stop_record_is_saved_before_queued_work_is_waited_on(h):
    """A stop that systemd's timeout cuts short while the queues finish still leaves the
    record the next start judges its agents by."""
    release = threading.Event()
    h.session.queues.submit(SANDBOX, lambda: release.wait(10), "held")
    closing = threading.Thread(target=h.session.close, daemon=True)
    closing.start()
    try:
        recorded = None
        for _ in range(100):
            recorded = json.loads(h.paths.intent.read_text())["stopped"]
            if recorded is not None:
                break
            closing.join(0.05)
        assert recorded is not None and closing.is_alive(), \
            "the stop record waited behind the queues"
        assert TAB in recorded["running_tabs"]
    finally:
        release.set()
        closing.join(10)
