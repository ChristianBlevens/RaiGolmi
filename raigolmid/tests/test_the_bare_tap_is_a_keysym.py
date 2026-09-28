"""The bare Super tap is bound to a keysym, never to a modifier name.

`bindsym --release Mod4` parses, passes `sway --validate` and reloads without a word, and then
never fires — a modifier name in the key position is a binding with no key in it. Nothing in
sway's answers distinguishes it from a working binding, so a test is the only place the
distinction can live.

The two files have to agree for a second reason: the generated include `unbindsym`s what
`host/sway/config` bound, and unbinding a key nothing has bound is itself a config error.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from raigolmid import settings   # noqa: E402
from raigolmid.hostkeys import STATIC_DEFAULTS   # noqa: E402

HOST_CONFIG = ROOT / "host" / "sway" / "config"

# A modifier name is only ever the thing held down, never the thing pressed.
MODIFIER_NAMES = {"Mod1", "Mod2", "Mod3", "Mod4", "Mod5", "Shift", "Control", "Alt", "Super"}


def _key_of(spec: str) -> str:
    """The last `+`-separated token of the last word: the key, as opposed to the flags before
    it and the modifiers before that."""
    return spec.split()[-1].split("+")[-1]


def test_the_selector_tap_is_not_bound_to_a_modifier_name():
    spec = settings.load(settings.SHIPPED).keys["selector"]
    assert "--release" in spec, spec
    assert _key_of(spec) not in MODIFIER_NAMES, (
        f"{spec!r} binds a modifier name as the key; it will never fire")


def test_the_static_defaults_are_exactly_what_the_host_config_binds():
    text = HOST_CONFIG.read_text(encoding="utf-8")
    # `set $mod Mod4` — expanded the way sway expands it, so the comparison is against what
    # is actually bound rather than against how it is spelled.
    mod = re.search(r"^set \$mod (\S+)", text, re.M)
    assert mod, "host/sway/config no longer sets $mod"
    bound = [re.sub(r"^bindsym\s+", "", line).rsplit(" exec ", 1)[0]
             .replace("$mod", mod.group(1))
             for line in text.splitlines() if line.startswith("bindsym ")]
    assert sorted(bound) == sorted(STATIC_DEFAULTS), (
        "the include unbinds STATIC_DEFAULTS before rebinding, so an unbind with no matching "
        f"static bind is a config error. host/sway/config binds {bound}")
