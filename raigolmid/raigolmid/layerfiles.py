"""The files a layer's build reads, and nothing else.

`copied` is a host image's inputs (`hostimages.HostImage`); the walk is the set a catalog
upload sends. What an upload carries is what makes the definition do something on another
machine, and the layer's doc, so it is used there as here; anything else in the directory is
noise that can leak the user's private content, so it is never sent. A Containerfile form this does not read is refused
rather than read as copying nothing, which would ship a layer that cannot build, or reuse a
stale image.

    every     its `LAYER.md`, which the user can untick
    face      its toml, each `config_dir`, and `_compositors/<name>`'s Containerfile + inputs
    toolbelt  its toml and its lock
    body      its toml, its Dockerfile, the ignore file docker reads, and the Dockerfile's
              COPY/ADD sources in the context, filtered as docker filters them

`.dockerignore` follows docker's documented rules: `#` in column one is a comment; each line
is cleaned (surrounding whitespace, `.` and `..`, leading and trailing slashes); `*`, `?` and
`[...]` do not cross `/`, `**` spans any number of directories; `!` re-includes; the last
matching line decides; a pattern matching a directory takes everything under it; and
`<Dockerfile>.dockerignore` beside the Dockerfile replaces the context's own.
"""
from __future__ import annotations

import posixpath
import re
import shlex
from pathlib import Path

from .definitions import THUMBNAIL, Body, Face, Toolbelt
from .documents import LAYER_DOC

TOML = {"face": "face.toml", "toolbelt": "toolbelt.toml", "body": "body.toml"}
NEVER = {".git", "__pycache__", ".pytest_cache"}


class LayerFilesError(RuntimeError):
    pass


# --- .dockerignore -----------------------------------------------------------------------

def _pattern(text: str) -> re.Pattern[str]:
    out, i = [], 0
    while i < len(text):
        c = text[i]
        if text.startswith("**", i):
            i += 2
            if text.startswith("/", i):
                i += 1
                out.append("(?:.*/)?")
            else:
                out.append(".*")
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            end = text.find("]", i + 1)
            if end < 0:
                raise LayerFilesError(f"ignore pattern {text!r} has an unclosed '['")
            body = text[i + 1:end]
            out.append("[" + ("^" + body[1:] if body.startswith("^") else body) + "]")
            i = end + 1
            continue
        elif c == "\\" and i + 1 < len(text):
            i += 1
            out.append(re.escape(text[i]))
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z")


class Ignore:
    def __init__(self, text: str = "") -> None:
        self.rules: list[tuple[bool, re.Pattern[str]]] = []
        for line in text.splitlines():
            if line.startswith("#"):
                continue
            line = line.strip()
            exclude = not line.startswith("!")
            line = line.lstrip("!").strip()
            if not line:
                continue
            cleaned = posixpath.normpath(line).strip("/")
            if cleaned in ("", "."):
                continue
            self.rules.append((exclude, _pattern(cleaned)))

    @classmethod
    def for_build(cls, dockerfile: Path, context: Path) -> tuple["Ignore", Path | None]:
        """The ignore file docker reads for this build, and where it is."""
        for candidate in (dockerfile.parent / f"{dockerfile.name}.dockerignore",
                          context / ".dockerignore"):
            if candidate.is_file():
                return cls(candidate.read_text()), candidate
        return cls(), None

    def excludes(self, relative: str) -> bool:
        parts = relative.split("/")
        prefixes = ["/".join(parts[:n]) for n in range(1, len(parts) + 1)]
        excluded = False
        for exclude, rule in self.rules:
            if any(rule.match(p) for p in prefixes):
                excluded = exclude
        return excluded


# --- a Containerfile's inputs ------------------------------------------------------------

