"""A terminal program's scrollback is raw PTY bytes, and a diagnostic that carries it
verbatim corrupts the terminal reading it — device-status *queries* in the stream get
answered by the receiving terminal, which types the reply at its shell. This is the one
place that turns those bytes back into words.
"""
from __future__ import annotations

from raigolmid.views import _readable

WEDGED_EDITOR = (
    "\x1b[?69;2$y\x1b=\x1b[4:3m\x1b(B\x1b(B    \n"
    "\x1b(Bdefaults.lua: Did not detect DSR response from terminal.\x1b(B\n"
    "\x1b(BError in VIMINIT..script /.face/editor/init.lua:\x1b(B\n"
    "\x1b(BE5113: Lua chunk: Vim:E739: Cannot create directory /work/.nvim-undo: "
    "permission denied\x1b(B\n"
    "\x1b(BPress ENTER or type command to continue\x1b(B\n"
    "\x1bP1$r0;4:3m\x1b\\\x1b]11;rgb:0c0c/0c0c/0c0c\x07\x1b[0n"
)


def test_nothing_that_can_drive_a_terminal_survives():
    out = _readable(WEDGED_EDITOR)
    assert "\x1b" not in out, "an escape character reached the reader's terminal"
    assert "(B" not in out, "a charset designator was left as literal noise"
    assert not any(c < " " and c not in "\n\t" for c in out)


def test_the_words_survive():
    out = _readable(WEDGED_EDITOR)
    assert "E5113" in out and "/work/.nvim-undo" in out
    assert "Press ENTER or type command to continue" in out


