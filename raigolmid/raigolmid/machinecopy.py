"""This machine's own things, carried to another RaiGolmi in one file.

What goes in is what nothing else can give back: the user's preferences, permissions,
settings, templates and plugins; their history and the tabs' messages; the janitor's documents;
the job and principles ledgers; the machine tab's `/work` (its direction) and the run reports;
each tab's lessons (`thoughts.md`, Claude Code's memory of `/work`, the archive record); the face
home's files; the local layer definitions; and of each body what its remote lacks — the
`origin` it is cloned from, its branch and HEAD, the commits on no remote as a git bundle, and
the files git does not track and the project does not ignore.

What never goes in: a credential of any kind (`_credential`), refused by name wherever it lies
and said in the manifest, because a file sent between machines leaves the user's hands. Nor what
the release, an image build or a remote rebuilds: transcripts (their lessons are distilled into
thoughts, memory and run reports), caches, rendered context, hook state, build output.

Each item is named (`ITEMS`); nothing is swept. Export only reads, and runs beside the daemon:
every state file it takes is replaced whole by the daemon (`intent.save_json`, `docwrite`),
and a ledger torn mid-append costs its readers that row alone (`events.append_lines`). Import
writes beside the daemon's in-memory history, messages and questions, so it refuses while the
daemon's socket answers.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import socket
import stat
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator

from . import credential, git
from .agents import imported_machine_memory
from .definitions import SearchPaths
from .intent import JANITOR
from .paths import Paths

FORMAT = 1
MANIFEST = "manifest.json"
BUNDLES = "bundles"

# Never in an archive, wherever it lies. `.claude-login-*` is a sign-in's temp file.
CREDENTIAL_NAMES = frozenset({
    "agent-credentials", "registry-token", "proxy-secret", "proxy-ca", ".credentials.json",
    ".pki", ".ssh", ".gnupg", ".git-credentials", ".netrc"})
CREDENTIAL_PREFIXES = ("claude-login", ".claude-login", "id_rsa", "id_dsa", "id_ecdsa",
                       "id_ed25519")

# The face home holds the user's things beside what its apps rebuild and what signs them in.
# Left out by name at any depth, each for its reason; everything else in it goes.
FACE_HOME_TOP = {".cache": "a cache", ".pki": "certificates and keys",
                 "Transfer": "the transfer folder's mount point"}
FACE_HOME_ANYWHERE = {
    # A browser profile: its cookies and saved passwords sign the user in.
    "mozilla": "a browser profile, holding sign-ins",
    # Electron and Chromium apps' (VSCodium's) profile: sign-ins, and what they rebuild.
    "Cookies": "sign-ins", "Cookies-journal": "sign-ins", "Local Storage": "sign-ins",
    "Session Storage": "sign-ins", "WebStorage": "sign-ins", "SharedStorage": "sign-ins",
    "SharedStorage-wal": "sign-ins", "Trust Tokens": "sign-ins",
    "Trust Tokens-journal": "sign-ins", "TransportSecurity": "sign-ins",
    "Network Persistent State": "sign-ins", "DIPS": "sign-ins", "DIPS-wal": "sign-ins",
    # VS Code's secret storage is in its global `state.vscdb`.
    "globalStorage": "an editor's secret storage", "machineid": "this machine's identity",
    "Cache": "a cache", "Code Cache": "a cache", "GPUCache": "a cache", "CachedData": "a cache",
    "CachedProfilesData": "a cache", "CachedConfigurations": "a cache",
    "DawnWebGPUCache": "a cache", "DawnGraphiteCache": "a cache", "Crashpad": "crash dumps",
    "Service Worker": "a cache", "blob_storage": "a cache", "Shared Dictionary": "a cache",
    "code.lock": "a running app's lock", "lock": "a running app's lock",
    "swap": "a running editor's swap files"}


class MachineCopyError(Exception):
    """An export or import that cannot be done as asked, saying why."""


@dataclass(frozen=True)
class Item:
    """One named thing in the archive: a file, a whole tree, or a ledger directory's
    `*.jsonl`, at `arc` in the archive and at `place` on a machine."""
    name: str
    arc: str
    kind: str                                   # "file" | "tree" | "ledgers"
    place: Callable[[Paths, SearchPaths], Path]


ITEMS: tuple[Item, ...] = (
    Item("preferences", "state/preferences.md", "file", lambda p, s: p.preferences),
    Item("permissions", "state/permissions.md", "file", lambda p, s: p.permissions),
    Item("history", "state/history.json", "file", lambda p, s: p.history),
    Item("messages", "state/messages.json", "file", lambda p, s: p.messages),
    Item("settings", "config/settings.toml", "file", lambda p, s: p.settings),
    Item("agent templates", "config/agent-templates", "tree", lambda p, s: p.agent_templates),
    Item("agent plugins", "config/agent-plugins", "tree", lambda p, s: p.agent_plugins),
    Item("janitor documents", "state/janitor", "tree", lambda p, s: p.janitor_documents),
    Item("job ledgers", "state/job-ledgers", "ledgers", lambda p, s: p.job_ledger("_").parent),
    # Not `principles.json`: its HEADs and counters are this machine's own.
    Item("principles ledgers", "state/principles", "ledgers",
         lambda p, s: p.principles_ledger("_").parent),
    Item("machine tab's /work", "data/work", "tree", lambda p, s: p.work),
    Item("run reports", "data/runs", "tree", lambda p, s: p.runs),
    Item("faces", "layers/faces", "tree", lambda p, s: _one(s.faces, "faces")),
    Item("toolbelts", "layers/toolbelts", "tree", lambda p, s: _one(s.toolbelts, "toolbelts")),
    # Built by `_lessons`, `_face_home` and `_bodies`, placed like the rest.
    Item("lessons", "data/agent-archive", "built", lambda p, s: p.agent_archive),
    Item("face home", "data/face-home", "built", lambda p, s: p.face_home),
    Item("bodies", "layers/bodies", "built", lambda p, s: _one(s.bodies, "bodies")),
)


def _one(roots: tuple[Path, ...], kind: str) -> Path:
    """A kind's definitions directory. One, because an archive names a layer by kind and id
    alone and a machine with two would not say which a layer belongs in."""
    if len(roots) != 1:
        raise MachineCopyError(f"{kind} are looked for in {len(roots)} directories "
                               f"({', '.join(map(str, roots))}); a machine copy carries one")
    return roots[0]


def _credential(rel: PurePosixPath) -> bool:
    parts = rel.parts
    return (any(p in CREDENTIAL_NAMES or p.startswith(CREDENTIAL_PREFIXES) for p in parts)
            or (len(parts) > 1 and parts[-2] == "sessions" and rel.suffix == ".key"))


# --- export ------------------------------------------------------------------------------

class _Archive:
    """What an export gathers: each member by its archive name, from a file on disk or from
    bytes, with what was left out and why."""

    def __init__(self) -> None:
        self.members: dict[str, tuple[Path | None, bytes | None, float]] = {}
        self.counts: dict[str, int] = {}
        self.withheld: list[dict[str, str]] = []

    def file(self, item: str, arc: str, source: Path) -> None:
        if _credential(PurePosixPath(arc)):
            self.withheld.append({"path": arc, "why": "a credential"})
            return
        if arc in self.members:
            raise MachineCopyError(f"{arc} would be in the archive twice")
        self.members[arc] = (source, None, 0.0)
        self.counts[item] = self.counts.get(item, 0) + 1

    def data(self, item: str, arc: str, body: bytes, mtime: float) -> None:
        self.members[arc] = (None, body, mtime)
        self.counts[item] = self.counts.get(item, 0) + 1

    def tree(self, item: str, arc: str, root: Path,
             leave_out: Callable[[PurePosixPath, Path], str | None] = lambda r, p: None) -> None:
        for path, rel in _walk(root, self.withheld, arc, leave_out):
            self.file(item, f"{arc}/{rel}", path)


def _walk(root: Path, withheld: list[dict[str, str]], arc: str,
          leave_out: Callable[[PurePosixPath, Path], str | None],
          rel: PurePosixPath = PurePosixPath()) -> Iterator[tuple[Path, PurePosixPath]]:
    """Every regular file and link under `root`, links never followed. A socket, a pipe, a
    device or a mount point is not a file to carry, and is said as left out."""
    for entry in sorted(os.scandir(root / rel), key=lambda e: e.name):
        path, child = Path(entry.path), rel / entry.name
        why = "a credential" if _credential(PurePosixPath(arc, child)) else leave_out(child, path)
        if why is None and entry.is_dir(follow_symlinks=False) and os.path.ismount(path):
            why = "a mount point"
        if why is None and not (entry.is_dir(follow_symlinks=False) or entry.is_symlink()
                                or entry.is_file(follow_symlinks=False)):
            why = "not a file"
        if why is not None:
            withheld.append({"path": f"{arc}/{child}", "why": why})
        elif entry.is_dir(follow_symlinks=False):
            yield from _walk(root, withheld, arc, leave_out, child)
        else:
            yield path, child


def export(paths: Paths, search: SearchPaths, out_dir: Path) -> Path:
    """`raigolmi-machine-<UTC time>.tar.gz` in `out_dir`, made beside it and moved in whole,
    so the launcher emptying the transfer folder never takes half of one."""
    if out_dir.is_symlink():
        raise MachineCopyError(f"{out_dir} is a link, not the folder to write into; the copy "
                               "is not written through it. Remove the link or name another.")
    out_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    archive = _Archive()
    for item in ITEMS:
        source = item.place(paths, search)
        if item.kind == "file" and source.is_file():
            archive.file(item.name, item.arc, source)
        elif item.kind == "tree" and source.is_dir():
            archive.tree(item.name, item.arc, source)
        elif item.kind == "ledgers" and source.is_dir():
            for ledger in sorted(source.glob("*.jsonl")):
                archive.file(item.name, f"{item.arc}/{ledger.name}", ledger)
    by = {i.name: i for i in ITEMS}
    _lessons(paths, archive, by["lessons"], now)
    _face_home(paths, archive, by["face home"])
    with tempfile.TemporaryDirectory(prefix="raigolmi-machine-") as scratch:
        bodies = _bodies(_one(search.bodies, "bodies"), archive, by["bodies"], Path(scratch))
        manifest = {
            "format": FORMAT, "made": stamp, "machine": socket.gethostname(),
            "items": [{"item": i.name, "archive": i.arc, "files": archive.counts.get(i.name, 0)}
                      for i in ITEMS],
            "bodies": bodies, "withheld": archive.withheld}
        name = f"raigolmi-machine-{stamp}.tar.gz"
        fd, partial = tempfile.mkstemp(dir=out_dir.parent, prefix=f".{name}-", suffix=".tmp")
        os.close(fd)
        try:
            with tarfile.open(partial, "w:gz") as tar:
                _add_bytes(tar, MANIFEST, json.dumps(manifest, indent=2).encode(), now)
                for arc, (source, body, mtime) in sorted(archive.members.items()):
                    if source is not None:
                        tar.add(source, arcname=arc, recursive=False)
                    else:
                        assert body is not None
                        _add_bytes(tar, arc, body, mtime)
            target = out_dir / name
            os.replace(partial, target)
        except BaseException:
            Path(partial).unlink(missing_ok=True)
            raise
    return target


def _add_bytes(tar: tarfile.TarFile, arc: str, body: bytes, mtime: float) -> None:
    info = tarfile.TarInfo(arc)
    info.size, info.mtime, info.mode = len(body), mtime, 0o600
    tar.addfile(info, io.BytesIO(body))


def _lessons(paths: Paths, archive: _Archive, item: Item, now: float) -> None:
    """Each tab's lessons as an archive entry: the archive's own entries as they are, and each
    live home as the entry its close would make, since the machine it lands on has no such tab.
    An entry's record says it was imported, and from where."""
    imported = {"machine": socket.gethostname(),
                "exported": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))}

    def entry(name: str, root: Path, memory: str, record: dict[str, Any] | None,
              mtime: float) -> None:
        arc = f"{item.arc}/{name}"
        if (root / "thoughts.md").is_file():
            archive.file(item.name, f"{arc}/thoughts.md", root / "thoughts.md")
        if (root / memory).is_dir():
            archive.tree(item.name, f"{arc}/{memory}", root / memory)
        if record is not None:
            archive.data(item.name, f"{arc}.json",
                         json.dumps({**record, "imported": imported}, indent=2,
                                    sort_keys=True).encode(), mtime)

    work_memory = ".claude/projects/-work/memory"
    if paths.agent_archive.is_dir():
        for home in sorted(d for d in paths.agent_archive.iterdir() if d.is_dir()):
            record_path = home.with_name(f"{home.name}.json")
            record = (json.loads(record_path.read_text(encoding="utf-8"))
                      if record_path.is_file() else None)
            fresh = record is not None and record.get("fresh_restart")
            entry(home.name, home, "memory" if fresh else work_memory, record,
                  record_path.stat().st_mtime if record is not None else now)
    tabs = (json.loads(paths.intent.read_text(encoding="utf-8")).get("tabs", {})
            if paths.intent.is_file() else {})
    stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(now))
    if paths.agent_homes.is_dir():
        for home in sorted(d for d in paths.agent_homes.iterdir() if d.is_dir()):
            tab = home.name
            # The record `Session` gives a closed tab's home, or an orphaned one's.
            record = ({"tab": tab, "body": tabs[tab].get("body")} if tab in tabs
                      else {"tab": tab, "orphaned": True})
            entry(f"{tab}-{stamp}", home, work_memory, record, now)


