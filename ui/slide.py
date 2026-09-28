"""How every host surface moves: out of its edge and back, by position alone.

A surface's size never changes while it moves, so nothing reflows on the way and the
compositor never places a new size under an old buffer — the one fact about layer surfaces
that has cost this shell four defects (`ui/layershell.py`). What changes is a single number,
how far out it is, eased over the user's look's `reveal_ms`.

⚠ **The clock starts once the first frame has been drawn, not when the slide is asked for
and not at that frame's tick.** A tick comes before its frame's layout and paint, so a panel
built for the opening is painted in the frame after the first tick (300 ms for the history
menu's first panel), and a clock started at that tick runs out before it: the menu pops open.

Kept apart from GTK so the suite can drive it; `ui/edge.py` runs it on a frame clock.
"""
from __future__ import annotations

from collections.abc import Callable

from ui import theme


def ease(t: float) -> float:
    """Fast first and settling at the end: a linear slide reads as something being dragged."""
    t = min(1.0, max(0.0, t))
    return 1 - (1 - t) ** 2


class Slide:
    """Where a surface is between closed (0) and open (1), and where it is going.

    `place` is told every position the surface passes through. `arrived` is told the target
    once it is reached, however the slide got there — reversed half way, or asked for where
    it already was."""

    def __init__(self, place: Callable[[float], None], *, opened: bool = False,
                 seconds: float | None = None) -> None:
        self.place = place
        self.seconds = theme.look().reveal_ms / 1000 if seconds is None else seconds
        self.position = 1.0 if opened else 0.0
        self.target = self.position
        self._from = self.position
        self._started: float | None = None
        # Whether the start position has been placed and drawn once: the clock starts at the
        # frame after that.
        self._primed = False
        self._waiting: list[Callable[[float], None]] = []
        # Whether a frame clock is already moving it (`ui/edge.py` `drive`).
        self.driven = False

    @property
    def moving(self) -> bool:
        return self.position != self.target

    @property
    def opened(self) -> bool:
        """Open, or on its way there: what a toggle reverses."""
        return self.target == 1.0

    def to(self, target: float, then: Callable[[float], None] | None = None) -> bool:
        """Start towards `target`; returns whether a frame is needed to get there. A slide
        reversed half way goes back from where it is, over the part of the time that
        distance takes, rather than jumping to the end it was heading for."""
        if then is not None:
            self._waiting.append(then)
        if target != self.target:
            self.target, self._from = target, self.position
            self._started, self._primed = None, False
        if not self.moving:
            self._arrive()
            return False
        return True

    def frame(self, now: float) -> bool:
        """One frame at `now` (seconds): place the surface, and return whether another frame
        is needed."""
        if not self.moving:
            return False
        if not self._primed:
            self._primed = True
            self.place(self.position)
            return True
        if self._started is None:
            self._started = now
        span = self.seconds * abs(self.target - self._from)
        t = 1.0 if span <= 0 else (now - self._started) / span
        self.position = self.target if t >= 1 else \
            self._from + (self.target - self._from) * ease(t)
        self.place(self.position)
        if self.moving:
            return True
        self._arrive()
        return False

    def jump(self, position: float) -> None:
        """Be at `position` now, with no slide: for a surface moved by something other than
        its own gestures, such as the terminal's window closing."""
        self.position = self.target = self._from = position
        self._started, self._primed = None, False
        self.place(position)
        self._arrive()

    def _arrive(self) -> None:
        waiting, self._waiting = self._waiting, []
        for then in waiting:
            then(self.target)


def margin(position: float, panel: int, room: int) -> int:
    """The margin on its edge that puts a surface `position` of the way out.

    The surface is `room` deep along its axis plus its tab, and holds a panel `panel` deep at
    the tab's side of that room. Closed, all of the room is past the edge and only the tab is
    on screen; open, the panel's far side is on the edge. A panel that changes size is a
    changed margin, never a changed surface."""
    closed, opened = -room, -(room - min(panel, room))
    return round(closed + (opened - closed) * position)
