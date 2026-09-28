"""A face container backed by a real process holding its display.

What a face's display is, is what the kernel reports through `/proc/<pid>/net/unix` for the
face's own network namespace (`faces.listening_unix_sockets`), so a face in the fake runtime
is backed by a process that really listens on `wayland-2` in the face's runtime dir from a
network namespace of its own — as a compositor in a container does. `Faces` then finds the
display the way it does on the machine, and a face that is gone has no display.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from raigolmid import labels
from raigolmid.faces import FaceError
from raigolmid.runtime.base import ContainerSpec
from tests.fakeruntime import FakeRuntime

# The name wlroots gives the first display in a runtime dir holding none but the host's.
DISPLAY = "wayland-2"

# Binds its own display and, when given a second path, also *connects* to that one — which
# is what a face really is: a compositor listening on its own socket and a client of the
# host's. Both endpoints carry a path in /proc/net/unix and only one of them is listening.
_BIND = ("import socket,sys,time\n"
         "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)\n"
         "s.bind(sys.argv[1]); s.listen(1)\n"
         "if len(sys.argv) > 2:\n"
         "    c=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); c.connect(sys.argv[2])\n"
         "print('ready',flush=True)\n"
         "time.sleep(300)")


def _unshare_net_command() -> list[str] | None:
    """How to get a private network namespace here, or None if there is no way.

    A plain `unshare --net` needs CAP_SYS_ADMIN; adding a user namespace gets the same
    thing unprivileged where unprivileged user namespaces are allowed. Both are tried
    because the suite runs as root in the dev container and as an ordinary user on the
    host, and the question is worth asking in both.
    """
    for candidate in (["unshare", "--net"], ["unshare", "--user", "--map-root-user", "--net"]):
        if subprocess.run([*candidate, "true"], capture_output=True).returncode == 0:
            return candidate
    return None


UNSHARE_NET = _unshare_net_command()

# Every backing process started, so none outlives its test (`conftest.py`).
_started: list[subprocess.Popen] = []


def holder_process(path: Path, *, own_netns: bool = False,
                   connect_to: Path | None = None) -> subprocess.Popen:
    """A second process really listening on a unix socket.

    Really, rather than a fixture that claims it: what is under test is what the kernel
    reports through `/proc/<pid>/net/unix`, and a fake holder would be testing the fake.
    `own_netns` puts it in its own network namespace, which is what a face container is —
    and the only thing that makes the socket attributable to it rather than to everything
    else on the host. `unshare` execs rather than forks, so the pid is the holder's.
    """
    argv = [sys.executable, "-c", _BIND, str(path)]
    if connect_to is not None:
        argv.append(str(connect_to))
    if own_netns:
        if UNSHARE_NET is None:
            pytest.skip("a face is a process in its own network namespace, and neither "
                        "`unshare --net` nor `unshare --user --map-root-user --net` "
                        "works here")
        argv = [*UNSHARE_NET, *argv]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True)
    _started.append(proc)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if proc.stdout and proc.stdout.readline().startswith("ready"):
            return proc
        if proc.poll() is not None:
            raise AssertionError(f"the socket holder on {path} exited "
                                 f"{proc.returncode} before it was listening")
    raise AssertionError(f"the socket holder on {path} never reported ready")


def _compositor(spec: ContainerSpec) -> subprocess.Popen:
    """The face's display, in the runtime dir `Faces._launch` gives it and later waits on."""
    if "XDG_RUNTIME_DIR" not in spec.environment:
        raise AssertionError(f"face container {spec.name} names no XDG_RUNTIME_DIR, so it "
                             "has nowhere to open a display")
    return holder_process(Path(spec.environment["XDG_RUNTIME_DIR"]) / DISPLAY,
                          own_netns=True)


def back_faces(runtime: FakeRuntime) -> None:
    """Every face `runtime` starts, the user's and a trial, backed by its own display."""
    for role in (labels.Role.FACE, labels.Role.FACE_TRIAL):
        runtime.backing[str(role)] = _compositor


def end_all() -> None:
    """End every holder still running, as the machine does the containers of a test run."""
    while _started:
        proc = _started.pop()
        proc.terminate()
        with proc:
            pass


class NestedSway:
    """A face's own compositor (`Faces._nested_compositor`): a window for each command
    spawned in the face that `maps` says opens one, and none for any other, as an app that
    dies at start never maps."""

    def __init__(self, runtime, maps=lambda command: True):
        self.runtime, self.maps = runtime, maps
        self.commands: list[str] = []

    def command(self, *words):
        """The user's keyboard is the one thing a started face's sway is told; anything else is
        a command this double does not model, refused as sway refuses one it cannot parse."""
        line = " ".join(words)
        if not line.startswith("input type:keyboard "):
            raise FaceError(f"the host compositor refused '{line}': not modelled")
        self.commands.append(line)

    def tree(self):
        windows = [{"id": 100 + n, "pid": 1000 + n, "nodes": []}
                   for n, (_, command, _) in enumerate(self.runtime.spawn_log)
                   if self.maps(" ".join(command))]
        return {"id": 1, "nodes": windows}
