"""`rai`.

Everything is observable as text (principle 8), and this is the text. `rai status` prints
the full state as JSON; `rai events` streams the event log; `rai diagnose` bundles
everything into one file the user can send back.

It is deliberately a thin client: it holds no state and computes no compatibility. The
selector never computes compatibility either — both ask raigolmid, so there is one
answer to every question rather than two that can disagree.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

from raigolmid.client import ApiClient, ApiError
from raigolmid.paths import Paths


def _client() -> ApiClient:
    return ApiClient(Paths.from_env().api_socket)


def _print(value: Any, raw: bool = False) -> None:
    if raw or not sys.stdout.isatty():
        print(json.dumps(value, indent=2, default=str))
        return
    print(json.dumps(value, indent=2, default=str))


# --- commands ---------------------------------------------------------------------------

def cmd_status(args) -> int:
    if getattr(args, "follow", False):
        return _follow_status()
    status = _client().call("status")
    if args.json:
        _print(status)
        return 0
    text, warnings = _status_text(status)
    print(text)
    for warning in warnings:
        print(f"\n{warning}", file=sys.stderr)
    return 0


def _status_text(status: dict) -> tuple[str, list[str]]:
    """What `rai status` prints: the state, and the warnings that go to stderr."""
    session = status["session"]
    lines = [f"face:     {session['face'] or '—'}",
             f"toolbelt: {session['toolbelt'] or '—'}",
             f"body:     {session['body'] or '—'}",
             f"state:    {session['meaning']}"]
    if status["instances"]:
        lines += ["", "sandboxes:"]
        for iid, inst in sorted(status["instances"].items()):
            health = inst["health"]
            reason = f" — {inst['reason']}" if inst.get("reason") else ""
            focus = " ←active" if iid == session["focused_instance"] else ""
            lines.append(f"  {iid:<35} {health}{reason}{focus}")
            lines.append(f"       gen {inst['view_generation']}  refs {', '.join(inst['refs'])}")
    if status["agents"]:
        lines += ["", "agents:"]
        for agent in status["agents"]:
            scope = agent["scope"]
            where = scope if isinstance(scope, str) else scope["body"]
            mark = "●" if agent.get("marked") else " "
            lines.append(f"{mark} {agent['tab']:<10} {where:<24} [{agent['status']}, "
                         f"{agent['state']}]")
    warnings = [f"definition error: {error}" for error in status.get("definition_errors", [])]
    warnings += [f"WARNING protected git files changed in {iid}: {', '.join(changed)}"
                 for iid, changed in (status.get("protected_git_changes") or {}).items()]
    return "\n".join(lines), warnings


def _follow_status() -> int:
    """`rai status`, redrawn from the daemon on each of its events for as long as it runs —
    the AI terminal's base window shows it above its shell. The events say only when to
    look (`ui.hostevents`); a daemon that is not answering is said, never left showing the
    last state it gave. In tmux the pane is fitted to what is drawn."""
    import threading

    from ui.hostevents import follow

    client = _client()
    lock = threading.Lock()

    def draw(text: str) -> None:
        columns = shutil.get_terminal_size().columns
        lines = [line[:columns] for line in text.splitlines()]
        with lock:
            pane = os.environ.get("TMUX_PANE")
            if pane is not None:
                # Never more than half the window: the shell under it is the one to type in.
                height = int(subprocess.run(
                    ["tmux", "display-message", "-p", "-t", pane, "#{window_height}"],
                    check=True, capture_output=True, text=True).stdout)
                subprocess.run(["tmux", "resize-pane", "-t", pane,
                                "-y", str(max(1, min(len(lines), height // 2)))], check=True)
            # Only the state now: tmux's `scroll-on-clear` puts a cleared screen into the
            # scrollback, and a shrink pushes lines there, so each draw clears it too (ED 3).
            sys.stdout.write("\033[?25l\033[H\033[2J\033[3J" + "\n".join(lines))
            sys.stdout.flush()

    def fetch() -> None:
        try:
            status = client.call("status")
        except ApiError as exc:
            lost(str(exc))
            return
        text, warnings = _status_text(status)
        draw("\n".join([text, *warnings]))

    def lost(why: str) -> None:
        draw(f"raigolmid is not answering: {why}\n"
             "`systemctl --user status raigolmid` says why; this redraws once it is back.")

    follow(client, ("",), fetch, lost)
    return 0


def cmd_list(args) -> int:
    rows = _client().call("list_items", kind=args.kind)
    if args.json:
        _print(rows)
        return 0
    for row_name, items in rows.items():
        print(f"\n{row_name.upper()}")
        for item in items:
            mark = "●" if item["selected"] else "○"
            if not item["selectable"]:
                print(f"  {mark} {item['id']:<24} (unavailable — {item['reason']})")
                continue
            note = f"  ⚠ {item['warning']}" if item.get("warning") else ""
            print(f"  {mark} {item['id']:<24} {item['name']}{note}")
            if item.get("tab"):
                print(f"       its tab: {item['tab']}")
            for inst in item.get("instances", []):
                extra = f" — {inst['reason']}" if inst.get("reason") else ""
                print(f"       {inst['instance']} [{inst['health']}]{extra}")
    return 0


def cmd_select(args) -> int:
    _client().call("select", kind=args.kind, id=args.id)
    return cmd_status(args)


def cmd_deselect(args) -> int:
    _client().call("deselect", kind=args.kind)
    return cmd_status(args)


def cmd_ask(args) -> int:
    """A face's: the user's words to an agent tab (`scopes.build_face_methods` `ask`)."""
    answer = _client().call("ask", content=" ".join(args.words), tab=args.tab)
    print(f"to {answer['tab']}: {answer['status']}")
    return 0


def cmd_exec(args) -> int:
    result = _client().call("exec", target=args.instance, cmd=args.cmd, cwd=args.cwd)
    sys.stdout.write(result["stdout"])
    sys.stderr.write(result["stderr"])
    return int(result["exit_code"])


def cmd_lsp(args) -> int:
    """A language or debug server in the focused instance's toolbelt container, as the stdio
    command an editor in the face starts. Through the launcher, never `docker exec`,
    for cmd_attach's reason: only the launcher runs on the view's root."""
    from raigolmid import lsp

    if not args.cmd:
        print("rai lsp needs the server's command", file=sys.stderr)
        return 2
    return lsp.main(Paths.from_env(), args.cmd)


TOOLBELT_SHELL = "/.toolbelt/bin/bash"


def cmd_attach(args) -> int:
    """Attach a terminal to a process in a session view, through its launcher.

    Never `docker exec`: that would restore the container's configured capabilities and
    miss the view's root switch. raigolmid opens the connection for the sandbox named,
    from the host or a face alike.
    """
    from raigolmid.launcher.client import BrokeredLauncherClient, LauncherError

    client = BrokeredLauncherClient(Paths.from_env().api_socket, args.instance)
    try:
        if args.proc is not None:
            proc = args.proc
            sock, launcher = client.attach(proc)
        else:
            size = shutil.get_terminal_size((120, 40))
            sock, proc, launcher = client.open_pty([args.shell], cwd=args.cwd,
                                                   rows=size.lines, cols=size.columns)
    except LauncherError as exc:
        print(f"rai attach: {exc}", file=sys.stderr)
        return 1
    if not _terminal(client, sock, proc):
        print(f"rai attach: detached; process {proc} runs on "
              f"(rai attach {args.instance} --proc {proc})", file=sys.stderr)
        return 0
    try:
        code = _how_it_ended(client, launcher, proc)
    except _ViewEnded as exc:
        print(f"rai attach: {exc}", file=sys.stderr)
        return 1
    if code is None:
        print(f"rai attach: the connection ended; process {proc} runs on "
              f"(rai attach {args.instance} --proc {proc})", file=sys.stderr)
        return 1
    return code


# foot shows the text I-beam over its grid unless the program in it tracks the mouse or asks
# for a pointer by name (OSC 22, foot 1.25 `terminal.c` term_xcursor_update_for_seat). All
# text is selectable, so the I-beam says nothing: every terminal the
# machine runs asks for the arrow.
PLAIN_POINTER = "\x1b]22;default\x1b\\"


def _plain_pointer() -> None:
    if sys.stdout.isatty():
        sys.stdout.write(PLAIN_POINTER)
        sys.stdout.flush()


def cmd_terminal(args) -> int:
    """A face's terminal: the window is the face's, the shell is a toolbelt's — the
    sandbox named, or the focused one — started through its view's launcher, which raigolmid
    opens for it. A face works with every body, so any sandbox may be named. The shell stays
    on the view it opened on when focus moves, because it holds the user's state and that
    view is still up. A shell cannot outlive its view, so when the view ends the window says
    so instead of closing, and opens a new shell by itself when the same instance's view is
    back — a rebuild, which the agent does as often as the user — or on Enter, on whatever
    is focused then, or on the sandbox named again."""
    import signal

    from raigolmid import lsp
    from raigolmid.launcher.client import (BrokeredLauncherClient, LauncherError,
                                          LauncherUnreachable)

    _plain_pointer()
    paths = Paths.from_env()
    named = args.instance
    where = f"{named}'s toolbelt" if named else "the focused toolbelt"

    while True:
        if named:
            instance = named
            client = BrokeredLauncherClient(paths.api_socket, named)
            view = _generation(client)
        else:
            focused = lsp.focused(paths)
            if focused is None:
                if not _await_a_shell(where, "the focused sandbox has no toolbelt, so there "
                                      "is no shell to open", _focus_moved(paths, None)):
                    return 1
                continue
            instance, view = focused
            client = BrokeredLauncherClient(paths.api_socket, instance)
        size = shutil.get_terminal_size((120, 40))
        try:
            sock, proc, launcher = client.open_pty([TOOLBELT_SHELL], cwd=lsp.WORK,
                                                   rows=size.lines, cols=size.columns)
        except (LauncherError, LauncherUnreachable) as exc:
            ready = _launcher_back(client, view) if named else _focus_moved(paths, focused)
            if not _await_a_shell(where, f"{instance}'s toolbelt did not open a shell: {exc}",
                                  ready):
                return 1
            continue

        def hang_up(*_, client=client, proc=proc):
            # The window closing ends its shell, as it would any terminal's. A launcher
            # that is gone took the shell with it, so there is nothing left to end.
            try:
                client.signal(proc, signal.SIGHUP)
            except LauncherUnreachable:
                pass
            os._exit(128 + signal.SIGHUP)

        signal.signal(signal.SIGHUP, hang_up)
        while True:
            if not _terminal(client, sock, proc):
                hang_up()
            try:
                code = _how_it_ended(client, launcher, proc)
            except _ViewEnded as exc:
                ended = exc
                break
            if code is not None:
                return code
            sock, launcher = client.attach(proc)
        signal.signal(signal.SIGHUP, signal.SIG_DFL)
        ready = _launcher_back(client, view) if named else _focus_back(paths, instance, view)
        if not _await_a_shell(where, f"{instance}: {ended}", ready):
            return 1


def _focus_moved(paths, seen):
    """Ready when the focused view is one other than `seen`, which may be None."""
    from raigolmid import lsp

    return lambda: lsp.focused(paths) not in (None, seen)


def _focus_back(paths, instance: str, view: str):
    """Ready when `instance` is focused again on a view other than `view`: its rebuild."""
    from raigolmid import lsp

    def ready() -> bool:
        now = lsp.focused(paths)
        return now is not None and now[0] == instance and now[1] != view
    return ready


def _launcher_back(client, generation: str | None):
    """Ready when a named sandbox's launcher answers and is not the one `generation` was."""
    return lambda: _generation(client) not in (None, generation)


