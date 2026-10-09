"""Agent containers and the scoped MCP server."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from raigolmid.channel import Channels
from raigolmid.questions import Questions
from raigolmid import credproxy, naming
from raigolmid.agents import AgentError, AgentSpec
from raigolmid.intent import TabIntent
from raigolmid.session import SessionError

from tests.harness import Harness, converse


@pytest.fixture()
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.session.select("body", "myapi")
    return harness


# A tab id no tab in the harness has: an agent started from `_spec` meets no running one.
SPARE = "tab-9"


def _spec(h, tab_id=SPARE) -> AgentSpec:
    tab = TabIntent(tab_id=tab_id, body="myapi")
    return AgentSpec(tab=tab, instance_id=naming.instance_id("myapi", tab_id),
                     working_copy=h.session._place(tab).working_copy)


def test_the_default_is_rendered_unwritten_and_a_users_template_replaces_it(h):
    """A default copied into the user's config goes stale the next time the default changes."""
    agents = h.session.agents
    assert "Working in RaiGolmi" in agents.render_context(_spec(h))
    assert not agents.template_path.exists()
    agents.template_path.parent.mkdir(parents=True, exist_ok=True)
    agents.template_path.write_text("the user's own words\n", encoding="utf-8")
    assert agents.render_context(_spec(h)) == "the user's own words\n"


def test_claude_gets_a_user_level_context_outside_the_repo(h, tmp_path):
    home = tmp_path / "agent-home"
    placement = h.session.agents.write_context(_spec(h), home)
    assert placement["placement"] == "user-level"
    assert (home / ".claude" / "CLAUDE.md").is_file()
    assert not (h.session.catalogue.bodies["myapi"].source_root / "AGENTS.md").exists()


def test_an_agent_container_gets_no_docker_socket(h):
    h.session.agents.start(_spec(h), fresh_home=True)
    spec = h.runtime._containers[naming.agent(SPARE)]["spec"]
    targets = {m.target for m in spec.mounts}
    assert "/var/run/docker.sock" not in targets
    assert "/run/raigolmid" in targets, "the scoped API socket is the only way out"
    assert spec.cap_drop == ("ALL",)


def test_the_users_git_hooks_and_config_are_read_only_in_an_agent(h):
    """Nothing an agent runs may make the host's git execute code."""
    h.session.agents.start(_spec(h), fresh_home=True)
    spec = h.runtime._containers[naming.agent(SPARE)]["spec"]
    work = Path(next(m.source for m in spec.mounts if m.target == "/work"))
    protected = {m.target: m for m in spec.mounts if m.target.startswith("/work/.git/")}
    assert set(protected) >= {"/work/.git/hooks", "/work/.git/config"}
    for target, mount in protected.items():
        assert mount.read_only, f"{target} is writable in the agent"
        assert Path(mount.source) == work / target.removeprefix("/work/")
    # The MCP server's path pairs are the project directories and the home, never the
    # .git overlays.
    assert [t for _, t in json.loads(spec.environment["RAIGOLMI_MOUNTS"])] == [
        "/work", "/guide", "/transfer", "/agent/plugins", "/home/agent"]


def test_ref_state_is_recorded_before_a_body_tabs_agent_starts(h):
    recorded = [e for e in h.events_of("git.refs_before_agent") if e.tab == h.tab("myapi")]
    assert recorded, "the repository's refs must be recorded before a body tab's agent starts"
    assert "refs/heads/main" in recorded[-1].data["refs"]


def test_the_old_containers_session_records_are_gone_before_the_restart(h):
    """A record from another PID namespace reads as live to `--continue`, which then skips
    that session's transcript."""
    tab = h.tab("myapi")
    home = h.session.agents.home(tab)
    converse(home)
    sessions = home / ".claude" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "214.json").write_text('{"kind": "bg", "pidDomain": "linux::pid:[1]"}')
    (sessions / "214.key").write_text("k")
    h.runtime.kill(naming.agent(tab))
    h.session.intent.tabs[tab].status = "crashed"

    h.session.restart_agent(tab, resume=True)

    assert list(sessions.iterdir()) == []
    assert (home / ".claude" / "projects" / "-work").is_dir(), "the conversation is kept"


