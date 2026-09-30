"""A turn an API error cut off is resumed without the user (`limits.py`): the account's usage
limit at the reset the proxy read, or by a probe when nobody said it; a transient error after a
wait; and only the tabs nobody sits in front of."""
from __future__ import annotations

import time

import pytest

from raigolmid import activity
from raigolmid.limits import GRACE_SECONDS, PROBE_SECONDS, RETRY_SECONDS, Limits
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