def _generation(client) -> str | None:
    """Which launcher answers for a named sandbox, or None when none does: a new one is its
    view recreated, as a new view id is in `Paths.focused_view`."""
    from raigolmid.launcher import protocol
    from raigolmid.launcher.client import LauncherUnreachable

    try:
        return str(client.ping()["generation"])
    except (LauncherUnreachable, protocol.ProtocolError):
        return None


# How often a waiting terminal asks whether its view is up: how soon a shell opens after the
# view it waits for is up, and nothing else.
FOCUS_POLL = 0.25


def _await_a_shell(where: str, reason: str, ready) -> bool:
    """Say why this window has no shell, and wait: a reason in a window that closes at once
    has not been said. True when a shell should open — on Enter, or by itself once
    `ready()` holds; False on Ctrl-D."""
    import select

    print(f"\r\naos terminal: {reason}\r\n"
          f"Enter opens a shell in {where}; Ctrl-D closes this window.",
          file=sys.stderr)
    while True:
        readable, _, _ = select.select([sys.stdin], [], [], FOCUS_POLL)
        if readable:
            return sys.stdin.readline() != ""
        if ready():
            print("rai terminal: its toolbelt is up; opening a shell", file=sys.stderr)
            return True


class _ViewEnded(Exception):
    """A terminal's shell ended with the view it ran in."""


