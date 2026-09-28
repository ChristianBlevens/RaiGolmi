"""A reconcile must not stop the face the user is looking at.

`reconcile` reference-counts **instance** lifetime, and it finds its subjects by listing every
container labelled managed. A face and the host's two surfaces are managed and
carry no instance label, because they belong to the host rather than to a session — so reading
"no instance label" as "leftover" removes a running face on the next reconcile.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from raigolmid import labels                                 # noqa: E402
from raigolmid.reconcile import group_containers             # noqa: E402
from raigolmid.runtime.base import ContainerInfo             # noqa: E402


def _container(name: str, role: labels.Role, instance: str | None = None) -> ContainerInfo:
    tags = {labels.MANAGED: "true", labels.ROLE: str(role)}
    if instance is not None:
        tags[labels.INSTANCE] = instance
    return ContainerInfo(id=name, name=name, image="img", status="running", labels=tags)


def test_the_hosts_own_surfaces_are_not_swept():
    containers = [_container("raigolmid-selector", labels.Role.SELECTOR),
                  _container("raigolmid-host-control", labels.Role.CONTROL),
                  _container("raigolmid-notify", labels.Role.NOTIFY)]
    _, orphans = group_containers(containers)
    assert orphans == []


def test_an_instance_container_with_no_instance_label_is_still_an_orphan():
    """The exemption is for host-scoped roles only — a view that lost its instance label is
    exactly the leftover the sweep exists for."""
    _, orphans = group_containers([_container("raigolmid-view-stray", labels.Role.VIEW)])
    assert [c.name for c in orphans] == ["raigolmid-view-stray"]


def test_instance_containers_still_group_normally():
    groups, orphans = group_containers([
        _container("raigolmid-face-writing", labels.Role.FACE),
        _container("anchor", labels.Role.ANCHOR, "api@tab-2"),
        _container("body", labels.Role.BODY, "api@tab-2"),
        _container("view", labels.Role.VIEW, "api@tab-2"),
    ])
    assert orphans == []
    assert set(groups) == {"api@tab-2"}
    group = groups["api@tab-2"]
    assert (group.anchor.name, group.body.name, group.view.name) == ("anchor", "body", "view")
