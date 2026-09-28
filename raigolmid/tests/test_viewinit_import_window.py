"""The view entrypoint may not import anything after it enters the body's mount namespace.

Between the `setns` and the `pivot_root`, the toolbelt closure is not reachable by any path,
and the interpreter's own standard library lives inside it. An import in that window
raises ModuleNotFoundError from inside a half-built root — a view that dies after mounting the
body's filesystem and before releasing it, which is the shape that leaves a container Docker
then refuses to remove.

The window is unreachable by any test that can run without Docker, and a review will not catch
a lazy import added inside a helper three calls down. Reading the file is what catches it, so
the reading is done here.
"""
from __future__ import annotations

import ast

import pytest

from .viewinit_ast import VIEWINIT, call_line as line_of, tree as parse_viewinit


@pytest.fixture(scope="module")
def tree() -> ast.Module:
    return parse_viewinit()


def test_nothing_is_imported_after_entering_the_bodys_namespace(tree: ast.Module) -> None:
    setns = line_of(tree, "libc.setns")
    late = [node for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom)) and node.lineno > setns]
    assert not late, (
        "imported after the setns at line "
        f"{setns}: " + ", ".join(f"line {n.lineno}: {ast.unparse(n)}" for n in late)
        + " — the standard library is unreachable until the pivot_root"
    )


def test_every_import_is_at_module_level(tree: ast.Module) -> None:
    """A deferred import inside a helper is an import wherever that helper is called."""
    nested = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nested += [f"{node.name}() line {child.lineno}: {ast.unparse(child)}"
                       for child in ast.walk(node)
                       if isinstance(child, (ast.Import, ast.ImportFrom))]
    assert not nested, "imports inside functions: " + ", ".join(nested)


def test_the_entrypoint_owns_the_socket_while_it_still_can(tree: ast.Module) -> None:
    """The privileged half of the pair whose other half is test_the_launcher_never_calls_chown.

    Binding in a directory that belongs to the user raigolmid runs as needs CAP_DAC_OVERRIDE,
    and giving the socket to that user needs CAP_CHOWN. Both exist here and neither exists in
    the launcher, so if this stops happening here it cannot start happening there: the view
    comes up with a socket raigolmid cannot reach, and reports itself healthy on it.
    """
    # Only the two operations that need capabilities are named. How the descriptor then
    # reaches the launcher is covered by running the real hand-over in
    # test_launcher_fd_handover.py, and naming that mechanism here would only pin the
    # implementation (a dup2 or a detach).
    source = VIEWINIT.read_text(encoding="utf-8")
    for needed in ("os.chown", "listener.bind"):
        assert needed in source, (
            f"{needed} is gone from the entrypoint — the launcher cannot do it instead"
        )
    setns = line_of(tree, "libc.setns")
    chown = line_of(tree, "os.chown")
    assert chown > setns, "the socket is bound before the body's namespace is entered"


def test_the_window_is_closed_by_a_pivot(tree: ast.Module) -> None:
    """If the pivot ever stops happening, the import rule above protects nothing."""
    assert line_of(tree, "SYS_pivot_root") > line_of(tree, "libc.setns")
