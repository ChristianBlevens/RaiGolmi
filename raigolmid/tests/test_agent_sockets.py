"""Each agent tab's own socket: served while the tab is open, bound to
that tab, and nothing else of the runtime dir."""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
import threading
from pathlib import Path

import pytest

from raigolmid.api import build_methods
from raigolmid.channel import Channels
from raigolmid.client import ApiClient, ApiError
from raigolmid.intent import JANITOR
from raigolmid.paths import Paths
from raigolmid.questions import Questions
from raigolmid.history import History
from raigolmid.viewing import Viewing
from raigolmid.scopes import SOCKET, AgentSockets

from tests.harness import Harness, answering


@pytest.fixture()
def world(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    h.session.select("body", "myapi")
    # Real sockets under a short runtime dir: a Unix socket path is limited to 108 bytes.
    short = Path(tempfile.mkdtemp(prefix="rai-", dir="/tmp"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short))
    paths = Paths.from_env()
    questions, channels = Questions(h.events, h.paths), Channels(h.session, h.events)
    sockets = AgentSockets(paths, h.session, h.events, questions, channels,
                           build_methods(h.session, h.events, questions, channels, Viewing(h.events, h.paths.viewing), History(h.events, h.paths)), answering())
    stop = threading.Event()
    try:
        yield h, paths, sockets, stop
    finally:
        stop.set()
        sockets.close_all()
        shutil.rmtree(short)


def running(sockets: AgentSockets, stop: threading.Event) -> threading.Thread:
    thread = threading.Thread(target=lambda: sockets.run(stop), daemon=True)
    thread.start()
    return thread


def answers(paths: Paths, tab: str, timeout: float = 5.0) -> ApiClient:
    client = ApiClient(paths.agent_socket_dir(tab) / SOCKET, timeout=5)
    for _ in range(int(timeout / 0.05)):
        try:
            client.call("version")
            return client
        except ApiError:
            threading.Event().wait(0.05)
    raise AssertionError(f"{tab}'s socket never answered")


def test_a_tab_opening_gets_its_socket_and_closing_takes_it(world):
    """Closing the selected body's tab takes its socket and directory; the fresh tab that
    replaces it gets its own."""
    h, paths, sockets, stop = world
    running(sockets, stop)
    old = h.tab("myapi")
    client = answers(paths, old)
    assert client.call("status")["tab"]["tab"] == old
    directory = paths.agent_socket_dir(old)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert [p.name for p in directory.iterdir()] == [SOCKET]

    h.session.close_tab(old)
    new = h.tab("myapi")
    assert new != old
    assert answers(paths, new).call("status")["tab"]["tab"] == new
    for _ in range(100):
        if not directory.exists():
            break
        threading.Event().wait(0.05)
    assert not directory.exists(), "a tab id is never reused, so its directory goes with it"


def test_the_janitor_reopened_at_once_gets_its_socket_in_the_directory_its_container_mounted(
        world):
    """The janitor's id is fixed, and its container binds the directory before the close
    reaches this thread. A held descriptor stands in for that bind mount: both pin the
    directory's inode."""
    _, paths, sockets, _ = world
    sockets.open(JANITOR)
    mounted = os.open(paths.agent_socket_dir(JANITOR), os.O_RDONLY | os.O_DIRECTORY)
    try:
        sockets.close(JANITOR)
        sockets.open(JANITOR)
        assert os.listdir(mounted) == [SOCKET]
    finally:
        os.close(mounted)
        sockets.close(JANITOR)


def test_a_tab_socket_serves_neither_the_event_stream_nor_the_hosts_methods(world):
    h, paths, sockets, stop = world
    running(sockets, stop)
    client = answers(paths, h.tab("myapi"))
    for method in ("subscribe", "close_tab", "ensure_tabs", "answer", "events", "journal"):
        with pytest.raises(ApiError, match="unknown method"):
            client.call(method)


def test_the_janitors_socket_answers_the_machine(world):
    h, paths, sockets, stop = world
    running(sockets, stop)
    h.session.open_janitor()
    client = answers(paths, JANITOR)
    assert "agents" in client.call("status"), "the janitor reads the whole machine"
    client.call("events", n=5)
    with pytest.raises(ApiError, match="unknown method"):
        client.call("exec", cmd=["true"])
