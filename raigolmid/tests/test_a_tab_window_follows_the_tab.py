"""A tab's window follows the tab, not a container.

The daemon reopens a crashed tab in a new container, and it may not open a tmux window
itself, so the window already on screen is the only thing that can
put the reopened agent in front of the user. These drive `terminal.follow` against a daemon
that answers `status` from a script and streams the tab's events, with `docker attach`
replaced by what the daemon does while the window is attached.
"""
from __future__ import annotations

import io
import queue
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ui import theme

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ui.ai_terminal import terminal        # noqa: E402

TAB = "tab-1"
# A resumed agent's first output: its conversation, and the three queries Claude Code 2.1.283
# asks its terminal at start. Replayed, a query would be answered into the agent's input.
SHOWN = b"\x1b[1m> the first thing they asked\x1b[0m\r\n"
CONVERSATION = b"\x1b[c\x1b[>0q\x1b[?u\x1b[>1u" + SHOWN


class _Daemon:
    """`status` answers the next scripted state of the tab, None being a closed tab; an
    exhausted script is a read nobody expected, and fails the test."""

    def __init__(self, states: list[str | None]) -> None:
        self.states = iter(states)
        self.stream: queue.Queue = queue.Queue()

    def emit(self, kind: str, tab: str = TAB) -> None:
        self.stream.put({"type": kind, "tab": tab})

    def subscribe(self, subscribed):
        subscribed.set()
        while True:
            yield self.stream.get()

    def call(self, method: str, **_params):
        assert method == "status", method
        state = next(self.states)
        agents = [] if state is None else [{"tab": TAB, "scope": "machine", "status": state}]
        return {"agents": agents}


def _follow(monkeypatch, daemon: _Daemon, while_attached: list, *,
            detached: bool = False) -> tuple[int, int, str]:
    attaches: list = []
    written: list[str] = []
    pane = io.BytesIO()

    def run(command, **_kwargs):
        if command[:2] == ["docker", "logs"]:
            return subprocess.CompletedProcess(command, 0, stdout=CONVERSATION, stderr=b"")
        assert command[:2] == ["docker", "attach"], command
        assert pane.getvalue().count(SHOWN) == len(attaches) + 1, \
            "each container's conversation is in the pane before it is attached"
        attaches.append(command)
        while_attached[len(attaches) - 1](daemon)

    monkeypatch.setattr(terminal.subprocess, "run", run)
    monkeypatch.setattr(terminal.sys, "stdout", SimpleNamespace(buffer=pane))
    monkeypatch.setattr(terminal, "_container_running", lambda _name: detached)
    monkeypatch.setattr(terminal, "_out", written.append)
    # The mark reads `status` too; `test_a_tab_that_needs_the_user_marks_its_own_window` is its.
    monkeypatch.setattr(terminal, "_keep", lambda _client, tab, pane, offered: None)
    monkeypatch.setenv("TMUX_PANE", "%1")
    return terminal.follow(daemon, TAB), len(attaches), "".join(written)


def _reopened(daemon: _Daemon) -> None:
    daemon.emit("agent.busy", tab="tab-9")      # another tab's: never wakes this window
    daemon.emit("agent.crashed")
    daemon.emit("agent.restarted")


def _closed(daemon: _Daemon) -> None:
    daemon.emit("tab.closed")


def test_a_reopened_tab_is_attached_again_in_the_same_window(monkeypatch):
    # opened running; read crashed between the crash and its reopen; running; closed.
    daemon = _Daemon(["running", "crashed", "running", None])
    code, attaches, written = _follow(monkeypatch, daemon, [_reopened, _closed])
    assert (code, attaches) == (0, 2)
    assert "could not bring it back" not in written, "a crash being reopened is not final"


def test_the_replay_draws_everything_and_asks_the_terminal_nothing():
    assert terminal._QUERIES.sub(b"", CONVERSATION) == b"\x1b[>1u" + SHOWN


