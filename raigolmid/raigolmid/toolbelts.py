"""Turning a package list into a closure.

A toolbelt is a package list, and that is the whole authoring format. It is chosen for
what an agent can write **and verify**, not for what is most expressive: a package list is
wrong in exactly one way — a name that doesn't exist — Nixery names it when the image is
asked for, and the fix is a single token. A hand-written flake has the opposite failure
shape, and the Nix language is the format a model is least reliable in.

Two resolution paths, in order:

1. **Nixery** (normal): `nixery.dev/shell/<pkg>/<pkg>/…`. No Nix language anywhere.
2. **Generated flake** (escape hatch): for a package Nixery can't serve. Generated from
   the same list, never hand-written, and never written by an agent.

Reproducibility comes from `toolbelt.lock`, written after a successful build, not from the
authoring format.
"""
from __future__ import annotations

import difflib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import flakes
from .definitions import Toolbelt
from .runtime.base import ContainerRuntime, ImageInfo, RuntimeError_

NIXERY = "nixery.dev"

# The session view's entrypoint needs these before it can build anything: `capsh` to drop
# capabilities and set no_new_privs, `python3` to run the launcher, and
# coreutils/util-linux for `stat`, `mount` and `pivot_root`. They are checked rather than
# silently added, because a toolbelt is the user's file and a view that dies in its
# entrypoint is a worse answer than being told which package is missing.
REQUIRED_VIEW_PACKAGES = ("libcap", "python3", "coreutils", "util-linux")
INDEX_URL = "https://channels.nixos.org/nixpkgs-unstable/packages.json.br"
INDEX_MAX_AGE = 14 * 24 * 3600
MANIFEST_TYPES = ("application/vnd.docker.distribution.manifest.v2+json, "
                  "application/vnd.oci.image.manifest.v1+json")


class ToolbeltError(Exception):
    pass


class MissingViewPackages(ToolbeltError):
    """A toolbelt that cannot build a session view. Raised at selection time, not when the
    view's entrypoint fails halfway through a pivot_root."""

    def __init__(self, toolbelt_id: str, missing: tuple[str, ...]) -> None:
        self.missing = missing
        super().__init__(
            f"toolbelt '{toolbelt_id}' cannot build a session view: it is missing "
            f"{', '.join(missing)}. The view's entrypoint needs capsh to drop "
            "capabilities, python3 to run the launcher, and coreutils/util-linux to build "
            "the mount tree. Add them to its packages list."
        )


# --- the package index ------------------------------------------------------------------

class PackageIndex:
    """nixpkgs holds ~100k packages and a model will confidently produce names that don't
    exist. `search_packages` finds names in the index first.

    ⚠ A hint, never the check: nixery.dev builds from a nixos-unstable snapshot pinned by
    hand and reported nowhere, and this index is today's nixpkgs-unstable, so it can list a
    name Nixery cannot build and miss one it can. Nixery's own answer is the check
    (`pull_from_nixery`).

    The index is a name list cached on disk. It is fetched, never guessed: an index that
    cannot be obtained makes `search` raise rather than quietly answering "not found",
    because "this package does not exist" and "I could not check" lead an agent to
    completely different next actions.
    """

    def __init__(self, cache_path: Path, url: str = INDEX_URL,
                 max_age: float = INDEX_MAX_AGE) -> None:
        self.cache_path = cache_path
        self.url = url
        self.max_age = max_age
        self._names: list[str] | None = None

    def _fresh_cache(self) -> bool:
        if not self.cache_path.exists():
            return False
        return (time.time() - self.cache_path.stat().st_mtime) < self.max_age

    def load(self, refresh: bool = False) -> list[str]:
        if self._names is not None and not refresh:
            return self._names
        if not refresh and self._fresh_cache():
            try:
                self._names = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                raise ToolbeltError(
                    f"the package index cache at {self.cache_path} is unreadable ({exc}). "
                    "Delete it to have it fetched again."
                ) from exc
        else:
            self._names = self._fetch()
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self._names), encoding="utf-8")
        return self._names

    def _fetch(self) -> list[str]:
        try:
            with urllib.request.urlopen(self.url, timeout=120) as response:
                raw = response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ToolbeltError(
                f"could not fetch the nixpkgs package index from {self.url}: {exc}. "
                "`search_packages` cannot answer without it, and will not guess. "
                "Fix the network or place a name list at "
                f"{self.cache_path} as a JSON array of strings."
            ) from exc
        if self.url.endswith(".br"):
            try:
                import brotli                       # type: ignore[import-not-found]
                raw = brotli.decompress(raw)
            except ImportError as exc:
                raise ToolbeltError(
                    "the nixpkgs index is brotli-compressed and the `brotli` module is "
                    "not installed. `pip install brotli`, or point INDEX_URL at an "
                    "uncompressed packages.json."
                ) from exc
        data = json.loads(raw)
        packages = data.get("packages", data)
        return sorted(packages.keys())

    def search(self, query: str, limit: int = 20) -> list[str]:
        names = self.load()
        exact = [n for n in names if n == query]
        contains = [n for n in names if query.lower() in n.lower() and n != query]
        if len(contains) < limit:
            near = difflib.get_close_matches(query, names, n=limit, cutoff=0.7)
            contains.extend(n for n in near if n not in contains and n != query)
        return (exact + contains)[:limit]


