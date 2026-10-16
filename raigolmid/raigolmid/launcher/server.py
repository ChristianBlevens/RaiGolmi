"""The in-view launcher.

This is the session view's long-lived process. The view's entrypoint builds the bind-mount
tree, pivot_roots into it, and drops every capability before exec'ing this — so every
process this starts is a child of an already-unprivileged process and inherits the
switched root, the empty capability sets, and `no_new_privs`. That inheritance is the
whole security argument, and it is why no process enters a view through `docker exec`,
which would restore the container's configured capabilities and miss the root switch.

The socket lives on the host side of a bind mount, so raigolmid can reconnect to a view it
did not create and a daemon restart never disturbs a running shell.

Written rather than adopted: the requirements — start after pivot_root
with zero capabilities under no_new_privs, PTY only on request, real exit codes — are
each unusual, and every candidate surveyed failed one of the unusual ones rather than one
of the ordinary ones.

It depends on nothing outside the standard library, because its dependencies would have to
be in the closure mounted into every view.
"""
from __future__ import annotations

import errno
import fcntl
import os
import pty
import select
import signal
import shutil
import socket
import socketserver
import struct
import sys
import termios
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import protocol

READ_SIZE = 65536
# How long a piped child whose caller hung up gets between SIGTERM and SIGKILL.
ABANDON_GRACE = 2.0

# What a reattaching client is shown of what it missed. Bounded because a chatty
# process left unattached for a day must not grow the launcher without limit.
SCROLLBACK_BYTES = 256 * 1024


# Every process a launcher child starts carries its tree's token in this variable, so the
# children that leave its session (`setsid`, a daemon's double fork) are still found as its.
TREE_VAR = "RAIGOLMI_TREE"


def tree_of(proc: "Process") -> dict[int, int]:
    """The live processes of `proc`'s tree, each with its CPU ticks: those in its session
    while it is unreaped, and any carrying its token in its environment. Read from /proc as
    this launcher's uid with no capability, so a process that clears its environment and
    leaves the session, overwrites its environment (setproctitle), or makes itself
    non-dumpable — whose environment only CAP_SYS_PTRACE may read — is not found."""
    me = os.getpid()
    token = f"\0{TREE_VAR}={proc.tree}\0".encode()
    found: dict[int, int] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == me:
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                stat = f.read()
        except OSError:
            continue                    # exited between the listing and the read
        # After the command, which may itself hold ')': state, ppid, pgrp, session, ...; the
        # 12th to 15th fields after it are utime, stime, cutime, cstime (proc(5)).
        fields = stat[stat.rfind(")") + 2:].split()
        if fields[0] in ("Z", "X"):
            continue
        mine = proc.exit_code is None and int(fields[3]) == proc.pid
        if not mine:
            try:
                with open(f"/proc/{entry}/environ", "rb") as f:
                    mine = token in b"\0" + f.read()
            except OSError:
                continue                # exited, or not this uid's to read
        if mine:
            found[int(entry)] = sum(int(x) for x in fields[11:15])
    return found


