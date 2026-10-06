"""raigolmid installs the reserved keys, draws the control and opens the selector over an
empty screen, and none of it can stop the daemon.

Both are the daemon's duties, and they are the way back to a working
system, which cuts both ways: they have to be brought up without being asked, and a daemon
that refused to start because it could not draw a button would take the session down over the
thing that exists to repair it.

The bring-up is tested on its own rather than through a real `Daemon`, which would take the
single-instance lock and open a socket. What is asserted is the policy, which is the part that
can be got wrong.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from raigolmid.daemon import BOOT_SCREEN_COMMAND, Daemon              # noqa: E402
from raigolmid import hostsurfaces, labels, settings                  # noqa: E402
from raigolmid.events import EventLog                                 # noqa: E402
from raigolmid.hostkeys import DEFAULT_COMMANDS, HostKeyError         # noqa: E402
from raigolmid.hostimages import SOURCE_ENV                           # noqa: E402
from raigolmid.hostsurfaces import (CONTAINERS, ai_terminal_command,  # noqa: E402
                                   CONTROL_CONTAINER, NOTIFY_CONTAINER,
                                   SELECTOR_CONTAINER, HostSurfaceError,
                                   restore_resident)
from raigolmid.paths import Paths                                     # noqa: E402
from tests.fakeruntime import FakeRuntime                        # noqa: E402
from tests import settingsdoc                                        # noqa: E402


class Keys:
    """A stand-in for the generator that records whether it was asked, and can refuse the
    way the real one refuses — by raising after it has rolled itself back."""

    def __init__(self, raises: Exception | None = None) -> None:
        self.commands = dict(DEFAULT_COMMANDS)
        self.applied = 0
        self.raises = raises

    def apply(self):
        self.applied += 1
        if self.raises is not None:
            raise self.raises
        return []


class Screen:
    """What `Faces.current()` reports: a running face, or nothing drawn."""

    def __init__(self, face=None) -> None:
        self.face = face

    def current(self):
        return self.face


def _sources(root: Path) -> Path:
    """The host image's source tree, in the repository's layout (`host/Containerfile`)."""
    for surface in ("host_control", "selector_native", "notify_popup", "catalog"):
        (root / "ui" / surface).mkdir(parents=True, exist_ok=True)
        # Copying its own directory, as each surface's does: an image is what it copies.
        (root / "ui" / surface / "Containerfile").write_text(
            f"FROM fedora:44\nCOPY ui/{surface}/ /opt/raigolmi/ui/{surface}/\n")
    (root / "agents" / "claude").mkdir(parents=True, exist_ok=True)
    (root / "agents" / "claude" / "Dockerfile").write_text("FROM scratch\n")
    return root


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    paths = Paths(state=tmp_path / "state", data=tmp_path / "data",
                  config=tmp_path / "config" / "raigolmid", runtime=tmp_path / "run")
    paths.state.mkdir(parents=True, exist_ok=True)
    # What the daemon's start writes before anything reads a setting.
    settings.install(paths.settings)
    paths.runtime.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(SOURCE_ENV, str(_sources(tmp_path / "src")))
    stub = SimpleNamespace(
        paths=paths, events=EventLog(paths.events), host_keys=Keys(), _threads=[],
        session=SimpleNamespace(runtime=FakeRuntime(), faces=Screen(),
                                catalogue=SimpleNamespace(faces={}), tabs_ensured=[]))
    stub.session.ensure_tabs = lambda: stub.session.tabs_ensured.append(True)
    # The real methods, on a stub that carries only what they touch — a `Daemon` would take
    # the single-instance lock and open a socket.
    for name in ("_try", "_draw_host_surfaces", "_start_resident_selector",
                 "_show_boot_screen", "_apply_keyboard", "_apply_look"):
        setattr(stub, name, MethodType(getattr(Daemon, name), stub))
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    # The first-start screen asks the host compositor to run a terminal; there is none here,
    # so what it asked for is recorded instead.
    stub.execs = []
    monkeypatch.setattr("raigolmid.faces.HostCompositor", lambda *a, **k: SimpleNamespace(
        exec=stub.execs.append))
    return stub


def _bring_up(stub) -> None:
    Daemon._bring_up_host_surfaces(stub)
    if not hasattr(stub, "_drawing"):
        return                              # nothing to draw on: said as unasked
    stub._drawing.join(timeout=10)
    assert not stub._drawing.is_alive(), "the surfaces never finished coming up"
    assert stub._threads == [], "drawing them is a one-off, not a lifelong thread"


def _events(stub) -> list[str]:
    return [e.type for e in EventLog(stub.paths.events).tail(50)]


def test_the_control_is_given_the_terminal_s_command_and_nothing_else(daemon):
    """The control is the terminal's surface: it starts the window and moves it, and the
    daemon owns what the window runs, so a change to `rai ai` reaches every door at once.
    The selector's door is its own handle, so the control carries no command for it."""
    _bring_up(daemon)
    spec = daemon.session.runtime._record(CONTROL_CONTAINER)["spec"]
    assert list(spec.command) == ["--terminal-command", ai_terminal_command(daemon.paths)]
    assert DEFAULT_COMMANDS["ai_terminal"] not in spec.command


