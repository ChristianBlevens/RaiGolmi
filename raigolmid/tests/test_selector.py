"""The selector's view-model, driven without a screen.

These run against the real `Session` behind a real view-model, so what they assert about
greying and reasons is what raigolmid actually says — the selector never computes
compatibility itself.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from raigolmid.channel import Channels  # noqa: E402
from ui.viewmodel import SelectorModel  # noqa: E402

from tests.harness import Harness, greying  # noqa: E402


@pytest.fixture()
def call(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    from raigolmid.api import build_methods
    from raigolmid.history import History
    from raigolmid.questions import Questions
    from raigolmid.viewing import Viewing
    questions = Questions(h.events, h.paths)
    methods = build_methods(h.session, h.events, questions, Channels(h.session, h.events), Viewing(h.events, h.paths.viewing), History(h.events, h.paths))

    def dispatch(method: str, **params):
        return methods[method](**params)

    dispatch.harness = h            # type: ignore[attr-defined]
    dispatch.questions = questions  # type: ignore[attr-defined]
    return dispatch


def test_an_incompatible_item_is_present_and_marked_unavailable(call):
    model = SelectorModel(call=greying(call, "faces", "backend-focus", "wants lsp")).refresh()
    face = next(i for i in model.rows["faces"].items if i.id == "backend-focus")
    assert face.selectable is False
    assert "lsp" in face.detail
    assert "unavailable" in face.detail
    assert model.toggle("faces", "backend-focus") == "wants lsp"
    assert call.harness.session.intent.selection.face is None

