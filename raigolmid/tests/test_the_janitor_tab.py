"""The janitor tab: the failures it takes, its machine scope, and their delivery on its
channel (`channel.py`, whose own rules are `test_the_channel.py`'s)."""
from __future__ import annotations

import pytest

from raigolmid import channel as channel_module, labels, naming
from raigolmid.api import build_methods
from raigolmid.channel import Channels
from raigolmid.intent import JANITOR
from raigolmid.janitor import Janitor
from raigolmid.questions import Questions
from raigolmid.history import History
from raigolmid.viewing import Viewing
from raigolmid.runtime import ContainerSpec
from raigolmid.session import SessionError

from tests.harness import Harness


@pytest.fixture()
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.session.select("body", "myapi")
    return harness


@pytest.fixture()
def m(h):
    janitor = Janitor(h.session, h.events)
    janitor.channels = Channels(h.session, h.events)
    return janitor


def _route(m) -> None:
    """What the janitor's thread does, run once: every event emitted so far is routed. The
    channel catches up on its own whenever it is read."""
    for event in m._sub.drain(timeout=0):
        m.on_event(event)


def _fail(h, m, type_="instance.degraded", instance="myapi@tab-2", **data) -> None:
    h.events.emit(type_, instance=instance, **data)
    _route(m)


def _up(h, m) -> None:
    """The janitor's SessionStart hook, as every real janitor session reports."""
    h.session.agent_session_started(JANITOR)
    _route(m)


def _report(h, m, busy: bool, seq: int | None = None) -> None:
    h.session.agent_activity(JANITOR, busy, seq)
    _route(m)


def _take(m):
    return m.channels.take(JANITOR)


def _queued(m) -> list[str]:
    return [q["meta"]["failure"] for q in m.channels.state(JANITOR)["queued"]]


def test_a_failure_it_takes_opens_the_janitor_scoped_to_the_machine(h, m):
    _fail(h, m, reason="the body exited")
    agents = {a["tab"]: a for a in h.session.status()["agents"]}
    assert agents[JANITOR]["scope"] == "janitor"
    spec = h.runtime._containers[naming.agent(JANITOR)]["spec"]
    assert spec.environment["RAIGOLMI_SCOPE"] == "machine"
    channel = spec.command.index("--channels")
    assert spec.command[channel + 1] == "plugin:raigolmi@raigolmi", \
        "an approved plugin channel is the only one that starts with no confirmation"
    assert labels.INSTANCE not in spec.labels
    work = next(mount.source for mount in spec.mounts if mount.target == "/work")
    assert work == str(h.session.definitions_root())
    context = (h.session.agents.home(JANITOR) / ".claude" / "CLAUDE.md").read_text()
    assert "janitor tab" in context and "{" not in context


def test_the_janitor_reads_the_disks_own_source_and_no_tab_does(h, m):
    """So it understands the whole OS: read-only, and only the janitor's."""
    from raigolmid import hostimages
    _fail(h, m, reason="the body exited")
    spec = h.runtime._containers[naming.agent(JANITOR)]["spec"]
    [source] = [mt for mt in spec.mounts if mt.target == "/source"]
    assert source.read_only and source.source == str(hostimages.source_root())
    tab = h.runtime._containers[naming.agent(h.tab("myapi"))]["spec"]
    assert "/source" not in {mt.target for mt in tab.mounts}


def test_what_it_does_not_take_opens_nothing(h, m):
    for type_ in ("build.failed", "api.error", "agent.stop_failed", "hostkeys.failed"):
        h.events.emit(type_)
    _route(m)
    assert JANITOR not in h.session.intent.tabs
    assert _queued(m) == []


