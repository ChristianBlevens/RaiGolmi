"""A host compositor on a socket, speaking sway-ipc(7) as sway does, for the surfaces that
reach it through `ui.hostipc`.

It holds one output, a face fullscreen on it, and the AI terminal's window once something has
started it. ⚠ **It refuses what sway refuses**: a command it does not model answers
`success: false`, as sway answers a command it cannot parse, so a surface that sends
something sway would not take fails here rather than passing on a double that agreed to
anything. What it models is sway's own behaviour that the surfaces lean on: `exec` maps the
window (and says so to subscribers), the terminal is ruled into the scratchpad as it maps
(`host/sway/config`), and showing the scratchpad ends the face's fullscreen. `reload` answers
success whatever the config says, then re-reads it later, spawning a real `swaynag` process
for a config it refuses before it tells workspace subscribers the reload is done
(sway 1.10.1 `commands/reload.c`, `config.c` `read_config`).
"""
from __future__ import annotations

import json
import re
import socket
import struct
import subprocess
import threading
from pathlib import Path
from typing import Callable

MAGIC = b"i3-ipc"
HEADER = struct.Struct("=II")
RUN_COMMAND, SUBSCRIBE, GET_TREE = 0, 2, 4
WORKSPACE_EVENT, WINDOW_EVENT = 0x80000000, 0x80000003
EVENTS = {"workspace": WORKSPACE_EVENT, "window": WINDOW_EVENT}
OUTPUT_HEIGHT = 800
# Some of what xkeyboard-config ships; a name outside them is refused as xkb refuses a
# symbols file it cannot find.
LAYOUTS = frozenset({"us", "de", "gb", "fr"})
VARIANTS = frozenset({"dvorak", "nodeadkeys", "intl"})
FACE_ID, TERMINAL_ID, SCRATCH_ID = 10, 20, 2147483646


