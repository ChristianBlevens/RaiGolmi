"""A daemon restarting in the middle of a call is `unreachable`, as one not listening is: the
channel's poller outlives that and nothing else, and a bare `ConnectionResetError` killed it."""
from __future__ import annotations

import socket
import threading
import time

import pytest

from raigolmid.client import ApiClient, ApiError


def test_a_connection_reset_mid_call_is_unreachable(tmp_path):
    path = tmp_path / "raigolmid.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)

    def die_with_the_request_unread() -> None:
        conn, _ = server.accept()
        time.sleep(0.2)
        conn.close()

    threading.Thread(target=die_with_the_request_unread, daemon=True).start()
    try:
        with pytest.raises(ApiError) as caught:
            ApiClient(path, timeout=5).call("channel_take")
    finally:
        server.close()
    assert caught.value.kind == "unreachable"
    assert "went away during 'channel_take'" in str(caught.value)


