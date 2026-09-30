"""The session: what is selected, what is running, and who owns it.

This is the object every API method lands on. It holds the catalogue, the
intent, the instances and the queues, and it is where the rules that span them live:

* **Every state is intentional**. Faces and bodies never constrain each other,
  so any selection is a working state and builds nothing. What is judged, with a
  reason, is the toolbelt a tab's sandbox opens with.
* **A machine tab and one tab per body**. The machine tab is always there; a body's
  tab opens when the body is selected and stays until the user closes it. One tab per body is
  what makes sharing their working copy safe: the agents writing a body's tree number zero or
  one.
* **Sandbox lifetime is derived from references, not locks**. A sandbox runs while its
  tab holds it, whatever is selected; the face is on the selected body's tab's.
* **Mutating operations are serialized per instance; builds are coalesced per definition**.
"""
from __future__ import annotations

import dataclasses
import os
import re
import socket
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (activity, boot, compatibility, compose, credential, documents, flakes, git,
               hostsurfaces, keep, labels, naming)
from .agents import AgentError, Agents, AgentSpec, definition_protection
from .anchors import DEFAULT_ANCHOR_IMAGE, Anchors
from .catalog import Catalog
from .closures import Closures
from .definitions import Body, Catalogue, Face, SearchPaths, Toolbelt, discover
from .doors import Door, DoorError
from .events import EventLog
from .faces import FaceError, Faces
from .facemounts import FaceMounts, FaceMountError
from .instances import Instance, Instances, RebuildResult
from .intent import MANAGER, Intent, InstanceIntent, IntentStore, Run, StopRecord, TabIntent
from .launcher import LauncherError, LauncherUnreachable
from .paths import Paths
from .presence import Presence, PresenceError
from .queues import BuildLock, BuildOutcome, Debouncer, InstanceQueues, Superseded
from .reconcile import Reconciler, Report, mark_crashed
from .runtime import ContainerRuntime, ImageInUse, RuntimeError_
from .supervisor import HOST_SURFACES, SANDBOX_PARTS, Supervisor, Unit
from .toolbelts import NIXERY, PackageIndex, ToolbeltResolver, nixery_reference
from .views import Views

# An agent's input waits for the user's pause this long; past it the agent is told they are
# using the machine, so its turn never hangs on them.
INPUT_PATIENCE = 30.0


class SessionError(Exception):
    pass


class NotSelectable(SessionError):
    """A selection was refused with the reason the selector shows on focus."""


@dataclass(frozen=True, slots=True)
class Place:
    """Where a tab works (`Session._place`): its sandbox's id, open or not, and the body and
    working copy that sandbox is, or would be, of."""
    sandbox: str
    body: str | None
    working_copy: Path
    branch: str


@dataclass(frozen=True, slots=True)
class RebuildReport:
    result: RebuildResult
    instance: str
    digest: str
    seconds: float = 0.0
    log: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = {"result": self.result, "instance": self.instance, "digest": self.digest,
             "seconds": round(self.seconds, 2)}
        if self.log:
            d["log"] = self.log[-8000:]
        if self.reason:
            d["reason"] = self.reason
        return d


