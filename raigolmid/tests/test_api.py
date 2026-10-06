"""The Unix socket API, over a real socket.

Not mocked: an `ApiServer` on a real socket in a temp dir, driven by the real `ApiClient`.
This is the path `rai`, both selectors and the MCP proxy all take, so a break here breaks
every surface at once.
"""
from __future__ import annotations

import threading
import time

import pytest

from raigolmid.channel import Channels
from raigolmid.questions import Questions
from raigolmid.history import History
from raigolmid.viewing import Viewing
from raigolmid.api import ApiServer, build_methods
from raigolmid.client import ApiClient, ApiError

from tests.harness import Harness, answering


@pytest.fixture()
def api(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    server = ApiServer(h.paths.api_socket,
                       build_methods(h.session, h.events, Questions(h.events, h.paths),
                  Channels(h.session, h.events), Viewing(h.events, h.paths.viewing), History(h.events, h.paths)), h.events,
                       ready=answering())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = ApiClient(h.paths.api_socket, timeout=30)
    try:
        yield client, h
    finally:
        server.shutdown()
        server.server_close()


def test_a_refused_selection_comes_back_as_a_reason_not_a_crash(api):
    client, _ = api
    with pytest.raises(ApiError, match="sandbox_open"):
        client.call("select", kind="toolbelt", id="python-dev")
    assert client.call("status")["session"]["face"] is None


def test_reads_do_not_queue_behind_a_running_mutation(api):
    """`status` must answer while a build is in flight, which is what lets a user
    watch a rebuild happen rather than stare at a frozen selector."""
    client, h = api
    client.call("select", kind="body", id="myapi")
    tab = h.tab("myapi")

    gate = threading.Event()
    original = h.session.instances.build_image

    def slow_build(body, digest, fresh):
        gate.wait(5)
        return original(body, digest, fresh)

    h.session.instances.build_image = slow_build

    mutation = threading.Thread(
        target=lambda: h.session.sandbox_open(tab, "python-dev"))
    mutation.start()
    time.sleep(0.3)

    started = time.monotonic()
    status = ApiClient(client.socket_path, timeout=10).call("status")
    elapsed = time.monotonic() - started
    gate.set()
    mutation.join(20)

    assert elapsed < 2.0, "a read waited behind a running mutation"
    assert status is not None


def test_subscribe_streams_events_as_they_happen(api):
    client, h = api
    received: list[dict] = []
    stop = threading.Event()

    def listen() -> None:
        for event in ApiClient(client.socket_path).subscribe():
            received.append(event)
            if stop.is_set():
                return

    thread = threading.Thread(target=listen, daemon=True)
    thread.start()
    time.sleep(0.4)
    client.call("select", kind="body", id="myapi")
    deadline = time.time() + 5
    while time.time() < deadline and not any(
            e["type"] == "selection.changed" for e in received):
        time.sleep(0.1)
    stop.set()
    assert any(e["type"] == "selection.changed" for e in received)


def test_history_is_scoped_to_one_instance(api):
    client, h = api
    h.open_sandbox("myapi", "python-dev")
    webui = h.open_sandbox("webui", "python-dev")

    history = client.call("history", instance_id=webui)
    assert history
    assert all(e["instance"] == webui for e in history), \
        "an agent must see its own instance's history, not the whole machine's"


def test_the_socket_is_owner_only(api):
    client, _ = api
    assert client.socket_path.stat().st_mode & 0o777 == 0o600


def test_a_missing_daemon_says_so_and_points_at_the_event_log(tmp_path):
    client = ApiClient(tmp_path / "absent.sock")
    with pytest.raises(ApiError, match="not listening"):
        client.call("status")


def test_reconcile_brings_back_the_resident_surfaces_that_are_not_running(api, monkeypatch):
    """The janitor's repair for a host surface that exited is `reconcile`."""
    client, _ = api
    started: list = []
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    monkeypatch.setattr("raigolmid.hostsurfaces.start_at_rest",
                        lambda runtime, paths, role: started.append(str(role)))
    report = client.call("reconcile")
    assert report["host_surfaces"] == {"selector": "restored", "control": "restored",
                                       "notify": "restored", "catalog": "restored"}
    assert started == ["selector", "control", "notify", "catalog"]


def test_only_parameters_that_do_not_fit_are_bad_params(tmp_path):
    """A TypeError raised inside a method is its failure, with its traceback in the event
    log; reported as the caller's bad params it would send the repair to the wrong side."""
    from raigolmid.events import EventLog

    def broken(n: int) -> None:
        raise TypeError("a bug inside the method")

    events = EventLog(tmp_path / "events.jsonl")
    server = ApiServer(tmp_path / "a.sock", {"broken": broken}, events, ready=answering())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = ApiClient(tmp_path / "a.sock", timeout=10)
    try:
        with pytest.raises(ApiError) as refused:
            client.call("broken", m=1)
        assert refused.value.kind == "bad_params"
        assert not [e for e in events.tail(10) if e.type == "api.error"]

        with pytest.raises(ApiError) as failed:
            client.call("broken", n=1)
        assert failed.value.kind == "TypeError"
        [error] = [e for e in events.tail(10) if e.type == "api.error"]
        assert "a bug inside the method" in error.data["traceback"]
    finally:
        server.shutdown()
        server.server_close()
