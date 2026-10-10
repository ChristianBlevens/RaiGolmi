"""`rai ai` — the AI terminal.

tmux, one window per tab, following claude-squad's structure. The host shows it in `foot`,
toggled by a reserved host key; it lives here rather than in either selector.

The **machine tab** is always there; a **body's tab** is named by its body, because "which
project is this agent in" is the question a user asks of a tab list most often. The
daemon opens tabs; the windows here follow: a tab opened gets a window, and selecting a body
goes to its tab.

The agent itself runs in a container, never on the host. tmux's job is only to give
each one a window and a name.
"""
from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import dataclass
from string import Template
from typing import Any

from ui import theme
from ui.theme import SPINNER

logger = logging.getLogger(__name__)

SESSION = "raigolmi-ai"
# The window that is no tab's: the first start's sign-ins, then a shell, under the machine's
# state kept current (`rai status --follow`) in the pane above it, which carries `@status`.
BASE = "raigolmi"
# prefix, then this: the current tab's permission put back after an Escape.
PERMISSION_KEY = "a"

# The status line in the palette of the host's other surfaces (`ui/theme.py`). Session
# options only: tmux resolves a window option set on a session to its current window alone,
# so the tab list is drawn by `status-format`, whose second `W` format is the current tab.
# Tabs as a browser draws them: a click on one selects it, its
# `×` closes it, and a line divides it from the next. The `×` is a user range naming the tab
# (`CLOSE_RANGE`), answered by the status click (`STATUS_CLICK`); the base window and the
# janitor have none, since neither is a tab the user closes. A tab that needs them is marked
# `●` and a tab the machine tab manages `◇`, from the `@marked` and `@managed`
# its own window's `follow` keeps (`_keep`).
# ⚠ tmux ends a range one cell past its last character (format-draw.c `fr->end = cx + 1`) and
# a click takes the first range holding its cell, so each range is followed by a space outside
# it for that cell; the name's range ending on its own trailing space covered the `×`.
CLOSE_RANGE = "x"
CLOSED_SECONDS = 0.25
# The `≡` at the bar's left, a user range answered by the status click: every tab as a menu,
# since the bar is cut at the terminal's right edge and a tab past it cannot be clicked.
MENU_RANGE = "m"
_CLOSE = ("#{?#{||:#{==:#{window_name},raigolmi},#{m:*⚙,#{window_name}}},,"
          "#[range=user|%s#{s/ .*//:window_name}%%s]×#[norange] }" % CLOSE_RANGE)
_TAB = ("#[range=window|#{window_index}%s] #{?@marked,● ,}#{?@managed,◇ ,}#W#[norange] "
        + _CLOSE + "#[default fg=$border]│#[default]")
STATUS_CLICK = (
    f"if-shell -F '#{{==:#{{mouse_status_range}},{MENU_RANGE}}}' "
    "{ run-shell -b \"rai ai menu '#{client_name}' 2>&1 | systemd-cat -t rai-ai\" } "
    f"{{ if-shell -F '#{{m:{CLOSE_RANGE}*,#{{mouse_status_range}}}}' "
    f"{{ run-shell -b \"rai ai kill '#{{s/^{CLOSE_RANGE}//:mouse_status_range}}' "
    "2>&1 | systemd-cat -t rai-ai\" } { select-window -t = } }")
_CURRENT = " bold fg=$bg bg=$accent"


def style(look: theme.Look) -> dict[str, str]:
    """The session's options in the user's look's palette, set each time the terminal starts."""
    def paint(text: str) -> str:
        return Template(text).substitute(look.palette)
    return {
        "status-style": paint("bg=$surface,fg=$muted"),
        "status-left": paint("#[range=user|%s]#[bold,fg=$accent] ≡ #[norange]" % MENU_RANGE),
        "status-format[0]": paint("#[align=left]#{T:status-left}#{W:%s,%s}" % (
            _TAB % ("", ""), _TAB % ((_CURRENT,) * 2))),
        "message-style": paint("bg=$accent_bg,fg=$text"),
        "mouse": "on",
    }

# Selecting copies and right-click pastes. The agent takes no mouse (`agents.py`), so the
# wheel scrolls tmux's scrollback a fixed step at a time, and every
# copy tmux makes goes to the clipboard and to the primary, which a bare terminal's right-click
# pastes (`host/foot/foot.ini`); `set-clipboard off` keeps it the one writer. Right-click pastes
# the clipboard, bracketed, and tmux's own menus go with it.
#
# A copy keeps the selection and the view where they are: copy mode stays until a click or a
# key. A selection turns scroll-exit off, so the wheel carries it to the bottom (and, held,
# extends it) without leaving copy mode, which would drop it. Copy mode is the mouse's alone:
# its tables hold only these, so every key falls through to the root table's `Any`, which
# leaves copy mode and hands the key to the agent. A mouse event there has a position; a key
# has none.
COPY_COMMAND = ("sh -c 'f=$(mktemp) && cat >\"$f\" && wl-copy <\"$f\" && "
                "wl-copy --primary <\"$f\"; rm -f \"$f\"'")
_COPY = "send-keys -X scroll-exit-off ; send-keys -X %s ; run-shell -d 0.3 ; " \
        "send-keys -X copy-pipe-no-clear"
