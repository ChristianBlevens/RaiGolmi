"""The machine's settings: one TOML document of the user's, so that nothing is stuck how it was set.

A value is a setting when the user chose it or it is how their keys are; an engineering timeout is not. Each is read where it is used, at the moment
it is used, so a save takes effect without a restart: the keys, the keyboard, the display and
the look by the settings watch (`daemon._watch_settings`, `keyboard.py`, `look.py`), the model and the context
budgets at a tab's next start and the daemon's next check of it (`Session.context_budget`), the lapse
and the history's age at their next check.

The document ships as `settings.toml` beside this module and is written to the user's config the
first time the daemon starts (`install`); from then on the file is the only place a value
lives, and is only ever added to: a setting a later release brings goes in with its shipped
value. A file that is missing, missing a setting, or wrong is an error that names it, because
filling the gap would be the daemon inventing an answer; the catalog's save runs `parse`
first, so a wrong file is refused before it is written, and one written past that check holds
the last right save in its place (`in_force`).
"""
from __future__ import annotations

import re
import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import hostkeys

if TYPE_CHECKING:
    from .events import EventLog
    from .paths import Paths


class SettingsError(Exception):
    pass


_MODEL = re.compile(r"^[A-Za-z0-9._\[\]-]+$")
# xkb names; no quote or `;`, which would end the compositor command they are sent in.
_LAYOUT = re.compile(r"^[a-z0-9_-]+(,[a-z0-9_-]+)*$")
_VARIANT = re.compile(r"^[A-Za-z0-9_,-]*$")
_COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")

SHIPPED = Path(__file__).with_name("settings.toml")

# (section, key) → the kind of value it holds. What each one is, and its value, are the
# shipped document's.
SCHEMA: dict[str, dict[str, str]] = {
    "keys": {"selector": "key", "ai_terminal": "key"},
    "agents": {"model": "model", "context_budget_tokens": "whole",
               "machine_budget_tokens": "whole"},
    "keyboard": {"layout": "layout", "variant": "variant", "repeat_rate": "whole",
                 "repeat_delay": "whole"},
    "display": {"scale": "positive"},
    "look": {**{name: "colour" for name in (
                 "bg", "surface", "raised", "border", "text", "muted", "dim", "accent",
                 "accent_bg", "ok", "warn", "bad")},
             **{name: "whole" for name in (
                 "tab_length", "tab_depth", "reveal_ms", "drawer_max",
                 "terminal_height_percent", "menu_width", "menu_max_height", "card_width")},
             **{name: "share" for name in (
                 "drawer_share", "catalog_width_share", "catalog_height_share",
                 "catalog_backdrop")}},
    "questions": {"lapse_minutes": "positive"},
    "history": {"kept_days": "positive"},
}


@dataclass(frozen=True)
class Settings:
    keys: dict[str, str]
    model: str
    budget_tokens: int
    machine_budget_tokens: int
    layout: str
    variant: str
    repeat_rate: int
    repeat_delay: int
    scale: float
    look: dict[str, object]     # the fields of `ui.theme.Look`
    lapse_seconds: float
    kept_seconds: float


def install(path: Path) -> list[str]:
    """The shipped document becomes the user's, once. A file they have is only added to: each
    setting it lacks goes in with its shipped value and comment at the end of its section, and
    nothing they wrote moves. A file that is not TOML is left for `parse` to refuse. The
    settings added, as `[section].key`."""
    shipped = SHIPPED.read_text(encoding="utf-8")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        _write(path, shipped)
        return []
    text = path.read_text(encoding="utf-8")
    try:
        theirs = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    blocks = _blocks(shipped)
    added: list[str] = []
    lines = text.splitlines()
    for section, keys in SCHEMA.items():
        missing = [k for k in keys if k not in theirs.get(section, {})]
        if not missing:
            continue
        new = [line for k in missing for line in blocks[section][k]]
        header = next((i for i, line in enumerate(lines) if line.strip() == f"[{section}]"),
                      None)
        if header is None:
            lines += ["", f"[{section}]", *new]
        else:
            end = next((i for i in range(header + 1, len(lines))
                        if lines[i].lstrip().startswith("[")), len(lines))
            while end > header + 1 and not lines[end - 1].strip():
                end -= 1
            lines[end:end] = new
        added += [f"[{section}].{k}" for k in missing]
    if added:
        _write(path, "\n".join(lines) + "\n")
    return added


def _blocks(document: str) -> dict[str, dict[str, list[str]]]:
    """Each setting of a document as its lines: the comments directly above it, then it."""
    blocks: dict[str, dict[str, list[str]]] = {}
    section, comments = None, []
    for line in document.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section, comments = stripped[1:-1], []
            blocks[section] = {}
        elif stripped.startswith("#"):
            comments.append(line)
        elif "=" in stripped and section is not None:
            blocks[section][stripped.split("=")[0].strip()] = [*comments, line]
            comments = []
        else:
            comments = []
    return blocks


def _write(path: Path, text: str) -> None:
    staged = path.with_name(f".{path.name}.new")
    staged.write_text(text, encoding="utf-8")
    staged.replace(path)


def _positive(raw: dict, section: str, key: str, source: str) -> float:
    value = _value(raw, section, key, source)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise SettingsError(f"{source}: [{section}].{key} must be a positive number, "
                            f"not {value!r}")
    return float(value)


def _value(raw: dict, section: str, key: str, source: str):
    table = raw.get(section, {})
    if key not in table:
        raise SettingsError(f"{source}: [{section}].{key} is missing; every setting is in "
                            f"the document ({SHIPPED} holds each one's first value)")
    return table[key]


