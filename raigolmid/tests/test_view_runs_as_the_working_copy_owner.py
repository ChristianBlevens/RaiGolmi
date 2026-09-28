"""The view's processes must become the working copy's owner, and must do it last.

`/work` is the user's own repository, owned by the user raigolmid runs as. Root does not
bypass file modes — that is CAP_DAC_OVERRIDE, dropped — so a launcher left as
uid 0 can read `/work` and write none of it: no file written, no commit.

This is asserted by reading rather than by running. The suite here runs as root, so the
socket directory it would read the identity from is root-owned and the switch is a no-op —
a live test would pass without exercising anything. The order
is the part that cannot be got wrong: the directories are handed over while capabilities
still exist, and the identity changes last.

Read as a **tree**, not as text: a text search is satisfied by a commented-out line, and a
view whose setuid is commented out runs as root while the test stays green.
"""
from __future__ import annotations

import ast

import pytest

from .viewinit_ast import call_line, stmt_line, string_constants, tree as parse_viewinit


@pytest.fixture(scope="module")
def tree() -> ast.Module:
    return parse_viewinit()


def test_the_identity_comes_from_the_socket_directorys_owner(tree: ast.Module) -> None:
    """One source for the identity. The socket handover already trusts this exact fact;
    a second source is how two answers to one question drift apart."""
    stmt_line(tree, "os.stat(SOCK_DIR)")
    call_line(tree, "os.setuid(owner.st_uid)")
    call_line(tree, "os.setgid(owner.st_gid)")


def test_supplementary_groups_are_dropped_before_the_uid_changes(tree: ast.Module) -> None:
    """setgroups needs the privilege it is about to give up, so it goes first."""
    setuid = call_line(tree, "os.setuid(owner.st_uid)")
    assert (call_line(tree, "os.setgroups([])")
            < call_line(tree, "os.setgid(owner.st_gid)")
            < setuid)
    assert setuid < call_line(tree, "os.execv(capsh")


def test_the_view_private_directory_is_handed_over_while_still_root(tree: ast.Module) -> None:
    """chown needs CAP_CHOWN. After the setuid there is none, and HOME lives in that
    directory."""
    assert (call_line(tree, "os.chown(directory, owner.st_uid, owner.st_gid)")
            < call_line(tree, "os.setuid(owner.st_uid)"))


def test_home_is_not_the_unwritable_root_home(tree: ast.Module) -> None:
    """A HOME the user cannot write is a shell and a language server each failing at
    their first state file."""
    assert "/root" not in string_constants(tree), \
        "a literal /root reached the view's environment"
    stmt_line(tree, 'os.environ[\'HOME\'] = VIEW_HOME')


def test_the_socket_directory_leaves_the_view_before_the_uid_changes(tree: ast.Module) -> None:
    """Everything after the setuid runs as the socket directory's owner. Still mounted, it
    would let any view replace another's socket, rewrite `focused`, or connect to another
    view's launcher. The detach needs SYS_ADMIN, so it comes before the setuid, and after
    the socket is bound."""
    detach = call_line(tree, "libc.umount2(SOCK_DIR.encode(), MNT_DETACH)")
    assert call_line(tree, "bind_for_the_launcher(sock)") < detach
    assert detach < call_line(tree, "os.setuid(owner.st_uid)")


def test_one_launcher_and_no_capability_kept_for_it(tree: ast.Module) -> None:
    """A debugger ptraces the body as its own uid, which needs no capability, so
    nothing in the view is started holding one."""
    assert not any("cap_sys_ptrace" in c for c in string_constants(tree))