MOUSE = {
    "MouseDrag1Pane": "select-pane -t = ; copy-mode -M",
    "DoubleClick1Pane": "select-pane -t = ; copy-mode -H ; " + _COPY % "select-word",
    "TripleClick1Pane": "select-pane -t = ; copy-mode -H ; " + _COPY % "select-line",
    "MouseDown3Pane": (
        "select-pane -t = ; run-shell -b \"wl-paste --no-newline --type text | "
        "tmux load-buffer -b rai-paste - && tmux paste-buffer -p -d -b rai-paste -t '#{pane_id}'\""),
    "Any": ("if-shell -F '#{&&:#{pane_in_mode},#{==:#{mouse_y},}}' "
            "'send-keys -X cancel' ; send-keys"),
}
# tmux names a drag's end after where the button comes up, so a drag carried past the text onto
# the scrollbar, a border or the tab bar ends under another name, and copies all the same.
DRAG_ENDS = ("Pane", "Border", "Status", "StatusLeft", "StatusRight", "StatusDefault",
             "ScrollbarUp", "ScrollbarSlider", "ScrollbarDown")
COPY_MODE = {
    "MouseDown1Pane": ("select-pane ; send-keys -X clear-selection ; "
                       "send-keys -X scroll-exit-on ; "
                       "if-shell -F '#{==:#{scroll_position},0}' 'send-keys -X cancel'"),
    "MouseDrag1Pane": "select-pane ; send-keys -X scroll-exit-off ; send-keys -X begin-selection",
    **{f"MouseDragEnd1{where}": "send-keys -X copy-pipe-no-clear" for where in DRAG_ENDS},
    "WheelUpPane": "select-pane ; send-keys -N5 -X scroll-up",
    "WheelDownPane": "select-pane ; send-keys -N5 -X scroll-down",
    "DoubleClick1Pane": "select-pane ; " + _COPY % "select-word",
    "TripleClick1Pane": "select-pane ; " + _COPY % "select-line",
    "MouseDown3Pane": "send-keys -X cancel ; " + MOUSE["MouseDown3Pane"],
}
COPY_TABLES = ("copy-mode", "copy-mode-vi")
MENUS = ("M-MouseDown3Pane", "MouseDown3Status", "MouseDown3StatusLeft", "M-MouseDown3Status",
         "M-MouseDown3StatusLeft")


class TerminalError(Exception):
    pass