def test_the_terminal_is_drawn_in_the_users_palette(daemon):
    settingsdoc.write(daemon.paths.settings, look={"bg": "#000000", "text": "#ffffff"})
    command = ai_terminal_command(daemon.paths)
    assert "-o colors.background=000000" in command
    assert "-o colors.foreground=ffffff -o colors.selection-background=283457" in command


def test_keys_that_will_not_install_still_leave_the_button(daemon):
    """The two doors fail independently, or the point of having two is lost."""
    daemon.host_keys = Keys(raises=HostKeyError("sway refused the include"))
    _bring_up(daemon)
    assert "hostkeys.failed" in _events(daemon)
    control = daemon.session.runtime.inspect(CONTROL_CONTAINER)
    assert control is not None and control.running


def test_the_popup_is_resident_from_the_start(daemon):
    """A failure of the daemon's own start is one the popup may have to say."""
    _bring_up(daemon)
    popup = daemon.session.runtime.inspect(NOTIFY_CONTAINER)
    assert popup is not None and popup.running
    assert popup.labels[labels.ROLE] == labels.Role.NOTIFY


def test_a_control_that_cannot_be_drawn_is_reported_and_does_not_raise(daemon, monkeypatch,
                                                                         tmp_path):
    """An image that cannot be built must not stop the daemon: everything else in the
    session is still the daemon's to run."""
    monkeypatch.setenv(SOURCE_ENV, str(tmp_path / "no-sources"))
    _bring_up(daemon)
    assert "control.failed" in _events(daemon)
    assert daemon.host_keys.applied == 1


def test_a_running_selector_is_kept_until_its_sources_change(daemon, tmp_path):
    """Kept across a daemon restart, since it may be open under the user's hand; replaced at
    rest once it is on an old image, or a code change or a new disk would never reach their
    screen."""
    _bring_up(daemon)
    first = daemon.session.runtime.inspect(SELECTOR_CONTAINER)
    _bring_up(daemon)
    assert daemon.session.runtime.inspect(SELECTOR_CONTAINER).id == first.id

    (tmp_path / "src" / "ui" / "selector_native" / "selector.py").write_text("# changed\n")
    _bring_up(daemon)
    now = daemon.session.runtime.inspect(SELECTOR_CONTAINER)
    assert now.id != first.id and now.image != first.image and now.running
    assert "--hidden" in daemon.session.runtime.spec_of(SELECTOR_CONTAINER).command


def test_an_unanticipated_failure_also_leaves_the_daemon_up(daemon):
    """The policy is not "the two failures we thought of": any exception in the generator
    leaves the daemon up."""
    daemon.host_keys = Keys(raises=AttributeError("Paths has no attribute host_keys_include"))
    _bring_up(daemon)
    assert "hostkeys.failed" in _events(daemon)
    control = daemon.session.runtime.inspect(CONTROL_CONTAINER)
    assert control is not None and control.running


def test_an_unanticipated_failure_keeps_its_traceback(daemon):
    """The reason is the whole value: an agent repairing this reads the event log."""
    daemon.host_keys = Keys(raises=AttributeError("no attribute host_keys_include"))
    _bring_up(daemon)
    failure = next(e for e in EventLog(daemon.paths.events).tail(50)
                   if e.type == "hostkeys.failed")
    assert "AttributeError" in failure.data["error"]
    assert "Traceback" in failure.data["traceback"]


def test_a_first_start_says_what_it_is_building(daemon):
    """Minutes of black screen while the images build is the machine looking broken. The
    screen is a host terminal because the host has no toolkit — GTK is in the very images
    being waited for."""
    _bring_up(daemon)
    assert daemon.execs == [BOOT_SCREEN_COMMAND]


