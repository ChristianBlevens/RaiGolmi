"""The user's keyboard and display (`[keyboard]`, `[display]` in `settings.toml`) on every
compositor that turns their keys into text.

The host's sway takes the layout, the repeat and the output scale. A face's sway is nested and
compiles its own keymap from its own config, taking nothing of the host's, so the keyboard
half is sent to it too: at its start (`Faces.start`) and on every save (`daemon._watch_settings`).
The scale is the host's alone, because the nested output is a window the host already sizes
in logical pixels.

Sway checks a layout by compiling it, so a value no keymap has is refused by the compositor
with xkb's own reason, and that refusal is the error raised here.
"""
from __future__ import annotations

from pathlib import Path

from . import settings as settings_
from .settings import Settings
from ui import hostipc
from ui.hostipc import HostIpcError

KEYBOARD = "input type:keyboard"


class KeyboardError(Exception):
    pass


def input_commands(s: Settings) -> list[str]:
    return [f'{KEYBOARD} xkb_layout "{s.layout}"',
            f'{KEYBOARD} xkb_variant "{s.variant}"',
            f"{KEYBOARD} repeat_rate {s.repeat_rate}",
            f"{KEYBOARD} repeat_delay {s.repeat_delay}"]


def host_commands(s: Settings) -> list[str]:
    return input_commands(s) + [f"output * scale {s.scale:g}"]


def apply_host(path: Path, swaysock: str | None = None) -> Settings:
    s = settings_.load(path)
    for line in host_commands(s):
        try:
            hostipc.run_command(line, swaysock=swaysock)
        except HostIpcError as exc:     # it names the command and sway's reason
            raise KeyboardError(f"{exc} (from settings.toml)") from exc
    return s
