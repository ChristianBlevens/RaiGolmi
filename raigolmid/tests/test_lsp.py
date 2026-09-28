"""`rai lsp`'s rewrite and framing, and the whole pipe through a real launcher."""
from __future__ import annotations

import io
import json
import os
import sys
import threading
from pathlib import Path

import pytest

from raigolmid import lsp
from raigolmid.launcher.client import LauncherClient
from raigolmid.launcher.server import LauncherServer
from tests.harness import launcher_broker


@pytest.mark.parametrize("uri", ["file:///work", "file:///work/app/main.py",
                                 "file:///nix/store/abc-pyright/typeshed/json/__init__.pyi"])
def test_the_working_copy_and_the_store_are_the_same_path_in_both(uri):
    assert lsp.uri_to_editor(uri) == uri
    assert lsp.uri_to_server(uri) == uri


def test_only_whole_path_components_count():
    assert lsp.uri_to_editor("file:///workshop/x") == "file:///body/workshop/x"
    assert lsp.uri_to_server("file:///bodyguard/x") == "file:///bodyguard/x"
    assert lsp.uri_to_server("file:///body") == "file:///"


def test_uris_are_rewritten_wherever_they_sit_including_keys():
    edit = {"changes": {"file:///usr/lib/a.py": [{"newText": "file:///usr/lib/b.py"}]},
            "label": "see file:///usr/lib/c.py", "n": 3, "flag": True, "none": None}
    assert lsp.rewrite(edit, lsp.uri_to_editor) == {
        "changes": {"file:///body/usr/lib/a.py": [{"newText": "file:///body/usr/lib/b.py"}]},
        "label": "see file:///usr/lib/c.py", "n": 3, "flag": True, "none": None}


def test_the_editors_pid_does_not_reach_the_server():
    """The server would watch a pid from another namespace and exit when it is not there."""
    init = {"id": 1, "method": "initialize",
            "params": {"processId": 4242, "rootUri": "file:///work"}}
    assert lsp.to_server(init)["params"] == {"processId": None, "rootUri": "file:///work"}


def test_framing_round_trips_and_counts_bytes_not_characters():
    out = io.BytesIO()
    lsp.write_message(out, "{\"é\":1}".encode())
    assert out.getvalue().startswith(b"Content-Length: 8\r\n\r\n")
    assert lsp.read_message(io.BytesIO(out.getvalue())) == "{\"é\":1}".encode()


# A server that answers each request with a location in a body library, and says which
# document it was asked about — so both directions are seen from the other end.
SERVER = r'''
import json, sys
def read():
    n = None
    while (line := sys.stdin.buffer.readline()) not in (b"\r\n", b""):
        if line.lower().startswith(b"content-length"):
            n = int(line.split(b":")[1])
    return None if n is None else json.loads(sys.stdin.buffer.read(n))
while (m := read()) is not None:
    body = json.dumps({"id": m["id"], "result": {
        "asked": m["params"]["textDocument"]["uri"],
        "uri": "file:///usr/lib/lib.py"}}).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    sys.stdout.buffer.flush()
'''


def test_the_pipe_through_a_launcher_rewrites_both_ways(tmp_path):
    (tmp_path / "server.py").write_text(SERVER)
    sock_path = tmp_path / "view.sock"
    launcher = LauncherServer(str(sock_path), instance="t@tab-2", generation=1)
    threading.Thread(target=launcher.serve_forever, daemon=True).start()
    try:
        sock, _ = LauncherClient(sock_path).open_stream(
            [sys.executable, str(tmp_path / "server.py")], cwd=str(tmp_path))
        editor_in_r, editor_in_w = os.pipe()
        editor_out = io.BytesIO()
        with os.fdopen(editor_in_w, "wb") as editor:
            request = {"id": 1, "params": {"textDocument": {
                "uri": "file:///body/usr/lib/other.py"}}}
            lsp.write_message(editor, json.dumps(request).encode())
        with os.fdopen(editor_in_r, "rb") as editor_in:
            lsp.relay(editor_in, editor_out, sock)
        sock.close()
    finally:
        launcher.shutdown()
        launcher.server_close()
    reply = json.loads(lsp.read_message(io.BytesIO(editor_out.getvalue())))
    assert reply["result"] == {"asked": "file:///body/usr/lib/other.py",
                               "uri": "file:///body/usr/lib/lib.py"}


