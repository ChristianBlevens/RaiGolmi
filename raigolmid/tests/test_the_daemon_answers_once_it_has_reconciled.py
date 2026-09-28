"""The daemon answers only once it has re-derived the machine. Its sockets are served
from the start, so an early caller is accepted rather than refused, and its answer waits for
the startup reconcile: an answer before it reads a machine half-derived, or one that
reconcile is changing under it."""
from __future__ import annotations

import threading
from types import SimpleNamespace

from raigolmid import daemon as daemon_module
from raigolmid.api import ApiServer
from raigolmid.client import ApiClient
from raigolmid.daemon import Daemon
from raigolmid.events import EventLog


def test_a_call_made_during_the_startup_reconcile_is_answered_after_it(tmp_path):
    daemon = object.__new__(Daemon)
    daemon._reconciled = threading.Event()
    daemon.epoch = 1
    daemon.events = EventLog(tmp_path / "events.jsonl")
    daemon.paths = SimpleNamespace(api_socket=tmp_path / "a.sock")
    reconciled = threading.Event()
    daemon.api = ApiServer(tmp_path / "a.sock",
                           {"status": lambda: "after" if reconciled.is_set() else "during"},
                           daemon.events, ready=daemon._reconciled)
    served: list[str] = []
    daemon.face_sockets = SimpleNamespace(start=lambda: served.append("face-sockets"))

    def run_until_stopped(target, name: str) -> None:
        served.append(name)
        if name == "api":
            threading.Thread(target=target, daemon=True).start()

    daemon._run_until_stopped = run_until_stopped
    answers: list[str] = []
    caller = threading.Thread(
        target=lambda: answers.append(ApiClient(tmp_path / "a.sock", timeout=10)
                                      .call("status")), daemon=True)

    def reconcile():
        assert served == ["face-sockets", "api"], "a socket is served only after reconcile"
        caller.start()
        caller.join(0.5)
        assert answers == [], "the daemon answered before it had reconciled"
        reconciled.set()
        return SimpleNamespace(to_dict=dict)

    daemon.session = SimpleNamespace(
        rediscover=lambda: SimpleNamespace(errors=[]), reconcile=reconcile,
        collect_garbage=lambda: None, intent=SimpleNamespace(tabs={}))
    daemon.questions = SimpleNamespace(forget_absent_tabs=lambda tabs: None,
                                       offer_to_judge=lambda: None)
    daemon.channels = SimpleNamespace(messages=SimpleNamespace(
        forget_absent_tabs=lambda tabs: None))
    daemon.coordinator = SimpleNamespace(announce=lambda: None)
    daemon._bring_up_host_surfaces = lambda: None
    daemon.credproxy = SimpleNamespace(serve=None)
    previous = threading.excepthook
    try:
        daemon.start()
        caller.join(10)
    finally:
        threading.excepthook = previous
        if "api" in served:
            daemon.api.shutdown()
        daemon.api.server_close()
    assert answers == ["after"]


def test_runtime_events_are_asked_for_from_the_last_one_handled(monkeypatch):
    """The first subscription asks from before the startup reconcile, and a reconnect from
    the last event handled, so none between is lost; the one at that instant, sent again, is
    not handled twice."""
    daemon = object.__new__(Daemon)
    daemon._stop = threading.Event()
    daemon._runtime_seen = 100
    daemon.events = SimpleNamespace(emit=lambda *a, **k: None)
    monkeypatch.setattr(daemon_module.time, "sleep", lambda _s: None)
    asked: list[int] = []
    handled: list[int] = []

    def events(label_filter, since):
        asked.append(since)
        if len(asked) == 1:
            yield {"timeNano": 150}
            yield {"timeNano": 200}
            raise ConnectionError("the Engine went away")
        yield {"timeNano": 200}
        yield {"timeNano": 250}
        daemon._stop.set()

    daemon.session = SimpleNamespace(runtime=SimpleNamespace(events=events))
    daemon._on_runtime_event = lambda event: handled.append(event["timeNano"])
    daemon._watch_runtime()
    assert asked == [100, 200]
    assert handled == [150, 200, 250]
