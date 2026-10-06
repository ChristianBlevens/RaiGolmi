"""Startup reconciliation.

raigolmid will be stopped, upgraded, and crashed. What it owns must survive that, and what
it can no longer account for must be surfaced rather than leaked. So on every start it
lists every container labelled `io.raigolmi.managed=true` and re-derives the world from
them — it never reads a stored inventory, because a stored inventory goes stale the
instant the daemon dies (principle 9).

The rules are applied **per instance, in this order**, and the order is the point:

1. A live view over a dead or replaced body is stale — tear it down *first*, because its
   mounts point at a filesystem that is going away, and a body container is never removed
   while a view still references it.
2. A view whose launcher does not answer is torn down and recreated. A view that cannot be
   talked to cannot be used, and adopting it would hand the user tools nobody can drive.
3. A healthy triple is adopted as-is. Terminals and language servers keep running across
   a daemon restart — this is the normal path for a raigolmid upgrade.
4. Missing pieces of a referenced instance are recreated in dependency order.
5. Unreferenced objects are stopped and removed. Reference counting is the instance-lifetime
   rule and it applies identically after a restart.
6. An agent container whose tab is gone is stopped; a tab whose agent is gone is marked
   crashed, said, and reopened, unless its dead container is the one the daemon's
   own restart started, which is the janitor's (`supervisor.py`) — or unless the daemon saw
   it running as it stopped and the machine has booted since: the shutdown ended it, and it
   resumes without a crash.

Anything that cannot be brought to a known-good state is marked **degraded** with a reason
and left alone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from . import boot, labels, naming
from .definitions import Catalogue
from .events import EventLog
from .instances import Instance, Instances
from .intent import Intent, TabIntent
from .runtime import ContainerInfo, ContainerRuntime, RemoveBusy, RuntimeError_
from .views import Views


@dataclass(slots=True)
class Group:
    """Every managed container belonging to one instance."""
    instance_id: str
    anchor: ContainerInfo | None = None
    body: ContainerInfo | None = None
    view: ContainerInfo | None = None
    agents: list[ContainerInfo] = field(default_factory=list)
    extra: list[ContainerInfo] = field(default_factory=list)


@dataclass(slots=True)
class Report:
    adopted: list[str] = field(default_factory=list)
    recreated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    degraded: dict[str, str] = field(default_factory=dict)
    crashed_tabs: list[str] = field(default_factory=list)
    # Ended by the machine going down under them: resumed, not crashed.
    resumable_tabs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"adopted": self.adopted, "recreated": self.recreated,
                "removed": self.removed, "degraded": self.degraded,
                "crashed_tabs": self.crashed_tabs, "resumable_tabs": self.resumable_tabs}


# Roles that belong to the host rather than to an instance.
HOST_SCOPED = frozenset({labels.Role.FACE, labels.Role.FACE_TRIAL, labels.Role.SELECTOR,
                         labels.Role.CONTROL, labels.Role.NOTIFY, labels.Role.CATALOG,
                         labels.Role.DOOR})


def group_containers(containers: list[ContainerInfo]) -> tuple[dict[str, Group],
                                                               list[ContainerInfo]]:
    groups: dict[str, Group] = {}
    orphans: list[ContainerInfo] = []
    for c in containers:
        instance_id = c.labels.get(labels.INSTANCE)
        role = c.labels.get(labels.ROLE)
        if role in HOST_SCOPED:
            # Neither a group nor an orphan. This reconcile counts references to *instances*,
            # and a face or a host surface has no instance to be referenced by — it belongs
            # to the host and is owned by whatever started it (`faces.py`, `hostsurfaces.py`).
            # Reading "no instance label" as "leftover" would stop the running face.
            continue
        if instance_id is None:
            # An agent container belongs to a tab, not an instance, until it opens one, so
            # `_reconcile_agents` gets first refusal on these before they are swept.
            orphans.append(c)
            continue
        group = groups.setdefault(instance_id, Group(instance_id=instance_id))
        if role == labels.Role.ANCHOR:
            group.anchor = c
        elif role == labels.Role.BODY:
            group.body = c
        elif role == labels.Role.VIEW:
            group.view = c
        elif role == labels.Role.AGENT:
            group.agents.append(c)
        else:
            group.extra.append(c)
    return groups, orphans


def mark_crashed(events: EventLog, tab_id: str, tab: TabIntent, sandbox: str | None,
                 **evidence: Any) -> None:
    """Said before the tab reopens, so a crash is never passed over in silence."""
    tab.status = "crashed"
    events.emit(
        "agent.crashed", tab=tab_id, instance=sandbox, **evidence,
        message=(f"Agent in tab {tab_id} crashed"
                 + (f" (sandbox {sandbox} still running)" if sandbox else "")),
    )


class Reconciler:
    def __init__(self, runtime: ContainerRuntime, views: Views, instances: Instances,
                 events: EventLog, epoch: int) -> None:
        self.runtime = runtime
        self.views = views
        self.instances = instances
        self.events = events
        self.epoch = epoch

    def run(self, intent: Intent, catalogue: Catalogue,
            recreate: Callable[[str], Instance] | None = None,
            on_queue: Callable[[str, Callable[[], None]], None] | None = None) -> Report:
        """`on_queue(instance_id, fn)` runs one instance's rules where that instance's other
        container work runs, so a reconcile never tears down a view a swap is building."""
        run_for = on_queue or (lambda _iid, fn: fn())
        report = Report()
        containers = self.runtime.list(label_filter={labels.MANAGED: "true"})
        groups, orphans = group_containers(containers)

        # A sandbox is held only by open tabs: a ref naming no tab holds nothing.
        open_tabs = {naming.tab_ref(t) for t in intent.tabs}
        for instance_id, want in intent.instances.items():
            for ref in [r for r in want.refs if r not in open_tabs]:
                want.refs.remove(ref)
                self.events.emit("reconcile.dangling_ref", instance=instance_id, ref=ref)

        seen = set(groups) | set(intent.instances)
        for instance_id in sorted(seen):
            group = groups.get(instance_id, Group(instance_id=instance_id))
            referenced = instance_id in intent.instances and \
                intent.instances[instance_id].referenced()
            try:
                run_for(instance_id,
                        lambda i=instance_id, g=group, ref=referenced: self._reconcile_instance(
                            i, g, intent, ref, recreate, report))
            except (RuntimeError_, OSError) as exc:
                report.degraded[instance_id] = str(exc)
                self.events.emit("reconcile.failed", instance=instance_id, reason=str(exc))

        # Rule 5's intent half: an unreferenced sandbox is not intended, and an entry left
        # behind would read as another tab's sandbox to the tab that opens it next.
        for instance_id in intent.unreferenced():
            intent.instances.pop(instance_id)

        self._reconcile_agents(intent, orphans, report)

        for c in orphans:
            if c.labels.get(labels.ROLE) == labels.Role.AGENT or self._caller_waits(c):
                continue
            self.events.emit("reconcile.orphan", container=c.name,
                             role=c.labels.get(labels.ROLE, "?"))
            self._remove(c, report)

        self.events.emit("reconcile.complete", **report.to_dict())
        return report

    def _caller_waits(self, c: ContainerInfo) -> bool:
        return (c.labels.get(labels.ROLE) in labels.ONE_SHOT and c.running
                and c.labels.get(labels.EPOCH) == str(self.epoch))

    # --- one instance -------------------------------------------------------------------
    def _reconcile_instance(self, instance_id: str, group: Group, intent: Intent,
                            referenced: bool,
                            recreate: Callable[[str], Instance] | None,
                            report: Report) -> None:
        # Rule 1 — a view over a dead or replaced body is stale. This runs before anything
        # else touches the body, because the view's mounts are what make the body
        # unremovable. An instance with no body has a view on the toolbelt's own root.
        has_body = naming.has_body(instance_id)
        if group.view is not None and has_body:
            view_body = group.view.labels.get(labels.BODY_CONTAINER)
            body_gone = group.body is None or not group.body.running
            replaced = group.body is not None and view_body not in (group.body.id, None)
            if body_gone or replaced:
                reason = "its body is gone" if body_gone else "its body was replaced"
                self.events.emit("reconcile.stale_view", instance=instance_id, reason=reason)
                self.views.teardown(instance_id)
                group.view = None

        # Rule 5 (taken early for the whole group) — nothing references this instance, so
        # nothing about it should be running.
        if not referenced:
            self._stop_group(instance_id, group, report)
            return

        # Rule 2 — a running view whose launcher does not answer cannot be driven.
        if group.view is not None and group.view.running:
            if not self.views.client(instance_id).alive(timeout=2.0):
                self.events.emit("reconcile.unreachable_view", instance=instance_id)
                self.views.teardown(instance_id)
                group.view = None
        elif group.view is not None:
            self.views.teardown(instance_id)
            group.view = None

        # The view is the toolbelt's container, so it belongs to the triple exactly when the
        # sandbox names a toolbelt.
        wants_view = intent.instances[instance_id].toolbelt is not None
        healthy = (group.anchor is not None and group.anchor.running
                   and (group.body is not None and group.body.running if has_body
                        else group.body is None)
                   and (group.view is not None and group.view.running
                        if wants_view else group.view is None))

        if healthy:
            # Rule 3 — adopt. The launcher socket lives on the host, so the daemon
            # reconnects to it rather than restarting anything: terminals and language
            # servers keep running across this.
            self.instances.put(self._adopt(instance_id, group, intent))
            report.adopted.append(instance_id)
            self.events.emit("reconcile.adopted", instance=instance_id,
                             generation=self.views.generation_of(instance_id))
            return

        # Rule 4 — recreate the missing pieces, in dependency order.
        if recreate is None:
            reason = "pieces are missing and no recreate path was given"
            self.instances.put(self._adopt(instance_id, group, intent, health="degraded",
                                           reason=reason))
            report.degraded[instance_id] = reason
            return
        try:
            self.instances.put(self._adopt(instance_id, group, intent, health="starting"))
            recreate(instance_id)
            report.recreated.append(instance_id)
            self.events.emit("reconcile.recreated", instance=instance_id)
        except Exception as exc:                       # noqa: BLE001
            self.instances.mark_degraded(instance_id, f"could not recreate: {exc}")
            report.degraded[instance_id] = str(exc)

    def _adopt(self, instance_id: str, group: Group, intent: Intent,
               health: str = "ok", reason: str = "") -> Instance:
        want = intent.instances.get(instance_id)
        body_id, _ = naming.split(instance_id)
        digest = None
        image = None
        if group.body is not None:
            digest = group.body.labels.get(labels.DEFINITION_DIGEST)
            image = group.body.image
        return Instance(
            instance_id=instance_id,
            body=want.body if want else body_id,
            toolbelt=want.toolbelt if want else None,
            working_copy=want.working_copy if want else "",
            branch=want.branch if want else "main",
            anchor=naming.anchor(instance_id),
            compose_project=(naming.compose_project(instance_id)
                             if naming.has_body(instance_id) else None),
            session_view=naming.view(instance_id),
            view_generation=self.views.generation_of(instance_id),
            definition_digest=digest,
            image=image,
            refs=list(want.refs) if want else [],
            health=health,                             # type: ignore[arg-type]
            reason=reason,
            ports=(tuple(int(p) for p in group.body.labels[labels.BODY_PORTS].split(","))
                   if group.body is not None and labels.BODY_PORTS in group.body.labels else ()),
        )

    def _stop_group(self, instance_id: str, group: Group, report: Report) -> None:
        if group.view is not None:
            self.views.teardown(instance_id)
            report.removed.append(group.view.name)
        for c in (group.body, *group.extra, group.anchor):
            if c is not None:
                self._remove(c, report)
        if group.anchor or group.body or group.view:
            self.events.emit("reconcile.unreferenced", instance=instance_id)
        self.instances.forget(instance_id)

    def _remove(self, container: ContainerInfo, report: Report) -> None:
        try:
            # Docker refuses to remove a paused container and stops one cleanly.
            if container.status in ("running", "paused"):
                self.runtime.stop(container.name)
            self.runtime.remove(container.name)
            report.removed.append(container.name)
        except RemoveBusy as exc:
            instance_id = container.labels.get(labels.INSTANCE, container.name)
            self.events.emit("body.remove_busy", instance=instance_id,
                             container=container.name, reason=str(exc))
            report.degraded[instance_id] = str(exc)

    # --- rule 6 -------------------------------------------------------------------------
    def _reconcile_agents(self, intent: Intent, orphans: list[ContainerInfo],
                          report: Report) -> None:
        agent_containers = {
            c.labels.get(labels.TAB): c
            for c in self.runtime.list(label_filter={labels.MANAGED: "true",
                                                     labels.ROLE: str(labels.Role.AGENT)})
        }
        for tab_id, container in agent_containers.items():
            if tab_id is None:
                continue
            if tab_id in intent.tabs:
                continue
            # The tab is gone; what it did is in the working copy it worked in.
            self.events.emit("agent.orphaned", tab=tab_id, container=container.name)
            self._remove(container, report)
            if container in orphans:
                orphans.remove(container)

        # Positive evidence only: the daemon saw the agent running as it stopped, and the
        # machine has booted since. Without the record it is a crash, as it always was.
        rebooted = intent.stopped is not None and intent.stopped.boot != boot.current()
        for tab_id, tab in intent.tabs.items():
            container = agent_containers.get(tab_id)
            if container is not None and container.running:
                continue
            if tab.status == "crashed":
                continue
            if rebooted and tab_id in intent.stopped.running_tabs:
                report.resumable_tabs.append(tab_id)
                continue
            mark_crashed(self.events, tab_id, tab, intent.sandbox_of(tab_id))
            report.crashed_tabs.append(tab_id)