def test_the_shim_reaches_the_view_the_face_shows_and_refuses_when_there_is_none(
        tmp_path, monkeypatch, capsys):
    from raigolmid.paths import Paths

    monkeypatch.setenv("RAIGOLMID_FOCUSED", str(tmp_path / "focused"))
    paths = Paths(state=tmp_path, data=tmp_path, config=tmp_path, runtime=tmp_path)
    paths.focused_view.write_text("app@tab-2 4f2c\n")
    assert lsp.focused(paths) == ("app@tab-2", "4f2c")

    paths.focused_view.write_text("")
    assert lsp.main(paths, ["pyright-langserver", "--stdio"]) == 1
    assert "no sandbox is open" in capsys.readouterr().err


QUITS = r'''
import os, sys, time
flag = sys.argv[1]
while not os.path.exists(flag):
    time.sleep(0.02)
'''


def _shim(tmp_path, paths_dir):
    """`rai lsp` as the editor runs it, over a server that ends when `flag` appears."""
    import subprocess
    (tmp_path / "quits.py").write_text(QUITS)
    program = (
        "import sys\n"
        "from raigolmid import lsp\n"
        "from raigolmid.paths import Paths\n"
        # The view's /work, which this machine has no need of: the server runs here.
        f"lsp.WORK = {str(tmp_path)!r}\n"
        f"p = Paths(state={str(paths_dir)!r}, data={str(paths_dir)!r}, "
        f"config={str(paths_dir)!r}, runtime={str(paths_dir)!r})\n"
        f"lsp.main(p, [sys.executable, {str(tmp_path / 'quits.py')!r}, "
        f"{str(tmp_path / 'flag')!r}])\n")
    env = {**os.environ, "RAIGOLMID_FOCUSED": str(paths_dir / "focused"),
           "RAIGOLMID_SOCKET": str(paths_dir / "raigolmid.sock"),
           "PYTHONPATH": str(Path(lsp.__file__).resolve().parents[1])}
    return subprocess.Popen([sys.executable, "-c", program], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)


@pytest.mark.parametrize("view_goes, code", [(False, 1), (True, 0)])
def test_a_server_whose_view_was_replaced_leaves_quietly(tmp_path, monkeypatch, view_goes,
                                                         code):
    """A swap tears the view down before `focused` names the new one, so the stream ends
    exactly as a server crash's does. The launcher tells them apart: gone is the view
    replaced — the editor restarts its servers on the new one — and alive is a crash. The
    launcher is a process here, killed as a view's teardown kills it."""
    import signal
    import subprocess
    import time
    from raigolmid.paths import Paths
    views = tmp_path / "views"
    views.mkdir()
    sock_path = views / "app-tab-2.sock"
    (views / "focused").write_text("app@tab-2 4f2c\n")
    code_root = str(Path(lsp.__file__).resolve().parents[1])
    launcher = subprocess.Popen(
        [sys.executable, "-m", "raigolmid.launcher.server", "--socket", str(sock_path),
         "--instance", "app@tab-2", "--generation", "1"],
        env={**os.environ, "PYTHONPATH": code_root}, stderr=subprocess.DEVNULL)
    for name, value in (("RAIGOLMID_VIEW_SOCKET_DIR", views),
                        ("RAIGOLMID_SOCKET", views / "raigolmid.sock")):
        monkeypatch.setenv(name, str(value))
    broker = launcher_broker(Paths(state=tmp_path, data=tmp_path, config=tmp_path,
                                   runtime=tmp_path))
    proc = None
    try:
        deadline = time.monotonic() + 10
        while not LauncherClient(sock_path).alive(0.2):
            assert time.monotonic() < deadline, "the launcher never listened"
            time.sleep(0.05)
        proc = _shim(tmp_path, views)
        time.sleep(1.0)
        assert proc.poll() is None, proc.stderr.read().decode()
        if view_goes:
            launcher.send_signal(signal.SIGKILL)
            launcher.wait(5)
        else:
            (tmp_path / "flag").write_text("")
        assert proc.wait(timeout=20) == code, proc.stderr.read().decode()
    finally:
        (tmp_path / "flag").write_text("")
        if proc is not None:
            proc.kill()
        launcher.kill()
        launcher.wait(5)
        broker.shutdown()
        broker.server_close()
