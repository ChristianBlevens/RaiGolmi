"""The reserved host keys.

The load-bearing assertion here is the `unbindsym` one, and it is not a style rule: binding
a key sway already has bound is a config error, and a config error puts a swaynag panel over
the user's screen.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from raigolmid import hostkeys, settings
from raigolmid.settings import SettingsError
from raigolmid.events import EventLog
from raigolmid.hostkeys import (Binding, DEFAULT_COMMANDS, HostKeyError,
                               HostKeys, STATIC_DEFAULTS, parse_spec, render)
from raigolmid.paths import Paths

from tests import settingsdoc
from tests.fakesway import FakeSway, nag


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("RAIGOLMID_HOST_KEYS_INCLUDE", str(tmp_path / "host-keys.conf"))
    p = Paths(state=tmp_path / "state", data=tmp_path / "data",
              config=tmp_path / "config" / "raigolmid", runtime=tmp_path / "run")
    p.state.mkdir(parents=True, exist_ok=True)
    # What the daemon's start writes before anything reads a setting.
    settings.install(p.settings)
    return p


def _default_bindings() -> list[Binding]:
    keys = settings.load(settings.SHIPPED).keys
    return [Binding(a, keys[a], DEFAULT_COMMANDS[a]) for a in sorted(keys)]


# --- the rule the screen enforces ---------------------------------------------------------

def test_every_static_default_is_unbound_before_anything_is_bound():
    out = render(_default_bindings(), Path("/etc/host-keys.toml"))
    lines = [l for l in out.splitlines() if l and not l.startswith("#")]
    for spec in STATIC_DEFAULTS:
        assert f"unbindsym {spec}" in lines, f"{spec} is bound statically and not unbound"
    first_bind = next(i for i, l in enumerate(lines) if l.startswith("bindsym"))
    assert all(not l.startswith("bindsym") for l in lines[:first_bind])
    assert all(l.startswith("unbindsym") for l in lines[:first_bind])


def test_the_unbind_set_is_the_static_defaults_and_never_grows():
    """A reload re-reads the whole config, so nothing a previous generation of this file
    bound is bound when sway parses it — and `unbindsym` on an unbound key is a config
    error. The rule is constant rather than stateful."""
    for bindings in ([Binding("selector", "Mod4+space", "cmd")],
                     [Binding("selector", "Mod4+F1", "cmd")],
                     _default_bindings()):
        out = render(bindings, Path("/x.toml"))
        unbinds = {l for l in out.splitlines() if l.startswith("unbindsym")}
        assert unbinds == {f"unbindsym {s}" for s in STATIC_DEFAULTS}


# --- what a key spec may be ---------------------------------------------------------------

@pytest.mark.parametrize("spec, why", [
    ("", "empty"),
    ("--nonsense Mod4", "not a sway flag"),
    ("Mod4+", "a dangling +"),
    ("Mod4 grave\nbindsym Mod4+x exec evil", "a newline smuggling a second directive"),
    ("Mod4+$(id)", "shell-looking punctuation"),
])
def test_a_malformed_spec_is_refused_here_rather_than_at_the_reload(spec, why):
    """A bad binding does not fail where it is written — it fails as a panel over the
    user's screen, after the daemon has already reported success."""
    with pytest.raises(HostKeyError):
        parse_spec(spec)


def test_a_spec_is_rebuilt_from_validated_tokens_rather_than_passed_through():
    """The guard that actually protects the config file.

    Rejecting an obvious newline is the readable error; *rebuilding* the spec from the
    tokens that passed validation is what makes it impossible for anything unvalidated —
    trailing whitespace, a stray newline in a position that would otherwise parse — to
    reach a line sway will read.
    """
    assert parse_spec("  --release   Mod4  ") == "--release Mod4"


def test_a_toml_that_is_there_and_wrong_is_an_error_not_a_silent_default(paths):
    settingsdoc.write(paths.settings, keys={"selector": "--nope Mod4"})
    with pytest.raises(SettingsError):
        settings.load(paths.settings)


def test_an_unknown_action_is_refused_because_4_1_reserves_exactly_two(paths):
    paths.settings.write_text(
        settingsdoc.text().replace("[keys]\n", '[keys]\nscreenshot = "Mod4+p"\n'))
    with pytest.raises(SettingsError, match="reserves exactly"):
        settings.load(paths.settings)


