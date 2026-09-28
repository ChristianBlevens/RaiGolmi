"""The rebuild loop and the controlled swap.

Two simultaneous rebuilds of one working copy produce one build,
and an unchanged definition returns `already_current`.
"""
from __future__ import annotations

import threading
import time

import pytest

from raigolmid import labels, naming

from raigolmid.runtime import RuntimeError_
from raigolmid.session import NotSelectable

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


def _change_dependency(h, text: str = "requests==2.33.0\n") -> None:
    (h.search.bodies[0] / "myapi" / "requirements.txt").write_text(text)
    h.session.rediscover()


def test_an_unchanged_definition_does_not_build(h):
    before = h.runtime.build_count
    report = h.session.rebuild_body(SANDBOX, why="your request")
    assert report.result == "already_current"
    assert h.runtime.build_count == before


def test_the_view_is_torn_down_before_the_body_is_replaced(h):
    """Teardown before replacement. The view holds mounts into the body's filesystem, so
    the other order is what produces 'device or resource busy' and a leaked layer."""
    _change_dependency(h)
    order: list[str] = []
    original_teardown = h.session.views.teardown
    original_up = h.compose.up

    def teardown(instance):
        order.append("view-down")
        return original_teardown(instance)

    def up(project, file, force_recreate=False):
        if force_recreate:
            order.append("body-replaced")
        return original_up(project, file, force_recreate)

    h.session.views.teardown = teardown
    h.compose.up = up
    h.session.rebuild_body(SANDBOX, why="your request")
    assert order[:2] == ["view-down", "body-replaced"]


def test_a_body_the_daemon_replaced_is_not_an_exit(h):
    """`force_recreate` destroys the old container, which emits `die` like any other exit,
    judged on the sandbox's queue behind the swap that caused it: by then a new body is
    running under the name. Recovering there would tear down a working view to replace a
    container that never failed."""
    _change_dependency(h)
    list(h.runtime.events())
    assert h.session.rebuild_body(SANDBOX, why="your request").result == "rebuilt"
    view = h.session.views.get(SANDBOX).id

    h.deliver_runtime_events()

    assert not h.events_of("container.exited")
    assert h.session.views.get(SANDBOX).id == view


def test_a_build_failure_reports_the_log_and_changes_nothing(h):
    _change_dependency(h)
    h.runtime.build_should_fail = True
    before = h.runtime.inspect(naming.body_container(SANDBOX)).id

    report = h.session.rebuild_body(SANDBOX, why="your request")

    assert report.result == "build_failed"
    assert "fake build failure" in report.log
    assert h.runtime.inspect(naming.body_container(SANDBOX)).id == before
    assert h.session.views.get(SANDBOX) is not None


def test_two_copies_rebuilding_at_once_both_build(h):
    """Two bodies' tabs, each with its sandbox open: the build is per working copy."""
    other = h.open_sandbox("webui", "python-dev")
    _change_dependency(h)
    (h.search.bodies[0] / "webui" / "requirements.txt").write_text("requests==2.34.0\n")
    h.session.rediscover()

    before = h.runtime.build_count
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(
            h.session.rebuild_body(SANDBOX, why="your request").result)),
        threading.Thread(target=lambda: results.append(
            h.session.rebuild_body(other, why="your request").result)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)

    assert sorted(results) == ["rebuilt", "rebuilt"], \
        "one copy's build superseded the other's"
    assert h.runtime.build_count - before == 2


def test_two_rebuilds_of_one_change_swap_once(h):
    """Two rebuilds of one change replace the body once.

    An agent edits `requirements.txt` and calls `rebuild_body`; the file watch the same
    edit tripped fires its own rebuild half a second later. Both read the
    instance's digest before the first swap records the new one, so both get past the
    caller's `already_current` check, and both attach to the single build — which is what
    the build lock is for. The second then swaps again over a healthy view: the body is
    force-recreated and the launcher the caller is talking to goes away mid-request.

    One build and one swap. The second request is `already_current`, and it says so.
    """
    _change_dependency(h)
    instance = h.session.instances.get(SANDBOX)
    generation_before = instance.view_generation
    builds_before = h.runtime.build_count

    building = threading.Event()
    release = threading.Event()
    original_build = h.runtime.build

    def blocking_build(*args, **kwargs):
        building.set()
        assert release.wait(60), "the build was never released"
        return original_build(*args, **kwargs)

    h.runtime.build = blocking_build
    results: list[str] = []

    def rebuild() -> None:
        results.append(h.session.rebuild_body(SANDBOX, why="your request").result)

    first = threading.Thread(target=rebuild)
    first.start()
    assert building.wait(60), "the first rebuild never reached the build"
    second = threading.Thread(target=rebuild)
    second.start()

    # Released only once the second request has *attached* to the in-flight build. Any
    # other moment and the second may instead arrive after the first swap recorded the
    # digest, where the caller's own check answers it — and then this test passes without
    # ever reaching the case it exists for.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        with h.session.builds._lock:
            flight = h.session.builds._in_flight.get(
                h.session.intent.instances[SANDBOX].working_copy)
            if flight is not None and flight.waiters >= 1:
                break
        time.sleep(0.01)
    else:
        release.set()
        pytest.fail("the second rebuild never attached to the in-flight build")
    release.set()

    for t in (first, second):
        t.join(60)

    assert sorted(results) == ["already_current", "rebuilt"]
    assert h.runtime.build_count - builds_before == 1
    assert instance.view_generation == generation_before + 1, \
        "the second request swapped again and replaced the view the first had just built"
    assert len(h.events_of("view.restarted")) == 1


