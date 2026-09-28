"""A client gone mid-request is ordinary — a follow window closing with its tab, a caller that
timed out — and is logged as one line, never as socketserver's traceback."""
from __future__ import annotations

import logging
import socket
import struct
import threading
import time

from raigolmid.api import ApiServer
from raigolmid.events import EventLog
from tests.harness import answering


def test_a_caller_gone_before_its_reply_is_one_log_line(tmp_path, caplog, capfd):
    answered = threading.Event()

    def slow() -> str:
        time.sleep(0.3)
        answered.set()
        return "late"

    server = ApiServer(tmp_path / "raigolmid.sock", {"slow": slow},
                       EventLog(tmp_path / "events.jsonl"), ready=answering())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with caplog.at_level(logging.INFO, logger="raigolmid.api"):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(tmp_path / "raigolmid.sock"))
            client.sendall(b'{"id": 1, "method": "slow"}\n{"id": 2, "method": "slow"}\n')
            # Closed with nothing read and linger 0: the reply meets a reset, as a window
            # killed with its tab does.
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            client.close()
            assert answered.wait(5)
            deadline = time.monotonic() + 5
            while not any("went away" in r.getMessage() for r in caplog.records):
                assert time.monotonic() < deadline, "the hang-up was never logged"
                time.sleep(0.05)
    finally:
        server.shutdown()
        server.server_close()
    assert "Traceback" not in capfd.readouterr().err
