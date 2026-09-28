"""The session's rules, end to end on fakes.

The compatibility engine, reference-counted instances, state
transitions, and the build-lock and queue logic, as pure Python.
"""
from __future__ import annotations

import json

from pathlib import Path

import pytest

from raigolmid.toolbelts import nixery_reference

from raigolmid import labels, naming
from raigolmid.agents import AgentError
from raigolmid.session import NotSelectable, SessionError

from tests.harness import Harness, converse


@pytest.fixture()
def h(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


# --- selection ---------------------------------------------------------

def test_incompatible_toolbelts_are_greyed_out_with_a_reason_not_hidden(h):
    """Faces and bodies never constrain each other; what a selection constrains is the
    toolbelt a tab's sandbox may open with."""
    h.session.select("face", "backend-focus")
    rows = h.session.list_items()
    toolbelts = {t["id"]: t for t in rows["toolbelts"]}
    assert set(toolbelts) == {"python-dev", "no-lsp"}, "an item was hidden rather than greyed"
    assert toolbelts["no-lsp"]["selectable"] is False
    assert "lsp" in toolbelts["no-lsp"]["reason"]
    assert toolbelts["python-dev"]["selectable"] is True
    assert all(f["selectable"] for f in rows["faces"])
    assert all(b["selectable"] for b in rows["bodies"])


def test_opening_a_sandbox_with_an_incompatible_toolbelt_is_refused_with_the_reason(h):
    h.session.select("face", "backend-focus")
    h.session.ensure_tabs()
    with pytest.raises(NotSelectable, match="lsp"):
        h.session.sandbox_open(h.tab(None), "no-lsp")
    assert h.session.intent.instances == {}


def test_a_toolbelt_with_no_body_runs_on_its_own_root_at_work(h):
    """The toolbelt's container with no body — an anchor and a view on the view's own
    root, `/work` the one directory outside every definition, and no body or project."""
    h.open_sandbox(None, "python-dev")
    assert h.session.status()["session"]["instances"] == [naming.WORK]

    instance = h.session.instances.all()[naming.WORK]
    assert instance.body is None and instance.compose_project is None
    assert instance.working_copy == str(h.paths.work)
    view = h.runtime._containers[naming.view(naming.WORK)]["spec"]
    assert view.environment["VIEW_ROOT"] == "toolbelt"
    assert labels.BODY_CONTAINER not in view.labels
    assert any(m.target == "/work" and m.source == str(h.paths.work) for m in view.mounts)
    assert h.runtime.inspect(naming.anchor(naming.WORK)).running
    assert not [c for c in h.runtime.list({labels.ROLE: str(labels.Role.BODY)})]


def test_a_command_with_no_sandbox_open_is_refused_never_run_in_the_body(h):
    h.session.select("body", "myapi")
    with pytest.raises(SessionError, match="no running sandbox"):
        h.session.exec(naming.instance_id("myapi", h.tab("myapi")), ["sh", "-c", "true"])
    assert ["sh", "-c", "true"] not in [cmd for _, cmd in h.runtime.exec_log], \
        "a command must never reach the body's container"


# --- starting and stopping -----------------------------------------------------------

def test_the_body_and_the_view_both_join_the_anchor_not_each_other(h):
    sandbox = h.open_sandbox("myapi", "python-dev")
    anchor = h.runtime.inspect(naming.anchor(sandbox))
    view_spec = h.runtime._containers[naming.view(sandbox)]["spec"]
    assert view_spec.pid_mode == f"container:{anchor.id}"
    assert view_spec.network_mode == f"container:{anchor.id}"


def test_the_view_gets_setup_capabilities_and_nothing_persistent(h):
    sandbox = h.open_sandbox("myapi", "python-dev")
    spec = h.runtime._containers[naming.view(sandbox)]["spec"]
    assert set(spec.cap_add) == {"SYS_ADMIN", "SYS_PTRACE"}
    assert not spec.privileged, "a view must not be privileged wholesale"


def test_leaving_a_body_leaves_its_tab_and_sandbox_running(h):
    """A sandbox runs while its tab holds it, whatever is selected, and a selection
    builds nothing."""
    sandbox = h.open_sandbox("myapi", "python-dev")
    tab = h.tab("myapi")
    h.session.select("body", "webui")
    running = {c.name for c in h.runtime.list() if c.running}
    assert naming.anchor(sandbox) in running and naming.view(sandbox) in running
    assert naming.agent(tab) in running
    assert h.session.intent.tabs[tab].body == "myapi"
    assert naming.anchor(naming.instance_id("webui", h.tab("webui"))) not in running


def test_the_view_is_torn_down_before_the_body_is_removed(h):
    """The view holds mounts into the body's filesystem, so it goes first.

    Recorded at the calls rather than read out of the event log. The events are emitted
    around the work rather than by it, so an event-order assertion can hold while the
    calls ran the other way — and an assertion that the log is merely non-empty holds
    however they ran.
    """
    h.open_sandbox("myapi", "python-dev")

    order: list[str] = []
    original_teardown = h.session.instances.views.teardown
    original_down = h.compose.down

    def teardown(instance):
        order.append(f"view-down:{instance}")
        return original_teardown(instance)

    def down(project, file):
        order.append(f"body-down:{project}")
        return original_down(project, file)

    h.session.instances.views.teardown = teardown
    h.compose.down = down
    h.session.close_tab(h.tab("myapi"))

    assert order, "neither the view nor the body was brought down"
    assert order[0].startswith("view-down:"), \
        f"the body was removed while the view still held mounts into it: {order}"
    assert any(o.startswith("body-down:") for o in order), \
        f"the view came down but the body was never removed: {order}"
    view_first = order.index(next(o for o in order if o.startswith("view-down:")))
    body_first = order.index(next(o for o in order if o.startswith("body-down:")))
    assert view_first < body_first, f"the view must stop before the body: {order}"


def test_an_unlabelled_body_stays_selectable_with_the_uncertainty_said_out_loud(h, tmp_path):
    import dataclasses
    from raigolmid.compatibility import body_toolbelt
    body = dataclasses.replace(h.session.catalogue.bodies["myapi"], runtime=None)
    verdict = body_toolbelt(body, h.session.catalogue.toolbelts["python-dev"])
    assert verdict.selectable is True
    assert "compatibility unknown" in verdict.warning


# --- the tabs that always exist -------------------------------------------------

def test_ensure_tabs_opens_the_machine_tab_and_the_selected_bodys(h):
    h.session.ensure_tabs()
    machine = h.session.intent.machine_tab()
    assert machine is not None and machine.body is None and not machine.manager
    assert [t.tab_id for t in h.session.intent.tabs.values()] == [machine.tab_id]

    h.session.select("body", "myapi")
    tab = h.session.intent.body_tab("myapi")
    assert tab is not None and tab.tab_id != machine.tab_id
    assert h.runtime.inspect(naming.agent(tab.tab_id)).running
    (opened,) = [e for e in h.events_of("tab.opened") if e.tab == tab.tab_id]
    assert (opened.data["body"], opened.data["by"]) == ("myapi", "user")
    assert h.session.status()["agents"][-1]["scope"] == {"body": "myapi"}
    assert {a["scope"] for a in h.session.status()["agents"][:1]} == {"machine"}

    h.session.ensure_tabs()
    assert len(h.session.intent.tabs) == 2, "a tab that exists is not opened again"


def test_no_tab_opens_without_a_credential(h):
    """No agent could start, so nothing is opened for one; the terminal says why."""
    h.paths.agent_credentials.unlink()
    h.session.ensure_tabs()
    h.session.select("body", "myapi")
    assert h.session.intent.tabs == {}
    assert h.session.status()["agents"] == []


def test_selecting_a_body_that_has_a_tab_opens_no_second(h):
    """One tab per body is what makes sharing the user's working copy safe."""
    h.session.select("body", "myapi")
    tab = h.tab("myapi")
    h.session.select("body", "webui")
    h.session.select("body", "myapi")
    assert h.tab("myapi") == tab
    assert [t.body for t in h.session.intent.tabs.values()] == [None, "myapi", "webui"]
    assert len(h.events_of("tab.opened")) == 3


def test_the_face_follows_the_selected_bodys_tab_and_the_machine_tabs_work(h):
    """The active sandbox is derived: the selected body's tab's, or with no body the
    machine tab's `work`, and none while that tab has none open."""
    machine = h.open_sandbox(None, "python-dev")
    assert machine == naming.WORK and h.session.intent.focused_instance == naming.WORK

    h.session.select("body", "myapi")
    assert h.session.intent.focused_instance is None
    assert h.events_of("focus.changed")[-1].instance is None
    assert naming.WORK in h.session.instances.all(), "the machine tab still holds it"

    myapi = h.open_sandbox("myapi", "python-dev")
    assert h.session.intent.focused_instance == myapi
    h.session.deselect("body")
    assert h.session.intent.focused_instance == naming.WORK
    assert h.events_of("focus.changed")[-1].instance == naming.WORK
    assert myapi in h.session.instances.all()

    with pytest.raises(AttributeError):
        h.session.intent.focused_instance = myapi


def test_the_manager_selects_nothing(h):
    h.session.open_manager()
    with pytest.raises(SessionError, match="manager changes no layer"):
        h.session.select("body", "myapi", by_tab="manager")
    with pytest.raises(SessionError, match="manager changes no layer"):
        h.session.deselect("face", by_tab="manager")
    assert h.session.intent.selection.body is None


def test_a_body_tab_that_finishes_stays_and_keeps_its_sandbox(h):
    """No tab closes itself: finishing it is the user's, whatever is selected."""
    sandbox = h.open_sandbox("myapi", "python-dev")
    tab = h.tab("myapi")
    h.session.agent_activity(tab, busy=True)
    h.session.select("body", "webui")

    h.session.agent_activity(tab, busy=False, done=True)

    assert tab in h.session.intent.tabs
    assert sandbox in h.session.instances.all()


def test_closing_the_selected_bodys_tab_stops_its_sandbox_and_opens_a_fresh_one(h):
    sandbox = h.open_sandbox("myapi", "python-dev")
    old = h.tab("myapi")

    h.session.close_tab(old)

    assert sandbox not in h.session.instances.all()
    assert h.session.intent.focused_instance is None
    assert h.events_of("focus.changed")[-1].instance is None
    fresh = h.tab("myapi")
    assert fresh != old and h.runtime.inspect(naming.agent(fresh)).running
    assert h.runtime.inspect(naming.agent(old)) is None


def test_closing_a_body_tab_that_is_not_selected_opens_nothing(h):
    h.session.select("body", "myapi")
    tab = h.tab("myapi")
    h.session.select("body", "webui")
    h.session.close_tab(tab)
    assert h.session.intent.body_tab("myapi") is None
    assert len(h.session.intent.tabs) == 2


def test_the_manager_tab_is_not_closed_by_the_user(h):
    h.session.open_manager()
    with pytest.raises(SessionError, match="the manager tab is the daemon's"):
        h.session.close_tab("manager")


# --- intent survives a restart ----------------------------------------------------

def test_intent_is_persisted_and_reloaded(h, tmp_path, monkeypatch):
    sandbox = h.open_sandbox("myapi", "python-dev")
    tab = h.tab("myapi")

    from raigolmid.intent import IntentStore
    reloaded = IntentStore(h.paths.intent).load()
    assert reloaded.selection.body == "myapi"
    assert reloaded.tabs[tab].body == "myapi"
    assert reloaded.instances[sandbox].toolbelt == "python-dev"
    assert reloaded.instances[sandbox].refs == [naming.tab_ref(tab)]
    assert reloaded.focused_instance == sandbox
    assert reloaded.next_tab == h.session.intent.next_tab


def test_a_toolbelt_lock_is_written_once_the_closure_has_worked(h, monkeypatch):
    """Reproducibility comes from the lock file, not the authoring format. It is
    written after the closure has demonstrably worked — a view built on it and answered."""
    import json

    from raigolmid.views import Views
    monkeypatch.setattr(Views, "store_paths",
                        lambda _self, instance, limit=4096: ("/nix/store/abc-bash-5.2",))
    h.open_sandbox("myapi", "python-dev")

    toolbelt = h.session.catalogue.toolbelts["python-dev"]
    assert toolbelt.lock_path.is_file(), "no toolbelt.lock was written"
    lock = json.loads(toolbelt.lock_path.read_text())
    assert lock["package_digest"] == toolbelt.package_digest
    assert lock["store_paths"] == ["/nix/store/abc-bash-5.2"]
    # By its manifest digest: Nixery answers a name with its current channel's closure.
    named = nixery_reference(toolbelt.packages)
    assert lock["image"] == f"{named}@{h.runtime.image(named).id}"
    sandbox = h.session.intent.focused_instance
    h.session.repair(sandbox)
    assert [e for e in h.events_of("view.created") if e.instance == sandbox][-1].data[
        "image"] == lock["image"]


def test_a_view_built_from_its_own_lock_leaves_it_untouched(h, monkeypatch):
    """The definition directory is watched: a rewrite of an unchanged lock is a rediscover
    for nothing, on every view."""
    from raigolmid.views import Views
    monkeypatch.setattr(Views, "store_paths",
                        lambda _self, instance, limit=4096: ("/nix/store/abc-bash-5.2",))
    sandbox = h.open_sandbox("myapi", "python-dev")
    toolbelt = h.session.catalogue.toolbelts["python-dev"]
    written = toolbelt.lock_path.read_text()

    h.session.repair(sandbox)

    assert len([e for e in h.events_of("view.created") if e.instance == sandbox]) \
        == 2, "no second view was built"
    assert toolbelt.lock_path.read_text() == written, "an unchanged lock was rewritten"


def test_a_repaired_view_is_the_next_generation(h):
    """A new view of the same instance increments the generation; a repair
    that starts it again makes the repaired view indistinguishable from the first."""
    sandbox = h.open_sandbox("myapi", "python-dev")
    h.session.repair(sandbox)
    assert [e.data["generation"] for e in h.events_of("view.created")
            if e.instance == sandbox] == [1, 2]
    assert h.session.views.generation_of(sandbox) == 2


def test_a_lock_that_cannot_be_written_does_not_take_the_view_down(h, monkeypatch):
    """The view is working; the lock is what makes the *next* resolution reproducible. The
    honest response is to say so loudly, not to tear down an environment the user is in."""
    from raigolmid.views import Views, ViewError

    def refuse(_self, instance, limit=4096):
        raise ViewError("the view would not list /nix/store")

    monkeypatch.setattr(Views, "store_paths", refuse)
    sandbox = h.open_sandbox("myapi", "python-dev")

    assert h.session.instances.get(sandbox).health == "ok"
    assert h.events_of("toolbelt.lock_failed"), "the failure was swallowed"


def test_the_digest_and_the_build_read_the_same_copy_of_the_files(h, tmp_path):
    """The body definition lives in the project repo. When `working_copy` points
    somewhere other than the definition directory, both the definition digest and the
    build context must follow it — otherwise a dependency change moves the digest while
    the build reads a different, stale `requirements.txt`, and the rebuild produces an
    image that does not contain the new dependency."""
    from raigolmid.definitions import load_body

    project = tmp_path / "project"
    project.mkdir()
    definition = h.search.bodies[0] / "myapi"
    for name in ("Dockerfile", "requirements.txt"):
        (project / name).write_text((definition / name).read_text())
    _point_at(definition, project)

    body = load_body(definition)
    assert body.source_root == project
    assert body.build_context == project
    assert body.dockerfile == project / "Dockerfile"

    before = body.definition_digest()
    (project / "requirements.txt").write_text("requests==9.9.9\n")
    assert body.definition_digest() != before, \
        "a change in the working copy did not move the digest"

    (definition / "requirements.txt").write_text("this-is-the-stale-copy\n")
    assert body.definition_digest() == load_body(definition).definition_digest(), \
        "the digest is reading the definition directory rather than the working copy"


def _point_at(definition: Path, project: Path) -> None:
    """Set `working_copy` as a top-level key. Appending it would land inside the trailing
    `[[develop.watch]]` table, which the loader rejects — correctly."""
    text = (definition / "body.toml").read_text()
    head, _, tables = text.partition("[[develop.watch]]")
    (definition / "body.toml").write_text(
        f'{head.rstrip()}\nworking_copy = "{project}"\n\n[[develop.watch]]{tables}')


def test_a_restart_that_cannot_start_the_agent_leaves_the_tab_crashed(h):
    """The old container is gone by then. Left `running`, the tab reads as a running agent."""
    h.open_sandbox("myapi", "python-dev")
    tab = h.tab("myapi")
    h.session.agents.template_path.write_text("{nonsense}", encoding="utf-8")

    with pytest.raises(AgentError):
        h.session.restart_agent(tab)

    assert h.session.intent.tabs[tab].status == "crashed"
    assert h.events_of("agent.crashed")
    assert not h.events_of("agent.restarted"), "a restart that did not happen was reported"


def test_a_closed_tabs_home_is_archived_and_the_fresh_tab_starts_fresh(h):
    """The home holds the conversation `claude --continue` resumes: kept for `/resume`,
    and closing the tab is how its context is cleared."""
    h.session.select("body", "myapi")
    tab = h.tab("myapi")
    home = h.session.agents.home(tab)
    converse(home)
    h.session.close_tab(tab)
    assert not home.exists()
    (closed,) = h.events_of("tab.closed")
    archived = h.paths.agent_archive / closed.data["archive"]
    assert list((archived / ".claude" / "projects" / "-work").glob("*.jsonl"))
    assert not h.session.agents.has_conversation(h.tab("myapi"))


def test_a_new_tab_refuses_a_home_an_earlier_tab_left(h):
    """Ids are never reused, so a home under the next one is not this daemon's: left by an
    intent that was lost, and never resumed as if it were the new tab's."""
    h.session.select("body", "myapi")
    upcoming = f"tab-{h.session.intent.next_tab}"
    (h.session.agents.home(upcoming) / ".claude").mkdir(parents=True)
    with pytest.raises(AgentError, match=f"left from an earlier {upcoming}"):
        h.session.close_tab(h.tab("myapi"))
    assert upcoming not in h.session.intent.tabs


def test_a_home_the_intent_no_longer_names_is_archived_at_reconcile(h):
    """A tab lost with its intent is closed: its conversation goes to the archive,
    and the manager's home, which is not a numbered tab, is left alone."""
    lost = h.session.agents.home("tab-40")
    converse(lost)
    (h.session.agents.home("manager") / ".claude").mkdir(parents=True, exist_ok=True)
    h.session.reconcile()
    assert not lost.exists()
    (archived,) = h.paths.agent_archive.glob("tab-40-*[0-9]")
    assert list((archived / ".claude" / "projects" / "-work").glob("*.jsonl"))
    assert h.session.agents.home("manager").exists()


def test_a_restarted_agent_keeps_its_home(h):
    h.open_sandbox("myapi", "python-dev")
    tab = h.tab("myapi")
    marker = h.session.agents.home(tab) / ".claude" / "kept"
    marker.write_text("the conversation")
    h.session.restart_agent(tab, resume=True)
    assert marker.read_text() == "the conversation"


def test_a_started_faces_editor_window_opens_after_the_sync_and_once(tmp_path, monkeypatch):
    from tests.harness import Harness

    h = Harness(tmp_path, monkeypatch)
    session = h.session
    order: list[str] = []
    monkeypatch.setattr(session.faces, "switch", lambda face: None)
    monkeypatch.setattr(session.face_mounts, "show", lambda instance: order.append("sync"))
    monkeypatch.setattr(session.faces, "open_editor_window",
                        lambda face: order.append(f"window {face.id}"))
    face = session.catalogue.faces["backend-focus"]
    assert face.editor is not None

    session._bring_up_face(face)
    session._show(None)
    session._show(None)
    assert order == ["sync", "window backend-focus", "sync"]


# --- the door --------------------------------------------------------------------

def _same_port_as_myapi(h) -> None:
    toml = h.search.bodies[0] / "webui" / "body.toml"
    toml.write_text(toml.read_text().replace("ports = [8001]", "ports = [8000]"))
    h.session.rediscover()


def test_only_the_focused_sandboxs_ports_reach_the_host_through_the_door(h):
    """A host port is exclusive, so no anchor publishes; two bodies' tabs whose
    bodies listen on the same port both run, and the door publishes the focused one's."""
    _same_port_as_myapi(h)
    myapi = h.open_sandbox("myapi", "python-dev")
    webui = h.open_sandbox("webui", "python-dev")

    for sandbox in (myapi, webui):
        assert h.runtime.inspect(naming.body_container(sandbox)).running
        assert h.runtime.spec_of(naming.anchor(sandbox)).ports == {}
        assert h.session.instances.get(sandbox).ports == (8000,)
    door = h.runtime.spec_of(naming.door())
    assert door.ports == {8000: 8000}
    assert h.runtime.inspect(naming.anchor(webui)).ip in door.command
    assert h.runtime.inspect(naming.door()).labels[labels.DOOR].startswith(webui + " ")


def test_the_door_is_replaced_when_the_face_moves_and_kept_when_it_does_not(h):
    _same_port_as_myapi(h)
    myapi = h.open_sandbox("myapi", "python-dev")
    first = h.runtime.inspect(naming.door()).id
    h.session.select("face", "writing")
    assert h.runtime.inspect(naming.door()).id == first, "showing the same thing again"

    webui = h.open_sandbox("webui", "python-dev")
    assert h.runtime.inspect(naming.anchor(webui)).ip in h.runtime.spec_of(naming.door()).command

    h.session.select("body", "myapi")
    door = h.runtime.inspect(naming.door())
    assert door.id != first and door.labels[labels.DOOR].startswith(myapi + " ")
    assert h.runtime.inspect(naming.anchor(myapi)).ip in h.runtime.spec_of(naming.door()).command
    assert h.runtime.inspect(naming.body_container(webui)).running


def test_the_door_closes_with_nothing_to_forward(h):
    """The machine tab's `work` has no body, so nothing listens and nothing is published."""
    h.open_sandbox("myapi", "python-dev")
    assert h.runtime.inspect(naming.door()) is not None
    h.session.select("body", "webui")
    assert h.runtime.inspect(naming.door()) is None
    assert h.events_of("door.closed")

    h.session.deselect("body")
    h.open_sandbox(None, "python-dev")
    assert h.session.intent.focused_instance == naming.WORK
    assert h.runtime.inspect(naming.door()) is None


def test_a_door_that_cannot_open_is_said_and_moves_the_face_anyway(h):
    """What moved the face does not fail on the ports; `status` carries why."""
    from raigolmid.runtime.base import ContainerSpec
    h.runtime.run(ContainerSpec(
        name="squatter", image="registry.k8s.io/pause:3.9", ports={8000: 8000}))
    sandbox = h.open_sandbox("myapi", "python-dev")

    assert h.session.intent.focused_instance == sandbox
    assert "8000" in h.session.status()["face_runtime"]["door_error"]
    assert h.events_of("door.failed")[-1].instance == sandbox

    h.runtime.remove("squatter", force=True)
    h.session.select("body", "myapi")
    assert h.session.status()["face_runtime"]["door_error"] is None


def test_a_body_runs_as_the_owner_of_its_working_copy(h):
    """What the body writes in /work is the user's, never root's."""
    instance = h.open_sandbox()
    owner = Path(h.session.intent.instances[instance].working_copy).stat()
    service = json.loads(h.session.instances.compose_file(instance).read_text(
        encoding="utf-8"))["services"]["body"]
    assert service["user"] == f"{owner.st_uid}:{owner.st_gid}"
