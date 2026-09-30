"""The launcher, run for real.

These are not mocks: a launcher server is started on a real Unix socket in a temp dir and
driven through the real client. The protocol's two shapes — framed exec with an exit code,
and a raw PTY — are the two things the rest of the daemon depends on absolutely.
"""
from __future__ import annotations

import os
from pathlib import Path
import socket
import stat
import threading
import time

import pytest

from raigolmid.launcher.client import (LauncherClient, LauncherError, LauncherOutputHeld,
                                       LauncherTimeout)
from raigolmid.launcher.server import LauncherServer
from raigolmid.paths import Paths


@pytest.fixture()
def launcher(tmp_path):
    sock = tmp_path / "view.sock"
    server = LauncherServer(str(sock), instance="test@tab-2", generation=2)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield LauncherClient(sock)
    finally:
        server.shutdown()
        server.server_close()


def test_ping_reports_the_generation_reconciliation_checks(launcher):
    reply = launcher.ping()
    assert reply["ok"] is True
    assert reply["generation"] == 2
    assert reply["instance"] == "test@tab-2"


def test_exec_returns_stdout_and_a_real_exit_code(launcher):
    result = launcher.exec(["/bin/sh", "-c", "echo hello"], cwd="/tmp")
    assert result.exit_code == 0
    assert result.stdout.strip() == "hello"
    assert result.ok


def test_exec_reports_failure_rather_than_swallowing_it(launcher):
    result = launcher.exec(["/bin/sh", "-c", "echo boom >&2; exit 3"], cwd="/tmp")
    assert result.exit_code == 3
    assert "boom" in result.stderr
    assert not result.ok


def test_exec_carries_environment_and_working_directory(launcher, tmp_path):
    result = launcher.exec(["/bin/sh", "-c", "echo $MARKER; pwd"],
                           cwd=str(tmp_path), env={"MARKER": "from-env"})
    assert "from-env" in result.stdout
    assert str(tmp_path) in result.stdout


def test_what_it_starts_finds_the_bodys_programs_before_the_toolbelts(launcher):
    """The toolbelt supplies what the body lacks, so a command's
    `python` is the body's, which sees the body's packages. The launcher's own PATH, the
    toolbelt's first, is not what its children get, and neither is its PYTHONPATH."""
    result = launcher.exec(["/bin/sh", "-c", 'echo "$PATH"; echo "py=$PYTHONPATH"'], cwd="/tmp")
    path, pythonpath = result.stdout.splitlines()
    dirs = path.split(":")
    assert dirs[-1] == "/.toolbelt/bin" and dirs.index("/usr/bin") < dirs.index("/.toolbelt/bin")
    assert pythonpath == "py="


def test_exec_accepts_stdin(launcher):
    result = launcher.exec(["/bin/cat"], cwd="/tmp", stdin="piped\n")
    assert result.stdout == "piped\n"


def test_pty_connection_is_the_terminal(launcher):
    sock, proc_id, _ = launcher.open_pty(["/bin/sh", "-i"], cwd="/tmp")
    try:
        sock.sendall(b"echo pty-works\n")
        deadline = time.time() + 5
        seen = b""
        sock.settimeout(1.0)
        while time.time() < deadline and b"pty-works" not in seen:
            try:
                seen += sock.recv(4096)
            except socket.timeout:
                continue
        assert b"pty-works" in seen
    finally:
        sock.close()
    assert proc_id >= 1


def _read_until(sock, marker, seconds=5.0):
    sock.settimeout(1.0)
    deadline = time.time() + seconds
    seen = b""
    while time.time() < deadline and marker not in seen:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        if not chunk:
            break
        seen += chunk
    return seen


def test_a_stream_carries_bytes_both_ways_untouched(launcher):
    """No terminal between: a `\n` stays a `\n` and nothing is echoed, which is what a
    Content-Length-framed protocol needs."""
    sock, _ = launcher.open_stream(["cat"], cwd="/tmp")
    try:
        payload = b"Content-Length: 7\r\n\r\n{\"a\":1}\n\x00\xff"
        sock.sendall(payload)
        assert _read_until(sock, b"\xff") == payload
    finally:
        sock.close()


