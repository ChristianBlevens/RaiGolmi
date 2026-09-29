"""The always-on-top host affordance, and the AI terminal's surface: its tab, its top edge
while open, and the one thing that moves it.

It is the door that needs no prior knowledge, which is why it exists alongside the reserved
key rather than instead of it — a user who has not been told a key still has one. It is a tab
rather than a button because the screen belongs to the face: at rest this surface is a tab on
the bottom edge, and the pointer touching it opens the terminal.

While the terminal is open the same surface is its top edge: a line the width of the screen,
sitting exactly where the terminal ends. Moving the pointer up out of the terminal crosses
that line, and crossing it closes the terminal. What shows where the terminal ends is
therefore what closes it, and no icon is left over on top of it.

⚠ **Crossing the line is the closing gesture that always works.** Focus events are the other
one and they are not enough alone: with nothing selected there is no window above the terminal
to take focus, so leaving it upwards produces no event at all. Both are wired.

**This process moves the terminal**, as the drawer moves itself (`ui/edge.py`): the window and
this strip are placed in the same frame, on this surface's frame clock, over sway's IPC
(`ui.hostipc`) — so the line rides the terminal's edge because both are placed by one slide,
not because one follows the other. Every show, hide and toggle, from the key (`rai ai`), from
a face, or from a gesture here, is the terminal's socket (`ui/surfaces.py`), and the answer is
what this process did.

The terminal window's command is passed in rather than decided here: `raigolmid` owns this
surface and the reserved keys together, so a change to what `rai ai` is called reaches
every door at once.
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gio, GLib, Gtk  # noqa: E402

from raigolmid.client import ApiClient, ApiError  # noqa: E402
from raigolmid.paths import Paths  # noqa: E402
from ui import surfaces, theme  # noqa: E402
from ui import copyable  # noqa: E402
from ui.edge import EdgeSurface, on_main  # noqa: E402
from ui.hostevents import follow  # noqa: E402
from ui.host_control.window import MAP_SECONDS, TerminalWindow  # noqa: E402
from ui.hostipc import HostIpcError, subscribe  # noqa: E402
from ui.layershell import LayerShellUnavailable, overlay, set_margin  # noqa: E402

logger = logging.getLogger(__name__)

NAMESPACE = "raigolmi-host-control"
# The tab: something to aim at, and the only thing this surface is while the terminal is shut.
# Wider than it is tall because it sits on a horizontal edge — the selector's tab is the same
# shape turned on its side: the user's look's `tab_length` across, `tab_depth` up.
# The terminal's top edge while it is open: a hairline to look at, and a band to cross. The
# band is the taller of the two because a pointer moving quickly reports few positions on the
# way, and a line thin enough to look right is thin enough to jump over.
LINE_HEIGHT = 2
GUARD_HEIGHT = 16
# The strip's depth, above the tab and the band: room for a line of status and the note that
# an agent is driving the user's face. Fixed, so nothing it says changes where it takes the pointer
# (`ui/edge.py`).
STATUS_ROOM = 96
# What begins a line the control writes when it cannot read the daemon, which it takes back
# once the daemon answers.
STATUS_PREFIX = "status: "

# What can change whether a tab needs the user: its agent's turns, what it asks, what they
# view. And whether an agent is driving their face: its input, their switch, its turn ending.
LIGHT_ON = ("agent.", "question.", "channel.", "terminal.", "tab.", "face.driv",
            "subscriber.dropped")

CSS = """
window { background-color: transparent; }
.handle { background-color: $accent_bg; border-radius: 8px 8px 0 0; }
.handle:hover, .handle.lit { background-color: $accent; color: $bg; }
.handle.driven { background-color: $warn; }
.driven-note { color: $bg; background-color: $warn; border-radius: 6px; padding: 4px 10px; }
.line { background-color: $border; }
.status { color: $bad; background-color: $surface; border-radius: 6px; padding: 4px 10px; }
"""


class ControlError(RuntimeError):
    pass


class TerminalSurface(EdgeSurface):
    """The strip and the terminal window as one surface: the band is the panel, the tab is
    the tab, and a position places both, the band's bottom on the window's top edge."""

    def __init__(self, window: Gtk.Window, *, tab: Gtk.Widget, band: Gtk.Widget,
                 terminal: TerminalWindow, on_open: Callable[[bool], None]) -> None:
        self.band, self.terminal, self.on_open = band, terminal, on_open
        # What sway refused during a slide. A frame cannot raise — GTK would drop the clock
        # driving it — so the move that asked for the slide raises it once it has arrived.
        self.refused: HostIpcError | None = None
        super().__init__(window, name=surfaces.TERMINAL, edge="bottom", tab=tab, panel=band,
                         room=0, panel_depth=lambda: 0, opening=self._opening)

    def _opening(self) -> None:
        self.band.set_opacity(1.0)
        self.on_open(True)

    def _arrived(self, target: float) -> None:
        if not target:
            self.band.set_opacity(0.0)
            self.on_open(False)
        super()._arrived(target)

    def _place(self, position: float) -> None:
        # The band's bottom meets the terminal's top when open; closed, the tab is on the edge.
        depth = self.terminal.height if self.terminal.out else 0
        set_margin(self.window, "bottom", round(position * max(0, depth - theme.look().tab_depth)))
        if self.refused is None:
            try:
                self.terminal.place(position)
            except HostIpcError as exc:
                logger.error("moving the terminal: %s", exc)
                self.refused = exc


class Control:
    """The tab, the terminal's top edge, and the terminal's socket.

    The status line is not decoration: a refused launch is something the user is looking
    straight at, and a tab that swallowed it would be an edge that silently does nothing."""

    def __init__(self, terminal_command: str, swaysock: str | None = None, *,
                 on_open: Callable[[bool], None]) -> None:
        # Told each time the terminal comes on screen or leaves it: which tab the user views is
        # the daemon's to know (`raigolmid/viewing.py`), and only this process knows it.
        self.on_open = on_open
        self.terminal = TerminalWindow(terminal_command, swaysock, floor=theme.look().tab_depth * 4)
        self.swaysock = swaysock
        self.status: Gtk.Label | None = None
        self.handle: Gtk.Widget | None = None
        self.driven_note: Gtk.Label | None = None
        self.driver: str | None = None
        self.window: Gtk.Window | None = None
        self.surface: TerminalSurface | None = None
        self.failure = ""
        # A tab needs the user, as the daemon's `status` says; read again on its events.
        self.lit = False
        # One move at a time, whoever asks.
        self._moving = threading.Lock()

    def build(self, app: Gtk.Application) -> None:
        """⚠ GTK swallows an exception raised in a signal handler: it prints a traceback and
        carries on, and `app.run()` then returns 0 with nothing drawn. A control that exits
        successfully without a surface is the failure the user cannot see, so the frame's
        refusal is caught here, recorded, and turned into a non-zero exit by `main`."""
        window = Gtk.ApplicationWindow(application=app)
        try:
            # The width of the bottom edge and a fixed depth, always; what it takes the
            # pointer over is its input region (`ui/edge.py`).
            overlay(window, namespace=NAMESPACE, anchors=("bottom", "left", "right"))
        except LayerShellUnavailable as exc:
            self.failure = str(exc)
            logger.error("%s", exc)
            app.quit()
            return

        self.window = window
        theme.apply(CSS)
        # No spacing: between the band and the tab it would lift the line off the terminal's
        # top. The labels above keep their own gap.
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0, valign=Gtk.Align.END)

        self.status = copyable.label(wrap=True, halign=Gtk.Align.CENTER)
        self.status.set_margin_bottom(4)
        self.status.add_css_class("status")
        self.status.set_visible(False)
        box.append(self.status)

        self.driven_note = copyable.label(halign=Gtk.Align.CENTER)
        self.driven_note.set_margin_bottom(4)
        self.driven_note.add_css_class("driven-note")
        self.driven_note.set_visible(False)
        box.append(self.driven_note)

        # The band's bottom edge is the one placed on the terminal's top, so the line is
        # drawn there, with the band above it.
        band = Gtk.Box()
        band.set_size_request(-1, GUARD_HEIGHT)
        line = Gtk.Box(valign=Gtk.Align.END, hexpand=True)
        line.add_css_class("line")
        line.set_size_request(-1, LINE_HEIGHT)
        band.append(line)
        band.set_opacity(0.0)
        box.append(band)

        # ⚠ No glyph on it: the image carries a text font and the emoji font, and neither
        # covers a handle glyph (U+2303, U+25B4 draw as missing-glyph boxes).
        # The shape is the affordance anyway, which is what a phone's drawer handle is.
        self.handle = Gtk.Box(halign=Gtk.Align.CENTER)
        self.handle.set_size_request(theme.look().tab_length, theme.look().tab_depth)
        box.append(self.handle)
        self.light(self.lit, self.driver)

        # ⚠ On the window rather than on a child: the window is what the compositor delivers
        # pointer events to. Its input region is the tab or the band, so entering it is the
        # gesture either way.
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", self.on_pointer_enter)
        window.add_controller(motion)

        window.set_child(box)
        strip = theme.look().tab_depth + GUARD_HEIGHT + STATUS_ROOM
        window.set_default_size(-1, strip)
        window.set_size_request(-1, strip)
        self.surface = TerminalSurface(window, tab=self.handle, band=band,
                                       terminal=self.terminal, on_open=self.on_open)
        window.present()
        # A frame clock exists once the window is presented, not before.
        window.get_frame_clock().connect("after-paint",
                                         lambda _clock: self.surface.pointer_region())
        self._adopt()
        surfaces.serve(surfaces.TERMINAL, self.answer)
        threading.Thread(target=self._watch, name="terminal-watch", daemon=True).start()

    # --- the gesture ---------------------------------------------------------------------
    def on_pointer_enter(self, _controller, _x: float, _y: float) -> None:
        """Touching the tab opens the terminal; crossing its top edge closes it. At once in
        both directions: this surface is only ever under the pointer on purpose, and a wait
        before anything moves reads as the machine being slow.

        ⚠ Nothing at all while the terminal is moving. Leaving the terminal starts it down,
        the line rides down with it, and the pointer coming back onto the line would otherwise
        send it up again — a reversal the user did not ask for, and one that reads as having
        knocked something by accident."""
        if self.surface is None or self.surface.slide.moving:
            return
        self._ask("close" if self.surface.slide.opened else "open")

    def _ask(self, verb: str) -> None:
        """A gesture here asks what the key asks, off GTK's thread: opening may start the
        window and wait for it, and the slide it waits on runs on this thread's frames."""
        def run() -> None:
            try:
                self.answer(verb)
            except Exception as exc:                # noqa: BLE001 — said, on screen and in the log
                GLib.idle_add(self._say, f"{verb}: {exc}")
        threading.Thread(target=run, name=f"terminal-{verb}", daemon=True).start()

    # --- the terminal's socket -----------------------------------------------------------
    def answer(self, verb: str) -> str:
        """Move the terminal and say what was done: `started` (a window this started, which
        runs its own `rai ai`), `shown`, `hidden`, or `already` either. Runs off GTK's
        thread."""
        assert self.surface is not None
        with self._moving:
            opened = self.surface.slide.opened
            if verb == "state":
                return "shown" if opened else "hidden"
            if verb == "toggle":
                verb = "close" if opened else "open"
            if verb == "open":
                if opened:
                    return "already shown"
                started = self.terminal.bring_out()
                surfaces.close_others(surfaces.TERMINAL)
                self._slide(True)
                self._say_later("")
                return "started" if started else "shown"
            if not opened:
                return "already hidden"
            self._slide(False)
            self.terminal.put_away()
            self._say_later("")
            return "hidden"

    def _slide(self, opened: bool) -> None:
        self.surface.refused = None
        on_main(lambda settle: self.surface.move(opened, settle), MAP_SECONDS)
        if self.surface.refused is not None:
            raise ControlError(f"sway refused a frame of the slide: {self.surface.refused}")

    def _adopt(self) -> None:
        """A control started over a terminal that is already out takes it as it is."""
        try:
            if self.terminal.found_out():
                self.surface.jump(True)
        except (HostIpcError, RuntimeError) as exc:
            self._say(f"reading the terminal: {exc}")

    # --- what it says ---------------------------------------------------------------------
    def _say(self, message: str) -> bool:
        if message:
            logger.error("%s", message)
        assert self.status is not None
        copyable.set_text(self.status, message)
        self.status.set_visible(bool(message))
        return GLib.SOURCE_REMOVE

    def _say_later(self, message: str) -> None:
        GLib.idle_add(self._say, message)

    def daemon_answered(self) -> bool:
        """A `status` read that failed said so; one that answers takes it back."""
        if self.status is not None and self.status.get_text().startswith(STATUS_PREFIX):
            self._say("")
        return GLib.SOURCE_REMOVE

    def light(self, lit: bool, driver: str | None = None) -> bool:
        """`driver` is the tab driving the user's face, which the handle and a line say while
        it does, so an agent's hands on their screen are never unmarked."""
        self.lit = lit
        self.driver = driver
        if self.handle is not None:
            self.handle.set_css_classes(
                ["handle", *(["lit"] if lit else []), *(["driven"] if driver else [])])
        if self.driven_note is not None:
            copyable.set_text(self.driven_note,
                              f"{driver} is driving your screen" if driver else "")
            self.driven_note.set_visible(driver is not None)
        return GLib.SOURCE_REMOVE

    # --- what sway says ---------------------------------------------------------------------
    def _watch(self) -> None:
        """The terminal's window as sway reports it: mapped (what `_bring_out` waits for),
        closed (its session ended — the surface is closed with no slide), and focus moving to
        anything else while it is out, which is the second closing gesture — sway focuses
        whatever the pointer is over (`focus_follows_mouse`, its default and not overridden in
        `host/sway/config`), so moving onto a face is a window event naming that face."""
        try:
            for event in subscribe(["window"], self.swaysock):
                if isinstance(event, dict):
                    self._window_event(event)
        except HostIpcError as exc:
            # The tab still opens the terminal and its top edge still closes it; only the
            # focus-driven half is gone, and saying so is the difference between a missing
            # behaviour and a silent one.
            logger.error("no window events, so the terminal will not follow focus: %s", exc)
            GLib.idle_add(self._say, f"terminal watch: {exc}")

    def _window_event(self, event: dict) -> None:
        change = event.get("change")
        app_id = (event.get("container") or {}).get("app_id")
        if app_id == surfaces.TERMINAL_APP_ID and change == "new":
            self.terminal.mapped.set()
        elif app_id == surfaces.TERMINAL_APP_ID and change == "close":
            GLib.idle_add(self._closed_under_it)
        elif change == "focus" and app_id != surfaces.TERMINAL_APP_ID:
            GLib.idle_add(self._focus_left)

    def _closed_under_it(self) -> bool:
        if self.surface is not None and self.surface.slide.opened:
            self.terminal.gone()
            self.surface.jump(False)
        return GLib.SOURCE_REMOVE

    def _focus_left(self) -> bool:
        if (self.surface is not None and self.surface.slide.opened
                and not self.surface.slide.moving):
            self._ask("close")
        return GLib.SOURCE_REMOVE


