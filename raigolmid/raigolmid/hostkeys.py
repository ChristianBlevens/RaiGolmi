"""The reserved host keys.

Two bindings reach the host even when a nested face has focus: a bare **Super tap** for the
selector and **Super+grave** for the AI terminal. They are read from the user's settings
(`settings.toml`'s `[keys]`, `settings.py`) and rebindable without rebuilding the host image:
this module regenerates the host compositor's binding config and reloads it.

**A binding is installed by writing the config include and reloading — never
`swaymsg bindsym`**, which answers `{"success":true}` and installs nothing. The include
is `/run/raigolmid/host-keys.conf`, created empty by tmpfiles.d and read by
`host/sway/config`.

⚠ **What is unbound is exactly what is currently bound — no more and no less.** Sway
refuses in both directions, and each refusal is a **swaynag** panel reading "There are
errors in your config file" over the user's screen:

- binding a key sway already has bound is a config error, and `host/sway/config` binds both
  keys statically — it must, because the bare host state is the one the user lands in
  when `raigolmid` will not start, so those bindings have to survive a boot with no
  daemon and cannot be deleted;
- **`unbindsym` on a key nothing has bound is equally a config error.** So the include
  cannot simply unbind everything it is about to bind: a newly rebound key has never been
  bound by anyone, and unbinding it is the error it was meant to avoid.

So the set to unbind is **exactly the static defaults, always**. A reload re-reads the whole
config from the top, so at the moment sway parses this include the only bindings that exist
are the ones `host/sway/config` has just made: whatever a previous generation of this file
bound is already gone. That makes the rule constant rather than stateful, and it is why the
generated specs are never unbound — a rebound key has never been bound by anyone.

The static defaults are unbound whatever the user has rebound to, because a rebind that
left them installed would leave the old key working as well as the new one.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from ui import hostipc
from ui.hostipc import HostIpcError

from .events import EventLog
from .paths import Paths

# What `host/sway/config` binds when there is no generated include. Unbound before anything
# is installed, so a rebound key does not leave its predecessor live. It must mirror that file
# exactly: unbinding a key nothing has bound is as much a config error as a duplicate bind.
#
# ⚠ The bare tap is the **keysym** `Super_L`, never the modifier name `Mod4`. `bindsym
# --release Mod4` parses, validates and reloads without complaint, and then never fires: a
# modifier name as the key means a binding with no key in it, and a tap never matches.
# `Mod4+grave` keeps `Mod4` because there
# Mod4 is the modifier and `grave` is the key, which is the case the name is for.
STATIC_DEFAULTS = ("--release Super_L", "Mod4+grave")

# The two reserved keys; which key each is on is the user's (`settings.toml` `[keys]`).
ACTIONS = ("selector", "ai_terminal")

# `rai selector` is the overlay, the same command `host/sway/config`'s floor binds. It
# toggles: the same key opens and closes it, because a key that only opens leaves the user holding a surface with no way
# back that they were not told about.
#
# `rai ai` rather than bare tmux, because the generated include replaces `host/sway/config`'s
# floor with the real AI terminal — the one with the tabs, the body names and the reconcile
# against the daemon. It still works *with raigolmid stopped*: `rai ai` reports the daemon
# unreachable and attaches anyway, onto a base window that is a shell and cannot exit out from
# under it. Bare tmux would be a different session from `rai ai`'s, so the key and the command
# would reach two terminals that cannot see each other's tabs.
DEFAULT_COMMANDS = {
    "selector": "rai selector",
    "ai_terminal": "rai ai --toggle",
}

# Sway's own binding flags. Listing them rather than accepting any `--word` is what keeps a
# typo from reaching the config, where it is an error banner rather than a message.
_FLAGS = frozenset({
    "--release", "--locked", "--to-code", "--whole-window", "--border",
    "--exclude-titlebar", "--inhibited", "--no-repeat",
})
_KEY_NAME = re.compile(r"^[A-Za-z0-9_]+$")

# Caps, not waits: sway reports a finished reload and a forked nag execs within milliseconds,
# so a cap is reached only by a compositor that has stopped answering.
RELOAD_TIMEOUT = 10.0
EXEC_TIMEOUT = 2.0
POLL = 0.01


def _processes() -> dict[int, tuple[str, tuple[str, ...]]]:
    """Every process now, by pid, with its kernel name and argv — read from /proc because
    `pgrep` is procps and the host ships a minimal package list."""
    found = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = entry.joinpath("comm").read_text().strip()
            argv = tuple(a for a in entry.joinpath("cmdline").read_bytes()
                         .decode("utf-8", "replace").split("\0") if a)
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        found[int(entry.name)] = (comm, argv)
    return found


def _is_nag(comm: str, argv: tuple[str, ...]) -> bool:
    """sway runs its nag as `sh -c "swaynag …"` (sway 1.10.1 `swaynag.c`), so one is seen
    either as that shell or, once the shell has exec'd it, as swaynag."""
    if comm == "swaynag":
        return True
    return (len(argv) >= 3 and Path(argv[0]).name == "sh" and argv[1] == "-c"
            and Path(argv[2].split(None, 1)[0] if argv[2].strip() else "").name == "swaynag")


