"""Git inside containers.

Two things must hold: git works inside session views and agent containers, and nothing
inside a container can make the **host's** git execute code or silently rewrite the user's
history.

The code-execution vectors are `.git/hooks/*`, settings in `.git/config` such as
`core.fsmonitor`, `core.hooksPath`, `core.sshCommand`, `core.pager` and filter/diff
drivers, `config.worktree` files, a `commondir` (which moves where git reads all of those
from), and the same files for every
submodule under `.git/modules/*/`. They are closed by mounting each of those read-only over
the writable `/work` mount (done in the view's entrypoint) and by checking them here.

Every git command raigolmid runs names its repository (`run`): it never discovers one, so a
repository a container planted anywhere below `/work` is never the one it runs.

The history vector — anything with write access to a shared `refs/` can move the user's
branches — is *not* closed for a body tab, and that is a deliberate, stated choice: the user watches an agent work in the files they are editing. A moved ref is
recoverable from the reflog, and the repository's ref state is recorded before an agent
starts so "what did it move" is answerable.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Hooks are disabled for every git command raigolmid runs against the user's repository:
# a hook is how a container would reach the host. A bare repository is never taken
# from the directory it is found in.
SAFE_GIT = ("git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
            "-c", "safe.bareRepository=explicit")
# What `commondir` says in a git directory that has none of its own: itself. Git reads an
# empty one as an error, and one naming another directory takes that directory's config.
OWN_COMMONDIR = ".\n"


class GitError(Exception):
    """A git command failed. Carries the command and stderr, because the next move is
    almost always to show the user exactly what git said."""


@dataclass(frozen=True, slots=True)
class ProtectedPaths:
    """What is bound over the user's repo wherever it is mounted: `paths` read-only, and
    `pinned` — every directory holding one of them, from `.git` down — onto itself.

    A directory that holds a read-only bind can still be renamed, taking the bind with it,
    and a copy put in its place is writable; one that is a mount point cannot (EBUSY).
    A bind over a
    directory hides the binds already under it, so `binds` is parents first."""
    paths: tuple[Path, ...]
    pinned: tuple[Path, ...] = ()

    def binds(self) -> list[tuple[Path, bool]]:
        """Each path with whether it is read-only, in the order they are bound."""
        return sorted([*((p, False) for p in self.pinned), *((p, True) for p in self.paths)],
                      key=lambda bind: len(bind[0].parts))

    def hashes(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for p in self.paths:
            if p.is_file():
                out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
            elif p.is_dir():
                digest = hashlib.sha256()
                for child in sorted(p.rglob("*")):
                    if child.is_file():
                        digest.update(str(child.relative_to(p)).encode())
                        digest.update(child.read_bytes())
                out[str(p)] = digest.hexdigest()
        return out


def protected_paths(working_copy: Path) -> ProtectedPaths:
    """The repo's own git directory and every submodule's, however deep: each one's hooks
    and config files read-only. An absent `hooks` directory is created empty first: git runs
    whatever one holds, so a missing one is a place a container could make and fill. A
    `.git` file — the working copy's own (a worktree's) or each submodule checkout's inside
    it — is itself read-only, since the directory it names is where git would look for hooks;
    each directory down to a checkout's is pinned, so none is renamed away with it."""
    git_dir = working_copy / ".git"
    if git_dir.is_file():
        return ProtectedPaths(paths=(git_dir,))
    if not git_dir.is_dir():
        return ProtectedPaths(paths=())
    found: list[Path] = []
    pinned: set[Path] = {git_dir}
    for directory in (git_dir, *_submodule_git_dirs(git_dir / "modules")):
        (directory / "hooks").mkdir(exist_ok=True)
        _own_commondir(directory)
        found += [directory / rel for rel in ("hooks", "config", "config.worktree", "commondir")
                  if (directory / rel).exists()]
        pinned.update(p for p in directory.parents if git_dir in p.parents)
        pinned.add(directory)
        checkout = _checkout_of(directory, working_copy) if directory != git_dir else None
        if checkout is not None and (checkout / ".git").is_file():
            found.append(checkout / ".git")
            pinned.update(p for p in (checkout, *checkout.parents) if working_copy in p.parents)
    return ProtectedPaths(paths=tuple(found), pinned=tuple(sorted(pinned)))


