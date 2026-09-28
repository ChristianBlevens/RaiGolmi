"""The native selector's keyboard, without a screen.

The selector is a thin layer over the view-model, so the only thing it owns beyond it is
where the cursor is. That part is arithmetic over the rows raigolmid returned, so it is tested here against a real
view-model rather than against a GTK window.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from raigolmid.channel import Channels                # noqa: E402
from ui.selector_native.cursor import Cursor    # noqa: E402
from ui.viewmodel import ROWS, SelectorModel    # noqa: E402

from tests.harness import Harness, greying      # noqa: E402


@pytest.fixture()
def model(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    from raigolmid.api import build_methods
    from raigolmid.history import History
    from raigolmid.questions import Questions
    from raigolmid.viewing import Viewing
    methods = build_methods(h.session, h.events, Questions(h.events, h.paths),
                  Channels(h.session, h.events), Viewing(h.events, h.paths.viewing), History(h.events, h.paths))

    def dispatch(method: str, **params):
        return methods[method](**params)

    return SelectorModel(call=dispatch).refresh()


def test_the_cursor_lands_on_unavailable_items(model):
    """They are greyed out rather than hidden, so skipping one would hide the reason
    the user is looking for. Every item in a row must be reachable."""
    cursor = Cursor()
    model.call = greying(model.call, "faces", "backend-focus", "wants lsp")
    model.refresh()
    row = next(name for name in ROWS
               if any(not i.selectable for i in model.rows[name].items))
    cursor.row = ROWS.index(row)
    reached = set()
    for _ in range(len(model.rows[row].items)):
        reached.add(cursor.focused(model.rows).id)
        cursor.next_item(model.rows)
    assert reached == {item.id for item in model.rows[row].items}
    assert any(not i.selectable for i in model.rows[row].items), "no unavailable item here"


def test_the_selector_calls_the_daemon_only_from_its_workers():
    """Everything else in `Selector` runs on the GTK thread, and a daemon call there
    freezes the selector — no repaint, no key, no Escape — for as long as the daemon takes,
    which an editor blocked on input made five seconds on every reload. Asserted statically:
    without a screen, the thread a method runs on cannot be observed here."""
    import ast
    source = (ROOT / "ui" / "selector_native" / "selector.py").read_text()
    workers = {"_fetch", "_call"}
    offenders = []
    for cls in ast.walk(ast.parse(source)):
        if not (isinstance(cls, ast.ClassDef) and cls.name == "Selector"):
            continue
        for fn in cls.body:
            if not isinstance(fn, ast.FunctionDef) or fn.name in workers:
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Attribute) and node.attr in ("fetch", "refresh", "call")
                        and isinstance(node.value, ast.Attribute) and node.value.attr == "model"):
                    offenders.append(f"{fn.name}: self.model.{node.attr}")
    assert offenders == [], offenders