class _Tmux:
    """What `_tmux` was told, answering `list-clients` with one attached terminal of `size`.
    A menu larger than it is drawn by nothing and still exits 0, as tmux 3.5a's is: a menu is
    as wide as its title at least, and every line of it wider than the client is trimmed."""

    def __init__(self, monkeypatch, size: tuple[int, int] = (155, 21), dismiss: int = 0) -> None:
        self.told: list[tuple] = []
        self.menus: list[list[str]] = []
        self.size, self.dismiss = size, dismiss
        self.options: dict[str, str] = {}
        monkeypatch.setattr(terminal, "_tmux", self)
        monkeypatch.setattr(terminal.subprocess, "run", self.run)
        monkeypatch.setattr(terminal.threading, "Thread", _Inline)

    def __call__(self, *args, **_kwargs):
        self.told.append(args)
        out = ""
        if args[0] == "list-clients":
            out = "/dev/pts/0 %d %d\n" % self.size
        elif args[:2] == ("show-options", "-gqv"):
            out = self.options.get(args[2], "")
        elif args[:2] == ("set-option", "-gu"):
            self.options.pop(args[2], None)
        elif args[:2] == ("set-option", "-g"):
            self.options[args[2]] = args[3]
        return terminal.subprocess.CompletedProcess(args, 0, out, "")

    def run(self, command, **_kwargs):
        assert command[:2] == ["tmux", "display-menu"], command
        width, height = self.size
        title = command[command.index("-T") + 1].replace("##", "#")
        if len(title) + 4 <= width and len(_rows(command)) + 2 <= height:
            self.menus.append(command)
            if self.dismiss:
                self.dismiss -= 1
            else:
                # The first choice's commands, as tmux runs them on the terminal's client.
                chosen = next(r for r in _rows(command) if len(r) == 3 and r[2])
                for part in chosen[2].split(" ; "):
                    if part.startswith("set-option "):
                        self(*part.split())
        return terminal.subprocess.CompletedProcess(command, 0, "", "")


def _rows(menu: list[str]) -> list[tuple[str, ...]]:
    """A `display-menu` command's rows, read as tmux reads them: an empty name is a separator
    alone, anything else a name, a key and a command. An entry after the options that starts
    with `-` is itself an option unless `--` came first."""
    entries, rows, i = menu[menu.index("C", menu.index("-y")) + 1:], [], 0
    if entries[:1] == ["--"]:
        entries = entries[1:]
    else:
        assert not entries[0].startswith("-"), f"tmux reads {entries[0]!r} as its options"
    while i < len(entries):
        step = 1 if entries[i] == "" else 3
        rows.append(tuple(entries[i:i + step]))
        i += step
    return rows


class _Inline:
    def __init__(self, target, **_kwargs) -> None:
        self.target = target

    def start(self) -> None:
        self.target()


class _Machine:
    def __init__(self, state: str = "idle", marked: bool = True, viewing: str | None = None,
                 permission: dict | None = None, managed: bool = False) -> None:
        self.state, self.marked, self.viewing, self.managed = state, marked, viewing, managed
        self.items = [] if permission is None else [permission]
        self.answers: list[dict] = []

    def call(self, method, **params):
        if method == "status":
            return {"agents": [{"tab": TAB, "state": self.state, "marked": self.marked,
                                "managed": self.managed}],
                    "terminal": {"viewing": self.viewing, "lit": self.marked}}
        if method == "questions":
            return self.items
        assert method == "answer", method
        self.answers.append(params)


PERMISSION = {"id": "q4", "kind": "permission", "tab": TAB, "state": "pending",
              "message": "Swap the toolbelt to #python?", "project": "myapi"}


def test_a_tab_that_needs_the_user_marks_its_own_window(monkeypatch):
    """`@marked` on the window the follow runs in, which the tab list draws."""
    tmux = _Tmux(monkeypatch)
    machine = _Machine()
    terminal._keep(machine, TAB, "%7", None)
    machine.marked = False
    terminal._keep(machine, TAB, "%7", None)
    terminal._keep(machine, "tab-9", "%8", None)
    marks = [told for told in tmux.told if told[4] == "@marked"]
    assert marks == [("set-option", "-w", "-t", "%7", "@marked", "1"),
                     ("set-option", "-w", "-t", "%7", "@marked", "0")]
    assert "#{?@marked," in terminal.style(theme.look())["status-format[0]"]