def test_a_stream_does_not_deadlock_when_both_pipes_fill(launcher):
    """The child writes a megabyte before it reads any of the megabyte it is sent, so both
    pipes are full at once: a launcher that blocks feeding stdin never drains stdout."""
    sock, _ = launcher.open_stream(["sh", "-c", "head -c 1048576 /dev/zero; "
                                    "head -c 1048576 >/dev/null; echo end"], cwd="/tmp")
    try:
        sock.settimeout(10.0)
        sender = threading.Thread(target=sock.sendall, args=(b"x" * 1048576,), daemon=True)
        sender.start()
        seen = bytearray()
        while not seen.endswith(b"end\n"):
            chunk = sock.recv(1 << 16)
            assert chunk, f"the stream closed after {len(seen)} bytes"
            seen += chunk
        assert len(seen) == 1048576 + 4
        sender.join(5)
        assert not sender.is_alive()
    finally:
        sock.close()


def test_a_stream_closes_when_the_process_exits(launcher):
    sock, proc_id = launcher.open_stream(["sh", "-c", "echo done"], cwd="/tmp")
    try:
        assert _read_until(sock, b"never") == b"done\n"
    finally:
        sock.close()
    deadline = time.time() + 5
    while time.time() < deadline:
        exit_code = {p["proc"]: p for p in launcher.list()}[proc_id]["exit"]
        if exit_code is not None:
            break
        time.sleep(0.05)
    assert exit_code == 0


def test_closing_a_stream_ends_its_process(launcher, tmp_path):
    marker = tmp_path / "pid"
    sock, _ = launcher.open_stream(["sh", "-c", f"echo $$ > {marker}; exec sleep 60"],
                                   cwd="/tmp")
    deadline = time.time() + 5
    while not marker.exists() and time.time() < deadline:
        time.sleep(0.05)
    pid = int(marker.read_text())
    sock.close()
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail("the process outlived its stream")


def test_a_pty_process_reports_itself_as_having_a_tty(launcher):
    sock, _, _ = launcher.open_pty(["/bin/sh", "-c", "test -t 0 && echo IS-A-TTY; sleep 1"],
                                cwd="/tmp")
    try:
        sock.settimeout(5.0)
        seen = b""
        deadline = time.time() + 5
        while time.time() < deadline and b"IS-A-TTY" not in seen:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            seen += chunk
        assert b"IS-A-TTY" in seen
    finally:
        sock.close()


def test_detached_processes_outlive_the_connection(launcher, tmp_path):
    marker = tmp_path / "marker"
    launcher.start_detached(
        ["/bin/sh", "-c", f"sleep 0.4; echo done > {marker}"], cwd="/tmp")
    deadline = time.time() + 5
    while time.time() < deadline and not marker.exists():
        time.sleep(0.1)
    assert marker.exists(), "the process died with the connection that started it"