def test_both_actions_on_one_key_is_refused_before_sway_ever_sees_it(paths):
    """Otherwise the generated file binds the same key twice, which is a config error —
    and a config error is a panel over the user's screen, not a message."""
    settingsdoc.write(paths.settings, keys={"selector": "Mod4+grave"})
    with pytest.raises(SettingsError, match="bound to both"):
        settings.load(paths.settings)


# --- applying it, where the reload's answer is not the evidence ----------------------------

@pytest.fixture
def sway(paths, tmp_path, monkeypatch):
    """A compositor that refuses whatever include `refuses` names, the way sway does: the
    reload answers success, and the refusal is a nag. Its socket names this process as the
    compositor, so this process's own binary stands for sway's."""
    made = []

    def make(**kwargs) -> FakeSway:
        fake = FakeSway(tmp_path / f"sway-ipc.0.{os.getpid()}.sock",
                        include=paths.host_keys_include, **kwargs)
        monkeypatch.setenv("SWAYSOCK", str(fake.path))
        made.append(fake)
        return fake

    yield make
    for fake in made:
        fake.close()


def test_applying_an_unchanged_file_does_not_reload(paths, sway):
    """A reload re-reads every config and is visible to the user; doing it for a no-op is a
    cost with no purchase."""
    fake = sway()
    hk = HostKeys(paths, EventLog(paths.events))
    hk.apply()
    hk.apply()
    assert fake.commands == ["reload"], "a second apply reloaded with nothing to change"


def test_a_config_error_rolls_back_so_the_static_bindings_still_hold(paths, sway):
    """The recovery state is the point. If the generated include is broken, the way back
    to a working system is the static bindings in host/sway/config — so a nag means the
    generated file goes away again rather than staying and being wrong."""
    paths.host_keys_include.write_text("# previous good\n")
    fake = sway(refuses=lambda config: "bindsym" in config)

    with pytest.raises(HostKeyError, match="rolled back") as raised:
        HostKeys(paths, EventLog(paths.events)).apply()
    assert paths.host_keys_include.read_text() == "# previous good\n"
    assert fake.commands == ["reload", "reload"], "the good include was not reloaded again"
    # A rebind restores the bindings generated before it, which are not the static floor.
    assert "generated before this change hold again" in str(raised.value)


def test_a_nag_that_was_already_there_is_not_blamed_on_this_write(paths, sway, tmp_path):
    """Only a nag that appeared *because of* this reload is evidence about it. A panel the
    user was already looking at is not."""
    fake = sway()
    fake.nags.append(nag(tmp_path))
    HostKeys(paths, EventLog(paths.events)).apply()
    assert "bindsym" in paths.host_keys_include.read_text()


def test_a_reload_that_never_finishes_rolls_back_rather_than_reading_as_applied(
        paths, sway, monkeypatch):
    """Sway's answer comes before the reload happens; only its event says the config was
    read. Without it nothing is known about the include, and the known one is restored."""
    paths.host_keys_include.write_text("# previous good\n")
    monkeypatch.setattr(hostkeys, "RELOAD_TIMEOUT", 0.2)
    sway(finishes_reloads=False)
    with pytest.raises(HostKeyError, match="could not be confirmed"):
        HostKeys(paths, EventLog(paths.events)).apply()
    assert paths.host_keys_include.read_text() == "# previous good\n"


def test_a_fork_of_the_compositor_that_has_not_exec_d_is_not_read_as_no_nag(
        paths, sway, monkeypatch):
    """A nag is sway's own binary until it execs. One that stays so is not a clean reload:
    the question is reported as unread, and the known include restored."""
    paths.host_keys_include.write_text("# previous good\n")
    monkeypatch.setattr(hostkeys, "EXEC_TIMEOUT", 0.2)
    unexeced = []
    fake = sway(refuses=lambda _config: unexeced.append(subprocess.Popen(
        (sys.executable, "-c", "import time; time.sleep(30)"))) or False)
    try:
        with pytest.raises(HostKeyError, match="is unread"):
            HostKeys(paths, EventLog(paths.events)).apply()
    finally:
        for process in unexeced:
            process.kill()
            process.wait()
    assert paths.host_keys_include.read_text() == "# previous good\n"
    assert fake.commands == ["reload"]


def test_a_nag_is_seen_as_the_shell_sway_starts_it_through():
    assert hostkeys._is_nag("sh", ("sh", "-c", "swaynag --type error --detailed-message"))
    assert hostkeys._is_nag("swaynag", ("swaynag", "--type", "error"))
    assert not hostkeys._is_nag("sh", ("sh", "-c", "swaybg -o *"))
    assert not hostkeys._is_nag("sh", ("sh", "-c", ""))