def test_a_permission_is_offered_in_its_window_once_each_time_he_comes_to_it(monkeypatch):
    tmux = _Tmux(monkeypatch)
    machine = _Machine("permission", viewing="tab-9", permission=PERMISSION)
    assert terminal._keep(machine, TAB, "%7", None) is None, "not in view: not offered"
    machine.viewing = TAB
    offered = terminal._keep(machine, TAB, "%7", None)
    assert offered == "q4" and len(tmux.menus) == 1
    assert terminal._keep(machine, TAB, "%7", offered) == "q4" and len(tmux.menus) == 1
    machine.viewing = None
    assert terminal._keep(machine, TAB, "%7", offered) is None, "the user left"
    machine.viewing = TAB
    terminal._keep(machine, TAB, "%7", None)
    assert len(tmux.menus) == 2, "and came back"

    menu = tmux.menus[0]
    assert menu[menu.index("-c") + 1] == "/dev/pts/0"
    assert "-M" in menu, "not opened by a click, a menu takes no mouse without it"
    rows = _rows(menu)
    assert rows[:2] == [("-Swap the toolbelt to ##python?", "", ""), ("",)], \
        "shown, not chosen; a # is not a format"
    labels, keys, commands = zip(*rows[2:])
    assert labels == ("Yes", "Yes, always in myapi", "Yes, always everywhere",
                      "No", "No, always in myapi", "No, always everywhere")
    assert keys == ("y", "p", "e", "n", "P", "E")
    assert "rai ai permission q4 --answer yes --always project" in commands[1]


def test_a_permission_longer_than_the_terminal_is_wide_is_still_drawn(monkeypatch):
    """The toolbelt swap's message is 161 characters; their terminal was 155 wide."""
    tmux = _Tmux(monkeypatch, (155, 21))
    long = dict(PERMISSION, message="The agent wants to swap this sandbox's toolbelt to "
                "'myworkspace'. Its language servers and your terminals' shells in the sandbox "
                "end, and the body keeps running.")
    machine = _Machine("permission", viewing=TAB, permission=long)
    assert terminal._keep(machine, TAB, "%7", None) == "q4" and len(tmux.menus) == 1

    tmux = _Tmux(monkeypatch, (60, 8))
    assert terminal._keep(machine, TAB, "%7", None) == "q4" and tmux.menus == []
    assert "too small" in tmux.told[-1][-1], "a menu that cannot be drawn says so"


def test_only_an_answer_closes_the_permission_menu(monkeypatch):
    """Closed twice without a choice it is drawn again each time; once answered, it stays shut."""
    tmux = _Tmux(monkeypatch, dismiss=2)
    machine = _Machine("permission", viewing=TAB, permission=PERMISSION)
    assert terminal.offer_permission(machine, TAB) == "q4"
    assert len(tmux.menus) == 3 and tmux.options == {}

    tmux = _Tmux(monkeypatch, dismiss=1)
    menus = []
    real_run = tmux.run

    def leaves(command, **kwargs):
        menus.append(command)
        machine.viewing = None      # they hid the terminal while it was up
        return real_run(command, **kwargs)
    monkeypatch.setattr(terminal.subprocess, "run", leaves)
    terminal.offer_permission(machine, TAB)
    assert len(menus) == 1, "a tab out of view is offered again when it comes back, not now"