def _how_it_ended(client, launcher: str, proc: int) -> int | None:
    """Why a terminal's connection ended, asked of the launcher that issued the shell,
    because the socket closes the same way for both causes: the shell's exit code, or None
    while it runs on and only the connection went. `_ViewEnded` when the view went and the
    shell with it."""
    from raigolmid.launcher.client import LauncherUnreachable

    try:
        known = client.process(launcher, proc)
    except LauncherUnreachable as exc:
        raise _ViewEnded(f"the toolbelt's view is gone, and its shell with it ({exc})") from exc
    if known is None:
        raise _ViewEnded("the toolbelt's view was recreated, and its shell ended with it")
    return known["exit"]


def _terminal(client, sock, proc: int) -> bool:
    """This terminal *is* the process's terminal until one side closes: True when the far
    side did, False when this terminal's input did. Its size follows this window, not the
    one the process was started with: a process started detached has a size nobody chose."""
    import select
    import signal
    import termios
    import tty

    from raigolmid.launcher.client import LauncherError

    stdin_fd = sys.stdin.fileno()
    saved = termios.tcgetattr(stdin_fd) if sys.stdin.isatty() else None
    refused: list[str] = []

    def follow_size(*_):
        size = os.get_terminal_size(stdin_fd)
        try:
            client.resize(proc, size.lines, size.columns)
        except LauncherError as exc:
            refused.append(str(exc))

    previous = signal.getsignal(signal.SIGWINCH)
    far_side = True
    try:
        if saved is not None:
            tty.setraw(stdin_fd)
            follow_size()
            signal.signal(signal.SIGWINCH, follow_size)
        while True:
            readable, _, _ = select.select([sock, stdin_fd], [], [])
            if sock in readable:
                try:
                    data = sock.recv(65536)
                except ConnectionResetError:
                    break
                if not data:
                    break
                os.write(sys.stdout.fileno(), data)
            if stdin_fd in readable:
                data = os.read(stdin_fd, 65536)
                if not data:
                    far_side = False
                    break
                try:
                    sock.sendall(data)
                except (BrokenPipeError, ConnectionResetError):
                    break
    finally:
        signal.signal(signal.SIGWINCH, previous)
        if saved is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved)
        sock.close()
    for reason in refused:
        print(f"rai: the terminal could not follow this window's size: {reason}",
              file=sys.stderr)
    return far_side