def end_tree(proc: "Process") -> list[int]:
    """Ends `proc`'s whole tree: SIGTERM, with SIGCONT so a paused process takes it, then
    SIGKILL to what is left after `ABANDON_GRACE`. Every process in it runs as this
    launcher's uid, so signalling needs no capability. The pids still alive after both,
    which only a process stuck in the kernel can be."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        pids = tree_of(proc)
        for pid in pids:
            for each in ((sig, signal.SIGCONT) if sig == signal.SIGTERM else (sig,)):
                try:
                    os.kill(pid, each)
                except ProcessLookupError:
                    break
        deadline = time.monotonic() + ABANDON_GRACE
        while pids and time.monotonic() < deadline:
            time.sleep(0.05)
            pids = tree_of(proc)
        if not pids:
            return []
    return sorted(tree_of(proc))


@dataclass
class Process:
    """A launcher child.

    A PTY process owns its master fd for its whole life, not for the life of the
    connection that started it. Closing the master would hang up the terminal and
    SIGHUP the child, which would make every shell die the moment raigolmid let go of
    it — the opposite of the design, where the launcher's children outlive their
    connections and the daemon. So detaching only unbinds `conn`, and `scrollback`
    keeps what was printed while nobody was attached, so a reattach shows it.
    """
    proc_id: int
    pid: int
    cmd: tuple[str, ...]
    pty: bool
    # Its value of `TREE_VAR`.
    tree: str
    started: float = field(default_factory=time.time)
    exit_code: int | None = None
    master_fd: int | None = None
    conn: socket.socket | None = None
    scrollback: bytearray = field(default_factory=bytearray)
    # Every byte it has printed, which the scrollback stops counting once it is full.
    printed: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def to_wire(self) -> dict[str, Any]:
        return {"proc": self.proc_id, "pid": self.pid, "cmd": list(self.cmd),
                "pty": self.pty, "started": self.started, "exit": self.exit_code,
                "attached": self.conn is not None, "printed": self.printed,
                "cpu": None if self.exit_code is not None else self.cpu()}

    def cpu(self) -> float | None:
        """CPU seconds used by its tree: each live process's own, and what each has reaped
        from its children. None when the tree is gone."""
        ticks = tree_of(self)
        return sum(ticks.values()) / os.sysconf("SC_CLK_TCK") if ticks else None

    def record(self, data: bytes) -> None:
        with self.lock:
            self.printed += len(data)
            self.scrollback.extend(data)
            if len(self.scrollback) > SCROLLBACK_BYTES:
                del self.scrollback[:len(self.scrollback) - SCROLLBACK_BYTES]

    def replay(self) -> bytes:
        """What this process has printed so far, without attaching to it."""
        with self.lock:
            return bytes(self.scrollback)

    def attach(self, conn: socket.socket) -> bytes:
        with self.lock:
            self.conn = conn
            return bytes(self.scrollback)
        # The previous connection is left to notice its own detachment; closing it here
        # would race its own read loop.

    def detach(self, conn: socket.socket) -> None:
        with self.lock:
            if self.conn is conn:
                self.conn = None

    def write_out(self, data: bytes) -> None:
        with self.lock:
            conn = self.conn
        if conn is None:
            return
        try:
            conn.sendall(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.detach(conn)

# What the user and the agents run finds the body's programs first; the toolbelt supplies what the
# body lacks. The launcher itself keeps the toolbelt first
# (`viewinit.py`), since it runs on the toolbelt's python3 whatever the body carries.
CHILD_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/.toolbelt/bin"


class Registry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next = 1
        self._procs: dict[int, Process] = {}

    def add(self, pid: int, cmd: tuple[str, ...], use_pty: bool, tree: str,
            master_fd: int | None = None) -> Process:
        with self._lock:
            proc = Process(proc_id=self._next, pid=pid, cmd=cmd, pty=use_pty, tree=tree,
                           master_fd=master_fd)
            self._procs[proc.proc_id] = proc
            self._next += 1
            return proc

    def get(self, proc_id: int) -> Process | None:
        with self._lock:
            return self._procs.get(proc_id)

    def finish(self, proc_id: int, exit_code: int) -> None:
        with self._lock:
            if proc_id in self._procs:
                self._procs[proc_id].exit_code = exit_code

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [p.to_wire() for p in self._procs.values()]


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _executable(cmd0: str, env: dict[str, str]) -> str:
    """The program `cmd0` names, looked up on the request's PATH here rather than in the child:
    the server forks from a threaded process, so the child does nothing but thin system calls
    before exec, never take a lock another thread may have held at the fork. One not found is
    left to the child's exec to fail as it would have."""
    if "/" in cmd0:
        return cmd0
    return shutil.which(cmd0, path=env.get("PATH", os.defpath)) or cmd0


