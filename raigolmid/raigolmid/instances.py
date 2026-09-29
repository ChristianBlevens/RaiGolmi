"""Body instances: lifecycle, the rebuild swap, and degraded state.

Controlled session-view restarts, serialization and degraded instances meet here. One rule
runs through all three: **the view's mounts are released before the old body container is
removed.** A view holds references into the body's filesystem, and Docker removes the body
regardless and leaks its storage; nothing but this ordering prevents that.
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from . import compose, flakes, naming, superseded
from .git import ProtectionWatch
from .anchors import Anchors
from .definitions import Body, Toolbelt
from .events import EventLog
from .facemounts import FaceMountError, FaceMounts
from .intent import load_json, save_json
from .paths import Paths
from .queues import BuildOutcome
from .runtime import ContainerRuntime, RuntimeError_
from .toolbelts import (Closure, NixeryRefused, ToolbeltError, ToolbeltResolver,
                        pull_from_nixery)
from .launcher import LauncherError, LauncherUnreachable
from .views import ViewError, ViewPlan, Views

Health = Literal["ok", "degraded", "starting", "stopped"]
RebuildResult = Literal["rebuilt", "already_current", "build_failed"]


class InstanceError(Exception):
    pass


@dataclass(slots=True)
class Instance:
    """Runtime facts about one body instance. Never persisted: after a crash the
    container runtime is the only thing that knows the truth. Intent — why
    this instance exists — lives in `intent.json` instead."""
    instance_id: str
    body: str | None            # None: the toolbelt on `Paths.work`, no body (`naming.WORK`)
    toolbelt: str | None
    working_copy: str
    branch: str
    anchor: str
    compose_project: str | None
    session_view: str
    view_generation: int = 0
    definition_digest: str | None = None
    image: str | None = None
    refs: list[str] = field(default_factory=list)
    health: Health = "starting"
    reason: str = ""
    ports: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if not d["reason"]:
            d.pop("reason")
        return d


def body_ports(body: Body | None) -> tuple[int, ...]:
    """The ports the body listens on inside its anchor's network namespace. None reaches the
    host but the active sandbox's, through the door."""
    return tuple(sorted(body.ports)) if body is not None else ()