def test_a_transcript_only_claude_p_wrote_is_not_continued(h):
    """Interactive `claude --continue` skips a `claude -p` transcript and exits, so
    restarting onto one would leave the tab with no agent."""
    tab = h.tab("myapi")
    converse(h.session.agents.home(tab), entrypoint="sdk-cli")
    h.runtime.kill(naming.agent(tab))
    h.session.intent.tabs[tab].status = "crashed"

    h.session.restart_agent(tab, resume=True)

    spec = h.runtime._containers[naming.agent(tab)]["spec"]
    assert "--continue" not in list(spec.command or ())
    assert h.events_of("agent.restarted")[-1].data["resumed"] is False


def test_an_agent_that_exits_on_its_own_is_announced_and_reopened(h):
    """The crash is said when it happens, with the dead container's exit code and
    output kept past the reopen that removes it, and the tab comes back with its
    conversation — the body's tab again, not a new one beside it."""
    tab = h.tab("myapi")
    converse(h.session.agents.home(tab))
    dead = h.runtime.inspect(naming.agent(tab)).id
    list(h.runtime.events())
    h.runtime.kill(naming.agent(tab), exit_code=1)

    h.deliver_runtime_events()

    crashed = h.events_of("agent.crashed")[-1]
    assert crashed.tab == tab and crashed.data["exit_code"] == 1
    assert Path(crashed.data["log"]).read_text().startswith("exit code: 1\n")
    assert h.session.intent.tabs[tab].status == "running"
    container = h.runtime.inspect(naming.agent(tab))
    assert container.running and container.id != dead
    assert h.events_of("agent.restarted")[-1].data["resumed"] is True
    assert set(h.session.intent.tabs) == {h.tab(None), tab}


def test_the_reopened_agent_exiting_on_its_own_goes_to_the_janitor(h):
    """One restart, then the janitor: reopening the container the reopen started
    would only repeat the crash, so the tab stays crashed and the evidence goes to an AI."""
    from raigolmid.janitor import TAKEN
    tab = h.tab("myapi")
    list(h.runtime.events())
    h.runtime.kill(naming.agent(tab), exit_code=1)
    h.deliver_runtime_events()
    assert h.session.intent.tabs[tab].status == "running"

    h.runtime.kill(naming.agent(tab), exit_code=2)
    h.deliver_runtime_events()

    assert h.session.intent.tabs[tab].status == "crashed"
    assert not h.runtime.inspect(naming.agent(tab)).running
    [unfixable] = h.events_of("container.unfixable")
    assert (unfixable.tab, unfixable.data["kind"], unfixable.data["exit_code"]) == \
        (tab, "agent", 2)
    assert Path(unfixable.data["evidence"]).read_text().startswith("exit code: 2\n")
    assert "container.unfixable" in TAKEN and "container.exited" not in TAKEN
    logs = {e.data["log"] for e in h.events_of("agent.crashed")}
    assert len(logs) == 2, "each crash keeps its own evidence, however close together"


def test_his_quit_is_not_a_crash_and_resumes_however_often(h):
    """Claude Code exits 0 only when the user quits it: no crash, no restart spent, no
    janitor, and the tab reopens on their conversation each time."""
    tab = h.tab("myapi")
    converse(h.session.agents.home(tab))
    list(h.runtime.events())
    for _ in range(3):
        h.runtime.kill(naming.agent(tab), exit_code=0)
        h.deliver_runtime_events()
        assert h.session.intent.tabs[tab].status == "running"
        assert h.runtime.inspect(naming.agent(tab)).running
        assert h.events_of("agent.restarted")[-1].data["resumed"] is True
    assert len(h.events_of("agent.quit")) == 3
    assert not h.events_of("agent.crashed") and not h.events_of("container.unfixable")


