"""What is bound over the user's repo so nothing in a container can make the host's git run
code (`git.protected_paths`), on repositories git itself laid out."""
from __future__ import annotations

import subprocess

import pytest
from pathlib import Path

from raigolmid import git


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "protocol.file.allow=always", "-c", "user.name=t",
                    "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True)


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "f").write_text("f\n")
    _git(path, "add", "f")
    _git(path, "commit", "-qm", "f")
    return path


def _with_nested_submodules(tmp_path: Path) -> Path:
    """`libs/foo` (a name with a slash), and `a`, which has its own submodule `b`."""
    inner = _repo(tmp_path / "b")
    middle = _repo(tmp_path / "a")
    _git(middle, "submodule", "add", "-q", str(inner), "b")
    _git(middle, "commit", "-qm", "b")
    top = _repo(tmp_path / "top")
    _git(top, "submodule", "add", "-q", str(_repo(tmp_path / "foo")), "libs/foo")
    _git(top, "submodule", "add", "-q", str(middle), "a")
    _git(top, "submodule", "update", "-q", "--init", "--recursive")
    return top


def test_every_submodule_however_deep_is_protected_and_no_directory_on_the_way_renames(tmp_path):
    top = _with_nested_submodules(tmp_path)
    found = git.protected_paths(top)
    rel = lambda paths: {str(p.relative_to(top)) for p in paths}   # noqa: E731

    for gitdir in (".git", ".git/modules/libs/foo", ".git/modules/a",
                   ".git/modules/a/modules/b"):
        assert {f"{gitdir}/hooks", f"{gitdir}/config"} <= rel(found.paths), gitdir
    # Each checkout's gitlink names where git takes its hooks from (seen writable on the VM).
    assert {"libs/foo/.git", "a/.git", "a/b/.git"} <= rel(found.paths)
    assert rel(found.pinned) == {".git", ".git/modules", ".git/modules/libs",
                                 ".git/modules/libs/foo", ".git/modules/a",
                                 ".git/modules/a/modules", ".git/modules/a/modules/b",
                                 "libs", "libs/foo", "a", "a/b"}
    order = [p for p, _ in found.binds()]
    for path in order:
        assert not any(earlier in path.parents for earlier in order[order.index(path) + 1:]), \
            f"{path} is bound before a directory holding it, which would hide it"


def test_a_git_file_is_itself_read_only(tmp_path):
    """A worktree's `.git` names the directory git takes hooks from."""
    main = _repo(tmp_path / "main")
    _git(main, "worktree", "add", "-q", str(tmp_path / "wt"))
    assert git.protected_paths(tmp_path / "wt").binds() == [(tmp_path / "wt" / ".git", True)]


def test_the_watch_says_a_submodule_added_while_an_agent_runs(tmp_path):
    top = _repo(tmp_path / "top")
    watch = git.ProtectionWatch(top)
    watch.snapshot()
    _git(top, "submodule", "add", "-q", str(_repo(tmp_path / "new")), "new")
    assert str(top / ".git/modules/new/config") in watch.changed()


def test_a_git_directory_says_it_is_its_own_and_a_planted_commondir_is_refused(tmp_path):
    """A `commondir` naming another directory makes the host's own `git status` read that
    directory's config and run its `core.fsmonitor`. So one saying "itself" is written and bound read-only, and one that names
    anything else is refused."""
    repo = _repo(tmp_path / "repo")

    paths = git.protected_paths(repo)

    assert repo / ".git" / "commondir" in paths.paths
    assert (repo / ".git" / "commondir").read_text() == git.OWN_COMMONDIR
    assert git.current_branch(repo)

    (repo / ".git" / "commondir").write_text(str(tmp_path / "elsewhere"))
    with pytest.raises(git.GitError, match="elsewhere"):
        git.protected_paths(repo)


def test_a_layer_is_never_read_through_a_repository_planted_above_it(tmp_path):
    """The definitions are every agent's to write, so a `.git` above a layer may be one a
    container made; the layer's files are asked of its own repository or of none."""
    from raigolmid import documents
    definitions = _repo(tmp_path / "definitions")
    layer = definitions / "bodies" / "web"
    layer.mkdir(parents=True)
    (layer / "body.toml").write_text("")
    (definitions / ".gitignore").write_text("bodies/\n")

    files = documents._layer_files(layer)

    assert files == [layer / "body.toml"], "an ignore rule of the repo above hid the layer"
