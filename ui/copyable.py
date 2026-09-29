"""Text on the host's panels is copied by selecting it : a drag over a label selects, and the selection lands on the clipboard as well as the
primary, the same as a drag in the AI terminal.

A selectable label sets only the primary, at every step of the drag, and GTK 4's label says
nothing else about its selection: it has no selection property to watch (gtklabel.c,
gtk-4-18). So the display's primary is watched instead, and once a change this process made
has been still for `SETTLE_MS` its text is put on the clipboard — once, rather than at each
step for the Windows bridge to carry. Anything this surface selects is copied that way,
the catalog's editor included.

The label is not focusable: a selectable label otherwise takes focus on the first click, and
with it the keys a surface reads from its window (the selector's arrows). A drag selects
without focus.

**Clickable text is copyable too.** A press on a selectable label is the label's: it claims
the press (gtklabel.c, 4.22), so a `Gtk.Button` or `Gtk.CheckButton` around it never clicks
when the press lands on its text. So `_Released` watches the widget in the capture phase, where
the label cannot take the press away, and a press released over the widget is its click
whatever the label did with it; GTK's own click, from a press beside the text, is the same
click and is not run twice. A drag that ends off the widget only selects. A click clears what
it selected. A `Gtk.Expander` whose title
is a selectable label never opens at all, so a heading that opens a section is a `button`
over a `Gtk.Revealer` (`section`).
"""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")

from typing import Callable  # noqa: E402

from gi.repository import Gdk, GLib, GObject, Graphene, Gtk  # noqa: E402

SETTLE_MS = 250
_ARROW = {True: "pan-down-symbolic", False: "pan-end-symbolic"}

_watched: set[int] = set()


def _plain_cursor(label: Gtk.Label, *_args) -> None:
    label.set_cursor(None)


def copyable(label: Gtk.Label) -> Gtk.Label:
    label.set_selectable(True)
    label.set_focusable(False)
    # No I-beam: all text is selectable, so it says nothing. The label
    # sets it on every state change, and this handler runs after the label's own.
    _plain_cursor(label)
    label.connect("state-flags-changed", _plain_cursor)
    _watch(label.get_display())
    return label


def label(text: str = "", css: str | None = None, **props) -> Gtk.Label:
    made = copyable(Gtk.Label(label=text, **props))
    if css:
        made.add_css_class(css)
    return made


def set_text(made: Gtk.Label, text: str) -> None:
    """Only when it differs: setting a label's text ends the selection in it, and a surface
    that redraws on a timer would end every selection before it could be copied."""
    if made.get_text() != text:
        made.set_text(text)


def button(text: str | Gtk.Label, clicked: Callable[[], None], child: Gtk.Widget | None = None,
           **props) -> Gtk.Button:
    """`child` when the button holds more than its label; the label is still what a drag over
    it selects and what decides whether the press was a click."""
    made = text if isinstance(text, Gtk.Label) else label(text)
    if not made.get_selectable():
        copyable(made)
    widget = Gtk.Button(child=child or made, **props)

    def activate() -> None:
        made.select_region(0, 0)
        clicked()
    released = _Released(widget, activate)
    widget.connect("clicked", lambda _button: released.clicked())
    return widget


