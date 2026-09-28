"""The path a view creates is the path raigolmid waits on.

An instance id is `<body>@<tab>`, and `@` is a poor filename component, so `Paths` folds it.
Only `Paths` knows that, so only raigolmid can name the socket — a view given the raw instance
id would listen on `body@tab-2.sock` while raigolmid waited on the folded name, and the
symptom is the worst kind: the launcher comes up, reports itself listening, and is never
reached. A test instance name with no `@` misses it; every real one has one.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from raigolmid import naming
from raigolmid.paths import Paths
from raigolmid.runtime.base import ContainerSpec
from tests.fakeruntime import FakeRuntime
from raigolmid.views import ViewPlan, Views

# The shape of every instance id.
INSTANCE = "myapi@tab-2"


@pytest.fixture()
def built(tmp_path: Path):
    paths = Paths(state=tmp_path / "state", data=tmp_path / "data",
                  config=tmp_path / "config", runtime=tmp_path / "run")
    paths.ensure()
    runtime = FakeRuntime()
    # A real anchor, running: the view joins its namespaces, and a namespace cannot be
    # joined unless the container holding it is up.
    runtime.add_image("registry.k8s.io/pause:3.9")
    anchor = runtime.run(ContainerSpec(name=naming.anchor(INSTANCE),
                                       image="registry.k8s.io/pause:3.9"))
    views = Views(runtime, paths, epoch=1)
    work = tmp_path / "work"
    work.mkdir()
    runtime.add_image("toolbelt:test")
    views.create(ViewPlan(instance=INSTANCE, body_container_id="body0",
                          anchor_ref=f"container:{anchor.id}",
                          toolbelt_image="toolbelt:test",
                          working_copy=work, generation=1))
    spec = runtime.spec_of(naming.view(INSTANCE))
    return paths, spec


def test_the_view_is_told_the_name_raigolmid_will_look_for(built) -> None:
    paths, spec = built
    assert spec.environment["VIEW_SOCK_NAME"] == paths.launcher_socket(INSTANCE).name


def test_the_instance_id_itself_is_still_the_real_one(built) -> None:
    """The fold is for the filename only: the launcher reports its identity to raigolmid, and
    reconciliation matches on the real id."""
    _, spec = built
    assert spec.environment["VIEW_INSTANCE"] == INSTANCE


def test_the_instance_with_no_body_has_names_no_body_can_take():
    """Body ids are not validated, so `work` must be a form no `<body>@<owner>` produces:
    no `@` in the id, and no owner segment in its container names."""
    assert naming.split(naming.WORK) == (None, naming.WORK)
    assert not naming.has_body(naming.WORK) and naming.has_body("work@tab-2")
    assert naming.anchor(naming.WORK) == "raigolmid-anchor-work"
    assert naming.view(naming.WORK) == "raigolmid-view-work"
    assert naming.view("work@tab-2") != naming.view(naming.WORK)
    with pytest.raises(ValueError, match="has no body"):
        naming.compose_project(naming.WORK)
    with pytest.raises(ValueError, match="has no body"):
        naming.body_container(naming.WORK)
