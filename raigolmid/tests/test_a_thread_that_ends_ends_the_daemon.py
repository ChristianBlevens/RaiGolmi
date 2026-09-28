"""Every daemon thread runs until it is told to stop. One that ends is a part of the machine
that stopped — its API, its runtime events, a tab's channel — and a daemon that carried on
would answer `status` over it. So it ends the daemon, and systemd restarts it
(`Daemon.run_forever`)."""
from __future__ import annotations

import threading

from raigolmid.daemon import Daemon


def _daemon_with(*targets) -> Daemon:
    daemon = object.__new__(Daemon)
    daemon._died = {}
    daemon._threads = []
    previous = threading.excepthook
    threading.excepthook = daemon._thread_died
    try:
        for name, target in targets:
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            daemon._threads.append(thread)
        for thread in daemon._threads:
            thread.join(0.5)
    finally:
        threading.excepthook = previous
    return daemon


def test_a_thread_that_raised_is_named_with_its_traceback():
    stop = threading.Event()

    def broken() -> None:
        raise KeyError("content")

    daemon = _daemon_with(("channels", broken), ("api", stop.wait))
    dead = daemon.dead_threads()
    stop.set()
    assert list(dead) == ["channels"]
    assert "KeyError: 'content'" in dead["channels"]


def test_the_exit_status_is_a_failure(monkeypatch):
    """`Restart=on-failure` restarts only a non-zero exit."""
    daemon = object.__new__(Daemon)
    stopped = []
    monkeypatch.setattr(daemon, "start", lambda: None, raising=False)
    monkeypatch.setattr(daemon, "stop", lambda: stopped.append(True), raising=False)
    monkeypatch.setattr(daemon, "dead_threads", lambda: {"api": "it returned"}, raising=False)
    emitted = []
    daemon.events = type("E", (), {"emit": lambda _s, t, **d: emitted.append((t, d))})()
    import signal
    previous = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
    try:
        assert daemon.run_forever() == 1
    finally:
        signal.signal(signal.SIGTERM, previous[0])
        signal.signal(signal.SIGINT, previous[1])
    assert stopped == [True]
    assert emitted[0][0] == "raigolmid.thread_died" and emitted[0][1]["threads"] == ["api"]


def test_only_the_threads_that_run_until_stopped_are_supervised():
    """A one-off thread that finishes its job — drawing the host surfaces — is not a dead
    part of the daemon. Supervised, it restarted the daemon every few seconds on the VM."""
    import ast
    import inspect

    from raigolmid import daemon as daemon_module
    tree = ast.parse(inspect.getsource(daemon_module))
    appends = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
               and ast.unparse(n.func) == "self._threads.append"]
    assert len(appends) == 1, "a thread joins `_threads` outside the long-running loop"