def _face_home(paths: Paths, archive: _Archive, item: Item) -> None:
    def leave_out(rel: PurePosixPath, path: Path) -> str | None:
        if len(rel.parts) == 1 and rel.name in FACE_HOME_TOP:
            return FACE_HOME_TOP[rel.name]
        if rel.name in FACE_HOME_ANYWHERE:
            return FACE_HOME_ANYWHERE[rel.name]
        if rel.suffix == ".log" or rel.name.startswith(".org.chromium."):
            return "a running app's log or temporary file"
        return None

    if paths.face_home.is_dir():
        archive.tree(item.name, item.arc, paths.face_home, leave_out)


def _bodies(root: Path, archive: _Archive, item: Item, scratch: Path) -> dict[str, Any]:
    """Each body as its remote cannot give it back. With git: its `origin`, branch and HEAD,
    the commits on no remote as a bundle (every commit when it has no `origin`), and the files
    git does not track that the project's own ignore files do not ignore — untracked, or
    ignored by `.git/info/exclude` alone, where RaiGolmi's own body files are. Without git:
    the whole directory."""
    bodies: dict[str, Any] = {}
    if not root.is_dir():
        return bodies
    for directory in sorted(root.iterdir()):
        body = directory.name
        arc = f"{item.arc}/{body}"
        if not directory.is_dir() or directory.is_symlink():
            archive.withheld.append({"path": arc, "why": "not a body's directory"})
            continue
        if not git.is_repo(directory):
            archive.tree(item.name, arc, directory)
            bodies[body] = {"git": False}
            continue
        if not (directory / ".git").is_dir():
            raise MachineCopyError(f"{directory}/.git is not a directory: a body's working "
                                   "copy is its own clone, which a machine copy carries")
        head = git.run(["rev-parse", "--verify", "-q", "HEAD"], directory, check=False)
        if head.returncode != 0:
            raise MachineCopyError(f"{directory} is a git repository with no commit; commit "
                                   "its files or remove its .git, then export")
        origin = git.run(["config", "--get", "remote.origin.url"], directory, check=False)
        url = origin.stdout.strip() if origin.returncode == 0 else None
        branch = git.current_branch(directory)
        revs = ["HEAD", "--branches", "--not", "--remotes"] if url else ["--all"]
        count = int(git.run(["rev-list", "--count", *revs], directory).stdout.strip())
        bundle = None
        if count:
            bundle = f"{BUNDLES}/{body}.bundle"
            path = scratch / f"{body}.bundle"
            git.run(["bundle", "create", "--quiet", str(path), *revs], directory, timeout=1800)
            archive.file(item.name, bundle, path)
        for rel in (r.rstrip("/") for r in _untracked(directory)):
            target = directory / rel
            if target.is_dir() and not target.is_symlink():
                # A repository inside the working copy, which git lists as one entry.
                archive.tree(item.name, f"{arc}/{rel}", target)
            else:
                archive.file(item.name, f"{arc}/{rel}", target)
        exclude = directory / ".git" / "info" / "exclude"
        # A clone has its own empty `info/exclude`; the body's lists what is the machine's.
        bodies[body] = {"git": True, "origin": url,
                        "branch": None if branch == "HEAD" else branch,
                        "head": head.stdout.strip(), "bundle": bundle,
                        "exclude": exclude.read_text() if exclude.is_file() else None}
    return bodies