def cmd_rebuild(args) -> int:
    report = _client().call("rebuild_body", instance_id=args.instance)
    if args.json:
        _print(report)
    else:
        print(f"{report['result']}  ({report['seconds']}s)")
        if report.get("reason"):
            print(report["reason"])
        if report.get("log"):
            print(report["log"], file=sys.stderr)
    return 0 if report["result"] in ("rebuilt", "already_current") else 1


def cmd_history(args) -> int:
    _print(_client().call("history", instance_id=args.instance, n=args.n))
    return 0


def cmd_events(args) -> int:
    """Readable with `raigolmid` dead, which is the point of the recovery path: the
    agent in the AI terminal has to be able to read what happened and fix the cause, and
    the daemon being the thing that broke is the likeliest reason it is looking."""
    client = _client()
    if not args.follow:
        try:
            events = client.call("events", n=args.n)
        except ApiError as exc:
            if exc.kind != "unreachable":
                raise
            return _events_from_disk(args.n, str(exc))
        for event in events:
            print(json.dumps(event, default=str))
        return 0
    try:
        for event in client.subscribe():
            print(json.dumps(event, default=str), flush=True)
    except KeyboardInterrupt:
        return 0
    return 0


def _events_from_disk(n: int, why: str) -> int:
    from raigolmid.events import EventLog

    path = Paths.from_env().events
    print(f"rai: {why}\n     reading {path} directly instead.", file=sys.stderr)
    if not path.exists():
        print(f"rai: {path} does not exist either — raigolmid has never run here.",
              file=sys.stderr)
        return 1
    for event in EventLog(path).tail(n):
        print(event.to_json())
    return 0


def cmd_search(args) -> int:
    matches = _client().call("search_packages", query=args.query, limit=args.limit)
    for name in matches:
        print(name)
    if not matches:
        print(f"no nixpkgs package matches '{args.query}'", file=sys.stderr)
        return 1
    return 0


def cmd_repair(args) -> int:
    _print(_client().call("repair", instance=args.instance))
    return 0


def cmd_reconcile(args) -> int:
    _print(_client().call("reconcile"))
    return 0


def cmd_selector(args) -> int:
    """Open or close the native selector, which is what the reserved host key runs.

    Deliberately not a call into raigolmid: the selector is part of the recovery path,
    and a selector you can only open by asking the daemon is unreachable in the state it
    exists for. The daemon being down is something the selector *shows*.
    """
    from raigolmid.hostsurfaces import HostSurfaceError, toggle_selector

    def docker():
        from raigolmid.runtime.docker_runtime import DockerRuntime
        return DockerRuntime()

    try:
        print(toggle_selector(docker, Paths.from_env()))
    except HostSurfaceError as exc:
        print(f"rai selector: {exc}", file=sys.stderr)
        return 1
    return 0


def boot_frame(step: str, frame: int) -> str:
    """One frame of the first-start screen: what is being built, and that it happens once.

    Separate from the loop so that what the machine says can be read without a machine."""
    from ui import theme
    return ("\033[2J\033[H\n\n    RaiGolmi\n\n"
            f"    {theme.SPINNER[frame % len(theme.SPINNER)]}  Preparing {step}\u2026\n\n"
            "    This happens once, the first time this machine starts.\n")