def test_the_menus_choice_answers_the_permission_and_the_key_puts_it_back(monkeypatch):
    tmux = _Tmux(monkeypatch)
    machine = _Machine("permission", viewing=TAB, permission=PERMISSION)
    args = lambda tab=None, answer=None, always=None: type(       # noqa: E731
        "Args", (), {"tab": tab, "answer": answer, "always": always})()
    terminal._permission(machine, args("q4", "no", "everywhere"))
    assert machine.answers == [{"id": "q4", "text": "no", "always": "everywhere"}]

    monkeypatch.setattr(terminal, "list_windows", lambda: [
        terminal.Window(tab=TAB, name=f"{TAB} machine", active=True, id="@1")])

    def no_thread(**_kwargs):
        raise AssertionError("a command that exits would take the menu's thread with it")
    monkeypatch.setattr(terminal.threading, "Thread", no_thread)
    terminal._permission(machine, args())
    assert len(tmux.menus) == 1
    machine.items = []
    terminal._permission(machine, args())
    assert tmux.told[-1] == ("display-message", f"{TAB} has no permission to answer")


def test_showing_the_terminal_says_which_window_is_current(monkeypatch, capsys):
    """The window-change hook says only a change; shown on the current window, the show says
    it (`raigolmid/viewing.py`). A daemon that cannot hear it does not stop the terminal."""
    from raigolmid.client import ApiError
    monkeypatch.setattr(terminal, "list_windows", lambda: [
        terminal.Window(tab="raigolmi", name="raigolmi", active=False, id="@0"),
        terminal.Window(tab=TAB, name=f"{TAB} machine", active=True, id="@1")])
    told: list[dict] = []

    class Daemon:
        def call(self, method, **params):
            assert method == "terminal_viewing"
            told.append(params)
    terminal._say_viewing(Daemon())
    assert told == [{"window": f"{TAB} machine"}]

    class Down:
        def call(self, method, **params):
            raise ApiError("raigolmid is not running")
    terminal._say_viewing(Down())
    assert "not told which tab is in view" in capsys.readouterr().err


class _Selecting:
    """`status` with the machine tab and myapi's, for `_follow_the_daemon`."""

    def call(self, method, **_params):
        assert method == "status", method
        return {"agents": [{"tab": "tab-1", "scope": "machine"},
                           {"tab": "tab-2", "scope": {"body": "myapi"}}]}