def test_an_agent_the_daemon_stops_is_not_a_crash(h):
    tab = h.tab("myapi")
    list(h.runtime.events())

    h.session.restart_agent(tab, resume=False)
    h.deliver_runtime_events()
    assert h.session.intent.tabs[tab].status == "running"

    h.session.close_tab(tab)
    h.deliver_runtime_events()
    assert not h.events_of("agent.crashed")


def test_the_scope_resolves_the_instance_at_call_time(h):
    """A tab opens its sandbox as it works, so a cached id would be wrong before the
    open; leaving the body leaves the tab's sandbox its own."""
    from raigolmid.scopes import instance_of

    tab = h.tab("myapi")
    with pytest.raises(SessionError, match="no sandbox open"):
        instance_of(h.session, tab)
    h.session.sandbox_open(tab, "python-dev")
    assert instance_of(h.session, tab) == naming.instance_id("myapi", tab)

    h.session.select("body", "webui")
    assert instance_of(h.session, tab) == naming.instance_id("myapi", tab)


def test_a_tabs_status_names_no_other_tab_or_sandbox(h):
    """Nothing in a container can name another tab. A body tab's sandbox id carries its
    tab's name, so another tab's view of `status` must not list it."""
    from raigolmid.scopes import build_tab_methods

    h.session.select("body", "webui")
    other = h.tab("webui")
    h.session.sandbox_open(other, "python-dev")
    mine = h.tab("myapi")
    methods = build_tab_methods(h.session, Questions(h.events, h.paths),
                                Channels(h.session, h.events), mine)
    status = methods["status"]()
    # Changing the selection answers with a status too, and it is the tab's own.
    for answer in (methods["select"]("face", "backend-focus"), methods["deselect"]("face")):
        assert other not in str(answer) and str(h.paths.state) not in str(answer)

    assert other not in str(status)
    assert status["tab"]["tab"] == mine
    assert status["instance"] is None and status["session"]["instances"] == []
    assert status["session"]["on_face"] is False


def test_a_tab_acts_only_on_its_own_sandbox(h):
    """The sandbox is resolved by the socket, never taken from the caller."""
    from raigolmid.scopes import build_tab_methods

    methods = build_tab_methods(h.session, Questions(h.events, h.paths),
                                Channels(h.session, h.events), h.tab("myapi"))
    for name, params in (("exec", {"target": "x@tab-1", "cmd": ["true"]}),
                         ("history", {"instance_id": "x@tab-1"}),
                         ("agent_activity", {"tab_id": "tab-1", "busy": True})):
        with pytest.raises(TypeError, match="unexpected keyword"):
            methods[name](**params)


# --- the recovery premise ---------------------------------------------------------------

def test_an_agent_starts_in_the_bare_host_state(h):
    """The bare host state must run an agent with every layer deselected or broken,
    and it must be able to repair all three. An agent that refuses to start without a
    working instance would be absent exactly when it is needed: the machine tab is that agent."""
    h.session.deselect("body")
    assert h.session.status()["session"]["instances"] == []

    container = h.runtime.inspect(naming.agent(h.tab(None)))
    assert container is not None and container.running, \
        "no agent started in the bare host state"


def test_the_recovery_agent_can_reach_all_three_definition_directories(h):
    h.session.deselect("body")
    spec = h.runtime._containers[naming.agent(h.tab(None))]["spec"]
    root = Path(next(m.source for m in spec.mounts if m.target == "/definitions"))
    for kind in ("faces", "toolbelts", "bodies"):
        assert (root / kind).is_dir(), \
            f"a recovery agent cannot reach {kind}/, so it cannot repair that layer"


