"""The face's side of a language or debug server's pipe.

The editor runs in the face and starts `rai lsp …` as though it were the server; the server
runs in the toolbelt's container, whose root is the body's filesystem. `/work` and
`/nix/store` are the same path in both, so project files and the toolbelt's own (a server's
bundled stubs) pass untouched. Everything else the server names is a body path, and the face
sees the body read-only at `/body`: a `file:///usr/lib/x.py` from the server is
`file:///body/usr/lib/x.py` to the editor, and back.

The server is the focused instance's, in the view `Paths.focused_view` names, reached by
naming that instance to raigolmid (`BrokeredLauncherClient`): `/body` is the focused root, so
the focused instance's is the one server whose paths the rewrite can show. A pipe carries
one server's life: when that view goes the stream ends, and a server for the next view is a
new client with its own handshake, started by the editor (`nvim/lua/raigolmi.lua`).

Every string that is a `file:///` URI is rewritten, and every object key that is one — a
workspace edit's `changes` is keyed by URI. A URI inside prose (a hover's markdown) is not:
it is text, and rewriting text would be guessing what the server meant.

A pid does not cross either. `initialize`'s `processId` is the editor's pid in the face, and
a server watches it and exits when it is gone — in the toolbelt's pid namespace that number is
nothing, or someone else. The shim sends null: the pipe is what ties the server's life to the
editor's.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
from typing import TYPE_CHECKING, Any, BinaryIO, Callable

if TYPE_CHECKING:
    from raigolmid.paths import Paths

FILE = "file://"
BODY = "/body"
WORK = "/work"
SHARED = (WORK, "/nix/store")


class FramingError(RuntimeError):
    pass


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


def uri_to_editor(uri: str) -> str:
    path = uri[len(FILE):]
    if any(_under(path, root) for root in SHARED):
        return uri
    return FILE + BODY + path


def uri_to_server(uri: str) -> str:
    path = uri[len(FILE):]
    if _under(path, BODY):
        return FILE + (path[len(BODY):] or "/")
    return uri


def to_server(message: Any) -> Any:
    message = rewrite(message, uri_to_server)
    if isinstance(message, dict) and message.get("method") == "initialize":
        message["params"]["processId"] = None
    return message


def to_editor(message: Any) -> Any:
    return rewrite(message, uri_to_editor)


def rewrite(value: Any, uri: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return uri(value) if value.startswith(FILE + "/") else value
    if isinstance(value, list):
        return [rewrite(v, uri) for v in value]
    if isinstance(value, dict):
        return {(uri(k) if k.startswith(FILE + "/") else k): rewrite(v, uri)
                for k, v in value.items()}
    return value


def read_message(stream: BinaryIO) -> bytes | None:
    """One message's body, or None at a clean end of stream between messages."""
    length = None
    while True:
        line = stream.readline()
        if not line:
            if length is None:
                return None
            raise FramingError("stream ended inside a message's headers")
        if line in (b"\r\n", b"\n"):
            break
        name, _, value = line.decode("ascii").partition(":")
        if name.strip().lower() == "content-length":
            length = int(value.strip())
    if length is None:
        raise FramingError("a message without Content-Length")
    body = stream.read(length)
    if len(body) != length:
        raise FramingError(f"stream ended {length - len(body)} bytes into a message")
    return body


def write_message(stream: BinaryIO, body: bytes) -> None:
    stream.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    stream.flush()


def pump(source: BinaryIO, sink: BinaryIO, translate: Callable[[Any], Any],
         seen: Callable[[Any], None] = lambda message: None) -> None:
    while (body := read_message(source)) is not None:
        message = translate(json.loads(body))
        seen(message)
        write_message(sink, json.dumps(message, ensure_ascii=False,
                                       separators=(",", ":")).encode("utf-8"))


def relay(editor_in: BinaryIO, editor_out: BinaryIO, server: socket.socket) -> bool:
    """Until the server's stream ends; True when the editor ended the session — sent `exit`,
    after which the server leaves first, or closed its side. The editor closing its side ends
    the server: the launcher stops a stream's process when its caller goes."""
    server_out = server.makefile("rb")
    server_in = server.makefile("wb")
    editor_done = threading.Event()

    def exit_sent(message: Any) -> None:
        if isinstance(message, dict) and message.get("method") == "exit":
            editor_done.set()

    def outbound() -> None:
        try:
            pump(editor_in, server_in, to_server, exit_sent)
        finally:
            editor_done.set()
            try:
                server.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    threading.Thread(target=outbound, name="lsp-outbound", daemon=True).start()
    pump(server_out, editor_out, to_editor)
    return editor_done.is_set()


def focused(paths: Paths) -> tuple[str, str] | None:
    """The instance whose view the face shows and that view's container id, or None when it
    has no view. A new id for the same instance is its view recreated."""
    line = paths.focused_view.read_text().split()
    return (line[0], line[1]) if line else None


def main(paths: Paths, cmd: list[str]) -> int:
    from raigolmid.launcher import protocol
    from raigolmid.launcher.client import (BrokeredLauncherClient, LauncherError,
                                          LauncherUnreachable)

    shown = focused(paths)
    if shown is None:
        sys.stderr.write(f"rai lsp: no sandbox is open, so there is no {cmd[0]} to start: "
                         f"the selected body's tab opens one with `sandbox_open`\n")
        return 1
    launcher = BrokeredLauncherClient(paths.api_socket, shown[0])
    try:
        generation = launcher.ping()["generation"]
        sock, proc = launcher.open_stream(cmd, cwd=WORK)
    except (LauncherUnreachable, LauncherError, protocol.ProtocolError) as exc:
        sys.stderr.write(f"rai lsp: the focused view, {launcher.where}, did not "
                         f"start {cmd[0]}: {exc}\n")
        return 1
    try:
        editor_closed = relay(sys.stdin.buffer, sys.stdout.buffer, sock)
    finally:
        sock.close()
    # The outbound thread may be blocked reading the editor's stdin, and no read can be woken:
    # a normal return aborts in interpreter shutdown, so both ends leave by os._exit.
    if editor_closed:
        sys.stdout.flush()
        os._exit(0)
    # The stream ends the same way when the server dies and when its whole view goes — a
    # swap tears the view down before `focused` names the new one. The launcher tells them
    # apart: gone, or another generation, is the view replaced, and the editor restarts its
    # servers when `focused` changes, so this one leaves quietly rather than as a crash.
    try:
        replaced = launcher.ping()["generation"] != generation
    except (LauncherUnreachable, protocol.ProtocolError):
        replaced = True
    if replaced:
        sys.stderr.write(f"rai lsp: {cmd[0]}'s view was replaced; the editor starts it again "
                         f"on the new one\n")
        sys.stderr.flush()
        os._exit(0)
    sys.stderr.write(f"rai lsp: {cmd[0]} (launcher proc {proc}) ended its stream while the "
                     f"editor was still talking to it; its stderr is in the view's log\n")
    sys.stderr.flush()
    os._exit(1)