def _own_commondir(directory: Path) -> None:
    """A `commondir` saying the directory is its own, written where there is none so it can
    be bound read-only: an absent one is a place a container could make and point anywhere.
    One that already names another directory is refused, never overwritten: a repository's
    own git directory has no other, so it was put there."""
    path = directory / "commondir"
    if not path.exists():
        path.write_text(OWN_COMMONDIR)
        return
    if path.read_text().strip() != ".":
        raise GitError(f"{path} names {path.read_text().strip()!r} as the directory git reads "
                       "this repository's config and hooks from. Nothing makes one in a "
                       "repository's own git directory; remove it after reading what it "
                       "points at.")


def _checkout_of(module: Path, working_copy: Path) -> Path | None:
    """Where a submodule's git directory says its checkout is (`core.worktree`, relative to
    it), under the working copy; None for one with no checkout there."""
    proc = run(["config", "--file", str(module / "config"), "--get", "core.worktree"],
               working_copy, check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    checkout = (module / proc.stdout.strip()).resolve()
    root = working_copy.resolve()
    if checkout == root or root not in checkout.parents:
        return None
    return working_copy / checkout.relative_to(root)


def _submodule_git_dirs(modules: Path) -> list[Path]:
    """Every git directory under a `modules` directory. A submodule's is at its name, which
    may hold slashes (`modules/libs/foo`), and holds its own submodules' under its own
    `modules`; a git directory is the one with a `HEAD`."""
    if not modules.is_dir():
        return []
    found: list[Path] = []
    for child in sorted(modules.iterdir()):
        if not child.is_dir() or child.is_symlink():
            continue
        if (child / "HEAD").is_file():
            found += [child, *_submodule_git_dirs(child / "modules")]
        else:
            found += _submodule_git_dirs(child)
    return found


def run(args: list[str], repo: Path, check: bool = True,
        timeout: int = 120, input: str | None = None) -> subprocess.CompletedProcess[str]:
    """`git <args>` on the repository whose root is `repo`, named outright rather than
    discovered: a `.git` planted below it, or in a directory above it, is never consulted."""
    cmd = [*SAFE_GIT, *args]
    env = {**os.environ, "GIT_DIR": str(repo / ".git"), "GIT_WORK_TREE": str(repo),
           "GIT_CEILING_DIRECTORIES": str(repo.parent)}
    try:
        proc = subprocess.run(cmd, cwd=str(repo), env=env, capture_output=True, text=True,
                              timeout=timeout, input=input)
    except FileNotFoundError as exc:
        raise GitError(f"git is not installed: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"{' '.join(cmd)} timed out after {timeout}s") from exc
    if check and proc.returncode != 0:
        raise GitError(f"{' '.join(cmd)} exited {proc.returncode}\n{proc.stderr.strip()}")
    return proc


def is_repo(path: Path) -> bool:
    return (path / ".git").exists()


def current_branch(repo: Path) -> str:
    return run(["rev-parse", "--abbrev-ref", "HEAD"], repo).stdout.strip()


def ref_state(repo: Path) -> dict[str, str]:
    """Recorded in `events.jsonl` before an agent starts, so "what did it move" is
    answerable afterwards."""
    proc = run(["for-each-ref", "--format=%(refname) %(objectname)"], repo, check=False)
    if proc.returncode != 0:
        return {}
    state: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        ref, _, sha = line.partition(" ")
        if ref:
            state[ref] = sha
    return state


def remaining(path: Path, limit: int = 5) -> str:
    """What is in a directory, by name. The count is the whole value of the message: it
    tells the reader whether this is one stray file or a whole tree."""
    try:
        found = sorted(p for p in path.rglob("*"))
    except OSError as exc:
        return f"contents that could not be listed: {exc}"
    if not found:
        return "nothing — the directory itself is what refused"
    shown = ", ".join(str(p.relative_to(path)) for p in found[:limit])
    more = f" and {len(found) - limit} more" if len(found) > limit else ""
    return f"{len(found)} path(s): {shown}{more}"


class ProtectionWatch:
    """Warns if a protected file changes while an agent is running, and checks them before
    the user runs git through `rai`."""

    def __init__(self, working_copy: Path, baseline: dict[str, str] | None = None) -> None:
        self.working_copy = working_copy
        self.baseline: dict[str, str] = baseline or {}

    def snapshot(self) -> dict[str, str]:
        self.baseline = protected_paths(self.working_copy).hashes()
        return self.baseline

    def changed(self) -> list[str]:
        """Protected paths changed, added (a new submodule's, unprotected until the view is
        rebuilt) or gone since the baseline."""
        if not self.baseline:
            return []
        now = protected_paths(self.working_copy).hashes()
        return sorted(path for path in {*now, *self.baseline}
                      if now.get(path) != self.baseline.get(path))