def test_a_source_only_change_does_not_move_the_digest(h):
    """Rebuilding for a source change would be the loop the design avoids: source is
    mounted at /work, not copied."""
    digest_before = h.session.catalogue.bodies["myapi"].definition_digest()
    (h.search.bodies[0] / "myapi" / "app.py").write_text("print('hello')\n")
    h.session.rediscover()
    assert h.session.catalogue.bodies["myapi"].definition_digest() == digest_before


def test_a_watched_change_triggers_one_debounced_rebuild(h):
    """The automatic half: the agent edits the definition and the body rebuilds
    without anyone calling rebuild. Compose Watch is deliberately not what does this — it
    would replace the body without the controlled swap."""
    import time as _time

    h.session.debouncer.delay = 0.1
    before = h.runtime.build_count
    _change_dependency(h)

    for _ in range(5):                 # a multi-file save, as the editor produces it
        h.session.on_watched_change(SANDBOX)
        _time.sleep(0.01)

    deadline = _time.time() + 10
    while _time.time() < deadline and not h.events_of("rebuild.finished"):
        _time.sleep(0.05)

    triggered = h.events_of("rebuild.finished")
    assert triggered, "the watched change never reached a rebuild"
    assert triggered[-1].data["report"]["result"] == "rebuilt"
    assert triggered[-1].data["why"] == "a watched file"
    assert h.runtime.build_count - before == 1, \
        "a multi-file save produced more than one build"


def test_a_rebuild_triggered_by_a_watch_reports_failure_rather_than_raising(h):
    """The watcher has no caller to raise into, so a failure has to land in the event log
    where the user and the agent can both read it."""
    import time as _time

    h.session.debouncer.delay = 0.05
    _change_dependency(h)
    h.runtime.build_should_fail = True
    h.session.on_watched_change(SANDBOX)

    deadline = _time.time() + 10
    while _time.time() < deadline and not h.events_of("rebuild.finished"):
        _time.sleep(0.05)

    triggered = h.events_of("rebuild.finished")
    assert triggered and triggered[-1].data["report"]["result"] == "build_failed"


def test_a_body_that_exits_on_its_own_is_recovered_in_place(h):
    """The same sequence runs when a body exits on its own, and
    the teardown must handle a view whose body is already gone without erroring."""
    dead = h.runtime.inspect(naming.body_container(SANDBOX)).id
    list(h.runtime.events())
    h.runtime.kill(naming.body_container(SANDBOX))

    h.deliver_runtime_events()

    body = h.runtime.inspect(naming.body_container(SANDBOX))
    assert body.running and body.id != dead
    assert h.session.views.get(SANDBOX) is not None
    assert h.session.instances.get(SANDBOX).health == "ok"
    assert h.events_of("container.restarted")[-1].data["kind"] == "body"


def test_a_body_restart_that_fails_is_the_managers_and_leaves_the_sandbox_degraded(h):
    list(h.runtime.events())
    h.runtime.kill(naming.body_container(SANDBOX))
    h.compose.fail_next_up = True

    h.deliver_runtime_events()

    [unfixable] = h.events_of("container.unfixable")
    assert (unfixable.instance, unfixable.data["kind"]) == (SANDBOX, "body")
    assert "did not restart" in unfixable.data["message"]
    assert h.session.instances.get(SANDBOX).health == "degraded"


