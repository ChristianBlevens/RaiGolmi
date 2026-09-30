"""The scoped MCP server.

An agent container's MCP server is `rai mcp`, shipped as the approved plugin
`raigolmi@raigolmi`: a stdio server proxying to raigolmid through the only socket the
container has, its tab's own. The scope is enforced
there, by the daemon (`scopes.py`): this side names no tab and no sandbox. **Never raw Docker
socket access.**

What the scope buys, beyond isolation: `history` gives an agent asked to fix a failing
rebuild the last failure to read instead of guessing, and `search_packages` lets
it find a package name in the nixpkgs index before writing it into a toolbelt.
Both are things it cannot reconstruct from the repository.

`--scope machine` is the manager tab's: the machine's state read-only and only the
repairs the daemon already has. Either scope is also its tab's **channel**: it pushes what
`raigolmid` hands it — a failure for the manager, a question's outcome for any tab — into the
session (`channel.py`).
"""
from __future__ import annotations

import functools
import json
import os
import sys
import traceback
from typing import Any

from .client import ApiClient, ApiError
from .paths import Paths

INSTRUCTIONS = """\
You are working inside a RaiGolmi agent container, scoped to one sandbox. `index` is
where to start: the documents for this tab and every layer on the machine.

Durable changes go in the definition files, never into a running container: edit the
body's Dockerfile or dependency files and call `rebuild_body`, or edit the toolbelt's
package list. Nothing is persisted with `docker commit`.

To run anything, open your sandbox first with `sandbox_open` and the toolbelt the work
needs. You may experiment in it with `exec` — those writes land in the body's writable
layer and are discarded on the next rebuild, which is the correct semantics for an
experiment.

Before adding a package to a toolbelt, find its name with `search_packages`. nixpkgs
holds about 100,000 packages, and Nixery refuses a guessed name by name.

If a rebuild fails, read `history` for this sandbox: it has the last build's outcome and
log, which is what actually happened on this machine.

A choice that is the user's, you put to them with `ask_user` and end your turn. Their answer
is your next message, typed by them in this tab; an answer from their preferences, or their
overturning of one, arrives as a <channel source="plugin:raigolmi:raigolmi" question="...">
message. "no answer" means they dismissed it or let it lapse; decide without their choice and
say what you decided.
"""


MACHINE = "machine"

MACHINE_INSTRUCTIONS = """\
Failures arrive as <channel source="plugin:raigolmi:raigolmi" seq="..." failure="...">
messages. They come from raigolmid itself and are your work, as your CLAUDE.md says.
The answer to an `ask_user` is your next message, typed in this tab or arriving as a
<channel ... question="..."> message. "no answer" means the user dismissed it or let it
lapse; decide without their choice.
"""