class _Released:
    """A primary press on `widget` released over it runs `activate` once, whether GTK's own
    click came (a press beside the label, a key) or the label took the press (one on its
    text). Read in the capture phase and never consumed, so the label still selects."""

    def __init__(self, widget: Gtk.Widget, activate: Callable[[], None]) -> None:
        self.widget = widget
        self.activate = activate
        self.pending = False
        self.clicked_now = False
        watch = Gtk.EventControllerLegacy()
        watch.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        watch.connect("event", self._event)
        widget.add_controller(watch)

    def clicked(self) -> None:
        """GTK's own click: run now, and the release that brought it runs nothing more."""
        self.clicked_now = True
        self.activate()

    def _event(self, watch: Gtk.EventControllerLegacy, _event) -> bool:
        # PyGObject hands this signal's GdkEvent over as None; the controller's own is whole.
        event = watch.get_current_event()
        kind = event.get_event_type()
        if kind == Gdk.EventType.BUTTON_PRESS:
            self.pending = event.get_button() == Gdk.BUTTON_PRIMARY
        elif kind == Gdk.EventType.BUTTON_RELEASE and self.pending:
            self.pending = False
            if self._over(event):
                self.clicked_now = False
                # After the release has gone through the widget's own gestures, which may
                # click it themselves.
                GLib.idle_add(self._settle)
        return Gdk.EVENT_PROPAGATE

    def _settle(self) -> bool:
        if not self.clicked_now:
            self.activate()
        self.clicked_now = False
        return GLib.SOURCE_REMOVE

    def _over(self, event: Gdk.Event) -> bool:
        """Whether the release is over the widget, from its position on the surface."""
        native = self.widget.get_native()
        found, x, y = event.get_position()
        if not found:
            raise RuntimeError(f"a release on {self.widget} carries no position")
        offset_x, offset_y = native.get_surface_transform()
        found, point = native.compute_point(
            self.widget, Graphene.Point().init(x - offset_x, y - offset_y))
        if not found:
            raise RuntimeError(f"{self.widget} is not inside its own native")
        return self.widget.contains(point.x, point.y)

def check(text: str, toggled: Callable[[bool], None],
          **props) -> tuple[Gtk.CheckButton, int]:
    """The box and its handler, which a caller blocks when it sets the box from elsewhere."""
    made = label(text)
    widget = Gtk.CheckButton(child=made, **props)

    def activate() -> None:
        made.select_region(0, 0)
        toggled(widget.get_active())

    def flip() -> None:
        with widget.handler_block(handler):
            widget.set_active(not widget.get_active())
        activate()
    released = _Released(widget, flip)
    handler = widget.connect("toggled", lambda _box: (
        setattr(released, "clicked_now", True), activate()))
    return widget, handler


def section(title: str, child: Gtk.Widget, expanded: bool,
            toggled: Callable[[bool], None]) -> tuple[Gtk.Box, Gtk.Label]:
    """A heading that opens and closes `child`: an arrow and a copyable title, one button. The
    title label is returned so the caller can say a count in it."""
    # GTK's own expander arrows, built into GTK: the surfaces' font has no arrow glyphs.
    arrow = Gtk.Image.new_from_icon_name(_ARROW[expanded])
    title_label = label(title)
    head = Gtk.Box(spacing=6)
    head.append(arrow)
    head.append(title_label)
    revealer = Gtk.Revealer(child=child, reveal_child=expanded,
                            transition_type=Gtk.RevealerTransitionType.NONE)

    def flip() -> None:
        now = not revealer.get_reveal_child()
        revealer.set_reveal_child(now)
        arrow.set_from_icon_name(_ARROW[now])
        toggled(now)
    heading = button(title_label, flip, child=head, halign=Gtk.Align.START)
    heading.add_css_class("heading")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    box.append(heading)
    box.append(revealer)
    return box, title_label


def _watch(display: Gdk.Display) -> None:
    if hash(display) in _watched:
        return
    _watched.add(hash(display))
    primary, clipboard = display.get_primary_clipboard(), display.get_clipboard()
    pending: list[int] = []

    def copied(_primary, result) -> None:
        text = primary.read_text_finish(result)
        if text:
            clipboard.set(text)

    def settled() -> bool:
        pending.clear()
        # A selection ended before it settled (a click clears what it selected) leaves the
        # primary holding no text, and there is nothing to copy.
        if primary.is_local() and primary.get_formats().contain_gtype(GObject.TYPE_STRING):
            primary.read_text_async(None, copied)
        return GLib.SOURCE_REMOVE

    def changed(_primary) -> None:
        if not primary.is_local():
            return
        if pending:
            GLib.source_remove(pending.pop())
        pending.append(GLib.timeout_add(SETTLE_MS, settled))

    primary.connect("changed", changed)
