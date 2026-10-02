"""raigolmid's side of the launcher socket.

Every process that runs in a session view is started through here: terminals' shells,
language servers, and agent `exec` calls. `ping` is how reconciliation decides a view is
usable — a view that cannot be talked to cannot be used, and adopting one would hand the
user tools nobody can drive.
"""
from __future__ import annotations

import json
import os
import socket
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import protocol

DEFAULT_TIMEOUT = 5.0


class LauncherUnreachable(Exception):
    """The socket is missing, refusing, or not answering. Distinct from a command that
    ran and failed: this one means the view itself has to be recreated."""


class LauncherError(Exception):
    """The launcher answered, and the answer was no."""


class LauncherTimeout(LauncherError):
    """The command started and produced nothing within the timeout. Its own class because
    a caller can say *what* did not answer, which the launcher cannot."""


class LauncherOutputHeld(LauncherTimeout):
    """The command exited, but a process it left running held its output at the timeout.
    Nothing about the view is in question; the work is: the launcher ends the command's
    process group once its caller stops waiting (`server._abandon`)."""


@dataclass(frozen=True, slots=True)
class ExecOutput:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class LauncherClient:
    def __init__(self, socket_path: Path, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.socket_path = Path(socket_path)
        self.timeout = timeout
        self.where = str(self.socket_path)

    def connect(self, timeout: float | None = None) -> socket.socket:
        """A fresh connection to the launcher, speaking its protocol from the first byte:
        what `Session.open_launcher` hands to a caller that named the sandbox."""
        return self._connect(timeout)

    def _connect(self, timeout: float | None = None) -> socket.socket:
        if not self.socket_path.exists():
            raise LauncherUnreachable(f"{self.socket_path} does not exist")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout if timeout is not None else self.timeout)
        try:
            sock.connect(str(self.socket_path))
        except OSError as exc:
            sock.close()
            raise LauncherUnreachable(f"{self.socket_path}: {exc}") from exc
        return sock

    @staticmethod
    def _readline(fh) -> dict[str, Any]:
        line = fh.readline()
        if not line:
            raise LauncherUnreachable("the launcher closed the connection without answering")
        return protocol.decode(line)

    @staticmethod
    def _read_frame(sock: socket.socket) -> dict[str, Any]:
        """Read exactly one newline-terminated frame off the socket and nothing more.

        `socket.makefile` reads ahead into a buffer, which is fine while the whole
        connection is framed, and wrong for the two ops where raw bytes follow the header
        immediately: the buffered reader swallows the first of the process's output and
        drops it on close. A PTY's first bytes are its prompt, so the symptom is a shell
        that looks hung.
        """
        buf = bytearray()
        while not buf.endswith(b"\n"):
            try:
                chunk = sock.recv(1)
            except OSError as exc:
                raise LauncherUnreachable(f"reading the launcher's answer: {exc}") from exc
            if not chunk:
                raise LauncherUnreachable(
                    "the launcher closed the connection without answering")
            buf.extend(chunk)
        return protocol.decode(bytes(buf))

    def ping(self, timeout: float = 2.0) -> dict[str, Any]:
        sock = self._connect(timeout)
        try:
            sock.sendall(protocol.encode({"op": "ping"}))
            with sock.makefile("rb") as fh:
                reply = self._readline(fh)
        except (OSError, protocol.ProtocolError) as exc:
            raise LauncherUnreachable(f"{self.where}: {exc}") from exc
        finally:
            sock.close()
        if not reply.get("ok"):
            raise LauncherUnreachable(reply.get("error", "ping refused"))
        return reply

    def alive(self, timeout: float = 2.0) -> bool:
        try:
            self.ping(timeout)
        except (LauncherUnreachable, protocol.ProtocolError):
            return False
        return True

    def list(self) -> list[dict[str, Any]]:
        sock = self._connect()
        try:
            sock.sendall(protocol.encode({"op": "list"}))
            with sock.makefile("rb") as fh:
                reply = self._readline(fh)
        finally:
            sock.close()
        if not reply.get("ok"):
            raise LauncherError(reply.get("error", "list refused"))
        return reply.get("procs", [])

    def process(self, launcher: str, proc_id: int) -> dict[str, Any] | None:
        """What the launcher that issued `proc_id` knows of it, or None when another launcher
        answers — a recreated view's, which never knew the process and numbers its own
        from 1 again."""
        sock = self._connect()
        try:
            sock.sendall(protocol.encode({"op": "list"}))
            with sock.makefile("rb") as fh:
                reply = self._readline(fh)
        finally:
            sock.close()
        if not reply.get("ok"):
            raise LauncherError(reply.get("error", "list refused"))
        if reply["launcher"] != launcher:
            return None
        return {p["proc"]: p for p in reply["procs"]}[proc_id]

    def scrollback(self, proc: int, timeout: float = 5.0) -> str:
        """What a process printed, read without `attach`, which takes over the terminal and
        would make a diagnostic an interaction."""
        sock = self._connect(timeout)
        try:
            sock.sendall(protocol.encode({"op": "scrollback", "proc": proc}))
            with sock.makefile("rb") as fh:
                reply = self._readline(fh)
        finally:
            sock.close()
        if not reply.get("ok"):
            raise LauncherError(reply.get("error", "scrollback refused"))
        return protocol.frame_bytes(reply).decode("utf-8", errors="replace")

    def exec(self, cmd: list[str], *, cwd: str = "/work",
             env: dict[str, str] | None = None, stdin: str | None = None,
             timeout: float = 300.0) -> ExecOutput:
        """Non-interactive: framed streams and a real exit code. This is what an agent's
        `exec` over MCP gets, and what a failing command has to report honestly."""
        req = protocol.StartRequest(cmd=tuple(cmd), env=env or {}, cwd=cwd,
                                    pty=False, stdin=stdin)
        sock = self._connect(timeout)
        out: list[str] = []
        err: list[str] = []
        exit_code: int | None = None
        exited: int | None = None
        try:
            sock.sendall(protocol.encode(req.to_wire()))
            with sock.makefile("rb") as fh:
                started = self._readline(fh)
                if not started.get("ok"):
                    raise LauncherError(started.get("error", "start refused"))
                while True:
                    line = fh.readline()
                    if not line:
                        break
                    frame = protocol.decode(line)
                    if "exit" in frame:
                        exit_code = int(frame["exit"])
                        break
                    if "exited" in frame:
                        exited = int(frame["exited"])
                        continue
                    text = protocol.frame_bytes(frame).decode("utf-8", errors="replace")
                    (out if frame.get("stream") == "stdout" else err).append(text)
        except socket.timeout as exc:
            if exited is not None:
                raise LauncherOutputHeld(
                    f"{' '.join(cmd)} exited {exited}, but a process it started in the "
                    f"background still held its output after {timeout}s, so everything it "
                    "started has been stopped. Anything of it still running left its process "
                    "group (`setsid`, or GNU `timeout`, which takes its own) and is cut off "
                    "from what started it: check before waiting on it. `&` after a `&&` list "
                    "backgrounds the whole list, whose shell keeps the output; a job that "
                    "outlives the call is started on its own output: `setsid cmd > log 2>&1 "
                    f"< /dev/null &`. It printed: stdout {''.join(out)[-2000:]!r} stderr "
                    f"{''.join(err)[-2000:]!r}") from exc
            raise LauncherTimeout(
                f"{' '.join(cmd)} produced no result within {timeout}s and has been stopped, "
                "with everything it started"
            ) from exc
        finally:
            sock.close()
        if exit_code is None:
            raise LauncherError(
                f"{' '.join(cmd)} ended without an exit code — the launcher connection "
                "closed early"
            )
        return ExecOutput(exit_code=exit_code, stdout="".join(out), stderr="".join(err))

    def start_detached(self, cmd: list[str], *, cwd: str = "/work",
                       env: dict[str, str] | None = None) -> int:
        """Start something long-lived — a language server — and let go. The
        process is a child of the launcher, so it outlives this connection and the daemon."""
        req = protocol.StartRequest(cmd=tuple(cmd), env=env or {}, cwd=cwd, pty=True)
        sock = self._connect()
        try:
            sock.sendall(protocol.encode(req.to_wire()))
            reply = self._read_frame(sock)
        finally:
            sock.close()
        if not reply.get("ok"):
            raise LauncherError(reply.get("error", "start refused"))
        return int(reply["proc"])

    def open_pty(self, cmd: list[str], *, cwd: str = "/work",
                 env: dict[str, str] | None = None,
                 rows: int = 40, cols: int = 120) -> tuple[socket.socket, int, str]:
        """Interactive: returns the connected socket, which *is* the terminal from here,
        the proc id, and the launcher that issued it (`process`).
        `rai attach` and the AI terminal's tmux windows read and write it directly."""
        req = protocol.StartRequest(cmd=tuple(cmd), env=env or {}, cwd=cwd, pty=True,
                                    rows=rows, cols=cols)
        sock = self._connect()
        sock.sendall(protocol.encode(req.to_wire()))
        reply = self._read_frame(sock)
        if not reply.get("ok"):
            sock.close()
            raise LauncherError(reply.get("error", "start refused"))
        sock.settimeout(None)
        return sock, int(reply["proc"]), reply["launcher"]

    def open_stream(self, cmd: list[str], *, cwd: str = "/work",
                    env: dict[str, str] | None = None) -> tuple[socket.socket, int]:
        """The process's stdin and stdout as the returned socket, bytes untouched: what a
        framed protocol needs (`rai lsp`). Shut down its write side or close it to end
        the process."""
        req = protocol.StartRequest(cmd=tuple(cmd), env=env or {}, cwd=cwd, stream=True)
        sock = self._connect()
        sock.sendall(protocol.encode(req.to_wire()))
        reply = self._read_frame(sock)
        if not reply.get("ok"):
            sock.close()
            raise LauncherError(reply.get("error", "start refused"))
        sock.settimeout(None)
        return sock, int(reply["proc"])

    def attach(self, proc_id: int) -> tuple[socket.socket, str]:
        """Reconnect to a running PTY process: the socket, and the launcher answering
        (`process`). What `rai attach` uses, and what makes a
        raigolmid restart survivable for a shell the user is sitting in."""
        sock = self._connect()
        try:
            sock.sendall(protocol.encode({"op": "attach", "proc": proc_id}))
            reply = self._read_frame(sock)
        except BaseException:
            sock.close()
            raise
        if not reply.get("ok"):
            sock.close()
            raise LauncherError(reply.get("error", "attach refused"))
        sock.settimeout(None)
        return sock, reply["launcher"]

    def resize(self, proc_id: int, rows: int, cols: int) -> None:
        sock = self._connect()
        try:
            sock.sendall(protocol.encode({"op": "resize", "proc": proc_id,
                                          "rows": rows, "cols": cols}))
            with sock.makefile("rb") as fh:
                reply = self._readline(fh)
        finally:
            sock.close()
        if not reply.get("ok"):
            raise LauncherError(reply.get("error", "resize refused"))

    def signal(self, proc_id: int, sig: int = 15) -> None:
        sock = self._connect()
        try:
            sock.sendall(protocol.encode({"op": "signal", "proc": proc_id, "sig": sig}))
            with sock.makefile("rb") as fh:
                reply = self._readline(fh)
        finally:
            sock.close()
        if not reply.get("ok"):
            raise LauncherError(reply.get("error", "signal refused"))

    def stream(self, cmd: list[str], *, cwd: str = "/work",
               env: dict[str, str] | None = None) -> Iterator[tuple[str, str]]:
        """Line-by-line output as it happens, for a build log an agent is watching."""
        req = protocol.StartRequest(cmd=tuple(cmd), env=env or {}, cwd=cwd, pty=False)
        sock = self._connect(timeout=None)
        try:
            sock.sendall(protocol.encode(req.to_wire()))
            with sock.makefile("rb") as fh:
                started = self._readline(fh)
                if not started.get("ok"):
                    raise LauncherError(started.get("error", "start refused"))
                while True:
                    line = fh.readline()
                    if not line:
                        return
                    frame = protocol.decode(line)
                    if "exit" in frame:
                        yield ("exit", str(frame["exit"]))
                        return
                    if "exited" in frame:
                        continue
                    yield (frame.get("stream", "stdout"),
                           protocol.frame_bytes(frame).decode("utf-8", errors="replace"))
        finally:
            sock.close()


