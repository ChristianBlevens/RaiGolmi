"""The client half of the socket API.

Separate from `api.py` because everything that *uses* raigolmid is not raigolmid: `rai`, both
selectors and the MCP proxy only need to open a socket and exchange a line of JSON, and
importing the server would pull `Session` and the container runtime in behind it. The
selectors ship as images that have no business carrying a Docker SDK.
"""
from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from typing import Any


class ApiError(Exception):
    def __init__(self, message: str, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind


class ApiClient:
    def __init__(self, socket_path: Path, timeout: float = 600.0) -> None:
        self.socket_path = Path(socket_path)
        self.timeout = timeout
        self._id = 0
        self._lock = threading.Lock()

    def call(self, method: str, **params: Any) -> Any:
        with self._lock:
            self._id += 1
            request_id = self._id
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(str(self.socket_path))
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise ApiError(
                f"raigolmid is not listening on {self.socket_path} ({exc}). Start it with "
                "`raigolmid`, or read the event log directly — it is a plain file.",
                kind="unreachable",
            ) from exc
        try:
            sock.sendall((json.dumps({"id": request_id, "method": method,
                                      "params": params}) + "\n").encode())
            with sock.makefile("rb") as fh:
                line = fh.readline()
            if not line:
                raise ApiError(f"raigolmid closed the connection during '{method}'")
            reply = json.loads(line)
        except (OSError, ValueError) as exc:
            # A daemon restarting mid-call resets the connection, times out, or leaves half a
            # reply that does not parse: the same "unreachable" as one not listening, which
            # every caller already outlives.
            raise ApiError(f"raigolmid went away during '{method}' ({exc})",
                           kind="unreachable") from exc
        finally:
            sock.close()
        if not reply.get("ok"):
            raise ApiError(reply.get("error", "unknown error"),
                           kind=reply.get("kind", "error"))
        return reply.get("result")

    def subscribe(self, subscribed: threading.Event | None = None):
        """Yields events as they happen, for `rai events` and the selectors. `subscribed` is
        set once the daemon has registered this subscriber: every event after that is
        yielded, so a state read after it misses nothing."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(None)
        try:
            sock.connect(str(self.socket_path))
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise ApiError(f"raigolmid is not listening on {self.socket_path} ({exc})",
                           kind="unreachable") from exc
        sock.sendall(b'{"id": 0, "method": "subscribe", "params": {}}\n')
        with sock.makefile("rb") as fh:
            for line in fh:
                if not line.strip():
                    continue
                payload = json.loads(line)
                if "event" in payload:
                    yield payload["event"]
                elif subscribed is not None and payload.get("ok"):
                    subscribed.set()