def _spawn_pty(req: protocol.StartRequest, env: dict[str, str]) -> tuple[int, int, int]:
    """fork with a controlling terminal. `pty.fork` does the setsid + TIOCSCTTY dance,
    which is the part that is easy to get subtly wrong by hand. Also handed back is a pidfd
    on the child, readable once it exits: a child it left running can hold the terminal
    open long after, so the terminal's end does not say the child's."""
    program = _executable(req.cmd[0], env)
    pid, fd = pty.fork()
    if pid == 0:                       # child
        try:
            os.chdir(req.cwd)
            os.execve(program, list(req.cmd), env)
        except BaseException as exc:    # noqa: BLE001 - the child must not return
            os.write(2, f"launcher: could not exec {req.cmd[0]}: {exc}\n".encode())
            os._exit(127)
    try:
        pidfd = os.pidfd_open(pid)
    except OSError:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        os.close(fd)
        raise
    _set_winsize(fd, req.rows, req.cols)
    return pid, fd, pidfd


def _spawn_piped(req: protocol.StartRequest, env: dict[str, str]
                 ) -> tuple[int, int, int | None, int]:
    """A stream's stderr is the launcher's own (`None` here): its stdout is a protocol the
    caller parses, so there is no frame to put a diagnostic in, and the launcher's stderr is
    the view container's log."""
    out_r, out_w = os.pipe()
    err_r, err_w = (None, None) if req.stream else os.pipe()
    in_r, in_w = os.pipe()
    program = _executable(req.cmd[0], env)
    pid = os.fork()
    if pid == 0:                       # child
        try:
            os.dup2(in_r, 0)
            os.dup2(out_w, 1)
            if err_w is not None:
                os.dup2(err_w, 2)
            for fd in (out_r, out_w, err_r, err_w, in_r, in_w):
                if fd is None:
                    continue
                try:
                    os.close(fd)
                except OSError:
                    pass
            os.setsid()
            os.chdir(req.cwd)
            os.execve(program, list(req.cmd), env)
        except BaseException as exc:    # noqa: BLE001
            os.write(2, f"launcher: could not exec {req.cmd[0]}: {exc}\n".encode())
            os._exit(127)
    os.close(out_w)
    if err_w is not None:
        os.close(err_w)
    os.close(in_r)
    return pid, out_r, err_r, in_w


