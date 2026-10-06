"""The history menu: what the agents and the machine did, so the user can understand them.

It sits top centre, and it is **passive**: appearing never takes the keyboard, because it
arrives while the user is doing something else and a surface that grabbed their keys mid-word would
put them in the wrong place. Clicking it takes the keyboard; Escape gives it back. Which shape it is in — the handle, a notice
shown in full, the hover-revealed menu — is `model.py`'s; this draws it.

Nothing in it asks for anything. A question is answered by typing in its tab and a permission
in its tab's window; the menu shows each, pending or settled and how. One the
preferences judge answered quotes the preference and can be overturned — another choice or
words — the one action it has. The answers kept *always* are listed and revoked in the
selector's catalog.

What is kept is the daemon's (`raigolmid/history.py`, each question read from
`questions.py`). This draws `menu` and draws it again on every `history.*` or `question.*`
event it follows. A daemon restart ends the event stream, so it reconnects and redraws from
the history: the history is the truth, the events only say when to look again.

It moves as every host surface does (`ui/edge.py`). Pointing at it opens it and closes the
others; a notice arriving opens it on the machine's account and closes nothing.
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")

from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from raigolmid.client import ApiClient, ApiError  # noqa: E402
from ui import theme  # noqa: E402
from ui.hostevents import follow  # noqa: E402
from ui import surfaces  # noqa: E402
from ui import copyable  # noqa: E402
from ui.edge import CLOSED, OPEN, EdgeSurface  # noqa: E402
from ui.layershell import LayerShellUnavailable, overlay, set_keyboard  # noqa: E402
from ui.notify_popup.model import ARRIVAL, HANDLE, MENU, MenuModel  # noqa: E402

logger = logging.getLogger(__name__)

NAMESPACE = "raigolmi-notify"
# The depth kept for the panel is the menu at its tallest (the user's look's `menu_max_height`) and
# this, for its padding and a status line. A panel shorter than that is a smaller margin,
# never a smaller surface (`ui/edge.py`).
MENU_PADDING = 120
TICK_MS = 500
REDRAW_ON = ("history.", "question.", "subscriber.dropped")

CSS = """
window { background-color: transparent; }
.handle { background-color: $accent_bg; border-radius: 0 0 8px 8px; }
.handle.lit { background-color: $accent; }
.panel { background-color: $surface; border: 1px solid $border; border-radius: 10px;
         padding: 10px 12px; }
.panel.menu { border-top-width: 0; padding-top: 0; }
.notice { background-color: $bg; border: 1px solid $bad; border-radius: 8px;
          padding: 10px 12px; }