# --- resolution ----------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Closure:
    """A resolved toolbelt: the image that carries it and how it was resolved."""
    image: str
    method: str                       # "nixery" | "flake"
    packages: tuple[str, ...]
    package_digest: str
    store_paths: tuple[str, ...] = ()
    # The nixpkgs commit a flake-built image is from; None for Nixery's.
    nixpkgs: str | None = None


@dataclass(slots=True)
class Lock:
    """`toolbelt.lock`, written next to the definition after a successful build. This is
    what a hand-written flake would have bought, obtained without asking an agent to
    author Nix."""
    package_digest: str
    image: str
    method: str
    packages: list[str]
    store_paths: list[str] = field(default_factory=list)
    resolved_at: float = 0.0
    nixpkgs: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"package_digest": self.package_digest, "image": self.image,
                "method": self.method, "packages": self.packages,
                "store_paths": self.store_paths, "resolved_at": self.resolved_at,
                **({"nixpkgs": self.nixpkgs} if self.nixpkgs else {})}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Lock":
        return cls(package_digest=d["package_digest"], image=d["image"],
                   method=d.get("method", "nixery"), packages=d.get("packages", []),
                   store_paths=d.get("store_paths", []),
                   resolved_at=d.get("resolved_at", 0.0), nixpkgs=d.get("nixpkgs"))


def nixery_reference(packages: tuple[str, ...], registry: str = NIXERY) -> str:
    """`nixery.dev/shell/<pkg>/<pkg>/…`. The `shell` prefix is what makes the image carry
    a usable environment rather than a bare closure.

    Lowercased, because a Docker repository name may not contain uppercase and plenty of
    nixpkgs attributes do — `bashInteractive`, for one. A definition keeps
    the real attribute name, which is what the package index lists; only the
    image reference is folded. Nixery resolves the folded name to the same closure."""
    if not packages:
        raise ToolbeltError("a toolbelt with no packages has no image to resolve to")
    return f"{registry}/shell/" + "/".join(packages).lower()


class NixeryRefused(RuntimeError_):
    """Nixery answered, and its answer was that it cannot build this list — not a pull that
    failed on the way. Only this sends a toolbelt to the flake escape hatch (`flakes.py`)."""


def pull_from_nixery(runtime: ContainerRuntime, reference: str) -> ImageInfo:
    """Pulls a Nixery image; a refused pull carries Nixery's own answer.

    Docker reports a name Nixery cannot build as `not found` and nothing more, while
    Nixery's registry names the package (`Could not find Nix packages: [name]`). That
    answer is the name check, so it is asked for when the pull fails.
    """
    try:
        return runtime.pull(reference)
    except RuntimeError_ as exc:
        said, refused = _nixery_answer(reference)
        raise (NixeryRefused if refused else RuntimeError_)(
            f"{exc}. Nixery says: {said}") from exc