def _report_open(client: ApiClient, control: Control, shown: bool) -> None:
    """Off the GTK thread: a socket call must not hold a frame."""
    def run() -> None:
        try:
            client.call("terminal_viewing", shown=shown)
        except (ApiError, OSError) as exc:
            logger.error("telling raigolmid the terminal is %s: %s",
                         "shown" if shown else "hidden", exc)
            GLib.idle_add(control._say, f"terminal_viewing: {exc}")
    threading.Thread(target=run, name="terminal-viewing", daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="host-control", description=__doc__)
    parser.add_argument("--terminal-command", required=True,
                        help="what sway runs to start the AI terminal's window")
    args = parser.parse_args(argv)
    theme.load()

    client = ApiClient(Paths.from_env().api_socket)
    control = Control(args.terminal_command,
                      on_open=lambda shown: _report_open(client, control, shown))
    # ⚠ Not unique: the daemon keeps one by container name, and a unique id on the session
    # bus in the shared runtime dir makes a second instance hand over to the first and exit 0
    # having drawn nothing.
    app = Gtk.Application(application_id="os.raigolmi.hostcontrol", flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.connect("activate", control.build)

    def fetch() -> None:
        try:
            status = client.call("status")
        except (ApiError, OSError) as exc:
            logger.error("asking raigolmid whether a tab needs the user: %s", exc)
            GLib.idle_add(control._say, f"{STATUS_PREFIX}{exc}")
            return
        GLib.idle_add(control.daemon_answered)
        GLib.idle_add(control.light, status["terminal"]["lit"],
                      status["face_runtime"]["driving"]["by"])
    threading.Thread(target=follow, args=(client, LIGHT_ON, fetch), name="events",
                     daemon=True).start()
    status = app.run([])
    return 1 if control.failure else status


if __name__ == "__main__":
    sys.exit(main())