def _exit_of(launcher, marker: str, within: float = 8.0):
    """The recorded exit of the one process whose command line carries `marker`, once it
    has one; None if it is still running at the deadline."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        found = [p for p in launcher.list() if marker in " ".join(p.get("cmd", []))]
        assert len(found) == 1, found
        if found[0].get("exit") is not None:
            return found[0]["exit"]
        time.sleep(0.1)
    return None


@pytest.mark.parametrize("script, code", [
    ("sleep 30 # quiet", 128 + 15),
    ("trap '' TERM; sleep 30 # deaf", 128 + 9),
])
def test_an_exec_whose_caller_timed_out_is_ended_not_left_running(launcher, script, code):
    """A command that prints nothing never shows the launcher that its caller hung up
    through a failed write, so it has to watch the caller's socket itself. Every editor
    probe that timed out against a blocked nvim was otherwise left running in the view."""
    with pytest.raises(LauncherTimeout):
        launcher.exec(["/bin/sh", "-c", script], cwd="/tmp", timeout=0.5)
    assert _exit_of(launcher, script) == code


def test_an_exec_whose_output_a_background_job_holds_says_the_command_exited(launcher):
    """`&` after a `&&` list backgrounds the whole list, whose shell keeps the output open
    while the command it waits on runs: the command has exited and said what it had to, and
    the timeout says that rather than that something failed."""
    script = "cd /tmp && sleep 30 > /dev/null 2>&1 < /dev/null & echo started"
    # bash, as an agent's `exec` runs it: dash execs the list's last command in its shell's
    # place, which leaves nothing holding the output.
    with pytest.raises(LauncherOutputHeld) as held:
        launcher.exec(["bash", "-c", script], cwd="/tmp", timeout=2.0)
    assert "exited 0" in str(held.value) and "started" in str(held.value)


def test_binary_output_survives_the_framing(launcher):
    result = launcher.exec(
        ["/bin/sh", "-c", "printf '\\xff\\xfe\\x00binary'"], cwd="/tmp")
    assert result.exit_code == 0
    assert "binary" in result.stdout


def test_stream_yields_output_before_the_exit_frame(launcher):
    seen = list(launcher.stream(["/bin/sh", "-c", "echo one; echo two; exit 7"], cwd="/tmp"))
    assert seen[-1] == ("exit", "7")
    text = "".join(chunk for stream, chunk in seen if stream == "stdout")
    assert "one" in text and "two" in text


def _drain_until(sock, needle: bytes, seconds: float = 5.0) -> bytes:
    sock.settimeout(0.5)
    seen = b""
    deadline = time.time() + seconds
    while time.time() < deadline and needle not in seen:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        if not chunk:
            break
        seen += chunk
    return seen


def test_a_pty_process_survives_its_client_detaching_and_can_be_reattached(launcher, tmp_path):
    """The property the launcher is built on: the editor is a child of the launcher, not of the
    connection, so closing `rai attach` — or restarting raigolmid — must not hang it up."""
    marker = tmp_path / "still-alive"
    sock, proc_id, _ = launcher.open_pty(
        ["/bin/sh", "-c", f"echo READY; sleep 2; echo woken > {marker}; sleep 30"],
        cwd="/tmp")
    assert b"READY" in _drain_until(sock, b"READY")
    sock.close()                                   # detach, as a client going away

    time.sleep(3)
    assert marker.exists(), "closing the connection killed the process"

    procs = {p["proc"]: p for p in launcher.list()}
    assert procs[proc_id]["exit"] is None
    assert procs[proc_id]["attached"] is False


def test_reattaching_replays_what_was_missed(launcher):
    sock, proc_id, _ = launcher.open_pty(["/bin/sh", "-i"], cwd="/tmp")
    _drain_until(sock, b"$", 2.0)
    sock.sendall(b"echo before-detach\n")
    assert b"before-detach" in _drain_until(sock, b"before-detach")
    sock.close()

    again, _ = launcher.attach(proc_id)
    try:
        assert b"before-detach" in _drain_until(again, b"before-detach")
    finally:
        again.close()


def test_attaching_to_an_exited_process_says_so_rather_than_hanging(launcher):
    proc_id = launcher.start_detached(["/bin/sh", "-c", "exit 4"], cwd="/tmp")
    deadline = time.time() + 5
    while time.time() < deadline:
        procs = {p["proc"]: p for p in launcher.list()}
        if procs.get(proc_id, {}).get("exit") is not None:
            break
        time.sleep(0.1)

    with pytest.raises(LauncherError, match="exited with 4"):
        launcher.attach(proc_id)


# --- where the sockets live --------------------------------------------

def test_the_daemons_user_can_connect_to_a_socket_bound_by_the_view(tmp_path):
    """The socket arrangement, end to end, in the three privilege states it spans.

    The entrypoint is root *with* capabilities: it binds inside a 0700 directory belonging to
    the user raigolmid runs as — which needs CAP_DAC_OVERRIDE — and gives the socket to that
    user, which needs CAP_CHOWN. The launcher is root with *no* capabilities and could do
    neither, so it adopts the listening fd. raigolmid is that user and connects.

    Each wrong answer fails: 0600 bound by the view is unreachable,
    chown from the launcher raises EPERM, and a 0700 directory makes bind itself fail.
    """
    if os.geteuid() != 0:
        pytest.skip("only root can bind for another uid and then drop to it")
    other_uid = 65534                             # nobody, present in every base image
    ancestor = tmp_path
    while str(ancestor) != "/tmp":                # pytest's temporaries are root-private
        os.chmod(ancestor, 0o755)
        ancestor = ancestor.parent

    directory = tmp_path / "sockets"
    directory.mkdir()
    os.chown(directory, other_uid, other_uid)
    os.chmod(directory, 0o700)                    # the gate: raigolmid's user, and no one else

    sock = directory / "view.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(sock))                      # allowed here: CAP_DAC_OVERRIDE
    listener.listen(16)
    os.chmod(sock, 0o600)
    os.chown(sock, other_uid, other_uid)          # allowed here: CAP_CHOWN
    fd = os.dup(listener.fileno())
    listener.close()

    server = LauncherServer(str(sock), instance="owner@tab-2", generation=3, socket_fd=fd)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:                                # raigolmid's user, not root
        os.close(read_fd)
        try:
            os.setgid(other_uid)
            os.setuid(other_uid)
            reply = LauncherClient(sock).ping()
            os.write(write_fd, str(reply.get("generation", "")).encode())
        except Exception as exc:                  # noqa: BLE001 — reported through the pipe
            os.write(write_fd, f"failed: {exc}".encode())
        finally:
            os.close(write_fd)
            os._exit(0)
    os.close(write_fd)
    try:
        answer = os.read(read_fd, 256).decode()
        os.waitpid(child, 0)
    finally:
        os.close(read_fd)
        server.shutdown()
        server.server_close()

    assert answer == "3", (
        f"uid {other_uid} could not reach a launcher that adopted a socket bound for it: "
        f"{answer!r} — raigolmid would see this view as unreachable"
    )


def test_the_socket_directory_is_the_gate(tmp_path, monkeypatch):
    """The socket is mode-open, so the directory is the only thing keeping it private."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    paths = Paths.from_env()
    paths.view_sockets.mkdir(parents=True, exist_ok=True)
    paths.view_sockets.chmod(0o755)               # a looser umask, or an older version
    paths.ensure()
    assert stat.S_IMODE(paths.view_sockets.stat().st_mode) == 0o700, \
        "the socket directory is not private, so a mode-open socket inside it is reachable"


