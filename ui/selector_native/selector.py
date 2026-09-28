"""The selector: three rows on a layer-shell overlay.

Drawn by the host over whatever face is fullscreen and toggled by the reserved host key.
It is a thin layer over the view-model (`ui/viewmodel`), which is why there is no
compatibility logic here — `SelectorModel` asks `raigolmid` and renders the answer.

What is genuinely this file's own is the drawing and the keyboard, and the keyboard's
arithmetic lives in `cursor.py` so it can be tested without a screen.

It is resident: started once, then opened and closed through its socket (`ui/surfaces.py`),
so the reserved key shows a surface already drawn rather than starting a container and GTK
per press (`raigolmid/hostsurfaces.py`). It moves as every host surface does (`ui/edge.py`).

At rest it is not gone but collapsed: a handle down the left edge, a few pixels of it, which
the pointer opens and leaving closes. That is the whole affordance — there is no button to
find, and the screen at rest is the face.
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from ui import theme  # noqa: E402
from ui import surfaces  # noqa: E402
from ui import copyable  # noqa: E402
from ui.edge import EdgeSurface  # noqa: E402
from ui.layershell import LayerShellUnavailable, overlay, set_keyboard  # noqa: E402
from ui.selector_native.cursor import Cursor  # noqa: E402
from ui.selector_native.visibility import hides_after  # noqa: E402
from ui.viewmodel import ROWS, SelectorModel  # noqa: E402

logger = logging.getLogger(__name__)

NAMESPACE = "raigolmi-selector"
REFRESH_SECONDS = 2
# The collapsed strip is the user's look's tab turned on its side: something to aim at, and the
# only thing this surface is while the drawer is closed. The open drawer takes their look's
# `drawer_share` of the output, capped at `drawer_max`. A share rather than a width: the guest
# is resized to whatever the launcher's window is, so a width that suits one size suits no
# other. The cap keeps it a drawer, not a sheet, on a wide screen instead of half a desk.

CSS = """
/* The window is only ever as big as what is drawn in it, so it carries no background of its
   own: collapsed, the surface is the handle and nothing else. */
window { background-color: transparent; color: $text; }
/* The padding is the drawer's own, not a margin around it: a margin sits outside the
   background, which left the sections against the right edge and inset on the left. */
.drawer { background-color: $bg; border-right: 1px solid $border; padding: 16px; }
.handle { background-color: $accent_bg; border-radius: 0 8px 8px 0; }
.handle:hover { background-color: $accent; color: $bg; }
.panel { background-color: $surface; border: 1px solid $border; border-radius: 8px;
         padding: 10px; }
