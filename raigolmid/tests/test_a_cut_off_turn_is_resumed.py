"""A turn an API error cut off is resumed without the user (`limits.py`): the account's usage
limit at the reset the proxy read, or earlier when a probe or any answer finds the API serving
again; a transient error after a wait; and only the tabs nobody sits in front of."""
from __future__ import annotations

import time

import pytest

from raigolmid import activity, boot, naming
from raigolmid.limits import (GRACE_SECONDS, PROBE_SECONDS, RETRY_CAP_SECONDS, RETRY_SECONDS,
                              Limits, plan_view)
from raigolmid.session import Session
from tests.test_the_machine_tab_coordinates import BODY, MACHINE, Machine, h  # noqa: F401


class Resumer(Machine):
    def __init__(self, h) -> None:
        self.limits = Limits(h.session, h.events)
        super().__init__(h)

    def pump(self) -> None:
        for event in self.limits._sub.drain(timeout=0.05):
            self.limits.on_event(event)
        super().pump()

    def cut(self, tab: str, error: str) -> None:
        self.tab(tab)["agent_activity"](busy=True)
        self.tab(tab)["agent_activity"](busy=False, error=error)
        self.pump()

    def contents(self, tab: str) -> list[str]:
        return [m["content"] for m in self.queued(tab)]


@pytest.fixture()
def m(h):  # noqa: F811
    m = Resumer(h)
    m.tab(MACHINE)["manage"](tab=BODY)
    return m


def test_the_usage_limit_holds_every_push_and_resumes_the_cut_tabs_at_its_reset(m):
    m.cut(BODY, "rate_limit")
    m.cut(MACHINE, "rate_limit")
    resets = time.time() + 100
    m.h.events.emit("credproxy.limited", owner=BODY, path="/v1/messages",
                    resets_at=resets, said=str(resets))
    m.pump()
    # Cut off by the limit, the body tab is the daemon's to resume, not the machine tab's.
    assert m.contents(MACHINE) == [] and m.contents(BODY) == []
    m.h.events.emit("coordinator.directed", tab=BODY, deliver={"content": "go on",
                                                                "meta": {"from": "machine"}})
    assert m.channels.take(BODY) is None
    m.limits.tick(resets + GRACE_SECONDS - 1)
    assert m.contents(MACHINE) == []
    m.limits.tick(resets + GRACE_SECONDS)
    for tab in (MACHINE, BODY):
        assert "usage limit" in m.contents(tab)[-1]
    assert m.channels.take(BODY) is not None


def test_a_timed_limit_ends_early_when_the_plan_answers_allowed(m):
    """An upgraded plan lifts the limit before its reset: the hold is probed regardless, and an
    answer reading `allowed` resumes every cut tab."""
    m.cut(BODY, "rate_limit")
    now = time.time()
    resets = now + 40 * 3600
    m.h.events.emit("credproxy.limited", owner=BODY, path="/v1/messages",
                    resets_at=resets, said=str(resets))
    m.pump()
    m.limits.tick(now + PROBE_SECONDS + 1)
    assert [e.tab for e in m.h.events_of("account.probed")] == [BODY]
    assert "usage limit" in m.contents(BODY)[-1]
    m.cut(BODY, "rate_limit")
    m.h.events.emit("credproxy.plan", status="allowed",
                    windows={"7d": {"used": 0.0, "resets_at": resets}})
    m.pump()
    assert len(m.h.events_of("account.resumed")) == 1
    assert m.h.session.status()["plan"]["windows"]["7d"]["used"] == 0.0


def test_the_plan_view_says_where_each_window_is_headed():
    now = 1_000_000.0
    view = plan_view({"status": "allowed", "read_at": now, "windows": {
        "7d": {"used": 0.25, "resets_at": now + 3.5 * 24 * 3600},
        "5h": {"used": 0.5, "resets_at": now - 1}}}, now)
    assert view["windows"]["7d"]["passed"] == 0.5
    assert view["windows"]["7d"]["on_course_for"] == 0.5, "a quarter used by half-way"
    assert view["windows"]["5h"] == {"resets_at": now - 1, "reset_since_read": True}
    assert plan_view(None, now) is None


