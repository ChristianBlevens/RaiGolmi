"""The AI terminal's window, as sway holds it: brought out, placed, put away.

The control moves it (`control.py`); this is every sway command that takes, kept apart from
GTK so the suite can drive it against a compositor's socket. Everything goes over sway's IPC
(`ui.hostipc`), never a forked `swaymsg`: a slide is a command a frame.

⚠ **sway draws nothing ordinary above a fullscreen view**: a window mapped under a face is
`visible: false` and would be something nobody can see. The scratchpad is drawn over it — and
sway ends the face's fullscreen to do so and does not restore it, which `put_away` does.
"""
from __future__ import annotations

import threading

from ui import surfaces, theme
from ui.hostipc import find, find_app, run_command, tree

MATCHER = f'[app_id="^{surfaces.TERMINAL_APP_ID}$"]'
# A band along the bottom edge, the width of the screen: the face stays the thing being looked
# at, and the terminal is read where a terminal is read. In percentage points because the
# window it sizes is on whatever output the guest was resized to: the user's look's
# `terminal_height_percent`.
MAP_SECONDS = 10.0


class WindowError(RuntimeError):
    pass


class TerminalWindow:
    """`command` is what sway runs to start the window. `height` and `output` are the window's
    depth and the screen's, read back from the tree each time it is brought out; `out` is
    whether there is a window on screen to move."""

    def __init__(self, command: str, swaysock: str | None = None, *,
                 floor: int = 0) -> None:
        self.command, self.swaysock = command, swaysock
        # How much of the screen above the band is always left: the band's top edge is the
        # way to close it, and must stay on screen.
        self.floor = floor
        self.height = self.output = 0
        self.out = False
        # The face that was fullscreen when the window came out.
        self.fullscreen: int | None = None
        self.mapped = threading.Event()

    def bring_out(self) -> bool:
        """The window in its band, just below the screen, ready to slide up; returns whether
        it had to be started."""
        root = tree(self.swaysock)
        node = find_app(root, surfaces.TERMINAL_APP_ID)
        # A view, by its pid: sway's scratchpad workspace reports `fullscreen_mode: 1` too.
        face = find(root, lambda n: n.get("fullscreen_mode") == 1 and n.get("pid"))
        self.fullscreen = None if face is None else face["id"]
        output = output_height(root)
        geometry = (f"resize set 100 ppt {theme.look().terminal_height_percent} ppt, "
                    f"move position 0 px {output} px")
        started = node is None
        if started:
            self.mapped.clear()
            run_command(f"exec {self.command}", self.swaysock)
            if not self.mapped.wait(MAP_SECONDS):
                raise WindowError(f"`{self.command}` was started and mapped no window within "
                                  f"{MAP_SECONDS:.0f}s")
            # One chained command: issued separately, the resize is lost to the new
            # window's first configure and it stays at sway's default floating size.
            run_command(f"{MATCHER} move scratchpad, scratchpad show, {geometry}",
                        self.swaysock)
        else:
            # The geometry again on every show: sway centres a scratchpad window on the
            # workspace when it shows it, so a band set once does not stay a band.
            run_command(f"{MATCHER} scratchpad show, {geometry}", self.swaysock)
        node = find_app(tree(self.swaysock), surfaces.TERMINAL_APP_ID)
        if node is None:
            raise WindowError("the AI terminal's window went while it was being shown")
        self._measure(node, output)
        return started

    def place(self, position: float) -> None:
        """`position` of the way up: 0 just below the screen, 1 the band's full depth."""
        if self.out:
            y = self.output - round(position * self.height)
            run_command(f"{MATCHER} move position 0 px {y} px", self.swaysock)

    def put_away(self) -> None:
        """Into the scratchpad, and the face given its screen back — confirmed by reading the
        tree back, since sway's answer to a command is not evidence of its effect."""
        self.out = False
        run_command(f"{MATCHER} move scratchpad", self.swaysock)
        face, self.fullscreen = self.fullscreen, None
        if face is None:
            return
        run_command(f"[con_id={face}] fullscreen enable", self.swaysock)
        node = find(tree(self.swaysock), lambda n: n.get("id") == face)
        if node is not None and node.get("fullscreen_mode") != 1:
            raise WindowError(f"the AI terminal is hidden, and the face (con {face}) did not "
                              f"return to fullscreen")

    def found_out(self) -> bool:
        """Whether the window is already on screen, taking its measure if it is: a control
        started over a terminal that is out takes it as it is."""
        root = tree(self.swaysock)
        node = find_app(root, surfaces.TERMINAL_APP_ID)
        if node is None or not node.get("visible"):
            return False
        self._measure(node, output_height(root))
        return True

    def gone(self) -> None:
        """Its window closed under it: nothing to move and no face to restore."""
        self.out = False
        self.fullscreen = None

    def _measure(self, node: dict, output: int) -> None:
        self.output = output
        self.height = min(node["rect"]["height"], output - self.floor)
        self.out = True


def output_height(root: dict) -> int:
    outputs = [n for n in root.get("nodes", ()) if n.get("type") == "output"
               and n.get("name") != "__i3"]
    if not outputs:
        raise WindowError("the host compositor lists no output to show the terminal on")
    return outputs[0]["rect"]["height"]