class Instances:
    """Everything that creates, replaces or removes an instance's containers.

    Every method here assumes it is already running inside that instance's queue.
    It does not take the queue itself, because the caller is what knows whether
    this is one step of a larger sequence.
    """

    def __init__(self, runtime: ContainerRuntime, paths: Paths, events: EventLog,
                 anchors: Anchors, views: Views, face_mounts: FaceMounts,
                 resolver: ToolbeltResolver, compose_cli: compose.ComposeCLI,
                 epoch: int) -> None:
        self.runtime = runtime
        self.paths = paths
        self.events = events
        self.anchors = anchors
        self.views = views
        self.face_mounts = face_mounts
        self.resolver = resolver
        self.compose = compose_cli
        self.epoch = epoch
        # Written on the instances' queues, read by `status` from any thread.
        self._lock = threading.Lock()
        self._pulls_lock = threading.Lock()
        self._state: dict[str, Instance] = {}
        # A running instance's protected git files, gone with the instance. The
        # baseline is what its agent started with, so it outlives a daemon restart: a sandbox
        # adopted by reconcile is watched against it, never against a fresh snapshot that would
        # take a change made before the restart as the start.
        self._watches: dict[str, ProtectionWatch] = {
            instance_id: ProtectionWatch(Path(record["working_copy"]), record["baseline"])
            for instance_id, record in (load_json(paths.protection, "the protection record")
                                        or {}).items()}

    # --- state ---------------------------------------------------------------------
    def get(self, instance_id: str) -> Instance | None:
        with self._lock:
            return self._state.get(instance_id)

    def all(self) -> dict[str, Instance]:
        with self._lock:
            return dict(self._state)

    def put(self, instance: Instance) -> None:
        with self._lock:
            self._state[instance.instance_id] = instance

    def forget(self, instance_id: str) -> None:
        with self._lock:
            self._state.pop(instance_id, None)
            if self._watches.pop(instance_id, None) is not None:
                self._save_watches()

    def protect(self, instance_id: str, watch: ProtectionWatch) -> None:
        with self._lock:
            self._watches[instance_id] = watch
            self._save_watches()

    def _save_watches(self) -> None:
        save_json(self.paths.protection, {
            i: {"working_copy": str(w.working_copy), "baseline": w.baseline}
            for i, w in self._watches.items()})

    def protected_changes(self) -> dict[str, list[str]]:
        """Each running instance's protected files changed since its agent started, where
        any did."""
        with self._lock:
            watches = {i: w for i, w in self._watches.items() if i in self._state}
        return {i: changed for i, w in watches.items() if (changed := w.changed())}

    def project_dir(self, instance_id: str) -> Path:
        return self.paths.state / "projects" / naming.compose_project(instance_id)

    def compose_file(self, instance_id: str) -> Path:
        return self.project_dir(instance_id) / "compose.yaml"

    def mark_degraded(self, instance_id: str, reason: str) -> Instance:
        """Any instance reconciliation cannot bring to a known-good state is marked
        degraded with a reason and left alone. Nothing is auto-recreated over an
        unexplained state, and nothing is silently discarded."""
        inst = self.get(instance_id)
        if inst is None:
            raise InstanceError(f"no sandbox {instance_id} to mark degraded")
        if inst.health != "degraded" or inst.reason != reason:
            self.events.emit("instance.degraded", instance=instance_id, reason=reason)
        inst.health = "degraded"
        inst.reason = reason
        return inst

    def mark_ok(self, instance_id: str) -> None:
        inst = self.get(instance_id)
        if inst is None:
            return
        if inst.health != "ok":
            self.events.emit("instance.health", instance=instance_id, health="ok")
        inst.health = "ok"
        inst.reason = ""

    # --- image building --------------------------------------------------
    def body_digest(self, body: Body) -> tuple[str, bool]:
        """The body's definition digest over its base images as the registry names them now,
        and whether the registry answered for every one. One it cannot reach is
        taken as the machine's copy, said as `body.base_local`."""
        resolved, fresh = [], True
        for ref in body.base_images():
            if "$" in ref:
                resolved.append(ref)
                continue
            try:
                resolved.append(f"{ref}@{self.runtime.registry_digest(ref)}")
            except RuntimeError_ as exc:
                fresh = False
                resolved.append(f"{ref}@{self._local_digest(body, ref, exc)}")
        return body.definition_digest("\n".join(resolved) or None), fresh

    def _local_digest(self, body: Body, ref: str, unreachable: Exception) -> str:
        """The digest the machine's copy of `ref` was pulled as, which is what the registry
        answered then; its id when it records none."""
        info = self.runtime.image(ref)
        if info is None:
            raise InstanceError(f"body '{body.id}' starts from {ref}, which the registry "
                                f"did not answer for ({unreachable}) and no copy of which "
                                "is on the machine")
        head, _, last = ref.rpartition("/")
        repo = f"{head}/{last.split(':')[0]}" if head else last.split(":")[0]
        pulled = [d.split("@", 1)[1] for d in info.repo_digests if d.split("@", 1)[0] == repo]
        self.events.emit("body.base_local", body=body.id, image=ref, reason=str(unreachable))
        return pulled[0] if pulled else info.id

    def _record_pull(self, reference: str, body_id: str) -> None:
        with self._pulls_lock:
            pulls = self.body_pulls()
            if pulls.get(reference) != body_id:
                save_json(self.paths.body_pulls, {**pulls, reference: body_id})

    def body_pulls(self) -> dict[str, str]:
        """Each image pulled as a body's, and the body it was pulled for."""
        return load_json(self.paths.body_pulls, "the images pulled for bodies") or {}

    def forget_pulls(self, references: set[str]) -> None:
        with self._pulls_lock:
            pulls = self.body_pulls()
            if references & pulls.keys():
                save_json(self.paths.body_pulls,
                          {r: b for r, b in pulls.items() if r not in references})

    def build_image(self, body: Body, digest: str, fresh: bool) -> BuildOutcome:
        """The work a `BuildLock` coalesces. It builds; it never swaps. `fresh`: the registry
        answered for the base images (`body_digest`), so the build fetches their new
        versions."""
        tag = naming.build_tag(body.id, digest)
        started = time.monotonic()

        if not body.builds_from_source:
            if body.image is None:
                raise InstanceError(f"body '{body.id}' has neither an image nor a Dockerfile")
            if fresh or self.runtime.image(body.image) is None:
                self.runtime.pull(body.image)
            self._record_pull(body.image, body.id)
            return BuildOutcome(digest=digest, succeeded=True, image=body.image,
                                log="", duration=time.monotonic() - started)

        if self.runtime.image(tag) is not None:
            superseded.drop_older(self.runtime, tag)
            return BuildOutcome(digest=digest, succeeded=True, image=tag, log="",
                                duration=time.monotonic() - started)

        self.events.emit("build.started", body=body.id, digest=digest, tag=tag)
        assert body.dockerfile is not None and body.build_context is not None
        result = self.runtime.build(
            context=str(body.build_context),
            dockerfile=str(body.dockerfile.relative_to(body.build_context))
            if body.dockerfile.is_relative_to(body.build_context) else str(body.dockerfile),
            tag=tag,
            target=body.build_target,
            pull=fresh,
        )
        duration = time.monotonic() - started
        if not result.succeeded:
            self.events.emit("build.failed", body=body.id, digest=digest,
                             seconds=round(duration, 2), log=result.log[-4000:])
            return BuildOutcome(digest=digest, succeeded=False, image="",
                                log=result.log, duration=duration)
        self.events.emit("build.complete", body=body.id, digest=digest, tag=tag,
                         seconds=round(duration, 2))
        superseded.drop_older(self.runtime, tag)
        return BuildOutcome(digest=digest, succeeded=True, image=tag, log=result.log,
                            duration=duration)

    # --- creating an instance ------------------------------------------------------
    def create(self, instance_id: str, body: Body | None, toolbelt: Toolbelt,
               working_copy: Path, branch: str, image: str | None, digest: str | None,
               refs: list[str], tab: str | None = None,
               view_generation: int = 0) -> Instance:
        """Anchor → body → view, in that order: each joins the one before it. A sandbox
        always has its toolbelt; with no body it is the anchor and the view alone."""
        ports = body_ports(body)

        self.anchors.ensure(instance_id, tab=tab)
        self.events.emit("anchor.created", instance=instance_id, tab=tab)

        instance = Instance(
            instance_id=instance_id,
            body=body.id if body is not None else None,
            toolbelt=toolbelt.id,
            working_copy=str(working_copy),
            branch=branch,
            anchor=naming.anchor(instance_id),
            compose_project=naming.compose_project(instance_id) if body is not None else None,
            session_view=naming.view(instance_id),
            definition_digest=digest,
            image=image,
            refs=list(refs),
            ports=ports,
            view_generation=view_generation,
        )
        self.put(instance)

        if body is not None:
            self.start_body(instance, body, image, digest)
        self.start_view(instance, toolbelt)
        self.events.emit("instance.started", instance=instance_id, body=instance.body,
                         toolbelt=instance.toolbelt, tab=tab)
        return instance

    def start_body(self, instance: Instance, body: Body, image: str, digest: str | None,
                   force_recreate: bool = False) -> None:
        """`digest` is the definition the new container runs, which in a swap is not yet the
        instance's: that is recorded only once the swap has succeeded."""
        owner = Path(instance.working_copy).stat()
        placement = compose.BodyPlacement(
            instance=instance.instance_id,
            namespace_ref=self.anchors.namespace_ref(instance.instance_id),
            working_copy=Path(instance.working_copy),
            image=image,
            user=f"{owner.st_uid}:{owner.st_gid}",
            toolbelt=instance.toolbelt,
        )
        path = compose.write(body, placement, self.epoch,
                             digest or "", self.project_dir(instance.instance_id))
        self.compose.up(instance.compose_project, path, force_recreate=force_recreate)
        instance.image = image
        # The container id is here so a later "the body exited" can be told apart from a
        # stale read of the container this call replaced. Without it the two are the same
        # event, and they need opposite repairs.
        started = self.runtime.inspect(naming.body_container(instance.instance_id))
        self.events.emit("body.started", instance=instance.instance_id, image=image,
                         digest=digest,
                         container=started.id[:12] if started else None,
                         recreated=force_recreate)

    def body_container_id(self, instance: Instance) -> str:
        cid = self.compose.container_id(instance.compose_project,
                                        self.compose_file(instance.instance_id),
                                        naming.body_service())
        if cid is None:
            raise InstanceError(
                f"the body for {instance.instance_id} is not running; its Compose project "
                f"{instance.compose_project} reports no container"
            )
        return cid

    def fetch_toolbelt(self, toolbelt: Toolbelt) -> None:
        """The toolbelt's image on the machine, so a view can start from it at once: pulled
        from Nixery, or — for a list Nixery refuses, or a lock from a flake — built from a
        generated flake (`flakes.py`)."""
        closure = self.resolver.resolve(toolbelt)
        if self.runtime.image(closure.image) is not None:
            return
        if closure.method != "flake":
            try:
                pull_from_nixery(self.runtime, closure.image)
                return
            except NixeryRefused as refused:
                self.events.emit("toolbelt.nixery_refused", toolbelt=toolbelt.id,
                                 reason=str(refused))
                nixery = str(refused)
        else:
            nixery = None
        self.events.emit("toolbelt.flake_building", toolbelt=toolbelt.id,
                         nixpkgs=closure.nixpkgs)
        try:
            built = flakes.build(self.runtime, self.resolver.flakes, toolbelt,
                                 closure.nixpkgs, self.epoch)
        except (flakes.FlakeError, RuntimeError_) as exc:
            self.events.emit("toolbelt.flake_failed", toolbelt=toolbelt.id, reason=str(exc))
            raise ToolbeltError(f"{nixery + '. ' if nixery else ''}Built from a flake "
                                f"instead, it failed: {exc}") from exc
        self.events.emit("toolbelt.flake_built", toolbelt=toolbelt.id, image=built.image,
                         nixpkgs=built.nixpkgs)

    def start_view(self, instance: Instance, toolbelt: Toolbelt) -> None:
        """The session view is the toolbelt's container."""
        closure = self.resolver.resolve(toolbelt)
        instance.view_generation += 1
        plan = ViewPlan(
            instance=instance.instance_id,
            body_container_id=(self.body_container_id(instance)
                               if instance.body is not None else None),
            anchor_ref=self.anchors.namespace_ref(instance.instance_id),
            toolbelt_image=closure.image,
            working_copy=Path(instance.working_copy),
            generation=instance.view_generation,
        )
        self.views.create(plan)
        try:
            self.views.wait_until_usable(instance.instance_id)
        except ViewError as exc:
            self.views.teardown(instance.instance_id)
            self.mark_degraded(instance.instance_id, str(exc))
            raise
        self.events.emit("view.created", instance=instance.instance_id,
                         generation=instance.view_generation, image=closure.image)
        self._record_lock(instance, toolbelt, closure)
        self.mark_ok(instance.instance_id)
        # The face shows this view's store paths, which may be new ones.
        self._face_follows(instance.instance_id)

    def _face_follows(self, instance_id: str) -> None:
        """The face picks up the instance's running body and view again. A face that cannot
        is the face's failure, not the sandbox's: `FaceMounts` has already said it and
        `status` carries it as `shown_error` until a sync succeeds, so the working sandbox
        is not failed over it. `release` is different and does raise: the face's hold on
        the old body is what must end before that body is replaced."""
        try:
            self.face_mounts.refresh(instance_id)
        except FaceMountError:
            pass

    def _record_lock(self, instance: Instance, toolbelt: Toolbelt,
                     closure: Closure) -> None:
        """Reproducibility comes from a lock file, not from the authoring format.
        It is written once the closure has demonstrably worked — a view built on it and
        answered — and rebuilds use it until the package list changes.

        A lock that cannot be written does not fail the view. The view is working; the
        lock is what makes the *next* resolution reproducible, so the honest response is to
        say so loudly and carry on, not to tear down an environment the user is in.
        """
        try:
            paths = self.views.store_paths(instance.instance_id)
            self.resolver.write_lock(
                # A flake's image is named by the flake it was built from; Nixery's only by
                # the digest it was pulled as.
                toolbelt, Closure(image=(closure.image if closure.method == "flake"
                                         else self._pinned(closure.image)),
                                  method=closure.method, packages=closure.packages,
                                  package_digest=closure.package_digest,
                                  store_paths=paths, nixpkgs=closure.nixpkgs))
        except (ViewError, LauncherError, LauncherUnreachable, OSError, ToolbeltError) as exc:
            self.events.emit("toolbelt.lock_failed", instance=instance.instance_id,
                             toolbelt=toolbelt.id, reason=str(exc))
            return
        self.events.emit("toolbelt.locked", instance=instance.instance_id,
                         toolbelt=toolbelt.id, digest=closure.package_digest,
                         store_paths=len(paths))

    def _pinned(self, image: str) -> str:
        """`image` by the manifest digest it was pulled as. Nixery answers a name with the
        closure of its current channel, so only the digest names this one again."""
        if "@sha256:" in image:
            return image
        info = self.runtime.image(image)
        repo = image.rsplit("@", 1)[0]
        pinned = [d for d in (info.repo_digests if info is not None else ())
                  if d.split("@", 1)[0] == repo]
        if not pinned:
            raise ToolbeltError(f"{image} has no digest from its registry to lock it by")
        return pinned[0]

    # --- the rebuild swap ---------------------------------------------------
    def swap(self, instance: Instance, body: Body, toolbelt: Toolbelt | None,
             image: str, digest: str) -> tuple[RebuildResult, str]:
        """Announce the restart and replace the body, after a build the caller holds as a
        successful `BuildOutcome`. The editor's save has no place here: the editor runs in
        the face, which outlives a body rebuild."""
        iid = instance.instance_id
        # Re-taken here, not only before the build. Two rebuild requests for the same
        # change — an agent's explicit one and the file watch the same edit tripped —
        # both pass the caller's check while the instance still carries the old digest,
        # and both attach to the one build. This is the serialization point: by the time
        # the second reaches it the first has swapped and recorded the digest, so the
        # second would tear down the view the first just built. The instance's own digest
        # is the test, not whether its build was coalesced.
        if instance.definition_digest == digest:
            self.events.emit("swap.already_current", instance=iid, digest=digest)
            return "already_current", (
                "the sandbox is already running this definition; another rebuild of the "
                "same change completed first")
        started = time.monotonic()
        self.events.emit("view.restart_pending", instance=iid, digest=digest)
        self.replace_body(instance, body, toolbelt, image, digest)
        seconds = round(time.monotonic() - started, 2)
        self.events.emit("view.restarted", instance=iid, seconds=seconds,
                         generation=instance.view_generation)
        return "rebuilt", ""

    def replace_body(self, instance: Instance, body: Body, toolbelt: Toolbelt | None,
                     image: str, digest: str | None) -> None:
        """The one sequence a rebuild, a restart and a recovery all run:
        the view down, the face's hold released, the body container replaced, a new view
        over it. A sandbox always runs with its toolbelt, so one whose toolbelt has no
        definition is refused before anything moves; and once the view is down, any failure
        leaves the instance degraded with the reason, never `ok` with no view."""
        iid = instance.instance_id
        if toolbelt is None:
            raise InstanceError(
                f"{iid}'s toolbelt '{instance.toolbelt}' is no longer defined, so its view "
                "could not be rebuilt after the body is replaced; nothing was changed")

        # The view's mounts of the old body are released before the body is touched.
        self.views.teardown(iid)
        self.events.emit("view.torn_down", instance=iid, generation=instance.view_generation)
        try:
            # The face's `/body` holds the old body's filesystem too.
            self.face_mounts.release(iid)
            # The anchor keeps the network identity; it publishes nothing, so a change to
            # the body's ports leaves it be (the door follows them).
            self.start_body(instance, body, image, digest, force_recreate=True)
            instance.definition_digest = digest
            instance.ports = body_ports(body)
            self.start_view(instance, toolbelt)
        except Exception as exc:
            self.mark_degraded(iid, f"replacing the body failed: {exc}")
            raise

    # --- teardown --------------------------------------------------------------------
    def stop(self, instance_id: str, remove_anchor: bool = True) -> None:
        """View and the face's `/body` first, then body, then anchor. The order is the mounts
        rule again: both hold mounts into the body's filesystem. A piece that will not go
        leaves the instance degraded with the reason and raises: the caller did not get the
        stop it asked for, and the retry is reconcile's sweep of what nothing references."""
        instance = self.get(instance_id)
        self.views.teardown(instance_id)
        self.face_mounts.release(instance_id)
        compose_file = self.compose_file(instance_id) if naming.has_body(instance_id) else None
        if compose_file is not None and compose_file.is_file():
            try:
                self.compose.down(naming.compose_project(instance_id), compose_file)
            except compose.ComposeError as exc:
                if instance is not None:
                    self.mark_degraded(instance_id, f"body teardown failed: {exc}")
                raise
        if remove_anchor:
            try:
                self.anchors.remove(instance_id)
            except RuntimeError_ as exc:
                if instance is not None:
                    self.mark_degraded(instance_id, f"anchor removal failed: {exc}")
                raise
        self.events.emit("instance.stopped", instance=instance_id)
        self.forget(instance_id)
