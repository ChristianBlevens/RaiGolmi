"""The host's three surfaces move by one mechanism:
one channel they are asked through, one slide they move by, and the terminal's window placed
by its surface over sway's IPC. The GTK half is seen on the machine; what is decided apart
from GTK is held here."""
from __future__ import annotations

import threading
import time

import pytest

from tests.fakesway import FACE_ID, OUTPUT_HEIGHT, FakeSway
from ui import surfaces
from ui.hostipc import HostIpcError, subscribe
from ui.host_control import window as terminal_window
from ui.host_control.window import TerminalWindow, WindowError
from ui.slide import Slide, margin

# What the control is handed to start the terminal; opaque to the mechanism.
AI_TERMINAL_COMMAND = f"foot --app-id={surfaces.TERMINAL_APP_ID} rai ai"


@pytest.fixture
def runtime_dir(tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run))
    return run


# --- the channel ---------------------------------------------------------------------------
def test_a_surface_answers_with_its_own_state_and_a_refusal_is_raised(runtime_dir):
    def answer(verb: str) -> str:
        if verb == "open":
            raise RuntimeError("the window never mapped")
        return "closed"
    surfaces.serve(surfaces.HISTORY, answer)
    assert surfaces.ask(surfaces.HISTORY, "state") == "closed"
    with pytest.raises(surfaces.SurfaceRefused, match="RuntimeError: the window never mapped"):
        surfaces.ask(surfaces.HISTORY, "open")


def test_a_surface_not_running_is_absent_not_refused(runtime_dir):
    with pytest.raises(surfaces.SurfaceAbsent):
        surfaces.ask(surfaces.TERMINAL, "state")


def test_opening_one_closes_the_others_and_never_itself(runtime_dir):
    asked: list[tuple[str, str]] = []
    for name in surfaces.NAMES:
        surfaces.serve(name, lambda verb, name=name: asked.append((name, verb)) or "closed")
    surfaces.close_others(surfaces.SELECTOR).join(5)
    assert sorted(asked) == [(surfaces.CATALOG, "close"), (surfaces.HISTORY, "close"),
                             (surfaces.TERMINAL, "close")]


# --- the slide -----------------------------------------------------------------------------
def test_a_slide_starts_its_clock_once_its_first_frame_is_drawn():
    """A tick comes before its frame's paint, so however long the first frame takes to
    paint — a panel built for the opening — none of it comes out of the slide."""
    placed: list[float] = []
    slide = Slide(placed.append, seconds=0.14)
    assert slide.to(1.0)
    assert slide.frame(100.0)                # the first frame: the start, drawn once
    assert slide.frame(100.3)                # painted 300 ms later: the clock starts here
    assert placed == [0.0, 0.0]
    assert slide.frame(100.37)
    assert 0 < placed[-1] < 1
    assert not slide.frame(100.44)
    assert placed[-1] == 1.0 and not slide.moving


def test_a_reversed_slide_goes_back_from_where_it_is():
    placed: list[float] = []
    arrived: list[float] = []
    slide = Slide(placed.append, seconds=0.1)
    slide.to(1.0, arrived.append)
    for now in (0.0, 0.01, 0.06):            # drawn once, the clock started, half way
        slide.frame(now)
    half = placed[-1]
    assert 0 < half < 1
    slide.to(0.0)
    slide.frame(1.0)
    assert placed[-1] == half                # no jump to either end
    now = 1.0
    while slide.frame(now := now + 0.016):
        assert 0 <= placed[-1] <= half
    assert placed[-1] == 0.0
    # The open that was asked for is answered with where the surface ended.
    assert arrived == [0.0]


def test_asking_for_where_it_already_is_arrives_at_once():
    arrived: list[float] = []
    slide = Slide(lambda _: None)
    assert not slide.to(0.0, arrived.append)
    assert arrived == [0.0]


def test_a_panel_smaller_than_its_room_is_a_margin_not_a_resize():
    assert margin(0.0, 300, 420) == -420                 # closed: only the tab on screen
    assert margin(1.0, 300, 420) == -120                 # open: the panel's far side on the edge
    assert margin(1.0, 420, 420) == 0
    assert margin(1.0, 900, 420) == 0                    # never further out than the room