def test_a_limit_nobody_timed_is_probed_through_one_tab_and_its_answer_resumes_the_rest(m):
    m.cut(BODY, "rate_limit")
    m.cut(MACHINE, "rate_limit")
    now = time.time()
    m.limits.tick(now + PROBE_SECONDS - 5)
    assert m.contents(MACHINE) == []
    m.limits.tick(now + PROBE_SECONDS + 1)
    assert len(m.contents(MACHINE)) == 1 and m.contents(BODY) == []
    # The probe fails again: nothing more until the next one.
    m.cut(MACHINE, "rate_limit")
    m.limits.tick(now + PROBE_SECONDS + 2)
    assert m.contents(BODY) == []
    m.tab(MACHINE)["agent_activity"](busy=True)
    m.tab(MACHINE)["agent_activity"](busy=False)
    m.pump()
    assert "usage limit" in m.contents(BODY)[-1]


def test_a_transient_error_is_resumed_after_a_wait_and_one_that_needs_the_user_is_said(m):
    m.cut(BODY, "overloaded")
    now = time.time()
    m.limits.tick(now + RETRY_SECONDS - 5)
    assert m.contents(BODY) == []
    m.limits.tick(now + RETRY_SECONDS + 1)
    assert "overloaded" in m.contents(BODY)[-1]

    m.cut(BODY, "authentication_failed")
    m.limits.tick(now + 10 * RETRY_SECONDS)
    assert len(m.contents(BODY)) == 1
    assert [e.tab for e in m.h.events_of("agent.turn_failed")] == [BODY]
    assert "will not fix" in m.contents(MACHINE)[-1]


def test_a_tab_the_user_sits_in_front_of_is_theirs_to_resume(m):
    m.tab(MACHINE)["manage"](tab=BODY, on=False)
    m.cut(BODY, "overloaded")
    m.limits.tick(time.time() + 10 * RETRY_SECONDS)
    assert m.contents(BODY) == []


def test_a_daemon_that_starts_finds_the_tabs_cut_off_while_it_was_down(m):
    activity.record(m.h.session.agents.home(BODY), busy=False, failed="rate_limit")
    fresh = Limits(m.h.session, m.h.events)
    fresh._read_cut()
    assert [e.data["resets_at"] for e in m.h.events_of("account.limited")] == [None]
    fresh.tick(time.time() + PROBE_SECONDS + 1)
    assert [e.tab for e in m.h.events_of("account.probed")] == [BODY]


def test_a_transient_failure_that_repeats_waits_twice_as_long_each_time_up_to_a_cap(m):
    m.cut(BODY, "server_error")
    now = time.time()
    m.limits.tick(now + RETRY_SECONDS + 1)
    assert len(m.contents(BODY)) == 1
    m.cut(BODY, "server_error")         # the network is still down
    now = time.time()
    m.limits.tick(now + RETRY_SECONDS + 1)
    assert len(m.contents(BODY)) == 1
    m.limits.tick(now + 2 * RETRY_SECONDS + 1)
    assert len(m.contents(BODY)) == 2
    for _ in range(10):
        m.cut(BODY, "server_error")
    assert m.limits.retry_after(BODY) == RETRY_CAP_SECONDS
    m.tab(BODY)["agent_activity"](busy=True)
    m.tab(BODY)["agent_activity"](busy=False)        # a turn the API answered ends the row
    m.pump()
    m.cut(BODY, "server_error")
    assert m.limits.retry_after(BODY) == RETRY_SECONDS


def test_a_reboot_tells_the_machine_tab_and_continues_the_managed_tab_cut_mid_turn(
        h, monkeypatch):
    h.session.manage(BODY, True)
    for tab in (MACHINE, BODY):
        activity.record(h.session.agents.home(tab), busy=True)
    monkeypatch.setattr(boot, "current", lambda: "boot-1")
    h.session.close()
    for tab in (MACHINE, BODY):
        h.runtime.stop(naming.agent(tab))
    monkeypatch.setattr(boot, "current", lambda: "boot-2")
    h.session = Session(h.runtime, h.paths, h.search, h.events, epoch=2,
                        compose_cli=h.compose)
    m = Resumer(h)          # subscribed before the start reconciles, as the daemon's are
    h.session.reconcile()
    m.pump()
    assert "your session ended under it" in m.contents(BODY)[-1]
    [told] = m.contents(MACHINE)
    assert told.startswith("From the daemon: The machine restarted")
    assert f"Tab {BODY} (myapi) was mid-turn" in told and "Your own turn" in told
    assert [e.tab for e in h.events_of("limits.continued")] == [BODY], \
        "the machine tab's own continuing is in what it is told"
