"""Every text on the host's surfaces can be selected and copied,
including one written later, such as a failure's line: a surface makes a label, a
button, a checkbox or a heading only through `ui/copyable.py`, which is what makes it
selectable and keeps a click a click."""
from __future__ import annotations

import ast
from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"
TEXT_WIDGETS = {"Label", "Button", "CheckButton", "ToggleButton", "Expander", "LinkButton",
                "MenuButton"}


def test_no_surface_makes_a_text_widget_but_through_copyable():
    made = []
    for source in sorted(UI.rglob("*.py")):
        if source.name == "copyable.py":
            continue
        for node in ast.walk(ast.parse(source.read_text(), str(source))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in TEXT_WIDGETS
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "Gtk"):
                made.append(f"{source.relative_to(UI.parent)}:{node.lineno} Gtk.{node.func.attr}")
    assert not made, "made outside ui/copyable.py, so not copyable: " + ", ".join(made)