def _untracked(repo: Path) -> list[str]:
    """Files git does not track: those no ignore file names (`--exclude-standard`), and those
    `.git/info/exclude` ignores where it decides — `check-ignore -v` names the pattern that
    decides, and a project's `.gitignore` outranks `info/exclude`, so a build directory both
    name is the project's and stays out."""
    def listed(*args: str) -> list[str]:
        out = git.run(["ls-files", "-z", "--others", *args], repo).stdout
        return [p for p in out.split("\0") if p]

    untracked = listed("--exclude-standard")
    if not (repo / ".git" / "info" / "exclude").is_file():
        return untracked
    excluded = listed("--ignored", "--exclude-from=.git/info/exclude")
    if not excluded:
        return untracked
    info = (repo / ".git" / "info" / "exclude").resolve()
    proc = git.run(["check-ignore", "-v", "-z", "--stdin"], repo, check=False,
                   input="\0".join(excluded) + "\0")
    if proc.returncode not in (0, 1):
        raise MachineCopyError(f"git check-ignore in {repo} exited {proc.returncode}: "
                               f"{proc.stderr.strip()}")
    fields = proc.stdout.split("\0")
    decided = {}
    for source, _line, pattern, path in zip(*[iter(fields)] * 4):
        decided[path] = (source, pattern)
    own = [p for p in excluded if p in decided
           and (repo / decided[p][0]).resolve() == info and not decided[p][1].startswith("!")]
    return untracked + own


