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

**Clickable text is copyable too.** A selectable label inside a `Gtk.Button` still lets the
button's click through, and a drag over it both selects and clicks; a plain click leaves the
label's selection as it was. So `button` runs its action unless the press made a new selection
(read at the press by a capture gesture on the button), and `check` undoes a toggle that came with a selection. A `Gtk.Expander` whose title
is a selectable label never opens at all, so a heading that opens a section is a `button`
over a `Gtk.Revealer` (`section`).
"""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from typing import Callable  # noqa: E402

from gi.repository import Gdk, GLib, Gtk  # noqa: E402

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


def selected(made: Gtk.Label) -> bool:
    found, start, end = made.get_selection_bounds()
    return found and end > start


def button(text: str | Gtk.Label, clicked: Callable[[], None], child: Gtk.Widget | None = None,
           **props) -> Gtk.Button:
    """`child` when the button holds more than its label; the label is still what a drag over
    it selects and what decides whether the press was a click."""
    made = text if isinstance(text, Gtk.Label) else label(text)
    if not made.get_selectable():
        copyable(made)
    widget = Gtk.Button(child=child or made, **props)
    at_press: list[tuple] = []

    def pressed(*_args) -> None:
        at_press[:] = [_bounds(made)]

    def on_clicked(_button) -> None:
        # A drag made a selection; a click inside an older one leaves it as it was, and is
        # still a click.
        if selected(made) and at_press and _bounds(made) != at_press[0]:
            return
        made.select_region(0, 0)
        clicked()
    press = Gtk.GestureClick()
    press.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    press.connect("pressed", pressed)
    widget.add_controller(press)
    widget.connect("clicked", on_clicked)
    return widget


def _bounds(made: Gtk.Label) -> tuple:
    found, start, end = made.get_selection_bounds()
    return (start, end) if found and end > start else ()


def check(text: str, toggled: Callable[[bool], None],
          **props) -> tuple[Gtk.CheckButton, int]:
    """The box and its handler, which a caller blocks when it sets the box from elsewhere."""
    made = label(text)
    widget = Gtk.CheckButton(child=made, **props)

    def on_toggled(box: Gtk.CheckButton) -> None:
        if selected(made):
            with box.handler_block(handler):
                box.set_active(not box.get_active())
            return
        toggled(box.get_active())
    handler = widget.connect("toggled", on_toggled)
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
        if primary.is_local():
            primary.read_text_async(None, copied)
        return GLib.SOURCE_REMOVE

    def changed(_primary) -> None:
        if not primary.is_local():
            return
        if pending:
            GLib.source_remove(pending.pop())
        pending.append(GLib.timeout_add(SETTLE_MS, settled))

    primary.connect("changed", changed)