class BrokeredLauncherClient(LauncherClient):
    """A sandbox's launcher reached by naming the sandbox to raigolmid, which opens the
    connection and hands it over (`open_launcher`, `api.Handoff`): the way `rai` reaches a
    view from a face or the host, so nothing outside the daemon knows where a launcher's
    socket is."""

    def __init__(self, api_socket: Path, instance: str,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        super().__init__(api_socket, timeout)
        self.instance = instance
        self.where = f"{instance}'s launcher (through {api_socket})"

    def _connect(self, timeout: float | None = None) -> socket.socket:
        timeout = timeout if timeout is not None else self.timeout
        asking = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        asking.settimeout(timeout)
        try:
            asking.connect(str(self.socket_path))
            asking.sendall((json.dumps({"id": 1, "method": "open_launcher",
                                        "params": {"instance": self.instance}}) + "\n").encode())
            data, fds = bytearray(), []
            while not data.endswith(b"\n"):
                chunk, got, _, _ = socket.recv_fds(asking, 65536, 1)
                fds.extend(got)
                if not chunk:
                    break
                data.extend(chunk)
        except OSError as exc:
            raise LauncherUnreachable(f"{self.where}: {exc}") from exc
        finally:
            asking.close()
        if not data.endswith(b"\n"):
            for fd in fds:
                os.close(fd)
            raise LauncherUnreachable(f"{self.where}: raigolmid closed the connection without "
                                      "answering")
        reply = json.loads(data)
        if not reply.get("ok"):
            for fd in fds:
                os.close(fd)
            raise LauncherUnreachable(f"{self.where}: {reply.get('error')}")
        if len(fds) != 1:
            for fd in fds:
                os.close(fd)
            raise LauncherUnreachable(f"{self.where}: raigolmid answered with {len(fds)} "
                                      "connections, not one")
        sock = socket.socket(fileno=fds[0])
        sock.settimeout(timeout)
        return sock
