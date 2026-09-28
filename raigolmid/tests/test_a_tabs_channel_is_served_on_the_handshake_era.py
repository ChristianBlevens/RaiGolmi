"""Claude Code takes a channel only on the handshake era, and asks for the 2026-07-28 revision
first when a feature flag of its own says so (`mcp_server.serve_with_channel`). The real
server, over stdio: the modern probe is refused, the handshake declares the channel, and what
`channel_take` hands out is pushed."""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

SERVER = textwrap.dedent("""
    from raigolmid import mcp_server
    from raigolmid.client import ApiError

    class Client:
        taken = False
        def call(self, method, **params):
            if method != "channel_take":
                raise ApiError(f"unknown method {method!r}")
            if not Client.taken:
                Client.taken = True
                return {"seq": 1, "content": "hello", "meta": {"seq": "1"}}
            return None

    mcp_server.serve_with_channel(Client(), mcp_server.build_machine_server(Client()))
""")


def _line(proc) -> dict:
    return json.loads(proc.stdout.readline())


def _send(proc, message: dict) -> None:
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()


def test_the_modern_probe_is_refused_and_the_handshake_carries_the_channel():
    raigolmid = Path(__file__).resolve().parents[1]
    proc = subprocess.Popen([sys.executable, "-B", "-c", SERVER], cwd=raigolmid, text=True,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env={"PYTHONPATH": str(raigolmid)})
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {
            "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}})
        assert _line(proc)["error"]["code"] == -32601
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "claude-code", "version": "0"}}})
        result = _line(proc)["result"]
        assert result["protocolVersion"] == "2025-11-25"
        assert "claude/channel" in result["capabilities"]["experimental"]
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        pushed = _line(proc)
        assert pushed["method"] == "notifications/claude/channel"
        assert pushed["params"] == {"content": "hello", "meta": {"seq": "1"}}
    finally:
        proc.kill()
        proc.wait()


BROKEN = textwrap.dedent("""
    from raigolmid import mcp_server
    from raigolmid.client import ApiError

    class Client:
        def call(self, method, **params):
            if method != "channel_take":
                raise ApiError(f"unknown method {method!r}")
            return {"seq": 1}                     # no content: a reply the push cannot carry

    mcp_server.serve_with_channel(Client(), mcp_server.build_machine_server(Client()))
""")


def test_a_push_that_fails_ends_the_server_with_its_reason():
    """Anything but the daemon being away ends the process: left to the task group, the
    server waited forever on stdin's reader with its tools and channel dead."""
    raigolmid = Path(__file__).resolve().parents[1]
    proc = subprocess.Popen([sys.executable, "-B", "-c", BROKEN], cwd=raigolmid, text=True,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env={"PYTHONPATH": str(raigolmid)})
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "claude-code", "version": "0"}}})
        _line(proc)
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert proc.wait(timeout=10) == 70
        assert "KeyError: 'content'" in proc.stderr.read()
    finally:
        proc.kill()
        proc.wait()


def test_the_server_exits_when_the_session_ends_during_a_take():
    """Stdin's end is the session's. A take still waiting on the daemon holds nothing
    anybody is left to hear, so it does not keep the process up."""
    raigolmid = Path(__file__).resolve().parents[1]
    slow = SERVER.replace("return None\n", "__import__('time').sleep(60)\n")
    proc = subprocess.Popen([sys.executable, "-B", "-c", slow], cwd=raigolmid, text=True,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env={"PYTHONPATH": str(raigolmid)})
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "claude-code", "version": "0"}}})
        _line(proc)
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _line(proc)                       # the first push; the next take sleeps
        __import__("time").sleep(2.0)     # past the push loop's 1 s pause, into that take
        proc.stdin.close()
        assert proc.wait(timeout=10) == 0
    finally:
        proc.kill()
        proc.wait()