def test_a_failure_opens_one_incident_doc_the_janitor_reaches_and_a_recurrence_joins_it(h, m):
    """The daemon opens the doc, so there is one per incident whether or not
    the janitor writes one; a doc moved to fixed/ closes the incident."""
    root = h.paths.janitor_documents
    _fail(h, m, reason="the body exited")
    spec = h.runtime._containers[naming.agent(JANITOR)]["spec"]
    assert {mt.target: mt.source for mt in spec.mounts}["/janitor"] == str(root)
    [doc] = (root / "incidents").glob("*.md")
    assert doc.name.endswith("-myapi@tab-2.md")
    _up(h, m)
    assert f"/janitor/incidents/{doc.name}" in _take(m)["content"]

    _fail(h, m, reason="exited again")
    assert list((root / "incidents").glob("*.md")) == [doc]
    assert "exited again" in doc.read_text()

    doc.rename(root / "incidents" / "fixed" / doc.name)
    _fail(h, m, reason="back after the fix")
    assert len(list((root / "incidents").glob("*.md"))) == 1

    from raigolmid.scopes import MACHINE, build_machine_methods
    methods = build_machine_methods({name: None for name in MACHINE}, h.session,
                                    Questions(h.events, h.paths), m.channels)
    found = methods["index"]()
    assert [d["path"] for d in found["documents"]] == [
        str(next((root / "incidents").glob("*.md")))]
    assert found["fixed"] == [str(root / "incidents" / "fixed" / doc.name)]
    assert str(root / "patterns.md") in {d["path"] for d in found["missing"]}


def test_an_incident_moved_to_fixed_is_said_at_the_end_of_the_janitors_turn(h, m):
    """What came of a failure the janitor was handed."""
    root = h.paths.janitor_documents
    _fail(h, m, reason="the body exited")
    [doc] = (root / "incidents").glob("*.md")
    _up(h, m)
    _report(h, m, busy=True)
    doc.rename(root / "incidents" / "fixed" / doc.name)
    assert h.events_of("janitor.incident_fixed") == [], "not until its turn ends"
    _report(h, m, busy=False)
    _report(h, m, busy=False)
    (fixed,) = h.events_of("janitor.incident_fixed")
    assert fixed.data["incident"] == f"/janitor/incidents/fixed/{doc.name}"


def test_a_doc_naming_the_dead_sends_the_janitor_one_job_until_its_facts_change(h, m):
    """On facts, not a count — the same facts never page it twice."""
    from raigolmid.maintenance import Maintenance
    upkeep = Maintenance(h.session, h.events)
    _every_layer_documented(h)
    toolbelt = h.session.catalogue.toolbelts["python-dev"].directory
    doc = toolbelt / "LAYER.md"
    doc.write_text(_headed("Built from `toolbelt.toml`.\n"), encoding="utf-8")
    upkeep.sweep()
    upkeep.sweep()
    _route(m)
    assert JANITOR not in h.session.intent.tabs, "a current doc is no job"

    doc.write_text(_headed("Built from `toolbelt.toml` and `gone.nix`.\n"), encoding="utf-8")
    upkeep.sweep()
    upkeep.sweep()
    _route(m)
    assert _queued(m) == ["documents.maintenance"]
    seen = "/definitions/" + str(doc.relative_to(h.session.definitions_root()))
    [incident] = (h.paths.janitor_documents / "incidents").glob("*.md")
    assert seen in incident.read_text() and "`gone.nix`: no such path" in incident.read_text()


def test_a_layer_left_without_a_doc_goes_to_the_janitor_once_no_tab_is_working(h, m):
    """An agent forgets what it is not reminded of, and the janitor takes up the
    slack — but never races the tab still writing the layer."""
    from raigolmid.maintenance import Maintenance
    upkeep = Maintenance(h.session, h.events)
    tab = h.tab("myapi")
    h.session.intent.tabs[tab].busy = True
    upkeep.sweep()
    _route(m)
    assert JANITOR not in h.session.intent.tabs, "a tab at work may be about to write it"

    h.session.intent.tabs[tab].busy = False
    upkeep.sweep()
    upkeep.sweep()
    _route(m)
    missing = [q for q in m.channels.state(JANITOR)["queued"]]
    assert missing and all(q["meta"]["failure"] == "documents.maintenance" for q in missing)
    [incident, *_] = sorted((h.paths.janitor_documents / "incidents").glob("*.md"))
    assert "missing: the layer at" in incident.read_text()


def _headed(text, shape="bounded", audited=None):
    body = text
    size = audited if audited is not None else len(body.encode())
    return (f"<!-- purpose: what it is\nnot-here: anything else, in its own doc\n"
            f"shape: {shape}\naudited: {size} 2026-10-02\n-->\n{body}")


