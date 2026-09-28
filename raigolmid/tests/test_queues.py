"""Concurrency.

Specifically: two simultaneous rebuild calls for the same
definition produce one build, and a call for an unchanged definition returns
`already_current`. The second belongs to the session; the first lives here, along with
the ordering properties the controlled swap depends on.
"""
from __future__ import annotations

import threading
import time

import pytest

from raigolmid.queues import (
    BuildLock,
    BuildOutcome,
    Debouncer,
    InstanceQueues,
    Superseded,
)


def _outcome(digest: str, duration: float = 0.0) -> BuildOutcome:
    return BuildOutcome(digest=digest, succeeded=True, image=f"img:{digest[:8]}",
                        log="", duration=duration)


# --- per-instance queues ---------------------------------------------------------------

def test_operations_on_one_instance_never_overlap():
    q = InstanceQueues()
    concurrent = []
    running = threading.Lock()

    def work(n: int) -> int:
        acquired = running.acquire(blocking=False)
        concurrent.append(acquired)
        time.sleep(0.02)
        if acquired:
            running.release()
        return n

    jobs = [q.submit("myapi@tab-2", lambda n=n: work(n), f"op-{n}") for n in range(8)]
    results = [j.wait(5) for j in jobs]
    q.close_all()
    assert results == list(range(8))
    assert all(concurrent), "two operations on one instance ran at the same time"


def test_a_failing_operation_raises_at_the_caller_and_does_not_stop_the_queue():
    q = InstanceQueues()

    def boom() -> None:
        raise ValueError("definition is wrong")

    job = q.submit("myapi@tab-2", boom, "bad")
    with pytest.raises(ValueError, match="definition is wrong"):
        job.wait(5)
    assert q.run("myapi@tab-2", lambda: "still working", "after") == "still working"
    q.close_all()


def test_instances_are_independent_and_run_in_parallel():
    queues = InstanceQueues()
    started = threading.Barrier(2, timeout=5)

    def work() -> str:
        started.wait()          # only passes if both instances run at once
        return "ok"

    a = queues.submit("a@tab-2", work, "a")
    b = queues.submit("b@tab-3", work, "b")
    assert a.wait(5) == "ok"
    assert b.wait(5) == "ok"
    queues.close_all()


# --- the build lock ----------------------------------------------------------------------

def test_two_simultaneous_builds_of_one_definition_produce_one_build():
    lock = BuildLock()
    builds = []
    gate = threading.Event()

    def work() -> BuildOutcome:
        builds.append(1)
        gate.wait(5)
        return _outcome("sha256:aaa")

    results: list[BuildOutcome] = []

    def caller() -> None:
        results.append(lock.build("myapi", "sha256:aaa", work, timeout=10))

    threads = [threading.Thread(target=caller) for _ in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    gate.set()
    for t in threads:
        t.join(10)

    assert len(builds) == 1, "the same definition was built more than once"
    assert len(results) == 4
    assert sum(1 for r in results if r.coalesced) == 3


def test_a_second_digest_is_queued_as_a_follow_on_and_the_first_is_not_cancelled():
    lock = BuildLock()
    order: list[str] = []
    first_started = threading.Event()
    release_first = threading.Event()

    def first() -> BuildOutcome:
        order.append("first-start")
        first_started.set()
        release_first.wait(5)
        order.append("first-done")
        return _outcome("sha256:aaa")

    def second() -> BuildOutcome:
        order.append("second-start")
        return _outcome("sha256:bbb")

    t = threading.Thread(target=lambda: lock.build("myapi", "sha256:aaa", first, 10))
    t.start()
    first_started.wait(5)

    follow = threading.Thread(target=lambda: _swallow(lock, "myapi", "sha256:bbb", second))
    follow.start()
    time.sleep(0.1)
    assert order == ["first-start"], "the in-flight build was cancelled or preempted"

    release_first.set()
    t.join(10)
    follow.join(10)
    time.sleep(0.3)
    assert order == ["first-start", "first-done", "second-start"]


def _swallow(lock: BuildLock, key: str, digest: str, work) -> None:
    try:
        lock.build(key, digest, work, timeout=10)
    except Superseded:
        pass


def test_a_newly_queued_request_replaces_the_queued_one():
    lock = BuildLock()
    ran: list[str] = []
    started = threading.Event()
    release = threading.Event()

    def first() -> BuildOutcome:
        started.set()
        release.wait(5)
        return _outcome("sha256:aaa")

    def make(tag: str):
        def work() -> BuildOutcome:
            ran.append(tag)
            return _outcome(f"sha256:{tag}")
        return work

    t = threading.Thread(target=lambda: lock.build("myapi", "sha256:aaa", first, 10))
    t.start()
    started.wait(5)

    for tag in ("bbb", "ccc", "ddd"):
        threading.Thread(target=lambda tag=tag: _swallow(
            lock, "myapi", f"sha256:{tag}", make(tag))).start()
        time.sleep(0.05)

    release.set()
    t.join(10)
    time.sleep(0.5)
    assert ran == ["ddd"], f"expected only the newest queued definition to build, got {ran}"


def test_a_build_failure_reaches_every_waiter():
    lock = BuildLock()
    gate = threading.Event()

    def work() -> BuildOutcome:
        gate.wait(5)
        raise RuntimeError("dockerfile is broken")

    errors: list[BaseException] = []

    def caller() -> None:
        try:
            lock.build("myapi", "sha256:aaa", work, timeout=10)
        except BaseException as exc:      # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=caller) for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    gate.set()
    for t in threads:
        t.join(10)
    # The caller that ran the build re-raises; those attached to it get the failed outcome
    # rather than an exception, because the build did complete — unsuccessfully.
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)


