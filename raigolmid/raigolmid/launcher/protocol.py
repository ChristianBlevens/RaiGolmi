"""The launcher wire protocol.

One Unix socket, newline-delimited JSON requests, and a response frame per request. Two
shapes after that, decided by `pty`:

  pty=true   the connection becomes the terminal. Raw bytes flow both ways until the
             process exits, then the connection closes. This is what a shell or a tmux
             window attaches to.
  pty=false  framed `{"stream": "stdout"|"stderr", "data": "..."}` lines followed by one
             `{"exit": N}`. This is what an agent `exec` over MCP reads.
  stream=true  the connection is the process's stdin and stdout, raw bytes both ways and no
             terminal between: a terminal's line discipline rewrites `\n` and echoes input,
             which breaks any framed protocol (LSP, DAP). stderr goes to the
             launcher's own. The connection closes when stdout does; closing it ends the
             process. The client sends nothing until it has read the start reply.

Requests:
  {"op": "ping"}                                     -> {"ok": true, "generation": N, ...}
  {"op": "start", "cmd": [...], "env": {}, "cwd": "/work", "pty": bool,
                  "stream": bool, "stdin": "..."}    -> {"ok": true, "proc": N}
  {"op": "signal", "proc": N, "sig": 15}             -> {"ok": true}
  {"op": "resize", "proc": N, "rows": R, "cols": C}  -> {"ok": true}
  {"op": "list"}                                     -> {"ok": true, "procs": [...]}
  {"op": "attach", "proc": N}                        -> the connection becomes that pty
  {"op": "scrollback", "proc": N}                    -> {"ok": true, "exit": N|null, "stream": "stdout", "data": ...}

An error is always `{"ok": false, "error": "..."}` and never a silent default: a launcher
that pretends to have started a process the caller cannot see is the worst failure this
component has, because the caller's next move is to wait for output that will never come.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = 1

# stdout/stderr frames carry base64 when the bytes are not valid UTF-8 — a debugger's
# output and a compiler's are not always text, and mangling them silently would be a lie.
ENCODING_TEXT = "text"
ENCODING_BASE64 = "base64"


class ProtocolError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class StartRequest:
    cmd: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = "/work"
    pty: bool = False
    stream: bool = False
    stdin: str | None = None
    term: str = "xterm-256color"
    rows: int = 40
    cols: int = 120

    def to_wire(self) -> dict[str, Any]:
        d: dict[str, Any] = {"op": "start", "cmd": list(self.cmd), "env": self.env,
                             "cwd": self.cwd, "pty": self.pty,
                             "term": self.term, "rows": self.rows, "cols": self.cols}
        if self.stream:
            d["stream"] = True
        if self.stdin is not None:
            d["stdin"] = self.stdin
        return d

    @classmethod
    def from_wire(cls, d: dict[str, Any]) -> "StartRequest":
        cmd = d.get("cmd")
        if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) for c in cmd):
            raise ProtocolError("'cmd' must be a non-empty list of strings")
        stream = bool(d.get("stream", False))
        if stream and (d.get("pty") or d.get("stdin") is not None):
            raise ProtocolError("a stream is its own stdin and has no terminal: "
                                "'stream' excludes 'pty' and 'stdin'")
        return cls(
            cmd=tuple(cmd),
            env={str(k): str(v) for k, v in (d.get("env") or {}).items()},
            cwd=str(d.get("cwd", "/work")),
            pty=bool(d.get("pty", False)),
            stream=stream,
            stdin=d.get("stdin"),
            term=str(d.get("term", "xterm-256color")),
            rows=int(d.get("rows", 40)),
            cols=int(d.get("cols", 120)),
        )


def encode(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line: bytes) -> dict[str, Any]:
    try:
        obj = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"not a protocol frame: {exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("a protocol frame is a JSON object")
    return obj


def ok(**fields: Any) -> dict[str, Any]:
    return {"ok": True, **fields}


def error(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


def stream_frame(stream: str, data: bytes) -> dict[str, Any]:
    try:
        return {"stream": stream, "data": data.decode("utf-8"), "encoding": ENCODING_TEXT}
    except UnicodeDecodeError:
        return {"stream": stream, "data": base64.b64encode(data).decode("ascii"),
                "encoding": ENCODING_BASE64}


def frame_bytes(frame: dict[str, Any]) -> bytes:
    data = frame.get("data", "")
    if frame.get("encoding") == ENCODING_BASE64:
        return base64.b64decode(data)
    return str(data).encode("utf-8")