def _lineage(pid: int) -> tuple[str, int] | None:
    """A process's kernel name and how many pid namespaces deep it is, both readable for any
    process. sway carries file capabilities, which makes its `/proc/<pid>/exe` unreadable
    even to its own user."""
    try:
        comm = Path(f"/proc/{pid}/comm").read_text().strip()
        status = Path(f"/proc/{pid}/status").read_text()
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return None
    nspid = next((line.split()[1:] for line in status.splitlines()
                  if line.startswith("NSpid:")), [])
    return comm, len(nspid)


def nags_since(before: set[int], swaysock: str) -> set[int]:
    """The nags sway spawned since `before` was read.

    Asked only once sway has said the reload is done: it spawns a nag while it re-reads
    the config, and tells subscribers only after (sway 1.10.1 `commands/reload.c`
    `do_reload`). A child it has forked but not yet exec'd still carries the compositor's
    name and cannot be judged, so it is waited for. A face's sway has the same name and
    never becomes anything else, and is told apart by its own pid namespace."""
    match = re.search(r"sway-ipc\.\d+\.(\d+)\.sock$", swaysock)
    if match is None:
        raise HostKeyError(f"{swaysock} is not named as sway names its socket, so the "
                           "compositor's own forks cannot be told from anything else")
    compositor = _lineage(int(match[1]))
    if compositor is None:
        raise HostKeyError(f"the compositor behind {swaysock} (pid {match[1]}) is not running")
    deadline = time.monotonic() + EXEC_TIMEOUT
    while True:
        new = {pid: p for pid, p in _processes().items() if pid not in before}
        forked = sorted(pid for pid, (comm, _) in new.items()
                        if comm == compositor[0] and _lineage(pid) == compositor)
        if not forked:
            return {pid for pid, (comm, argv) in new.items() if _is_nag(comm, argv)}
        if time.monotonic() >= deadline:
            raise HostKeyError(
                f"sway forked {forked} during the reload and within {EXEC_TIMEOUT:g}s they "
                "had not become anything, so whether it reported a config error is unread")
        time.sleep(POLL)


class HostKeyError(ValueError):
    """A host-key definition that must not reach the compositor's config."""


@dataclass(frozen=True, slots=True)
class Binding:
    action: str
    spec: str      # flags plus key combo, as sway writes them
    command: str

    def bindsym(self) -> str:
        return f"bindsym {self.spec} exec {self.command}"


def parse_spec(spec: str) -> str:
    """Validate a sway key spec and return it normalised.

    Conservative on purpose. An invalid binding does not fail at the point it is written —
    it fails at the reload, as a panel over the user's screen, and by then the daemon has
    already reported success. So the refusal happens here, where it can carry a reason.
    """
    if "\n" in spec or "\r" in spec:
        raise HostKeyError("a key spec is one line; this one contains a newline")
    parts = spec.split()
    if not parts:
        raise HostKeyError("a key spec cannot be empty")
    *flags, combo = parts
    for flag in flags:
        if flag not in _FLAGS:
            raise HostKeyError(
                f"'{flag}' is not a sway binding flag (known: {', '.join(sorted(_FLAGS))})")
    keys = combo.split("+")
    if not all(keys) or not all(_KEY_NAME.match(k) for k in keys):
        raise HostKeyError(
            f"'{combo}' is not a key combination — expected names joined by '+', "
            f"such as 'Mod4+grave'")
    return " ".join((*flags, combo))


def keys_from(section: object, source: str) -> dict[str, str]:
    """The user's key choices from the settings' `[keys]` table, one for each of `ACTIONS`."""
    if not isinstance(section, dict):
        raise HostKeyError(f"{source}: [keys] must be a table")
    unknown, missing = set(section) - set(ACTIONS), set(ACTIONS) - set(section)
    if unknown or missing:
        raise HostKeyError(
            f"{source}: [keys] holds {sorted(section)}. The host reserves exactly two key "
            f"actions: {sorted(ACTIONS)}")
    keys = {}
    for action, spec in section.items():
        if not isinstance(spec, str):
            raise HostKeyError(f"{source}: [keys].{action} must be a string")
        keys[action] = parse_spec(spec)

    # Two actions on one key is a config error when sway reads it, which means a panel over
    # the user's screen rather than a message. A *face* losing a key to the host is
    # left unresolved deliberately, but the host's own two keys colliding is just wrong.
    seen: dict[str, str] = {}
    for action in sorted(keys):
        if (other := seen.get(keys[action])) is not None:
            raise HostKeyError(
                f"{source}: '{keys[action]}' is bound to both {other} and {action}. The host's "
                f"two reserved keys must differ.")
        seen[keys[action]] = action
    return keys


