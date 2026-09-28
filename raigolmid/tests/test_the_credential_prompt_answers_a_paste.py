"""The credential prompt has to show that a paste arrived without showing what arrived.

The defect this pins is not a crash: `getpass` reads the token perfectly and draws nothing,
so the one user this prompt exists for — first launch, a token on a clipboard, a terminal
they have not used before — cannot tell a paste that worked from a paste that never happened,
and concludes the terminal cannot paste. A mask per character is the whole of the fix, and
what it must never do is put the characters themselves on screen.
"""
from __future__ import annotations

import io
import os
import pty
import sys


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rai.prompt import MASK, masked                                  # noqa: E402

TOKEN = "sk-ant-oat01-EXAMPLE"


class Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


def _through_a_terminal(keystrokes: str) -> tuple[str, str]:
    """Drive `masked` on a real pty and return what it read and what it drew.

    A pty rather than a fake: the point of the function is what it does to a terminal's
    line discipline, and a double with an `isatty` that says yes would be a test that
    cannot fail the way the terminal can.
    """
    import termios
    import tty

    primary, secondary = pty.openpty()
    try:
        # ⚠ cbreak *before* the keys are written. A pty's line discipline acts on what is
        # already in its queue, so keystrokes fed to a canonical terminal are edited by the
        # driver — a backspace erases the character itself and the reader under test never
        # sees either. That is the terminal the user does not have: `masked` puts theirs in
        # cbreak before they touch a key.
        tty.setcbreak(secondary, termios.TCSANOW)
        os.write(primary, keystrokes.encode())
        with open(secondary, "r", closefd=False) as reader, io.StringIO() as drawn:
            value = masked("TOKEN: ", stream=reader, out=drawn)
            painted = drawn.getvalue()
    finally:
        os.close(primary)
        os.close(secondary)
    return value, painted


def test_a_pasted_token_is_read_whole():
    """foot sends a paste as its characters, so this is a paste as the prompt meets one."""
    value, _ = _through_a_terminal(f"{TOKEN}\r")
    assert value == TOKEN


def test_every_character_of_a_paste_draws_a_mark():
    """The feedback, and the whole reason this is not `getpass`: sixty characters arriving
    at once draw sixty marks at once, which is what says the paste landed."""
    _, painted = _through_a_terminal(f"{TOKEN}\r")
    assert painted.count(MASK) == len(TOKEN)


def test_the_token_itself_is_never_drawn():
    _, painted = _through_a_terminal(f"{TOKEN}\r")
    assert TOKEN not in painted
    for char in set(TOKEN):
        assert char not in painted.replace("TOKEN: ", ""), f"{char!r} reached the screen"


def test_a_pipe_is_read_as_a_line_and_draws_nothing():
    """`echo $TOKEN | rai credential --set`. There is no terminal to mask for."""
    drawn = io.StringIO()
    assert masked("TOKEN: ", stream=Pipe(f"{TOKEN}\n"), out=drawn) == TOKEN
    assert drawn.getvalue() == ""