# --- import ------------------------------------------------------------------------------

def daemon_answers(paths: Paths) -> bool:
    """Whether something listens on the daemon's socket."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        try:
            sock.connect(str(paths.api_socket))
        except (FileNotFoundError, ConnectionRefusedError):
            return False
    return True


def import_archive(paths: Paths, search: SearchPaths, archive: Path) -> dict[str, Any]:
    """The archive's files written into place, overwriting what it holds and nothing else;
    each body cloned from its `origin` (or fetched, when already here), its bundle laid over
    and its recorded HEAD checked out, then its own files laid over that. A body git could not
    restore is said with git's error, its files written all the same. The machine tab open
    here takes up the imported machine tab's memory."""
    if daemon_answers(paths):
        raise MachineCopyError(
            f"raigolmid answers on {paths.api_socket}: it holds the history, the messages and "
            "the questions in memory and would write them back over the import. Stop it "
            "(`systemctl --user stop raigolmid`) or use `rai import`, which does.")
    written: dict[str, int] = {}
    with tarfile.open(archive, "r:gz") as tar:
        members = {m.name: m for m in tar.getmembers()}
        if MANIFEST not in members:
            raise MachineCopyError(f"{archive} has no {MANIFEST}: it is not a machine copy")
        manifest = json.loads(tar.extractfile(members[MANIFEST]).read())  # type: ignore[union-attr]
        if manifest.get("format") != FORMAT:
            raise MachineCopyError(f"{archive} is format {manifest.get('format')!r}; this "
                                   f"RaiGolmi reads format {FORMAT}")
        roots = [(i, i.place(paths, search)) for i in ITEMS]
        bodies_root = _one(search.bodies, "bodies")
        # A private project is cloned with this machine's GitHub sign-in, as its tabs reach it;
        # with none, `origin` is asked anonymously, and a refusal is the body's error.
        github = (credential.read(paths.registry_token, credential.REGISTRY_KEYS)["GITHUB_TOKEN"]
                  if paths.registry_token.is_file() else None)
        bodies = {}
        with tempfile.TemporaryDirectory(prefix="raigolmi-import-") as scratch:
            for body, record in manifest["bodies"].items():
                bundle = None
                if record.get("bundle"):
                    bundle = Path(scratch) / f"{body}.bundle"
                    _write(tar, members[record["bundle"]], bundle, Path(scratch))
                bodies[body] = _restore(bodies_root / body, record, bundle, github)
        for name, member in sorted(members.items()):
            if name == MANIFEST or name.startswith(f"{BUNDLES}/"):
                continue
            if _credential(PurePosixPath(name)):
                raise MachineCopyError(f"{archive} holds {name}, a credential; no export "
                                       "writes one, so it is not taken")
            item, target, root = _placed(name, roots)
            _write(tar, member, target, root)
            written[item.name] = written.get(item.name, 0) + 1
    return {"written": written, "bodies": bodies, "machine_memory": _to_machine_tab(paths),
            "from": {"machine": manifest["machine"], "made": manifest["made"]}}


