"""The user's keyboard and display (`raigolmid/keyboard.py`) on a compositor that refuses what
sway refuses: the settings they save are what the host is told, and a layout no keymap has fails
with the compositor's own reason rather than reading as applied."""
from __future__ import annotations

import os

import pytest

from raigolmid import keyboard, settings
from raigolmid.keyboard import KeyboardError
from tests import settingsdoc
from tests.fakesway import FakeSway


@pytest.fixture
def sway(tmp_path):
    fake = FakeSway(tmp_path / f"sway-ipc.0.{os.getpid()}.sock")
    yield fake
    fake.close()


def test_his_keyboard_and_scale_are_what_the_host_is_told(sway, tmp_path):
    path = settingsdoc.write(tmp_path / "settings.toml",
                             keyboard={"layout": "us,de", "variant": "intl", "repeat_rate": 40},
                             display={"scale": 1.5})
    keyboard.apply_host(settings.load(path), str(sway.path))
    assert sway.settings == {
        "input type:keyboard xkb_layout": '"us,de"',
        "input type:keyboard xkb_variant": '"intl"',
        "input type:keyboard repeat_rate": "40",
        "input type:keyboard repeat_delay": "600",
        "output * scale": "1.5",
    }


def test_a_layout_no_keymap_has_is_refused_with_the_compositors_reason(sway, tmp_path):
    path = settingsdoc.write(tmp_path / "settings.toml", keyboard={"layout": "qq"})
    with pytest.raises(KeyboardError, match=r"xkb_layout \"qq\".*Failed to compile keymap"):
        keyboard.apply_host(settings.load(path), str(sway.path))
    assert sway.settings == {}, "nothing after the refusal is sent"