def cmd_boot(_args) -> int:
    """The screen a first start shows while the machine prepares its own surfaces.

    Nothing can be drawn before those images exist, because the surfaces *are* images,
    and loading or building them is a wait in which a black screen says nothing at all about
    whether the machine is working. This ends when the selector is up,
    and the terminal it runs in closes with it.

    ⚠ Written to stdout rather than logged: it is a screen being drawn in a terminal and
    redrawn in place, not a diagnostic. `logging` is for the daemon's account of itself.
    """
    import time

    from raigolmid import hostimages
    from raigolmid.hostsurfaces import selector_running
    _plain_pointer()
    from raigolmid.runtime.docker_runtime import DockerRuntime

    steps = (("the selector", hostimages.selector), ("the controls", hostimages.control))
    runtime = DockerRuntime()
    step, asked = "the selector", 0.0
    try:
        frame = 0
        while True:
            now = time.monotonic()
            if now - asked > 1.0:
                # Docker is asked once a second; the spinner turns ten times faster, because a
                # still spinner is what a hung machine looks like.
                asked = now
                if selector_running(runtime):
                    return 0
                step = next((name for name, image in steps
                             if not hostimages.present(runtime, image())), "the last pieces")
            sys.stdout.write(boot_frame(step, frame))
            sys.stdout.flush()
            frame += 1
            time.sleep(0.1)
    finally:
        runtime.close()


def cmd_credential(args) -> int:
    """The one secret this machine keeps. A new user meets this in the AI terminal,
    which opens on it when no credential is set — there is nobody else to ask, and a machine
    whose agents cannot start should say so where the user already is."""
    from raigolmid import credential
    from rai.prompt import masked

    path = Paths.from_env().agent_credentials
    if not args.set:
        try:
            key = next(iter(credential.read(path)))
        except credential.CredentialError as exc:
            print(f"rai credential: {exc}", file=sys.stderr)
            return 1
        print(f"{key} is set in {path}")
        return 0

    key = "ANTHROPIC_API_KEY" if args.api_key else "CLAUDE_CODE_OAUTH_TOKEN"
    print("RaiGolmi runs every agent on one Claude Code credential.\n"
          "On any machine signed in to Claude, run `claude setup-token` and paste the token\n"
          "here with a right-click. It is stored on this machine only, in {path},\n"
          "readable by you alone, and it is masked as you type it.\n"
          .format(path=path))
    # ⚠ Flushed first: the question is written straight to the terminal, so anything still
    # sitting in stdout's buffer would arrive after the question it was meant to explain.
    sys.stdout.flush()
    # Masked rather than unechoed: a credential does not belong in the scrollback of a window
    # the user reopens all day, and a prompt that showed *nothing* could not tell a paste that
    # worked from one that never happened (`rai/prompt.py`).
    try:
        value = masked(f"{key}: ")
    except (EOFError, KeyboardInterrupt):
        print("\nnothing stored; run `rai credential --set` when you have a token")
        return 1
    try:
        credential.write(path, key, value)
    except credential.CredentialError as exc:
        print(f"rai credential: {exc}", file=sys.stderr)
        return 1
    print(f"stored {key} in {path}. Agents can start now.")
    return 0


def cmd_claude_login(args) -> int:
    """The claude.ai sign-in Remote Control needs (`raigolmid/claude_login.py`), by Claude
    Code's own `claude auth login` in a scratch agent container: it shows an address to open in
    any browser and takes the code the page gives back. Its home is a directory made here,
    beside where the sign-in is kept, and removed once the sign-in is read out of it."""
    from raigolmid import claude_login

    path = Paths.from_env().claude_login
    if not args.login:
        try:
            claude_login.read(path)
        except claude_login.LoginError as exc:
            print(f"rai claude-login: {exc}", file=sys.stderr)
            return 1
        print(f"a claude.ai sign-in is set in {path}")
        return 0
    print("A tab held for you reaches your phone through Claude Code's Remote Control, which\n"
          "needs your claude.ai sign-in. Open the address below in any browser, sign in, and\n"
          "paste the code it shows here. Ctrl+C skips this for now; `rai claude-login\n"
          "--login` asks again.\n")
    sys.stdout.flush()
    from raigolmid import hostimages
    from raigolmid.runtime.docker_runtime import DockerRuntime

    runtime = DockerRuntime()
    try:
        image = hostimages.ensure(runtime, hostimages.agent())
    except hostimages.HostImageError as exc:
        print(f"rai claude-login: {exc}", file=sys.stderr)
        return 1
    finally:
        runtime.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=path.parent, prefix=".claude-login-") as home:
        try:
            result = subprocess.run(
                ["docker", "run", "--rm", "-it", "--user", f"{os.getuid()}:{os.getgid()}",
                 "-e", "HOME=/login", "-v", f"{home}:/login", "--entrypoint", "claude",
                 image, "auth", "login"])
        except KeyboardInterrupt:
            print("\nnot signed in")
            return 1
        if result.returncode != 0:
            print(f"rai claude-login: claude auth login exited {result.returncode}; nothing "
                  "stored", file=sys.stderr)
            return 1
        try:
            claude_login.write(path, claude_login.from_claude_home(Path(home)))
            claude_login.read(path)
        except claude_login.LoginError as exc:
            print(f"rai claude-login: {exc}", file=sys.stderr)
            return 1
    print(f"signed in; the sign-in is in {path}, readable by you alone, and raigolmid keeps "
          "it fresh")
    return 0