def test_the_launcher_never_calls_chown() -> None:
    """The launcher runs with no capabilities, so chown to another uid is EPERM.

    This is asserted statically because it cannot be asserted dynamically here: the test
    process is root *with* CAP_CHOWN, so a chown succeeds in the suite and fails only inside
    a real view, which a test of the happy path never reaches.
    """
    source = (Path(__file__).resolve().parents[1]
              / "raigolmid" / "launcher" / "server.py").read_text(encoding="utf-8")
    offenders = [f"line {n}: {line.strip()}"
                 for n, line in enumerate(source.splitlines(), 1)
                 if "chown" in line and not line.lstrip().startswith("#")]
    assert not offenders, (
        "the launcher cannot chown — it has dropped every capability by the time it binds: "
        + "; ".join(offenders)
    )


def test_a_resize_reaches_the_process_through_its_own_terminal(launcher):
    """The editor starts detached at a size nobody chose; the window that attaches later
    sets the real one. `stty size` reads it back from the process's side of the pty."""
    sock, proc_id, _ = launcher.open_pty(["/bin/sh", "-c", "sleep 0.5; stty size; sleep 1"],
                                      cwd="/tmp", rows=40, cols=120)
    try:
        launcher.resize(proc_id, 31, 97)
        sock.settimeout(5.0)
        seen = b""
        deadline = time.time() + 5
        while time.time() < deadline and b"31 97" not in seen:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            seen += chunk
        assert b"31 97" in seen, seen
    finally:
        sock.close()


