"""Discovering and reading the three layers' definitions.

Everything a layer is lives in its own directory, which is what makes it agent-drivable:
an agent told to change the desktop edits files here and raigolmid reloads them.
Reading is therefore deliberately literal — an unknown key is an error the author sees,
not a value quietly ignored.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import labels


class DefinitionError(Exception):
    """A definition file is missing, malformed, or contradicts itself. Always raised with
    the path, because the author is the one who has to act on it."""


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError as exc:
        raise DefinitionError(f"{path}: no such file") from exc
    except tomllib.TOMLDecodeError as exc:
        raise DefinitionError(f"{path}: {exc}") from exc


# One path component that is neither `.` nor `..`: a face's id names its settings directory in
# the user's home (`faces.FACE_SETTINGS`), and a registry entry's names the directory a download
# unpacks into (`registry._entry`).
PLAIN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


_PLACE = re.compile(r"\{(\w+)\}")


def _argv(value: Any, path: Path, what: str, places: set[str]) -> tuple[str, ...]:
    """A command as its argv, naming only the `{place}`s it is given. Any other brace is
    its own text."""
    if not isinstance(value, list) or not value or not all(isinstance(a, str) for a in value):
        raise DefinitionError(f"{path}: {what} is not a non-empty list of strings")
    for arg in value:
        for name in _PLACE.findall(arg):
            if name not in places:
                raise DefinitionError(f"{path}: {what} names {{{name}}}; it is given "
                                      f"{', '.join(f'{{{p}}}' for p in sorted(places))}")
    return tuple(value)


def _require(d: dict[str, Any], key: str, path: Path) -> Any:
    if key not in d:
        raise DefinitionError(f"{path}: missing required key '{key}'")
    return d[key]


def _reject_unknown(d: dict[str, Any], known: set[str], path: Path, where: str) -> None:
    unknown = set(d) - known
    if unknown:
        raise DefinitionError(
            f"{path}: unknown key(s) in {where}: {', '.join(sorted(unknown))}"
        )


def _confined(value: str, root: Path, path: Path, what: str) -> Path:
    """`root / value`, refused when it resolves (links followed) outside `root`: a layer
    reaches only its own directory, or a body its project, so a downloaded one cannot
    mount or build from anywhere else on the machine."""
    target = root / value
    if not target.resolve().is_relative_to(root.resolve()):
        raise DefinitionError(f"{path}: {what} '{value}' is outside {root}; a layer names "
                              "only paths inside its own directory")
    return target


# --- Face ---------------------------------------------------------------------------

# What the catalog shows of any layer: its words, whose it is, and its picture.
ABOUT_KEYS = {"description", "author"}
THUMBNAIL = "thumbnail.png"


def _about(raw: dict[str, Any], path: Path) -> dict[str, Any]:
    about: dict[str, Any] = {}
    for key in ABOUT_KEYS:
        value = raw.get(key)
        if value is not None and not isinstance(value, str):
            raise DefinitionError(f"{path}: '{key}' is {value!r}, not text")
        about[key] = value
    thumbnail = path.parent / THUMBNAIL
    about["thumbnail"] = thumbnail if thumbnail.is_file() else None
    return about


@dataclass(frozen=True, slots=True)
class FaceEditor:
    package: str
    config_dir: Path | None
    # What the editor window runs, and what shows it a file at a line (`faces.EDITOR_PLACES`).
    command: tuple[str, ...]
    open: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class FaceDesktop:
    compositor: str
    config_dir: Path | None
    apps: tuple[str, ...]
    # The command that opens a URL in the face, from its `apps`; None when the
    # face has no browser.
    browser: str | None = None


@dataclass(frozen=True, slots=True)
class Face:
    id: str
    name: str
    directory: Path
    requires_toolbelt_capabilities: tuple[str, ...]
    editor: FaceEditor | None
    desktop: FaceDesktop | None
    description: str | None = None
    author: str | None = None
    thumbnail: Path | None = None

    kind = labels.Kind.FACE


def load_face(directory: Path) -> Face:
    path = directory / "face.toml"
    raw = _load_toml(path)
    _reject_unknown(raw, {"id", "name", "requires_toolbelt_capabilities",
                          "desktop", "editor", *ABOUT_KEYS}, path, "face.toml")

    editor = None
    if (e := raw.get("editor")) is not None:
        _reject_unknown(e, {"package", "config_dir", "command", "open"}, path, "[editor]")
        config_dir = (_confined(e["config_dir"], directory, path, "[editor] config_dir")
                      if e.get("config_dir") else None)
        editor = FaceEditor(
            package=_require(e, "package", path),
            config_dir=config_dir,
            command=_argv(_require(e, "command", path), path, "[editor] command",
                          {"socket", "glue", *(("config",) if config_dir else ())}),
            open=(None if e.get("open") is None else
                  _argv(e["open"], path, "[editor] open", {"path", "line", "socket", "request"})),
        )

    desktop = None
    if (d := raw.get("desktop")) is not None:
        _reject_unknown(d, {"compositor", "config_dir", "apps", "browser"}, path,
                        "[desktop]")
        desktop = FaceDesktop(
            compositor=_require(d, "compositor", path),
            config_dir=(_confined(d["config_dir"], directory, path, "[desktop] config_dir")
                        if d.get("config_dir") else None),
            apps=tuple(d.get("apps", [])),
            browser=d.get("browser"),
        )

    if editor is None and desktop is None:
        raise DefinitionError(f"{path}: a face with neither [editor] nor [desktop] is nothing")

    if editor and editor.config_dir and not editor.config_dir.is_dir():
        raise DefinitionError(f"{path}: [editor] config_dir '{editor.config_dir}' does not exist")

    if desktop and desktop.config_dir is not None:
        # The editor half's check. A desktop config_dir that points nowhere would otherwise
        # surface as a compositor starting with no configuration at all — a face that comes up looking like somebody else's.
        if not desktop.config_dir.is_dir():
            raise DefinitionError(
                f"{path}: [desktop] config_dir '{desktop.config_dir}' does not exist")
        # `faces.py` mounts the directory at /etc/face and the image's entrypoint reads
        # `<compositor>.conf` from it, so a face declaring one compositor and shipping
        # another's config is a face whose desktop half can never run.
        conf = desktop.config_dir / f"{desktop.compositor}.conf"
        if not conf.is_file():
            raise DefinitionError(
                f"{path}: [desktop] declares compositor '{desktop.compositor}' but "
                f"'{conf.name}' is not in '{desktop.config_dir}'")

    face_id = _require(raw, "id", path)
    if not isinstance(face_id, str) or not PLAIN_ID.fullmatch(face_id):
        raise DefinitionError(f"{path}: id {face_id!r} is not letters, digits, '_', '.' and "
                              "'-' starting with a letter or digit")
    return Face(
        id=face_id,
        name=raw.get("name", raw["id"]),
        directory=directory,
        requires_toolbelt_capabilities=tuple(raw.get("requires_toolbelt_capabilities", [])),
        editor=editor,
        desktop=desktop,
        **_about(raw, path),
    )


# --- Toolbelt -----------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Toolbelt:
    id: str
    name: str
    directory: Path
    supports: tuple[str, ...]
    capabilities: tuple[str, ...]
    packages: tuple[str, ...]
    description: str | None = None
    author: str | None = None
    thumbnail: Path | None = None

    kind = labels.Kind.TOOLBELT

    @property
    def lock_path(self) -> Path:
        return self.directory / "toolbelt.lock"

    @property
    def package_digest(self) -> str:
        """What a lock file is keyed on: the package list and nothing else."""
        joined = "\n".join(sorted(self.packages))
        return "sha256:" + hashlib.sha256(joined.encode()).hexdigest()


def load_toolbelt(directory: Path) -> Toolbelt:
    path = directory / "toolbelt.toml"
    raw = _load_toml(path)
    _reject_unknown(raw, {"id", "name", "supports", "capabilities", "packages",
                          *ABOUT_KEYS}, path, "toolbelt.toml")
    packages = tuple(raw.get("packages", []))
    if not packages:
        raise DefinitionError(f"{path}: a toolbelt with no packages carries no tools")
    return Toolbelt(
        id=_require(raw, "id", path),
        name=raw.get("name", raw["id"]),
        directory=directory,
        supports=tuple(raw.get("supports", [])),
        capabilities=tuple(raw.get("capabilities", [])),
        packages=packages,
        **_about(raw, path),
    )


# --- Body ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class WatchRule:
    path: str
    action: str = "rebuild"


# What a body's tab says its project should take (`[budget]` in body.toml), each in bytes,
# None where it has said nothing: its build caches, the rest of the output its git ignores,
# and the peak working memory of its sandbox. Watched by `disk.py` and `memory.py`.
BUDGET_KINDS = ("caches", "output", "memory")
_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*([KMGT]?)i?B?", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Budget:
    caches: int | None = None
    output: int | None = None
    memory: int | None = None


def _size(value: Any, path: Path, key: str) -> int:
    """`"8G"`, `"512M"`, `"1.5GiB"`: binary units."""
    found = _SIZE.fullmatch(str(value).strip())
    if found is None:
        raise DefinitionError(f"{path}: [budget] {key} = {value!r} is not a size like \"8G\"")
    power = " KMGT".index(found[2].upper() or " ")
    return int(float(found[1]) * 1024 ** power)


def _budget(raw: dict[str, Any], path: Path) -> Budget | None:
    table = raw.get("budget")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise DefinitionError(f"{path}: 'budget' is a table: [budget] with caches, output, memory")
    _reject_unknown(table, set(BUDGET_KINDS), path, "[budget]")
    return Budget(**{k: _size(v, path, k) for k, v in table.items()})


@dataclass(frozen=True, slots=True)
class Body:
    id: str
    name: str
    directory: Path | None
    image: str | None
    dockerfile_name: str | None
    build_target: str | None
    context_name: str | None
    working_copy: Path | None
    command: tuple[str, ...] | None
    ports: tuple[int, ...]
    runtime: str | None
    shell: str | None
    read_only: bool
    environment: dict[str, str] = field(default_factory=dict)
    watch: tuple[WatchRule, ...] = ()
    description: str | None = None
    author: str | None = None
    thumbnail: Path | None = None
    # None: its tab has set no budget yet.
    budget: Budget | None = None

    kind = labels.Kind.BODY

    @property
    def builds_from_source(self) -> bool:
        return self.dockerfile_name is not None

    @property
    def source_root(self) -> Path | None:
        """The one root everything about this body resolves from: the Dockerfile, the
        build context, and the files `develop.watch` lists.

        It is the user's working copy when that exists and the definition's own directory
        otherwise — which are the same thing whenever the definition lives in the project
        repo, as it does. Keeping it to *one* root is what stops the definition
        digest being computed over one copy of `requirements.txt` while the build reads
        another, which would make a dependency change rebuild an image that does not
        contain it.
        """
        if self.working_copy is not None and self.working_copy.is_dir():
            return self.working_copy
        return self.directory

    @property
    def build_context(self) -> Path | None:
        root = self.source_root
        if root is None:
            return None
        return (root / self.context_name).resolve() if self.context_name else root

    @property
    def dockerfile(self) -> Path | None:
        context = self.build_context
        if context is None or self.dockerfile_name is None:
            return None
        return context / self.dockerfile_name

    def base_images(self) -> tuple[str, ...]:
        """Each image the body starts from: its `image`, or each Dockerfile `FROM` that names
        no earlier stage. A global `ARG`'s default is substituted; one with none is left as
        written, since only the build is told its value."""
        if not self.builds_from_source:
            return (self.image,) if self.image else ()
        dockerfile = self.dockerfile
        if dockerfile is None or not dockerfile.is_file():
            return ()
        text = re.sub(r"\\\n", " ", dockerfile.read_text(encoding="utf-8"))
        args: dict[str, str] = {}
        stages: set[str] = set()
        bases: list[str] = []
        for line in text.splitlines():
            words = line.split()
            if not words or words[0].startswith("#"):
                continue
            op = words[0].upper()
            if op == "ARG" and not bases and not stages and len(words) > 1:
                name, _, value = words[1].partition("=")
                args[name] = value.strip("\"'")
            elif op == "FROM":
                rest = [w for w in words[1:] if not w.startswith("--")]
                ref = re.sub(r"\$\{?(\w+)\}?",
                             lambda m: args.get(m.group(1)) or m.group(0), rest[0])
                if ref.lower() not in stages and ref != "scratch":
                    bases.append(ref)
                if len(rest) >= 3 and rest[1].upper() == "AS":
                    stages.add(rest[2].lower())
        return tuple(bases)

    def definition_digest(self, base_image_ref: str | None = None) -> str:
        """A hash over the Dockerfile, the files `develop.watch` lists, the resolved base
        image reference, and what the container runs with. Two bodies with the same
        digest are the same build run the same way, which is what lets concurrent rebuilds
        coalesce onto one job — and what "already current" means, so a change to the
        ports or the command alone is still a change."""
        h = hashlib.sha256()
        h.update(b"raigolmi-body-v2\0")
        h.update(json.dumps([self.command, self.ports, sorted(self.environment.items()),
                             self.read_only, self.shell, self.runtime]).encode())
        h.update(b"\0")
        h.update((base_image_ref or self.image or "").encode())
        h.update(b"\0")
        h.update((self.build_target or "").encode())
        h.update(b"\0")
        dockerfile = self.dockerfile
        if dockerfile is not None and dockerfile.is_file():
            h.update(dockerfile.read_bytes())
        h.update(b"\0")
        for rule in sorted(self.watch, key=lambda r: r.path):
            h.update(rule.path.encode())
            h.update(b"\0")
            for f in sorted(self.watched_files(rule)):
                h.update(str(f).encode())
                h.update(b"\0")
                h.update(f.read_bytes() if f.is_file() else b"")
                h.update(b"\0")
        return "sha256:" + h.hexdigest()

    def watched_files(self, rule: WatchRule) -> list[Path]:
        root = self.source_root
        if root is None:
            return []
        target = (root / rule.path).resolve()
        if target.is_file():
            return [target]
        if target.is_dir():
            return sorted(p for p in target.rglob("*") if p.is_file())
        return []

    def all_watched_files(self) -> list[Path]:
        out: list[Path] = []
        for rule in self.watch:
            out.extend(self.watched_files(rule))
        return out


def _working_copy(value: str, private: tuple[Path, ...], path: Path) -> Path:
    """The user's project, which may be anywhere they keep one, but never where the machine
    keeps its credentials, state and sockets, nor a directory another user owns: the body and
    its tab both mount it, and the body runs as its owner."""
    working_copy = Path(os.path.expandvars(value)).expanduser()
    real = working_copy.resolve()
    for own in private:
        own = own.resolve()
        if real.is_relative_to(own) or own.is_relative_to(real):
            raise DefinitionError(f"{path}: working_copy '{value}' overlaps {own}, which is the "
                                  "machine's own and is never a body's project")
    if real.is_dir() and (owner := real.stat().st_uid) != os.getuid():
        raise DefinitionError(f"{path}: working_copy '{value}' is owned by uid {owner}, not "
                              "this machine's user, and a body runs as its working copy's owner")
    return working_copy


def load_body(directory: Path, private: tuple[Path, ...]) -> Body:
    """`private` is what a working copy may not overlap (`Paths.private`)."""
    path = directory / "body.toml"
    raw = _load_toml(path)
    _reject_unknown(raw, {"id", "name", "image", "dockerfile", "target", "context",
                          "working_copy", "command", "ports", "runtime", "shell",
                          "read_only", "environment", "develop", "budget", *ABOUT_KEYS},
                    path, "body.toml")

    dockerfile_name = raw.get("dockerfile")
    if dockerfile_name is None and not raw.get("image"):
        raise DefinitionError(f"{path}: a body needs either 'image' or 'dockerfile'")

    watch: list[WatchRule] = []
    if (dev := raw.get("develop")) is not None:
        _reject_unknown(dev, {"watch"}, path, "[develop]")
        for entry in dev.get("watch", []):
            _reject_unknown(entry, {"path", "action"}, path, "[[develop.watch]]")
            watch.append(WatchRule(path=_require(entry, "path", path),
                                   action=entry.get("action", "rebuild")))

    working_copy = None
    if wc := raw.get("working_copy"):
        working_copy = _working_copy(wc, private, path)

    body = Body(
        id=_require(raw, "id", path),
        name=raw.get("name", raw["id"]),
        directory=directory,
        image=raw.get("image"),
        dockerfile_name=dockerfile_name,
        build_target=raw.get("target"),
        context_name=raw.get("context") if raw.get("context") not in (None, ".") else None,
        working_copy=working_copy,
        command=tuple(raw["command"]) if raw.get("command") else None,
        ports=tuple(int(p) for p in raw.get("ports", [])),
        runtime=raw.get("runtime"),
        shell=raw.get("shell"),
        read_only=bool(raw.get("read_only", False)),
        environment={str(k): str(v) for k, v in raw.get("environment", {}).items()},
        watch=tuple(watch),
        budget=_budget(raw, path),
        **_about(raw, path),
    )
    # Checked after construction, because where the Dockerfile *is* depends on the working
    # copy, and the message has to name the path that was actually looked at.
    if (root := body.source_root) is not None:
        if body.context_name:
            _confined(body.context_name, root, path, "context")
        if dockerfile_name:
            _confined(str(Path(body.context_name or ".") / dockerfile_name), root, path,
                      "dockerfile")
        for rule in body.watch:
            _confined(rule.path, root, path, "[[develop.watch]] path")
    if body.builds_from_source:
        resolved = body.dockerfile
        if resolved is None or not resolved.is_file():
            raise DefinitionError(
                f"{path}: dockerfile '{dockerfile_name}' does not exist at {resolved}. "
                "It is resolved against the working copy when one is set, because the body "
                "definition lives in the project repo."
            )
    return body


# --- Discovery ----------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SearchPaths:
    faces: tuple[Path, ...]
    toolbelts: tuple[Path, ...]
    bodies: tuple[Path, ...]
    private: tuple[Path, ...]

    @classmethod
    def defaults(cls, repo_root: Path, private: tuple[Path, ...]) -> "SearchPaths":
        def env_paths(var: str, fallback: Path) -> tuple[Path, ...]:
            raw = os.environ.get(var)
            if raw:
                return tuple(Path(p).expanduser() for p in raw.split(os.pathsep) if p)
            return (fallback,)

        return cls(
            faces=env_paths("RAIGOLMID_FACE_PATH", repo_root / "faces"),
            toolbelts=env_paths("RAIGOLMID_TOOLBELT_PATH", repo_root / "toolbelts"),
            bodies=env_paths("RAIGOLMID_BODY_PATH", repo_root / "bodies"),
            private=private,
        )


@dataclass(slots=True)
class Catalogue:
    faces: dict[str, Face] = field(default_factory=dict)
    toolbelts: dict[str, Toolbelt] = field(default_factory=dict)
    bodies: dict[str, Body] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)



def discover(search: SearchPaths) -> Catalogue:
    """A malformed definition is recorded as an error and the others still load — one bad
    face must not hide every toolbelt. The errors are surfaced, never swallowed."""
    cat = Catalogue()
    for roots, marker, loader, table in (
        (search.faces, "face.toml", load_face, cat.faces),
        (search.toolbelts, "toolbelt.toml", load_toolbelt, cat.toolbelts),
        (search.bodies, "body.toml", lambda d: load_body(d, search.private), cat.bodies),
    ):
        for root in roots:
            if not root.is_dir():
                continue
            for child in sorted(root.iterdir()):
                if not (child / marker).is_file():
                    continue
                try:
                    item = loader(child)
                except DefinitionError as exc:
                    cat.errors.append(str(exc))
                    continue
                if item.id in table:
                    cat.errors.append(
                        f"{child / marker}: id '{item.id}' is already defined by "
                        f"{table[item.id].directory}"
                    )
                    continue
                table[item.id] = item
    return cat