def render(bindings: list[Binding], source: Path) -> str:
    """The include's contents.

    Unbinds exactly the static defaults and then binds the current set. Not the specs being
    bound: sway re-reads the whole config on reload, so those are not bound at this point
    and `unbindsym` on an unbound key is a config error.
    """
    unbind = list(STATIC_DEFAULTS)
    lines = [
        f"# Generated by raigolmid from {source}. Do not edit: it is rewritten",
        "# whenever that file changes, and it is empty until raigolmid first runs.",
        "#",
        "# The unbindsym lines are required, not tidiness. host/sway/config binds these keys",
        "# statically so the bare host state survives a boot with no daemon, and",
        "# binding an already-bound key is a config error that puts a swaynag panel over the",
        "# user's screen. Only those two are unbound: sway re-reads the whole config on",
        "# reload, so nothing this file bound last time is bound now, and unbindsym on an",
        "# unbound key is the same error.",
        "",
        *(f"unbindsym {spec}" for spec in unbind),
        "",
        *(b.bindsym() for b in bindings),
        "",
    ]
    return "\n".join(lines)


class HostKeys:
    """Generates the host compositor's binding include and reloads it."""

    def __init__(self, paths: Paths, events: EventLog,
                 commands: dict[str, str] | None = None) -> None:
        self.paths = paths
        self.events = events
        self.commands = dict(commands or DEFAULT_COMMANDS)

    def bindings(self) -> list[Binding]:
        from . import settings            # settings validates its [keys] table with this module
        keys = settings.current(self.paths, self.events).keys
        return [Binding(action=action, spec=keys[action], command=self.commands[action])
                for action in sorted(keys)]

    def apply(self, swaysock: str | None = None) -> list[Binding]:
        """Write the include and reload the compositor.

        The write is atomic. A half-written include is a config error, and a config error in
        *this* file costs the two keys that are the way back to a working system — so the
        compositor never sees a partial one.

        ⚠ The reload's status is not the check. sway answers a reload `{"success":true}`
        with a config error outstanding, and sway's own report of that error is a
        **swaynag** panel over the user's screen. So a nag that was not there before the
        reload is what is looked for, and the generated file is rolled back when one
        appears — leaving the static bindings, which are the way back to a working system.
        """
        target = self.paths.host_keys_include
        target.parent.mkdir(parents=True, exist_ok=True)
        previous = target.read_text(encoding="utf-8") if target.is_file() else ""

        bindings = self.bindings()
        content = render(bindings, self.paths.settings)
        if previous == content:
            # Nothing changed, so nothing is reloaded. A reload is visible to the user —
            # it restarts nothing but it does re-read every config — and doing it on a
            # no-op is a cost with no purchase. What the user's settings say is what holds,
            # which is what `applied` reports: it settles a refusal they saved back over.
            self.events.emit("hostkeys.applied", unchanged=True,
                             bindings={b.action: b.spec for b in bindings})
            return bindings

        swaysock = swaysock or hostipc.socket_path()
        before = set(_processes())
        self._write(target, content)
        try:
            events = hostipc.subscribe(["workspace"], swaysock, timeout=RELOAD_TIMEOUT)
            try:
                hostipc.run_command("reload", swaysock)
                next(e for e in events if e.get("change") == "reload")
            finally:
                events.close()
            new_nags = nags_since(before, swaysock)
        except (HostIpcError, HostKeyError) as exc:
            self._write(target, previous)
            raise HostKeyError(f"the host compositor's reload could not be confirmed, so the "
                               f"include is rolled back: {exc}") from exc

        if new_nags:
            self._write(target, previous)
            holding = ("the static bindings in host/sway/config hold" if not previous.strip()
                       else "the bindings generated before this change hold again")
            try:
                hostipc.run_command("reload", swaysock)
            except HostIpcError as exc:
                holding = f"the rollback's reload failed too ({exc}), so they are still live"
            raise HostKeyError(
                f"sway reported a config error for the generated bindings (swaynag "
                f"{sorted(new_nags)}), so they have been rolled back and {holding}. "
                f"The file that was refused:\n{content}")

        self.events.emit("hostkeys.applied",
                         bindings={b.action: b.spec for b in bindings})
        return bindings

    @staticmethod
    def _write(target: Path, content: str) -> None:
        """Atomically, because a half-written include is a config error — and a config
        error in *this* file costs the two keys that are the way back to a working
        system."""
        tmp = target.with_name(target.name + ".new")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(target)