def _tmux(*args: str, check: bool = True,
          capture: bool = True) -> subprocess.CompletedProcess[str]:
    if shutil.which("tmux") is None:
        raise TerminalError(
            "tmux is not installed. The AI terminal is tmux; install "
            "it, or run the agent directly with `docker attach` on its container."
        )
    proc = subprocess.run(["tmux", *args], capture_output=capture, text=True)
    if check and proc.returncode != 0:
        raise TerminalError(f"tmux {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


@dataclass(frozen=True, slots=True)
class Window:
    tab: str
    name: str
    active: bool
    id: str


def _first_command() -> str:
    """What the AI terminal opens on.

    A machine with no credential can start no agent, and the user who has to supply one
    is whoever opened this window — on a fresh machine there is nobody else, and no browser to
    be redirected to. So the terminal opens on the question instead of on a status that would
    only say the same thing in smaller print. Nothing asked is the empty command."""
    from raigolmid import claude_login, credential
    from raigolmid.paths import Paths

    paths = Paths.from_env()
    # Signing in to GitHub follows the Claude token on a first start, and is asked again on
    # every opening until it is done: an upload from the catalog needs it. A skipped
    # sign-in costs uploads only, so the agent's tab opens either way. So is the claude.ai
    # sign-in, which only a tab followed from the user's phone needs. Each is numbered among
    # those asked, so the user sees how far through they are.
    asked = [command for command, done in (
        ("rai credential --set", credential.is_set(paths.agent_credentials)),
        ("rai registry-token --login",
         credential.is_set(paths.registry_token, credential.REGISTRY_KEYS)),
        ("rai claude-login --login", claude_login.is_set(paths.claude_login))) if not done]
    steps = [f"{command} --step {n}/{len(asked)}" for n, command in enumerate(asked, 1)]
    if not credential.is_set(paths.agent_credentials):
        # A token given is an agent wanted: the tab opens the moment there is one.
        return f"{steps[0]} && {{ {''.join(f'{s}; ' for s in steps[1:])}rai ai ready; }}"
    return "; ".join(steps)


def _shell() -> str:
    return os.environ.get("SHELL") or "/bin/bash"


def _base_command(first: str) -> list[str]:
    """The base window's shell pane: `first`, then the user's shell, whose exit closes the
    window as a lone pane's would, status pane included. A Ctrl+C that kills a command outright
    would take the non-interactive shell with it; trapped (not ignored, so each command still
    takes it), the shell goes on to its interactive one."""
    steps = ["trap : INT", *([first] if first else []), shlex.quote(_shell()),
             'exec tmux kill-window -t "$TMUX_PANE"']
    return [_shell(), "-c", "; ".join(steps)]


def _base_shell() -> str:
    """The base window's shell pane: the one that is not its status pane."""
    panes = _tmux("list-panes", "-t", f"={SESSION}:={BASE}", "-F",
                  "#{pane_id} #{@status}").stdout.split("\n")
    shells = [line.split()[0] for line in panes if line.strip() and len(line.split()) == 1]
    if len(shells) != 1:
        raise TerminalError(f"the base window has {len(shells)} shell panes, not one: {panes}")
    return shells[0]


def offer_claude_login(ended: str) -> None:
    """The claude.ai sign-in asked again in the base window, where a first start asked it,
    once the daemon has removed one that ended (`claude_login.lost`, whose time is `ended`).
    Every tab window hears that event, so the window keeps which ending it was asked for. A
    base window running a command is the user's and is left; the history says where to sign in."""
    target = f"={SESSION}:={BASE}"
    if _tmux("show-options", "-wqv", "-t", target, "@claude_login_asked").stdout.strip() == ended:
        return
    shell = _base_shell()
    at = _tmux("display-message", "-p", "-t", shell, "#{pane_current_command}").stdout.strip()
    if at != os.path.basename(_shell()):
        return
    _tmux("set-option", "-w", "-t", target, "@claude_login_asked", ended)
    _tmux("respawn-pane", "-k", "-t", shell, *_base_command("rai claude-login --login"))


def session_exists() -> bool:
    return _tmux("has-session", "-t", SESSION, check=False).returncode == 0


def ensure_session() -> None:
    """The base window is a shell, with `rai status --follow` in a pane above it rather than
    as the window's command. A window whose command exits takes the window with it and the
    last window takes the session, so a status that fails would leave no AI terminal in the
    one state that requires it — raigolmid down, which is the state an agent repairs the
    daemon from. The status pane says the daemon is not answering, and stays when it fails.
    """
    if not session_exists():
        _tmux("new-session", "-d", "-s", SESSION, "-n", BASE, *_base_command(_first_command()))
        status = _tmux("split-window", "-d", "-b", "-v", "-l", "1", "-P", "-F", "#{pane_id}",
                       "-t", f"={SESSION}:={BASE}", "rai", "status", "--follow").stdout.strip()
        _tmux("set-option", "-p", "-t", status, "@status", "on", ";",
              "set-option", "-p", "-t", status, "remain-on-exit", "on")
    # Also on a session this did not create: the host config's floor starts bare tmux on
    # the same session name, and adopting it should not leave it unstyled.
    commands: list[str] = []
    look = theme.load()
    for option, value in style(look).items():
        commands += [";", "set-option", "-t", SESSION, option, value]
    # A tab's conversation is written into its pane's scrollback (`agents.py`), not redrawn
    # by the agent, so the scrollback holds a long one; tmux's default keeps 2000 lines. The
    # scrollbar is always shown, on every window, and is dragged or clicked like any other.
    commands += [";", "set-option", "-t", SESSION, "history-limit", "100000",
                 ";", "set-option", "-wg", "pane-scrollbars", "on",
                 ";", "set-option", "-wg", "pane-scrollbars-style",
                 Template("bg=$surface,fg=$dim,width=1,pad=0").substitute(look.palette)]
    # A closed tab leaves the terminal ready again: closing the last agent opens the next.
    # Its account goes to the journal, since a hook has no terminal to print on.
    commands += [";", "set-hook", "-t", SESSION, "window-unlinked",
                 "run-shell -b 'rai ai ready 2>&1 | systemd-cat -t rai-ai'"]
    # Which tab the user views is half the terminal's to say (`raigolmid/viewing.py`): this hook
    # fires on every change of the current window — a select, a new window, the current one
    # killed — where `after-select-window` fires on a select alone.
    commands += [";", "set-hook", "-t", SESSION, "session-window-changed",
                 "run-shell -b \"rai ai viewing '#{window_name}' 2>&1 | systemd-cat -t rai-ai\""]
    commands += [";", "bind-key", "-T", "root", "MouseDown1Status", STATUS_CLICK]
    commands += [";", "set-option", "-s", "copy-command", COPY_COMMAND,
                 ";", "set-option", "-s", "set-clipboard", "off"]
    for key, command in MOUSE.items():
        commands += [";", "bind-key", "-T", "root", key, command]
    for key in MENUS:
        commands += [";", "unbind-key", "-q", "-T", "root", key]
    for table in COPY_TABLES:
        commands += [";", "unbind-key", "-a", "-T", table]
        for key, command in COPY_MODE.items():
            commands += [";", "bind-key", "-T", table, key, command]
    # A permission's menu that could not be drawn is drawn by this key (`offer_permission`).
    commands += [";", "bind-key", "-T", "prefix", PERMISSION_KEY,
                 "run-shell -b 'rai ai permission 2>&1 | systemd-cat -t rai-ai'"]
    _tmux(*commands[1:])


def window_name(tab: str, scope: Any) -> str:
    """`tab-3 machine`, a body's tab by its body — `tab-5 notes-api` — and the janitor marked
    as the machine's, not a tab the user works in. A tab's scope never changes, so
    neither does its name."""
    if scope == "janitor":
        return f"{tab} ⚙"
    if scope == "machine":
        return f"{tab} machine"
    return f"{tab} {scope['body']}"


def list_windows() -> list[Window]:
    if not session_exists():
        return []
    proc = _tmux("list-windows", "-t", SESSION,
                 "-F", "#{window_id}\t#{window_name}\t#{window_active}")
    windows = []
    for line in proc.stdout.splitlines():
        window_id, name, active = line.split("\t")
        windows.append(Window(tab=name.split()[0], name=name, active=active == "1",
                              id=window_id))
    return windows


def open_window(tab: str, scope: Any, agent_command: list[str]) -> str:
    """Opened behind the one in view: a tab the daemon opened is not where the user was looking.
    Where they go is `select_window`'s."""
    ensure_session()
    name = window_name(tab, scope)
    if tab not in {w.tab for w in list_windows()}:
        _tmux("new-window", "-d", "-t", SESSION, "-n", name, *agent_command)
    return name


def select_window(tab: str) -> None:
    _tmux("select-window", "-t", f"{SESSION}:{tab}*")


def close_window(tab: str) -> None:
    if not session_exists():
        return
    _tmux("kill-window", "-t", f"{SESSION}:{tab}*", check=False)


def _typed() -> bool:
    """Whether the user typed the command, in the AI terminal or a terminal of their own. From
    a script — ssh with no terminal, an agent — the terminal on their screen stays where they
    left it, and there is nothing to attach to."""
    return bool(os.environ.get("TMUX")) or sys.stdin.isatty()


def attach() -> int:
    """Typed inside the AI terminal, `rai ai` already runs in a tmux client, and tmux
    refuses a nested attach; that client is moved instead."""
    ensure_session()
    if os.environ.get("TMUX"):
        _tmux("switch-client", "-t", SESSION)
        return 0
    return subprocess.run(["tmux", "attach-session", "-t", SESSION]).returncode


def agent_command(tab: str) -> list[str]:
    """The window follows the tab, not a container (`follow`). The agent process is the
    container's own, so it survives the window closing — a tab is a view onto an agent,
    not the agent itself."""
    return ["rai", "ai", "follow", tab]


def attach_command(container: str) -> list[str]:
    return ["docker", "attach", "--detach-keys=ctrl-p,ctrl-q", container]


# What a terminal answers rather than draws: device attributes, the version, the keyboard
# protocol, a status or mode report, a colour, a capability, the window's size. Replayed, each
# would be answered again, and the answer would reach the agent as typed input.
_QUERIES = re.compile(
    rb"\x1b\[(?:[>=]?[0-9;]*c|>[0-9;]*q|\?u|\??[0-9;]*n|\??[0-9;]*\$p|1[468]t)"
    rb"|\x1b\][0-9;]*;\?(?:\x07|\x1b\\)"
    rb"|\x1bP\+q[0-9A-Fa-f;]*(?:\x07|\x1b\\)")


def _replay(container: str) -> None:
    """Everything the container's agent has written, into the pane, before it is attached:
    an attach carries only what is written after it, and a tab resumed at boot or opened in a
    new window wrote its conversation before any window attached. What the agent writes
    between the two is not shown until it draws again. Docker's refusal is said in the pane,
    and the attach after it says the rest."""
    logs = subprocess.run(["docker", "logs", container], capture_output=True)
    if logs.returncode != 0:
        _out(logs.stderr.decode(errors="replace"))
        return
    sys.stdout.buffer.write(_QUERIES.sub(b"", logs.stdout))
    sys.stdout.buffer.flush()


def follow(client, tab: str) -> int:
    """What a tab's window runs: the tab's agent, through every container the
    daemon puts under it — a reopen after a crash, a restart — so a window closes
    with its tab rather than with a container. Between containers it waits on the tab's
    events, subscribed before the tab is read so none falls between the two. A tab past
    reopening says so and waits for a Restart. A detach leaves the agent running and ends
    the window, and the closed-window hook's `ready` opens it again. Its listener keeps the
    window's mark and its permission's menu (`_keep`) on each of the tab's events and each
    change of the tab in view, while the agent is attached too. Every window opens the
    window of a tab the daemon opened, and the window in view brings a body the user selected into
    view (`_follow_the_daemon`).

    A daemon restart ends the event stream and is a state this outlives, as every host
    surface does (`ui.hostevents`): the listener follows the next stream on its own thread,
    whatever the attach is doing, and the tab is read again once the daemon answers."""
    import queue
    from raigolmid import naming
    from raigolmid.client import ApiError
    from ui.hostevents import RECONNECT_SECONDS

    pane = os.environ.get("TMUX_PANE")
    if pane is None:
        raise TerminalError(f"rai ai follow {tab} is a tab's window command, and runs in tmux")
    wake: queue.Queue = queue.Queue()
    offered: list[str | None] = [None]
    first = threading.Event()

    def on(event: dict) -> None:
        # A dropped subscriber may have lost one of this tab's: read it again.
        ours = event.get("tab") == tab or event.get("type") == "subscriber.dropped"
        if ours or event.get("type") == "terminal.viewing":
            offered[0] = _keep(client, tab, pane, offered[0])
        # Every window answers a tab opening, since the one in view may be the tab
        # just closed; reconciling is locked and idempotent, so one window results.
        if event.get("type") == "tab.opened" or (
                event.get("type") == "selection.changed" and _in_view(pane)):
            _follow_the_daemon(client, event)
        if event.get("type") == "claude_login.lost":
            with _one_at_a_time():
                offer_claude_login(str(event["ts"]))
        if ours:
            wake.put(event)

    def acknowledged(subscribed: threading.Event, ended: threading.Event) -> None:
        subscribed.wait()
        if ended.is_set():
            return
        if first.is_set():
            # A stream after a restart: whatever changed while there was none is read again.
            offered[0] = _keep(client, tab, pane, offered[0])
            wake.put(None)
        first.set()

    def listen() -> None:
        while True:
            subscribed, ended = threading.Event(), threading.Event()
            threading.Thread(target=acknowledged, args=(subscribed, ended),
                             name="follow-acknowledged", daemon=True).start()
            stream = client.subscribe(subscribed)
            while True:
                try:
                    event = next(stream)
                except StopIteration:
                    logger.warning("the daemon closed the event stream; following it again")
                    break
                except (ApiError, OSError) as exc:
                    logger.warning("the daemon's event stream ended: %s; following it again "
                                   "in %.0fs", exc, RECONNECT_SECONDS)
                    break
                try:
                    on(event)
                except (ApiError, OSError, TerminalError) as exc:
                    # The daemon going away mid-event, the stream ending says the rest; or
                    # tmux refusing one, and the next event reconciles the windows again.
                    logger.warning("%s's window could not act on %s: %s",
                                   tab, event.get("type"), exc)
            ended.set()
            subscribed.set()
            time.sleep(RECONNECT_SECONDS)

    threading.Thread(target=listen, name="follow-events", daemon=True).start()
    # The tab is first read once the stream is acknowledged, so no event falls between.
    first.wait()

    past_reopening = (f"\n{tab} crashed, and reopening it could not bring it back — "
                      "`rai events` says why.\n"
                      f"`rai ai restart {tab}` tries again; `rai ai kill {tab}` closes it.\n")
    offered[0] = _keep(client, tab, pane, offered[0])
    event: dict | None = {}
    while True:
        status = _status(client)
        agent = next((a for a in status["agents"] if a["tab"] == tab), None)
        if agent is None:
            # A tab that ended its conversation: the one taking its work over comes into
            # view in its place, as the user would have it.
            successor = next((a["tab"] for a in status["agents"] if a["continues"] == tab),
                             None)
            if successor is not None and _in_view(pane):
                with _one_at_a_time():
                    sync_windows(client)
                select_window(successor)
            return 0
        if agent["status"] == "running":
            _replay(naming.agent(tab))
            subprocess.run(attach_command(naming.agent(tab)))
            if _container_running(naming.agent(tab)):
                return 0
        elif agent["status"] == "crashed" and (
                event == {} or (event or {}).get("type") == "container.unfixable"):
            # Read at the window's opening, or said by the daemon: not the moment between a
            # crash and its reopen, when the tab also reads crashed.
            _out(past_reopening)
        event = wake.get()


def _to_the_journal() -> None:
    """A tab's window says its own account in the journal, as the terminal's hooks do
    (`rai-ai`): its tty is the agent's pane, in raw mode while attached, so a line written
    there lands in the agent's screen, unreturned. A thread's death goes there too."""
    import logging.handlers
    handler = logging.handlers.SysLogHandler(address="/dev/log")
    handler.ident = "rai-ai: "
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    threading.excepthook = lambda hook: logger.error(
        "the %s thread died", hook.thread.name if hook.thread else "?",
        exc_info=(hook.exc_type, hook.exc_value, hook.exc_traceback))


def _status(client) -> dict:
    """The daemon's `status`, waiting out a restart: a window is the user's, and outlives one."""
    from raigolmid.client import ApiError
    from ui.hostevents import RECONNECT_SECONDS
    while True:
        try:
            return client.call("status")
        except ApiError as exc:
            if exc.kind != "unreachable":
                raise
            time.sleep(RECONNECT_SECONDS)


def _in_view(pane: str) -> bool:
    """Whether this pane's window is the one in view: every tab's window hears every event,
    and one of them answers for the terminal."""
    proc = _tmux("display-message", "-p", "-t", pane, "#{window_active}", check=False)
    return proc.stdout.strip() == "1"


def _follow_the_daemon(client, event: dict) -> None:
    """A tab the daemon opened gets its window — a body's tab opened by a selection, the
    machine tab reopened after the user closed it, the janitor opened for a failure — and a
    body they selected brings its tab into view. An agent's selection moves nothing here:
    the agent is not the user."""
    with _one_at_a_time():
        sync_windows(client)
        if (event["type"] == "selection.changed" and event.get("kind") == "body"
                and event.get("by") == "user" and event.get("id") is not None):
            tab = _tab_of(client.call("status"), {"body": event["id"]})
            if tab is not None:
                select_window(tab)


def _tab_of(status: dict, scope: Any) -> str | None:
    return next((a["tab"] for a in status["agents"] if a["scope"] == scope), None)


def _permission(client, args) -> int:
    """`rai ai permission ID --answer yes|no [--always project|everywhere]`, a menu item;
    bare, `PERMISSION_KEY`'s: the current tab's permission offered again."""
    if args.tab is not None:
        if args.answer is None:
            raise TerminalError(f"rai ai permission {args.tab} needs --answer yes or no")
        client.call("answer", id=args.tab, text=args.answer,
                    **({"always": args.always} if args.always else {}))
        return 0
    current = next((w.tab for w in list_windows() if w.active), None)
    if current is None or offer_permission(client, current, wait=True) is None:
        _tmux("display-message", f"{current or 'this window'} has no permission to answer")
    return 0


def _keep(client, tab: str, pane: str, offered: str | None) -> str | None:
    """The tab's window marked while it needs the user and while the machine tab manages it, as
    the daemon's `status` says, and
    its permission offered once each time they come to it (`offer_permission`): returns the
    one offered while they stay, None once they have gone. A tab gone has a window on its way
    out, and nothing to mark."""
    status = client.call("status")
    agent = next((a for a in status["agents"] if a["tab"] == tab), None)
    if agent is None:
        return None
    _tmux("set-option", "-w", "-t", pane, "@marked", "1" if agent["marked"] else "0")
    _tmux("set-option", "-w", "-t", pane, "@managed", "1" if agent["managed"] else "0")
    if status["terminal"]["viewing"] != tab:
        return None
    if agent["state"] != "permission":
        return offered
    return offer_permission(client, tab, unless=offered)


def offer_permission(client, tab: str, unless: str | None = None,
                     wait: bool = False) -> str | None:
    """The tab's pending permission answered in its own window, as a tmux menu on the
    attached terminal — yes or no, either kept *always* for its project or everywhere.
    Only an answer closes it: closed any other way (Escape, a click outside it) while the
    permission is pending and its tab in view, it is drawn again. Returns the permission
    offered, or None with none pending; `unless` is one already offered, not offered again.
    `display-menu` waits for the choice: on its own thread for `follow`, which goes on
    listening, and here with `wait` for a command that would otherwise exit before the menu
    was drawn. One that could not be drawn says why on the status line, and
    `PERMISSION_KEY` draws it again."""
    item = next((i for i in client.call("questions") if i["kind"] == "permission"
                 and i["tab"] == tab), None)
    if item is None or item["id"] == unless:
        return None if item is None else unless
    menu = _permission_menu(item, tab)
    if menu is None:
        return None
    answered = _answered(item)

    def show() -> None:
        command = menu
        while command:
            client_name = command[command.index("-c") + 1]
            proc = subprocess.run(command, capture_output=True, text=True)
            if proc.returncode != 0:
                _tmux("display-message", "-c", client_name,
                      f"permission {item['id']}: the menu could not be drawn: "
                      f"{proc.stderr.strip()}".replace("#", "##"), check=False)
                return
            if _tmux("show-options", "-gqv", answered).stdout.strip():
                _tmux("set-option", "-gu", answered)
                return
            pending = any(i["id"] == item["id"] for i in client.call("questions"))
            if not pending or client.call("status")["terminal"]["viewing"] != tab:
                return
            command = _permission_menu(item, tab)
    if menu:
        if wait:
            show()
        else:
            threading.Thread(target=show, name=f"permission-{item['id']}", daemon=True).start()
    return item["id"]


def _answered(item: dict) -> str:
    """The tmux option a choice sets before its `run-shell`: its commands run on the terminal's
    client, not the one `display-menu` returns on, and this is how the return tells a choice
    from a close."""
    return f"@answered-{item['id']}"


def _permission_menu(item: dict, tab: str) -> list[str] | None:
    """The `display-menu` command for `item`, sized to the attached terminal as it is now; None
    with no terminal attached, and [] for one too small, which is told so on its status line."""
    clients = _tmux("list-clients", "-t", SESSION, "-F",
                    "#{client_name} #{client_width} #{client_height}").stdout.split("\n")
    attached = next((c.split() for c in clients if c.strip()), None)
    if attached is None:
        return None
    width, height = int(attached[1]), int(attached[2])
    # tmux draws nothing, and exits 0, for a menu larger than the client, and a menu is at
    # least as wide as its title: the message is wrapped into the menu's own lines instead.
    # A line is disabled by a leading `-`, so the entries follow `--` or the first is an option.
    lines = textwrap.wrap(item["message"], max(width - 4, 1))
    items = [x for line in lines for x in ("-" + line.replace("#", "##"), "", "")] + [""]
    for choice, key in (("yes", "y"), ("no", "n")):
        for scope, suffix, label in ((None, "", ""),
                                     ("project", "p", f", always in {item['project']}"),
                                     ("everywhere", "e", ", always everywhere")):
            answer = f"rai ai permission {item['id']} --answer {choice}" + (
                f" --always {scope}" if scope else "")
            items += [f"{choice.capitalize()}{label}".replace("#", "##"),
                      key if not suffix else (suffix if choice == "yes" else suffix.upper()),
                      f"set-option -g {_answered(item)} 1 ; "
                      f"run-shell -b \"{answer} 2>&1 | systemd-cat -t rai-ai\""]
    title = f" {tab} asks permission "
    if len(title) + 4 > width or len(lines) + 1 + 6 + 2 > height:
        _tmux("display-message", "-c", attached[0],
              f"permission {item['id']}: the terminal ({width}x{height}) is too small for its "
              f"menu; enlarge it and press prefix+{PERMISSION_KEY}", check=False)
        return []
    # `-M`: a menu not opened by a click takes no mouse, and closes at any click, without it.
    return ["tmux", "display-menu", "-M", "-c", attached[0], "-t", f"{SESSION}:{tab}*",
            "-T", title, "-x", "C", "-y", "C", "--", *items]


def tab_menu(client_name: str) -> list[str]:
    """The `display-menu` command listing every window in the bar's order, marked as the bar
    marks them, the current one `▸`; choosing one selects it. Opened by a click on the `≡`,
    from `run-shell`, so it carries no mouse event of its own: `-M` gives it the mouse."""
    lines = _tmux("list-windows", "-t", SESSION, "-F",
                  "#{window_id}\t#{window_name}\t#{window_active}\t#{@marked}\t#{@managed}"
                  ).stdout.splitlines()
    items: list[str] = []
    for n, line in enumerate(lines, start=1):
        window_id, name, active, marked, managed = line.split("\t")
        # `_keep` writes both marks as "1" or "0"; a window it has not reached has neither.
        label = (("▸ " if active == "1" else "  ") + ("● " if marked == "1" else "")
                 + ("◇ " if managed == "1" else "") + name)
        items += [label.replace("#", "##"), str(n) if n < 10 else "",
                  f"select-window -t {window_id}"]
    height = int(_tmux("display-message", "-p", "-c", client_name,
                       "#{client_height}").stdout.strip())
    if len(lines) + 2 > height:
        raise TerminalError(f"the terminal is {height} lines high, too few for a menu of "
                            f"{len(lines)} tabs")
    return ["tmux", "display-menu", "-M", "-c", client_name, "-T", " Tabs ",
            "-x", "0", "-y", "S", "--", *items]


def _container_running(container: str) -> bool:
    proc = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", container],
                          capture_output=True, text=True)
    return proc.stdout.strip() == "true"