def _every_layer_documented(h):
    for table in (h.session.catalogue.faces, h.session.catalogue.toolbelts,
                  h.session.catalogue.bodies):
        for item in table.values():
            if item.directory:
                for doc in item.directory.rglob("*.md"):
                    doc.write_text(_headed("What it is.\n"), encoding="utf-8")
                (item.directory / "LAYER.md").write_text(_headed("What it is.\n"),
                                                         encoding="utf-8")


def test_a_bodys_docs_are_judged_only_while_its_tab_is_idle(h, m):
    """A body's tab declares every doc it read before its turn ends, so its docs are judged at
    rest and never while it may be mid-edit — and a kept tab does not shield them for good."""
    from raigolmid.maintenance import Maintenance
    upkeep = Maintenance(h.session, h.events)
    _every_layer_documented(h)
    tab = h.tab("myapi")
    h.session.intent.tabs[tab].busy = True
    doc = h.session.catalogue.bodies["myapi"].directory / "LAYER.md"
    doc.write_text(_headed("Runs `gone.py`.\n"), encoding="utf-8")
    upkeep.sweep()
    _route(m)
    assert JANITOR not in h.session.intent.tabs

    h.session.intent.tabs[tab].busy = False
    upkeep.sweep()
    _route(m)
    assert _queued(m) == ["documents.maintenance"]


def test_every_doc_of_a_working_copy_is_held_to_its_header_in_one_job(h, m):
    """A doc with no purpose header, and one grown a quarter past its audit, are one job for
    the body that owns them — and growth is one fact however far it goes. A project's own doc
    names files as it pleases; only a layer doc and SESSION-START.md are held to what they cite."""
    from raigolmid.maintenance import Maintenance
    upkeep = Maintenance(h.session, h.events)
    _every_layer_documented(h)
    copy = h.session.catalogue.bodies["myapi"].source_root
    (copy / "docs").mkdir(exist_ok=True)
    (copy / "docs" / "notes.md").write_text("Notes on `market.rs`.\n", encoding="utf-8")
    design = copy / "DESIGN.md"
    design.write_text(_headed("x" * 1000 + "\n", audited=1000), encoding="utf-8")
    upkeep.sweep()
    design.write_text(_headed("x" * 8000 + "\n", audited=1000), encoding="utf-8")
    upkeep.sweep()
    design.write_text(_headed("x" * 9000 + "\n", audited=1000), encoding="utf-8")
    upkeep.sweep()
    jobs = h.events_of("documents.maintenance")
    assert [job.data["document"] for job in jobs] == ["/definitions/bodies/myapi"] * 2
    (h.paths.janitor_documents / "patterns.md").parent.mkdir(parents=True, exist_ok=True)
    (h.paths.janitor_documents / "patterns.md").write_text("A pattern.\n", encoding="utf-8")
    upkeep.sweep()
    assert h.events_of("documents.maintenance")[-1].data["document"] == "/janitor"
    first, grown = (job.data["documents"] for job in jobs)
    assert first == {"/definitions/bodies/myapi/docs/notes.md":
                     ["header: it does not open with its purpose header"]}
    [(name, [reason])] = grown.items()
    assert name.endswith("/DESIGN.md") and reason.startswith("grown since its audit: from 1000")


def test_a_definition_is_judged_only_while_the_machine_tab_is_not_changing_it(h, m):
    from raigolmid.maintenance import Maintenance
    upkeep = Maintenance(h.session, h.events)
    _every_layer_documented(h)
    machine = h.session.intent.machine_tab()
    assert machine is not None
    machine.busy = True
    doc = h.session.catalogue.toolbelts["python-dev"].directory / "LAYER.md"
    doc.write_text(_headed("Built from `gone.nix`.\n"), encoding="utf-8")
    upkeep.sweep()
    _route(m)
    assert JANITOR not in h.session.intent.tabs, "the machine tab may be mid-edit"

    machine.busy = False
    upkeep.sweep()
    _route(m)
    assert _queued(m) == ["documents.maintenance"]