# --- the terminal's window -----------------------------------------------------------------
@pytest.fixture
def sway(tmp_path, monkeypatch):
    fake = FakeSway(tmp_path / "sway.sock", terminal_command=AI_TERMINAL_COMMAND,
                    app_id=surfaces.TERMINAL_APP_ID)
    monkeypatch.setenv("SWAYSOCK", str(tmp_path / "sway.sock"))
    yield fake
    fake.close()


def watched(sway) -> TerminalWindow:
    """A window with its map watched from sway's event stream, as the control watches it."""
    window = TerminalWindow(AI_TERMINAL_COMMAND, str(sway.path), floor=96)
    subscribed = threading.Event()

    def watch() -> None:
        try:
            events = subscribe(["window"], str(sway.path))
            subscribed.set()
            for event in events:
                if (event.get("container") or {}).get("app_id") == surfaces.TERMINAL_APP_ID \
                        and event.get("change") == "new":
                    window.mapped.set()
        except HostIpcError:
            return
    threading.Thread(target=watch, daemon=True).start()
    subscribed.wait(5)
    time.sleep(0.05)                        # the subscription registered on sway's side
    return window


def test_the_first_show_starts_the_window_below_the_screen_over_the_face(sway):
    """"started", not "shown": a started window runs its own `rai ai`, and the caller doing
    that work as well is what flickered between the base window and the agent's."""
    window = watched(sway)
    assert window.bring_out() is True
    assert sway.terminal["visible"] and sway.face["fullscreen_mode"] == 0
    assert window.output == OUTPUT_HEIGHT and window.height == OUTPUT_HEIGHT * 38 // 100
    assert sway.positions[-1] == OUTPUT_HEIGHT           # waiting below the edge
    assert window.fullscreen == FACE_ID


def test_a_slide_moves_the_window_by_position_alone(sway):
    window = watched(sway)
    window.bring_out()
    for position in (0.0, 0.5, 1.0):
        window.place(position)
    assert sway.positions[-3:] == [OUTPUT_HEIGHT, OUTPUT_HEIGHT - window.height // 2,
                                   OUTPUT_HEIGHT - window.height]
    assert sway.terminal["rect"]["height"] == window.height


def test_putting_it_away_gives_the_face_its_fullscreen_back(sway):
    window = watched(sway)
    window.bring_out()
    window.put_away()
    assert not sway.terminal["visible"] and sway.face["fullscreen_mode"] == 1
    assert not window.out


def test_the_second_show_brings_back_the_same_window(sway):
    window = watched(sway)
    window.bring_out()
    window.put_away()
    assert window.bring_out() is False
    assert sum(c.startswith("exec ") for c in sway.commands) == 1


def test_a_window_that_never_maps_is_reported(sway, monkeypatch):
    sway.maps = False
    monkeypatch.setattr(terminal_window, "MAP_SECONDS", 0.3)
    with pytest.raises(WindowError, match="mapped no window"):
        watched(sway).bring_out()


def test_a_control_started_over_an_open_terminal_takes_it_as_it_is(sway):
    watched(sway).bring_out()
    fresh = TerminalWindow(AI_TERMINAL_COMMAND, str(sway.path))
    assert fresh.found_out() and fresh.out


# --- the host's side -----------------------------------------------------------------------
def test_the_reserved_key_s_command_asks_the_terminal_and_prints_its_answer(runtime_dir, capsys):
    # The binding is `rai ai --toggle` (hostkeys.py), so the command itself is run, not the
    # function it calls: a name the command shadowed locally raised before the socket was asked.
    from rai.__main__ import main
    asked = []
    surfaces.serve(surfaces.TERMINAL, lambda verb: asked.append(verb) or "hidden")
    assert main(["ai", "--toggle"]) == 0
    assert asked == ["toggle"] and capsys.readouterr().out.strip() == "hidden"


