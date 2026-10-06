"""The user's settings document (`raigolmid/settings.py`): the shipped one is whole and is
what a machine starts from, and a missing, left-out or wrong value is refused by name rather than
filled in."""
from __future__ import annotations

import pytest

from raigolmid import settings
from raigolmid.settings import SettingsError
from tests import settingsdoc


def test_the_shipped_document_is_installed_once_and_never_over_the_users(tmp_path):
    path = tmp_path / "raigolmi" / "settings.toml"
    with pytest.raises(SettingsError, match="does not exist"):
        settings.load(path)
    settings.install(path)
    assert settings.load(path) == settings.load(settings.SHIPPED)
    path.write_text(settingsdoc.text(agents={"model": "their-model"}))
    settings.install(path)
    assert settings.load(path).model == "their-model"


@pytest.mark.parametrize("section, key", [
    (section, key) for section, keys in settings.SCHEMA.items() for key in keys])
def test_a_setting_left_out_is_refused_naming_it(section, key):
    text = "\n".join(line for line in settingsdoc.text().splitlines()
                     if line.split("=")[0].strip() != key)
    with pytest.raises(SettingsError, match=key):
        settings.parse(text, "settings.toml")


@pytest.mark.parametrize("section, key, value", [
    ("questions", "lapse_minutes", 0),
    ("history", "kept_days", "a week"),
    ("agents", "model", "two words"),
    ("agents", "context_budget_tokens", 400000.5),
    ("keyboard", "layout", "de; exec foot"),
    ("keyboard", "variant", 'a"b'),
    ("keyboard", "repeat_delay", 0),
    ("display", "scale", 0),
    ("look", "accent", "blue"),
    ("look", "drawer_share", 1.5),
    ("look", "terminal_height_percent", 120),
    ("look", "tab_depth", 0),
])
def test_a_wrong_value_is_refused_naming_it(section, key, value):
    with pytest.raises(SettingsError, match=key):
        settings.parse(settingsdoc.text(**{section: {key: value}}), "settings.toml")


@pytest.mark.parametrize("text, names", [
    (settingsdoc.text().replace("[agents]\n", "[agents]\neffort = 3\n"), "effort"),
    (settingsdoc.text() + "[theme]\n", "theme"),
])
def test_an_unknown_setting_is_refused_naming_it(text, names):
    with pytest.raises(SettingsError, match=names):
        settings.parse(text, "settings.toml")


def test_a_wrong_save_holds_the_last_right_one_and_is_said_once(tmp_path, monkeypatch):
    """A thread that reads the settings every tick must not die of a typo: the daemon is one
    with its threads, and the janitor that would fix the file dies with it."""
    from raigolmid.events import EventLog
    from raigolmid.paths import Paths
    for var in ("XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var))
    paths = Paths.from_env()
    events = EventLog(tmp_path / "events.jsonl")
    paths.settings.parent.mkdir(parents=True)
    paths.settings.write_text(settingsdoc.text(agents={"model": "theirs"}))
    assert settings.current(paths, events).model == "theirs"

    paths.settings.write_text("[agents\n")
    for _ in range(3):
        assert settings.current(paths, events).model == "theirs"
    said = [e for e in events.read() if e.type == "settings.invalid"]
    assert len(said) == 1

    monkeypatch.setattr(settings, "_in_force", {})
    assert settings.in_force(paths).model == "theirs", "a restart finds the last right save"