def test_a_broken_definition_is_visible_to_the_recovery_agent(h):
    """The state the user actually lands in: a definition is malformed, so the layer will
    not load, and the agent has to see *which* one."""
    import asyncio
    from raigolmid.mcp_server import build_server
    from raigolmid.scopes import build_tab_methods

    (h.search.faces[0] / "broken" / "editor").mkdir(parents=True)
    (h.search.faces[0] / "broken" / "face.toml").write_text('id = "broken"\nnonsense = 1\n')
    h.session.rediscover()
    h.session.deselect("body")

    tools = build_server(h.served(build_tab_methods(h.session, Questions(h.events, h.paths), Channels(h.session, h.events),
                                                          h.tab(None))))
    result = asyncio.run(tools.call_tool("status", {}))

    payload = str(result)
    assert "broken" in payload and "nonsense" in payload, \
        "the recovery agent cannot see which definition is broken"
    assert "no sandbox open" in payload, \
        "status must say why there is no instance rather than failing"


def test_the_credential_never_enters_a_tab_and_the_agent_can_be_attached_to(h):
    """A tab is given a placeholder and the proxy's address, never the credential, and
    runs with no capabilities. A tab reaches the agent with `docker attach`, which gives an
    interactive `claude` nothing without a terminal."""
    h.session.agents.start(_spec(h), fresh_home=True)
    spec = h.runtime._containers[naming.agent(SPARE)]["spec"]
    placeholder = spec.environment["CLAUDE_CODE_OAUTH_TOKEN"]
    assert "test-token" not in json.dumps(spec.environment)
    assert h.session.agents.broker.placeholders.owner_of(placeholder) == SPARE
    assert spec.environment["HTTPS_PROXY"] == \
        f"http://{h.runtime.bridge_gateway()}:{credproxy.PORT}"
    assert any(m.target == spec.environment["NODE_EXTRA_CA_CERTS"] and m.read_only
               for m in spec.mounts), "told to trust an authority it was not given"
    assert spec.tty and spec.stdin_open
    assert not spec.cap_add and spec.cap_drop == ("ALL",)


def test_a_tab_cannot_send_its_session_to_the_background(h):
    """A background session's record outlives the container and blocks `--continue`."""
    h.session.agents.start(_spec(h), fresh_home=True)
    spec = h.runtime._containers[naming.agent(SPARE)]["spec"]
    assert spec.environment["CLAUDE_CODE_DISABLE_AGENT_VIEW"] == "1"
    assert "no-new-privileges:true" in spec.security_opt


def test_a_tab_whose_agent_cannot_start_is_not_left_behind(h):
    """Otherwise it stays `starting` for good, each retry adds another, and status claims
    an agent that will never exist. The agent image failing to build is one way it cannot."""
    from raigolmid import hostimages
    tag = hostimages.agent(None).tag()
    del h.runtime._images[tag]
    h.runtime.build_failures.add(tag)
    closed = h.tab("myapi")
    with pytest.raises(Exception):
        h.session.close_tab(closed)
    assert h.session.intent.body_tab("myapi") is None
    assert [a["tab"] for a in h.session.status()["agents"]] == [h.tab(None)]


def test_a_credential_others_can_read_is_refused(h):
    h.paths.agent_credentials.chmod(0o644)
    with pytest.raises(AgentError, match="0600"):
        h.session.agents.start(_spec(h), fresh_home=True)


def test_a_credentials_file_may_set_only_the_two_names_claude_reads(h):
    h.paths.agent_credentials.write_text("PATH=/tmp/evil\n")
    with pytest.raises(AgentError, match="CLAUDE_CODE_OAUTH_TOKEN"):
        h.session.agents.start(_spec(h), fresh_home=True)


def test_a_credentials_file_may_not_set_both_names(h):
    """With both set, Claude Code uses the API key: paid tokens behind a subscription."""
    h.paths.agent_credentials.write_text(
        "CLAUDE_CODE_OAUTH_TOKEN=sub\nANTHROPIC_API_KEY=paid\n")
    with pytest.raises(AgentError, match="exactly one"):
        h.session.agents.start(_spec(h), fresh_home=True)


