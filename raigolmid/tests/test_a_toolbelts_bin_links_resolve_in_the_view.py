"""`/.toolbelt/bin` is the toolbelt image's `/bin`, and what its entries link into
relatively sits beside it — Nixery's `/bin/go -> ../share/go/bin/go`."""
from __future__ import annotations

import ast
import os

from .viewinit_ast import tree as parse_viewinit


def _bin_siblings():
    tree = parse_viewinit()
    function, = (node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name == "bin_siblings")
    namespace = {"os": os}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "viewinit.py", "exec"),
         namespace)
    return namespace["bin_siblings"]


def test_the_directories_bin_links_into_are_found_from_the_links(tmp_path):
    for directory in ("bin", "share/go/bin", "libexec/x", "lib", "etc"):
        (tmp_path / directory).mkdir(parents=True)
    (tmp_path / "bin" / "go").symlink_to("../share/go/bin/go")
    (tmp_path / "bin" / "helper").symlink_to("../libexec/x/helper")
    (tmp_path / "bin" / "bash").symlink_to("/nix/store/abc-bash/bin/bash")
    (tmp_path / "bin" / "self").symlink_to("../bin/bash")
    (tmp_path / "bin" / "dangling").symlink_to("../missing/tool")
    (tmp_path / "bin" / "plain").write_text("")

    assert _bin_siblings()(str(tmp_path)) == ["libexec", "share"]


def test_both_roots_place_them_beside_bin():
    source = ast.unparse(parse_viewinit())
    assert 'for name in bin_siblings()' in source
    assert "for name in ('bin', *bin_siblings())" in source
