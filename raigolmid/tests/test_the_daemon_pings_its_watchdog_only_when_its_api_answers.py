"""systemd restarts a daemon whose API has stopped answering: the daemon arms its watchdog and
pings it only after its own socket has answered `version` (`Daemon._keep_alive`). A ping
sent without that answer would keep a wedged daemon alive."""
from __future__ import annotations

import socket
import threading
from types import SimpleNamespace

from raigolmid.api import ApiServer
from raigolmid.daemon import WATCHDOG_SECONDS, Daemon
from raigolmid.session import SessionError


def _notified(tmp_path, monkeypatch, version) -> list[bytes]:
    """What the daemon told systemd's notify socket while its API answered `version` with
    `version`, over one turn of its watchdog loop."""
    notify = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    notify.bind(str(tmp_path / "notify"))
    monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "notify"))
    asked = threading.Event()

    def answer():
        asked.set()
        return version()

    ready = threading.Event()
    ready.set()
    api = ApiServer(tmp_path / "api.sock", {"version": answer}, SimpleNamespace(), ready=ready)
    threading.Thread(target=api.serve_forever, daemon=True).start()
    daemon = object.__new__(Daemon)
    daemon.paths = SimpleNamespace(api_socket=tmp_path / "api.sock")
    daemon._stop = threading.Event()
    keeping = threading.Thread(target=daemon._keep_alive, daemon=True)
    try:
        keeping.start()
        assert asked.wait(10), "the watchdog never asked the API"
        daemon._stop.set()
        keeping.join(10)
        assert not keeping.is_alive()
    finally:
        api.shutdown()
        api.server_close()
    notify.setblocking(False)
    said = []
    while True:
        try:
            said.append(notify.recv(4096))
        except BlockingIOError:
            notify.close()
            return said


def test_a_ping_follows_an_answered_version(tmp_path, monkeypatch):
    said = _notified(tmp_path, monkeypatch, lambda: {"protocol": 1, "epoch": 1})
    assert said == [b"WATCHDOG_USEC=%d" % (WATCHDOG_SECONDS * 1_000_000), b"WATCHDOG=1"]


def test_no_ping_follows_a_failed_version(tmp_path, monkeypatch):
    def refused():
        raise SessionError("wedged")

    said = _notified(tmp_path, monkeypatch, refused)
    assert said == [b"WATCHDOG_USEC=%d" % (WATCHDOG_SECONDS * 1_000_000)]
