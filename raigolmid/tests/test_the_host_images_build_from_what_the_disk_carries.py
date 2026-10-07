"""The host image carries the sources `hostimages` builds from, at the path it builds from.

The path is a fact in two files no import joins — `host/Containerfile` and
`hostimages.DEFAULT_SOURCE` — and each surface's Containerfile assumes the repository root as
its context. A disk that disagreed would boot with no selector and no control, and nothing in
either file alone would show it.
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from raigolmid import hostimages

ROOT = Path(__file__).resolve().parents[2]


def _copies(containerfile: Path) -> list[tuple[str, str]]:
    """(source, destination) for each COPY without a --from."""
    text = re.sub(r"\\\n", " ", containerfile.read_text(encoding="utf-8"))
    out = []
    for line in text.splitlines():
        words = line.split()
        if not words or words[0] != "COPY" or any(w.startswith("--from") for w in words):
            continue
        args = [w for w in words[1:] if not w.startswith("--")]
        out.extend((src, args[-1]) for src in args[:-1])
    return out


def _ignored(path: str) -> bool:
    """Whether `.containerignore` leaves `path` (relative to the root) out of the disk build's
    context, read the way podman does: a pattern anchored at the root, `**/` for any depth,
    and a directory's exclusion covering what is under it."""
    parts = path.split("/")
    for pattern in (ROOT / ".containerignore").read_text(encoding="utf-8").splitlines():
        pattern = pattern.strip()
        if not pattern or pattern.startswith("#"):
            continue
        anywhere = pattern.startswith("**/")
        body = pattern[3:] if anywhere else pattern
        depth = body.count("/") + 1
        starts = range(len(parts)) if anywhere else (0,)
        if any(fnmatch.fnmatchcase("/".join(parts[i:i + depth]), body) for i in starts
               if i + depth <= len(parts)):
            return True
    return False


def test_the_host_image_ships_the_whole_tree_where_hostimages_reads_it():
    shipped = dict(_copies(ROOT / "host" / "Containerfile"))
    assert shipped.get(".") == str(hostimages.DEFAULT_SOURCE)


def test_every_image_raigolmid_builds_does_so_from_the_shipped_tree():
    """Each image's Containerfile and every COPY source in it are in the tree the disk ships
    and not left out of it by `.containerignore`, so an image cannot fail on first boot for
    a file the disk does not carry."""
    for image in (hostimages.selector, hostimages.control, hostimages.notify, hostimages.welcome,
                  hostimages.agent, hostimages.face_mount):
        relative = image().containerfile.relative_to(hostimages.DEFAULT_SOURCE)
        assert (ROOT / relative).exists() and not _ignored(str(relative)), relative
        for src, _ in _copies(ROOT / relative):
            assert (ROOT / src).exists(), (relative, src)
            assert not _ignored(src.rstrip("/")), (relative, src)


def test_what_the_disk_leaves_out_is_what_is_machine_local():
    """The launcher's keys and logs and the dev container's caches stay off the disk; the
    source the janitor reads stays on it."""
    for local in ("windows/authorized_keys", "windows/qemu.log", "windows/screen.ppm",
                  "windows/RaiGolmi.pdb", "windows/obj/project.assets.json",
                  "windows/bin/Release/x.dll", "windows/screen.ppm.partial", ".git", "raigolmid/raigolmid/__pycache__/x.pyc"):
        assert _ignored(local), local
    for source in ("README.md", "windows/RaiGolmi.cs",
                   "windows/Renderer.cs", "windows/RaiGolmi.csproj",
                   "raigolmid/raigolmid/session.py", "host/Containerfile"):
        assert not _ignored(source), source