def _whole(raw: dict, section: str, key: str, source: str, least: int) -> int:
    value = _value(raw, section, key, source)
    if isinstance(value, bool) or not isinstance(value, int) or value < least:
        raise SettingsError(f"{source}: [{section}].{key} must be a whole number of at least "
                            f"{least}, not {value!r}")
    return value


def _named(raw: dict, section: str, key: str, source: str, form: re.Pattern, what: str) -> str:
    value = _value(raw, section, key, source)
    if not isinstance(value, str) or not form.match(value):
        raise SettingsError(f"{source}: [{section}].{key} must be {what}, not {value!r}")
    return value


def _look(raw: dict, source: str) -> dict[str, object]:
    look: dict[str, object] = {}
    for key, kind in SCHEMA["look"].items():
        if kind == "colour":
            look[key] = _named(raw, "look", key, source, _COLOUR, "a colour written #rrggbb")
        elif kind == "share":
            look[key] = _positive(raw, "look", key, source)
            if look[key] > 1:
                raise SettingsError(f"{source}: [look].{key} is a share, 0 to 1, "
                                    f"not {look[key]!r}")
        else:
            look[key] = _whole(raw, "look", key, source, 1)
    if look["terminal_height_percent"] > 100:
        raise SettingsError(f"{source}: [look].terminal_height_percent is at most 100, "
                            f"not {look['terminal_height_percent']!r}")
    return look


def parse(text: str, source: str) -> Settings:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"{source} is not valid TOML: {exc}") from exc
    unknown = set(raw) - set(SCHEMA)
    if unknown:
        raise SettingsError(f"{source}: unknown section(s) {sorted(unknown)}; the settings "
                            f"are {sorted(SCHEMA)}")
    for section, table in raw.items():
        if not isinstance(table, dict):
            raise SettingsError(f"{source}: [{section}] must be a table")
        extra = set(table) - set(SCHEMA[section])
        if extra and section != "keys":         # hostkeys names the two reserved keys
            raise SettingsError(f"{source}: unknown setting(s) {sorted(extra)} in "
                                f"[{section}]; it holds {sorted(SCHEMA[section])}")
    for action in SCHEMA["keys"]:
        _value(raw, "keys", action, source)
    try:
        keys = hostkeys.keys_from(raw["keys"], source)
    except hostkeys.HostKeyError as exc:
        raise SettingsError(str(exc)) from exc
    model = _named(raw, "agents", "model", source, _MODEL, "a model id")
    return Settings(
        keys=keys, model=model,
        budget_tokens=_whole(raw, "agents", "context_budget_tokens", source, 1000),
        machine_budget_tokens=_whole(raw, "agents", "machine_budget_tokens", source, 1000),
        layout=_named(raw, "keyboard", "layout", source, _LAYOUT,
                      "xkb layout names, comma-separated"),
        variant=_named(raw, "keyboard", "variant", source, _VARIANT, "an xkb variant name"),
        repeat_rate=_whole(raw, "keyboard", "repeat_rate", source, 0),
        repeat_delay=_whole(raw, "keyboard", "repeat_delay", source, 1),
        scale=_positive(raw, "display", "scale", source),
        look=_look(raw, source),
        lapse_seconds=_positive(raw, "questions", "lapse_minutes", source) * 60,
        kept_seconds=_positive(raw, "history", "kept_days", source) * 24 * 3600)


def load(path: Path) -> Settings:
    if not path.is_file():
        raise SettingsError(f"{path} does not exist; raigolmid writes it from {SHIPPED} when "
                            f"it starts")
    return parse(path.read_text(encoding="utf-8"), str(path))


_lock = threading.Lock()
# Per settings file: the save last looked at, the settings in force for it, and what was wrong
# with that save (None when it parsed).
_in_force: dict[Path, tuple[tuple[int, int] | None, Settings, str | None]] = {}
_reported: dict[Path, tuple[int, int] | None] = {}


def in_force(paths: "Paths") -> Settings:
    """The settings in force, for the daemon's own reads: the file re-read whenever it is saved.

    A wrong save (an agent's or an editor's that skipped the catalog's check) stops nothing
    it configures. The last right settings hold until it is put right, kept in the state
    directory so a restart finds them too. Nothing is invented: what holds is always what the
    user last saved right. Raises only when no right save was ever read. `current` is the same
    read, saying a wrong save."""
    return _read(paths)[0]


def current(paths: "Paths", events: "EventLog") -> Settings:
    """`in_force`, saying a wrong save once as `settings.invalid`, which the janitor takes. The
    questions thread reads this every second, so a wrong save is said within one."""
    held, save, error = _read(paths)
    with _lock:
        if error is not None and _reported.get(paths.settings, ()) != save:
            _reported[paths.settings] = save
            events.emit("settings.invalid", error=error)
    return held


def _read(paths: "Paths") -> tuple[Settings, tuple[int, int] | None, str | None]:
    path, kept = paths.settings, paths.settings_last_right
    with _lock:
        stat = path.stat() if path.is_file() else None
        save = (stat.st_mtime_ns, stat.st_size) if stat else None
        known = _in_force.get(path)
        if known is not None and known[0] == save:
            return known[1], save, known[2]
        try:
            text = path.read_text(encoding="utf-8") if save else None
            right = load(path) if text is None else parse(text, str(path))
        except SettingsError as exc:
            if known is not None:
                held = known[1]
            elif kept.is_file():
                held = parse(kept.read_text(encoding="utf-8"), str(kept))
            else:
                raise
            _in_force[path] = (save, held, str(exc))
            return held, save, str(exc)
        if not kept.is_file() or kept.read_text(encoding="utf-8") != text:
            kept.parent.mkdir(parents=True, exist_ok=True)
            staged = kept.with_name(f".{kept.name}.new")
            staged.write_text(text, encoding="utf-8")
            staged.replace(kept)
        _in_force[path] = (save, right, None)
        return right, save, None
