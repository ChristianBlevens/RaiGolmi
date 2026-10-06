"""What the history menu shows, decided apart from GTK so it can be tested.

Three shapes. At rest it is the **handle**, a short bar at the top centre, lit while the
history holds something the user has not seen in the open menu (`lit`, `unseen_through`). A notice,
a failure that is theirs, is shown **in full** when it arrives (`arrival`), because a lit handle
alone does not tell it from anything else new; it folds back into the handle
`ARRIVAL_SECONDS` after it came or after the pointer left it, never while the pointer is on it
or it holds the keyboard. Pointing at the handle opens the **menu**: every entry, newest
first; leaving it folds it, unless it holds the keyboard, which only overturning an answer
takes. Escape gives the keyboard back and folds, whatever the shape.

A new notice the menu starts with arrives too, so a failure from before this surface started
is put in front of the user once rather than left folded where they may not look.

The menu's own failure — the history unreadable, an answer not sent — arrives the same way,
once however often it is said, and keeps the handle lit: an answer not sent until the open
menu has shown it, the history unreadable until it reads again.
"""
from __future__ import annotations

ARRIVAL_SECONDS = 8.0

HANDLE, ARRIVAL, MENU = "handle", "arrival", "menu"


def _number(id: str) -> int:
    return int(id[1:])


class MenuModel:
    def __init__(self) -> None:
        self.shape = HANDLE
        self.entries: list[dict] = []
        self.arrivals: list[str] = []
        self.pointer_in = False
        self.holding = False
        self._known: set[str] = set()
        self._arrived_at = 0.0
        self.failure = ""
        self._failure_is_reading = False

    @property
    def lit(self) -> bool:
        """Something new, as the daemon keeps it (`History.entries`), or a failure unseen."""
        return bool(self.failure) or self._new

    @property
    def _new(self) -> bool:
        return any(e["new"] for e in self.entries)

    def unseen_through(self) -> str | None:
        """The newest entry, when the open menu is showing the user something new: all of it
        is seen once the menu is open, whatever they scroll to."""
        if self.shape != MENU or not self._new:
            return None
        return max((e["id"] for e in self.entries), key=_number)

    def shown(self, shape: str | None = None) -> list[dict]:
        """The entries drawn in `shape` (the current one by default), newest first."""
        shape = shape or self.shape
        if shape == ARRIVAL:
            return [e for e in reversed(self.entries) if e["id"] in self.arrivals]
        return list(reversed(self.entries)) if shape == MENU else []

    def fail(self, text: str, now: float, *, reading: bool) -> None:
        if text != self.failure and self.shape != MENU:
            self.shape, self._arrived_at = ARRIVAL, now
        self.failure, self._failure_is_reading = text, reading

    def take(self, entries: list[dict], now: float) -> None:
        if self._failure_is_reading:
            self.failure = ""
        self.entries = entries
        standing = {e["id"] for e in entries if e["notice"] and e["over"] is None}
        new = [e["id"] for e in entries
               if e["id"] in standing and e["new"] and e["id"] not in self._known]
        self._known |= {e["id"] for e in entries}
        self.arrivals = [id for id in self.arrivals if id in standing]
        if new and self.shape != MENU:
            self.arrivals += new
            self.shape, self._arrived_at = ARRIVAL, now
        elif self.shape == ARRIVAL and not self.arrivals and not self.failure:
            self._fold()

    def enter(self) -> None:
        self.pointer_in = True
        if self.shape == HANDLE:
            self.shape = MENU

    def open(self) -> None:
        """Asked through its socket rather than pointed at: the menu, until Escape or
        another surface closes it, since no pointer is on it to leave."""
        self.shape, self.arrivals = MENU, []

    def leave(self, now: float) -> None:
        self.pointer_in = False
        if self.shape == ARRIVAL:
            self._arrived_at = now
        elif self.shape == MENU and not self.holding:
            self._fold()

    def hold(self) -> None:
        self.holding = True

    def release(self) -> None:
        """The keyboard given back: an overturn sent, and the menu folds if the pointer is
        already elsewhere."""
        self.holding = False
        if self.shape == MENU and not self.pointer_in:
            self._fold()

    def escape(self) -> None:
        self.holding = False
        self._fold()

    def tick(self, now: float) -> None:
        if (self.shape == ARRIVAL and not self.pointer_in and not self.holding
                and now - self._arrived_at >= ARRIVAL_SECONDS):
            self._fold()

    def _fold(self) -> None:
        if self.shape == MENU and not self._failure_is_reading:
            self.failure = ""
        self.shape, self.arrivals = HANDLE, []
