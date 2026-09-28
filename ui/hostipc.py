"""Asking the host compositor to run a command, from inside a container.

The host control draws from an image (the host `/` is a read-only ostree overlay and
cannot gain a toolkit), but the two things it launches — the selector and the AI terminal —
are host commands. So the click goes the same way a reserved key goes: sway execs it.

This is the sway IPC protocol from sway-ipc(7) rather than the `swaymsg` binary, because
`swaymsg` means the whole `sway` package inside an image that exists only to carry GTK. The
socket is already reachable: it lives in `XDG_RUNTIME_DIR`, which the control mounts to speak
Wayland at all.

⚠ **Sway's answer is not evidence that the command ran.** `exec` forks and sway replies
`{"success":true}` without waiting, so a missing binary is reported as success — the same
trap `faces.py` records for `swaymsg` generally. What `run_command` promises is that the
compositor accepted the command, and that is all it says.
"""
from __future__ import annotations

import json
import os
import socket
import struct

MAGIC = b"i3-ipc"
RUN_COMMAND = 0
SUBSCRIBE = 2
GET_OUTPUTS = 3
GET_TREE = 4
GET_SEATS = 101
# sway-ipc(7): magic string, then a 32-bit length and a 32-bit type, both in NATIVE byte
# order — not network order, which is the mistake this format invites.
_HEADER = struct.Struct("=II")


class HostIpcError(RuntimeError):
    pass


def socket_path() -> str:
    path = os.environ.get("SWAYSOCK") or ""
    if not path:
        raise HostIpcError(
            "SWAYSOCK is unset, so the host compositor cannot be reached. It is the host's "
            "own socket and must be passed in: a glob of /run/user/1000/sway-ipc.* picks a "
            "face's dead socket instead, because a face's sway is pid 1 in its namespace."
        )
    return path


def run_command(command: str, swaysock: str | None = None) -> None:
    """Ask the host compositor to run one sway command. Raises if it refuses."""
    reply = _ask(RUN_COMMAND, command.encode("utf-8"), swaysock)
    for result in reply if isinstance(reply, list) else [reply]:
        if not result.get("success", False):
            raise HostIpcError(
                f"the host compositor refused '{command}': "
                f"{result.get('error', 'no reason given')}"
            )


def tree(swaysock: str | None = None) -> dict:
    """The host compositor's window tree."""
    return _query(GET_TREE, dict, swaysock)


def outputs(swaysock: str | None = None) -> list[dict]:
    return _query(GET_OUTPUTS, list, swaysock)


def seats(swaysock: str | None = None) -> list[dict]:
    return _query(GET_SEATS, list, swaysock)


def subscribe(events: list[str], swaysock: str | None = None,
              timeout: float | None = None):
    """The host compositor's events as they happen, forever.

    The subscription is confirmed before this returns, so anything the caller asks of the
    compositor afterwards is seen. ⚠ `timeout` is per event and defaults to none, unlike
    `run_command`'s: an event stream is idle most of the time, and a timeout would read a
    quiet compositor as a broken one. The caller owns the thread this blocks."""
    path = swaysock or socket_path()
    payload = json.dumps(events).encode("utf-8")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(path)
        sock.sendall(MAGIC + _HEADER.pack(len(payload), SUBSCRIBE) + payload)
        reply = _read_reply(sock)
    except BaseException as exc:
        sock.close()
        if isinstance(exc, OSError):
            raise HostIpcError(f"host compositor at {path}: {exc}") from exc
        raise
    if not (isinstance(reply, dict) and reply.get("success", False)):
        sock.close()
        raise HostIpcError(f"the host compositor refused the subscription: {reply}")
    return _events(sock, path)


def _events(sock: socket.socket, path: str):
    with sock:
        try:
            while True:
                yield _read_reply(sock)
        except OSError as exc:
            raise HostIpcError(f"host compositor at {path}: {exc}") from exc


def nodes(node: dict):
    """Every node in the tree, floating windows included, parents before children."""
    yield node
    for child in (*node.get("nodes", ()), *node.get("floating_nodes", ())):
        yield from nodes(child)


def find(node: dict, match) -> dict | None:
    """The first node `match` accepts, anywhere in the tree."""
    return next((n for n in nodes(node) if match(n)), None)


def find_app(node: dict, app_id: str) -> dict | None:
    """The first node with this app id, anywhere in the tree."""
    return find(node, lambda n: n.get("app_id") == app_id)


def _query(kind: int, shape: type, swaysock: str | None):
    reply = _ask(kind, b"", swaysock)
    if not isinstance(reply, shape):
        raise HostIpcError(f"the host compositor answered IPC type {kind} with "
                           f"{type(reply).__name__}, not {shape.__name__}")
    return reply


def _ask(kind: int, payload: bytes, swaysock: str | None) -> object:
    path = swaysock or socket_path()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(5.0)
            sock.connect(path)
            sock.sendall(MAGIC + _HEADER.pack(len(payload), kind) + payload)
            return _read_reply(sock)
    except OSError as exc:
        raise HostIpcError(f"host compositor at {path}: {exc}") from exc


def _read_reply(sock: socket.socket) -> object:
    header = _recv_exactly(sock, len(MAGIC) + _HEADER.size)
    if not header.startswith(MAGIC):
        raise HostIpcError(f"not a sway IPC reply: {header!r}")
    length, _type = _HEADER.unpack(header[len(MAGIC):])
    return json.loads(_recv_exactly(sock, length).decode("utf-8"))


def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    chunks, got = [], 0
    while got < n:
        chunk = sock.recv(n - got)
        if not chunk:
            raise HostIpcError(
                f"the host compositor closed the connection after {got} of {n} bytes"
            )
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)