def nixery_says(reference: str) -> str:
    """What Nixery's registry answers for `reference`'s manifest, in its own words."""
    return _nixery_answer(reference)[0]


def _nixery_answer(reference: str) -> tuple[str, bool]:
    """Nixery's words for `reference`, and whether they are a refusal of the list."""
    registry, _, rest = reference.partition("/")
    if "@" in rest:
        repository, _, tag = rest.partition("@")
    elif ":" in rest.rsplit("/", 1)[-1]:
        repository, _, tag = rest.rpartition(":")
    else:
        repository, tag = rest, "latest"
    request = urllib.request.Request(f"https://{registry}/v2/{repository}/manifests/{tag}",
                                     headers={"Accept": MANIFEST_TYPES})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return (f"it serves {reference} (HTTP {response.status}), so the pull failed on "
                    "this side"), False
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace").strip()
        try:
            return "; ".join(error["message"] for error in json.loads(body)["errors"]), True
        except (json.JSONDecodeError, KeyError, TypeError):
            return f"HTTP {exc.code}: {body[:500]}", False
    except urllib.error.URLError as exc:
        return f"nothing: it could not be reached ({exc.reason})", False
    except (TimeoutError, OSError) as exc:
        return f"nothing: it could not be asked ({exc})", False


class ToolbeltResolver:
    def __init__(self, registry: str = NIXERY, flakes: Path | None = None) -> None:
        self.registry = registry
        # Where flake builds are kept (`flakes.py`): one made for a list Nixery refused is the
        # toolbelt's image until a view has worked on it and the lock says so.
        self.flakes = flakes

    def read_lock(self, toolbelt: Toolbelt) -> Lock | None:
        path = toolbelt.lock_path
        if not path.is_file():
            return None
        try:
            return Lock.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, KeyError, OSError) as exc:
            raise ToolbeltError(f"{path} is not a readable lock file: {exc}") from exc

    def write_lock(self, toolbelt: Toolbelt, closure: Closure) -> Lock:
        """Written only when what it pins changes. The definition directory is watched, so a
        view built from its own lock that rewrote it would set off a rediscover each time."""
        lock = Lock(package_digest=closure.package_digest, image=closure.image,
                    method=closure.method, packages=list(closure.packages),
                    store_paths=list(closure.store_paths), resolved_at=time.time(),
                    nixpkgs=closure.nixpkgs)
        current = self.read_lock(toolbelt)
        if current is not None and replace(current, resolved_at=0.0) == replace(
                lock, resolved_at=0.0):
            return current
        toolbelt.lock_path.write_text(
            json.dumps(lock.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return lock

    @staticmethod
    def check_view_packages(toolbelt: Toolbelt) -> None:
        missing = tuple(p for p in REQUIRED_VIEW_PACKAGES if p not in toolbelt.packages)
        if missing:
            raise MissingViewPackages(toolbelt.id, missing)

    def resolve(self, toolbelt: Toolbelt) -> Closure:
        """Rebuilds use the lock until the package list changes."""
        self.check_view_packages(toolbelt)
        lock = self.read_lock(toolbelt)
        if lock is not None and lock.package_digest == toolbelt.package_digest:
            return Closure(image=lock.image, method=lock.method,
                           packages=tuple(lock.packages),
                           package_digest=lock.package_digest,
                           store_paths=tuple(lock.store_paths), nixpkgs=lock.nixpkgs)
        built = flakes.record_of(self.flakes, toolbelt) if self.flakes is not None else None
        if built is not None:
            return Closure(image=built.image, method="flake", packages=toolbelt.packages,
                           package_digest=toolbelt.package_digest, nixpkgs=built.nixpkgs)

        return Closure(
            image=nixery_reference(toolbelt.packages, self.registry),
            method="nixery",
            packages=toolbelt.packages,
            package_digest=toolbelt.package_digest,
        )