def test_a_stale_doc_is_one_fact_whichever_files_move(h, m):
    """One job per fact: a tab editing its layer file by file pages the janitor once."""
    import os
    from raigolmid.maintenance import Maintenance
    upkeep = Maintenance(h.session, h.events)
    _every_layer_documented(h)
    layer = h.session.catalogue.toolbelts["python-dev"].directory
    os.utime(layer / "LAYER.md", (1, 1))
    (layer / "a.nix").write_text("{}\n", encoding="utf-8")
    upkeep.sweep()
    (layer / "b.nix").write_text("{}\n", encoding="utf-8")
    upkeep.sweep()
    _route(m)
    stale = [e for e in h.events_of("documents.maintenance")
             if any(d.endswith("python-dev/LAYER.md") for d in e.data["documents"])]
    assert len(stale) == 1 and stale[0].data["document"].endswith("python-dev")


def test_one_failure_said_twice_is_one_incident_and_one_message(h, m):
    """A sandbox part's failed restart degrades the sandbox and spends its restart: the janitor
    is sent to one doc holding both, once. Said again after it was taken, it is news."""
    root = h.paths.janitor_documents
    _fail(h, m, reason="replacing the body failed: no space")
    _fail(h, m, "container.unfixable", kind="body", unit="myapi@tab-2",
          message="did not restart")
    [doc] = (root / "incidents").glob("*.md")
    assert "no space" in doc.read_text() and "did not restart" in doc.read_text()
    assert _queued(m) == ["instance.degraded"]
    _up(h, m)
    assert _take(m)["meta"]["incident"] == f"/janitor/incidents/{doc.name}"
    _fail(h, m, reason="again")
    assert _queued(m) == ["instance.degraded"]


def test_failures_are_pushed_one_at_a_time_and_only_to_an_idle_janitor(h, m):
    _fail(h, m)
    _fail(h, m, "reconcile.failed", instance="other@tab-3")
    _up(h, m)
    _report(h, m, busy=True)
    assert _take(m) is None, "a push into a busy turn is refused as untrusted"
    _report(h, m, busy=False)
    first = _take(m)
    assert first["meta"]["failure"] == "instance.degraded"
    assert "instance.degraded" in first["content"]
    assert _take(m) is None, "the next waits until the first is heard and its turn ends"
    _report(h, m, busy=True, seq=first["seq"])
    assert m.channels.state(JANITOR)["pushed"]["heard"]
    assert _take(m) is None
    _report(h, m, busy=False)
    assert _take(m) is None, "the next failure is for a fresh session"
    _route(m)                           # the janitor restarts without its conversation
    assert h.events_of("agent.restarted")[-1].tab == JANITOR
    _up(h, m)
    second = _take(m)
    assert second["meta"]["failure"] == "reconcile.failed"
    assert second["seq"] == first["seq"] + 1


def test_a_restarted_janitor_waits_for_its_new_session(h, m):
    _fail(h, m)
    _up(h, m)
    h.session.restart_agent(JANITOR, resume=True)
    _route(m)
    assert _take(m) is None, "pushed into a container whose session is not up"
    _up(h, m)
    assert _take(m) is not None


def test_another_tabs_deaf_channel_is_the_janitors(h, m):
    h.events.emit("channel.unheard", tab="tab-1", seq=1, cause="question.answered", pushes=3,
                  message=channel_module.unheard_message("tab-1"))
    _route(m)
    assert _queued(m) == ["channel.unheard"]


def test_the_janitor_past_reopening_is_the_users_not_a_janitors(h, m):
    _fail(h, m)
    h.events.emit("container.unfixable", tab=JANITOR, kind="agent", unit=JANITOR,
                  message="exited again")
    _route(m)
    assert h.events_of("janitor.unfixable")
    assert _queued(m) == ["instance.degraded"]


def test_the_janitor_is_neither_the_machine_tab_nor_a_bodys_nor_in_their_way(h, m):
    _fail(h, m)
    assert {a["tab"]: a["scope"] for a in h.session.status()["agents"]} == {
        h.tab(None): "machine", h.tab("myapi"): {"body": "myapi"}, JANITOR: "janitor"}
    h.session.deselect("body")
    h.session.close_tab(h.tab(None))
    assert JANITOR in h.session.intent.tabs
    assert h.session.intent.machine_tab().tab_id != JANITOR, "a fresh machine tab opened"
    assert h.session.intent.face_tab() is not h.session.intent.tabs[JANITOR]