class FakeSway:
    def __init__(self, path: Path, *, terminal_command: str = "", app_id: str = "",
                 maps: bool = True, face: bool = True, include: Path | None = None,
                 refuses: Callable[[str], bool] = lambda _config: False,
                 finishes_reloads: bool = True) -> None:
        self.path, self.terminal_command, self.app_id = path, terminal_command, app_id
        self.maps = maps
        self.include, self.refuses, self.finishes_reloads = include, refuses, finishes_reloads
        self.nags: list[subprocess.Popen] = []
        self.face = ({"id": FACE_ID, "type": "con", "app_id": "wlroots", "pid": 4242,
                      "fullscreen_mode": 1, "visible": True} if face else None)
        self.terminal: dict | None = None
        self.commands: list[str] = []
        self.settings: dict[str, str] = {}      # "input type:keyboard xkb_layout" → '"de"'
        self.positions: list[int] = []
        self._subscribers: list[tuple[socket.socket, set[int]]] = []
        self._lock = threading.Lock()
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(path))
        self._listener.listen(8)
        threading.Thread(target=self._serve, daemon=True).start()

    # --- the model ------------------------------------------------------------------
    def tree(self) -> dict:
        tiled = [self.face] if self.face else []
        floating = [self.terminal] if self.terminal else []
        workspace = {"type": "workspace", "name": "1", "nodes": tiled,
                     "floating_nodes": floating}
        output = {"type": "output", "name": "HEADLESS-1",
                  "rect": {"x": 0, "y": 0, "width": 1280, "height": OUTPUT_HEIGHT},
                  "nodes": [workspace]}
        # sway's own scratchpad workspace, which reports itself fullscreen.
        scratch = {"type": "output", "name": "__i3",
                   "nodes": [{"type": "workspace", "name": "__i3_scratch", "id": SCRATCH_ID,
                              "fullscreen_mode": 1, "nodes": [], "floating_nodes": []}]}
        return {"type": "root", "nodes": [scratch, output]}

    def run(self, line: str) -> list[dict]:
        self.commands.append(line)
        if line.startswith(("input ", "output ")):
            return [self._setting(line)]
        if line == "reload":
            threading.Thread(target=self._reload, daemon=True).start()
            return [{"success": True}]
        if line.startswith("exec "):
            if line[len("exec "):] != self.terminal_command:
                return [{"success": False, "error": f"unexpected exec {line!r}"}]
            if self.maps:
                # Ruled into the scratchpad as it maps (`for_window … move scratchpad`).
                self.terminal = {"id": TERMINAL_ID, "type": "floating_con",
                                 "app_id": self.app_id, "visible": False,
                                 "rect": {"x": 0, "y": 0, "width": 640, "height": 480}}
                self._emit(WINDOW_EVENT, {"change": "new", "container": {"app_id": self.app_id}})
            return [{"success": True}]
        match = re.fullmatch(r"\[(app_id=\"\^(?P<app>[^$]+)\$\"|con_id=(?P<con>\d+))\] (?P<verbs>.+)",
                             line)
        if match is None:
            return [{"success": False, "error": f"cannot parse {line!r}"}]
        if match["app"] is not None:
            target = self.terminal if match["app"] == self.app_id else None
        else:
            target = next((n for n in (self.face, self.terminal)
                           if n and n["id"] == int(match["con"])), None)
            if int(match["con"]) == SCRATCH_ID:
                return [{"success": False, "error": "No matching node."}]
        if target is None:
            return [{"success": False, "error": "No matching node."}]
        return [self._verb(target, verb) for verb in match["verbs"].split(", ")]

    def _setting(self, line: str) -> dict:
        """The user's keyboard and scale as sway 1.10 takes them at runtime: a layout or variant
        is compiled, so one no keymap has is refused with xkb's reason; `output * scale` takes
        any number, 0 included."""
        if m := re.fullmatch(r'input type:keyboard (xkb_layout|xkb_variant) "([^"]*)"', line):
            names = m[2].split(",") if m[2] else []
            known = LAYOUTS if m[1] == "xkb_layout" else VARIANTS
            if m[1] == "xkb_layout" and not names or any(n not in known for n in names):
                return {"success": False, "parse_error": False,
                        "error": f"Failed to compile keymap: [XKB-338] Couldn't find file "
                                 f"\"symbols/{m[2]}\" in include paths"}
        elif m := re.fullmatch(r"input type:keyboard (repeat_rate|repeat_delay) (\d+)", line):
            pass
        elif m := re.fullmatch(r"output \* scale (\d+(\.\d+)?)", line):
            pass
        else:
            return {"success": False, "error": f"sway would refuse or misread {line!r}"}
        self.settings[line.rsplit(" ", 1)[0]] = line.rsplit(" ", 1)[1]
        return {"success": True}

    def _verb(self, node: dict, verb: str) -> dict:
        if verb == "move scratchpad":
            node.update(visible=False)
        elif verb == "scratchpad show":
            if node["visible"]:
                node["visible"] = False
            else:
                node["visible"] = True
                if self.face:
                    self.face["fullscreen_mode"] = 0
        elif verb == "fullscreen enable":
            node["fullscreen_mode"] = 1
        elif m := re.fullmatch(r"resize set 100 ppt (\d+) ppt", verb):
            node["rect"]["height"] = OUTPUT_HEIGHT * int(m[1]) // 100
        elif m := re.fullmatch(r"move position 0 px (-?\d+) px", verb):
            node["rect"]["y"] = int(m[1])
            self.positions.append(int(m[1]))
        else:
            return {"success": False, "error": f"sway would refuse or misread {verb!r}"}
        return {"success": True}

    def _reload(self) -> None:
        config = self.include.read_text() if self.include and self.include.is_file() else ""
        if self.refuses(config):
            self.nags.append(nag(self.path.parent))
        if self.finishes_reloads:
            self._emit(WORKSPACE_EVENT, {"change": "reload"})

    def _emit(self, kind: int, event: dict) -> None:
        body = json.dumps(event).encode()
        for sub in list(self._subscribers):
            conn, kinds = sub
            if kind not in kinds:
                continue
            try:
                conn.sendall(MAGIC + HEADER.pack(len(body), kind) + body)
            except OSError:
                self._subscribers.remove(sub)

    # --- the socket -----------------------------------------------------------------
    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn: socket.socket) -> None:
        while True:
            head = _recv(conn, len(MAGIC) + HEADER.size)
            if head is None:
                conn.close()
                return
            length, kind = HEADER.unpack(head[len(MAGIC):])
            payload = (_recv(conn, length) or b"").decode()
            with self._lock:
                if kind == RUN_COMMAND:
                    reply: object = [r for line in payload.split("; ") for r in self.run(line)]
                elif kind == GET_TREE:
                    reply = self.tree()
                elif kind == SUBSCRIBE:
                    wanted = json.loads(payload)
                    reply = {"success": bool(wanted) and set(wanted) <= set(EVENTS)}
                else:
                    reply = {"success": False, "error": f"type {kind} is not modelled"}
            body = json.dumps(reply).encode()
            conn.sendall(MAGIC + HEADER.pack(len(body), kind) + body)
            if kind == SUBSCRIBE and reply["success"]:
                self._subscribers.append((conn, {EVENTS[e] for e in wanted}))
                return

    def close(self) -> None:
        self._listener.close()
        for process in self.nags:
            process.kill()
            process.wait()


def nag(directory: Path) -> subprocess.Popen:
    """A process the kernel names `swaynag`, as sway's own report of a config error is."""
    link = directory / "swaynag"
    if not link.exists():
        link.symlink_to(subprocess.run(("sh", "-c", "command -v sleep"), capture_output=True,
                                       text=True, check=True).stdout.strip())
    return subprocess.Popen((str(link), "30"))


def _recv(conn: socket.socket, n: int) -> bytes | None:
    data = b""
    while len(data) < n:
        chunk = conn.recv(n - len(data))
        if not chunk:
            return None
        data += chunk
    return data
