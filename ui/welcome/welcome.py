"""The bare host's welcome: what the screen is before there is a desktop on it.

A fresh machine has nothing on its screen but the three tabs at its edges, and nothing there
says they open. This draws, on the background layer under every window, a welcome in the middle
and a few words beside each tab saying what it holds. A face is a window, so the moment one is on
the screen it covers this; with none, this is what the user sees.
"""
from __future__ import annotations

import argparse
import logging

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gio, Gtk  # noqa: E402

from ui import copyable, theme  # noqa: E402
from ui.layershell import LayerShellUnavailable, overlay  # noqa: E402

logger = logging.getLogger(__name__)

NAMESPACE = "raigolmi-welcome"
# How far a tab's words sit from the tab itself.
GAP = 16

CSS = """
window { background-color: $bg; }
.title { color: $accent; font-size: 40px; font-weight: bold; }
.lead { color: $text; font-size: 17px; }
.hint { color: $muted; font-size: 15px; }
.edge { color: $muted; font-size: 14px; }
.edge-name { color: $text; font-size: 14px; font-weight: bold; }
"""


def _label(text: str, css: str, **props) -> Gtk.Label:
    return copyable.label(text, css, wrap=True, **props)


def _edge(name: str, what: str, **placed) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, **placed)
    box.append(_label(name, "edge-name"))
    box.append(_label(what, "edge", justify=Gtk.Justification.CENTER))
    return box


class Welcome:
    def __init__(self) -> None:
        self.failure: str | None = None

    def build(self, app: Gtk.Application) -> None:
        """⚠ GTK swallows an exception raised in a signal handler and `app.run()` returns 0
        with nothing drawn, so the frame's refusal is recorded and made the exit status."""
        window = Gtk.ApplicationWindow(application=app)
        try:
            overlay(window, namespace=NAMESPACE, anchors=("left", "right", "top", "bottom"),
                    background=True)
        except LayerShellUnavailable as exc:
            self.failure = str(exc)
            logger.error("%s", exc)
            app.quit()
            return
        theme.apply(CSS)
        away = theme.look().tab_depth + GAP

        middle = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                         halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        middle.append(_label("Welcome to RaiGolmi", "title"))
        middle.append(_label("There is no desktop here yet. You make one, and anything else, "
                             "by asking for it.", "lead", justify=Gtk.Justification.CENTER))
        middle.append(_label("Put your mouse on a tab at the edge of the screen to open it, and "
                             "move away to close it. Start with the one at the bottom.", "hint",
                             justify=Gtk.Justification.CENTER, max_width_chars=60))

        screen = Gtk.Overlay(child=middle)
        screen.add_overlay(_edge("Selector", "your desktops, projects\nand the catalog",
                                 halign=Gtk.Align.START, valign=Gtk.Align.CENTER,
                                 margin_start=away))
        screen.add_overlay(_edge("History", "everything the agents\nand the machine have done",
                                 halign=Gtk.Align.CENTER, valign=Gtk.Align.START,
                                 margin_top=away))
        screen.add_overlay(_edge("AI terminal", "talk to the machine tab here: ask it for\n"
                                 "a desktop, or to work on a project",
                                 halign=Gtk.Align.CENTER, valign=Gtk.Align.END,
                                 margin_bottom=away))
        window.set_child(screen)
        window.present()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="raigolmi-welcome", description=__doc__)
    parser.parse_args(argv)
    theme.load()
    welcome = Welcome()
    # ⚠ Not unique, as every host surface: a unique id on the session bus in the shared runtime
    # dir makes a second instance hand over to the first and exit 0 having drawn nothing.
    app = Gtk.Application(application_id="os.raigolmi.welcome",
                          flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.connect("activate", welcome.build)
    status = app.run([])
    return 1 if welcome.failure else status


if __name__ == "__main__":
    raise SystemExit(main())
