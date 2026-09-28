"""Every document an agent reads, opened and saved through the daemon as the catalog does
(`raigolmid/library.py`, `docwrite.py`): a save over a
newer write is refused and the newer text kept, what is shown is never saved, and a document
whose reader has rules is refused naming what it cannot read, before anything is written."""
from __future__ import annotations

import pytest

from raigolmid.api import build_methods
from raigolmid.channel import Channels
from raigolmid.docwrite import DocumentError, StaleDocument
from raigolmid.history import History
from raigolmid.questions import Questions
from raigolmid.viewing import Viewing

from tests import settingsdoc
from tests.harness import Harness


@pytest.fixture()
def world(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    methods = build_methods(h.session, h.events, Questions(h.events, h.paths),
                            Channels(h.session, h.events), Viewing(h.events, h.paths.viewing),
                            History(h.events, h.paths))
    return h, methods


def test_a_save_over_a_newer_write_is_refused_and_the_newer_text_kept(world):
    h, m = world
    opened = m["document"](id="preferences")
    assert (opened["exists"], opened["version"]) == (False, None)
    saved = m["document_save"](id="preferences", text="- Terse.\n", version=None)
    h.paths.preferences.write_text("- Terse.\n- Written by the judge meanwhile.\n")
    with pytest.raises(StaleDocument):
        m["document_save"](id="preferences", text="- Mine.\n", version=saved["version"])
    assert "meanwhile" in m["document"](id="preferences")["text"]


def test_a_thought_is_shown_and_never_saved(world):
    h, m = world
    thoughts = h.paths.agent_archive / "tab-1-20260927T120000" / "thoughts.md"
    thoughts.parent.mkdir(parents=True)
    thoughts.write_text("what it was thinking\n")
    doc = next(d for d in m["documents"]() if d["id"] == "thought/archive/tab-1-20260927T120000")
    assert (doc["group"], doc["editable"]) == ("Thoughts", False)
    with pytest.raises(DocumentError, match="never edited"):
        m["document_save"](id=doc["id"], text="rewritten", version=None)
    assert thoughts.read_text() == "what it was thinking\n"


@pytest.mark.parametrize("id, text, names", [
    ("settings", settingsdoc.text(questions={"lapse_minutes": -1}), "lapse_minutes"),
    ("permissions", "- yes toolbelt_swap everywhere\n- maybe\n", "line 2"),
    ("primer/claude.md", "Hello {nobody}\n", "nobody"),
])
def test_a_document_its_reader_cannot_read_is_refused_naming_why(world, id, text, names):
    h, m = world
    opened = m["document"](id=id)
    with pytest.raises(DocumentError, match=names):
        m["document_save"](id=id, text=text, version=opened["version"])
    assert m["document"](id=id) == opened, "nothing is written"