def main(args) -> int:
    from raigolmid.client import ApiClient, ApiError
    from raigolmid.paths import Paths

    client = ApiClient(Paths.from_env().api_socket)
    action = getattr(args, "action", "attach")

    try:
        if action == "list":
            status = client.call("status")
            for agent in status["agents"]:
                print(f"{agent['tab']:<10} "
                      f"{window_name(agent['tab'], agent['scope'])}  [{agent['status']}]")
            return 0

        if action == "ready":
            show_agent(client)
            return 0

        if action in ("kill", "restart", "follow", "viewing", "menu") and not args.tab:
            print(f"rai ai {action} needs a tab", file=sys.stderr)
            return 2

        if action == "kill":
            # A tab that always exists opens afresh: the user lands on the new one.
            scope = next((a["scope"] for a in client.call("status")["agents"]
                          if a["tab"] == args.tab), None)
            # A body's tab reopens at once in its place; gone from the bar for a moment is
            # what shows the user the × did something. The lock is taken
            # first because the tab's window closes itself when its tab does, and the
            # closed-window hook's `ready` would otherwise reopen it before the pause.
            with _one_at_a_time():
                status = client.call("close_tab", tab_id=args.tab)
                close_window(args.tab)
                time.sleep(CLOSED_SECONDS)
                sync_windows(client)
            again = _tab_of(status, scope)
            if again is not None and _typed():
                select_window(again)
            return 0

        if action == "restart":
            # Restart agent: the conversation is resumed, the container is new, and
            # the tab's window follows it there.
            client.call("restart_agent", tab_id=args.tab, resume=True)
            with _one_at_a_time():
                sync_windows(client)
            if not _typed():
                return 0
            select_window(args.tab)
            return attach()

        if action == "follow":
            _to_the_journal()
            return follow(client, args.tab)

        if action == "permission":
            return _permission(client, args)

        if action == "menu":
            # The client the `≡` was clicked on, from the status click (`STATUS_CLICK`). Why
            # a menu is not drawn is said on that client's status line, where it was asked.
            try:
                proc = subprocess.run(tab_menu(args.tab), capture_output=True, text=True)
                if proc.returncode != 0:
                    raise TerminalError(proc.stderr.strip())
            except TerminalError as exc:
                _tmux("display-message", "-c", args.tab,
                      f"the tab menu could not be drawn: {exc}".replace("#", "##"))
                raise
            return 0

        if action == "viewing":
            # The current window, by name, from the window-change hook (`ensure_session`).
            client.call("terminal_viewing", window=args.tab)
            return 0

        show_agent(client)
        if args.tab:
            select_window(args.tab)
        return attach()
    except (TerminalError, ApiError) as exc:
        print(f"rai ai: {exc}", file=sys.stderr)
        return 1


