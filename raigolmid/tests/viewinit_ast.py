"""Reading `viewinit.py` as a tree, for the tests that assert on what it does.

Not a test module. Two of them need the same reading and a second copy of it is how they
come to disagree about what the file says.

Why a tree and not the text: `viewinit.py`'s invariants are about **order** — what runs
before the namespace is entered, what runs before the uid changes — and the window they
describe cannot be reached by any test that runs without Docker (the suite runs
as root, so the identity switch is a no-op here). Reading is the instrument. But reading
the *text* accepts a commented-out line, a line inside a docstring, and a line in a branch
that never runs, all of which satisfy `"os.setuid(...)" in source` while the view stays
root. A parsed tree has no node for any of them.
"""
from __future__ import annotations

import ast
from pathlib import Path

VIEWINIT = (Path(__file__).resolve().parents[1]
            / "raigolmid" / "launcher" / "view" / "viewinit.py")


def tree() -> ast.Module:
    return ast.parse(VIEWINIT.read_text(encoding="utf-8"))


def call_line(tree: ast.Module, needle: str) -> int:
    """Line of the first *call* whose source contains `needle`. Raises if there is none."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and needle in ast.unparse(node):
            return node.lineno
    raise AssertionError(f"{needle} is not called in {VIEWINIT.name}")


def stmt_line(tree: ast.Module, needle: str) -> int:
    """Line of the first *statement* whose source contains `needle`.

    For the things that are not calls — an assignment to `os.environ["HOME"]`, say — where
    a text search would also match the comment explaining it.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt) and needle in ast.unparse(node):
            return node.lineno
    raise AssertionError(f"no statement in {VIEWINIT.name} contains {needle!r}")


def string_constants(tree: ast.Module) -> set[str]:
    """Every *whole* string literal the code contains.

    F-string pieces are excluded, and that is the point rather than a convenience: an
    f-string decomposes into `Constant` parts, so `f"/proc/{pid}/root"` contributes a part
    equal to `/root` and a search for that literal would report a path segment as a
    hardcoded home directory.
    """
    inside_fstring = {id(part)
                      for node in ast.walk(tree) if isinstance(node, ast.JoinedStr)
                      for part in ast.walk(node)}
    return {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in inside_fstring}
