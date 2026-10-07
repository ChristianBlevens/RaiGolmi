"""A drag in the AI terminal copies when the button comes up, wherever that is: tmux names the
drag's end after the place it lands, and one that overshoots the text lands on the scrollbar, a
border or the tab bar."""
from __future__ import annotations

from ui.ai_terminal.terminal import COPY_MODE

# Every location tmux 3.7 gives a mouse key (`key-string.c`); `bind-key` refuses any other.
LOCATIONS = ("Pane", "Border", "Status", "StatusLeft", "StatusRight", "StatusDefault",
             "ScrollbarUp", "ScrollbarSlider", "ScrollbarDown")


def test_a_drag_ending_anywhere_copies():
    for where in LOCATIONS:
        assert COPY_MODE.get(f"MouseDragEnd1{where}") == "send-keys -X copy-pipe-no-clear", where
