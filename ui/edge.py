"""A host surface on the screen: a layer surface that slides out of its edge.

The drawer and the history menu are each one of these, and the AI terminal's control runs
the same `Slide` on the same clock (`ui/host_control/control.py`). What they share is the
whole of how a host surface moves:

- **Sized once, never re-anchored.** The surface is its panel's largest extent plus its tab;
  open and closed differ only in its margin on its edge (`ui/slide.py`), negative while
  closed so the panel waits past the edge with only the tab on screen.
- **Moved on the frame clock.** A tick callback places it each frame the compositor draws.
- **Taking the pointer where it draws.** The input region is the tab while closed, the
  panel while open or closing, and both while opening. A region that shrank under the
  pointer on the way out would be a leave, and a leave closes it.
- **Asked through its socket** (`ui/surfaces.py`), answering once the slide has arrived.

⚠ The tab exists only while the surface is closed: drawn when a close arrives, clear as
an open begins. Clear rather than hidden, so it keeps its place in the layout and the panel
does not move inside the surface as it goes.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_foreign("cairo")

from gi.repository import GLib, Gtk  # noqa: E402

from ui import surfaces  # noqa: E402
from ui.layershell import set_margin  # noqa: E402
from ui.slide import Slide, margin  # noqa: E402

logger = logging.getLogger(__name__)

# The state each verb answers with.
OPEN, CLOSED = "open", "closed"


def on_main(act: Callable[[Callable[[str], None]], None], timeout: float) -> str:
    """Run `act` on GTK's thread and wait, on this one, for the answer it hands to its
    callback. What `act` raises is raised here, so the asker is told."""
    done = threading.Event()
    outcome: dict = {}

    def settle(answer: str) -> None:
        outcome["answer"] = answer
        done.set()

    def run() -> bool:
        try:
            act(settle)
        except Exception as exc:                # noqa: BLE001 — handed to the asker
            outcome["error"] = exc
            done.set()
        return GLib.SOURCE_REMOVE

    GLib.idle_add(run)
    if not done.wait(timeout):
        raise surfaces.SurfaceError(f"no answer within {timeout:.0f}s")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["answer"]


def drive(widget: Gtk.Widget, slide: Slide) -> None:
    """Move `slide` on `widget`'s frame clock until it arrives. Asked again while it is
    still moving, it adds nothing: one tick a frame, however often a slide is redirected."""
    if slide.driven:
        return
    slide.driven = True

    def tick(_widget, clock) -> bool:
        if slide.frame(clock.get_frame_time() / 1_000_000):
            return GLib.SOURCE_CONTINUE
        slide.driven = False
        return GLib.SOURCE_REMOVE
    widget.add_tick_callback(tick)


class EdgeSurface:
    """One layer surface sliding out of `edge`: `panel` is what comes out, `tab` is what is
    left at rest, and `room` is the depth reserved for the panel along the edge's axis.
    `panel_depth` says how deep the panel is now, which may be less than `room`.

    `opening` and `closing` are told as a slide towards open or closed begins — the drawer
    takes the keyboard and gives it back there."""

    def __init__(self, window: Gtk.Window, *, name: str, edge: str, tab: Gtk.Widget,
                 panel: Gtk.Widget, room: int, panel_depth: Callable[[], int],
                 opening: Callable[[], None] = lambda: None,
                 closing: Callable[[], None] = lambda: None) -> None:
        self.window, self.name, self.edge = window, name, edge
        self.tab, self.panel = tab, panel
        self.room, self.panel_depth = room, panel_depth
        self.opening, self.closing = opening, closing
        self.slide = Slide(self._place)
        self._region: tuple | None = None
        self._place(0.0)

    # --- asking ----------------------------------------------------------------------
    def move(self, opened: bool, then: Callable[[str], None] | None = None, *,
             gesture: bool = False) -> None:
        """Slide open or closed. `gesture` is a person opening it, which closes the other
        surfaces; the machine opening it (a notice arriving) closes nothing."""
        arrived = None if then is None else (lambda target: then(OPEN if target else CLOSED))
        if opened and not self.slide.opened:
            if gesture:
                surfaces.close_others(self.name)
            self.tab.set_opacity(0.0)
            self.opening()
        elif not opened and self.slide.opened:
            self.closing()
        if self.slide.to(1.0 if opened else 0.0, self._arrived):
            drive(self.window, self.slide)
        if arrived is not None:
            self.slide.to(self.slide.target, arrived)
        self.pointer_region()

    def serve(self, verb: Callable[[str, Callable[[str], None]], None] | None = None) -> None:
        """Answer this surface's socket. `verb` is given the verb and the callback for its
        answer, on GTK's thread; by default a verb moves the surface and nothing else."""
        act = verb or self._verb
        surfaces.serve(self.name, lambda v: on_main(lambda settle: act(v, settle),
                                                    surfaces.ANSWER_SECONDS - 1))

    def _verb(self, verb: str, settle: Callable[[str], None]) -> None:
        if verb == "state":
            settle(OPEN if self.slide.opened else CLOSED)
        elif verb == "toggle":
            self.move(not self.slide.opened, settle, gesture=True)
        else:
            self.move(verb == "open", settle, gesture=True)

    def jump(self, opened: bool) -> None:
        """Be open or closed now, with no slide: for a surface found already out, or moved by
        something other than its own gestures."""
        if opened and not self.slide.opened:
            self.tab.set_opacity(0.0)
            self.opening()
        elif not opened and self.slide.opened:
            self.closing()
        self.slide.jump(1.0 if opened else 0.0)
        self._arrived(self.slide.target)

    def refit(self) -> None:
        """The panel's depth changed: go where that depth puts it, open or closed, with no
        slide — the content changed, not the surface's state."""
        if not self.slide.moving:
            self._place(self.slide.position)
        self.pointer_region()

    # --- on screen -------------------------------------------------------------------
    def _place(self, position: float) -> None:
        set_margin(self.window, self.edge, margin(position, self.panel_depth(), self.room))

    def _arrived(self, target: float) -> None:
        if not target:
            self.tab.set_opacity(1.0)
        self.pointer_region()

    def pointer_region(self) -> None:
        """Take the pointer over what is drawn and nowhere else (the module's rule).

        ⚠ **A region reaches the compositor only with the next commit**, and at rest
        nothing commits again, so a changed region asks for a frame to carry it: without
        that, the region of the first frame — nothing — stays in force."""
        surface = self.window.get_surface()
        if surface is None:
            return
        if not (self.slide.opened or self.slide.moving):
            parts = [self.tab]
        elif self.slide.opened and self.slide.moving:
            parts = [self.panel, self.tab]
        else:
            parts = [self.panel]
        rects = [r for r in (self._bounds(w) for w in parts) if r is not None]
        region = tuple((r.x, r.y, r.width, r.height) for r in rects)
        if region == self._region:
            return
        self._region = region
        surface.set_input_region(cairo.Region(rects))
        self.window.queue_draw()

    def _bounds(self, widget: Gtk.Widget) -> cairo.RectangleInt | None:
        ok, bounds = widget.compute_bounds(self.window)
        if not ok or bounds.get_width() < 1 or bounds.get_height() < 1:
            return None
        dx, dy = self.window.get_surface_transform()
        return cairo.RectangleInt(int(bounds.get_x() + dx), int(bounds.get_y() + dy),
                                  int(bounds.get_width()), int(bounds.get_height()))