def copied(containerfile: Path, context: Path, ignore: Ignore | None = None) -> list[Path]:
    """The files the Containerfile's `COPY` and `ADD` take from the context."""
    ignore = ignore or Ignore()
    text = re.sub(r"\\\n", " ", containerfile.read_text())
    files: set[Path] = set()

    def take(path: Path) -> None:
        relative = path.relative_to(context).as_posix()
        if not NEVER.intersection(path.parts) and not ignore.excludes(relative):
            files.add(path)

    for line in text.splitlines():
        words = line.strip().split(None, 1)
        if len(words) < 2 or words[0].upper() not in ("COPY", "ADD"):
            continue
        args = shlex.split(words[1])
        if any(a.startswith("--from") for a in args):
            continue                    # from another stage, not the context
        args = [a for a in args if not a.startswith("--")]
        if not args or args[0].startswith(("[", "<<")):
            raise LayerFilesError(
                f"{containerfile}: `{line.strip()}` is a form this does not read")
        for source in args[:-1]:
            if words[0].upper() == "ADD" and re.match(r"[a-z]+://", source):
                continue                # fetched by the build, not from the context
            if any(c in source for c in "*?["):
                matches = sorted(context.glob(source.lstrip("/")))
                if not matches:
                    raise LayerFilesError(
                        f"{containerfile} copies {source}, which matches nothing in {context}")
            else:
                matches = [context / source.lstrip("/").rstrip("/")]
            for path in matches:
                if not path.resolve().is_relative_to(context.resolve()):
                    raise LayerFilesError(f"{containerfile} copies {source}, outside {context}")
                if path.is_dir():
                    for p in path.rglob("*"):
                        if p.is_file():
                            take(p)
                elif path.is_file():
                    take(path)
                else:
                    raise LayerFilesError(
                        f"{containerfile} copies {source}, which is not in {context}")
    return sorted(files)


# --- a layer's upload set ----------------------------------------------------------------

def _walk(layer: Face | Toolbelt | Body) -> tuple[dict[str, Path], set[str]]:
    """Every file an upload could send, and those a build reads, which it cannot go without."""
    if layer.directory is None:
        raise LayerFilesError(f"'{layer.id}' is an image, not a definition; there is nothing to upload")
    root = layer.directory
    kind = str(layer.kind)
    out: dict[str, Path] = {TOML[kind]: root / TOML[kind]}
    required = {TOML[kind]}
    if layer.thumbnail is not None:
        out[THUMBNAIL] = layer.thumbnail
    # Its doc, so a downloaded layer is used as the uploader's is.
    if (root / LAYER_DOC).is_file():
        out[LAYER_DOC] = root / LAYER_DOC

    def add_tree(directory: Path, under: str) -> None:
        for p in sorted(directory.rglob("*")):
            if p.is_file() and not NEVER.intersection(p.relative_to(directory).parts):
                out[f"{under}/{p.relative_to(directory).as_posix()}"] = p

    if isinstance(layer, Face):
        for part in (layer.editor, layer.desktop):
            if part is not None and part.config_dir is not None:
                add_tree(part.config_dir, part.config_dir.relative_to(root).as_posix())
        if layer.desktop is not None:
            compositor = root.parent / "_compositors" / layer.desktop.compositor
            containerfile = compositor / "Containerfile"
            if not containerfile.is_file():
                raise LayerFilesError(f"face '{layer.id}' names compositor "
                                      f"'{layer.desktop.compositor}', which has no {containerfile}")
            under = f"_compositors/{layer.desktop.compositor}"
            out[f"{under}/Containerfile"] = containerfile
            required.add(f"{under}/Containerfile")
            for p in copied(containerfile, compositor):
                out[f"{under}/{p.relative_to(compositor).as_posix()}"] = p
                required.add(f"{under}/{p.relative_to(compositor).as_posix()}")
    elif isinstance(layer, Toolbelt):
        if layer.lock_path.is_file():
            out[layer.lock_path.name] = layer.lock_path
    else:
        if layer.working_copy is not None:
            raise LayerFilesError(
                f"body '{layer.id}' builds from the user's project at {layer.working_copy}, which "
                "is their work, not a layer: it is not uploaded")
        if layer.builds_from_source:
            dockerfile, context = layer.dockerfile, layer.build_context
            ignore, ignore_file = Ignore.for_build(dockerfile, context)
            for p in (dockerfile, ignore_file, *copied(dockerfile, context, ignore)):
                if p is not None:
                    if not p.is_relative_to(root):
                        raise LayerFilesError(f"body '{layer.id}' builds from {p}, outside its "
                                              f"directory {root}")
                    out[p.relative_to(root).as_posix()] = p
                    if p != ignore_file:
                        required.add(p.relative_to(root).as_posix())
    for name, path in out.items():
        _not_a_link(path, root, name)
    return out, required