# The pause between the wait's frames: ten times faster than anything it reports on.
FRAME_SECONDS = 0.1
# Under this, a tab that opens promptly says nothing at all: a spinner that flashes up and
# vanishes is noise, and what is being answered here is a wait long enough to read as a break.
QUIET_SECONDS = 1.0


def while_it_opens(what: str, run):
    """Run `run()` on a worker and say what is happening until it returns.

    ⚠ **Opening a tab is this machine's longest wait and the only one it says nothing about.**
    The agent image is built once per machine, and the first tab either runs that build or
    waits on the one the daemon started at boot, a minute or two. The daemon client waits ten minutes before it gives up, so a
    terminal that prints nothing is indistinguishable from a broken one for longer than anyone
    will sit. The count of seconds is the part that matters: a number going up is a machine
    working, and that is the whole difference from a machine that has stopped.

    ⚠ Written to stdout and redrawn in place, not logged: this is a screen in a terminal the
    user is looking at. `attach` draws tmux over it the moment there is something to attach
    to, so the last thing written is a cleared line rather than a stale one. With no terminal
    (the closed-window hook, into the journal) there is nothing to redraw: the wait is said
    once and its end once.
    """
    import sys
    import time

    done: list = []
    failed: list = []

    def work() -> None:
        try:
            done.append(run())
        except BaseException as exc:        # noqa: BLE001 — re-raised below, never swallowed
            failed.append(exc)

    worker = threading.Thread(target=work, name="open-tab", daemon=True)
    worker.start()
    started = time.monotonic()
    frame = 0
    drew = False
    tty = sys.stdout.isatty()
    while worker.is_alive():
        waited = time.monotonic() - started
        if waited >= QUIET_SECONDS:
            if not drew:
                drew = True
                _out(what + "\n"
                     "The agent image is built once per machine, so the first tab on a new "
                     "machine waits for it.\n")
            if tty:
                _out(f"\r\033[2K{SPINNER[frame % len(SPINNER)]}  {int(waited)}s")
        frame += 1
        time.sleep(FRAME_SECONDS)
    worker.join()
    if drew:
        _out("\r\033[2K" if tty else
             f"{'failed' if failed else 'done'} after {int(time.monotonic() - started)}s\n")
    if failed:
        raise failed[0]
    return done[0]