def test_a_sandbox_exit_does_not_take_the_session_lock(h):
    """Nothing that runs on an instance queue may take the session lock: every lock-holding
    caller waits on a queue, so a queue job waiting on the lock is a lock-order inversion.
    This asserts the property directly rather than hoping no future caller introduces the
    second `run()` that would turn it into a deadlock."""
    import threading
    from raigolmid.supervisor import Unit

    holder_has_lock = threading.Event()
    release = threading.Event()

    def hold_lock():
        with h.session._lock:
            holder_has_lock.set()
            release.wait(5)

    threading.Thread(target=hold_lock, daemon=True).start()
    holder_has_lock.wait(5)

    died = h.runtime.inspect(naming.body_container(SANDBOX)).id
    h.runtime.kill(naming.body_container(SANDBOX))
    done = threading.Event()
    threading.Thread(
        target=lambda: (h.session.on_exit(Unit(labels.Role.BODY, SANDBOX), died), done.set()),
        daemon=True).start()

    finished = done.wait(5)
    release.set()
    assert finished, "a body's exit blocked on the session lock"


def test_a_face_switch_leaves_the_view_alone(h):
    """The view is the toolbelt's container, and nothing of the face runs in it."""
    view = h.session.views.get(SANDBOX).id
    generation = h.session.instances.get(SANDBOX).view_generation

    h.session.select("face", "writing")

    assert h.session.views.get(SANDBOX).id == view
    assert h.session.instances.get(SANDBOX).view_generation == generation
    assert "view.torn_down" not in h.event_types()


def test_a_toolbelt_swap_recreates_the_view_and_keeps_the_body(h):
    view = h.session.views.get(SANDBOX).id
    body = h.runtime.inspect(naming.body_container(SANDBOX)).id
    with pytest.raises(NotSelectable, match="needs lsp"):
        h.session.toolbelt_swap(TAB, "no-lsp")
    h.session.deselect("face")
    h.session.toolbelt_swap(TAB, "no-lsp")
    assert h.session.views.get(SANDBOX).id != view
    assert h.session.intent.instances[SANDBOX].toolbelt == "no-lsp"
    assert h.runtime.inspect(naming.body_container(SANDBOX)).id == body
    assert h.session.active_toolbelt() == "no-lsp"


def test_a_toolbelt_swap_takes_no_session_lock_on_the_sandboxs_queue(h, monkeypatch):
    """Reconcile holds the session lock while it waits on each sandbox's queue, so a swap job
    that took the lock on the queue would deadlock it until the queue's timeout."""
    h.session.deselect("face")
    on_queue = threading.local()
    taken: list[str] = []
    submit, lock = h.session.queues.submit, h.session._lock

    def marked(instance, fn, name="operation"):
        def run():
            on_queue.job = name
            try:
                return fn()
            finally:
                on_queue.job = None
        return submit(instance, run, name)

    class Watched:
        def __enter__(self):
            if getattr(on_queue, "job", None):
                taken.append(on_queue.job)
            return lock.__enter__()

        def __exit__(self, *exc):
            return lock.__exit__(*exc)

    monkeypatch.setattr(h.session.queues, "submit", marked)
    monkeypatch.setattr(h.session, "_lock", Watched())
    h.session.toolbelt_swap(TAB, "no-lsp")
    assert h.session.intent.instances[SANDBOX].toolbelt == "no-lsp"
    assert taken == []


def test_a_toolbelt_swap_fetches_the_new_image_while_the_old_view_serves(h, monkeypatch):
    """Build first: the pull comes before the teardown, and a toolbelt that
    cannot be fetched leaves the sandbox on the one it has."""
    h.session.deselect("face")

    def image_of(toolbelt: str) -> str:
        return h.session.resolver.resolve(h.session.catalogue.toolbelts[toolbelt]).image

    if h.runtime.image(image_of("no-lsp")) is not None:
        h.runtime.remove_image(image_of("no-lsp"))
    order: list[str] = []
    pull, teardown = h.runtime.pull, h.session.views.teardown
    monkeypatch.setattr(h.runtime, "pull", lambda ref: (order.append("pull"), pull(ref))[1])
    monkeypatch.setattr(h.session.views, "teardown",
                        lambda i: (order.append("teardown"), teardown(i))[1])
    h.session.toolbelt_swap(TAB, "no-lsp")
    assert order[:2] == ["pull", "teardown"]

    def unreachable(ref):
        raise RuntimeError_(f"could not pull '{ref}': registry unreachable")

    monkeypatch.setattr(h.runtime, "pull", unreachable)
    h.runtime.remove_image(image_of("python-dev"))
    view = h.session.views.get(SANDBOX).id
    with pytest.raises(RuntimeError_, match="registry unreachable"):
        h.session.toolbelt_swap(TAB, "python-dev")
    assert h.session.views.get(SANDBOX).id == view
    assert h.session.intent.instances[SANDBOX].toolbelt == "no-lsp"