def _not_a_link(path: Path, root: Path, name: str) -> None:
    """An upload sends files, never what a link points at: a link to `~/.ssh/id_ed25519` in a
    config directory would send the key."""
    at = path
    while True:
        if at.is_symlink():
            raise LayerFilesError(f"{name} is, or is under, a symbolic link ({at}); an upload "
                                  "sends files, never what a link points at. Replace it with the "
                                  "file it should carry, or remove it")
        if at == root or at == at.parent:
            return
        at = at.parent


# Names that hold credentials more often than not: left out of an upload unless the user ticks
# them, and flagged in the list they choose from.
_SECRET = re.compile(r"(\.env(\..*)?|.*\.pem|.*\.key|.*\.p12|.*\.pfx|id_(rsa|dsa|ecdsa|ed25519)(\.pub)?"
                     r"|\.credentials\.json|.*token.*|.*secret.*|\.netrc|\.npmrc|\.pypirc)",
                     re.IGNORECASE)


def looks_secret(name: str) -> bool:
    return bool(_SECRET.fullmatch(posixpath.basename(name)))


# The user's choice of what an upload leaves out, kept in the layer and never sent: the files
# they unticked, one path to a line, in the ignore syntax above.
UPLOAD_IGNORE = ".uploadignore"
_GLOB = re.compile(r"[*?\[\]!#]")


def upload_choice(layer: Face | Toolbelt | Body) -> tuple[dict[str, Path], set[str], set[str]]:
    """Every file an upload could send, those a build reads, and those the user's `.uploadignore`
    leaves out. A saved choice that leaves out what a build reads is refused by name."""
    files, required = _walk(layer)
    saved = layer.directory / UPLOAD_IGNORE
    text = saved.read_text(encoding="utf-8") if saved.is_file() else ""
    ignore = Ignore(text)
    # A secret-looking name the user ticked is saved as a re-include (`!name`).
    ticked = {line[1:] for line in text.splitlines() if line.startswith("!")}
    excluded = {name for name in files if ignore.excludes(name)} | {
        name for name in files
        if looks_secret(name) and name not in ticked and name not in required}
    if excluded & required:
        raise LayerFilesError(f"{saved} leaves out {sorted(excluded & required)}, which its "
                              "build reads; another machine could not build it")
    return files, required, excluded


def choose(layer: Face | Toolbelt | Body, excluded: set[str]) -> None:
    """Save which files the user's next upload of this layer leaves out."""
    files, required, _ = upload_choice(layer)
    unknown = excluded - set(files)
    if unknown:
        raise LayerFilesError(f"not among the layer's files: {sorted(unknown)}")
    if excluded & required:
        raise LayerFilesError(f"{sorted(excluded & required)} cannot be left out: the build "
                              "reads them, and another machine could not build the layer")
    odd = sorted(name for name in excluded if _GLOB.search(name))
    if odd:
        raise LayerFilesError(f"{odd} hold pattern characters, so {UPLOAD_IGNORE} cannot name "
                              "them alone; rename them to leave them out")
    path = layer.directory / UPLOAD_IGNORE
    ticked = sorted(name for name in files if looks_secret(name) and name not in excluded
                    and name not in required and not _GLOB.search(name))
    lines = [*sorted(excluded), *(f"!{name}" for name in ticked)]
    if lines:
        path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    else:
        path.unlink(missing_ok=True)