def _out(text: str) -> None:
    import sys
    sys.stdout.write(text)
    sys.stdout.flush()


NO_AGENT = "no-agent"


def show_agent(client) -> None:
    """The AI terminal is Claude Code: shown, it is on an agent tab. The daemon keeps the
    machine tab and the selected body's, and this asks it to before landing — on the
    selected body's tab, or the machine tab with none, unless a tab is already in view.

    ⚠ **Exactly one window is opened on, and the credential decides which.** With none
    stored the terminal is the question that asks for it and nothing else; with one, it is
    the agent's tab.

    AI is always ready (`rai ai ready`): this also runs when the token is given, when a window
    closes, and at the daemon's start, so there is a running agent to land on whenever a
    credential exists. ⚠ **One at a time, by a lock**: killing the no-agent window below fires
    the window-closed hook, and two of these at once would both open a window for one tab.

    When no agent can start — raigolmid down, above all — the reason is shown in
    a shell window, which is the terminal staying usable for the repair. Printed instead, it
    would be under tmux the moment the client attaches.
    """
    from raigolmid import credential
    from raigolmid.paths import Paths
    with _one_at_a_time():
        _ready(client, credential.is_set(Paths.from_env().agent_credentials))
    _say_viewing(client)


def _say_viewing(client) -> None:
    """The current window told to raigolmid (`raigolmid/viewing.py`): the window-change hook
    says only a change, and the terminal shown on the window already current is none. A daemon
    that cannot hear it is said here, and does not stop the terminal opening."""
    from raigolmid.client import ApiError
    name = next((w.name for w in list_windows() if w.active), None)
    if name is None:
        return
    try:
        client.call("terminal_viewing", window=name)
    except (ApiError, OSError) as exc:
        print(f"rai ai: raigolmid was not told which tab is in view: {exc}", file=sys.stderr)