def test_the_tabs_that_always_exist_are_opened_as_the_surfaces_come_up(daemon):
    """AI is always ready: the machine tab, and the selected body's, before the terminal
    is first shown."""
    _bring_up(daemon)
    assert daemon.session.tabs_ensured == [True]


def test_a_surface_that_exited_comes_back_on_reconcile_as_it_rests(daemon):
    """The janitor takes a host surface that exits, and its reconcile is the repair.
    The popup has no key to start it again, so without this only a daemon restart would."""
    _bring_up(daemon)
    runtime = daemon.session.runtime
    runtime.kill(NOTIFY_CONTAINER)
    runtime.kill(SELECTOR_CONTAINER)
    control = runtime.inspect(CONTROL_CONTAINER).id
    failures: list = []

    outcome = restore_resident(runtime, daemon.paths, lambda *f: failures.append(f))

    assert outcome == {"selector": "restored", "control": "running", "notify": "restored",
                       "catalog": "running"}
    assert runtime.inspect(NOTIFY_CONTAINER).running
    assert "--hidden" in runtime.spec_of(SELECTOR_CONTAINER).command, \
        "a restored drawer must not open itself and take the keyboard"
    assert runtime.inspect(CONTROL_CONTAINER).id == control, "a running surface was replaced"
    assert failures == []


def test_a_surface_that_will_not_come_back_is_handed_on_and_the_rest_still_are(
        daemon, monkeypatch, tmp_path):
    _bring_up(daemon)
    runtime = daemon.session.runtime
    for name in (SELECTOR_CONTAINER, CONTROL_CONTAINER, NOTIFY_CONTAINER):
        runtime.kill(name)
    monkeypatch.setattr("raigolmid.hostsurfaces.start_control", _refuse)
    failures: list = []

    outcome = restore_resident(runtime, daemon.paths, lambda *f: failures.append(f))

    assert outcome["control"].startswith("failed: no GTK") and failures == [("control",
                                                                             "no GTK")]
    assert outcome["selector"] == outcome["notify"] == "restored"


def _refuse(*_a, **_k):
    raise HostSurfaceError("no GTK")


@pytest.mark.parametrize("surface", ["host_control/control.py", "notify_popup/popup.py",
                                     "selector_native/selector.py", "catalog/window.py"])
def test_a_host_surface_is_not_a_unique_application(surface):
    """Static, because a second GTK instance cannot run here. A unique application id on the
    session bus in the shared runtime dir makes a restarted surface hand over to the one still
    exiting and exit 0 having drawn nothing, read as `container.unfixable`."""
    source = (ROOT / "ui" / surface).read_text()
    assert "flags=Gio.ApplicationFlags.NON_UNIQUE" in source


def test_a_look_he_saves_redraws_each_surface_drawn_with_the_old_one(daemon, monkeypatch):
    """A surface reads its look once, as it starts, so a save is seen only by redrawing; the
    terminal's colours are the control's command, so its window is closed for the next show
    to open in them."""
    closed = []
    monkeypatch.setattr("raigolmid.hostsurfaces.close_ai_terminal", lambda: closed.append(1))
    _bring_up(daemon)
    runtime = daemon.session.runtime
    before = {name: runtime.inspect(name).id for name in CONTAINERS.values()}
    daemon._apply_look()
    assert {name: runtime.inspect(name).id for name in CONTAINERS.values()} == before
    assert closed == [] and "look.applied" not in _events(daemon), "the same look is no change"

    settingsdoc.write(daemon.paths.settings, look={"accent": "#ff8800"})
    daemon._apply_look()
    for name in CONTAINERS.values():
        assert runtime.inspect(name).id != before[name], f"{name} is drawn with the old look"
    assert json.loads(daemon.paths.look.read_text())["accent"] == "#ff8800"
    assert closed == [1]
    assert "look.applied" in _events(daemon)
    assert hostsurfaces.selector_current(runtime, daemon.paths)


def test_foot_ini_draws_the_palettes_colours_as_the_users_defaults():
    """The host's other terminals read `host/foot/foot.ini`; the AI terminal is given the
    user's palette on its command line. As shipped they are the same colours."""
    ini = (Path(__file__).resolve().parents[2] / "host" / "foot" / "foot.ini").read_text()
    colours = dict(line.split("=", 1) for line in
                   ini.split("[colors]", 1)[1].split("\n[", 1)[0].splitlines()
                   if "=" in line and not line.startswith("#"))
    defaults = settings.load(settings.SHIPPED).look
    for key, name in hostsurfaces.FOOT_COLOURS.items():
        assert "#" + colours[key].strip() == defaults[name], key