def test_different_definitions_build_at_the_same_time():
    lock = BuildLock()
    together = threading.Barrier(2, timeout=5)

    def work(digest: str):
        def run() -> BuildOutcome:
            together.wait()
            return _outcome(digest)
        return run

    threads = [
        threading.Thread(target=lambda: lock.build("a", "sha256:a", work("sha256:a"), 10)),
        threading.Thread(target=lambda: lock.build("b", "sha256:b", work("sha256:b"), 10)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not together.broken, "two different definitions were serialized against each other"


# --- debouncing ---------------------------------------------------------------------------

def test_a_multi_file_save_produces_one_rebuild_request():
    fired: list[int] = []
    d = Debouncer(delay=0.1)
    for _ in range(6):
        d.trigger("myapi", lambda: fired.append(1))
        time.sleep(0.01)
    time.sleep(0.4)
    assert fired == [1]


def test_a_queued_request_whose_build_runs_gets_its_outcome_not_superseded():
    """The follow-on is installed as in flight in the same acquisition that retires the
    first build, so its caller never sees the key with neither and mistakes that for
    having been replaced — however fast the follow-on finishes."""
    for _ in range(20):
        lock = BuildLock()
        started, release = threading.Event(), threading.Event()

        def first() -> BuildOutcome:
            started.set()
            release.wait(5)
            return _outcome("sha256:aaa")

        t = threading.Thread(target=lambda: lock.build("myapi", "sha256:aaa", first, 10))
        t.start()
        started.wait(5)
        got: list[object] = []

        def queued() -> None:
            try:
                got.append(lock.build("myapi", "sha256:bbb",
                                      lambda: _outcome("sha256:bbb"), 10))
            except Superseded as exc:
                got.append(exc)

        q = threading.Thread(target=queued)
        q.start()
        while lock.state()["myapi"]["queued"] is None:
            time.sleep(0.001)
        release.set()
        t.join(10)
        q.join(10)
        assert len(got) == 1 and isinstance(got[0], BuildOutcome), got
        assert got[0].digest == "sha256:bbb"


def test_a_replaced_queued_request_is_told_it_was_superseded():
    lock = BuildLock()
    started, release = threading.Event(), threading.Event()

    def first() -> BuildOutcome:
        started.set()
        release.wait(5)
        return _outcome("sha256:aaa")

    t = threading.Thread(target=lambda: lock.build("myapi", "sha256:aaa", first, 10))
    t.start()
    started.wait(5)
    got: list[object] = []

    def queued() -> None:
        try:
            got.append(lock.build("myapi", "sha256:bbb", lambda: _outcome("sha256:bbb"), 10))
        except Superseded as exc:
            got.append(exc)

    q = threading.Thread(target=queued)
    q.start()
    while lock.state()["myapi"]["queued"] is None:
        time.sleep(0.001)
    newest = threading.Thread(target=lambda: lock.build(
        "myapi", "sha256:ccc", lambda: _outcome("sha256:ccc"), 10))
    newest.start()
    q.join(5)
    assert len(got) == 1 and isinstance(got[0], Superseded), \
        "the replaced request waited for the first build instead of being told at once"
    release.set()
    t.join(10)
    newest.join(10)


def test_two_digests_arriving_together_never_build_at_once():
    for _ in range(20):
        lock = BuildLock()
        running = 0
        overlap: list[bool] = []
        guard = threading.Lock()
        go = threading.Barrier(2, timeout=5)

        def work(digest: str):
            def run() -> BuildOutcome:
                nonlocal running
                with guard:
                    running += 1
                    overlap.append(running > 1)
                time.sleep(0.02)
                with guard:
                    running -= 1
                return _outcome(digest)
            return run

        def caller(digest: str) -> None:
            go.wait()
            try:
                lock.build("myapi", digest, work(digest), 10)
            except Superseded:
                pass

        threads = [threading.Thread(target=caller, args=(d,))
                   for d in ("sha256:aaa", "sha256:bbb")]
        for th in threads:
            th.start()
        for th in threads:
            th.join(10)
        assert not any(overlap), "two builds of one working copy ran at the same time"


def test_a_job_whose_caller_gave_up_before_it_started_never_runs():
    """A swap that timed out behind other work would otherwise run later, with nobody waiting
    and what was to follow it skipped."""
    q = InstanceQueues()
    release, ran = threading.Event(), []
    q.submit("x@tab-1", release.wait)
    with pytest.raises(TimeoutError, match="withdrawn"):
        q.run("x@tab-1", lambda: ran.append("late"), "swap", timeout=0.1)
    release.set()
    q.run("x@tab-1", lambda: None, "after", timeout=5)
    assert ran == []
    q.close_all()
