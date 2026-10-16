"""A body whose definition goes while its tab is open: the tab is told, and the body stays
listed with its sandbox while either lasts, so what still runs on it can be seen and stopped."""
import shutil

import pytest

from raigolmid.channel import Channels
from raigolmid.questions import Questions
from raigolmid.scopes import tab_status
from tests.harness import Harness


@pytest.fixture()
def h(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


def test_the_tab_is_told_and_its_sandbox_stays_listed(h):
    sandbox = h.open_sandbox("myapi")
    tab = h.tab("myapi")

    shutil.rmtree(h.search.bodies[0] / "myapi")
    h.session.rediscover()

    [told] = h.events_of("body.undefined")
    assert told.tab == tab and told.data["sandboxes"] == [sandbox]
    assert sandbox in told.data["deliver"]["content"]
    rows = {b["id"]: b for b in h.session.list_items("body")["bodies"]}
    assert rows["myapi"]["selectable"] is False and rows["myapi"]["tab"] == tab
    assert [i["instance"] for i in rows["myapi"]["instances"]] == [sandbox]

    h.session.rediscover()
    assert len(h.events_of("body.undefined")) == 1, "told once, when the body went"


def test_a_body_gone_with_nothing_on_it_leaves_the_listing(h):
    shutil.rmtree(h.search.bodies[0] / "webui")
    h.session.rediscover()
    assert not h.events_of("body.undefined")
    assert "webui" not in {b["id"] for b in h.session.list_items("body")["bodies"]}


def test_a_tab_on_a_body_gone_does_not_fail_another_tabs_turn_end(h):
    h.open_sandbox("myapi")
    orphan = h.tab("myapi")
    other = h.tab(None)
    shutil.rmtree(h.search.bodies[0] / "myapi")
    h.session.rediscover()

    for tab in (other, orphan):
        h.session.agent_activity(tab, busy=False)
    assert h.session.session_start(orphan) is None
    h.session.queues.close_all()


def test_a_tab_on_a_body_gone_reads_its_own_status(h):
    sandbox = h.open_sandbox("myapi")
    orphan = h.tab("myapi")
    shutil.rmtree(h.search.bodies[0] / "myapi")
    h.session.rediscover()

    status = tab_status(h.session, Questions(h.events, h.paths), Channels(h.session, h.events),
                        orphan)
    assert status["instance"] == sandbox and status["builds"] == {}
    assert "'myapi' is no longer defined" in status["body_note"]
    assert "toolbelts" not in status