.panel.focused { border-color: $accent_bg; }
.row-title { font-weight: bold; color: $accent; font-size: 1.1em; margin-bottom: 6px; }
.item { padding: 5px 10px; border-radius: 5px; font-size: 1.05em; }
.item-button { background: none; border: none; box-shadow: none; padding: 0; min-height: 0; }
.item-button:hover .item { background-color: $raised; }
.selected { font-weight: bold; color: $ok; }
.unavailable { color: $dim; }
.warned { color: $warn; }
.cursor { background-color: $accent_bg; box-shadow: inset 3px 0 $accent; }
.detail { color: $muted; }
.problem { color: $bad; }
.hints { color: $dim; font-size: 0.8em; }
.page-button { background: none; color: $muted; padding: 2px 8px; }
.page-button:hover { background-color: $raised; color: $text; }
"""

# What `on_key` answers, shown so no key has to be known beforehand — the three that are worth
# a line of a narrow drawer. Up and down are the whole of the movement, because the sections are
# stacked and one list runs through all three. `r` reloads and is not advertised: the two-second
# refresh means nobody needs it, and a hint that wraps costs more than it tells.
HINTS = (("↑ ↓", "move"), ("Enter", "select"), ("c", "catalog"), ("Esc", "close"))


def _wraps(label: Gtk.Label) -> None:
    """Let a label be narrower than its text.

    ⚠ A GTK label asks for the width of its longest line however it is wrapped, and a box asks
    for the width of its widest child — so one long line of detail sets the width of the whole
    drawer, and the drawer stops being one. `max_width_chars=1` with wrapping on is how a label
    is told to take the width it is given and wrap inside it."""
    label.set_wrap(True)
    label.set_max_width_chars(1)


class Selector:
    def __init__(self, model: SelectorModel, shown: bool) -> None:
        self.model = model
        # Whether it opens as soon as it is drawn: a selector started by the key, not at rest.
        self.start_open = shown
        self.cursor = Cursor()
        self.failure = ""
        self.detail = ""
        self.problem = False
        # The message of the toggle whose daemon call is still out, or None.
        self.pending: str | None = None
        # The last toggle's outcome, shown over the reloads until the next key.
        self.outcome: tuple[str, bool] | None = None
        # A reload on its worker, and whether another was asked for meanwhile.
        self.fetching = False
        self.fetch_again = False
        self.window: Gtk.Window | None = None
        self.edge: EdgeSurface | None = None
        self.drawer: Gtk.Box | None = None
        self.drawer_width = theme.look().drawer_max
        self.rows_box: Gtk.Box | None = None
        self.detail_label: Gtk.Label | None = None
        self.drawn: object = None
        self.toolbelt_label: Gtk.Label | None = None
        self.chevron: Gtk.Label | None = None

    # --- the frame -------------------------------------------------------------------
    def build(self, app: Gtk.Application) -> None:
        """⚠ An exception raised in a GTK signal handler is printed and swallowed, and
        `app.run()` then returns 0 having drawn nothing. The frame's refusal is recorded
        here so `main` can exit non-zero on it."""
        window = Gtk.ApplicationWindow(application=app)
        try:
            # The full height of the left edge and the widest drawer plus its tab, always:
            # open and closed are its margin (`ui/edge.py`). The keyboard is taken as it
            # opens, not here — a surface that is always up may not hold the keys the face
            # wants (`ui/layershell.py`).
            overlay(window, namespace=NAMESPACE, anchors=("left", "top", "bottom"))
        except LayerShellUnavailable as exc:
            self.failure = str(exc)
            logger.error("%s", exc)
            app.quit()
            return
        self.window = window

        theme.apply(CSS)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        # Stacked, not side by side: the sections down the drawer read as one list, and a
        # list needs only up and down. Columns needed a second key to say which column.
        self.rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

        self.toolbelt_label = copyable.label(xalign=0)
        self.toolbelt_label.add_css_class("detail")

        self.detail_label = copyable.label(xalign=0, wrap=True)
        _wraps(self.detail_label)
        self.detail_label.add_css_class("detail")

        # The rows and the line about them sit together in the middle of the drawer, which is
        # taller than they are: short panels pinned to the top of a full-height surface
        # read as something that failed to fill it.
        middle = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, vexpand=True,
                         valign=Gtk.Align.CENTER)
        middle.append(self.rows_box)
        middle.append(self.toolbelt_label)
        middle.append(self.detail_label)

        # The user's switch: on, no agent's input reaches the face they are looking at.
        self.no_driving, self.no_driving_handler = copyable.check(
            "Don't drive my current face", lambda active: self._set_driving(not active))
        self.no_driving.add_css_class("detail")
        outer.append(self.no_driving)

        # The catalog is its own window (`ui/catalog/`); opening it closes this drawer.
        catalog = copyable.button("Catalog", self._open_catalog, halign=Gtk.Align.END)
        catalog.add_css_class("page-button")
        outer.append(catalog)
        outer.append(middle)

        hints = copyable.label(xalign=0, valign=Gtk.Align.END)
        hints.set_markup("   ".join(
            f"<span foreground='{theme.look().muted}' weight='bold'>{key}</span>  {what}"
            for key, what in HINTS))
        _wraps(hints)
        hints.add_css_class("hints")
        outer.append(hints)

        outer.add_css_class("drawer")
        # At the tab's side of the room kept for the widest drawer, so a narrower one is a
        # smaller margin rather than a smaller surface.
        outer.set_halign(Gtk.Align.END)
        outer.set_hexpand(True)
        self.drawer = outer
        self._fit_drawer()
        room = Gtk.Box()
        look = theme.look()
        room.set_size_request(look.drawer_max, -1)
        room.append(outer)

        # What is on screen at rest, and the only thing that is. It goes when the drawer is
        # out: an open drawer is closed by leaving it, so a tab on top of it would be a control
        # for something already done.
        # No glyph on it: the image carries few fonts, and arrows and chevrons draw as
        # missing-glyph boxes. The shape is the affordance anyway.
        self.chevron = Gtk.Box(valign=Gtk.Align.CENTER)
        self.chevron.add_css_class("handle")
        self.chevron.set_size_request(look.tab_depth, look.tab_length)

        frame = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        frame.append(room)
        frame.append(self.chevron)
        window.set_default_size(look.drawer_max + look.tab_depth, -1)
        window.set_size_request(look.drawer_max + look.tab_depth, -1)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        window.add_controller(keys)

        # ⚠ On the window rather than on the tab widget: the window is what the compositor
        # delivers pointer events to, and whether GTK picks a particular child for them is a
        # question this does not need to ask. Closed, the input region is the tab alone, so
        # entering the surface is entering the tab.
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", self.on_pointer_enter)
        motion.connect("leave", self.on_pointer_leave)
        window.add_controller(motion)

        window.set_child(frame)
        # Fetched even when starting collapsed, so the first opening has rows to draw at once.
        self.refresh()
        GLib.timeout_add_seconds(REFRESH_SECONDS, self._tick)
        # The window is up from here on; what changes is whether the drawer is out.
        window.present()
        self.edge = EdgeSurface(window, name=surfaces.SELECTOR, edge="left", tab=self.chevron,
                                panel=outer, room=look.drawer_max,
                                panel_depth=lambda: self.drawer_width,
                                opening=self._opening, closing=self._closing)
        # After each paint: the tab's rectangle is where GTK laid it out, which follows the
        # output's height without a second copy of the centring.
        window.get_frame_clock().connect("after-paint", lambda _clock: self.edge.pointer_region())
        self.edge.serve()
        if self.start_open:
            self.edge.move(True, gesture=True)

    def _geometry(self):
        """The output's geometry, or None where the display lists no monitor — a headless one.

        Read each time rather than kept: the guest is resized to whatever the launcher's
        window is, so an output size read once at startup is a guess after the
        first drag of the window's corner."""
        assert self.window is not None
        monitors = self.window.get_display().get_monitors()
        monitor = monitors.get_item(0) if monitors.get_n_items() else None
        return None if monitor is None else monitor.get_geometry()

    def _drawer_width(self) -> int:
        geometry = self._geometry()
        if geometry is None:
            # A display that lists no monitor is a headless one; the drawer is still built so
            # the refusal is visible as a too-wide drawer rather than as no selector at all.
            logger.warning("no monitor listed; the drawer takes its maximum width")
            return theme.look().drawer_max
        return min(theme.look().drawer_max, int(geometry.width * theme.look().drawer_share))

    def _fit_drawer(self) -> None:
        """The drawer as wide as this output's share, read as it opens: the guest is resized
        to whatever the launcher's window is, so a width read once is a guess after the first
        drag of the window's corner."""
        assert self.drawer is not None
        self.drawer_width = self._drawer_width()
        self.drawer.set_size_request(self.drawer_width, -1)

    @property
    def shown(self) -> bool:
        return self.edge is not None and self.edge.slide.opened

    def close(self) -> None:
        if self.edge is not None:
            self.edge.move(False)

    def _opening(self) -> None:
        """What opening costs is paid here, before the slide's first frame, which is where
        its clock starts (`ui/slide.py`). The keyboard goes with the drawer: held while it is
        open so the arrows and Enter land here, given back as it closes so the face has a
        keyboard again."""
        assert self.window is not None
        self.outcome = None
        self._set_detail(*self._line())
        self._fit_drawer()
        self.draw()
        self.refresh()
        set_keyboard(self.window, True)

    def _closing(self) -> None:
        assert self.window is not None
        set_keyboard(self.window, False)

    def on_pointer_enter(self, _controller, _x: float, _y: float) -> None:
        """Touching the tab opens the drawer, at once. There is no dwell: the surface is
        exactly the tab, so the pointer is only ever here on purpose, and a wait before the
        slide is a wait the user reads as the machine being slow."""
        if not self.shown:
            self.edge.move(True, gesture=True)

    def on_pointer_leave(self, _controller) -> None:
        if self.shown:
            self.close()

    # --- state -----------------------------------------------------------------------
    def _tick(self) -> bool:
        if self.shown:
            self.refresh()
        return GLib.SOURCE_CONTINUE

    def refresh(self, detail: str | None = None, problem: bool = False) -> None:
        """Reload the model on a worker, then redraw. `detail` replaces the focused item's
        own line, which is how a toggle's result survives the reload that follows it.

        ⚠ Never on this thread: `status` asks the runtime, and the daemon can be slow for
        reasons that are not the selector's, which is exactly when it must still take keys.
        A reload asked for while one is out runs once that one lands."""
        if detail is not None:
            self.outcome = (detail, problem)
        if self.fetching:
            self.fetch_again = True
            return
        self.fetching = True
        threading.Thread(target=self._fetch, name="refresh", daemon=True).start()

    def _fetch(self) -> None:
        try:
            fetched, error = self.model.fetch(), None
        except Exception as exc:                       # noqa: BLE001
            fetched, error = None, exc
        GLib.idle_add(self._fetched, fetched, error)

    def _fetched(self, fetched, error: Exception | None) -> bool:
        self.fetching = False
        if error is not None:
            # The daemon being unreachable is a state to show, not a crash: the selector is
            # one of the two things on screen when everything else is broken.
            self._set_detail(f"raigolmid: {error}", problem=True)
        else:
            self.model.apply(fetched)
            self.cursor.clamp(self.model.rows)
            self._set_detail(*self._line())
        self.draw()
        if self.fetch_again:
            self.fetch_again = False
            self.refresh()
        return GLib.SOURCE_REMOVE

    def _line(self) -> tuple[str, bool]:
        """The detail line: a call still out, else the last outcome, else the focused item."""
        if self.pending is not None:
            return f"{self.pending} — applying…", False
        if self.outcome is not None:
            return self.outcome
        item = self.cursor.focused(self.model.rows)
        return (item.detail if item else ""), False

    def _set_detail(self, text: str, problem: bool) -> None:
        self.detail, self.problem = text, problem

    # --- drawing ---------------------------------------------------------------------
    def draw(self) -> None:
        if self.rows_box is None:
            return
        driving = (self.model.status or {}).get("face_runtime", {}).get("driving")
        if driving is not None:
            # Drawn from the daemon, so the toggle it answers to must not fire back.
            with self.no_driving.handler_block(self.no_driving_handler):
                self.no_driving.set_active(not driving["allowed"])
        assert self.toolbelt_label is not None and self.detail_label is not None
        copyable.set_text(self.toolbelt_label, self.model.toolbelt)
        copyable.set_text(self.detail_label, self.detail)
        (self.detail_label.add_css_class if self.problem
         else self.detail_label.remove_css_class)("problem")
        # Rebuilt only when what they show changed: a rebuild ends any selection in them.
        signature = (repr(self.model.rows), self.cursor.row, self.cursor.item)
        if signature == self.drawn:
            return
        self.drawn = signature
        while (child := self.rows_box.get_first_child()) is not None:
            self.rows_box.remove(child)

        for index, name in enumerate(ROWS):
            row = self.model.rows.get(name)
            panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            panel.add_css_class("panel")
            if index == self.cursor.row:
                panel.add_css_class("focused")
            panel.set_hexpand(True)
            title = copyable.label(row.title if row else name.capitalize(), "row-title",
                                   xalign=0)
            panel.append(title)
            for position, item in enumerate(row.items if row else ()):
                panel.append(self._item_button(index, position, item, focused=(
                    index == self.cursor.row and position == self.cursor.item)))
            self.rows_box.append(panel)

    def _item_button(self, row: int, position: int, item, focused: bool) -> Gtk.Button:
        """A click is Return on that item. Chosen on idle: `draw` destroys this button, and
        replacing children inside a pointer handler trips GTK's compute_point critical."""
        button = copyable.button(self._item_label(item, focused),
                                 lambda: GLib.idle_add(self._clicked, row, position))
        button.add_css_class("item-button")
        button.set_focus_on_click(False)
        return button

    def _clicked(self, row: int, position: int) -> bool:
        self.cursor.row, self.cursor.item = row, position
        self.cursor.clamp(self.model.rows)
        self.outcome = None
        self.toggle()
        return GLib.SOURCE_REMOVE

    def _item_label(self, item, focused: bool) -> Gtk.Label:
        badges = item.badges
        text = f"{item.marker} {item.name}" + (f"   {badges}" if badges else "")
        label = copyable.label(text, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        label.add_css_class("item")
        if not item.selectable:
            # Greyed out, not hidden: the user can see it exists and ask why it is refused.
            label.add_css_class("unavailable")
        elif item.selected:
            label.add_css_class("selected")
        elif item.warning:
            label.add_css_class("warned")
        if focused:
            label.add_css_class("cursor")
        return label

    def _open_catalog(self) -> None:
        """On a worker: the window's socket answers once it is shown, and the catalog asks
        this drawer to close as it opens, which this thread must be free to do."""
        def run() -> None:
            try:
                surfaces.ask(surfaces.CATALOG, "open")
            except surfaces.SurfaceError as exc:
                GLib.idle_add(self._catalog_refused, f"the catalog: {exc}")
        threading.Thread(target=run, name="catalog", daemon=True).start()

    def _catalog_refused(self, text: str) -> bool:
        self.refresh(text, problem=True)
        return GLib.SOURCE_REMOVE

    # --- the keyboard ----------------------------------------------------------------
    def on_key(self, _controller, keyval: int, _keycode: int, _state) -> bool:
        name = Gdk.keyval_name(keyval) or ""
        rows = self.model.rows
        if name == "c":
            self._open_catalog()
            return True
        if name in ("Escape", "q"):
            # Hidden, never closed: closing the only window ends the resident process.
            self.close()
            return True
        if not rows:
            # The first reload has not landed; there is nothing to move over yet.
            return True
        if name in ("Down", "j"):
            self.cursor.next_item(rows)
        elif name in ("Up", "k"):
            self.cursor.next_item(rows, -1)
        elif name in ("Return", "KP_Enter", "space"):
            self.toggle()
            return True
        elif name == "r":
            self.refresh()
            return True
        else:
            return False
        self.outcome = None
        self._set_detail(*self._line())
        self.draw()
        return True

    def toggle(self) -> None:
        """⚠ The daemon applies a selection before it answers, and a first selection pulls a
        toolbelt — over a minute on a fresh machine. Made on this thread, that call freezes the
        selector: no repaint, no cursor, no Escape. So it runs on a worker; `status` takes no
        session lock, and the two-second reload shows the selection land while it is out."""
        item = self.cursor.focused(self.model.rows)
        if item is None or self.pending is not None:
            return
        message, request = self.model.toggle_request(self.cursor.row_name, item.id)
        if request is None:
            # A refused selection reports the reason rather than doing nothing, which is
            # what makes "greyed out, not hidden" worth anything.
            self.refresh(message, problem=not item.selectable and not item.selected)
            return
        self.pending = message
        self.refresh()
        threading.Thread(target=self._call, args=(message, request), name="toggle",
                         daemon=True).start()

    def _set_driving(self, allowed: bool) -> None:
        """On a worker, like a selection: the drawer never waits on the daemon. The checkbox
        says what it is set to, so this call has no line of its own; only a refusal is shown."""
        threading.Thread(target=self._call, name="driving", daemon=True,
                         args=(None, ("set_face_driving", {"allowed": allowed}))).start()

    def _call(self, message: str | None, request: tuple[str, dict]) -> None:
        """`message` is None for a call that holds no `pending` and draws no line."""
        method, params = request
        try:
            self.model.call(method, **params)
            outcome = (message, False)
        except Exception as exc:                       # noqa: BLE001
            outcome = (f"raigolmid: {exc}", True)
        GLib.idle_add(self._called, *outcome, request, message is not None)

    def _called(self, message: str | None, problem: bool, request: tuple[str, dict],
                held_pending: bool) -> bool:
        if held_pending:
            self.pending = None
        self.refresh(message, problem)
        if hides_after(request, failed=problem):
            self.close()
        return GLib.SOURCE_REMOVE

def main(argv: list[str] | None = None) -> int:
    from raigolmid.client import ApiClient
    from raigolmid.paths import Paths

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="raigolmi-selector", description=__doc__)
    parser.add_argument("--hidden", action="store_true",
                        help="start resident and closed, for the first ask to open")
    args = parser.parse_args(argv)
    theme.load()

    paths = Paths.from_env()
    client = ApiClient(paths.api_socket)
    selector = Selector(SelectorModel(call=client.call), shown=not args.hidden)
    # ⚠ Not unique: the daemon keeps one by container name, and a unique id on the session
    # bus in the shared runtime dir makes a second instance hand over to the first and exit 0
    # having drawn nothing.
    app = Gtk.Application(application_id="os.raigolmi.selector", flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.connect("activate", selector.build)
    status = app.run([])
    return 1 if selector.failure else status


if __name__ == "__main__":
    sys.exit(main())