def test_rai_inside_an_agent_container_finds_its_tabs_socket(h, monkeypatch):
    """The container mounts its tab's socket directory at /run/raigolmid and says so in
    RAIGOLMID_SOCKET; `rai mcp` resolving its own runtime dir instead never connects."""
    from raigolmid.paths import Paths
    from raigolmid.scopes import SOCKET
    h.session.agents.start(_spec(h), fresh_home=True)
    spec = h.runtime._containers[naming.agent(SPARE)]["spec"]
    monkeypatch.setenv("RAIGOLMID_SOCKET", spec.environment["RAIGOLMID_SOCKET"])
    assert str(Paths.from_env().api_socket) == f"/run/raigolmid/{SOCKET}"
    (mount,) = [m for m in spec.mounts if m.target == "/run/raigolmid"]
    assert mount.source == str(h.paths.agent_socket_dir(SPARE))


def test_an_agent_is_given_nothing_else_of_the_runtime_dir(h):
    """The runtime dir holds the host compositor's IPC socket, D-Bus and the user's
    systemd, each of which runs host commands, and the agent runs as their owner."""
    h.session.agents.start(_spec(h), fresh_home=True)
    spec = h.runtime._containers[naming.agent(SPARE)]["spec"]
    runtime = h.paths.runtime.resolve()
    reached = [m.source for m in spec.mounts
               if Path(m.source).resolve() == runtime
               or runtime in Path(m.source).resolve().parents]
    assert reached == [str(h.paths.agent_socket_dir(SPARE))]


def test_every_tab_takes_up_the_machines_plugins_and_only_the_machine_tab_changes_them(h):
    """Skills and MCP servers are the machine's, one folder of plugins: loaded
    at every launch, and changed, with the instruction templates, by the machine tab."""
    def agent(tab_id):
        spec = h.runtime._containers[naming.agent(tab_id)]["spec"]
        return spec, {m.target: m for m in spec.mounts}

    for tab_id in (h.tab("myapi"), h.session.intent.machine_tab().tab_id):
        spec, mounts = agent(tab_id)
        at = spec.command.index("--plugin-dir")
        assert spec.command[at + 1] == "/agent/plugins"
        assert Path(mounts["/agent/plugins"].source) == h.paths.agent_plugins
    _, body = agent(h.tab("myapi"))
    assert body["/agent/plugins"].read_only and "/agent/templates" not in body
    _, machine = agent(h.session.intent.machine_tab().tab_id)
    assert not machine["/agent/plugins"].read_only
    assert Path(machine["/agent/templates"].source) == h.paths.agent_templates
    assert not machine["/agent/templates"].read_only


def test_only_the_machine_tab_can_edit_a_face(h):
    """A body tab uses the face and never edits one: the rule is the mount's."""
    def faces_of(tab_id):
        spec = h.runtime._containers[naming.agent(tab_id)]["spec"]
        root = Path(next(m.source for m in spec.mounts if m.target == "/definitions"))
        return {m.target: m for m in spec.mounts
                if m.target == f"/definitions/{h.search.faces[0].relative_to(root)}"}

    body = faces_of(h.tab("myapi"))
    assert [m.read_only for m in body.values()] == [True]
    assert Path(next(iter(body.values())).source) == h.search.faces[0]
    assert faces_of(h.session.intent.machine_tab().tab_id) == {}


def test_the_context_never_names_a_host_path(h):
    tab = h.tab("myapi")
    home = h.paths.data / "agent-home" / tab
    # The trailing provenance comment names the user's template on the host, for the user.
    text = "\n".join(line for line in (home / ".claude" / "CLAUDE.md").read_text().splitlines()
                     if not line.startswith("<!-- Generated by raigolmid."))
    assert str(h.root) not in text, "a host path in the context is a dead end in the container"
    assert "/definitions" in text