def tool(server, **options: Any):
    """`server.tool`, with the daemon's refusals reaching the agent. mcp 2.2.0 treats any
    exception but its own `ToolError` as a crash and hands the model only "Error executing
    tool <name>" (`mcpserver/exceptions.py`), so a refusal that says what to do instead would
    arrive as the tool's name."""
    from mcp.server.mcpserver.exceptions import ToolError

    def register(fn):
        @functools.wraps(fn)
        def said(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except ApiError as exc:
                raise ToolError(str(exc)) from exc
        return server.tool(**options)(said)
    return register


ASK_USER = ("Put a question to the user, then end your turn: your tab in their terminal is "
            "marked until they answer, and this returns at once. Their answer is your next "
            "message, typed by them in this tab. `choices` are options to offer; they can "
            "always answer in words instead. Their preferences may answer first, which arrives "
            "as a channel message naming the question. \"no answer\" means they dismissed it "
            "or left it 30 minutes: go with your own choice.")


def ask_user(client: ApiClient, message: str, choices: list[str] | None) -> dict:
    """The question is registered and the asker goes on; it does not wait."""
    question = client.call("ask", message=message, choices=choices or [])
    return {"status": "asked", "question": question,
            "next": "End your turn now. The answer is your next message."}


def as_seen_inside(value: Any, mounts: list[tuple[str, str]]) -> Any:
    """The daemon answers in host paths; the agent reaches its project only through its
    mounts, so a host path it is handed reads as some other copy. Each mounted host
    directory is rewritten to where the container mounts it, in order — `/work` before
    `/definitions`, which the bare host state mounts from one directory."""
    if isinstance(value, str):
        for host, inside in mounts:
            if value == host:
                return inside
            value = value.replace(host + "/", inside + "/")
        return value
    if isinstance(value, list):
        return [as_seen_inside(v, mounts) for v in value]
    if isinstance(value, dict):
        return {k: as_seen_inside(v, mounts) for k, v in value.items()}
    return value


def container_mounts() -> list[tuple[str, str]]:
    """Set by `Agents.start` from the mounts it gives the container. Outside one, there is
    nothing to translate, and the answers are left as the host's."""
    raw = os.environ.get("RAIGOLMI_MOUNTS")
    return [] if raw is None else [(host, inside) for host, inside in json.loads(raw)]


def build_server(client: ApiClient):
    from mcp.server.mcpserver import MCPServer

    mounts = container_mounts()
    server = MCPServer(
        name="raigolmi",
        version="0.1.0",
        instructions=INSTRUCTIONS,
    )

    def call(method: str, **params: Any) -> Any:
        return as_seen_inside(client.call(method, **params), mounts)

    @tool(server, description="What is running on this machine, scoped to your sandbox: "
                             "its body, toolbelt, health, and the state of any build. "
                             "Works in the bare host state too, where it tells you what "
                             "is selected and what is broken.")
    def status() -> dict[str, Any]:
        return call("status")

    @tool(server, description="Where to start: this tab's documents, each with what it is "
                             "for — those that exist, a layer doc with the files changed "
                             "since it, and those expected but unwritten — and every face, "
                             "toolbelt and body with its definition directory and whether "
                             "it can be selected. Generated at each call, so never stale.")
    def index() -> dict[str, Any]:
        return call("index")

    @tool(server, description="The faces, toolbelts and bodies as the user's selector shows "
                             "them: whether each is selected and selectable and why not, a "
                             "toolbelt's capabilities, and the sandboxes running on each body. "
                             "`kind` is face, toolbelt or body; omitted, all three.")
    def list_items(kind: str | None = None) -> dict[str, Any]:
        return call("list_items", kind=kind)

    @tool(server, description="Run a command in your sandbox's toolbelt container, where the "
                             "toolbelt and the language servers run. Returns stdout, "
                             "stderr and the exit code.")
    def exec(cmd: list[str], cwd: str = "/work", timeout: float = 300.0) -> dict[str, Any]:
        return client.call("exec", cmd=cmd, cwd=cwd, timeout=timeout)

    @tool(server, description="Rebuild your sandbox's body from its definition and swap "
                             "it in. Returns exactly one of rebuilt, already_current or "
                             "build_failed (with the log). It never reports success "
                             "without a completed swap.")
    def rebuild_body() -> dict[str, Any]:
        return call("rebuild_body")

    @tool(server, description="Recent lifecycle events for your sandbox: builds and their "
                             "outcomes, view restarts and their timings, crashes, and "
                             "degraded reasons. Read this before guessing why something "
                             "failed.")
    def history(n: int = 50) -> list[dict[str, Any]]:
        return call("history", n=n)

    @tool(server, description="Container logs for your sandbox's body and toolbelt container.")
    def logs(tail: int = 100) -> dict[str, Any]:
        return call("logs", tail=tail)

    @tool(server, description="Find package names in the nixpkgs-unstable index. Use this "
                             "before putting a name in a toolbelt's package list. A hint: "
                             "Nixery builds from its own pinned snapshot, and a name it "
                             "cannot build fails the pull with its answer naming it.")
    def search_packages(query: str, limit: int = 20) -> list[str]:
        return client.call("search_packages", query=query, limit=limit)

    @tool(server, description="Start your sandbox's body again from the image it runs, "
                             "without a build: its container is replaced and the toolbelt's "
                             "view comes back over it. `rebuild_body` is the one that builds.")
    def restart_body() -> dict[str, Any]:
        return client.call("restart_body")

    @tool(server, description="Open a file under /work at a line in the editor on the user's "
                             "screen, to show them something. Only while your sandbox is the "
                             "one on their screen.")
    def show_file(path: str, line: int = 1) -> dict[str, Any]:
        return client.call("show_file", path=path, line=line)

    @tool(server, description="Open a URL in the browser of the face on the user's screen. A "
                             "localhost URL is your sandbox's page, on any port its body or "
                             "toolbelt listens on, whether or not it is on their screen; any "
                             "other URL, from any tab.")
    def show_url(url: str) -> dict[str, Any]:
        return client.call("show_url", url=url)

    @tool(server, description="Capture the face, the desktop on the user's screen, as a PNG. "
                             "Returns its path; read that file to see it, for instance to "
                             "check a UI you built. `trial` captures the face you are trying "
                             "off their screen instead (`try_face`).")
    def screenshot(trial: bool = False) -> dict[str, Any]:
        return call("screenshot", trial=trial)

    @tool(server, description="Machine tab only: run a face off the user's screen, as its "
                             "definition is on disk now, to try a change to it before they "
                             "see it. Its own compositor on a headless output the size of "
                             "their screen, with its apps and editor window; see and drive it "
                             "with `screenshot` and `face_input` given `trial`. One trial at "
                             "a time: this replaces any before it. The user never sees it.")
    def try_face(face: str) -> dict[str, Any]:
        return call("try_face", face=face)

    @tool(server, description="Machine tab only: replace a face's app settings (`face`) "
                             "with a copy of another face's (`source`). Each face's apps keep "
                             "their own config and state; this is how a new face starts from "
                             "an existing one's. Refused for the face on the user's screen.")
    def seed_face_settings(face: str, source: str) -> dict[str, Any]:
        return call("seed_face_settings", face=face, source=source)

    @tool(server, description="Machine tab only: stop the face you are trying off the "
                             "user's screen.")
    def stop_trial() -> dict[str, Any]:
        return call("stop_trial")

    @tool(server, description="Machine tab only: mark a body tab (`tab`) as one you manage "
                             "(`on` true) or give it back (`on` false). Only when the user "
                             "hands you the tab: a managed tab's questions come to you rather "
                             "than to them, and you are told each time its turn ends. "
                             "`stop_when` is where they said it stops for them, in their "
                             "words — a goal, or a decision that is theirs — and you `hold` it "
                             "there.")
    def manage(tab: str, on: bool = True, stop_when: str | None = None) -> dict[str, Any]:
        return call("manage", tab=tab, on=on, stop_when=stop_when)

    @tool(server, description="Machine tab only: hold a tab you manage for the user, at the "
                             "stop they gave. It is resumed on Remote Control, which reaches "
                             "their phone, and told to put `situation` to them — where the "
                             "work stands, what is theirs to decide, the options. Nothing of "
                             "yours reaches it until they answer in it; you are told then.")
    def hold(tab: str, situation: str) -> dict[str, Any]:
        return call("hold", tab=tab, situation=situation)

    @tool(server, description="Machine tab only: every tab you manage — its state, the "
                             "questions it waits on you for, and its context use against the "
                             "budget.")
    def managed() -> list[dict[str, Any]]:
        return call("managed")

    @tool(server, description="Machine tab only: one tab you manage closer — its state, "
                             "the tail of its thought doc (`lines`) and its last `turns` "
                             "turns.")
    def managed_tab(tab: str, turns: int = 20, lines: int = 60) -> dict[str, Any]:
        return call("managed_tab", tab=tab, turns=turns, lines=lines)

    @tool(server, description="Machine tab only: tell a tab you manage what to do. It is "
                             "that tab's next message once its turn ends; nothing comes back "
                             "but the note that its turn ended.")
    def direct(tab: str, content: str) -> dict[str, Any]:
        return call("direct", tab=tab, content=content)

    @tool(server, description="Machine tab only: answer a question a tab you manage asked "
                             "the user, which their preferences did not answer. They see your "
                             "answer in their history and may overturn it.")
    def answer_question(id: str, answer: str) -> dict[str, Any]:
        return call("answer_question", id=id, answer=answer)

    @tool(server, description="Machine tab only: hand a tab you manage over to a fresh "
                             "conversation, in two calls. The first has it make its documents "
                             "ready; once told that turn ended, read them (`managed_tab`), and "
                             "the second restarts it on SESSION-START.md and its thought doc "
                             "(and `brief`, if given), its old conversation archived. Only "
                             "while it is idle with nothing asked.")
    def restart_fresh(tab: str, brief: str | None = None) -> dict[str, Any]:
        return call("restart_fresh", tab=tab, brief=brief)

    @tool(server, description="Machine tab only: say your thought doc is ready for your next "
                             "conversation, once the daemon has asked at your context budget. "
                             "You are restarted fresh when this turn ends.")
    def ready_to_restart() -> dict[str, Any]:
        return call("ready_to_restart")

    @tool(server, description="Your hands on the face on the user's screen: `type` text, "
                             "press a `key` (`ctrl+s`, `Return`), `move` the pointer or "
                             "`click` (`button` left, middle or right) at `x`, `y` — the "
                             "screenshot's pixels. One tab drives at a time: your first input "
                             "holds the face until your turn ends, and another tab's waits. "
                             "Waits until they have paused for 2.5 s; refused while they are "
                             "using the machine, while another tab keeps driving, and when "
                             "they have switched it off. `trial` drives the face you are "
                             "trying off their screen instead (`try_face`), with none of "
                             "those waits.")
    def face_input(action: str, text: str | None = None, x: int | None = None,
                   y: int | None = None, button: str = "left",
                   trial: bool = False) -> dict[str, Any]:
        return call("face_input", action=action, text=text, x=x, y=y, button=button,
                    trial=trial)

    @tool(server, description="Ask another agent tab for what needs its judgement — the "
                             "machine tab (`to` \"machine\") for a change to a face, "
                             "toolbelt or body definition, or the tab working on a body (`to` "
                             "the body's id). Returns at once; end your turn, and its answer "
                             "is your next message.")
    def message(to: str, content: str) -> dict[str, Any]:
        return client.call("message", to=to, content=content)

    @tool(server, description="Answer a message another tab sent you, by its id (`m…`). "
                             "Ending your turn without replying tells the sender you did not "
                             "answer.")
    def reply(message: str, content: str) -> dict[str, Any]:
        return client.call("reply", message=message, content=content)

    @tool(server, description="Select a face or body (`kind`) by id, as the user does in the "
                             "selector: the layers on their screen change, and nothing is "
                             "built. A body with no tab gets one, where its own agent works on "
                             "it; you stay the tab you are, on the /work you have, and their "
                             "terminal stays where they left it.")
    def select(kind: str, id: str) -> dict[str, Any]:
        return call("select", kind=kind, id=id)

    @tool(server, description="Open your sandbox — your body on your /work, with the toolbelt "
                             "you name — when you need to run something: `exec`, a build, the "
                             "body's page. One per tab, kept until the tab closes. `status` "
                             "lists the toolbelts it may run. When your body is the selected "
                             "one, yours is the sandbox the user's editor and terminals are "
                             "on.")
    def sandbox_open(toolbelt: str) -> dict[str, Any]:
        return call("sandbox_open", toolbelt=toolbelt)

    @tool(server, description="Swap your open sandbox's toolbelt in place: the body keeps "
                             "running, and what ran in the old toolbelt ends. When it is the "
                             "sandbox the user's face is on, this asks them first, in this "
                             "tab's window, and returns at once: end your turn, and whether "
                             "they allowed it is your next message.")
    def toolbelt_swap(toolbelt: str) -> dict[str, Any]:
        return call("toolbelt_swap", toolbelt=toolbelt)

    @tool(server, description="Deselect the face or body (`kind`). The same rules as "
                             "`select`.")
    def deselect(kind: str) -> dict[str, Any]:
        return call("deselect", kind=kind)

    @tool(server, name="ask_user", description=ASK_USER)
    def ask_user_tool(message: str, choices: list[str] | None = None) -> dict[str, Any]:
        return ask_user(client, message, choices)

    return server


def build_machine_server(client: ApiClient):
    from mcp.server.mcpserver import MCPServer

    mounts = container_mounts()
    server = MCPServer(name="raigolmi", version="0.1.0", instructions=MACHINE_INSTRUCTIONS)

    def call(method: str, **params: Any) -> Any:
        return as_seen_inside(client.call(method, **params), mounts)

    @tool(server, description="The whole machine: selection, every sandbox and its health, "
                             "builds, queues, every agent tab, the face and host surfaces.")
    def status() -> dict[str, Any]:
        return call("status")

    @tool(server, description="Where to start: your incidents (open, and fixed), this "
                             "machine's failure patterns, each layer's doc with the files "
                             "changed since it, the docs expected but unwritten, and every "
                             "face, toolbelt and body. Generated at each call.")
    def index() -> dict[str, Any]:
        return call("index")

    @tool(server, description="The machine's recent events, newest last: every failure, crash, "
                             "reconcile and start, with its reason.")
    def events(n: int = 100) -> list[dict[str, Any]]:
        return call("events", n=n)

    @tool(server, description="Output of any container raigolmid manages (agent, body, view, "
                             "face, selector, control), with whether it runs and its exit "
                             "code. Names are in `status` and the events.")
    def container_logs(container: str, tail: int = 200) -> dict[str, Any]:
        return call("container_logs", container=container, tail=tail)

    @tool(server, description="raigolmid's own journal: what the daemon logged, including the "
                             "tracebacks events point at.")
    def journal(n: int = 200) -> dict[str, Any]:
        return call("journal", n=n)

    @tool(server, description="The evidence each container that exited on its own left: "
                             "with no name, the list; with a name, that log (exit code and "
                             "the dead container's output).")
    def crash_logs(name: str | None = None) -> dict[str, Any]:
        return call("crash_logs", name=name)

    @tool(server, name="ask_user", description=ASK_USER)
    def ask_user_tool(message: str, choices: list[str] | None = None) -> dict[str, Any]:
        return ask_user(client, message, choices)

    @tool(server, description="What raigolmid holds for your channel — failures, and answers "
                             "to your questions — and the one last pushed.")
    def failures() -> dict[str, Any]:
        return call("channel_state")

    @tool(server, description="Restart an agent tab's agent, resuming its conversation.")
    def restart_agent(tab_id: str, resume: bool = True) -> dict[str, Any]:
        return call("restart_agent", tab_id=tab_id, resume=resume)

    @tool(server, description="Recreate a sandbox from its recorded intent: its anchor, body "
                             "and view.")
    def repair(instance: str) -> dict[str, Any]:
        return call("repair", instance=instance)

    @tool(server, description="Start a sandbox's body again from the image it runs, without "
                             "a build: its container is replaced and its view comes back "
                             "over it.")
    def restart_body(instance: str) -> dict[str, Any]:
        return call("restart_body", instance=instance)

    @tool(server, description="Rebuild a sandbox's body from its definition and swap it in; "
                             "`already_current` when nothing changed, and a failed build's "
                             "log.")
    def rebuild_body(instance: str) -> dict[str, Any]:
        return call("rebuild_body", instance=instance)

    @tool(server, description="Bring what runs back in line with what is intended, machine-wide: "
                             "the sandboxes, the user's face, the door, and a selector, control "
                             "or popup that exited.")
    def reconcile() -> dict[str, Any]:
        return call("reconcile")

    @tool(server, description="Re-read every face, toolbelt and body definition; returns the "
                             "definition errors.")
    def rediscover() -> dict[str, Any]:
        return call("rediscover")

    return server


def serve_with_channel(client: ApiClient, server) -> None:
    """The tools, and the channel beside them: once Claude Code has initialized, whatever
    `channel_take` hands out is pushed as `notifications/claude/channel`. The daemon decides
    what and when (only to an idle tab); this only carries it.

    Claude Code takes a channel only on the handshake era: on the 2026-07-28 revision, which
    a feature flag of its own decides to ask for, it skips it ("no unsolicited notification
    path"). So only that era is served (`serve_loop`): its `server/discover` probe gets
    METHOD_NOT_FOUND and it falls back to `initialize`.

    ⚠ Two private seams of mcp 2.2.0, pinned in `pyproject.toml`: the low-level server under
    `MCPServer` (`_lowlevel_server`), whose initialization options are the only place the
    channel capability can be declared, and the connection a notification is sent on
    (`session._connection`), because `send_notification` refuses anything off-spec."""
    import anyio
    import mcp.types as types
    from mcp.server.runner import serve_loop
    from mcp.server.stdio import stdio_server

    lowlevel = server._lowlevel_server
    ready = anyio.Event()
    state: dict[str, Any] = {}

    async def on_initialized(ctx, params) -> None:
        # Raised here, mcp would log it and serve on with the channel never ready.
        try:
            state["connection"] = ctx.session._connection
        except AttributeError:
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
            os._exit(70)
        ready.set()

    lowlevel.add_notification_handler("notifications/initialized", types.NotificationParams,
                                      on_initialized)

    async def push() -> None:
        """Anything but the daemon being away ends the process, loudly. Left to the task
        group, it cancels the server and then waits on stdin's reader, a worker thread
        blocked in `readline()` that cancellation cannot reach (python-sdk #3551): the
        process stays up with its tools and channel dead, and an idle tab never says why.
        `os._exit` is the one exit that thread cannot hold, and Claude Code shows the
        server as failed with this traceback on its stderr."""
        try:
            await _push()
        except Exception:                              # noqa: BLE001 - said, then exited
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
            os._exit(70)

    async def _push() -> None:
        await ready.wait()
        said = None
        while True:
            try:
                item = await anyio.to_thread.run_sync(lambda: client.call("channel_take"))
                said = None
            except ApiError as exc:
                # raigolmid restarting is a state this outlives; each new reason is said once,
                # on the stderr Claude Code keeps for the server.
                if str(exc) != said:
                    print(json.dumps({"channel_take": str(exc)}), file=sys.stderr, flush=True)
                    said = str(exc)
                item = None
            if item is not None:
                await state["connection"].notify(
                    "notifications/claude/channel",
                    {"content": item["content"], "meta": item["meta"]})
            await anyio.sleep(1.0)

    async def main() -> None:
        options = lowlevel.create_initialization_options(
            experimental_capabilities={"claude/channel": {}})
        async with (stdio_server() as (read, write), lowlevel.lifespan(lowlevel) as state_,
                    anyio.create_task_group() as tg):
            tg.start_soon(push)
            await serve_loop(lowlevel, read, write, lifespan_state=state_, init_options=options)
            # The session is over. Cancelling would wait out a `channel_take` in its worker
            # thread, up to the client's timeout, for an item nobody is left to hear.
            sys.stdout.flush()
            os._exit(0)

    anyio.run(main)


def serve(scope: str, socket_path: str | None = None) -> int:
    client = ApiClient(Paths.from_env().api_socket if socket_path is None
                       else __import__("pathlib").Path(socket_path))
    try:
        client.call("version")
    except ApiError as exc:
        # stdio MCP servers are read by an agent, so the failure has to be legible there.
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    serve_with_channel(client, build_machine_server(client) if scope == MACHINE
                       else build_server(client))
    return 0
