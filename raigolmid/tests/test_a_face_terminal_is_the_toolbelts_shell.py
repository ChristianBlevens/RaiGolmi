"""A face's terminal: the shell is a toolbelt's — the sandbox named, or the focused
one — through its view's launcher, which raigolmid opens for it (`open_launcher`).

The launchers are real, on real sockets. The socket closes the same way whether the shell
exited or its view went, so what these hold is that the terminal asks the launcher which,
and that a recreated view's launcher — which numbers its processes from 1 again — is never
mistaken for the one that started the shell.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import threading
import time

import pytest

import rai.__main__ as rai
from raigolmid.launcher.client import LauncherClient
from raigolmid.launcher.server import LauncherServer
from raigolmid.paths import Paths
from tests.harness import launcher_broker

def _serve(sock: Path) -> LauncherServer:
    server = LauncherServer(str(sock), instance="test@tab-2")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _stop(server: LauncherServer) -> None:
    server.shutdown()
    server.server_close()


class _Keyboard:
    """This terminal's input: a pipe held open, so only the far side can end the session."""

    def __init__(self) -> None:
        self.read_fd, self.write_fd = os.pipe()

    def fileno(self) -> int:
        return self.read_fd

    def isatty(self) -> bool:
        return False


def test_a_recreated_view_is_not_the_launcher_that_started_the_shell(tmp_path):
    sock_path = tmp_path / "view.sock"
    first = _serve(sock_path)
    client = LauncherClient(sock_path)
    sock, proc, issued_by = client.open_pty(["/bin/sh", "-c", "sleep 30"], cwd="/tmp")
    client.signal(proc, 9)
    sock.close()
    _stop(first)

    # The new view's launcher, on the same path, with a process of the same number.
    second = _serve(sock_path)
    try:
        other, same_number, _ = client.open_pty(["/bin/sh", "-c", "sleep 5"], cwd="/tmp")
        assert same_number == proc
        with pytest.raises(rai._ViewEnded, match="recreated"):
            rai._how_it_ended(client, issued_by, proc)
        other.close()
        client.signal(same_number, 9)
    finally:
        _stop(second)


def test_a_view_that_is_gone_ended_its_shell(tmp_path):
    sock_path = tmp_path / "view.sock"
    server = _serve(sock_path)
    client = LauncherClient(sock_path)
    sock, proc, issued_by = client.open_pty(["/bin/sh", "-c", "sleep 30"], cwd="/tmp")
    client.signal(proc, 9)
    sock.close()
    _stop(server)

    with pytest.raises(rai._ViewEnded, match="gone"):
        rai._how_it_ended(client, issued_by, proc)


def _face(tmp_path, monkeypatch, focused: str) -> Paths:
    """The environment `rai` has in a face: its own socket and `focused`, and the daemon's
    launcher sockets where the broker finds them."""
    monkeypatch.setenv("RAIGOLMID_VIEW_SOCKET_DIR", str(tmp_path / "views"))
    monkeypatch.setenv("RAIGOLMID_SOCKET", str(tmp_path / "raigolmid.sock"))
    monkeypatch.setenv("RAIGOLMID_FOCUSED", str(tmp_path / "focused"))
    (tmp_path / "views").mkdir(exist_ok=True)
    (tmp_path / "focused").write_text(focused)
    return Paths.from_env()


def _shells_exit(monkeypatch, code: int) -> list[list[str]]:
    """Every shell the terminal opens is one that exits with `code`; what it asked for is kept."""
    opened: list[list[str]] = []
    real_open = LauncherClient.open_pty

    def open_pty(self, cmd, **kwargs):
        opened.append(cmd)
        return real_open(self, ["/bin/sh", "-c", f"exit {code}"], **{**kwargs, "cwd": "/tmp"})

    monkeypatch.setattr(LauncherClient, "open_pty", open_pty)
    return opened


def test_the_terminal_opens_on_the_focused_view_and_ends_with_its_shell(
        tmp_path, monkeypatch, capfd):
    paths = _face(tmp_path, monkeypatch, "demo@tab-2 0123abcd\n")
    server, broker = _serve(paths.launcher_socket("demo@tab-2")), launcher_broker(paths)
    opened = _shells_exit(monkeypatch, 5)
    monkeypatch.setattr("sys.stdin", _Keyboard())
    try:
        assert rai.cmd_terminal(argparse.Namespace(instance=None)) == 5
    finally:
        _stop(server)
        _stop(broker)
    assert opened == [[rai.TOOLBELT_SHELL]]


def test_a_named_sandbox_opens_its_own_shell_whatever_is_focused(
        tmp_path, monkeypatch, capfd):
    """A face works with every body: the shell is the named sandbox's, not the focused one's,
    whose launcher would answer the same request with a different exit code."""
    paths = _face(tmp_path, monkeypatch, "demo@tab-2 0123abcd\n")
    focused = LauncherServer(str(paths.launcher_socket("demo@tab-2")), instance="demo@tab-2")
    other = LauncherServer(str(paths.launcher_socket("api@tab-3")), instance="api@tab-3")
    for server in (focused, other):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    broker = launcher_broker(paths)
    real_open = LauncherClient.open_pty

    def open_pty(self, cmd, **kwargs):
        code = 7 if self.instance == "api@tab-3" else 5
        return real_open(self, ["/bin/sh", "-c", f"exit {code}"], **{**kwargs, "cwd": "/tmp"})

    monkeypatch.setattr(LauncherClient, "open_pty", open_pty)
    monkeypatch.setattr("sys.stdin", _Keyboard())
    try:
        assert rai.cmd_terminal(argparse.Namespace(instance="api@tab-3")) == 7
    finally:
        for server in (focused, other, broker):
            _stop(server)


def _refocus_after(path: Path, line: str, seconds: float) -> None:
    def later():
        time.sleep(seconds)
        path.write_text(line)
    threading.Thread(target=later, daemon=True).start()


def _waits_for(tmp_path, monkeypatch, capsys, line: str) -> bool:
    paths = _face(tmp_path, monkeypatch, "")
    ready = rai._focus_back(paths, "demo@tab-2", "old")
    read_fd, keyboard = os.pipe()
    monkeypatch.setattr("sys.stdin", os.fdopen(read_fd))
    _refocus_after(tmp_path / "focused", line, 0.5)
    done: list[bool] = []
    waiter = threading.Thread(
        target=lambda: done.append(rai._await_a_shell("the focused toolbelt", "why", ready)),
        daemon=True)
    waiter.start()
    waiter.join(timeout=3)
    opened_by_itself = bool(done) and done[0]
    os.close(keyboard)  # Ctrl-D: a terminal still waiting ends here
    waiter.join(timeout=3)
    assert not waiter.is_alive()
    return opened_by_itself


def test_the_same_instance_back_on_a_new_view_opens_a_shell_by_itself(
        tmp_path, monkeypatch, capsys):
    assert _waits_for(tmp_path, monkeypatch, capsys, "demo@tab-2 new\n")
    assert "opening a shell" in capsys.readouterr().err


def test_another_instance_focused_waits_for_the_user(tmp_path, monkeypatch, capsys):
    assert not _waits_for(tmp_path, monkeypatch, capsys, "other@tab-2 new\n")
