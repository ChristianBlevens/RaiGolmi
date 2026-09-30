"""There is one AI terminal, and three places name it.

`host/sway/config`'s floor binding, `hostkeys.DEFAULT_COMMANDS` and `rai ai` all have to
reach the same tmux session, or the reserved key and the command are two terminals that
cannot see each other's tabs. The name is a string in a compositor config that
no import can reach, so nothing but a test can hold it to the one in `terminal.py`.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ui.ai_terminal.terminal import SESSION             # noqa: E402

HOST_CONFIG = Path(__file__).resolve().parents[2] / "host" / "sway" / "config"


def _ai_binding() -> str:
    for line in HOST_CONFIG.read_text(encoding="utf-8").splitlines():
        if line.startswith("bindsym") and "raigolmi-ai" in line:
            return line
    raise AssertionError(f"no AI terminal binding in {HOST_CONFIG}")


def test_the_floor_binding_names_the_session_rai_ai_uses():
    session = re.search(r"-s\s+(\S+)", _ai_binding())
    assert session, "the floor binding no longer starts a named tmux session"
    assert session.group(1) == SESSION


def test_the_managed_terminal_is_in_the_scratchpad_before_it_is_drawn():
    """The band arrives from the edge and is seen nowhere else.

    `raigolmid` moves it to the scratchpad only after it has waited for it to map, and it is
    on the workspace for the whole of that wait — a window appearing for a moment on the
    first open of every boot. The rule runs at map time instead. It is a string in a
    compositor config that no import can reach, so nothing but a test holds it to the app_id
    `hostsurfaces` actually starts."""
    from ui.surfaces import TERMINAL_APP_ID as AI_TERMINAL_APP_ID
    rules = [line for line in HOST_CONFIG.read_text(encoding="utf-8").splitlines()
             if line.startswith("for_window") and "move scratchpad" in line]
    assert rules == [f'for_window [app_id="^{AI_TERMINAL_APP_ID}$"] move scratchpad']


def test_the_floor_binding_is_not_taken_by_that_rule():
    """⚠ The one way this breaks is silent: the floor is the terminal for a machine whose
    daemon never started, and a rule that swallowed it into the scratchpad would leave the
    reserved key drawing nothing at all — with no daemon to notice. sway matches an app_id
    as a regex anywhere in the string, so the anchors are what keeps them apart."""
    import re as _re
    from ui.surfaces import TERMINAL_APP_ID as AI_TERMINAL_APP_ID
    floor = _re.search(r"--app-id=(\S+)", _ai_binding())
    assert floor and floor.group(1) != AI_TERMINAL_APP_ID
    assert not _re.search(f"^{AI_TERMINAL_APP_ID}$", floor.group(1))


def test_the_base_window_command_cannot_exit():
    """A window whose command exits takes the window, and the last window takes the session.
    The terminal must be reachable with raigolmid down, so the base window's command is a
    shell and the status is a pane beside it that stays when it fails."""
    import inspect

    from ui.ai_terminal import terminal

    source = inspect.getsource(terminal.ensure_session)
    assert '"new-session", "-d", "-s", SESSION, "-n", BASE, *_base_command(' in source, source
    assert '"rai", "status", "--follow"' in source and '"remain-on-exit", "on"' in source