class Handler(socketserver.BaseRequestHandler):
    server: "LauncherServer"

    def handle(self) -> None:
        conn: socket.socket = self.request
        conn.settimeout(None)
        reader = conn.makefile("rb")
        try:
            line = reader.readline()
            if not line:
                return
            try:
                request = protocol.decode(line)
            except protocol.ProtocolError as exc:
                conn.sendall(protocol.encode(protocol.error(str(exc))))
                return

            op = request.get("op")
            if op == "ping":
                conn.sendall(protocol.encode(protocol.ok(
                    generation=self.server.generation,
                    instance=self.server.instance,
                    version=protocol.PROTOCOL_VERSION,
                    pid=os.getpid(),
                )))
            elif op == "list":
                conn.sendall(protocol.encode(protocol.ok(
                    procs=self.server.registry.snapshot(),
                    launcher=self.server.launcher_id)))
            elif op == "scrollback":
                # What a process printed, without taking over its terminal. `attach` is
                # the interactive door and it hands the connection to the pty; a failure
                # being diagnosed needs the same bytes and no terminal.
                proc = self.server.registry.get(int(request.get("proc", -1)))
                if proc is None:
                    conn.sendall(protocol.encode(protocol.error("no such proc")))
                else:
                    conn.sendall(protocol.encode(protocol.ok(
                        proc=proc.proc_id, exit=proc.exit_code,
                        **protocol.stream_frame("stdout", proc.replay()))))
            elif op == "signal":
                self._signal(conn, request)
            elif op == "end":
                self._end(conn, request)
            elif op == "attach":
                self._attach(conn, request)
            elif op == "resize":
                self._resize(conn, request)
            elif op == "start":
                self._start(conn, request)
            else:
                conn.sendall(protocol.encode(protocol.error(f"unknown op '{op}'")))
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            try:
                reader.close()
            except OSError:
                pass

    def _signal(self, conn: socket.socket, request: dict[str, Any]) -> None:
        proc = self.server.registry.get(int(request.get("proc", -1)))
        if proc is None:
            conn.sendall(protocol.encode(protocol.error("no such proc")))
            return
        sig = int(request.get("sig", signal.SIGTERM))
        if request.get("tree"):
            pids = tree_of(proc)
            if not pids:
                conn.sendall(protocol.encode(protocol.error("process is already gone")))
                return
            for pid in pids:
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass                # ended between the reading and the signal
            conn.sendall(protocol.encode(protocol.ok()))
            return
        # A reaped pid may already be another process's.
        if proc.exit_code is not None:
            conn.sendall(protocol.encode(protocol.error("process is already gone")))
            return
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except ProcessLookupError:
            conn.sendall(protocol.encode(protocol.error("process is already gone")))
            return
        except OSError as exc:
            conn.sendall(protocol.encode(protocol.error(str(exc))))
            return
        conn.sendall(protocol.encode(protocol.ok()))

    def _end(self, conn: socket.socket, request: dict[str, Any]) -> None:
        """Answered only once nothing of the process's tree runs and its exit is recorded."""
        proc = self.server.registry.get(int(request.get("proc", -1)))
        if proc is None:
            conn.sendall(protocol.encode(protocol.error("no such proc")))
            return
        left = end_tree(proc)
        if left:
            conn.sendall(protocol.encode(protocol.error(
                f"still running after SIGKILL: pids {left}")))
            return
        deadline = time.monotonic() + ABANDON_GRACE
        while proc.exit_code is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if proc.exit_code is None:
            conn.sendall(protocol.encode(protocol.error(
                f"its tree has ended, but its exit was not recorded within {ABANDON_GRACE}s")))
            return
        conn.sendall(protocol.encode(protocol.ok(proc=proc.proc_id, exit=proc.exit_code)))

    def _start(self, conn: socket.socket, request: dict[str, Any]) -> None:
        try:
            req = protocol.StartRequest.from_wire(request)
        except protocol.ProtocolError as exc:
            conn.sendall(protocol.encode(protocol.error(str(exc))))
            return

        env = dict(self.server.base_env)
        env.update(req.env)
        tree = uuid.uuid4().hex
        env[TREE_VAR] = tree
        if req.pty:
            env.setdefault("TERM", req.term)

        try:
            if req.pty:
                pid, master, pidfd = _spawn_pty(req, env)
                proc = self.server.registry.add(pid, req.cmd, True, tree, master_fd=master)
                conn.sendall(protocol.encode(protocol.ok(
                    proc=proc.proc_id, pid=pid, launcher=self.server.launcher_id)))
                self.server.start_pty_pump(proc, pidfd)
                self._serve_pty(conn, proc)
            else:
                pid, out_r, err_r, in_w = _spawn_piped(req, env)
                proc = self.server.registry.add(pid, req.cmd, False, tree)
                conn.sendall(protocol.encode(protocol.ok(
                    proc=proc.proc_id, pid=pid, launcher=self.server.launcher_id)))
                if req.stream:
                    self._pump_stream(conn, out_r, in_w, proc)
                else:
                    self._pump_piped(conn, out_r, err_r, in_w, proc, req.stdin)
        except OSError as exc:
            conn.sendall(protocol.encode(protocol.error(f"could not start: {exc}")))

    def _reap(self, proc: Process) -> int:
        return self.server.reap(proc)

    def _attach(self, conn: socket.socket, request: dict[str, Any]) -> None:
        proc = self.server.registry.get(int(request.get("proc", -1)))
        if proc is None:
            conn.sendall(protocol.encode(protocol.error("no such proc")))
            return
        # Exit is checked first: an exited PTY process has already had its master closed,
        # so the "no terminal" branch would otherwise answer a true question with a
        # misleading reason.
        if proc.exit_code is not None:
            conn.sendall(protocol.encode(protocol.error(
                f"that process exited with {proc.exit_code}")))
            return
        if not proc.pty or proc.master_fd is None:
            conn.sendall(protocol.encode(protocol.error("that process has no terminal")))
            return
        conn.sendall(protocol.encode(protocol.ok(proc=proc.proc_id, pid=proc.pid,
                                                 launcher=self.server.launcher_id)))
        self._serve_pty(conn, proc)

    def _resize(self, conn: socket.socket, request: dict[str, Any]) -> None:
        """A terminal's size belongs to whoever is looking at it. A detached process starts
        with nobody attached, and each window that attaches later has its own size; the kernel
        tells the process with SIGWINCH."""
        proc = self.server.registry.get(int(request.get("proc", -1)))
        if proc is None:
            conn.sendall(protocol.encode(protocol.error("no such proc")))
            return
        if proc.exit_code is not None:
            conn.sendall(protocol.encode(protocol.error(
                f"that process exited with {proc.exit_code}")))
            return
        if not proc.pty or proc.master_fd is None:
            conn.sendall(protocol.encode(protocol.error("that process has no terminal")))
            return
        try:
            _set_winsize(proc.master_fd, int(request["rows"]), int(request["cols"]))
        except (KeyError, TypeError, ValueError):
            conn.sendall(protocol.encode(protocol.error("resize needs integer rows and cols")))
            return
        except OSError as exc:
            conn.sendall(protocol.encode(protocol.error(f"resize failed: {exc}")))
            return
        conn.sendall(protocol.encode(protocol.ok(proc=proc.proc_id)))

    def _serve_pty(self, conn: socket.socket, proc: Process) -> None:
        """This connection *is* the terminal: raw bytes both ways, no framing. Anything
        else would put an encoder between a user and their shell.

        Detaching does not touch the process. The pump thread owns the master fd and
        keeps the child alive and its output recorded, so `rai attach` can come back to
        it and a raigolmid restart never disturbs a running shell."""
        missed = proc.attach(conn)
        if missed:
            try:
                conn.sendall(missed)
            except (BrokenPipeError, ConnectionResetError, OSError):
                proc.detach(conn)
                return
        conn.settimeout(1.0)
        try:
            while proc.exit_code is None:
                try:
                    data = conn.recv(READ_SIZE)
                except socket.timeout:
                    continue
                except (ConnectionResetError, OSError):
                    break
                if not data:
                    break
                if proc.master_fd is None:
                    break
                try:
                    os.write(proc.master_fd, data)
                except OSError:
                    break
        finally:
            proc.detach(conn)

    def _pump_piped(self, conn: socket.socket, out_r: int, err_r: int, in_w: int,
                    proc: Process, stdin: str | None) -> None:
        if stdin:
            try:
                os.write(in_w, stdin.encode())
            except OSError:
                pass
        try:
            os.close(in_w)
        except OSError:
            pass

        open_fds = {out_r: "stdout", err_r: "stderr"}
        caller = conn.fileno()
        abandoned = False
        exited = False
        try:
            while open_fds:
                readable, _, _ = select.select([*open_fds, caller], [], [], 1.0)
                if not exited and (code := _exited(proc.pid)) is not None:
                    exited = True
                    conn.sendall(protocol.encode({"exited": code}))
                if caller in readable:
                    # The client sends nothing after the start request, so the socket is
                    # readable only at end-of-file: the caller timed out or went away. A
                    # silent child would otherwise run, and hold this thread, forever.
                    try:
                        abandoned = not conn.recv(READ_SIZE)
                    except OSError:
                        abandoned = True
                    if abandoned:
                        break
                for fd in readable:
                    if fd == caller:
                        continue
                    try:
                        data = os.read(fd, READ_SIZE)
                    except OSError:
                        data = b""
                    if not data:
                        os.close(fd)
                        del open_fds[fd]
                        continue
                    conn.sendall(protocol.encode(protocol.stream_frame(open_fds[fd], data)))
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            for fd in list(open_fds):
                try:
                    os.close(fd)
                except OSError:
                    pass
        if abandoned:
            self._abandon(proc)
        code = self._reap(proc)
        try:
            conn.sendall(protocol.encode({"exit": code}))
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _pump_stream(self, conn: socket.socket, out_r: int, in_w: int, proc: Process) -> None:
        """Raw bytes both ways until stdout closes. Each direction has its own thread: one
        blocked writing a full stdin pipe while the child blocks on a full stdout pipe is a
        deadlock. The caller closing its end is the editor stopping its server: stdin is closed,
        the server has the grace to answer what it was last asked and exit on its own, and is
        then ended like an abandoned exec."""
        def feed() -> None:
            try:
                while data := conn.recv(READ_SIZE):
                    os.write(in_w, data)
            except OSError:
                pass
            finally:
                os.close(in_w)
            if not _exits_within(proc.pid, ABANDON_GRACE):
                self._abandon(proc)

        feeder = threading.Thread(target=feed, name=f"stream-{proc.proc_id}", daemon=True)
        feeder.start()
        try:
            while data := os.read(out_r, READ_SIZE):
                conn.sendall(data)
        except OSError:
            pass
        finally:
            os.close(out_r)
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        feeder.join()
        self._reap(proc)

    @staticmethod
    def _abandon(proc: Process) -> None:
        """End a piped child nobody is reading, with everything it started (`end_tree`).
        SIGTERM first, so a `git` can drop its lock. Nobody is left to tell of a survivor
        but the view's log."""
        if left := end_tree(proc):
            sys.stderr.write(f"launcher: proc {proc.proc_id} ({' '.join(proc.cmd)}) left "
                             f"pids {left} running after SIGKILL\n")
            sys.stderr.flush()