def test_the_mcp_tools_answer_in_the_paths_the_agent_sees(h, monkeypatch):
    """The daemon answers in host paths. An agent handed its working copy's host path
    read it as some other copy of the project; it reaches that directory only as /work."""
    import asyncio
    from raigolmid.mcp_server import build_server
    from raigolmid.scopes import build_tab_methods

    tab = h.tab("myapi")
    spec = h.runtime._containers[naming.agent(tab)]["spec"]
    monkeypatch.setenv("RAIGOLMI_MOUNTS", spec.environment["RAIGOLMI_MOUNTS"])
    host = str(next(m.source for m in spec.mounts if m.target == "/work"))
    h.session.sandbox_open(tab, "python-dev")

    tools = build_server(h.served(build_tab_methods(h.session, Questions(h.events, h.paths), Channels(h.session, h.events),
                                                          tab)))
    # Every tool that answers with the tab's status: `select` and `deselect` answer with it too.
    for name, args in (("status", {}), ("select", {"kind": "body", "id": "myapi"}),
                       ("deselect", {"kind": "face"})):
        payload = str(asyncio.run(tools.call_tool(name, args)))
        assert host not in payload, name
        assert "'working_copy': '/work'" in payload or '"working_copy": "/work"' in payload, name


def test_the_index_routes_to_the_documents_that_exist_and_every_layer(h, monkeypatch):
    """Generated at the call, so a document written a moment ago is in it,
    and in the paths the agent reaches."""
    import asyncio
    from raigolmid.mcp_server import build_server
    from raigolmid.scopes import build_tab_methods

    tab = h.tab("myapi")
    spec = h.runtime._containers[naming.agent(tab)]["spec"]
    monkeypatch.setenv("RAIGOLMI_MOUNTS", spec.environment["RAIGOLMI_MOUNTS"])
    work = next(Path(m.source) for m in spec.mounts if m.target == "/work")
    tools = build_server(h.served(build_tab_methods(
        h.session, Questions(h.events, h.paths), Channels(h.session, h.events),
        tab)))

    def index():
        return json.loads(asyncio.run(tools.call_tool("index", {})).content[0].text)

    first = index()
    assert first["documents"] == []
    assert {"/work/SESSION-START.md", "/home/agent/thoughts.md"} <= {
        d["path"] for d in first["missing"]}
    (work / "SESSION-START.md").write_text("where it stands\n", encoding="utf-8")
    assert [d["path"] for d in index()["documents"]] == ["/work/SESSION-START.md"]
    assert "/work/SESSION-START.md" not in {d["path"] for d in index()["missing"]}
    (work / "docs").mkdir(exist_ok=True)
    (work / "docs" / "DESIGN.md").write_text(
        "<!-- purpose: the design of the api\nnot-here: n\nshape: bounded\naudited: 1 "
        "2026-10-02\n-->\n", encoding="utf-8")
    (work / "NOTES.md").write_text("notes\n", encoding="utf-8")
    routed = {d["path"]: d["what"] for d in index()["documents"]}
    assert routed["/work/docs/DESIGN.md"] == "the design of the api"
    assert routed["/work/NOTES.md"].startswith("no purpose header yet")
    (work / "docs" / "DESIGN.md").unlink()
    (work / "NOTES.md").unlink()

    toolbelt = h.session.catalogue.toolbelts["python-dev"].directory
    doc = toolbelt / "LAYER.md"
    doc.write_text("what python-dev is\n", encoding="utf-8")
    os.utime(doc, (1, 1))
    listed = {d["path"]: d for d in index()["documents"]}
    assert listed["/definitions/" + str(doc.relative_to(h.session.definitions_root()))][
        "changed_after"] == ["toolbelt.toml"], listed

    layers = index()["layers"]
    assert {row["id"] for row in layers["toolbelts"]} >= {"python-dev"}
    for kind in ("faces", "toolbelts", "bodies"):
        for row in layers[kind]:
            # A body defined in its own working copy is reached, and edited, as /work.
            assert row["directory"] is None or row["directory"].startswith(
                ("/definitions/", "/work")), row
    listed = json.loads(asyncio.run(tools.call_tool("list_items", {"kind": "toolbelt"})).content[0].text)
    assert list(listed) == ["toolbelts"]


