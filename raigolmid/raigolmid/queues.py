"""Serializing builds and swaps.

Two agents in two tabs, a file watcher, and the user can all ask for the same work at the
same time. The rebuild sequence is only correct when one actor runs it at a time.

Two mechanisms, and they are different on purpose:

* **Per-instance queue.** Every mutating operation on an instance runs single-threaded for
  that instance. Instances are independent, so their queues run in parallel, and reads
  never queue behind a write.
* **Per-definition build lock.** A build is *shared work* — several instances can run the
  same body definition — so it is keyed by definition digest and coalesced rather than
  serialized. A second caller for a digest already building **attaches to that build**
  instead of starting another.

At most one build is in flight and one queued per working copy. A newly queued request
replaces the queued one, since only the newest definition matters. In-flight builds are
never cancelled: they are cheap to let finish and may be what a third caller is waiting on.
"""
from __future__ import annotations

import collections
import dataclasses
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class QueueClosed(Exception):
    pass


@dataclass
class _Job(Generic[T]):
    """A caller that stops waiting withdraws a job that has not started, so nothing runs
    later that nobody asked for then; one already running finishes, because stopping a swap
    part-way leaves a sandbox the design does not account for."""
    fn: Callable[[], T]
    name: str
    result: T | None = None
    error: BaseException | None = None
    done: threading.Event = field(default_factory=threading.Event)
    _state: str = "queued"            # queued | running | withdrawn
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def run(self) -> None:
        with self._lock:
            if self._state == "withdrawn":
                return
            self._state = "running"
        try:
            self.result = self.fn()
        except BaseException as exc:                  # noqa: BLE001 - re-raised at the caller
            self.error = exc
        finally:
            self.done.set()

    def wait(self, timeout: float | None = None) -> T:
        if not self.done.wait(timeout):
            with self._lock:
                if self._state == "queued":
                    self._state = "withdrawn"
                    raise TimeoutError(f"'{self.name}' waited {timeout}s behind other work "
                                       "on its queue and was withdrawn; it will not run")
            if not self.done.is_set():
                raise TimeoutError(f"'{self.name}' is still running after {timeout}s; it "
                                   "will finish, and what was to follow it here will not run")
        if self.error is not None:
            raise self.error
        return self.result       # type: ignore[return-value]