def _ready(client, has_credential: bool) -> None:
    from raigolmid.client import ApiError
    ensure_session()
    if not has_credential:
        # No credential, no agent. The base window is already asking for one
        # (`_first_command`), so the terminal opens on that and on nothing else — opening an
        # agent here would replace the question with the failure that follows from not
        # answering it, after a flick through the window that was asking.
        return
    _tmux("kill-window", "-t", f"{SESSION}:{NO_AGENT}", check=False)
    try:
        status = while_it_opens("Opening the tabs.", lambda: client.call("ensure_tabs"))
        sync_windows(client)
        # The janitor is not a tab the user works in, so it is never the one landed on —
        # but one they are looking at is not moved off.
        current = next((w.tab for w in list_windows() if w.active), None)
        if current not in {a["tab"] for a in status["agents"]}:
            body = status["session"]["body"]
            land = _tab_of(status, {"body": body} if body is not None else "machine")
            if land is None:
                raise TerminalError("raigolmid opened no tab to land on; `rai events` says why")
            select_window(land)
    except (ApiError, TerminalError) as exc:
        shell = os.environ.get("SHELL") or "/bin/bash"
        message = (f"No agent could be started: {exc}\n"
                   "This is a shell. `rai ai ready` tries again; `rai status` and "
                   "`journalctl --user -u raigolmid` say what raigolmid is doing.")
        _tmux("new-window", "-t", SESSION, "-n", NO_AGENT, shell, "-c",
              f"printf '%s\\n' {shlex.quote(message)}; exec {shlex.quote(shell)}")


