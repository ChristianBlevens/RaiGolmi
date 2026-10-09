"""The user's catalog from a tab: every tab reads it by id, a run's reports included, and only the
machine tab changes it, through the same checks as the user's own save."""
from __future__ import annotations

import pytest

from raigolmid.channel import Channels
from raigolmid.docwrite import DocumentError
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


def test_a_body_tab_reads_a_runs_report_by_id_and_cannot_change_the_catalog(h):
    h.paths.runs.mkdir(parents=True)
    (h.paths.runs / "20261009T000000-progress-01.md").write_text("# stretch 1\n\nport done\n")
    body = _tab(h, h.tab("myapi"))

    listed = {d["id"]: d for d in body["documents"]()}
    assert listed["run/20261009T000000-progress-01.md"]["group"] == "Runs"
    read = body["document"]("run/20261009T000000-progress-01.md")
    assert "port done" in read["text"] and "path" not in read

    preferences = body["document"]("preferences")
    with pytest.raises(SessionError, match="machine tab's to change"):
        body["document_save"]("preferences", "mine\n", preferences["version"])


def test_the_machine_tab_saves_through_the_users_checks(h):
    machine = _tab(h, h.tab(None))
    preferences = machine["document"]("preferences")
    saved = machine["document_save"]("preferences", "short answers\n", preferences["version"])
    assert saved["text"] == "short answers\n" and h.paths.preferences.read_text() == "short answers\n"

    settings = machine["document"]("settings")
    with pytest.raises(DocumentError):
        machine["document_save"]("settings", "[agents]\nmodel = 'two words'\n",
                                 settings["version"])
    with pytest.raises(DocumentError, match="never edited"):
        machine["document_save"]("index", "x", None)
