"""The layers from a tab: the machine tab lists, downloads, installs and deletes them as the
user's catalog does, and is told how its install ended; no other tab may, and no tab uploads."""
from __future__ import annotations

import pytest

from raigolmid.catalog import QUEUE, CatalogError
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


def _tab(h, tab_id):
    return build_tab_methods(h.session, Questions(h.events, h.paths),
                             Channels(h.session, h.events), tab_id)


def test_only_the_machine_tab_works_the_layers_and_no_tab_uploads(h):
    body = _tab(h, h.tab("myapi"))
    with pytest.raises(SessionError, match="only the machine tab lists the layers"):
        body["layers"]()
    with pytest.raises(SessionError, match="only the machine tab deletes a layer"):
        body["layer_delete"]("body", "webui")
    machine = _tab(h, h.tab(None))
    assert not [name for name in {**body, **machine} if "upload" in name]
    entries = machine["layers"]()["entries"]
    assert entries and all("thumbnail" not in e for e in entries)


def test_the_machine_tab_is_told_how_its_install_ended(h):
    machine_tab = h.tab(None)
    _tab(h, machine_tab)["layer_install"]("body", "webui")
    h.session.queues.run(QUEUE, lambda: None, "settle", timeout=25)
    [installed] = h.events_of("catalog.installed")
    assert installed.tab == machine_tab
    assert installed.data["deliver"]["content"] == "body 'webui' is installed."


def test_a_body_whose_tab_is_open_is_in_use(h):
    h.session.select("body", "webui")
    tab = h.tab("webui")
    h.session.select("body", "myapi")
    with pytest.raises(CatalogError, match=f"its tab {tab} is open"):
        _tab(h, h.tab(None))["layer_delete"]("body", "webui")