@contextlib.contextmanager
def _one_at_a_time():
    """Whatever reconciles windows holds this: two at once both see a tab without a window
    and both open one."""
    from raigolmid.paths import Paths
    with open(Paths.from_env().ai_ready_lock, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def sync_windows(client) -> None:
    """Windows are reconciled against raigolmid rather than tracked here: the daemon opens
    tabs, and cannot open a tmux window. Every tab has one window, a crashed one
    included: it says so there, and follows the tab when it comes back. A second window on a
    tab is closed, the one in view kept. Callers hold `_one_at_a_time`."""
    if not session_exists():
        return
    windows = list_windows()
    present = {w.tab for w in windows}
    for tab in present:
        same = sorted((w for w in windows if w.tab == tab), key=lambda w: not w.active)
        for extra in same[1:]:
            _tmux("kill-window", "-t", extra.id, check=False)
    status = client.call("status")
    for agent in status["agents"]:
        if agent["tab"] not in present:
            open_window(agent["tab"], agent["scope"], agent_command(agent["tab"]))
    arrange_windows(status)


def _rank(window: Window, scopes: dict[str, Any], body: str | None) -> tuple:
    """The tab bar's order, the tabs always there before those that come and go: the base
    window, then the janitor, the machine tab, the selected body's tab, and the other bodies'
    tabs as they were opened. A window that is no tab goes last."""
    if window.name == BASE or window.tab == NO_AGENT:
        return (0,)
    scope = scopes.get(window.tab)
    if scope == "janitor":
        return (1,)
    if scope == "machine":
        return (2,)
    if isinstance(scope, dict):
        number = window.tab.removeprefix("tab-")
        return (3 if scope["body"] == body else 4, int(number) if number.isdigit() else 0)
    return (5,)


def arrange_windows(status: dict) -> None:
    """The windows put in `_rank`'s order by swapping them among the indices they hold. A swap
    without `-d` fires no hook and leaves the current index where it was, with another window
    in it (tmux 3.7c; with `-d` it selects the destination), so the window in view is selected
    again once, if a swap moved it. A window that closed while this ran is arranged without
    (`_still_there`)."""
    windows = list_windows()
    scopes = {a["tab"]: a["scope"] for a in status["agents"]}
    body = status["session"]["body"]
    placed = [w.id for w in windows]
    wanted = [w.id for w in sorted(windows, key=lambda w: _rank(w, scopes, body))]
    current = next((i for i, w in enumerate(windows) if w.active), None)
    for i, window_id in enumerate(wanted):
        if placed[i] != window_id:
            j = placed.index(window_id)
            if not _still_there("swap-window", "-s", window_id, "-t", placed[i]):
                arrange_windows(status)
                return
            placed[i], placed[j] = placed[j], placed[i]
    if current is not None and placed[current] != windows[current].id:
        _still_there("select-window", "-t", windows[current].id)


def _still_there(*args: str) -> bool:
    """A tmux command on windows, False when tmux refused it because one it names has closed.
    A tab's window closes itself when its tab ends, outside `_one_at_a_time`, so a window read
    a moment ago can be gone; a refusal with every named window still listed is raised."""
    try:
        _tmux(*args)
    except TerminalError:
        if {a for a in args if a.startswith("@")} <= {w.id for w in list_windows()}:
            raise
        return False
    return True
