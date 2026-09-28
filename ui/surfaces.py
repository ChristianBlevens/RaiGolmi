"""The host's surfaces as one kind of thing: how each is asked to move.

The selector drawer, the history menu, the AI terminal and the catalog window each serve a
socket here, named for the surface, and answer four verbs — `open`, `close`, `toggle`,
`state` — with the state they end in. **The surface answers, never a caller's guess**: it is the only process that knows
whether it is out, so a toggle's report and an "already shown" are read off the thing itself.

**Only one is ever open**. A surface a gesture opens asks every other to close
(`close_others`); a surface that opens on the machine's own account — a notice arriving —
asks nothing, because closing the user's terminal over an event they did not cause is the
machine acting on them.

⚠ **Nothing here goes through `raigolmid`.** The surfaces are the way back when the daemon is
what is broken, so the channel is a socket in the runtime directory every surface already
mounts to speak Wayland. The host's `rai` and the surface images all import this module, so
it needs nothing but the standard library.
"""
from __future__ import annotations

import logging
import os
import socket
import threading
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

SELECTOR, HISTORY, TERMINAL, CATALOG = "selector", "history", "terminal", "catalog"
NAMES = (SELECTOR, HISTORY, TERMINAL, CATALOG)
VERBS = ("open", "close", "toggle", "state")
# The AI terminal's window, which the terminal's surface moves and sway's rules match
# (`host/sway/config`).
TERMINAL_APP_ID = "raigolmi-ai"
# A surface answers once its slide has arrived. Opening the terminal may first start its
# window and wait for sway to map it, which is the long one.
ANSWER_SECONDS = 5.0
TERMINAL_ANSWER_SECONDS = 20.0


class SurfaceError(RuntimeError):
    pass


class SurfaceAbsent(SurfaceError):
    """No process answers for the surface: it is not running."""


class SurfaceRefused(SurfaceError):
    """The surface answered, and its answer was a failure."""


def socket_path(runtime: Path, name: str) -> Path:
    if name not in NAMES:
        raise ValueError(f"{name!r} is not a host surface; they are {', '.join(NAMES)}")
    return runtime / "raigolmi-surfaces" / f"{name}.sock"


def runtime_dir() -> Path:
    raw = os.environ.get("XDG_RUNTIME_DIR", "")
    if not raw:
        raise SurfaceError("XDG_RUNTIME_DIR is unset, so no host surface can be reached")
    return Path(raw)


def ask(name: str, verb: str, runtime: Path | None = None,
        timeout: float | None = None) -> str:
    """Ask one surface to move, and return the state it answers with."""
    if verb not in VERBS:
        raise ValueError(f"{verb!r} is not a verb a surface answers; they are {VERBS}")
    path = socket_path(runtime or runtime_dir(), name)
    if timeout is None:
        timeout = TERMINAL_ANSWER_SECONDS if name == TERMINAL else ANSWER_SECONDS
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        try:
            conn.connect(str(path))
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            # A socket file nobody listens on is what a surface that died leaves behind.
            raise SurfaceAbsent(f"the {name} is not running ({path}: {exc})") from exc
        try:
            conn.sendall(f"{verb}\n".encode())
            reply = _read_line(conn)
        except OSError as exc:
            raise SurfaceError(f"the {name} did not answer {verb!r}: {exc}") from exc
    status, _, answer = reply.partition(" ")
    if status == "ok":
        return answer
    if status == "error":
        raise SurfaceRefused(f"the {name} refused {verb!r}: {answer}")
    raise SurfaceError(f"the {name} answered {verb!r} with {reply!r}")


def close_others(name: str, runtime: Path | None = None) -> threading.Thread:
    """Ask every surface but `name` to close, off the caller's thread: two surfaces opening
    at once would otherwise each hold its own main loop waiting on the other's."""
    runtime = runtime or runtime_dir()

    def run() -> None:
        for other in NAMES:
            if other == name:
                continue
            try:
                ask(other, "close", runtime)
            except SurfaceAbsent:
                continue            # not running is not open
            except SurfaceError as exc:
                logger.error("the %s opening could not close the %s: %s", name, other, exc)

    thread = threading.Thread(target=run, name=f"close-others-{name}", daemon=True)
    thread.start()
    return thread


def serve(name: str, answer: Callable[[str], str],
          runtime: Path | None = None) -> socket.socket:
    """Answer the surface's socket for the life of the process, one connection at a time.
    `answer` is called on the serving thread with the verb and returns the state; whatever it
    raises goes back to the asker as the refusal, with its message."""
    path = socket_path(runtime or runtime_dir(), name)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    # A previous process's socket: bind refuses a path that exists, and this surface is
    # the only thing that serves it.
    path.unlink(missing_ok=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(8)

    def run() -> None:
        while True:
            conn, _ = listener.accept()
            with conn:
                try:
                    verb = _read_line(conn)
                    if verb not in VERBS:
                        raise SurfaceError(f"{verb!r} is not a verb; they are {VERBS}")
                    reply = f"ok {answer(verb)}"
                except Exception as exc:        # noqa: BLE001 — the asker is told, with the type
                    logger.error("the %s refused a request: %s", name, exc)
                    reply = f"error {type(exc).__name__}: {exc}".replace("\n", " ")
                try:
                    conn.sendall(f"{reply}\n".encode())
                except OSError as exc:
                    logger.error("the %s could not answer: %s", name, exc)

    threading.Thread(target=run, name=f"surface-{name}", daemon=True).start()
    return listener


def _read_line(conn: socket.socket) -> str:
    data = b""
    while not data.endswith(b"\n"):
        chunk = conn.recv(4096)
        if not chunk:
            raise SurfaceError(f"the connection closed after {data!r}")
        data += chunk
    return data.decode().strip()
