"""Where the keyboard is, in the three rows.

A GTK overlay has to own this. It is kept apart from the widgets because the selector
is tested without a screen, and everything here is arithmetic over the row lengths
the view-model already produced.

Movement never skips an unavailable item. They are greyed out **rather than hidden**,
and a cursor that refused to land on one would hide the reason the user is looking for.
"""
from __future__ import annotations

from dataclasses import dataclass

from ui.viewmodel import ROWS, Row


@dataclass(slots=True)
class Cursor:
    row: int = 0
    item: int = 0

    def clamp(self, rows: dict[str, Row]) -> None:
        """Rows change under the cursor whenever a selection changes what is available, so
        the position is re-validated against the new lengths rather than trusted."""
        self.row = max(0, min(self.row, len(ROWS) - 1))
        count = len(rows[ROWS[self.row]].items) if rows else 0
        self.item = 0 if count == 0 else max(0, min(self.item, count - 1))

    def next_item(self, rows: dict[str, Row], step: int = 1) -> None:
        """One step through every item there is, crossing from one section into the next.

        The sections are stacked, so up and down are the only directions the layout suggests
        and they are the only ones there are: a second key for "which section" would be a
        second idea to learn for a move the user can already see how to make."""
        places = self._places(rows)
        if not places:
            self.item = 0
            return
        here = (self.row, self.item)
        index = places.index(here) if here in places else 0
        self.row, self.item = places[(index + step) % len(places)]

    @staticmethod
    def _places(rows: dict[str, Row]) -> list[tuple[int, int]]:
        """Every (section, item) there is, in the order they are drawn. An empty section is
        not a place the cursor can be, and a machine with no bodies yet has one."""
        return [(r, i)
                for r, name in enumerate(ROWS)
                for i in range(len(rows[name].items) if rows else 0)]

    def focused(self, rows: dict[str, Row]):
        """The item under the cursor, or None when the row is empty — which is a real state:
        a machine with no bodies defined yet has one."""
        items = rows[ROWS[self.row]].items if rows else ()
        return items[self.item] if items else None

    @property
    def row_name(self) -> str:
        return ROWS[self.row]
