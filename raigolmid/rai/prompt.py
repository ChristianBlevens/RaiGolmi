"""Reading a secret from the AI terminal without printing it and without swallowing it.

`getpass` is the obvious thing and it is the wrong thing here. It turns echo off at
the driver and gives back nothing at all until Enter, so a paste that worked and a paste that
never happened look identical — an empty line either way. The one user this prompt exists for
is meeting the machine for the first time, in a terminal, with a token on a clipboard they
cannot see the state of; handing them a black box and asking them to guess is the prompt
failing at the only job it has.

So the echo is ours rather than the driver's: one mask character per byte, which says *a
character arrived* and says nothing about which. A paste of sixty characters draws sixty
marks at once, which is the feedback, and the token is still not in the scrollback of a window
the user reopens all day.
"""
from __future__ import annotations

import sys

MASK = "•"
# What a terminal sends for the keys this has to answer, rather than pass through as text.
ENTER = ("\r", "\n")
BACKSPACE = ("\x7f", "\x08")
END_OF_TEXT = "\x04"
KILL_LINE = "\x15"


def masked(question: str, stream=None, out=None) -> str:
    """Ask `question` and return what was typed or pasted, drawing a mask for each character.

    ⚠ **cbreak, not raw, and the difference is the way out.** cbreak turns off line editing
    and echo and leaves ISIG alone, so ctrl-c is still the driver's business and still arrives
    as `KeyboardInterrupt` without this reader doing anything about it. Raw mode would deliver
    it as a byte instead, and a prompt that then had to recognise it — and would be a prompt
    with no way out on the day it did not. ctrl-d is not a signal and is answered here, as
    `EOFError`, the way a terminal's own reader answers it on an empty line.
    """
    stream = stream if stream is not None else sys.stdin
    out = out if out is not None else sys.stdout
    if not stream.isatty():
        # A pipe, which is `echo $TOKEN | rai credential --set`. There is nothing to draw for
        # and no terminal to put in raw mode; the line is the answer.
        line = stream.readline()
        if not line:
            raise EOFError
        return line.rstrip("\n")

    import codecs
    import os
    import termios
    import tty

    out.write(question)
    out.flush()
    fd = stream.fileno()
    saved = termios.tcgetattr(fd)
    typed: list[str] = []
    # ⚠ One byte from the descriptor, never `stream.read(1)`. A buffered text reader answers
    # a one-character read by trying to fill its buffer, so it blocks until a *chunk* arrives
    # and a prompt built on it hangs on the last key of every answer. The decoder is what
    # makes a byte at a time safe to do: it holds an incomplete character and returns nothing
    # until the rest of it lands.
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def key() -> str:
        while True:
            byte = os.read(fd, 1)
            if not byte:
                return ""
            char = decoder.decode(byte)
            if char:
                return char

    try:
        tty.setcbreak(fd, termios.TCSANOW)
        while True:
            char = key()
            if char == "" or (char == END_OF_TEXT and not typed):
                raise EOFError
            if char in ENTER:
                return "".join(typed)
            if char in BACKSPACE:
                if typed:
                    typed.pop()
                    # Back over the mask, paint a space where it was, back over that: a
                    # terminal moves the cursor and never unpaints what it passed.
                    out.write("\b \b")
                    out.flush()
                continue
            if char == KILL_LINE:
                out.write("\b \b" * len(typed))
                out.flush()
                typed.clear()
                continue
            if char.isprintable():
                typed.append(char)
                out.write(MASK)
                out.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        out.write("\n")
        out.flush()
