"""The host surfaces' one look, so the selector, the control, the menu, the catalog and the AI
terminal read as one system over the desktop.

It is the user's `[look]` in `settings.toml`. A surface runs in a container that has only the
runtime dir, so the daemon writes the look there as JSON (`look.write`, `Paths.look`) before it
draws any surface, and again on every save, restarting them; each surface calls `load` in its
`main` before it builds, and reads `look()` where it draws. foot has no way to import this, so
the AI terminal is given the palette's colours on its command line
(`hostsurfaces.ai_terminal_command`).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path
from string import Template


@dataclass(frozen=True)
class Look:
    bg: str             # the desktop, and every surface's own background
    surface: str        # a panel or a button at rest
    raised: str         # a button under the pointer
    border: str
    text: str
    muted: str
    dim: str            # unavailable, and hints
    accent: str         # titles, and wherever the keyboard is
    accent_bg: str
    ok: str             # selected
    warn: str
    bad: str
    tab_length: int     # the tab each edge surface is at rest, the same on every edge
    tab_depth: int
    reveal_ms: int      # how long a surface takes to slide out from its tab
    drawer_share: float
    drawer_max: int
    terminal_height_percent: int
    menu_width: int
    menu_max_height: int
    card_width: int
    catalog_width_share: float
    catalog_height_share: float
    catalog_backdrop: float     # the black drawn over the screen behind the catalog

    @property
    def palette(self) -> dict[str, str]:
        return {f.name: getattr(self, f.name) for f in fields(self) if f.type == "str"}



# A wait that is still working turns; a still one is what a hung machine looks like.
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

_look: Look | None = None


def load(path: Path | None = None) -> Look:
    """The look the daemon wrote. A surface started by the daemon always has one, so a
    missing file is a surface started some other way, and it fails naming the file."""
    global _look
    if path is None:
        from raigolmid.paths import Paths
        path = Paths.from_env().look
    if not path.is_file():
        raise FileNotFoundError(f"{path} is not there: the daemon writes the look before it "
                                "starts a surface (raigolmid/look.py)")
    _look = Look(**json.loads(path.read_text(encoding="utf-8")))
    return _look


def look() -> Look:
    if _look is None:
        raise RuntimeError("theme.load() has not run: a surface loads its look in its main")
    return _look


def css(template: str) -> bytes:
    """A stylesheet with `$name` standing for each field of the look. An unknown name raises."""
    current = look()
    return Template(template).substitute(
        {f.name: getattr(current, f.name) for f in fields(current)}).encode()


def apply(stylesheet: str) -> None:
    """Install the `css` template `stylesheet` over GTK's theme, and ask for the theme's dark
    variant so whatever the stylesheet does not name — a focus ring, a tooltip — is not drawn
    light."""
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk, Gtk

    Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
    provider = Gtk.CssProvider()
    # A selection in any surface's text reads as one, on the accent rather than GTK's grey.
    provider.load_from_data(css(stylesheet + """
selection { background-color: $accent_bg; color: $text; }
"""))
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