class _Refusing:
    """The daemon as the agent's tools meet it: one refusal, in the daemon's words."""

    def __init__(self, reason):
        self.reason = reason

    def call(self, method, **params):
        from raigolmid.client import ApiError
        raise ApiError(self.reason)


@pytest.mark.parametrize("reason", [
    "port 8080 is not one x@tab-2's body listens on (it declares none)",
    __import__("raigolmid.scopes", fromlist=["NO_SANDBOX"]).NO_SANDBOX,
])
def test_a_refusal_reaches_the_agent_in_its_own_words(reason):
    """The refusal says what to do instead; the tool's name alone says nothing."""
    import asyncio
    from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
    from raigolmid.mcp_server import build_server

    server = build_server(_Refusing(reason))
    with pytest.raises(ToolError) as raised:
        asyncio.run(server.call_tool("show_url", {"url": "http://localhost:8080"}))
    assert not isinstance(raised.value, UnexpectedToolError)
    assert reason in str(raised.value)


def test_a_repository_among_the_definitions_is_protected_in_every_agent(h):
    """A body whose working copy is its definition directory keeps its `.git` under the
    definitions, which every agent mounts writable; its hooks and config are read-only
    there too, or any tab could plant what the owner's git runs."""
    h.session.open_janitor()
    spec = h.runtime._containers[naming.agent("janitor")]["spec"]
    targets = {m.target: m for m in spec.mounts}

    for place in ("/definitions", "/work"):
        for name in ("hooks", "config", "commondir"):
            mount = targets.get(f"{place}/bodies/myapi/.git/{name}")
            assert mount is not None and mount.read_only, f"{place}/bodies/myapi/.git/{name}"


def test_a_repository_made_while_agents_run_is_protected_once_a_turn_ends(h):
    """Binds are made with a container, so a `git init` among the definitions leaves every
    running agent able to write its hooks until that agent is made again; a turn's end
    restarts each idle agent made before it."""
    h.session.open_janitor()
    subprocess.run(["git", "init", "-q", str(h.search.toolbelts[0] / "no-lsp")], check=True)
    h.session.agent_activity("janitor", busy=False)

    target = "/definitions/toolbelts/no-lsp/.git/hooks"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        mounts = {m.target: m for m in h.runtime._containers[naming.agent("janitor")]["spec"].mounts}
        if target in mounts:
            break
        time.sleep(0.05)
    assert target in mounts and mounts[target].read_only


def test_a_declared_document_is_one_that_exists_in_a_state_it_can_be_in(tmp_path):
    """A declaration answers the stop hook's question, so one naming no doc, or a state that is
    no answer, is refused in words rather than taken as one."""
    import asyncio
    from mcp.server.mcpserver.exceptions import ToolError
    from raigolmid.mcp_server import build_server

    doc = tmp_path / "DESIGN.md"
    doc.write_text("<!-- purpose: p -->\n", encoding="utf-8")
    server = build_server(_Refusing("unused"))

    def declare(*documents):
        return asyncio.run(server.call_tool("declare_documents",
                                            {"documents": list(documents)}))

    assert "declared 1" in str(declare({"path": str(doc), "state": "current"}))
    for wrong, said in (({"path": "DESIGN.md", "state": "current"}, "absolute path"),
                        ({"path": str(tmp_path / "gone.md"), "state": "current"}, "no such"),
                        ({"path": str(doc), "state": "fine"}, "`current` or `updated`")):
        with pytest.raises(ToolError) as raised:
            declare(wrong)
        assert said in str(raised.value)
