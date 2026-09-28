"""One restart, then the manager, for every container but the host (`supervisor.py`).

Agents' own cases are in `test_agents.py`, a body's in `test_rebuild.py`, and the budget across
a daemon restart in `test_reconcile.py`.
"""
from __future__ import annotations

import pytest

from raigolmid import hostsurfaces, labels, naming
from raigolmid.hostsurfaces import SELECTOR_CONTAINER
from raigolmid.runtime import ContainerSpec

from tests.harness import Harness

# The harness opens the machine tab first, so selecting myapi opens tab-2.
TAB = "tab-2"
SANDBOX = f"myapi@{TAB}"


@pytest.fixture()
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    assert harness.open_sandbox("myapi", "python-dev") == SANDBOX
    list(harness.runtime.events())
    return harness


def test_a_start_by_anyone_else_gives_the_restart_back(h):
    """The budget is the container its restart started: the user's restart makes another, whose
    exit on its own is restarted again rather than sent to the manager."""
    h.runtime.kill(naming.agent(TAB), exit_code=1)
    h.deliver_runtime_events()
    h.session.restart_agent(TAB)
    h.runtime.kill(naming.agent(TAB), exit_code=1)
    h.deliver_runtime_events()

    assert len(h.events_of("container.restarted")) == 2
    assert not h.events_of("container.unfixable")
    assert h.runtime.inspect(naming.agent(TAB)).running


def test_a_view_that_exits_on_its_own_is_restarted(h):
    dead = h.session.views.get(SANDBOX).id
    h.runtime.kill(naming.view(SANDBOX), exit_code=1)

    h.deliver_runtime_events()

    view = h.session.views.get(SANDBOX)
    assert view.running and view.id != dead
    assert h.session.instances.get(SANDBOX).health == "ok"
    [restarted] = h.events_of("container.restarted")
    assert (restarted.instance, restarted.data["kind"]) == (SANDBOX, "view")


def _parts(h):
    return {name: h.runtime.inspect(name) for name in (
        naming.anchor(SANDBOX), naming.body_container(SANDBOX), naming.view(SANDBOX))}


def test_an_anchor_that_exits_on_its_own_brings_its_body_and_view_back(h):
    """The body and view share the anchor's PID namespace and die with it (137); the anchor's
    restart is theirs, so their deaths are neither restarted again nor the manager's."""
    dead = {name: info.id for name, info in _parts(h).items()}
    h.runtime.kill(naming.anchor(SANDBOX), exit_code=1)
    assert not any(info.running for info in _parts(h).values())

    h.deliver_runtime_events()

    assert all(info.running and info.id != dead[name] for name, info in _parts(h).items())
    assert h.session.instances.get(SANDBOX).health == "ok"
    assert [e.data["kind"] for e in h.events_of("container.restarted")] == ["anchor"]
    assert not h.events_of("container.unfixable")


def test_an_anchor_exit_does_not_spend_a_body_already_restarted(h):
    """A body whose restart is spent and whose anchor then exits did not exit on its own."""
    h.runtime.kill(naming.body_container(SANDBOX), exit_code=1)
    h.deliver_runtime_events()
    assert [e.data["kind"] for e in h.events_of("container.restarted")] == ["body"]

    h.runtime.kill(naming.anchor(SANDBOX), exit_code=1)
    h.deliver_runtime_events()

    assert all(info.running for info in _parts(h).values())
    assert not h.events_of("container.unfixable")


def _selector(h):
    if h.runtime.inspect(SELECTOR_CONTAINER) is not None:
        h.runtime.remove(SELECTOR_CONTAINER, force=True)
    h.runtime.run(ContainerSpec(name=SELECTOR_CONTAINER, image="selector", labels={
        labels.MANAGED: "true", labels.ROLE: str(labels.Role.SELECTOR)}))


def test_a_host_surface_restarted_once_that_exits_again_is_the_managers(h, monkeypatch):
    h.runtime.add_image("selector")
    monkeypatch.setattr(hostsurfaces, "start_at_rest", lambda runtime, paths, role: _selector(h))
    _selector(h)

    h.runtime.kill(SELECTOR_CONTAINER, exit_code=1)
    h.deliver_runtime_events()
    assert h.runtime.inspect(SELECTOR_CONTAINER).running
    h.runtime.kill(SELECTOR_CONTAINER, exit_code=2)
    h.deliver_runtime_events()

    assert len(h.events_of("container.restarted")) == 1, "restarted into the same exit"
    assert not h.runtime.inspect(SELECTOR_CONTAINER).running
    [unfixable] = h.events_of("container.unfixable")
    assert (unfixable.data["unit"], unfixable.data["exit_code"]) == ("selector", 2)
    assert [e.data["exit_code"] for e in h.events_of("container.exited")] == [1, 2]