def test_the_janitor_id_is_the_daemons_and_it_selects_and_closes_nothing(h, m):
    _fail(h, m)
    with pytest.raises(SessionError, match="the janitor tab is the daemon's"):
        h.session.close_tab(JANITOR)
    with pytest.raises(SessionError, match="janitor changes no layer"):
        h.session.select("body", "webui", by_tab=JANITOR)
    with pytest.raises(SessionError, match="opens no sandbox"):
        h.session.sandbox_open(JANITOR, "python-dev")


def test_with_no_credential_the_janitor_is_not_tried_says_so_once_and_opens_when_one_is_stored(h, m):
    """A fresh machine's first start sends failures before the user has stored a credential."""
    from raigolmid import credential
    h.paths.agent_credentials.unlink()
    _fail(h, m)
    _fail(h, m, "reconcile.failed", instance="other@tab-3")
    _fail(h, m, "face.failed", instance="third@tab-4")
    assert len(h.events_of("janitor.open_failed")) == 1, "one reason, said once"
    assert not [e for e in h.events_of("tab.opened") if e.tab == JANITOR], \
        "no agent can start without a credential"
    credential.write(h.paths.agent_credentials, "CLAUDE_CODE_OAUTH_TOKEN", "token")
    h.events.emit("credential.stored")
    _route(m)
    assert JANITOR in h.session.intent.tabs
    _up(h, m)
    _report(h, m, busy=False)
    assert _take(m)["meta"]["failure"] == "instance.degraded", "the held failures are delivered"
    assert _queued(m) == ["reconcile.failed", "face.failed"]


def test_container_logs_reads_only_what_raigolmid_manages(h, m):
    methods = build_methods(h.session, h.events, Questions(h.events, h.paths), m.channels, Viewing(h.events, h.paths.viewing), History(h.events, h.paths))
    h.runtime.add_image("busybox")
    h.runtime.run(ContainerSpec(name="someone-elses", image="busybox"))
    with pytest.raises(SessionError, match="not one raigolmid manages"):
        methods["container_logs"](container="someone-elses")
    _fail(h, m)
    assert methods["container_logs"](container=naming.agent(JANITOR))["running"]


def test_crash_logs_lists_and_reads_only_the_crash_directory(h, m):
    methods = build_methods(h.session, h.events, Questions(h.events, h.paths), m.channels, Viewing(h.events, h.paths.viewing), History(h.events, h.paths))
    (h.paths.crashes / "tab-1-x.log").write_text("exit code: 1\n")
    assert methods["crash_logs"]()["logs"] == ["tab-1-x.log"]
    assert methods["crash_logs"](name="tab-1-x.log")["log"].startswith("exit code: 1")
    with pytest.raises(SessionError):
        methods["crash_logs"](name="../events.jsonl")


def test_the_terminal_marks_the_janitors_window():
    from ui.ai_terminal.terminal import window_name
    assert window_name(JANITOR, "janitor") == "janitor ⚙"


def test_the_janitor_closes_a_failing_tab_with_why_and_never_a_managed_one(h, m):
    tab = h.tab("myapi")
    with pytest.raises(SessionError, match="needs why"):
        h.session.close_tab(tab, why=" ")
    h.session.manage(tab, True, why="test")
    with pytest.raises(SessionError, match="managed by the machine tab"):
        h.session.close_tab(tab, why="crashes as it comes back")
    h.session.manage(tab, False, why="test")

    h.session.close_tab(tab, why="crashes as it comes back")

    assert tab not in h.session.intent.tabs
    [closed] = [e for e in h.events_of("tab.closed") if e.tab == tab]
    assert (closed.data["by"], closed.data["why"]) == ("janitor", "crashes as it comes back")
    from raigolmid.history import _closed
    assert _closed(closed).startswith("closed by the janitor: crashes as it comes back")


def test_the_machine_state_is_what_raigolmid_manages_and_an_unaddressable_sway_is_unasked(h, m):
    methods = build_methods(h.session, h.events, Questions(h.events, h.paths), m.channels, Viewing(h.events, h.paths.viewing), History(h.events, h.paths))
    h.runtime.add_image("busybox")
    h.runtime.run(ContainerSpec(name="someone-elses", image="busybox"))
    _fail(h, m)
    h.session.faces.host.swaysock = ""
    state = methods["machine_state"]()
    names = [c["name"] for c in state["containers"]]
    assert naming.agent(JANITOR) in names and "someone-elses" not in names
    assert "unasked" in state["sway"]