def _placed(name: str, roots: list[tuple[Item, Path]]) -> tuple[Item, Path, Path]:
    for item, place in roots:
        if name == item.arc and item.kind == "file":
            return item, place, place.parent
        if name.startswith(f"{item.arc}/") and item.kind != "file":
            rel = PurePosixPath(name[len(item.arc) + 1:])
            if rel.is_absolute() or ".." in rel.parts:
                break
            return item, place / rel, place
    raise MachineCopyError(f"{name} is not anything a machine copy carries")


def _write(tar: tarfile.TarFile, member: tarfile.TarInfo, target: Path, root: Path) -> None:
    """One member at `target`, which stays under `root`: no directory on the way is a link."""
    rel = target.relative_to(root)
    at = root
    for part in rel.parts[:-1]:
        at = at / part
        if at.is_symlink():
            raise MachineCopyError(f"{at} is a link; {member.name} is not written through it")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_dir() and not target.is_symlink():
        raise MachineCopyError(f"{target} is a directory where the archive has a file")
    if member.issym():
        target.unlink(missing_ok=True)
        os.symlink(member.linkname, target)
        return
    if not member.isfile():
        raise MachineCopyError(f"{member.name} is neither a file nor a link")
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(tar.extractfile(member), out)  # type: ignore[arg-type]
        os.chmod(tmp, stat.S_IMODE(member.mode))
        os.utime(tmp, (member.mtime, member.mtime))
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _restore(directory: Path, record: dict[str, Any], bundle: Path | None,
             github: str | None) -> dict[str, Any]:
    """A body's git as its record says: cloned from `origin` when absent, fetched when here
    with the same `origin`, a fresh repository when it had none; the bundle's commits fetched,
    the recorded HEAD checked out on its branch and the bundle's other refs set. `origin` is
    reached with this machine's GitHub sign-in when it has one, as its tabs reach it."""
    if not record["git"]:
        return {"restored": "files"}
    url, branch, head = record["origin"], record["branch"], record["head"]
    try:
        if directory.exists() and not git.is_repo(directory):
            raise git.GitError(f"{directory} is here and is not a git repository")
        if not directory.exists():
            directory.parent.mkdir(parents=True, exist_ok=True)
            if url:
                git.clone(url, directory, github=github)
                done = "cloned"
            else:
                directory.mkdir()
                git.run(["init", "--quiet"], directory)
                done = "made from the bundle"
        else:
            here = git.run(["config", "--get", "remote.origin.url"], directory, check=False)
            if (here.stdout.strip() or None) != url:
                raise git.GitError(f"{directory} is cloned from {here.stdout.strip() or 'nowhere'}"
                                   f", not {url or 'nowhere'}")
            if url:
                git.run(["fetch", "--quiet", "origin"], directory, timeout=1800, github=github)
            done = "fetched"
        heads: list[tuple[str, str]] = []
        if bundle is not None:
            listing = git.run(["bundle", "list-heads", str(bundle)], directory).stdout
            heads = [(ref, sha) for sha, _, ref in
                     (line.partition(" ") for line in listing.splitlines())]
            # HEAD too: a detached HEAD's commits are on no branch.
            git.run(["fetch", "--quiet", str(bundle), *(ref for ref, _ in heads)], directory,
                    timeout=1800)
        if branch is None:
            git.run(["checkout", "--quiet", "--detach", head], directory)
        else:
            git.run(["checkout", "--quiet", "-B", branch, head], directory)
            if url and git.run(["rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}"],
                               directory, check=False).returncode == 0:
                git.run(["branch", "--quiet", f"--set-upstream-to=origin/{branch}"], directory)
        for ref, sha in heads:
            if ref not in ("HEAD", f"refs/heads/{branch}"):
                git.run(["update-ref", ref, sha], directory)
        if record["exclude"] is not None:
            (directory / ".git" / "info").mkdir(exist_ok=True)
            (directory / ".git" / "info" / "exclude").write_text(record["exclude"])
    except git.GitError as exc:
        return {"error": str(exc)}
    return {"restored": done}


def _to_machine_tab(paths: Paths) -> str | None:
    """The imported machine tab's memory laid into the machine tab open here, which reads it
    at its next start; one this machine opens later takes it up itself (`Agents._carry_memory`).
    The memory's path, or None when there is no machine tab here or no memory came."""
    memory = imported_machine_memory(paths.agent_archive)
    if memory is None or not paths.intent.is_file():
        return None
    tabs = json.loads(paths.intent.read_text(encoding="utf-8")).get("tabs", {})
    machine = next((t for t, tab in tabs.items()
                    if tab.get("body") is None and t != JANITOR), None)
    if machine is None or not (paths.agent_homes / machine).is_dir():
        return None
    into = paths.agent_homes / machine / ".claude" / "projects" / "-work" / "memory"
    shutil.copytree(memory, into, dirs_exist_ok=True)
    return str(into)