class Session:
    def __init__(self, runtime: ContainerRuntime, paths: Paths, search: SearchPaths,
                 events: EventLog, epoch: int,
                 compose_cli: compose.ComposeCLI | None = None,
                 anchor_image: str | None = None) -> None:
        self.runtime = runtime
        self.paths = paths
        self.search = search
        self.events = events
        self.epoch = epoch

        self.catalogue: Catalogue = discover(search)
        self.store = IntentStore(paths.intent)
        self.intent: Intent = self.store.load(
            p.name for d in (paths.agent_homes, paths.agent_archive) if d.is_dir()
            for p in d.iterdir())
        self.intent.epoch = epoch

        self.anchors = Anchors(runtime, epoch, image=anchor_image or DEFAULT_ANCHOR_IMAGE)
        self.views = Views(runtime, paths, epoch)
        self.index = PackageIndex(paths.data / "nixpkgs-names.json")
        self.resolver = ToolbeltResolver(flakes=paths.state / "flakes")
        self.compose_cli = compose_cli or compose.ComposeCLI()
        self.closures = Closures(runtime, paths, events, epoch)
        self.face_mounts = FaceMounts(runtime, events, epoch, self.closures,
                                      working_copy=self._face_work,
                                      focused_view=paths.focused_view,
                                      face_runtimes=paths.face_runtimes())
        self.instances = Instances(runtime, paths, events, self.anchors, self.views,
                                   self.face_mounts, self.resolver, self.compose_cli, epoch)
        self.agents = Agents(runtime, paths, events, epoch)
        self._read_agent_activity()
        self.supervisor = Supervisor(runtime, paths, events, prune=self._prune_kept)
        self.faces = Faces(runtime, paths, events, epoch=epoch, closures=self.closures)
        self.presence = Presence(events, paths.runtime, os.environ.get("WAYLAND_DISPLAY"))
        # The tab that last drove the user's face; it holds the face while that turn goes on, and
        # another tab's input waits on `_face_free` until it ends.
        self._driver: str | None = None
        self._face_free = threading.Condition()
        # The machine tab's face off the user's screen: one trial, started and stopped in turn.
        self._trial_lock = threading.Lock()
        self.door = Door(runtime, events, epoch)
        self._door_failure: str | None = None
        self.reconciler = Reconciler(runtime, self.views, self.instances, events, epoch)

        self.queues = InstanceQueues()
        self.builds = BuildLock()
        self.debouncer = Debouncer()
        self._lock = threading.RLock()
        self._face_failure: str | None = None
        # A face started with an editor whose window waits for the face's first sync.
        self._editor_window_due: Face | None = None
        self.catalog = Catalog(self)

    # --- catalogue -------------------------------------------------------------------
    def collect_garbage(self) -> None:
        """Removes the toolbelt images — Nixery's and flake builds' — and closures nothing
        names: no container, no toolbelt (its lock, its package list now, or its flake build)
        and no face's apps. Each edit to a package list makes a new image and closure, and
        nothing else would ever remove the old ones; the build cache only they held goes too.
        A body's images go once no body names them — built for a body with no definition any
        more, or pulled as the image of one that names another now — for an image nothing
        claims is abandoned; a newer build of a body still defined replaces its older ones as
        it is built (`superseded.py`).

        ⚠ Only at the daemon's start, before anything can fetch: a fetch holds an image nothing
        names yet until its view exists (`toolbelt_swap` fetches before it records).
        """
        keep: set[str] = set()
        for container in self.runtime.list(labels.managed_filter()):
            keep.add(container.image_id)
            if labels.FACE_CLOSURE in container.labels:
                keep.add(container.labels[labels.FACE_CLOSURE])
        references = [self.faces.apps_reference(face) for face in self.catalogue.faces.values()]
        for toolbelt in self.catalogue.toolbelts.values():
            lock = self.resolver.read_lock(toolbelt)
            if lock is not None:
                references.append(lock.image)
            references.append(nixery_reference(toolbelt.packages))
            built = flakes.record_of(self.resolver.flakes, toolbelt)
            if built is not None:
                references.append(built.image)
        for reference in references:
            image = self.runtime.image(reference)
            if image is not None:
                keep.add(image.id)
        images = self._collect_body_images(keep)
        for image in self.runtime.list_images():
            names = image.tags or image.repo_digests
            if (image.id in keep or not names
                    or not all(name.startswith((f"{NIXERY}/", flakes.IMAGE_PREFIX))
                               for name in names)):
                continue
            try:
                for name in names:
                    self.runtime.remove_image(name)
            except ImageInUse:
                continue  # a container outside the daemon's labels still runs it
            images += 1
        if images:
            self.runtime.prune_build_cache()
        closures, store_paths = self.closures.collect(keep)
        self.events.emit("garbage.collected", images=images, closures=closures,
                         store_paths=store_paths)

    def _collect_body_images(self, keep: set[str]) -> int:
        """`collect_garbage`'s bodies: how many images went. One a container outside the
        daemon's labels still runs is left for the next start."""
        bodies = self.catalogue.bodies.values()
        defined = {naming.body_repository(body.id) for body in bodies}
        named = {body.image for body in bodies if not body.builds_from_source and body.image}
        abandoned = [tag for image in self.runtime.list_images() if image.id not in keep
                     for tag in image.tags
                     if tag.startswith(naming.BODY_REPOSITORIES)
                     and tag.rsplit(":", 1)[0] not in defined]
        pulls = self.instances.body_pulls()
        gone: set[str] = set()
        for reference in pulls:
            if reference in named:
                continue
            image = self.runtime.image(reference)
            if image is None:
                gone.add(reference)
            elif image.id not in keep:
                abandoned.append(reference)
        images = 0
        for reference in abandoned:
            try:
                self.runtime.remove_image(reference)
            except ImageInUse:
                continue
            gone.add(reference)
            images += 1
        self.instances.forget_pulls(gone & pulls.keys())
        return images

    def rediscover(self) -> Catalogue:
        with self._lock:
            self.catalogue = discover(self.search)
            if self.catalogue.errors:
                for error in self.catalogue.errors:
                    self.events.emit("definition.error", message=error)
            return self.catalogue

    def definition_roots(self) -> list[Path]:
        """Where definitions are discovered. They are user-owned and agent-editable, so
        a change here is read again while the daemon runs."""
        return [p for p in (*self.search.faces, *self.search.toolbelts, *self.search.bodies)
                if p.is_dir()]

    def list_items(self, kind: str | None = None) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        verdicts = compatibility.evaluate(self.catalogue, self.intent.selection)
        wanted = {"face": "faces", "toolbelt": "toolbelts", "body": "bodies"}
        rows = wanted.values() if kind is None else [wanted[kind]]
        for row in rows:
            table = getattr(self.catalogue, row)
            out[row] = [self._item_row(row, item_id, item, verdicts[row][item_id])
                        for item_id, item in table.items()]
        return out

    def document_index(self, tab_id: str) -> dict[str, Any]:
        """The router the primer points at — the tab's documents, those
        expected and unwritten, and every layer with its directory and verdict."""
        tab = self.intent.tabs.get(tab_id)
        if tab is None:
            raise SessionError(f"no tab {tab_id}")
        layers = self.list_items()
        found = documents.index(
            self._place(tab).working_copy, self.agents.home(tab_id),
            self._layers_of(layers))
        return {**found, "layers": layers}

    def machine_index(self) -> dict[str, Any]:
        """The manager's router — its incidents, open and fixed, this
        machine's failure patterns, and every layer."""
        layers = self.list_items()
        return {**documents.manager_index(self.paths.manager_documents,
                                          self._layers_of(layers)), "layers": layers}

    @staticmethod
    def _layers_of(layers: dict[str, list[dict[str, Any]]]) -> list[documents.Layer]:
        return [documents.Layer(kind, row["id"], Path(row["directory"]))
                for kind, rows in layers.items() for row in rows if row["directory"]]

    def _item_row(self, row: str, item_id: str, item: Any,
                  verdict: compatibility.Verdict) -> dict[str, Any]:
        # A toolbelt is not selected; the one marked is the active sandbox's.
        selected = (self.active_toolbelt() if row == "toolbelts" else self.intent.selection.get(
            {"faces": labels.Kind.FACE, "bodies": labels.Kind.BODY}[row])) == item_id
        entry: dict[str, Any] = {
            "id": item_id, "name": item.name, "selected": selected,
            "selectable": verdict.selectable, "reason": verdict.reason,
            "warning": verdict.warning,
            "directory": str(item.directory) if item.directory else None,
        }
        if row == "bodies":
            tab = self.intent.body_tab(item_id)
            entry["tab"] = tab.tab_id if tab is not None else None
            entry["instances"] = [
                {"instance": inst.instance_id, "health": inst.health,
                 "reason": inst.reason, "refs": inst.refs}
                for inst in self.instances.all().values() if inst.body == item_id
            ]
        if row == "toolbelts":
            entry["capabilities"] = list(item.capabilities)
        if row == "faces":
            entry["requires"] = list(item.requires_toolbelt_capabilities)
        return entry

    # --- selection -------------------------------------------------------------
    def select(self, kind: str, item_id: str, by_tab: str | None = None) -> dict[str, Any]:
        """`by_tab` is the agent tab asking, when an agent does."""
        with self._lock:
            self._may_select(by_tab)
            k = self._kind(kind)
            if item_id not in self._table(k):
                raise SessionError(f"no {kind} named '{item_id}'")
            verdict = compatibility.evaluate(self.catalogue,
                                             self.intent.selection)[self._row(k)][item_id]
            if not verdict.selectable:
                raise NotSelectable(verdict.reason)
            previous, before = self.intent.selection, self.intent.focused_instance
            self.intent.selection = previous.with_(k, item_id)
            self.events.emit("selection.changed", kind=kind, id=item_id, by=by_tab or "user",
                             selection=dataclasses.asdict(self.intent.selection))
            return self._apply(previous, before, by_tab)

    def deselect(self, kind: str, by_tab: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._may_select(by_tab)
            k = self._kind(kind)
            previous, before = self.intent.selection, self.intent.focused_instance
            if previous.get(k) is None:
                return self.status()
            self.intent.selection = previous.without(k)
            self.events.emit("selection.changed", kind=kind, id=None, by=by_tab or "user",
                             selection=dataclasses.asdict(self.intent.selection))
            return self._apply(previous, before, by_tab)

    def active_toolbelt(self) -> str | None:
        """The active sandbox's toolbelt, which the selector shows in place of a toolbelt
        row: None when the face tab has none open."""
        focused = self.intent.focused_instance
        want = self.intent.instances.get(focused) if focused else None
        return want.toolbelt if want is not None else None

    def _kind(self, kind: str) -> labels.Kind:
        k = labels.Kind(kind)
        if k is labels.Kind.TOOLBELT:
            raise SessionError(
                "the toolbelt is not selected: an agent names one when it opens its sandbox "
                "(`sandbox_open`), and changes it with `toolbelt_swap`")
        return k

    def _may_select(self, by_tab: str | None) -> None:
        """Any tab the user works in selects for them, and stays the tab it was; the
        manager selects nothing."""
        if by_tab is None:
            return
        tab = self.intent.tabs.get(by_tab)
        if tab is None:
            raise SessionError(f"no tab {by_tab}")
        if tab.manager:
            raise SessionError("the manager changes no layer: it repairs the machine")

    @staticmethod
    def _row(kind: labels.Kind) -> str:
        return {labels.Kind.FACE: "faces", labels.Kind.TOOLBELT: "toolbelts",
                labels.Kind.BODY: "bodies"}[kind]

    def _table(self, kind: labels.Kind) -> dict[str, Any]:
        return getattr(self.catalogue, self._row(kind))

    # --- applying a selection -----------------------------------------------------------
    def _apply(self, previous: compatibility.Selection, before: str | None,
               by_tab: str | None = None) -> dict[str, Any]:
        """Bring the world to the selection. **A selection starts no sandbox**: a face
        is put on the screen, and a body gets its tab if it has none. The face goes to
        the selected body's tab's sandbox; every other tab keeps its own. `before` is the
        active sandbox under `previous`, taken before the selection moved it."""
        self._apply_face(previous)
        if previous.body != self.intent.selection.body:
            self.ensure_tabs(by="user" if by_tab is None else by_tab)
        self._refocus(before, always_show=True)
        self.store.save(self.intent)
        return self.status()

    def _refocus(self, before: str | None, always_show: bool = False) -> None:
        """The face follows the active sandbox, which is derived: said when it moved.
        The face's `/work` follows the selection even with no sandbox open, so a selection
        shows it either way."""
        focused = self.intent.focused_instance
        if focused != before:
            self.events.emit("focus.changed", instance=focused)
        if focused != before or always_show:
            self._show(focused)

    def _apply_face(self, previous: compatibility.Selection) -> None:
        """Bring the nested compositor to the selected face.

        A face that will not start does **not** fail the selection, and that is not
        leniency: the bare host state is the one the user lands in when a face will not
        come up, and it has to be reachable in order to repair the face. So the reason is
        emitted with the failure and carried in `status()`, where `rai status` shows it — it
        is reported, never swallowed.
        """
        selection = self.intent.selection
        if selection.face == previous.face and self._face_desktop_is_up(selection.face):
            return

        face = self.catalogue.faces.get(selection.face) if selection.face else None
        if face is None or face.desktop is None:
            # Deselected, or an editor-only face: there is nothing to nest, and
            # whatever was on the screen is no longer the selection.
            self._face_failure = None
            self.faces.stop()
            return

        if not self.faces.host.available:
            # No host compositor, where `[desktop]` does not apply.
            self._face_failure = None
            self.events.emit("face.desktop.skipped", face=face.id,
                             message="no host compositor, so the face's desktop half does "
                                     "not apply: a face's [desktop] needs a host compositor")
            return

        self._bring_up_face(face)

    def _face_desktop_is_up(self, face_id: str | None) -> bool:
        """Whether the screen already shows the face the selection names.

        ⚠ **Asked of the runtime, never inferred from the selection.** The selection is what
        the user asked for and `faces.current()` is what is actually nested, and after a
        daemon restart those are two different things: the session comes back saying
        `face: minimal` and nothing is on the screen, because a face's container is the
        daemon's to start and nothing started it. Treating the two as one makes picking the
        selected face again do nothing — ignoring the one gesture that repairs it.
        """
        if not self.faces.host.available:
            # No host compositor means no desktop half at all, so there is never one to bring up.
            return True
        running = self.faces.current()
        if face_id is None:
            return running is None
        return running is not None and running.face_id == face_id

    def _bring_up_face(self, face) -> None:
        """Nest the face, and report a refusal rather than raising it.

        The bare host state is the one the user lands in when a face will not come up,
        and it has to stay reachable so the face can be repaired from it."""
        try:
            self.faces.switch(face)
            self._face_failure = None
        except FaceError as exc:
            self._face_failure = str(exc)
            self.events.emit("face.failed", face=face.id, error=str(exc))
            return
        if face.editor is not None:
            self._editor_window_due = face

    def _show(self, instance: str | None) -> None:
        """The face shows `instance`, and a face that has just started opens its editor
        window now: the editor is in the apps closure, which is not in the face's
        `/nix/store` until this sync has put it there."""
        self._open_door()
        self.face_mounts.show(instance)
        face, self._editor_window_due = self._editor_window_due, None
        if face is not None:
            self.faces.open_editor_window(face)

    def _open_door(self) -> None:
        """The active sandbox's ports reach the host. A door that cannot open does
        not fail what moved the face, for `_bring_up_face`'s reason: the reason is said and
        carried in `status`."""
        focused = self.intent.focused_instance
        instance = self.instances.get(focused) if focused is not None else None
        try:
            self.door.show(focused, instance.ports if instance is not None else ())
            self._door_failure = None
        except (DoorError, RuntimeError_) as exc:
            self._door_failure = str(exc)
            self.events.emit("door.failed", instance=focused, error=str(exc))

    def _restore_face_desktop(self) -> None:
        """Put the selected face back on the screen when the daemon starts.

        ⚠ The daemon comes back with the session it saved; the nested compositor does not come
        back with it. Without this, every restart leaves `status` saying a face is selected
        over a screen with no face on it — the selection and the world disagreeing about the
        one thing the user can actually see."""
        selection = self.intent.selection
        if selection.face is None or self._face_desktop_is_up(selection.face):
            return
        face = self.catalogue.faces.get(selection.face)
        if face is None or face.desktop is None:
            return
        self.events.emit("face.restoring", face=face.id,
                         message="the session names this face and nothing is nested")
        self._bring_up_face(face)

    def _release(self, instance_id: str, tab_id: str) -> str | None:
        """The tab lets go of its sandbox, which stops unless another tab still holds it.
        Intent only, under the session lock: the sandbox to
        stop is returned for `_stop_released` to stop once the lock is let go."""
        before = self.intent.focused_instance
        self.intent.drop_ref(instance_id, naming.tab_ref(tab_id))
        self._refocus(before)
        want = self.intent.instances.get(instance_id)
        if want is None:
            return None
        if want.referenced():
            self.events.emit("instance.kept", instance=instance_id, refs=want.refs,
                             message="another tab still references this sandbox")
            return None
        self.intent.instances.pop(instance_id, None)
        return instance_id

    def _stop_released(self, instance_id: str | None) -> None:
        """Stops what `_release` let go, on the sandbox's queue and **never under the session
        lock**: the lock guards intent, the queue serialises an instance's containers, and a
        lock held across a queue wait would stall every tab behind one sandbox."""
        if instance_id is None:
            return
        self.queues.run(instance_id, lambda: self.instances.stop(instance_id), "stop",
                        timeout=180)

    # --- sandboxes, opened by agents -------------------------------------------------
    def sandbox_open(self, tab_id: str, toolbelt_id: str) -> dict[str, Any]:
        """A tab opens its sandbox — the body on the tab's own working copy, with the
        toolbelt it names — when it needs to run something. At most one per tab, held by the
        tab's reference until it closes. The face tab's is the active sandbox: the one the
        face shows and the only one whose ports reach the host."""
        with self._lock:
            tab = self._tab(tab_id)
            place = self._place(tab)
            if self._holds(tab_id, place.sandbox):
                raise SessionError(
                    f"this tab's sandbox {place.sandbox} is already open, with toolbelt "
                    f"'{self.intent.instances[place.sandbox].toolbelt}'. Change its toolbelt "
                    "with `toolbelt_swap`.")
            toolbelt = self._toolbelt_for(tab, place, toolbelt_id)
            want = InstanceIntent(
                instance_id=place.sandbox, body=place.body, toolbelt=toolbelt.id,
                working_copy=str(place.working_copy), branch=place.branch)
            self.intent.instances[place.sandbox] = want
            before = self.intent.focused_instance
            ref = naming.tab_ref(tab_id)
            self.intent.add_ref(place.sandbox, ref)
            body = self.catalogue.bodies[place.body] if place.body is not None else None
        try:
            self.queues.run(
                place.sandbox, lambda: self._create(place.sandbox, body, want, [ref], tab_id),
                "sandbox-open", timeout=900)
        except BaseException:
            # A sandbox that did not come up is not held: the tab may open it again.
            with self._lock:
                released = self._release(place.sandbox, tab_id)
            self._stop_released(released)
            raise
        with self._lock:
            if not self._holds(tab_id, place.sandbox):
                # Closed while it opened; the close's stop was queued behind this create.
                raise SessionError(f"tab {tab_id} was closed while its sandbox opened")
            self._refocus(before)
            self.store.save(self.intent)
            self.events.emit("sandbox.opened", tab=tab_id, instance=place.sandbox,
                             toolbelt=toolbelt.id)
            return self.status()

    def check_toolbelt_swap(self, tab_id: str, toolbelt_id: str) -> bool:
        """What `toolbelt_swap` would refuse, asked before the user is: a permission for a
        swap that cannot happen would waste their answer. False when the sandbox already has
        that toolbelt, so there is nothing to ask."""
        with self._lock:
            tab = self._tab(tab_id)
            place = self._place(tab)
            if not self._holds(tab_id, place.sandbox):
                raise SessionError(f"this tab has no sandbox open; open {place.sandbox} "
                                   "with `sandbox_open`")
            self._toolbelt_for(tab, place, toolbelt_id)
            return self.intent.instances[place.sandbox].toolbelt != toolbelt_id

    def toolbelt_swap(self, tab_id: str, toolbelt_id: str) -> dict[str, Any]:
        """The toolbelt swapped in place: the body keeps running and the views
        are recreated, which ends whatever ran in the toolbelt's container. The active
        sandbox is the one the user works in, so its swap waits on their permission, asked
        before this is called (`scopes.py`)."""
        with self._lock:
            tab = self._tab(tab_id)
            place = self._place(tab)
            if not self._holds(tab_id, place.sandbox):
                raise SessionError(f"this tab has no sandbox open; open {place.sandbox} "
                                   "with `sandbox_open`")
            want = self.intent.instances[place.sandbox]
            toolbelt = self._toolbelt_for(tab, place, toolbelt_id)
            if want.toolbelt == toolbelt.id:
                return self.status()
        # Build first: the image is fetched while the old view still serves, and
        # only a fetched toolbelt becomes the sandbox's. The fetch touches no container of the
        # sandbox, so it is off its queue; the intent is written under the lock and never on the
        # queue, since a lock holder (reconcile) waits on the queue.
        self.instances.fetch_toolbelt(toolbelt)
        with self._lock:
            if not self._holds(tab_id, place.sandbox):
                raise SessionError(f"{place.sandbox} was let go while its toolbelt was fetched")
            self.intent.instances[place.sandbox].toolbelt = toolbelt.id
            self.store.save(self.intent)
        self.queues.run(place.sandbox, lambda: self._recreate_view(place.sandbox),
                        "toolbelt-swap", timeout=300)
        with self._lock:
            if self.intent.focused_instance == place.sandbox:
                self._show(place.sandbox)
            self.events.emit("toolbelt.swapped", tab=tab_id, instance=place.sandbox,
                             toolbelt=toolbelt.id)
            return self.status()

    def toolbelts_for(self, tab_id: str) -> dict[str, dict[str, Any]]:
        """Every toolbelt with whether this tab's sandbox may run it, and why not."""
        with self._lock:
            tab = self._tab(tab_id)
            place = self._place(tab)
            body = self.catalogue.bodies.get(place.body) if place.body is not None else None
            face = self._face_judging(tab)
            out = {}
            for tb_id, toolbelt in self.catalogue.toolbelts.items():
                verdict = compatibility.sandbox_toolbelt(body, face, toolbelt)
                out[tb_id] = {"selectable": verdict.selectable, "reason": verdict.reason,
                              "warning": verdict.warning}
            return out

    def _toolbelt_for(self, tab: TabIntent, place: Place, toolbelt_id: str) -> Toolbelt:
        """The toolbelt a sandbox of this place may run, or the reason it may not: judged
        against its body, and against the user's face for the face tab, whose tools the
        face shows."""
        toolbelt = self.catalogue.toolbelts.get(toolbelt_id)
        if toolbelt is None:
            raise SessionError(f"no toolbelt named '{toolbelt_id}'")
        body = self.catalogue.bodies.get(place.body) if place.body is not None else None
        face = self._face_judging(tab)
        verdict = compatibility.sandbox_toolbelt(body, face, toolbelt)
        if not verdict.selectable:
            raise NotSelectable(verdict.reason)
        return toolbelt

    def _face_judging(self, tab: TabIntent) -> Face | None:
        """The selected face, for the tab whose sandbox it is on; None for any other."""
        face_tab = self.intent.face_tab()
        if face_tab is None or face_tab.tab_id != tab.tab_id or not self.intent.selection.face:
            return None
        return self.catalogue.faces.get(self.intent.selection.face)

    def _create(self, instance_id: str, body: Body | None, want: InstanceIntent,
                refs: list[str], tab: str | None = None,
                view_generation: int = 0) -> Instance:
        image = digest = None
        toolbelt = self._defined_toolbelt(instance_id, want)
        # The toolbelt's pull and the body's build share nothing, so neither waits on the other.
        with ThreadPoolExecutor(1, thread_name_prefix="toolbelt-fetch") as pool:
            fetched = pool.submit(self.instances.fetch_toolbelt, toolbelt)
            if body is not None:
                body = self._rooted(body, want.working_copy)
                digest, fresh = self.instances.body_digest(body)
                outcome = self.builds.build(
                    str(body.source_root), digest,
                    lambda: self.instances.build_image(body, digest, fresh))
                if not outcome.succeeded:
                    raise SessionError(
                        f"the body '{body.id}' did not build:\n{outcome.log[-4000:]}")
                image = outcome.image
            fetched.result()
        # Before the view is built, because the snapshot is what creates an absent `hooks`
        # for the view's entrypoint to protect (`git.protected_paths`).
        watch = None
        if git.is_repo(Path(want.working_copy)):
            watch = git.ProtectionWatch(Path(want.working_copy))
            watch.snapshot()
        instance = self.instances.create(
            instance_id=instance_id, body=body, toolbelt=toolbelt,
            working_copy=Path(want.working_copy), branch=want.branch,
            image=image, digest=digest, refs=refs, tab=tab,
            view_generation=view_generation)
        if watch is not None:
            self.instances.protect(instance_id, watch)
        return instance

    def _recreate_view(self, instance_id: str) -> Instance:
        instance = self.instances.get(instance_id)
        if instance is None:
            raise SessionError(f"no running sandbox {instance_id}")
        want = self.intent.instances[instance_id]
        toolbelt = self._defined_toolbelt(instance_id, want)
        self.views.teardown(instance_id)
        instance.toolbelt = want.toolbelt
        self.instances.start_view(instance, toolbelt)
        return instance

    def _defined_toolbelt(self, instance_id: str, want: InstanceIntent) -> Toolbelt:
        """A sandbox runs only with its toolbelt: one whose definition is gone is
        refused, never run as a body with no view."""
        toolbelt = self.catalogue.toolbelts.get(want.toolbelt) if want.toolbelt else None
        if toolbelt is None:
            raise SessionError(
                f"sandbox {instance_id} refers to toolbelt '{want.toolbelt}', which is not "
                "defined. Its containers are left alone; restore the definition, or close "
                "the tab holding it.")
        return toolbelt

    def _face_work(self, instance_id: str | None) -> str | None:
        """The face's `/work`: the active sandbox's working copy, or with none open the one
        its tab works on — the selected body's, or `Paths.work`."""
        if instance_id is None:
            body = (self.catalogue.bodies.get(self.intent.selection.body)
                    if self.intent.selection.body else None)
            return str(self._working_copy(body) if body is not None else self.paths.work)
        instance = self.instances.get(instance_id)
        return instance.working_copy if instance is not None else None

    def _working_copy(self, body: Body) -> Path:
        root = body.source_root
        if root is None:
            raise SessionError(
                f"body '{body.id}' has no directory and no working_copy, so there is "
                "nothing to mount at /work"
            )
        return root

    @staticmethod
    def _rooted(body: Body, working_copy: str) -> Body:
        """The body as one instance builds it: its definition, resolved from that
        instance's own working copy. The build is per copy, and the copy's path is
        its build key.

        Refuses a working copy that is not a directory, because `source_root` would then
        resolve from the definition's directory and build a tree the instance does not
        mount."""
        path = Path(working_copy)
        if not path.is_dir():
            raise SessionError(f"the working copy {path} of body '{body.id}' is not a "
                               "directory, so there is nothing to build it from")
        return dataclasses.replace(body, working_copy=path)

    # --- tabs: where each works, opening and closing -------------------------
    def _tab(self, tab_id: str) -> TabIntent:
        tab = self.intent.tabs.get(tab_id)
        if tab is None:
            raise SessionError(f"tab '{tab_id}' is not open")
        if tab.manager:
            raise SessionError("the manager tab is scoped to the machine and opens no "
                               "sandbox")
        return tab

    def _place(self, tab: TabIntent) -> Place:
        """Where a tab works, sandbox open or not: a body tab on its body's working copy, the
        machine tab on the no-body `/work`. The sandbox id is its sandbox's whether or
        not it is open."""
        if tab.body is None:
            return Place(naming.WORK, None, self.paths.work, "main")
        body = self.catalogue.bodies.get(tab.body)
        if body is None:
            raise SessionError(f"tab {tab.tab_id} works on body '{tab.body}', which is no "
                               "longer defined")
        working_copy = self._working_copy(body)
        branch = git.current_branch(working_copy) if git.is_repo(working_copy) else "main"
        return Place(naming.instance_id(tab.body, tab.tab_id), tab.body, working_copy, branch)

    def _holds(self, tab_id: str, sandbox: str) -> bool:
        want = self.intent.instances.get(sandbox)
        return want is not None and naming.tab_ref(tab_id) in want.refs

    def _open_sandbox_of(self, tab: TabIntent) -> str | None:
        return None if tab.manager else self.intent.sandbox_of(tab.tab_id)

    def ensure_tabs(self, by: str = "daemon") -> dict[str, Any]:
        """The tabs that always exist: the machine tab, and the selected body's. None
        opens without a credential, since no agent could start; the terminal says why
        (`rai ai ready`). A crashed tab is still its tab and is reopened, never replaced.
        `by` is who made it needed: "user" for the user's selection or close, "daemon", or
        the tab whose selection it was."""
        with self._lock:
            if not credential.is_set(self.paths.agent_credentials):
                return self.status()
            if self.intent.machine_tab() is None:
                self._open_tab(None, by)
            body = self.intent.selection.body
            if body is not None and self.intent.body_tab(body) is None:
                self._open_tab(body, by)
            return self.status()

    def _open_tab(self, body: str | None, by: str) -> TabIntent:
        """An agent starts even with nothing selected. The bare host state is the one the user
        lands in when a face won't start, a toolbelt closure is malformed, or a body
        definition is wrong, and it must run an agent that can repair all
        three — which is the machine tab."""
        tab = TabIntent(tab_id=self.intent.new_tab_id(), body=body)
        self.intent.tabs[tab.tab_id] = tab
        self.store.save(self.intent)
        self.events.emit("tab.opened", tab=tab.tab_id, body=body, by=by)
        try:
            self._start_agent(tab, resume=False)
        except Exception:
            # A tab whose agent never started is not a tab.
            del self.intent.tabs[tab.tab_id]
            self.store.save(self.intent)
            self.events.emit("tab.closed", tab=tab.tab_id, body=body,
                             reason="agent did not start")
            raise
        return tab

    def open_manager(self) -> bool:
        """The manager tab, opened for the first failure it takes. False when it is
        already open — a crashed one reopens on its own path and is not replaced."""
        with self._lock:
            if MANAGER in self.intent.tabs:
                return False
            tab = TabIntent(tab_id=MANAGER)
            self.intent.tabs[MANAGER] = tab
            self.store.save(self.intent)
            self.events.emit("tab.opened", tab=MANAGER, instance=None)
            try:
                self.agents.start(self._agent_spec(tab), fresh_home=True)
            except Exception:
                del self.intent.tabs[MANAGER]
                self.events.emit("tab.closed", tab=MANAGER, reason="agent did not start")
                raise
            finally:
                self.store.save(self.intent)
            return True

    def definitions_root(self) -> Path:
        """The directories the three layers are defined in, which every agent can edit:
        a bare-host agent gets it as `/work` to repair a face, toolbelt or body, and
        a tab's agent at `/definitions` to change its own toolbelt or body."""
        roots = [*self.search.faces, *self.search.toolbelts, *self.search.bodies]
        existing = [r for r in roots if r.is_dir()]
        if not existing:
            raise SessionError(
                "none of the configured search paths exist, so there are no definitions "
                "for a recovery agent to repair. Check RAIGOLMID_FACE_PATH, "
                "RAIGOLMID_TOOLBELT_PATH and RAIGOLMID_BODY_PATH."
            )
        # The common parent of the definition directories — the repository root in the
        # ordinary layout.
        import os
        return Path(os.path.commonpath([str(r) for r in existing]))

    def _start_agent(self, tab: TabIntent, resume: bool) -> str:
        """The agent's ref state is recorded before it starts, so "what did it move" is
        answerable afterwards. This is the whole mitigation for the accepted
        history risk of an agent in the user's working copy, so it happens before the container
        exists. `resume` is
        a reopened tab, whose restored home holds the conversation to continue."""
        spec = self._agent_spec(tab)
        if git.is_repo(spec.working_copy):
            self.events.emit("git.refs_before_agent", tab=tab.tab_id,
                             instance=spec.instance_id or None,
                             refs=git.ref_state(spec.working_copy))
        if resume:
            return self.agents.restart(spec, resume=True)[0]
        return self.agents.start(spec, fresh_home=True)

    def _agent_spec(self, tab: TabIntent) -> AgentSpec:
        """The container a tab's agent runs in: its working copy at `/work`, whether or not
        its sandbox is open — the sandbox is reached through the tab's socket, not mounted.
        The manager's `/work` is the definitions. Only the machine tab edits faces,
        so a body tab has them read-only."""
        definitions = self.definitions_root()
        repos = self._definition_repos(definitions)
        if tab.manager:
            return AgentSpec(tab=tab, instance_id="", working_copy=definitions,
                             definitions=definitions, definition_repos=repos)
        place = self._place(tab)
        opened = place.sandbox if self._holds(tab.tab_id, place.sandbox) else ""
        faces = (tuple(f for f in self.search.faces if f.is_dir())
                 if tab.body is not None else ())
        return AgentSpec(tab=tab, instance_id=opened, working_copy=place.working_copy,
                         definitions=definitions, read_only_definitions=faces,
                         definition_repos=repos)

    def _definition_repos(self, definitions: Path) -> tuple[Path, ...]:
        """The definitions root and each layer directory that is a git repository."""
        catalogue = self.catalogue
        candidates = {definitions, *(item.directory for table in (
            catalogue.faces, catalogue.toolbelts, catalogue.bodies) for item in table.values()
            if item.directory is not None)}
        return tuple(sorted(p for p in candidates
                            if (p == definitions or definitions in p.parents) and git.is_repo(p)))

    def restart_agent(self, tab_id: str, resume: bool = True,
                      new_thoughts: bool = False) -> dict[str, Any]:
        """A crashed tab's reopen, and the user's Restart; a fresh one ends its handover.
        `new_thoughts`: the fresh conversation starts its own thought doc (`Agents.restart`)."""
        with self._lock:
            tab = self.intent.tabs.get(tab_id)
            if tab is None:
                raise SessionError(f"no tab {tab_id}")
            try:
                container, resumed = self.agents.restart(self._agent_spec(tab), resume=resume,
                                                         new_thoughts=new_thoughts)
                if not resume:
                    tab.handover = None
            finally:
                self.store.save(self.intent)
            return {"tab": tab_id, "container": container, "resumed": resumed}

    def hold(self, tab_id: str, situation: str) -> dict[str, Any]:
        """A managed tab stopped for the user: resumed on Remote Control, which reaches their
        phone (`Agents.start`), until their own words in it release it (`release`)."""
        with self._lock:
            tab = self.intent.tabs.get(tab_id)
            if tab is None:
                raise SessionError(f"no tab {tab_id}")
            tab.held = situation
            self.store.save(self.intent)
        self.events.emit("tab.held", tab=tab_id, body=tab.body, situation=situation)
        return self.restart_agent(tab_id, resume=True)

    def release(self, tab_id: str) -> None:
        """The user answered in a held tab. It keeps Remote Control until its next restart,
        so they can go on talking to it."""
        with self._lock:
            tab = self.intent.tabs.get(tab_id)
            if tab is None or tab.held is None:
                return
            tab.held = None
            self.store.save(self.intent)
        self.events.emit("tab.released", tab=tab_id, body=tab.body)

    def hand_over(self, tab_id: str, state: str | None) -> None:
        """Where a tab's handover to a fresh conversation stands (`TabIntent.handover`)."""
        with self._lock:
            tab = self.intent.tabs.get(tab_id)
            if tab is None:
                raise SessionError(f"no tab {tab_id}")
            tab.handover = state
            self.store.save(self.intent)

    # --- containers that exit on their own (`supervisor.py`) --------------------------
    def on_exit(self, unit: Unit, container_id: str) -> str:
        """A supervised container's `die`, on `unit.queue`: restarted once, or the manager's."""
        if unit.kind == labels.Role.AGENT:
            return self.on_agent_exit(unit.name, container_id)
        if unit.kind in SANDBOX_PARTS:
            return self._on_sandbox_exit(unit, container_id)
        if unit.kind in HOST_SURFACES:
            role = labels.Role(unit.kind)
            name = hostsurfaces.CONTAINERS[role]
            return self.supervisor.handle(unit, name, container_id, lambda: (
                hostsurfaces.start_at_rest(self.runtime, self.paths, role),
                self.supervisor.running_id(name))[1])
        # The user's face and the door start and stop under the session lock (`_apply_face`,
        # `_open_door`), so their exit is judged under it too. Nothing holding the lock waits
        # on these units' queues.
        with self._lock:
            if unit.kind == labels.Role.FACE:
                return self.supervisor.handle(unit, naming.face(unit.name), container_id,
                                              lambda: self._restart_face(unit.name))
            if unit.kind == labels.Role.DOOR:
                return self.supervisor.handle(unit, naming.door(), container_id,
                                              self._restart_door)
        raise SessionError(f"{unit.kind} is not a container the daemon keeps running")

    def on_agent_exit(self, tab_id: str, container_id: str) -> str:
        """An agent that exits on its own is a crash, said when it happens, and the tab
        reopens. Under the lock, because every stop and restart of an agent holds it and
        removes or replaces the container before letting go (`Agents.stop`)."""
        with self._lock:
            tab = self.intent.tabs.get(tab_id)
            if tab is None or tab.status != "running":
                return "not_an_exit"
            exit = self.supervisor.exited(Unit(labels.Role.AGENT, tab_id),
                                          naming.agent(tab_id), container_id)
            if exit is None:
                return "not_an_exit"
            if exit.container.exit_code == 0:
                # Claude Code exits 0 only when told to quit (`agent-session.sh` execs it):
                # the user's Ctrl+C twice or `/exit`. That is their act, not a crash, so it spends
                # no restart and calls no manager; the tab reopens on their conversation.
                self.events.emit("agent.quit", tab=tab_id, container=exit.container.name)
                try:
                    self._restart_or_crash(tab_id, "quit and did not reopen")
                finally:
                    self.store.save(self.intent)
                return "quit"
            mark_crashed(self.events, tab_id, tab, self._open_sandbox_of(tab),
                         exit_code=exit.container.exit_code, log=str(exit.evidence))
            try:
                return self.supervisor.settle(exit, lambda: self._reopen(tab_id))
            finally:
                self.store.save(self.intent)

    def _prune_kept(self) -> None:
        """Said, never raised: the close or crash it follows has already happened."""
        try:
            keep.prune(self.paths, self.events)
        except keep.KeepError as exc:
            self.events.emit("kept.prune_failed", reason=str(exc))

    def _reopen(self, tab_id: str) -> str:
        """A crashed tab reopens with its conversation. Returns its new container's id;
        a failure leaves the tab crashed, said, and raises."""
        tab = self.intent.tabs[tab_id]
        tab.status = "starting"
        try:
            return self.restart_agent(tab_id, resume=True)["container"]
        except Exception as exc:
            # `Agents.restart` has said it already when the container was what failed.
            if tab.status != "crashed":
                tab.status = "crashed"
                self.events.emit("agent.crashed", tab=tab_id,
                                 instance=self._open_sandbox_of(tab),
                                 message=f"Agent in tab {tab_id} did not reopen: {exc}")
            raise

    def _restart_or_crash(self, tab_id: str, failed: str) -> None:
        """A reopen with no exit of its own to answer for — the machine shut it down, or its
        container is gone. Its failure goes to the manager, as a restart's would."""
        try:
            self._reopen(tab_id)
        except Exception as exc:                       # noqa: BLE001 - said as unfixable
            self.supervisor.unfixable(Unit(labels.Role.AGENT, tab_id), naming.agent(tab_id),
                                      f"Agent in tab {tab_id} {failed}: {exc}")

    def _read_agent_activity(self) -> None:
        """At the start: whether each tab's agent is working, and whether its running
        container's session has come up, as it last said (`activity.py`)."""
        for tab_id, tab in self.intent.tabs.items():
            said = activity.read(self.agents.home(tab_id))
            container = self.runtime.inspect(naming.agent(tab_id))
            running = container is not None and container.running
            tab.busy = running and said.busy
            tab.awaiting_session = running and not container.id.startswith(said.session or "\0")

    def agent_session_started(self, tab_id: str) -> dict[str, Any]:
        """The agent's SessionStart hook: its Claude Code session is up, and hears its
        channel (`channel.py`)."""
        with self._lock:
            tab = self.intent.tabs.get(tab_id)
            if tab is None:
                raise SessionError(f"no tab {tab_id}")
            tab.awaiting_session = False
            self.events.emit("agent.session_started", tab=tab_id)
            return {"tab": tab_id}

    def agent_activity(self, tab_id: str, busy: bool, channel_seq: int | None = None,
                       done: bool = False, error: str | None = None) -> dict[str, Any]:
        """The agent's hooks report a prompt taken and an answer finished. A prompt
        its channel pushed carries the push's `channel_seq`; `done` is a turn that ended with
        nothing asked of the user and nothing on its way to it; `error` is the API error that
        ended one (`limits.py`). No tab closes itself."""
        with self._lock:
            tab = self.intent.tabs.get(tab_id)
            if tab is None:
                raise SessionError(f"no tab {tab_id}")
            tab.busy = busy
            if not busy:
                with self._face_free:
                    self._face_free.notify_all()
            if busy:
                self.events.emit("agent.busy", tab=tab_id, channel_seq=channel_seq)
            else:
                self.events.emit("agent.idle", tab=tab_id, done=done, error=error)
                self._reprotect()
            return self.status()

    def _reprotect(self) -> None:
        """Every definition repository is protected in every agent, and an agent's
        binds are made with its container, so one made before a repository (or a submodule
        in one) appeared is restarted on its conversation once it is idle. Checked as each
        turn ends: the turn that made the repository ends here, and the definitions watch
        never hears inside a `.git`. Queued, because the ending turn's own hook is the
        caller, inside a container this may replace."""
        for tab_id, tab in self.intent.tabs.items():
            if tab.busy or tab.status != "running" or not self._unprotected(tab_id):
                continue
            self.events.emit("agent.reprotecting", tab=tab_id)
            self.queues.submit(Unit(labels.Role.AGENT, tab_id).queue,
                               lambda t=tab_id: self._reprotect_one(t), "reprotect")

    def _unprotected(self, tab_id: str) -> bool:
        container = self.runtime.inspect(naming.agent(tab_id))
        return (container is not None and container.running
                and container.labels.get(labels.GIT_PROTECTED)
                != definition_protection(self._agent_spec(self.intent.tabs[tab_id])))

    def _reprotect_one(self, tab_id: str) -> None:
        """Nobody waits on the queued job, so its failure is said here or not at all. A tab
        busy again by now is left to its own turn's end."""
        try:
            with self._lock:
                tab = self.intent.tabs.get(tab_id)
                if tab is not None and not tab.busy and self._unprotected(tab_id):
                    self.restart_agent(tab_id, resume=True)
        except Exception as exc:
            self.events.emit("agent.reprotect_failed", tab=tab_id,
                             error=f"{type(exc).__name__}: {exc}")

    def close_tab(self, tab_id: str) -> dict[str, Any]:
        """The user closes a tab: its sandbox stops and its home, with the conversation in it,
        is archived. The tabs that always exist open afresh — the machine tab, or the
        selected body's — so closing one is how its context is cleared."""
        with self._lock:
            tab = self.intent.tabs.get(tab_id)
            if tab is None:
                raise SessionError(f"no tab {tab_id}")
            if tab.manager:
                raise SessionError("the manager tab is the daemon's; it closes nothing the "
                                   "user works in")
            try:
                self.agents.stop(tab_id)
            except AgentError as exc:
                self.events.emit("agent.stop_failed", tab=tab_id, reason=str(exc))
            archive = None
            try:
                archive = self.agents.archive_home(tab_id, {"tab": tab_id, "body": tab.body})
            except AgentError as exc:
                self.events.emit("agent.home_archive_failed", tab=tab_id, reason=str(exc))
            self._prune_kept()
            held = self.intent.sandbox_of(tab_id)
            released = None
            if held is not None:
                # While the tab is still the face tab, so the face is seen to leave its sandbox.
                released = self._release(held, tab_id)
            del self.intent.tabs[tab_id]
            ended = self._end_run_if_over()
            self.store.save(self.intent)
            self.events.emit("tab.closed", tab=tab_id, body=tab.body, instance=held,
                             archive=archive, by="user")
        self._say_run_ended(ended)
        self._stop_released(released)
        return self.ensure_tabs(by="user")

    # --- rebuilds ----------------------------------------------------
    def rebuild_body(self, instance_id: str, why: str) -> RebuildReport:
        """`why` is what asked for it, as the history says it: "your request", "a
        watched file", or a tab's request."""
        report = self._rebuild(instance_id)
        self.events.emit("rebuild.finished", instance=instance_id, why=why,
                         report=report.to_dict())
        return report

    def _rebuild(self, instance_id: str) -> RebuildReport:
        """Returns exactly one of `rebuilt`, `already_current` or `build_failed`. It never
        reports success without a completed swap."""
        instance = self.instances.get(instance_id)
        if instance is None:
            raise SessionError(f"no running sandbox {instance_id}")
        if instance.body is None:
            raise SessionError(f"{instance_id} has no body to rebuild")
        body = self.catalogue.bodies.get(instance.body)
        if body is None:
            raise SessionError(f"body '{instance.body}' is no longer defined")
        body = self._rooted(body, instance.working_copy)

        digest, fresh = self.instances.body_digest(body)
        if digest == instance.definition_digest:
            return RebuildReport(result="already_current", instance=instance_id,
                                 digest=digest)

        started = time.monotonic()
        try:
            outcome: BuildOutcome = self.builds.build(
                str(body.source_root), digest,
                lambda: self.instances.build_image(body, digest, fresh), timeout=3600)
        except Superseded as exc:
            return RebuildReport(result="already_current", instance=instance_id,
                                 digest=digest, reason=str(exc))
        if not outcome.succeeded:
            return RebuildReport(result="build_failed", instance=instance_id,
                                 digest=digest, log=outcome.log,
                                 seconds=time.monotonic() - started)

        def swap() -> tuple[RebuildResult, str]:
            # Read in the sandbox's queue: a toolbelt swap queued before this one has
            # changed the toolbelt and the view by the time it runs.
            current = self.instances.get(instance_id)
            if current is None:
                raise SessionError(f"{instance_id} closed before its rebuild could swap in")
            toolbelt = (self.catalogue.toolbelts.get(current.toolbelt)
                        if current.toolbelt else None)
            return self.instances.swap(current, body, toolbelt, outcome.image, digest)

        result, reason = self.queues.run(instance_id, swap, "swap", timeout=900)
        with self._lock:
            if self.intent.focused_instance == instance_id:
                # The definition may have changed the body's ports.
                self._open_door()
        return RebuildReport(result=result, instance=instance_id, digest=digest,
                             seconds=time.monotonic() - started, reason=reason)

    def watched_paths(self) -> dict[str, list[Path]]:
        out: dict[str, list[Path]] = {}
        for instance in self.instances.all().values():
            body = self.catalogue.bodies.get(instance.body)
            # A working copy already gone has nothing left to watch.
            if body is None or not body.watch or not Path(instance.working_copy).is_dir():
                continue
            out[instance.instance_id] = self._rooted(
                body, instance.working_copy).all_watched_files()
        return out

    def on_watched_change(self, instance_id: str) -> None:
        """A file-watch trigger. Debounced, so a multi-file save produces one build."""
        self.debouncer.trigger(instance_id, lambda: self._watched_rebuild(instance_id))

    def _watched_rebuild(self, instance_id: str) -> None:
        """No caller waits on a watched file's rebuild, so its end is said as an event: a
        refusal as one, and any other failure as the watch's, for the manager."""
        why = "a watched file"
        try:
            self.rebuild_body(instance_id, why)
        except SessionError as exc:
            self.events.emit("rebuild.refused", instance=instance_id, why=why,
                             error=str(exc))
        except Exception as exc:                       # noqa: BLE001 - said, for the manager
            self.events.emit("watch.failed", instance=instance_id,
                             error=f"{type(exc).__name__}: {exc}",
                             traceback=traceback.format_exc()[-4000:])

    # --- running things ---------------------------------------------------------------
    def open_launcher(self, instance_id: str) -> socket.socket:
        """A connection to `instance_id`'s launcher, for a caller that names the sandbox and
        is handed the pipe (`api.Handoff`). Any sandbox, not only the focused one: a face
        works with every body."""
        if self.instances.get(instance_id) is None:
            raise SessionError(f"no running sandbox {instance_id}; running: "
                               f"{', '.join(sorted(self.instances.all())) or 'none'}")
        try:
            return self.views.client(instance_id).connect(timeout=None)
        except LauncherUnreachable as exc:
            raise SessionError(f"{instance_id}'s toolbelt does not answer: {exc}") from exc

    def exec(self, instance_id: str, cmd: list[str], cwd: str = "/work",
             timeout: float = 300.0) -> dict[str, Any]:
        """Reads do not queue behind writes, so this runs directly."""
        instance = self.instances.get(instance_id)
        if instance is None:
            raise SessionError(f"no running sandbox {instance_id}")
        if self.views.get(instance_id) is None:
            # Commands run in the toolbelt's container, never in the body, whose image is what
            # deploys and carries no tools.
            raise SessionError(
                f"{instance_id}'s toolbelt container is not running, so there is nowhere "
                f"to run {cmd[0]!r}")
        try:
            output = self.views.client(instance_id).exec(cmd, cwd=cwd, timeout=timeout)
        except LauncherError as exc:
            # A command that ends without an exit code says nothing about *why*. The two
            # cases behave identically here and need opposite investigations: the launcher
            # died, or it is alive and one request's handler failed. It is asked.
            raise SessionError(
                f"{exc}{self.views.launcher_state(instance_id)}"
                f"{self._recent_view_events(instance_id)}") from exc
        return {"exit_code": output.exit_code, "stdout": output.stdout,
                "stderr": output.stderr, "through": "session-view"}

    def search_packages(self, query: str, limit: int = 20) -> list[str]:
        return self.index.search(query, limit)

    def history(self, instance_id: str, n: int = 50) -> list[dict[str, Any]]:
        import json
        return [json.loads(e.to_json()) for e in self.events.history(instance_id, n)]

    def _on_sandbox_exit(self, unit: Unit, container_id: str) -> str:
        """A sandbox's body, view or anchor that exits on its own.

        On the sandbox's queue and never under the session lock: every lock-holding caller
        waits on a queue, so a queue job waiting on the lock is a lock-order inversion. The
        queue is also what serialises this with the sandbox's own swaps and stops.

        A body or view with no running anchor did not exit on its own: they share its PID
        namespace, which the kernel empties when the anchor's init exits, and the anchor's
        restart is theirs."""
        instance_id = unit.name
        if unit.kind != labels.Role.ANCHOR:
            anchor = self.anchors.get(instance_id)
            if anchor is None or not anchor.running:
                return "anchor"
        if unit.kind == labels.Role.BODY:
            return self.supervisor.handle(unit, naming.body_container(instance_id),
                                          container_id, lambda: self._restart_body(instance_id))
        if unit.kind == labels.Role.VIEW:
            return self.supervisor.handle(unit, naming.view(instance_id), container_id,
                                          lambda: self._restart_view(instance_id))
        return self.supervisor.handle(unit, naming.anchor(instance_id), container_id,
                                      lambda: self._restart_anchor(instance_id))

    def _held_on_its_anchor(self, instance_id: str) -> Instance | None:
        """The running sandbox a body or view comes back in: still held, and on a running
        anchor. An anchor's exit takes its body and view with it, and its restart is what
        brings them back."""
        instance = self.instances.get(instance_id)
        want = self.intent.instances.get(instance_id)
        anchor = self.anchors.get(instance_id)
        if (instance is None or want is None or not want.referenced()
                or anchor is None or not anchor.running):
            return None
        return instance

    def _restart_body(self, instance_id: str) -> str | None:
        instance = self._held_on_its_anchor(instance_id)
        if instance is None:
            return None
        self.events.emit("view.restart_pending", instance=instance_id,
                         reason="the body exited on its own")
        self._replace_body(instance, "recovered after the body exited")
        return self.supervisor.running_id(naming.body_container(instance_id))

    def _restart_view(self, instance_id: str) -> str | None:
        if self._held_on_its_anchor(instance_id) is None:
            return None
        self._recreate_view(instance_id)
        return self.supervisor.running_id(naming.view(instance_id))

    def _restart_anchor(self, instance_id: str) -> str | None:
        want = self.intent.instances.get(instance_id)
        if want is None or not want.referenced():
            return None
        self._recreate_from_intent(instance_id)
        return self.supervisor.running_id(naming.anchor(instance_id))

    def _restart_face(self, face_id: str) -> str | None:
        """The user's face again, if it is still the selected one. Under the session lock."""
        face = self.catalogue.faces.get(face_id)
        if (self.intent.selection.face != face_id or face is None or face.desktop is None
                or not self.faces.host.available):
            return None
        try:
            state = self.faces.switch(face)
        except FaceError as exc:
            self._face_failure = str(exc)
            raise
        self._face_failure = None
        started = self.supervisor.running_id(state.container)
        if face.editor is not None:
            self._editor_window_due = face
        try:
            self._show(self.intent.focused_instance)
        except FaceMountError:
            # `FaceMounts` has said it, and `status` carries it until a sync succeeds: the
            # face is up, showing nothing, which is the face's failure and not its restart's.
            pass
        return started

    def _restart_door(self) -> str | None:
        """The active sandbox's ports again, if it still has any. Under the session lock."""
        focused = self.intent.focused_instance
        instance = self.instances.get(focused) if focused is not None else None
        if instance is None or not instance.ports:
            return None
        try:
            self.door.show(focused, instance.ports)
        except (DoorError, RuntimeError_) as exc:
            self._door_failure = str(exc)
            raise
        self._door_failure = None
        return self.supervisor.running_id(naming.door())

    def _replace_body(self, instance: Instance, reason: str) -> None:
        """The body swap, with the image the body already has (`Instances.replace_body`).
        Never a restart in place, which leaves a view over a body whose mounts the design
        does not account for. On the instance's queue."""
        instance_id = instance.instance_id
        if instance.body is None:
            raise SessionError(f"{instance_id} has no body to restart")
        body = self.catalogue.bodies.get(instance.body)
        if body is None:
            raise SessionError(f"body '{instance.body}' is no longer defined")
        toolbelt = (self.catalogue.toolbelts.get(instance.toolbelt)
                    if instance.toolbelt else None)
        self.instances.replace_body(instance, body, toolbelt, instance.image or "",
                                    instance.definition_digest)
        self.events.emit("view.restarted", instance=instance_id,
                         generation=instance.view_generation, reason=reason)

    def restart_body(self, instance_id: str) -> dict[str, Any]:
        """The body started again from the image it runs, without a build, so an
        agent can close its own loop (`rebuild_body` is the one that builds)."""
        instance = self.instances.get(instance_id)
        if instance is None:
            raise SessionError(f"no running sandbox {instance_id}")
        self.queues.run(instance_id,
                        lambda: self._replace_body(instance, "restarted on request"),
                        "restart-body", timeout=300)
        return {"instance": instance_id, "result": "restarted"}

    def show_file(self, instance_id: str, path: str, line: int = 1) -> dict[str, Any]:
        """A file shown to the user in the face's editor. The face shows the
        focused sandbox alone, and its `/work` is that sandbox's, so any other
        sandbox is refused rather than having a path open some other copy."""
        focused = self.intent.focused_instance
        if instance_id != focused:
            raise SessionError(
                f"{instance_id} is not the sandbox on the face "
                f"({focused or 'nothing'} is), so the face cannot show its files")
        if not path.startswith("/work/") or "\n" in path:
            raise SessionError(f"{path!r} is not a file under /work, where the face sees it")
        if line < 1:
            raise SessionError(f"line {line}: lines count from 1")
        face_id = self.intent.selection.face
        if face_id is None or face_id not in self.catalogue.faces:
            raise SessionError("no face is selected, so there is no editor to show it in")
        self.faces.show_file(self.catalogue.faces[face_id], path, line)
        return {"shown": path, "line": line}

    def show_url(self, instance_id: str | None, url: str) -> dict[str, Any]:
        """A page shown in the face's browser, by any tab. The face
        is not in the sandbox's network namespace, so its `localhost` is its own. A local URL
        is the asking tab's sandbox's page, reached at that sandbox's name on the machine's
        network (`naming.host`): any port its body or toolbelt listens on, focused or
        not. `instance_id` is the asking tab's sandbox, None for none."""
        from urllib.parse import urlsplit

        face_id = self.intent.selection.face
        if face_id is None or face_id not in self.catalogue.faces:
            raise SessionError("no face is selected, so there is no browser to show it in")
        parts = urlsplit(url)
        if parts.hostname in ("localhost", "127.0.0.1", "0.0.0.0"):
            if instance_id is None:
                raise SessionError("a localhost page is a sandbox's, and you have no sandbox "
                                   "open: open one with `sandbox_open` and serve it there")
            host = naming.host(instance_id)
            netloc = f"{host}:{parts.port}" if parts.port else host
            url = parts._replace(netloc=netloc).geturl()
        self.faces.show_url(self.catalogue.faces[face_id], url)
        return {"shown": url}

    def set_face_driving(self, allowed: bool) -> dict[str, Any]:
        """The user's switch in the drawer: whether an agent may drive the face on their screen."""
        with self._lock:
            self.intent.face_driving = bool(allowed)
            self.store.save(self.intent)
        if not allowed:
            # No agent drives it from here on, so none is holding it for its turn.
            with self._face_free:
                self._driver = None
                self._face_free.notify_all()
        self.events.emit("face.driving", allowed=bool(allowed))
        return {"face_driving": bool(allowed)}

    def manage(self, tab_id: str, on: bool, stop_when: str | None = None,
               until: float | None = None, why: str | None = None) -> dict[str, Any]:
        """A body tab handed to the machine tab to coordinate, with where it stops for the
        user and when their time for it runs out, or given back (`why` "time" when that is
        the reason). The first tab handed over starts a run; the last given back ends it."""
        with self._lock:
            tab = self.intent.tabs.get(tab_id)
            if tab is None:
                raise SessionError(f"no tab {tab_id}")
            if tab.body is None:
                raise SessionError(f"{tab_id} is not a body tab; only body tabs are managed")
            changed = tab.managed != bool(on)
            tab.managed = bool(on)
            if on:
                tab.stop_when = stop_when or tab.stop_when
                tab.until = until or tab.until
                if self.intent.run is None:
                    self.intent.run = Run(started=time.time())
                # A tab handed over before the machine tab reports joins the same run.
                self.intent.run.ended = None
            else:
                # A handover and a hold are the machine tab's, ended with its hold on the tab.
                tab.handover = tab.stop_when = tab.held = tab.until = None
            ended = self._end_run_if_over()
            self.store.save(self.intent)
        if changed:
            self.events.emit("tab.managed", tab=tab_id, body=tab.body, on=bool(on), why=why,
                             stop_when=tab.stop_when, until=tab.until)
        self._say_run_ended(ended)
        return {"tab": tab_id, "managed": bool(on)}

    def _end_run_if_over(self) -> Run | None:
        """Under the lock: the run, ended now, when no tab is managed any more."""
        run = self.intent.run
        if run is None or run.ended is not None or self.managed_tabs():
            return None
        run.ended = time.time()
        return run

    def _say_run_ended(self, run: Run | None) -> None:
        if run is not None:
            self.events.emit("run.ended", started=run.started, ended=run.ended)

    def report_run(self, machine_tab: str, report: str) -> dict[str, Any]:
        """The machine tab's report on the run that is over, kept under `Paths.runs` beside
        its record of the run (`documents.RUN_RECORD`), which leaves its home so the next run
        keeps its own. The run is over once reported."""
        if not report.strip():
            raise SessionError("a report needs words: what each tab did and where it stands")
        with self._lock:
            run = self.intent.run
            if run is None:
                raise SessionError("there is no run to report on: one starts when the user "
                                   "hands you a tab")
            if run.ended is None:
                raise SessionError(
                    f"the run is not over: you still manage {', '.join(sorted(self.managed_tabs()))}"
                    "; give each back with `manage` (`on` false) first")
            name = time.strftime("%Y%m%dT%H%M%S", time.localtime(run.started))
            span = " to ".join(time.strftime("%Y-%m-%d %H:%M", time.localtime(t))
                               for t in (run.started, run.ended))
            self.paths.runs.mkdir(parents=True, exist_ok=True)
            path = self.paths.runs / f"{name}.md"
            path.write_text(f"# The run from {span}\n\n{report.strip()}\n", encoding="utf-8")
            record = self.agents.home(machine_tab) / documents.RUN_RECORD
            kept = None
            if record.is_file():
                kept = self.paths.runs / f"{name}-record.md"
                os.replace(record, kept)
            self.intent.run = None
            self.store.save(self.intent)
        self.events.emit("run.reported", tab=machine_tab, started=run.started, ended=run.ended,
                         report=path.name)
        return {"report": str(path), "record": str(kept) if kept else None,
                "status": "reported",
                "next": "The user reads it in the catalog, under Documents, Runs."}

    def managed_tabs(self) -> set[str]:
        return {t.tab_id for t in self.intent.tabs.values() if t.managed}

    def _machine_tab(self, tab_id: str) -> None:
        tab = self.intent.tabs.get(tab_id)
        if tab is None:
            raise SessionError(f"no tab {tab_id}")
        if not tab.machine:
            raise SessionError("only the machine tab tries a face off the user's screen, as only "
                               "it edits faces; message it (`to` \"machine\") for a change")

    def seed_face_settings(self, tab_id: str, face_id: str,
                           source_id: str) -> dict[str, Any]:
        """The machine tab starts a face's app settings from another face's,
        replacing its own. Refused for the face on the user's screen, whose apps hold them."""
        self._machine_tab(tab_id)
        for fid in (face_id, source_id):
            if fid not in self.catalogue.faces:
                raise SessionError(f"no face {fid!r}: {', '.join(sorted(self.catalogue.faces))}")
        if face_id == source_id:
            raise SessionError(f"face {face_id!r} cannot be seeded from itself")
        current = self.faces.current()
        if current is not None and current.face_id == face_id:
            raise SessionError(f"face {face_id!r} is on the user's screen and its apps hold its "
                               "settings; seed it while another face is shown")
        try:
            self.faces.seed_settings(face_id, source_id)
        except FaceError as exc:
            raise SessionError(str(exc)) from exc
        return {"face": face_id, "seeded_from": source_id}

    def try_face(self, tab_id: str, face_id: str) -> dict[str, Any]:
        """The machine tab runs a face off the user's screen, as its definition is on
        disk now, and drives it with `face_input` and `screenshot` given `trial`. One trial at
        a time, replacing any before it; one that does not come up is removed, not left half
        made."""
        self._machine_tab(tab_id)
        face = self.catalogue.faces.get(face_id)
        if face is None:
            raise SessionError(f"no face {face_id!r}: {', '.join(sorted(self.catalogue.faces))}")
        with self._trial_lock:
            try:
                state = self.faces.start_trial(face)
                self.face_mounts.stock_trial(self.runtime.inspect(state.container))
                self.faces.ready_trial(face)
            except (FaceError, FaceMountError, RuntimeError_) as exc:
                self.faces.stop_trial()
                raise SessionError(f"face {face_id!r} did not come up off the user's screen: "
                                   f"{exc}") from exc
        return {"trial": state.to_dict()}

    def stop_trial(self, tab_id: str) -> dict[str, Any]:
        self._machine_tab(tab_id)
        with self._trial_lock:
            self.faces.stop_trial()
        return {"trial": None}

    def remove_ended_trial(self) -> None:
        with self._trial_lock:
            self.faces.remove_ended_trial()

    def face_input(self, tab_id: str, action: str, text: str | None = None,
                   x: int | None = None, y: int | None = None,
                   button: str = "left", trial: bool = False) -> dict[str, Any]:
        """A tab's input on the face the user is looking at. Their switch refuses it
        outright. One tab drives at a time: the first input of a turn takes the face until
        that turn ends, and another tab's waits for it. Then it waits until they have paused.
        Either wait longer than a tool call's is refused, so a turn never hangs on it.

        With `trial`, the machine tab's face off the user's screen, which they are not using and no
        other tab drives, so none of those waits apply."""
        if trial:
            self._machine_tab(tab_id)
            self.faces.input(action, text=text, x=x, y=y, button=button, trial=True)
            self.events.emit("face.driven", tab=tab_id, action=action, trial=True)
            return {"done": action}
        if tab_id not in self.intent.tabs:
            raise SessionError(f"no tab {tab_id}")
        if not self.intent.face_driving:
            raise SessionError("the user has turned off agents driving their face (the drawer's "
                               "\"don't drive my current face\"); ask them to switch it on")
        deadline = time.monotonic() + INPUT_PATIENCE
        with self._face_free:
            # A holder's tab closing or its agent stopping notifies nothing, so the wait
            # looks again each second.
            while (holder := self._driving()) not in (None, tab_id):
                left = deadline - time.monotonic()
                if left <= 0:
                    raise SessionError(
                        f"not driven: tab {holder} is driving the face for its turn. Try "
                        f"again later, or message {holder} if you need it sooner")
                self._face_free.wait(min(left, 1.0))
            self._driver = tab_id
        try:
            self.presence.wait_until_away(max(deadline - time.monotonic(), 0.0))
        except PresenceError as exc:
            raise SessionError(f"not driven: {exc}. Try again when the user has stepped away, "
                               "or ask them to") from exc
        self.faces.input(action, text=text, x=x, y=y, button=button)
        self.events.emit("face.driven", tab=tab_id, action=action)
        return {"done": action}

    def _driving(self) -> str | None:
        """The tab driving the user's face: the last to, while its turn has not ended."""
        tab = self.intent.tabs.get(self._driver) if self._driver else None
        return tab.tab_id if tab is not None and tab.busy else None

    def screenshot(self, tab_id: str, trial: bool = False) -> dict[str, Any]:
        """The face as the user sees it, written into the tab's home, which is the
        one directory of the agent's that is not their project. With `trial`, the machine
        tab's face off their screen."""
        if trial:
            self._machine_tab(tab_id)
        elif tab_id not in self.intent.tabs:
            raise SessionError(f"no tab {tab_id}")
        shot = self.faces.screenshot(self.agents.home(tab_id) / "screenshots", trial=trial)
        return {"path": str(shot), "focused_instance": self.intent.focused_instance}

    def reconcile(self) -> Report:
        with self._lock:
            # Each instance's container work runs on its queue, serialised with its swaps
            # and exits. The lock is held across those waits, which cannot deadlock: nothing
            # on a sandbox's queue takes it (`_on_sandbox_exit`).
            report = self.reconciler.run(
                self.intent, self.catalogue, recreate=self._recreate_from_intent,
                on_queue=lambda iid, fn: self.queues.run(iid, fn, "reconcile", timeout=900))
            self._archive_orphaned_homes()
            self.intent.stopped = None
            # The rest of the start goes on past a tab that does not come back.
            for tab_id in report.resumable_tabs:
                self._restart_or_crash(tab_id, "did not resume after the machine restarted")
            for tab_id in report.crashed_tabs:
                self._reopen_crashed(tab_id)
            self._restore_face_desktop()
            try:
                self._show(self.intent.focused_instance)
            except FaceMountError:
                # Reported, not raised, for `_bring_up_face`'s reason: a daemon that
                # does not start leaves nothing to repair the face from. `status` carries it.
                pass
            self.store.save(self.intent)
            return report

    def _reopen_crashed(self, tab_id: str) -> None:
        """A crash found at the start. A container still there exited on its own while the
        daemon was down, and is judged as if seen then (`supervisor.py`): the one its restart
        started is not reopened. With none left, the tab reopens as any start would."""
        container = self.runtime.inspect(naming.agent(tab_id))
        exit = (self.supervisor.exited(Unit(labels.Role.AGENT, tab_id), container.name,
                                       container.id) if container is not None else None)
        if exit is None:
            self._restart_or_crash(tab_id, "did not reopen")
            return
        self.supervisor.settle(exit, lambda: self._reopen(tab_id))

    def _archive_orphaned_homes(self) -> None:
        """A tab the intent no longer names, lost with an intent removed or refused, is closed:
        its home is archived like any closed tab's, not left where nothing lists it."""
        homes = self.paths.agent_homes
        if not homes.is_dir():
            return
        for home in sorted(homes.iterdir()):
            if re.fullmatch(r"tab-\d+", home.name) and home.name not in self.intent.tabs:
                try:
                    self.agents.archive_home(home.name, {"tab": home.name, "orphaned": True})
                except AgentError as exc:
                    self.events.emit("agent.home_archive_failed", tab=home.name,
                                     reason=str(exc))
        self._prune_kept()

    def _recreate_from_intent(self, instance_id: str) -> Instance:
        want = self.intent.instances[instance_id]
        body = self.catalogue.bodies.get(want.body) if want.body is not None else None
        if want.body is not None and body is None:
            raise SessionError(
                f"sandbox {instance_id} refers to body '{want.body}', which is no longer "
                "defined. Its containers are left alone; remove the reference or restore "
                "the definition."
            )
        self._defined_toolbelt(instance_id, want)
        tab = self.intent.tab_for_instance(instance_id)
        # The view built next is a new one of the same instance, so its
        # generation follows the old view's rather than starting again.
        running = self.instances.get(instance_id)
        generation = running.view_generation if running is not None else 0
        self.instances.stop(instance_id)
        return self._create(instance_id, body, want, list(want.refs),
                            tab.tab_id if tab else None, view_generation=generation)

    def repair(self, instance_id: str) -> dict[str, Any]:
        """`rai repair` — tear the instance down completely and rebuild it from its
        definition, as an explicit user action."""
        with self._lock:
            if instance_id not in self.intent.instances:
                raise SessionError(f"no sandbox {instance_id}")
            self.events.emit("instance.repair_requested", instance=instance_id)
        result = self.queues.run(instance_id,
                                 lambda: self._recreate_from_intent(instance_id), "repair",
                                 timeout=900)
        return {"instance": result.instance_id, "health": result.health}

    # --- status ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        # The intent read whole, never part-way through a mutation of it.
        with self._lock:
            selection = self.intent.selection
            instances = self.instances.all()
            # A host compositor that cannot be addressed is reported as the reason the desktop
            # half is unknown. It is not an unknown state and it is not a healthy one, and
            # `status` is the one place a user looks to tell those apart.
            try:
                desktop, desktop_error = self.faces.current(), self._face_failure
            except FaceError as exc:
                desktop, desktop_error = None, str(exc)
            return {
                "epoch": self.epoch,
                "session": {
                    "face": selection.face,
                    "toolbelt": self.active_toolbelt(),
                    "body": selection.body,
                    "instances": sorted(instances),
                    "focused_instance": self.intent.focused_instance,
                    "meaning": compatibility.describe(selection, self.catalogue),
                },
                "instances": {i: inst.to_dict() for i, inst in instances.items()},
                "builds": self.builds.state(),
                "queues": self.queues.depths(),
                "agents": [
                    {"tab": t.tab_id,
                     "scope": "manager" if t.manager else "machine" if t.machine
                     else {"body": t.body},
                     "sandbox": self._open_sandbox_of(t),
                     "status": t.status, "busy": t.busy, "managed": t.managed,
                     "hands_off": self.intent.hands_off(t.tab_id)}
                    for t in self.intent.tabs.values()
                ],
                "face_runtime": {
                    "face": selection.face,
                    # The desktop half, read from the runtime and the host compositor rather
                    # than from what was asked for: a face the daemon started and that has
                    # since died reads here as absent, which is the difference between the
                    # screen and the intent.
                    "desktop": desktop.to_dict() if desktop else None,
                    "desktop_error": desktop_error,
                    # Why the face is not showing the focused instance, until a sync succeeds.
                    "shown_error": self.face_mounts.failure,
                    # Why the active sandbox's ports do not reach the host, until a door opens.
                    "door_error": self._door_failure,
                    "driving": {"allowed": self.intent.face_driving,
                                "by": self._driving(), **self.presence.state()},
                    # The machine tab's face off the user's screen (`try_face`).
                    "trial": trial.to_dict() if (trial := self.faces.trial()) else None,
                },
                "definition_errors": self.catalogue.errors,
                "protected_git_changes": self.instances.protected_changes(),
            }

    def _recent_view_events(self, instance_id: str) -> str:
        """What the daemon did to this view, in its own words. A view that vanished was
        torn down by something, and every teardown path emits before it acts."""
        prefixes = ("view.", "reconcile.", "instance.", "body.")
        interesting = [e for e in self.events.tail(200)
                       if (e.instance == instance_id or e.data.get("instance") == instance_id)
                       and e.type.startswith(prefixes)]
        if not interesting:
            return ""
        lines = "\n".join(f"  {e.type} {e.data}" for e in interesting[-8:])
        return f"\nWhat the daemon did to this view:\n{lines}"

    def close(self) -> None:
        """On SIGTERM the daemon stops accepting work and **leaves running containers
        running**: views and bodies outlive the daemon by design, and
        reconciliation adopts them on the next start. It does not tear down the user's
        environment because its own process is restarting.

        The stop record is saved before the queues are waited on, so a stop cut short by
        systemd's timeout still leaves it for the next start's reconcile."""
        with self._lock:
            self.intent.stopped = StopRecord(boot=boot.current(), running_tabs=[
                tab_id for tab_id in self.intent.tabs
                if (c := self.runtime.inspect(naming.agent(tab_id))) is not None
                and c.running])
            self.store.save(self.intent)
        self.debouncer.cancel_all()
        self.queues.close_all()