def cmd_registry_token(args) -> int:
    """The GitHub account a catalog upload opens its pull request from, signed in the
    way `gh auth login` signs in: a one-time code shown here and entered at github.com in any
    browser, since the machine has none of its own. gh runs with no terminal, so it asks
    nothing and prints only the code and the address; its config lives and dies with its
    container, and the token it was given is kept once, in `Paths.registry_token`."""
    from raigolmid import credential

    path = Paths.from_env().registry_token
    if not args.login:
        try:
            credential.read(path, credential.REGISTRY_KEYS)
        except credential.CredentialError as exc:
            print(f"rai registry-token: {exc}", file=sys.stderr)
            return 1
        print(f"a GitHub sign-in is set in {path}")
        return 0
    print("Catalog uploads are pull requests from your GitHub account. Sign in: open the\n"
          "address below in any browser and enter the code. Ctrl+C skips this for now;\n"
          "`rai registry-token --login` asks again.\n")
    sys.stdout.flush()
    from raigolmid import hostimages
    from raigolmid.runtime.docker_runtime import DockerRuntime

    runtime = DockerRuntime()
    try:
        image = hostimages.ensure(runtime, hostimages.gh())
    except hostimages.HostImageError as exc:
        print(f"rai registry-token: {exc}", file=sys.stderr)
        return 1
    finally:
        runtime.close()
    try:
        result = subprocess.run(
            # Ctrl+C must end gh, or the container waits on for a code nobody will enter: a
            # shell as pid 1 ignores the signal docker forwards, and `--init` (tini) passes it
            # only to the shell, which waits for gh, unless told to signal the whole group.
            ["docker", "run", "--rm", "--init", "-e", "TINI_KILL_PROCESS_GROUP=1",
             "--tmpfs", "/tmp", "-e", "GH_CONFIG_DIR=/tmp/gh",
             "-e", "HOME=/tmp", image, "sh", "-c",
             "gh auth login --hostname github.com --git-protocol https --web </dev/null >&2 "
             "&& gh auth token"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)
    except KeyboardInterrupt:
        print("\nnot signed in")
        return 1
    if result.returncode != 0:
        print(f"rai registry-token: gh exited {result.returncode}; nothing stored",
              file=sys.stderr)
        return 1
    try:
        credential.write(path, "GITHUB_TOKEN", result.stdout.strip(), credential.REGISTRY_KEYS)
    except credential.CredentialError as exc:
        print(f"rai registry-token: {exc}", file=sys.stderr)
        return 1
    print(f"signed in; the token is in {path}, readable by you alone")
    return 0


def cmd_ai(args) -> int:
    for flag in ("toggle", "show", "hide"):
        if getattr(args, flag, False):
            return _move_ai_terminal(flag)
    from ui.ai_terminal.terminal import main as ai_main
    return ai_main(args)


def _move_ai_terminal(direction: str) -> int:
    """What the reserved key runs: the terminal's surface asked directly, never the
    daemon, for `cmd_selector`'s reason — this is a door for when the daemon is broken.

    ⚠ In a face, `SWAYSOCK` is the face's own sway, so this asks the face's socket instead,
    and only to show: that is all a face is given."""
    if os.environ.get("RAIGOLMID_FACE"):
        return _ask_from_a_face(direction)
    from raigolmid import hostsurfaces

    verb = {"toggle": "toggle", "show": "open", "hide": "close"}[direction]
    try:
        result = hostsurfaces.move_ai_terminal(verb, Paths.from_env())
    except hostsurfaces.HostSurfaceError as exc:
        print(f"rai ai --{direction}: {exc}", file=sys.stderr)
        return 1
    print(result)
    if result == "shown":
        # Only when the window was already there. A window this call *started* runs its own
        # `rai ai`, which picks the tmux window; doing it here as well would put two of them
        # through the same session at once, flickering between the base window and the agent's.
        from ui.ai_terminal.terminal import TerminalError, show_agent
        try:
            show_agent(ApiClient(Paths.from_env().api_socket))
        except TerminalError as exc:
            print(f"rai ai --{direction}: {exc}", file=sys.stderr)
            return 1
    return 0


def _ask_from_a_face(direction: str) -> int:
    if direction != "show":
        print(f"rai ai --{direction}: a face may only ask for the AI terminal to be shown; "
              "the reserved key puts it away", file=sys.stderr)
        return 1
    try:
        print(ApiClient(Paths.from_env().api_socket).call("show_ai_terminal"))
    except ApiError as exc:
        print(f"rai ai --show: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_agent_activity(args) -> int:
    """Run by the agent's own Claude Code hooks, inside its container: a
    session started is the evidence its channel is heard, a prompt taken is busy — with the
    channel push's `seq` when the prompt is one, read from the `UserPromptSubmit` hook's
    input on stdin — and a turn ending is decided by `agent_stop` from the `Stop` hook's
    input on stdin — refused on stdout while the agent has background commands to answer
    for, idle or busy once it has — or by an API error, from the `StopFailure` hook's. The tab is the socket's: only an agent container's
    answers these."""
    from raigolmid import activity
    if args.state == "busy":
        from raigolmid.channel import pushed_seq
        prompt = json.load(sys.stdin)["prompt"]
        seq = pushed_seq(prompt)
        activity.record(Path.home(), busy=True)
        # The user's own prompt is sent as well: it may be the answer to what the tab asked.
        _client().call("agent_activity", busy=True, channel_seq=seq,
                       **({"prompt": prompt} if seq is None else {}))
        return 0
    if args.state == "session":
        # SessionStart: printed output would be added to the agent's context, so none is.
        import socket
        activity.record(Path.home(), busy=False, session=socket.gethostname())
        _client().call("agent_session_started")
        return 0

    if args.state == "failed":
        # StopFailure fires instead of Stop when an API error ended the turn, and ignores
        # what it prints; the turn is over, and the daemon resumes it (`limits.py`).
        error = json.load(sys.stdin)["error"]
        activity.record(Path.home(), busy=False, failed=error)
        _client().call("agent_activity", busy=False, error=error)
        return 0

    from raigolmid.agent_stop import Memory, decide
    # The tab's home is the agent's own and goes with the tab, as this memory must.
    store = Path.home() / ".raigolmi" / "stop-memory.json"
    memory = (Memory.from_json(json.loads(store.read_text())) if store.exists()
              else Memory())
    hook = json.load(sys.stdin)
    decision = decide(hook, memory)
    store.parent.mkdir(exist_ok=True)
    store.write_text(json.dumps(decision.memory.to_json()))
    if decision.refusal is not None:
        print(json.dumps({"decision": "block", "reason": decision.refusal}))
    if decision.report is not None:
        activity.record(Path.home(), busy=decision.report == "busy")
        _client().call("agent_activity", busy=decision.report == "busy")
    return 0


def cmd_mcp(args) -> int:
    from raigolmid.mcp_server import serve
    scope = args.scope or os.environ.get("RAIGOLMI_SCOPE")
    if scope not in ("tab", "machine"):
        print(f"rai mcp: no scope: --scope not given and RAIGOLMI_SCOPE is {scope!r}",
              file=sys.stderr)
        return 2
    return serve(scope=scope)


def cmd_diagnose(args) -> int:
    """Bundle logs, state, generated Compose files and compositor state into one file the
    user can send back."""
    paths = Paths.from_env()
    out = Path(args.output or f"raigolmi-diagnose-{int(time.time())}.tar.gz")
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "diagnose"
        staging.mkdir()

        def capture(name: str, fn) -> None:
            try:
                (staging / name).write_text(fn(), encoding="utf-8")
            except Exception as exc:                   # noqa: BLE001
                (staging / name).write_text(f"unavailable: {exc}\n", encoding="utf-8")

        client = _client()
        capture("status.json", lambda: json.dumps(client.call("status"), indent=2))
        capture("items.json", lambda: json.dumps(client.call("list_items"), indent=2))
        capture("version.json", lambda: json.dumps(client.call("version"), indent=2))
        capture("docker-ps.txt", lambda: subprocess.run(
            ["docker", "ps", "-a", "--filter", "label=io.raigolmi.managed=true",
             "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"],
            capture_output=True, text=True, timeout=30).stdout)
        capture("swaymsg-tree.json", lambda: subprocess.run(
            ["swaymsg", "-t", "get_tree"], capture_output=True, text=True,
            timeout=10).stdout)
        capture("hyprctl-clients.json", lambda: subprocess.run(
            ["hyprctl", "-j", "clients"], capture_output=True, text=True,
            timeout=10).stdout)

        for source in (paths.events, paths.intent):
            if source.exists():
                shutil.copy2(source, staging / source.name)
        projects = paths.state / "projects"
        if projects.is_dir():
            shutil.copytree(projects, staging / "projects")

        with tarfile.open(out, "w:gz") as tar:
            tar.add(staging, arcname="raigolmi-diagnose")
    print(out)
    return 0


# --- wiring -------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rai", description="RaiGolmi CLI")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="full state")
    p.add_argument("--follow", action="store_true",
                   help="redraw on every change until interrupted")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("list", help="faces, toolbelts and bodies with availability")
    p.add_argument("kind", nargs="?", choices=["face", "toolbelt", "body"])
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("select")
    p.add_argument("kind", choices=["face", "body"])
    p.add_argument("id")
    p.set_defaults(fn=cmd_select)

    p = sub.add_parser("deselect")
    p.add_argument("kind", choices=["face", "body"])
    p.set_defaults(fn=cmd_deselect)

    p = sub.add_parser("ask", help="from a face: the user's words to an agent tab (the one "
                                   "they view, else the machine tab)")
    p.add_argument("words", nargs="+")
    p.add_argument("--tab")
    p.set_defaults(fn=cmd_ask)

    p = sub.add_parser("exec", help="run a command in a sandbox's toolbelt")
    p.add_argument("instance", metavar="sandbox")
    p.add_argument("cmd", nargs=argparse.REMAINDER)
    p.add_argument("--cwd", default="/work")
    p.set_defaults(fn=cmd_exec)

    p = sub.add_parser("lsp", help="a language server in the focused toolbelt, over stdio")
    p.add_argument("cmd", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_lsp)

    p = sub.add_parser("terminal", help="a face's terminal: a shell in a sandbox's toolbelt")
    p.add_argument("instance", metavar="sandbox", nargs="?",
                   help="the sandbox whose toolbelt opens the shell; the focused one if none")
    p.set_defaults(fn=cmd_terminal)

    p = sub.add_parser("attach", help="attach a terminal to a session view")
    p.add_argument("instance", metavar="sandbox")
    p.add_argument("--proc", type=int, help="attach to an existing process instead")
    p.add_argument("--shell", default=TOOLBELT_SHELL)
    p.add_argument("--cwd", default="/work")
    p.set_defaults(fn=cmd_attach)

    p = sub.add_parser("rebuild")
    p.add_argument("instance", metavar="sandbox")
    p.set_defaults(fn=cmd_rebuild)

    p = sub.add_parser("history")
    p.add_argument("instance", metavar="sandbox")
    p.add_argument("-n", type=int, default=50)
    p.set_defaults(fn=cmd_history)

    p = sub.add_parser("events")
    p.add_argument("-n", type=int, default=50)
    p.add_argument("-f", "--follow", action="store_true")
    p.set_defaults(fn=cmd_events)

    p = sub.add_parser("search", help="resolve package names against nixpkgs")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("repair", help="tear a sandbox down and rebuild it")
    p.add_argument("instance", metavar="sandbox")
    p.set_defaults(fn=cmd_repair)

    sub.add_parser("reconcile").set_defaults(fn=cmd_reconcile)

    sub.add_parser("selector", help="open or close the selector"
                   ).set_defaults(fn=cmd_selector)

    p = sub.add_parser("ai", help="the AI terminal")
    p.add_argument("action", nargs="?", default="attach",
                   choices=["attach", "list", "kill", "restart", "ready", "follow",
                            "viewing", "permission", "menu"])
    p.add_argument("tab", nargs="?")
    p.add_argument("--toggle", action="store_true",
                   help="show the AI terminal over the face, or hide it (the reserved key)")
    p.add_argument("--show", action="store_true", help="bring the AI terminal out")
    p.add_argument("--hide", action="store_true", help="put the AI terminal away")
    p.add_argument("--answer", choices=["yes", "no"],
                   help="with `permission ID`: the answer to that permission")
    p.add_argument("--always", choices=["project", "everywhere"],
                   help="with --answer: keep it for the permission's project, or everywhere")
    p.set_defaults(fn=cmd_ai)

    sub.add_parser("boot", help="the first-start screen, while the surfaces are built"
                   ).set_defaults(fn=cmd_boot)

    p = sub.add_parser("credential", help="the Claude Code token every agent runs on")
    p.add_argument("--set", action="store_true", help="ask for a token and store it")
    p.add_argument("--api-key", action="store_true",
                   help="store an Anthropic Console API key instead of a Claude Code token")
    p.set_defaults(fn=cmd_credential)

    p = sub.add_parser("registry-token", help="the GitHub sign-in a catalog upload uses")
    p.add_argument("--login", action="store_true", help="sign in to GitHub with a one-time code")
    p.set_defaults(fn=cmd_registry_token)

    p = sub.add_parser("claude-login",
                       help="the claude.ai sign-in a held tab's Remote Control uses")
    p.add_argument("--login", action="store_true", help="sign in to claude.ai")
    p.set_defaults(fn=cmd_claude_login)

    p = sub.add_parser("agent-activity",
                       help="an agent's hooks report its session up, busy, idle or failed")
    p.add_argument("state", choices=["session", "busy", "stop", "failed"])
    p.set_defaults(fn=cmd_agent_activity)

    p = sub.add_parser("mcp", help="stdio MCP server inside an agent container")
    p.add_argument("--scope", choices=["tab", "machine"],
                   help="default: $RAIGOLMI_SCOPE, which the daemon gives every agent container")
    p.set_defaults(fn=cmd_mcp)

    p = sub.add_parser("diagnose")
    p.add_argument("-o", "--output")
    p.set_defaults(fn=cmd_diagnose)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except ApiError as exc:
        print(f"rai: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