@pytest.mark.parametrize("by, lands", [("user", ["tab-2"]), ("tab-1", [])])
def test_only_the_users_selection_of_a_body_brings_its_tab_into_view(monkeypatch, tmp_path, by, lands):
    """A body the user selected brings its tab into view; an agent's selection opens the
    tab's window and moves nothing, since the agent is not them."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    synced, selected = [], []
    monkeypatch.setattr(terminal, "sync_windows", synced.append)
    monkeypatch.setattr(terminal, "select_window", selected.append)
    client = _Selecting()
    terminal._follow_the_daemon(client, {"type": "selection.changed", "kind": "body",
                                         "id": "myapi", "by": by})
    assert synced == [client] and selected == lands


def test_a_tab_the_daemon_opened_gets_its_window_and_takes_no_one_there(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    synced, selected = [], []
    monkeypatch.setattr(terminal, "sync_windows", synced.append)
    monkeypatch.setattr(terminal, "select_window", selected.append)
    terminal._follow_the_daemon(_Selecting(), {"type": "tab.opened", "tab": "tab-2",
                                               "body": "myapi", "by": "daemon"})
    assert len(synced) == 1 and selected == []


def test_a_tab_has_one_window_and_a_second_is_closed(monkeypatch):
    """Two reconciles at once once opened a tab's window twice; one found is closed, the one
    in view kept, and a tab with none gets one."""
    killed, opened = [], []
    monkeypatch.setattr(terminal, "session_exists", lambda: True)
    monkeypatch.setattr(terminal, "list_windows", lambda: [
        terminal.Window(tab="tab-4", name="tab-4 requests", active=False, id="@5"),
        terminal.Window(tab="tab-4", name="tab-4 requests", active=True, id="@6")])
    monkeypatch.setattr(terminal, "_tmux", lambda *a, **k: killed.append(a))
    monkeypatch.setattr(terminal, "open_window", lambda tab, scope, cmd: opened.append(tab))

    class Client:
        def call(self, method):
            return {"agents": [{"tab": "tab-4", "scope": {"body": "requests"}},
                               {"tab": "tab-5", "scope": "machine"}],
                    "session": {"body": "requests"}}
    terminal.sync_windows(Client())
    assert killed == [("kill-window", "-t", "@5")]
    assert opened == ["tab-5"]


class _Windows:
    """tmux's windows in index order: `list-windows` read from them, `swap-window` exchanging
    two in place, `select-window` moving the current one. Any other command is refused."""

    def __init__(self, monkeypatch, names: list[str], current: str) -> None:
        self.windows = [(f"@{i}", name) for i, name in enumerate(names)]
        self.current = next(i for i, (_, n) in enumerate(self.windows) if n == current)
        self.selected: list[str] = []
        monkeypatch.setattr(terminal, "session_exists", lambda: True)
        monkeypatch.setattr(terminal, "_tmux", self)

    def __call__(self, *args, **_kwargs):
        ids = [i for i, _ in self.windows]
        out = ""
        if args[0] == "list-windows":
            out = "".join(f"{i}\t{n}\t{int(k == self.current)}\n"
                          for k, (i, n) in enumerate(self.windows))
        elif args[0] == "swap-window" and args[1] == "-s" and args[3] == "-t":
            a, b = ids.index(args[2]), ids.index(args[4])
            self.windows[a], self.windows[b] = self.windows[b], self.windows[a]
        elif args[:2] == ("select-window", "-t"):
            self.current = ids.index(args[2])
            self.selected.append(args[2])
        else:
            raise AssertionError(f"tmux {args} is not what arranging the windows asks")
        return terminal.subprocess.CompletedProcess(args, 0, out, "")

    def names(self) -> list[str]:
        return [n for _, n in self.windows]


def test_the_tabs_are_in_order_janitor_machine_selected_body_then_the_rest(monkeypatch):
    tmux = _Windows(monkeypatch, ["raigolmi", "tab-12 notes", "tab-3 api", "tab-11 machine",
                                  "janitor ⚙", "tab-7 web"], current="tab-3 api")
    terminal.arrange_windows({
        "agents": [{"tab": "janitor", "scope": "janitor"}, {"tab": "tab-11", "scope": "machine"},
                   {"tab": "tab-3", "scope": {"body": "api"}},
                   {"tab": "tab-7", "scope": {"body": "web"}},
                   {"tab": "tab-12", "scope": {"body": "notes"}}],
        "session": {"body": "notes"}})
    assert tmux.names() == ["raigolmi", "janitor ⚙", "tab-11 machine", "tab-12 notes",
                            "tab-3 api", "tab-7 web"]
    assert tmux.names()[tmux.current] == "tab-3 api", "the window in view stays in view"


class _Restarting(_Daemon):
    """A daemon that restarts once while the window is attached: its first stream ends, and
    it is unreachable for the next `status` read, as a daemon between two processes is."""

    END = {"type": "__end__"}

    def __init__(self, states):
        super().__init__(states)
        self.streams = 0
        self.down_once = True

    def subscribe(self, subscribed):
        self.streams += 1
        subscribed.set()
        while True:
            event = self.stream.get()
            if event is self.END:
                return
            yield event

    def call(self, method: str, **params):
        if self.down_once:
            self.down_once = False
            from raigolmid.client import ApiError
            raise ApiError("raigolmid is not listening", kind="unreachable")
        return super().call(method, **params)


def test_a_daemon_restart_is_outlived_by_the_window(monkeypatch):
    """The daemon ending its stream is a restart, which every host surface outlives: the
    window follows the next stream and reads the tab once the daemon answers, and does not
    close under the user."""
    import ui.hostevents
    monkeypatch.setattr(ui.hostevents, "RECONNECT_SECONDS", 0.01)
    daemon = _Restarting(["running", "running", None])
    daemon.down_once = False                  # up for the window's first read

    def restart_then_close(d: _Restarting) -> None:
        d.stream.put(_Restarting.END)
        d.down_once = True
        import time
        deadline = time.monotonic() + 5
        while d.streams < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        d.emit("agent.restarted")

    code, attaches, _ = _follow(monkeypatch, daemon,
                                [restart_then_close, _closed])
    assert daemon.streams >= 2, "the window did not follow the daemon's next stream"
    assert (code, attaches) == (0, 2)


class _BaseWindow:
    """The base window as tmux keeps it: its status pane and its shell pane, what runs in the
    shell, the window's options, and `respawn-pane -k` replacing what runs. Any other command
    is refused, and so is the status pane as a target."""
    SHELL, STATUS = "%1", "%0"

    def __init__(self, monkeypatch, running: str) -> None:
        self.running, self.options, self.respawned = running, {}, []
        monkeypatch.setenv("SHELL", "/bin/bash")
        monkeypatch.setattr(terminal, "_tmux", self)

    def __call__(self, *args, **_kwargs):
        window = f"={terminal.SESSION}:={terminal.BASE}"
        out = ""
        if args[:4] == ("show-options", "-wqv", "-t", window):
            out = self.options.get(args[4], "")
        elif args == ("list-panes", "-t", window, "-F", "#{pane_id} #{@status}"):
            out = f"{self.STATUS} on\n{self.SHELL} \n"
        elif args == ("display-message", "-p", "-t", self.SHELL, "#{pane_current_command}"):
            out = self.running
        elif args[:4] == ("set-option", "-w", "-t", window):
            self.options[args[4]] = args[5]
        elif args[:4] == ("respawn-pane", "-k", "-t", self.SHELL):
            self.respawned.append(args[4:])
            self.running = "rai"
        else:
            raise AssertionError(f"tmux {args} is not what the base window is asked")
        return terminal.subprocess.CompletedProcess(args, 0, out, "")


def test_a_claude_login_that_ended_is_asked_again_in_the_base_window_once(monkeypatch):
    base = _BaseWindow(monkeypatch, running="bash")
    terminal.offer_claude_login("100.5")
    [(shell, flag, command)] = base.respawned
    assert (shell, flag) == ("/bin/bash", "-c")
    assert command == ("trap : INT; rai claude-login --login; /bin/bash; "
                       'exec tmux kill-window -t "$TMUX_PANE"')
    base.running = "bash"               # signed in, back at the shell
    terminal.offer_claude_login("100.5")
    assert len(base.respawned) == 1, "every tab window hears the ending; it is asked once"
    terminal.offer_claude_login("200.0")
    assert len(base.respawned) == 2, "a later ending is asked again"


def test_a_base_window_running_a_command_is_left_to_the_user(monkeypatch):
    base = _BaseWindow(monkeypatch, running="vim")
    terminal.offer_claude_login("100.5")
    assert base.respawned == [] and base.options == {}


def test_the_tab_menu_lists_every_tab_in_the_bars_order_and_selects_it(monkeypatch):
    """The bar is cut at the terminal's right edge; the `≡` menu reaches every tab."""
    # As `_keep` writes the marks: "1" or "0"; the base window, which it never reaches, has none.
    rows = ["@0\traigolmi\t0\t\t", "@4\tjanitor ⚙\t0\t0\t0", "@2\ttab-11 machine\t1\t0\t0",
            "@7\ttab-10 myapi\t0\t1\t1"]

    def tmux(*args, **_kwargs):
        if args[0] == "list-windows":
            out = "\n".join(rows) + "\n"
        elif args[:2] == ("display-message", "-p"):
            out = "30\n"
        else:
            raise AssertionError(f"tmux {args} is not what the tab menu asks")
        return terminal.subprocess.CompletedProcess(args, 0, out, "")
    monkeypatch.setattr(terminal, "_tmux", tmux)
    command = terminal.tab_menu("/dev/pts/3")
    assert command[:7] == ["tmux", "display-menu", "-M", "-c", "/dev/pts/3", "-T", " Tabs "]
    items = command[command.index("--") + 1:]
    assert [tuple(items[i:i + 3]) for i in range(0, len(items), 3)] == [
        ("  raigolmi", "1", "select-window -t @0"),
        ("  janitor ⚙", "2", "select-window -t @4"),
        ("▸ tab-11 machine", "3", "select-window -t @2"),
        ("  ● ◇ tab-10 myapi", "4", "select-window -t @7")]
