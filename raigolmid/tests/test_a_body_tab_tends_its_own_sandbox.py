"""A body tab repairs its own sandbox and reads its own project's disk and memory — never
another's, and the machine tab, with no project, is told so."""
from __future__ import annotations

import pytest

from raigolmid.channel import Channels
from raigolmid.questions import Questions
from raigolmid.scopes import build_tab_methods
from raigolmid.session import SessionError
from tests.harness import Harness


@pytest.fixture()
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.session.select("body", "myapi")
    return harness


def _tab(h, tab_id, memory=None):
    return build_tab_methods(h.session, Questions(h.events, h.paths),
                             Channels(h.session, h.events), tab_id, memory)


def test_a_body_tab_repairs_its_own_sandbox(h):
    sandbox = h.open_sandbox("myapi", "python-dev")
    assert _tab(h, h.tab("myapi"))["repair"]()["instance"] == sandbox
    [asked] = h.events_of("instance.repair_requested")
    assert asked.data.get("instance", asked.instance) == sandbox


def test_a_body_tab_reads_its_own_project_and_the_machine_tab_has_none(h):
    tab = h.tab("myapi")
    root = h.session.working_copy(tab)
    (root / "big.bin").write_bytes(b"x" * 4096)
    read = _tab(h, tab)["disk"]()
    assert read["bytes"] >= 4096 and "budget" in read and read["machine_free"] > 0
    assert all(str(root) not in u for u in read["unread"])

    asked = []
    memory = lambda body=None: asked.append(body) or {"peak": 1}       # noqa: E731
    assert _tab(h, tab, memory)["memory"]() == {"peak": 1} and asked == ["myapi"]

    machine = _tab(h, h.tab(None), memory)
    with pytest.raises(SessionError, match="has none"):
        machine["disk"]()
    with pytest.raises(SessionError, match="has none"):
        machine["memory"]()
