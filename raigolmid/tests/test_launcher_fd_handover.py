"""A listening socket survives the hand-over the view's entrypoint performs.

The entrypoint binds the socket while it still has capabilities and the launcher adopts the
descriptor, because a launcher with an empty capability set can neither create the socket in
raigolmid's directory nor chown it afterwards. Between the two sits `capsh -- -c 'exec python3
…'`: two execs, either of which losing the descriptor leaves a bound socket with nothing
listening on it, which a client sees as "connection refused" — a view that looks present and
answers nothing.

The descriptor can be lost where no unit test looks: moved to a chosen number with `dup2`,
when the socket already has that number `dup2(fd, fd)` does nothing, so closing the socket
object closes the descriptor it was preserving. The chain is
real here — capsh, bash and python are all present — so it is exercised rather than reasoned
about.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from raigolmid.launcher.client import LauncherClient

REPO = Path(__file__).resolve().parents[1]
CAPSH = shutil.which("capsh")


def bind_as_the_entrypoint_does(path: Path) -> int:
    """The same sequence as `viewinit.bind_for_the_launcher`, ending in a detached fd."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(64)
    os.chmod(path, 0o600)
    fd = listener.detach()
    os.set_inheritable(fd, True)
    return fd


@pytest.mark.skipif(CAPSH is None, reason="capsh is part of the toolbelt, absent here")
def test_the_launcher_answers_on_a_socket_handed_through_capsh(tmp_path):
    sock = tmp_path / "view.sock"
    fd = bind_as_the_entrypoint_does(sock)

    # Exactly the shape viewinit uses: capsh drops capabilities, its shell execs python.
    command = (f"exec {sys.executable} -m raigolmid.launcher.server "
               f"--socket '{sock}' --socket-fd {fd} --instance 'fd@tab-2' --generation 5")
    child = subprocess.Popen(
        [CAPSH, "--no-new-privs", "--caps=", f"--shell={shutil.which('bash') or '/bin/bash'}",
         "--", "-c", command],
        pass_fds=(fd,),
        env=dict(os.environ, PYTHONPATH=str(REPO), PYTHONDONTWRITEBYTECODE="1"),
        stderr=subprocess.PIPE,
    )
    os.close(fd)
    try:
        client = LauncherClient(sock)
        deadline = time.time() + 20
        reply = None
        while time.time() < deadline:
            if child.poll() is not None:
                break
            try:
                reply = client.ping()
                break
            except Exception:                      # noqa: BLE001 — still starting
                time.sleep(0.2)
        if reply is None:
            child.terminate()
            _, err = child.communicate(timeout=10)
            pytest.fail("the launcher never answered on the handed-over socket; its stderr "
                        f"was: {err.decode(errors='replace')[-800:]}")
        assert reply["generation"] == 5
        assert reply["instance"] == "fd@tab-2"
    finally:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()


@pytest.mark.skipif(CAPSH is None, reason="capsh is part of the toolbelt, absent here")
def test_the_handed_over_socket_keeps_the_mode_the_entrypoint_set(tmp_path):
    """Adoption must not rebind: a rebind replaces the file the entrypoint prepared.

    That happened — the socket came back 0755 and root-owned after adoption, because a stray
    `bind` was left above the branch that adopts. Every other test passed, since as root with
    capabilities the rebind succeeds.
    """
    from raigolmid.launcher.server import LauncherServer

    sock = tmp_path / "view.sock"
    fd = bind_as_the_entrypoint_does(sock)
    before = os.stat(sock)
    server = LauncherServer(str(sock), instance="mode@tab-2", generation=1, socket_fd=fd)
    try:
        after = os.stat(sock)
        assert (after.st_ino, after.st_mode) == (before.st_ino, before.st_mode), \
            "adopting the socket replaced it, so the entrypoint's ownership and mode are gone"
    finally:
        server.server_close()
