"""A RaiGolmi bug the machine tab meets goes to the keeper outside the VM (`faults.py`): it is
filed with `report_fault`, the keeper sees it at its next look, takes it and answers it, and the
answer reaches the machine tab, or the one that took its work over, naming the tabs to resume."""
from __future__ import annotations

import time

import pytest

from raigolmid import faults
from raigolmid.api import build_methods
from raigolmid.history import History
from raigolmid.session import SessionError
from raigolmid.viewing import Viewing
from tests.test_the_machine_tab_coordinates import BODY, MACHINE, Machine, h  # noqa: F401


def keeper(h, m: Machine) -> dict:
    """The host API's table, which `rai keeper` calls over ssh."""
    return build_methods(h.session, h.events, m.questions, m.channels,
                         Viewing(h.events, h.paths.viewing), History(h.events, h.paths))


def told_of(m: Machine, tab: str, fault: str) -> list[str]:
    return [q["content"] for q in m.queued(tab) if fault in q["content"]]


def test_a_fault_is_filed_and_says_whether_a_keeper_is_reachable(h):
    m = Machine(h)
    with pytest.raises(SessionError, match="only the machine tab"):
        m.tab(BODY)["report_fault"](title="x", body="y")
    with pytest.raises(SessionError, match="not a tab you manage"):
        m.tab(MACHINE)["report_fault"](title="x", body="y", affects=[BODY])
    m.tab(MACHINE)["manage"](tab=BODY)
    first = m.tab(MACHINE)["report_fault"](title="jobs leak", body="job_stop: no such job",
                                           affects=[BODY])
    assert first["keeper"] == "unreachable" and "/transfer/out" in first["next"]
    assert "ever looked" in first["next"]
    api = keeper(h, m)
    [listed] = api["keeper_seen"]()["faults"]
    assert (listed["id"], listed["status"], listed["affects"]) == (first["fault"], "open", [BODY])
    second = m.tab(MACHINE)["report_fault"](title="notices replayed", body="twice")
    assert second["keeper"] == "reachable"
    assert [e.data["id"] for e in h.events_of("fault.reported")] == [first["fault"],
                                                                     second["fault"]]


def test_a_keeper_gone_longer_than_its_looks_is_unreachable_unless_in_a_session(h):
    store = faults.Faults(h.paths, h.events)
    store.seen()
    now = time.time()
    assert store.keeper_reachable(now)[0]
    assert not store.keeper_reachable(now + faults.SEEN_SECONDS + 1)[0]
    store.seen(busy_seconds=4 * 3600)
    assert store.keeper_reachable(now + 3 * 3600)[0]


def test_the_keepers_answer_reaches_the_machine_tab_naming_the_tabs_to_resume(h):
    m = Machine(h)
    m.tab(MACHINE)["manage"](tab=BODY)
    m.tab(MACHINE)["pause"](tab=BODY)
    id = m.tab(MACHINE)["report_fault"](title="jobs leak", body="job_stop: no such job",
                                        affects=[BODY])["fault"]
    api = keeper(h, m)
    assert api["keeper_take"](id=id, busy_seconds=3600)["status"] == "taken"
    assert [f["id"] for f in api["keeper_seen"]()["faults"]] == [id], "taken is still the keeper's"
    assert told_of(m, MACHINE, id) == []
    with pytest.raises(SessionError, match="says in words"):
        api["keeper_answer"](id=id, verdict="not_fixed")
    api["keeper_answer"](id=id, verdict="fixed")
    [told] = told_of(m, MACHINE, id)
    assert "fix is in" in told and "`resume`" in told and BODY in told
    assert api["keeper_seen"]()["faults"] == []
    assert api["keeper_answer"](id=id, verdict="fixed")["status"] == "fixed", \
        "an answer resent is the fault as answered"
    with pytest.raises(SessionError, match="answered already"):
        api["keeper_answer"](id=id, verdict="not_fixed", words="no")
    m.pump()
    assert len(told_of(m, MACHINE, id)) == 1


def test_an_answer_after_a_machine_tab_handover_reaches_its_successor(h):
    m = Machine(h)
    m.tab(MACHINE)["manage"](tab=BODY)
    id = m.tab(MACHINE)["report_fault"](title="idle notices", body="replayed",
                                        affects=[BODY])["fault"]
    m.tab(MACHINE)["agent_activity"](busy=True)
    m.tab(MACHINE)["end_conversation"](report="stretch one")
    m.tab(MACHINE)["agent_activity"](busy=False)
    m.pump()
    successor = h.session.intent.machine_tab().tab_id
    assert successor != MACHINE
    keeper(h, m)["keeper_answer"](id=id, verdict="not_fixed", words="the channel loses seq")
    [told] = told_of(m, successor, id)
    assert "could not fix" in told and "the channel loses seq" in told and "/transfer/out" in told
    assert faults.Faults(h.paths, h.events).get(id)["told"] == successor


def test_a_fault_left_waiting_by_a_keeper_gone_is_said_once_with_the_fallback(h):
    m = Machine(h)
    m.tab(MACHINE)["manage"](tab=BODY)
    m.tab(MACHINE)["pause"](tab=BODY)
    faults.Faults(h.paths, h.events).seen()
    id = m.tab(MACHINE)["report_fault"](title="jobs leak", body="job_stop: no such job",
                                        affects=[BODY])["fault"]
    m.coordinator.tick(time.time())
    assert told_of(m, MACHINE, id) == [], "a reachable keeper has it"
    gone = time.time() + faults.SEEN_SECONDS + 1
    m.coordinator.tick(gone)
    [told] = told_of(m, MACHINE, id)
    assert "no keeper is reachable" in told and "/transfer/out" in told and BODY in told
    m.coordinator.tick(gone + 60)
    assert len(told_of(m, MACHINE, id)) == 1, "said once"
