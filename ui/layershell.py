"""The overlay frame the host's two surfaces are drawn on.

The host control and the native selector are both layer-shell surfaces over whatever
face is fullscreen, and both are GTK4 through PyGObject. The frame is here rather than in
each of them because it is one fact about the compositor, and two copies of it are how they
come to disagree about what an overlay is.

⚠ **`init_for_window` fails silently.** gtk4-layer-shell has to be loaded before
libwayland-client, which a linker cannot arrange when GTK is reached through PyGObject at
runtime — the images set `LD_PRELOAD=/usr/lib64/libgtk4-layer-shell.so.0` for that reason.
Without it `init_for_window` does nothing, an ordinary window appears, and every later call
warns. `overlay()` therefore asks the library whether the window actually became a layer
surface and raises when it did not, because a control that is drawn as an ordinary window
under a fullscreen face is invisible, and invisible is the one failure the user cannot report.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")

from gi.repository import Gtk, Gtk4LayerShell as LayerShell  # noqa: E402

EDGES = {
    "top": LayerShell.Edge.TOP,
    "bottom": LayerShell.Edge.BOTTOM,
    "left": LayerShell.Edge.LEFT,
    "right": LayerShell.Edge.RIGHT,
}


SONAME = "libgtk4-layer-shell.so.0"


class LayerShellUnavailable(RuntimeError):
    pass


def _why_unsupported() -> str:
    """`is_supported()` is False for two causes that look identical from outside — the
    compositor has no zwlr_layer_shell_v1, or the library was loaded too late to see the
    registry at all — so the two are told apart rather than one of them guessed at.

    Loading late is decided by LD_PRELOAD, and the way it goes wrong silently is naming a
    path that is not there, so the paths are checked rather than the variable's presence.
    """
    preloaded = [Path(entry) for entry in
                 re.split(r"[:\s]+", os.environ.get("LD_PRELOAD", "")) if entry]
    naming_it = [p for p in preloaded if p.name.startswith("libgtk4-layer-shell")]

    if not naming_it:
        return (
            f"gtk4-layer-shell was loaded after libwayland-client, so its init sees no "
            f"registry and every layer-shell call is a no-op. LD_PRELOAD does not name it "
            f"(LD_PRELOAD={os.environ.get('LD_PRELOAD', '') or 'unset'}); it has to be "
            f"LD_PRELOAD=/usr/lib64/{SONAME}."
        )
    missing = [p for p in naming_it if not p.exists()]
    if missing:
        return (
            f"LD_PRELOAD names {', '.join(str(p) for p in missing)}, which does not exist — "
            f"the loader ignores a missing preload silently, so this reads exactly like the "
            f"library not being installed. Use the SONAME {SONAME}: only the -devel package "
            f"ships the unversioned .so symlink."
        )
    return (
        f"{', '.join(str(p) for p in naming_it)} was preloaded and the compositor still "
        f"offers no zwlr_layer_shell_v1, so this is the compositor rather than the load "
        f"order. The host compositor is sway and does offer it — check WAYLAND_DISPLAY "
        f"points at the host's socket and not a face's."
    )


def overlay(window: Gtk.Window, *, namespace: str,
            anchors: tuple[str, ...] = (), margin: int = 0,
            keyboard: bool = False, reserve: bool = False) -> None:
    """Make `window` a layer-shell surface on the overlay layer.

    `anchors` are edge names; an empty tuple centres the surface. `keyboard` asks the compositor for focus — the selector needs it to be navigable,
    the control does not and takes clicks only.

    `reserve` claims the surface's own height as an exclusive zone, so every other overlay
    surface is laid out clear of it. Two overlay surfaces otherwise stack by map order.
    ⚠ wlroots honours an exclusive zone only for a surface anchored to one edge, or to one
    edge and both its neighbours — a corner anchor reserves nothing.
    """
    if not LayerShell.is_supported():
        raise LayerShellUnavailable(_why_unsupported())

    LayerShell.init_for_window(window)
    if not LayerShell.is_layer_window(window):
        raise LayerShellUnavailable(
            "init_for_window did not make this a layer surface. gtk4-layer-shell was loaded "
            "after libwayland-client, which makes it a no-op: set "
            "LD_PRELOAD=/usr/lib64/libgtk4-layer-shell.so.0 — the SONAME, since only the "
            "-devel package ships the unversioned symlink and a missing LD_PRELOAD file is "
            "itself silent."
        )

    LayerShell.set_namespace(window, namespace)
    LayerShell.set_layer(window, LayerShell.Layer.OVERLAY)
    for name in anchors:
        LayerShell.set_anchor(window, EDGES[name], True)
        LayerShell.set_margin(window, EDGES[name], margin)
    if reserve:
        LayerShell.auto_exclusive_zone_enable(window)
    set_keyboard(window, keyboard)


def set_margin(window: Gtk.Window, edge: str, pixels: int) -> None:
    """Move an anchored surface off its edge, while it is up.

    The AI terminal's tab rides on top of the terminal when it is open, so that the thing that
    opened it is also the thing that closes it. A surface that stayed on the edge would be
    under the window it just opened, and there would be no way back."""
    LayerShell.set_margin(window, EDGES[edge], pixels)


def set_keyboard(window: Gtk.Window, holds: bool) -> None:
    """Take the compositor's keyboard, or give it back, while the surface is up.

    A surface that is on screen at all times cannot hold the keyboard at all times: sway gives
    the keys to the topmost overlay surface that asks for them, so a resident selector that
    never let go would leave the face with no keyboard at all. It asks while it is open and
    lets go as it closes."""
    LayerShell.set_keyboard_mode(
        window,
        LayerShell.KeyboardMode.EXCLUSIVE if holds else LayerShell.KeyboardMode.NONE,
    )
