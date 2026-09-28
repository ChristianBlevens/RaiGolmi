"""The menu's shapes: a notice shown in full folds into the handle, hover opens the menu, and
neither folds under the pointer or while it holds the keyboard (`ui/notify_popup/model.py`).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dataclasses import asdict  # noqa: E402

from raigolmid.history import Entry  # noqa: E402
from ui.notify_popup.model import (ARRIVAL, ARRIVAL_SECONDS, HANDLE, MENU,  # noqa: E402
                                   MenuModel)


def entry(id: str, notice: bool = False, over: str | None = None, new: bool = True) -> dict:
    """An entry as the daemon's `menu` gives it."""
    return {**asdict(Entry(id=id, at=0.0, kind="tab.opened", tab="tab-1", text="t",
                           notice=notice, over=over)), "new": new}


def notice(id: str, over: str | None = None, new: bool = True) -> dict:
    return entry(id, notice=True, over=over, new=new)


def test_anything_new_lights_the_handle_until_the_menu_is_opened():
    """Lit by what is new, dark once the user has opened the menu."""
    m = MenuModel()
    m.take([entry("h2"), entry("h10")], now=0)
    assert m.lit and m.shape == HANDLE and m.unseen_through() is None
    m.enter()
    assert m.unseen_through() == "h10", "the newest by number, not by text"
    m.take([entry("h2", new=False), entry("h10", new=False)], now=1)
    assert not m.lit and m.unseen_through() is None


def test_only_a_standing_notice_arrives():
    """Anything new lights the handle; a failure that is the user's is also shown in full."""
    m = MenuModel()
    m.take([entry("h1"), notice("h2", over="manager.opened"), notice("h3")], now=0)
    assert m.shape == ARRIVAL and m.arrivals == ["h3"]


def test_a_seen_notice_does_not_arrive_again_when_the_surface_restarts():
    m = MenuModel()
    m.take([notice("h1", new=False), notice("h2")], now=0)
    assert m.shape == ARRIVAL and m.arrivals == ["h2"]


def test_a_notice_arrives_once():
    m = MenuModel()
    m.take([notice("h1")], now=0)
    m.tick(ARRIVAL_SECONDS)
    m.take([notice("h1"), entry("h2")], now=20)
    assert m.shape == HANDLE


def test_an_arrival_does_not_fold_under_the_pointer_or_while_holding_the_keyboard():
    m = MenuModel()
    m.take([notice("h1")], now=0)
    m.enter()
    m.tick(100)
    assert m.shape == ARRIVAL
    m.hold()
    m.leave(now=100)
    m.tick(200)
    assert m.shape == ARRIVAL
    m.release()
    m.tick(200 + ARRIVAL_SECONDS)
    assert m.shape == HANDLE


def test_an_arrival_whose_failure_is_over_folds_at_once():
    m = MenuModel()
    m.take([notice("h1")], now=0)
    m.take([notice("h1", over="manager.opened")], now=1)
    assert m.shape == HANDLE


def test_hover_opens_the_menu_with_everything_and_leaving_folds_it():
    m = MenuModel()
    history = [entry("h1"), notice("h2", over="manager.opened")]
    m.take(history, now=0)
    m.enter()
    assert m.shape == MENU and m.shown() == history[::-1], "newest first"
    m.leave(now=1)
    assert m.shape == HANDLE


def test_the_menu_stays_while_it_holds_the_keyboard_and_folds_when_given_back():
    m = MenuModel()
    m.take([entry("h1"), entry("h2")], now=0)
    m.enter()
    m.hold()
    m.leave(now=10)
    assert m.shape == MENU
    m.release()
    assert m.shape == HANDLE


def test_a_notice_arriving_while_the_menu_is_open_is_drawn_there_not_again_later():
    m = MenuModel()
    m.take([], now=0)
    m.enter()
    m.take([notice("h1")], now=1)
    assert m.shape == MENU and m.shown()[0]["id"] == "h1"
    m.leave(now=2)
    m.take([notice("h1")], now=3)
    assert m.shape == HANDLE


def test_the_menus_own_failure_arrives_and_stays_lit_until_the_open_menu_shows_it():
    """A failure said only inside the folded panel is said to nobody."""
    m = MenuModel()
    m.take([entry("h1", new=False)], now=0)
    m.fail("choose failed: refused", now=1, reading=False)
    assert (m.shape, m.failure) == (ARRIVAL, "choose failed: refused")
    m.take([entry("h1", new=False)], now=2)
    assert m.shape == ARRIVAL, "an arrival with a standing failure folded at the next read"
    m.tick(1 + ARRIVAL_SECONDS)
    assert (m.shape, m.lit) == (HANDLE, True), "folded, and nothing says it failed"
    m.enter()
    assert m.unseen_through() is None, "a failure is not an entry to mark seen"
    m.leave(now=20)
    assert (m.failure, m.lit) == ("", False)


def test_a_failure_to_read_arrives_once_and_is_taken_back_only_when_the_history_reads():
    """The surface retries every few seconds while the daemon is away; each retry says the
    same thing, and a menu that arrived on every one would never fold."""
    m = MenuModel()
    said = "could not read the history: refused"
    m.fail(said, now=0, reading=True)
    m.fail(said, now=ARRIVAL_SECONDS - 1, reading=True)
    m.tick(ARRIVAL_SECONDS)
    assert (m.shape, m.lit) == (HANDLE, True), "a repeat of the same failure arrived again"
    m.enter()
    m.leave(now=20)
    m.fail(said, now=21, reading=True)
    assert (m.shape, m.failure) == (HANDLE, said), "seen, yet still true, and not said again"
    m.take([], now=22)
    assert (m.failure, m.shape, m.lit) == ("", HANDLE, False)