.message { color: $text; }
.when { color: $dim; font-size: smaller; }
.settled { color: $muted; }
.choice { background-color: $raised; color: $text; }
.choice:hover { background-color: $accent; color: $bg; }
.status { color: $bad; }
"""

SETTLED_SAYS = {"answered": "answered", "lapsed": "lapsed", "withdrawn": "withdrawn",
                "judging": "with the preferences judge"}
WITHDRAWN_SAYS = {"answered_in_terminal": "you answered it in its tab",
                  "tab_gone": "its tab went"}


def when(ts: float) -> str:
    local = time.localtime(ts)
    today = time.localtime()
    same_day = local[:3] == today[:3]
    return time.strftime("%H:%M" if same_day else "%d %b %H:%M", local)


def outcome(item: dict) -> str:
    """How a question or permission stands."""
    if item["state"] == "pending":
        if item["kind"] == "permission":
            return f"waiting for you: answer it in {item['tab']}'s window"
        return (f"with the machine tab, which manages {item['tab']}" if item["managed"]
                else f"waiting for you: type in {item['tab']}")
    said = SETTLED_SAYS[item["state"]]
    if item["state"] == "withdrawn":
        reason = item["outcome"]
        said += f": {WITHDRAWN_SAYS.get(reason, f'its tab went ({reason})')}"
    elif item["outcome"]:
        said += f": {item['outcome']}"
    if item["by"] == "always":
        said += " — by an always"
    elif item["by"] == "preferences":
        said += " — from your preferences"
    elif item["by"] == "machine":
        said += " — by the machine tab"
    elif item["by"] == "overturn":
        said += f" — overturning {item['overturned']!r}"
    return said


def _whole_line(label: Gtk.Label, _x, _y, _keyboard, tooltip: Gtk.Tooltip, text: str) -> bool:
    if not label.get_layout().is_ellipsized():
        return False
    tooltip.set_text(text)
    return True


class Popup:
    def __init__(self, client) -> None:
        self.client = client
        self.model = MenuModel()
        self.window: Gtk.Window | None = None
        self.handle: Gtk.Box | None = None
        self.edge: EdgeSurface | None = None
        self.holder: Gtk.Box | None = None
        self.panel: Gtk.Widget | None = None
        self.entries: dict[str, Gtk.Entry] = {}
        self.drafts: dict[str, str] = {}
        self.failure = ""
        self._drawn: tuple | None = None
        self._built: tuple | None = None
        self._keyboard = False
        self._said_seen: str | None = None  # the newest entry said seen, so it is said once

    # --- frame ------------------------------------------------------------------------
    def build(self, app: Gtk.Application) -> None:
        """⚠ GTK swallows an exception raised in a signal handler and `app.run()` returns 0
        with nothing drawn, so the frame's refusal is recorded and made the exit status."""
        window = Gtk.ApplicationWindow(application=app)
        try:
            # The top edge alone, so it is centred along it; the keyboard is not taken here.
            overlay(window, namespace=NAMESPACE, anchors=("top",))
        except LayerShellUnavailable as exc:
            self.failure = str(exc)
            logger.error("%s", exc)
            app.quit()
            return
        self.window = window
        theme.apply(CSS)

        # Capture phase, so a click anywhere on it — a choice, the field — first takes the
        # keyboard and then does what it was aimed at.
        click = Gtk.GestureClick()
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", lambda *_: self._hold())
        window.add_controller(click)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        window.add_controller(keys)
        # ⚠ On the window, as the control's is: the window is what the compositor delivers
        # pointer events to. The redraw waits for idle: replacing the window's child inside
        # the handler pulls it from under the event GTK is still delivering (a
        # `gtk_widget_compute_point` critical).
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_: GLib.idle_add(self._then, self.model.enter))
        motion.connect("leave", lambda *_: GLib.idle_add(self._then, self.model.leave,
                                                         time.time()))
        window.add_controller(motion)

        # The selector's shape turned downwards: the tab at rest, and the panel sliding out of
        # the top edge over it, which goes while the panel is out. The panel sits at the
        # bottom of the room kept for it, against the tab.
        self.handle = Gtk.Box(halign=Gtk.Align.CENTER)
        self.handle.add_css_class("handle")
        look = theme.look()
        width, room_depth = look.menu_width, look.menu_max_height + MENU_PADDING
        self.handle.set_size_request(look.tab_length, look.tab_depth)
        self.holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.END,
                              vexpand=True)
        # The panel is built by the first `_draw`, before the window is presented, so its
        # first paint is paid at start rather than by the first opening.
        room = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        room.set_size_request(width, room_depth)
        room.append(self.holder)
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        frame.append(room)
        frame.append(self.handle)
        window.set_child(frame)
        window.set_default_size(width, room_depth + look.tab_depth)
        window.set_size_request(width, room_depth + look.tab_depth)
        self.edge = EdgeSurface(window, name=surfaces.HISTORY, edge="top", tab=self.handle,
                                panel=self.holder, room=room_depth, panel_depth=self._panel_depth)

        self._draw()
        window.present()
        # A frame clock exists once the window is presented, not before.
        window.get_frame_clock().connect("after-paint",
                                         lambda _clock: self.edge.pointer_region())
        self.edge.serve(self._verb)
        GLib.timeout_add(TICK_MS, self._tick)
        threading.Thread(target=follow, args=(self.client, REDRAW_ON, self._fetch),
                         name="events", daemon=True).start()

    # --- the daemon -------------------------------------------------------------------
    def _fetch(self) -> None:
        try:
            entries = self.client.call("menu")
        except (ApiError, OSError) as exc:
            GLib.idle_add(self._fail, f"could not read the history: {exc}", True)
            return
        GLib.idle_add(self._then, self.model.take, entries, time.time())

    def _call(self, method: str, **params) -> None:
        def run() -> None:
            try:
                self.client.call(method, **params)
            except (ApiError, OSError) as exc:
                GLib.idle_add(self._fail, f"{method} failed: {exc}", False)
                return
            # Sent: the keyboard goes back with the entry the user clicked into.
            GLib.idle_add(self._then, self.model.release)
            self._fetch()
        threading.Thread(target=run, name=method, daemon=True).start()

    def _mark_seen(self, through: str) -> None:
        """Not `_call`: being shown answers nothing, so the keyboard stays where it is."""
        def run() -> None:
            try:
                self.client.call("seen", through=through)
            except (ApiError, OSError) as exc:
                GLib.idle_add(self._unsay_seen, through)
                GLib.idle_add(self._fail, f"seen failed: {exc}", False)
        threading.Thread(target=run, name="seen", daemon=True).start()

    def _unsay_seen(self, through: str) -> bool:
        """Not heard, so said again on the next draw."""
        if self._said_seen == through:
            self._said_seen = None
        return GLib.SOURCE_REMOVE

    # --- the model, then the drawing ----------------------------------------------------
    def _then(self, change, *args, **kwargs) -> bool:
        change(*args, **kwargs)
        self._draw()
        return GLib.SOURCE_REMOVE

    def _tick(self) -> bool:
        self._then(self.model.tick, time.time())
        return GLib.SOURCE_CONTINUE

    def _hold(self) -> None:
        if self.model.shape != HANDLE and not self.model.holding:
            self._then(self.model.hold)

    def _draw(self) -> None:
        model = self.model
        if self.window is None:
            return
        if model.holding != self._keyboard:
            self._keyboard = model.holding
            set_keyboard(self.window, model.holding)
            logger.info("keyboard %s", "taken" if model.holding else "given back")
            first = next(iter(self.entries.values()), None)
            if model.holding and first is not None \
                    and not any(e.has_focus() for e in self.entries.values()):
                first.grab_focus()
        through = model.unseen_through()
        if through is not None and through != self._said_seen:
            self._said_seen = through
            self._mark_seen(through)
        # ⚠ The keyboard is not part of what is drawn: the click that takes it is still on
        # its way to a button, and rebuilding the panel under it would lose that click.
        # Folded, the panel waiting past the edge is the menu's, so pointing at the tab only
        # slides it: building every row on the way out is what held the opening back.
        shape = MENU if model.shape == HANDLE else model.shape
        content = (shape, model.failure,
                   tuple((e["id"], e["over"], e.get("item", {}).get("state"),
                          e.get("item", {}).get("by")) for e in model.shown(shape)))
        drawn = (model.shape, model.lit, content)
        if drawn == self._drawn:
            return
        focused = next((id for id, e in self.entries.items() if e.has_focus()), None)
        self._drawn = drawn

        self.handle.set_css_classes(["handle", "lit"] if model.lit else ["handle"])
        if model.shape == HANDLE:
            # Swapped once it is shut, so a notice sliding away keeps its own rows.
            self.edge.move(False, lambda _: self._wait(content))
        else:
            self._build(content)
            # Pointed at, it is the user's gesture and closes the others; a notice is the
            # machine's.
            self.edge.move(True, gesture=model.shape == MENU)
        if focused in self.entries:
            self.entries[focused].grab_focus()

    def _wait(self, content: tuple) -> None:
        if self.model.shape == HANDLE and self._drawn[2] == content:
            self._build(content)

    def _build(self, content: tuple) -> None:
        """The panel for `content`, unless it is the one already in place."""
        if content == self._built:
            return
        self.drafts.update({id: e.get_text() for id, e in self.entries.items()})
        self.entries = {}
        if self.panel is not None:
            self.holder.remove(self.panel)
        self.panel = self._panel(content[0])
        self.holder.append(self.panel)
        self._built = content
        self.edge.refit()

    def _panel_depth(self) -> int:
        if self.panel is None:
            return 0
        return self.panel.measure(Gtk.Orientation.VERTICAL, theme.look().menu_width)[1]

    def _verb(self, verb: str, settle) -> None:
        """Its socket (`ui/surfaces.py`): moved through the model, so the menu's shape and
        the surface never disagree."""
        if verb == "state":
            settle(OPEN if self.edge.slide.opened else CLOSED)
            return
        opened = not self.edge.slide.opened if verb == "toggle" else verb == "open"
        self._then(self.model.open if opened else self.model.escape)
        self.edge.move(self.edge.slide.opened, settle)

    def _panel(self, shape: str) -> Gtk.Widget:
        model = self.model
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        panel.add_css_class("panel")
        rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for entry in model.shown(shape):
            rows.append(self._row(entry))
        if shape == ARRIVAL:
            panel.append(rows)
        else:
            if not model.entries:
                rows.append(self._label("Nothing yet.", "settled"))
            # GTK draws the scroll's edge light at the scroller's own top, so the scroller
            # starts at the window's top and the panel's top padding is inside it.
            panel.add_css_class("menu")
            rows.set_margin_top(10)
            panel.append(Gtk.ScrolledWindow(
                hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                max_content_height=theme.look().menu_max_height, child=rows))
        status = copyable.label(model.failure, wrap=True, xalign=0,
                                visible=bool(model.failure))
        status.add_css_class("status")
        panel.append(status)
        return panel

    def _row(self, entry: dict) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        item = entry.get("item")
        head = f"{when(entry['at'])}" + (f"  {entry['tab']}" if entry["tab"] else "")
        if item is not None:
            head += f"  {item['kind']}  —  {outcome(item)}"
            text = item["message"]
        else:
            text = entry["text"]
            if entry["notice"] and entry["over"] is not None:
                head += "  —  over"
        row.append(self._label(head, "when"))
        standing = entry["notice"] and entry["over"] is None
        if standing:
            row.add_css_class("notice")
        pending = item is not None and item["state"] == "pending"
        message = self._label(text, "message" if standing or pending else "settled",
                              wrap=standing or pending)
        if not (standing or pending):
            message.set_ellipsize(Pango.EllipsizeMode.END)
            # The whole line, only when the ellipsis hides some of it.
            message.set_has_tooltip(True)
            message.connect("query-tooltip", _whole_line, text)
        row.append(message)
        if item is not None and item["by"] in ("preferences", "machine"):
            row.append(self._overturn(item))
        return row

    def _choices(self, item: dict, choices) -> Gtk.Widget:
        row = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=6,
                          row_spacing=6, max_children_per_line=4)
        for choice in choices:
            button = copyable.button(choice, lambda c=choice:
                                     self._call("overturn", id=item["id"], text=c))
            button.add_css_class("choice")
            row.append(button)
        return row

    def _overturn(self, item: dict) -> Gtk.Widget:
        """The preference it came from, and the user's answer in its place: another choice or
        words."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(self._label(f"“{item['quote']}”", "settled", wrap=True))
        others = [c for c in item["choices"] if c != item["outcome"]]
        if others:
            box.append(self._choices(item, others))
        entry = Gtk.Entry(placeholder_text="Overturn in words, then Enter",
                          text=self.drafts.get(item["id"], ""))
        entry.connect("activate", lambda e: e.get_text().strip() and self._call(
            "overturn", id=item["id"], text=e.get_text()))
        self.entries[item["id"]] = entry
        box.append(entry)
        return box

    @staticmethod
    def _label(text: str, css: str, wrap: bool = False, hexpand: bool = False) -> Gtk.Label:
        label = copyable.label(text, wrap=wrap, xalign=0, hexpand=hexpand)
        label.add_css_class(css)
        return label

    def _fail(self, text: str, reading: bool) -> bool:
        logger.error("%s", text)
        return self._then(self.model.fail, text, time.time(), reading=reading)

    # --- input ------------------------------------------------------------------------
    def _on_key(self, _controller, keyval: int, _code: int, _state) -> bool:
        if keyval == Gdk.KEY_Escape:
            # Keys back and folded: Escape never overturns for the user.
            self._then(self.model.escape)
            return True
        return False


def main(argv: list[str] | None = None) -> int:
    from raigolmid.paths import Paths

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="raigolmi-notify", description=__doc__)
    parser.parse_args(argv)
    theme.load()

    popup = Popup(ApiClient(Paths.from_env().api_socket))
    # ⚠ Not unique: the daemon keeps one by container name, and a unique id on the session
    # bus in the shared runtime dir makes a second instance hand over to the first and exit 0
    # having drawn nothing.
    app = Gtk.Application(application_id="os.raigolmi.notify", flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.connect("activate", popup.build)
    status = app.run([])
    return 1 if popup.failure else status


if __name__ == "__main__":
    sys.exit(main())