def test_a_first_open_fetches_the_toolbelt_while_the_body_builds(tmp_path, monkeypatch):
    """The two share nothing; a body build that waits for the fetch to start would time out
    if they ran one after the other."""
    other = Harness(tmp_path, monkeypatch)
    started = threading.Event()
    fetch, build = other.session.instances.fetch_toolbelt, other.session.instances.build_image
    monkeypatch.setattr(other.session.instances, "fetch_toolbelt",
                        lambda t: (started.set(), fetch(t))[1])

    built: list[str] = []

    def waits_for_the_fetch(body, digest, fresh):
        built.append(body.id)
        assert started.wait(10), "the toolbelt was not being fetched while the body built"
        return build(body, digest, fresh)

    monkeypatch.setattr(other.session.instances, "build_image", waits_for_the_fetch)
    assert other.open_sandbox("myapi", "python-dev") == SANDBOX
    assert built == ["myapi"]


BASE = "python:3.12-slim"


def test_a_base_image_moved_at_the_registry_is_a_new_definition(h):
    """The digest covers the base as the registry names it now, and the build
    fetches the new one."""
    assert h.session.rebuild_body(SANDBOX, why="t").result == "already_current"
    h.runtime.registry[BASE] = "sha256:" + "1" * 64
    assert h.session.rebuild_body(SANDBOX, why="t").result != "already_current"
    assert h.runtime.builds_pulled[-1] is True
    assert h.runtime.image(BASE).id == "sha256:" + "1" * 64


def test_an_unreachable_registry_takes_the_machines_copy_and_says_so(h):
    h.runtime.registry_offline = True
    assert h.session.rebuild_body(SANDBOX, why="t").result == "already_current", \
        "the copy was pulled as the digest the registry gave, so nothing changed"
    assert [e.data["image"] for e in h.events_of("body.base_local")] == [BASE]


def test_a_rebuild_queued_behind_a_toolbelt_swap_keeps_the_swapped_toolbelt(h):
    """The rebuild's swap reads the sandbox in its queue: queued behind a toolbelt swap, it
    must not recreate the view from the toolbelt it read before that swap ran."""
    import threading
    import time

    h.session.deselect("face")
    _change_dependency(h)
    before = h.runtime.spec_of(naming.view(SANDBOX)).image
    queues = h.session.queues
    gate = threading.Event()
    queues.submit(SANDBOX, lambda: gate.wait(10), "held")

    def queued(n: int) -> None:
        deadline = time.monotonic() + 10
        while queues.depth(SANDBOX) < n and time.monotonic() < deadline:
            time.sleep(0.01)
        assert queues.depth(SANDBOX) >= n, "the operation never reached the sandbox's queue"

    swap = threading.Thread(target=lambda: h.session.toolbelt_swap(TAB, "no-lsp"))
    swap.start()
    queued(1)
    reports: list = []
    rebuild = threading.Thread(target=lambda: reports.append(
        h.session.rebuild_body(SANDBOX, why="your request")))
    rebuild.start()
    queued(2)
    gate.set()
    swap.join(20)
    rebuild.join(20)

    assert reports and reports[0].result == "rebuilt"
    assert h.session.instances.get(SANDBOX).toolbelt == "no-lsp"
    after = h.runtime.spec_of(naming.view(SANDBOX)).image
    assert after != before, "the view came back on the toolbelt the swap replaced"


def _declare_ports(h, ports: str) -> None:
    toml = h.search.bodies[0] / "myapi" / "body.toml"
    toml.write_text(toml.read_text().replace("ports = [8000]", f"ports = [{ports}]"))
    h.session.rediscover()


def test_a_port_declared_after_the_sandbox_exists_reaches_the_host_after_the_rebuild(h):
    """The anchor publishes nothing, so it stays: the rebuilt body carries its new
    ports, and the door publishes them for the sandbox the face is on."""
    anchor_before = h.runtime.inspect(naming.anchor(SANDBOX)).id
    _declare_ports(h, "8000, 8080")

    assert h.session.rebuild_body(SANDBOX, why="your request").result == "rebuilt"

    assert h.runtime.inspect(naming.anchor(SANDBOX)).id == anchor_before
    assert h.runtime.spec_of(naming.anchor(SANDBOX)).ports == {}
    assert h.session.instances.get(SANDBOX).ports == (8000, 8080)
    body = h.runtime.inspect(naming.body_container(SANDBOX))
    assert body.running and body.labels[labels.BODY_PORTS] == "8000,8080"
    assert h.runtime.spec_of(naming.door()).ports == {8000: 8000, 8080: 8080}
    assert h.session.views.get(SANDBOX) is not None


def test_the_swapped_body_carries_the_digest_it_runs(h):
    _change_dependency(h)
    report = h.session.rebuild_body(SANDBOX, why="your request")
    started = h.events_of("body.started")[-1]
    assert started.data["digest"] == report.digest
    body = h.runtime.inspect(naming.body_container(SANDBOX))
    assert body.labels[labels.DEFINITION_DIGEST] == report.digest