def _exited(pid: int) -> int | None:
    """The child's exit code once it has exited, left unreaped for `reap` to collect."""
    try:
        info = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return None
    if info is None:
        return None
    return info.si_status if info.si_code == os.CLD_EXITED else 128 + info.si_status


def _exits_within(pid: int, seconds: float) -> bool:
    """Whether the child exits within `seconds`, leaving it for `reap` to collect."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
                return True
        except ChildProcessError:
            return True
        time.sleep(0.05)
    return False


class LauncherServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, socket_path: str, instance: str, generation: int = 0,
                 socket_fd: int | None = None) -> None:
        self.socket_path = socket_path
        self.instance = instance
        self.generation = generation
        # Proc ids restart at 1 in every launcher, so a proc id means something only
        # alongside the launcher that issued it; a recreated view's launcher is another.
        self.launcher_id = uuid.uuid4().hex
        self.bound_here = socket_fd is None
        self.registry = Registry()
        self.base_env = {
            "PATH": CHILD_PATH,
            "HOME": os.environ.get("HOME", "/root"),
            "SHELL": os.environ.get("SHELL", "/.toolbelt/bin/bash"),
        }
        for key in ("LANG", "LC_ALL", "NIX_PATH", "TERMINFO", "TERMINFO_DIRS"):
            if key in os.environ:
                self.base_env[key] = os.environ[key]

        if socket_fd is not None:
            # The view's entrypoint bound the socket and then detached the socket directory
            # from the view, so the path is not reachable from here: the fd is adopted,
            # and the file is the next entrypoint's to replace, never this process's to remove.
            super().__init__(socket_path, Handler, bind_and_activate=False)
            self.socket.close()
            self.socket = socket.fromfd(socket_fd, socket.AF_UNIX, socket.SOCK_STREAM)
            os.close(socket_fd)
        else:
            # Binding here is for tests and for any caller that is still privileged.
            if os.path.exists(socket_path):
                os.unlink(socket_path)
            super().__init__(socket_path, Handler)
            os.chmod(socket_path, 0o600)

    def reap(self, proc: Process) -> int:
        while True:
            try:
                _, status = os.waitpid(proc.pid, 0)
            except ChildProcessError:
                return proc.exit_code if proc.exit_code is not None else -1
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                return -1
            if os.WIFEXITED(status):
                code = os.WEXITSTATUS(status)
            elif os.WIFSIGNALED(status):
                code = 128 + os.WTERMSIG(status)
            else:
                continue
            self.registry.finish(proc.proc_id, code)
            return code

    def start_pty_pump(self, proc: Process, pidfd: int) -> None:
        """One thread per PTY process, owning its master fd for as long as anything holds
        the terminal open.

        This is what makes a launcher child outlive the connection that asked for it: the
        terminal stays open whether or not anyone is attached, so closing `rai attach` —
        or restarting raigolmid — does not hang up the shell."""
        threading.Thread(target=self._pty_pump, args=(proc, pidfd), daemon=True).start()

    def _pty_pump(self, proc: Process, pidfd: int) -> None:
        """The process's exit is recorded when its pidfd says it exited, after what it left
        in the terminal is read; a child it started that left its session can hold the
        terminal open after, and what it prints is still recorded until it lets go."""
        master = proc.master_fd
        assert master is not None
        watched = [master, pidfd]
        try:
            while True:
                readable, _, _ = select.select(watched, [], [])
                if pidfd in readable:
                    self._drain_pending(proc, master)
                    os.close(pidfd)
                    watched.remove(pidfd)
                    pidfd = -1
                    self.reap(proc)
                if master not in readable:
                    continue
                try:
                    data = os.read(master, READ_SIZE)
                except OSError:
                    break
                if not data:
                    break
                proc.record(data)
                proc.write_out(data)
        finally:
            if pidfd != -1:
                os.close(pidfd)
            code = self.reap(proc)
            # Recorded before the connection ends: a client told EOF asks `list` why, and
            # must find the exit already there.
            self.registry.finish(proc.proc_id, code)
            with proc.lock:
                conn = proc.conn
                proc.conn = None
                proc.master_fd = None
            try:
                os.close(master)
            except OSError:
                pass
            if conn is not None:
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    @staticmethod
    def _drain_pending(proc: Process, master: int) -> None:
        """What the terminal holds unread now — all an exited process can have left in it —
        bounded by that count, so a child still printing to it cannot hold the exit back."""
        pending = struct.unpack("i", fcntl.ioctl(master, termios.FIONREAD, b"\0" * 4))[0]
        while pending > 0:
            try:
                data = os.read(master, min(pending, READ_SIZE))
            except OSError:
                return
            if not data:
                return
            proc.record(data)
            proc.write_out(data)
            pending -= len(data)

    def server_close(self) -> None:
        super().server_close()
        if self.bound_here:
            os.unlink(self.socket_path)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="raigolmid-launcher",
        description="The in-view process launcher",
    )
    parser.add_argument("--socket", required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--generation", type=int, default=0)
    parser.add_argument("--socket-fd", type=int, default=None,
                        help="adopt this already-bound listening socket instead of binding, "
                             "which a launcher with no capabilities cannot do")
    args = parser.parse_args(argv)

    # Children are reaped explicitly per process; a default SIGCHLD handler that reaped
    # them first would race `waitpid` and lose the exit code the caller is waiting for.
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)

    server = LauncherServer(args.socket, args.instance, args.generation,
                            args.socket_fd)
    caps = _own_capabilities()
    sys.stderr.write(
        f"launcher: listening on {args.socket} for {args.instance} "
        f"(generation {args.generation}, CapEff {caps['CapEff']}, "
        f"NoNewPrivs {caps['NoNewPrivs']})\n")
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _own_capabilities() -> dict[str, str]:
    """Printed at startup so having none is verifiable from the view's log rather than
    asserted in a document."""
    out = {"CapEff": "?", "NoNewPrivs": "?"}
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                key, _, value = line.partition(":")
                if key in out:
                    out[key] = value.strip()
    except OSError:
        pass
    return out


if __name__ == "__main__":
    raise SystemExit(main())