class InstanceQueues:
    """One single-threaded worker per instance while it has work. The worker ends when its
    instance's work runs out, and the next job starts another, so a sandbox stopped or a tab
    closed leaves no queue behind — even when its containers' last exits arrive after it went."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # An instance is here exactly while its worker runs.
        self._pending: dict[str, collections.deque[_Job]] = {}
        self._workers: dict[str, threading.Thread] = {}
        self._closed = False

    def submit(self, instance: str, fn: Callable[[], T], name: str = "operation") -> _Job[T]:
        job: _Job[T] = _Job(fn=fn, name=f"{instance}:{name}")
        with self._lock:
            if self._closed:
                raise QueueClosed(f"the queues are closed; {job.name} will not run")
            jobs = self._pending.get(instance)
            if jobs is None:
                jobs = self._pending[instance] = collections.deque()
                worker = threading.Thread(target=self._work, args=(instance, jobs),
                                          name=f"queue-{instance}", daemon=True)
                self._workers[instance] = worker
                worker.start()
            jobs.append(job)
        return job

    def run(self, instance: str, fn: Callable[[], T], name: str = "operation",
            timeout: float | None = None) -> T:
        return self.submit(instance, fn, name).wait(timeout)

    def _work(self, instance: str, jobs: collections.deque[_Job]) -> None:
        while True:
            with self._lock:
                if not jobs:
                    del self._pending[instance], self._workers[instance]
                    return
                job = jobs.popleft()
            job.run()

    def close_all(self, timeout: float = 5.0) -> None:
        """No new work; what is queued runs, and is waited on together for `timeout`."""
        with self._lock:
            self._closed = True
            workers = list(self._workers.values())
        deadline = time.monotonic() + timeout
        for worker in workers:
            worker.join(max(0.0, deadline - time.monotonic()))

    def depth(self, instance: str) -> int:
        """Jobs waiting behind the one running."""
        with self._lock:
            return len(self._pending.get(instance, ()))

    def depths(self) -> dict[str, int]:
        with self._lock:
            return {k: len(jobs) for k, jobs in self._pending.items()}


# --- builds ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class BuildOutcome:
    digest: str
    succeeded: bool
    image: str
    log: str
    duration: float
    coalesced: bool = False      # this caller attached to a build someone else started


@dataclass
class _Build:
    """One build of one digest, in flight or queued. Every caller for that digest waits on
    its event; a queued one replaced by a newer definition is ended with `superseded_by`
    rather than an outcome, so the waiter is told positively instead of inferring it."""
    digest: str
    event: threading.Event = field(default_factory=threading.Event)
    outcome: BuildOutcome | None = None
    superseded_by: str | None = None
    waiters: int = 0                 # callers attached to it and still waiting


class BuildLock:
    """Keyed by the working copy a body builds from, not by body id: two copies are two
    trees, so neither's build may supersede the other's."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._in_flight: dict[str, _Build] = {}      # working copy -> build
        self._queued: dict[str, tuple[_Build, Callable[[], BuildOutcome]]] = {}

    def state(self) -> dict[str, dict[str, Any]]:
        """What `status()` reports as `builds`."""
        with self._lock:
            keys = set(self._in_flight) | set(self._queued)
            return {key: {"in_flight": (b.digest if (b := self._in_flight.get(key)) else None),
                          "queued": (q[0].digest if (q := self._queued.get(key)) else None),
                          "waiters": b.waiters if b else 0}
                    for key in keys}

    def build(self, key: str, digest: str,
              work: Callable[[], BuildOutcome],
              timeout: float | None = None) -> BuildOutcome:
        """Run `work` for `key`, or attach to whatever is already running for it.

        `key` identifies the tree built (its working copy); `digest` identifies the *content*.
        A caller whose digest matches the in-flight or the queued build attaches to it. A
        caller with a different digest is queued as the follow-on, replacing — and ending
        with `Superseded` — any other queued request. Which of these a caller is, is decided
        under one lock acquisition, so two callers can never both start a build for one key.
        """
        with self._lock:
            in_flight = self._in_flight.get(key)
            if in_flight is None:
                mine = _Build(digest=digest)
                self._in_flight[key] = mine
                waiting = None
            elif in_flight.digest == digest:
                waiting = in_flight
            else:
                queued = self._queued.get(key)
                if queued is not None and queued[0].digest == digest:
                    waiting = queued[0]
                else:
                    if queued is not None:
                        queued[0].superseded_by = digest
                        queued[0].event.set()
                    waiting = _Build(digest=digest)
                    self._queued[key] = (waiting, work)
            if waiting is not None:
                waiting.waiters += 1

        if waiting is None:
            return self._execute(key, mine, work)

        finished = waiting.event.wait(timeout)
        with self._lock:
            waiting.waiters -= 1
        if not finished:
            raise TimeoutError(f"the build for {key} did not finish within {timeout}s")
        if waiting.superseded_by is not None:
            raise Superseded(
                f"the build request for {key} was replaced by a newer definition "
                f"({waiting.superseded_by[:19]}…) before it ran")
        assert waiting.outcome is not None
        return dataclasses.replace(waiting.outcome, coalesced=True)

    def _execute(self, key: str, build: _Build, work: Callable[[], BuildOutcome]) -> BuildOutcome:
        """Runs a build already installed as `key`'s in-flight one. A failure is recorded as
        the outcome every attached caller reads, and re-raised to the caller that ran it."""
        started = time.monotonic()
        try:
            build.outcome = work()
        except BaseException as exc:                  # noqa: BLE001 - re-raised below
            build.outcome = BuildOutcome(digest=build.digest, succeeded=False, image="",
                                         log=f"{type(exc).__name__}: {exc}",
                                         duration=time.monotonic() - started)
            self._finish(key, build)
            raise
        self._finish(key, build)
        return build.outcome

    def _finish(self, key: str, build: _Build) -> None:
        """Wake the build's waiters and install the queued follow-on as in flight in the same
        acquisition, so no caller ever sees the key with neither."""
        with self._lock:
            build.event.set()
            self._in_flight.pop(key, None)
            follow_on = self._queued.pop(key, None)
            if follow_on is not None:
                self._in_flight[key] = follow_on[0]
        if follow_on is None:
            return
        threading.Thread(target=self._run_follow_on, args=(key, *follow_on),
                         name=f"build-{key}", daemon=True).start()

    def _run_follow_on(self, key: str, build: _Build,
                       work: Callable[[], BuildOutcome]) -> None:
        try:
            self._execute(key, build, work)
        except BaseException:                        # noqa: BLE001
            # The failure is the build's outcome, which every caller waiting on it reads;
            # a follow-on has no caller of its own to raise into.
            return


class Superseded(Exception):
    """A queued build was replaced by a newer definition before it ran. Not a failure:
    the newer definition is the one that matters, and the caller is told rather than being
    given a stale success."""


# --- file-watch debouncing ---------------------------------------------------------------

class Debouncer:
    """Changes are coalesced over a short quiet period before a rebuild is requested, so a
    multi-file save produces one build."""

    def __init__(self, delay: float = 0.5) -> None:
        self.delay = delay
        self._lock = threading.Lock()
        self._timers: dict[str, threading.Timer] = {}

    def trigger(self, key: str, fn: Callable[[], Any]) -> None:
        with self._lock:
            existing = self._timers.pop(key, None)
            if existing is not None:
                existing.cancel()
            timer = threading.Timer(self.delay, self._fire, args=(key, fn))
            timer.daemon = True
            self._timers[key] = timer
            timer.start()

    def _fire(self, key: str, fn: Callable[[], Any]) -> None:
        with self._lock:
            self._timers.pop(key, None)
        fn()

    def cancel_all(self) -> None:
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
