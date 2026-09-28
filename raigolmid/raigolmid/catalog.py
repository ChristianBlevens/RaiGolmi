"""The catalog: every face, toolbelt and body — on this machine or on the registry — with
what it is, whose it is, and how far it has come here.

    server      listed by the registry, not on this machine
    downloaded  its definition is here
    installed   its image is built (a body's image, a toolbelt's Nixery image, a face's
                compositor and apps images)

Download fetches the registry's entry into the definitions; install builds or pulls its
images; delete takes the images first and the definition after, and refuses either while
anything uses it; upload sends `layerfiles.upload_choice`'s files less what the user left out
(`.uploadignore`). Install and upload are long, so they run on the catalog's own queue and are told through events
(`catalog.*`); nobody waits on them, so a failure is said there or not at all.

Where a downloaded layer came from is kept in `Paths.registry_state`, never in the layer's
directory, whose files are only what its build reads.
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from . import credential, flakes, hostimages, layerfiles, naming, registry
from .definitions import Body, Face, Toolbelt
from .runtime.base import ImageInUse
from .toolbelts import nixery_reference

if TYPE_CHECKING:
    from .session import Session

QUEUE = "catalog"
# How long one read of the registry's index answers for: the window re-lists every few
# seconds, and the index changes only when an upload is merged.
INDEX_SECONDS = 300.0


class CatalogError(RuntimeError):
    pass


class Catalog:
    def __init__(self, session: "Session",
                 make_registry: Callable[[Callable[[], str]], registry.Registry] | None = None
                 ) -> None:
        self.session = session
        self.paths = session.paths
        self.registry = (make_registry or (lambda token: registry.GitHubRegistry(token)))(
            self._token)
        self._index: tuple[float, list[registry.RegistryEntry]] | None = None
        self._lock = threading.Lock()
        # What is happening to each entry, or last happened, keyed "kind/id": the window
        # shows it on the entry, since nobody waits on the queued job itself.
        self._activity: dict[str, dict[str, Any]] = {}

    def _did(self, kind: str, layer_id: str, what: str, **detail: Any) -> None:
        with self._lock:
            self._activity[f"{kind}/{layer_id}"] = {"what": what, **detail}

    # --- what is known ---------------------------------------------------------------------
    def _token(self) -> str:
        try:
            return credential.read(self.paths.registry_token,
                                   credential.REGISTRY_KEYS)["GITHUB_TOKEN"]
        except credential.CredentialError as exc:
            raise CatalogError(str(exc)) from exc

    def _provenance(self) -> dict[str, dict[str, Any]]:
        path = self.paths.registry_state
        return json.loads(path.read_text()) if path.is_file() else {}

    def _save_provenance(self, data: dict[str, dict[str, Any]]) -> None:
        path = self.paths.registry_state
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(path)

    def _server_index(self) -> list[registry.RegistryEntry]:
        with self._lock:
            if self._index is not None and time.monotonic() - self._index[0] < INDEX_SECONDS:
                return self._index[1]
        entries = self.registry.index()
        with self._lock:
            self._index = (time.monotonic(), entries)
        return entries

    def _layer(self, kind: str, layer_id: str) -> Face | Toolbelt | Body:
        table = {"face": self.session.catalogue.faces, "toolbelt": self.session.catalogue.toolbelts,
                 "body": self.session.catalogue.bodies}.get(kind)
        if table is None:
            raise CatalogError(f"{kind!r} is not face, toolbelt or body")
        layer = table.get(layer_id)
        if layer is None or layer.directory is None:
            raise CatalogError(f"no {kind} '{layer_id}' is defined on this machine")
        return layer

    def _root(self, kind: str) -> Path:
        roots = {"face": self.session.search.faces, "toolbelt": self.session.search.toolbelts,
                 "body": self.session.search.bodies}[kind]
        if not roots:
            raise CatalogError(f"this machine has no directory for {kind} definitions")
        return Path(roots[0])

    # --- images ----------------------------------------------------------------------------
    def _body_images(self, body: Body) -> list[str]:
        if not body.builds_from_source:
            return [body.image] if body.image and self.session.runtime.image(body.image) else []
        repo = naming.build_tag(body.id, "sha256:0").rsplit(":", 1)[0]
        return sorted(tag for info in self.session.runtime.list_images()
                      for tag in info.tags if tag.rsplit(":", 1)[0] == repo)

    def _toolbelt_images(self, toolbelt: Toolbelt) -> list[str]:
        lock = self.session.resolver.read_lock(toolbelt)
        refs = [lock.image] if lock is not None else []
        refs.append(nixery_reference(toolbelt.packages))
        # A flake build no view has locked yet (`flakes.py`).
        built = flakes.record_of(self.session.resolver.flakes, toolbelt)
        if built is not None:
            refs.append(built.image)
        return [ref for ref in dict.fromkeys(refs) if self.session.runtime.image(ref) is not None]

    def _face_images(self, face: Face) -> tuple[list[str], bool]:
        """Its images here, and whether both halves are."""
        runtime, found = self.session.runtime, []
        apps = self.session.faces.apps_reference(face)
        if runtime.image(apps) is not None:
            found.append(apps)
        compositor = None
        if face.desktop is not None:
            compositor = hostimages.face_compositor(face.directory.parent, face.desktop.compositor)
            try:
                if hostimages.present(runtime, compositor):
                    found.append(compositor.tag())
            except hostimages.HostImageError as exc:
                raise CatalogError(f"face '{face.id}' cannot be installed: {exc}") from exc
        return found, (apps in found) and (compositor is None or compositor.tag() in found)

    def _installed(self, layer) -> bool:
        if isinstance(layer, Body):
            return bool(self._body_images(layer))
        if isinstance(layer, Toolbelt):
            return bool(self._toolbelt_images(layer))
        return self._face_images(layer)[1]

    def _in_use(self, layer) -> str | None:
        """What holds the layer, said the way the window shows it, or None."""
        session, intent = self.session, self.session.intent
        if isinstance(layer, Face):
            if intent.selection.face == layer.id:
                return "it is the face on screen"
            trial = session.faces.trial()
            if trial is not None and trial.face_id == layer.id:
                return "a trial of it is running"
        elif isinstance(layer, Body):
            if intent.selection.body == layer.id:
                return "it is the selected body"
            if any(i.body == layer.id for i in intent.instances.values()):
                return "a sandbox is built from it"
        else:
            if session.active_toolbelt() == layer.id:
                return "it is the active toolbelt"
            if any(i.toolbelt == layer.id for i in intent.instances.values()):
                return "a sandbox's view runs it"
        return None

    # --- the listing -----------------------------------------------------------------------
    def listing(self, server: bool = False) -> dict[str, Any]:
        provenance = self._provenance()
        catalogue = self.session.catalogue
        entries, here = [], set()
        for kind, table in (("face", catalogue.faces), ("toolbelt", catalogue.toolbelts),
                            ("body", catalogue.bodies)):
            for layer in table.values():
                if layer.directory is None:
                    continue
                came = provenance.get(f"{kind}/{layer.id}")
                here.add((kind, layer.id))
                # One layer that cannot be read is said on its own entry, never by
                # failing the listing every other layer is in.
                try:
                    state, problem = ("installed" if self._installed(layer) else "downloaded"), None
                except CatalogError as exc:
                    state, problem = "downloaded", str(exc)
                entries.append({
                    "kind": kind, "id": layer.id, "name": layer.name,
                    "description": layer.description,
                    "author": came["author"] if came else layer.author,
                    "thumbnail": self._thumbnail_local(kind, layer),
                    "state": state, "problem": problem,
                    "in_use": self._in_use(layer),
                    "authored": self._mine(came),
                    "downloads": None,
                    "activity": self._activity.get(f"{kind}/{layer.id}"),
                })
        out: dict[str, Any] = {"entries": entries, "server_error": None}
        if server:
            try:
                index = self._server_index()
            except registry.RegistryError as exc:
                out["server_error"] = str(exc)
                return out
            by_key = {(e.kind, e.id): e for e in index}
            for entry in entries:
                found = by_key.get((entry["kind"], entry["id"]))
                if found is not None:
                    entry["downloads"] = found.downloads
            for e in index:
                if (e.kind, e.id) in here:
                    continue
                entries.append({"kind": e.kind, "id": e.id, "name": e.name,
                                "description": e.description, "author": e.author,
                                "thumbnail": None, "state": "server", "problem": None,
                                "in_use": None,
                                "authored": False, "downloads": e.downloads,
                                "activity": self._activity.get(f"{e.kind}/{e.id}")})
        return out

    # --- thumbnails, where a surface container can read them -----------------------------
    def _thumbnails(self) -> Path:
        directory = self.paths.runtime / "raigolmid-catalog"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _thumbnail_local(self, kind: str, layer) -> str | None:
        if layer.thumbnail is None:
            return None
        target = self._thumbnails() / f"{kind}-{layer.id}.png"
        source = layer.thumbnail
        if not target.is_file() or target.stat().st_mtime < source.stat().st_mtime:
            shutil.copyfile(source, target)
        return str(target)

    def thumbnail(self, kind: str, layer_id: str) -> str | None:
        """A server entry's thumbnail, fetched once per version."""
        entry = self._entry(kind, layer_id)
        if entry.thumbnail is None:
            return None
        target = self._thumbnails() / f"{kind}-{layer_id}-v{entry.version}.png"
        if not target.is_file():
            target.write_bytes(self.registry.thumbnail(entry))
        return str(target)

    def _entry(self, kind: str, layer_id: str) -> registry.RegistryEntry:
        for e in self._server_index():
            if (e.kind, e.id) == (kind, layer_id):
                return e
        raise CatalogError(f"the registry lists no {kind} '{layer_id}'")

    # --- download --------------------------------------------------------------------------
    def download(self, kind: str, layer_id: str) -> dict[str, Any]:
        entry = self._entry(kind, layer_id)
        root = self._root(kind)
        destination = root / layer_id
        if destination.exists():
            raise CatalogError(f"{destination} already exists")
        names = registry.unpack(self.registry.fetch(entry), destination)
        if kind == "face":
            self._place_compositors(destination)
        provenance = self._provenance()
        provenance[f"{kind}/{layer_id}"] = {"author": entry.author, "version": entry.version}
        self._save_provenance(provenance)
        self.session.rediscover()
        self.session.events.emit("catalog.downloaded", kind=kind, id=layer_id,
                                 version=entry.version, files=len(names))
        return {"kind": kind, "id": layer_id, "state": "downloaded"}

    def _place_compositors(self, face_dir: Path) -> None:
        """A face carries its compositor's files; they belong beside the faces, shared."""
        carried = face_dir / "_compositors"
        if not carried.is_dir():
            return
        shared = face_dir.parent / "_compositors"
        for compositor in carried.iterdir():
            target = shared / compositor.name
            if target.exists():
                if not _same_tree(compositor, target):
                    shutil.rmtree(face_dir)
                    raise CatalogError(
                        f"the face's compositor '{compositor.name}' differs from the one at "
                        f"{target}; the download is undone rather than replace it")
            else:
                shared.mkdir(parents=True, exist_ok=True)
                shutil.copytree(compositor, target)
        shutil.rmtree(carried)

    # --- install ---------------------------------------------------------------------------
    def install(self, kind: str, layer_id: str) -> dict[str, Any]:
        layer = self._layer(kind, layer_id)
        self._did(kind, layer_id, "installing")
        self.session.events.emit("catalog.installing", kind=kind, id=layer_id)
        self.session.queues.submit(QUEUE, lambda: self._install(kind, layer), "install")
        return {"kind": kind, "id": layer_id, "state": "installing"}

    def _install(self, kind: str, layer) -> None:
        session = self.session
        try:
            if isinstance(layer, Body):
                if layer.builds_from_source:
                    digest, fresh = session.instances.body_digest(layer)
                    session.builds.build(str(layer.source_root), digest,
                                         lambda: session.instances.build_image(layer, digest, fresh),
                                         timeout=3600)
                elif session.runtime.image(layer.image) is None:
                    session.runtime.pull(layer.image)
            elif isinstance(layer, Toolbelt):
                # Nixery's image, or the flake escape hatch's for a list Nixery refuses.
                session.instances.fetch_toolbelt(layer)
                session.closures.of_image(session.resolver.resolve(layer).image)
            else:
                if layer.desktop is not None:
                    session.faces.image_for(layer, layer.desktop)
                session.faces.apps_closure(layer)
        except Exception as exc:                        # noqa: BLE001
            # Nobody waits on this job: its failure is said here or not at all.
            error = f"{type(exc).__name__}: {exc}"
            self._did(kind, layer.id, "install_failed", error=error)
            session.events.emit("catalog.install_failed", kind=kind, id=layer.id, error=error)
            return
        self._did(kind, layer.id, "installed")
        session.events.emit("catalog.installed", kind=kind, id=layer.id)

    # --- delete: the images first, the definition after ------------------------------------
    def delete(self, kind: str, layer_id: str) -> dict[str, Any]:
        layer = self._layer(kind, layer_id)
        held = self._in_use(layer)
        if held is not None:
            raise CatalogError(f"{kind} '{layer_id}' is in use: {held}")
        if self._installed(layer):
            removed = self._remove_images(kind, layer)
            self.session.events.emit("catalog.deleted", kind=kind, id=layer_id,
                                     what="images", images=removed)
            return {"kind": kind, "id": layer_id, "state": "downloaded", "removed": removed}
        shutil.rmtree(layer.directory)
        provenance = self._provenance()
        if provenance.pop(f"{kind}/{layer_id}", None) is not None:
            self._save_provenance(provenance)
        self.session.rediscover()
        self.session.events.emit("catalog.deleted", kind=kind, id=layer_id, what="definition")
        return {"kind": kind, "id": layer_id, "state": None}

    def _remove_images(self, kind: str, layer) -> list[str]:
        if isinstance(layer, Body):
            refs = self._body_images(layer)
        elif isinstance(layer, Toolbelt):
            refs = self._toolbelt_images(layer)
        else:
            refs, _ = self._face_images(layer)
            if layer.desktop is not None:
                compositor = hostimages.face_compositor(layer.directory.parent,
                                                        layer.desktop.compositor).tag()
                sharing = [f.id for f in self.session.catalogue.faces.values()
                           if f.id != layer.id and f.desktop is not None
                           and f.desktop.compositor == layer.desktop.compositor]
                if sharing and compositor in refs:
                    refs.remove(compositor)     # the other faces' too
        removed = []
        for ref in refs:
            try:
                self.session.runtime.remove_image(ref)
            except ImageInUse as exc:
                raise CatalogError(f"{ref} is in use by a container, so {kind} "
                                   f"'{layer.id}' keeps it: {exc}") from exc
            removed.append(ref)
        return removed

    # --- upload ----------------------------------------------------------------------------
    def upload_files(self, kind: str, layer_id: str) -> dict[str, Any]:
        """What an upload could send, for the user to read and choose from before it goes:
        each file, whether a build reads it (and so it goes), and whether they left it out last
        time."""
        layer = self._layer(kind, layer_id)
        self._authored(kind, layer_id)
        try:
            files, required, excluded = layerfiles.upload_choice(layer)
        except layerfiles.LayerFilesError as exc:
            raise CatalogError(str(exc)) from exc
        return {"kind": kind, "id": layer_id,
                "files": [{"name": n, "bytes": p.stat().st_size, "required": n in required,
                           "excluded": n in excluded} for n, p in sorted(files.items())]}

    def upload(self, kind: str, layer_id: str,
               excluded: list[str] | None = None) -> dict[str, Any]:
        """Send what the user chose; `excluded`, when given, is saved as their choice first."""
        layer = self._layer(kind, layer_id)
        self._authored(kind, layer_id)
        try:
            if excluded is not None:
                layerfiles.choose(layer, set(excluded))
            everything, _, left_out = layerfiles.upload_choice(layer)
        except layerfiles.LayerFilesError as exc:
            raise CatalogError(str(exc)) from exc
        files = {name: path for name, path in everything.items() if name not in left_out}
        self._token()                                   # refused here, not on the queue
        self._did(kind, layer_id, "uploading")
        self.session.events.emit("catalog.uploading", kind=kind, id=layer_id, files=len(files))
        self.session.queues.submit(QUEUE, lambda: self._upload(kind, layer_id, files), "upload")
        return {"kind": kind, "id": layer_id, "state": "uploading"}

    def _upload(self, kind: str, layer_id: str, files: dict[str, Path]) -> None:
        try:
            url = self.registry.upload(kind, layer_id, files)
        except Exception as exc:                        # noqa: BLE001
            # Nobody waits on this job: its failure is said here or not at all.
            error = f"{type(exc).__name__}: {exc}"
            self._did(kind, layer_id, "upload_failed", error=error)
            self.session.events.emit("catalog.upload_failed", kind=kind, id=layer_id, error=error)
            return
        self._did(kind, layer_id, "uploaded", pull_request=url)
        self.session.events.emit("catalog.uploaded", kind=kind, id=layer_id, pull_request=url)

    def _mine(self, came: dict[str, Any] | None) -> bool:
        """Made here, or downloaded from the user's own upload. Without a token their login
        is not known, and a downloaded layer is not shown as theirs."""
        if came is None:
            return True
        try:
            return self.registry.login() == came["author"]
        except (CatalogError, registry.RegistryError):
            return False

    def _authored(self, kind: str, layer_id: str) -> None:
        came = self._provenance().get(f"{kind}/{layer_id}")
        if not self._mine(came):
            raise CatalogError(f"{kind} '{layer_id}' was downloaded from {came['author']}; "
                               "only its author uploads it")


def _same_tree(a: Path, b: Path) -> bool:
    files_a = {p.relative_to(a): p for p in a.rglob("*") if p.is_file()}
    files_b = {p.relative_to(b): p for p in b.rglob("*") if p.is_file()}
    return files_a.keys() == files_b.keys() and all(
        files_a[k].read_bytes() == files_b[k].read_bytes() for k in files_a)
