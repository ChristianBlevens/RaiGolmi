"""A whole Session wired onto fakes, with a real repo and real definitions on disk."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import itertools
import threading

from raigolmid import hostimages, labels, settings
from raigolmid.api import ApiServer
from raigolmid.client import ApiClient
from raigolmid import faces as faces_module
from raigolmid import views as views_module
from raigolmid.credproxy import Broker
from raigolmid.definitions import SearchPaths
from raigolmid.events import EventLog
from raigolmid.paths import Paths
from tests.fakeruntime import FakeRuntime
from raigolmid.session import Session

from tests.facedisplay import NestedSway, back_faces
from tests.fakes import FakeCompose, FakeLaunchers

BODY_TOML = """\
id = "{body_id}"
name = "{name}"
dockerfile = "Dockerfile"
target = "prod"
runtime = "python:3.12"
shell = "/bin/sh"
ports = [{port}]

[[develop.watch]]
path = "requirements.txt"
action = "rebuild"
"""

DOCKERFILE = """\
FROM python:3.12-slim AS deps
COPY requirements.txt /tmp/requirements.txt
FROM deps AS prod
CMD ["sleep", "infinity"]
"""

TOOLBELT_TOML = """\
id = "{tb_id}"
name = "{name}"
supports = ["python:3.*"]
capabilities = ["lsp", "debug", "shell"]
packages = ["bashInteractive", "coreutils", "python3", "util-linux", "libcap",
            "pyright", "neovim"]
"""

FACE_TOML = """\
id = "{face_id}"
name = "{name}"
requires_toolbelt_capabilities = [{requires}]

[desktop]
compositor = "sway"
config_dir = "desktop/"
apps = []

[editor]
package = "neovim"
config_dir = "editor/"
command = ["foot", "--app-id=raigolmi-editor", "nvim", "--listen", "{{socket}}",
           "--cmd", "set rtp^={{glue}}", "-u", "{{config}}/init.lua"]
# Keys, not `--remote-expr`, which waits on an editor waiting for input; normal mode first,
# and `<Cmd>` runs without echoing on the user's command line.
open = ["nvim", "--server", "{{socket}}", "--remote-send",
        '<C-\\><C-N><Cmd>lua require("raigolmi").show("{{request}}")<CR>']
"""


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "initial"],
                   cwd=path, check=True)
    return path


def converse(home: Path, entrypoint: str = "cli") -> None:
    """A transcript as Claude Code writes one in /work: `~/.claude/projects/-work/<id>.jsonl`,
    its rows tagged with the entrypoint that wrote them — `cli` for a turn typed into a tab,
    `sdk-cli` for `claude -p`. The rows are the shape the CLI writes."""
    projects = home / ".claude" / "projects" / "-work"
    projects.mkdir(parents=True)
    rows = [
        {"type": "queue-operation", "operation": "enqueue", "sessionId": "s1"},
        {"type": "user", "entrypoint": entrypoint, "cwd": "/work", "sessionId": "s1",
         "message": {"role": "user", "content": "Reply with exactly: PONG"}},
        {"type": "assistant", "entrypoint": entrypoint, "cwd": "/work", "sessionId": "s1"},
    ]
    (projects / "s1.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def build_tree(root: Path) -> SearchPaths:
    """Definitions on disk, because discovery and the definition digest both read files —
    faking them would test the fake."""
    # Distinct ports, because a host port is exclusive and two bodies declaring the same
    # one collide on real Docker. Giving every fixture 8000 modelled a state the daemon
    # refuses, which is the kind of unrepresentative data a mechanism passes on.
    for body_id, name, port in (("myapi", "My API", 8000), ("webui", "Web UI", 8001)):
        d = root / "bodies" / body_id
        make_repo(d)
        (d / "body.toml").write_text(
            BODY_TOML.format(body_id=body_id, name=name, port=port))
        (d / "Dockerfile").write_text(DOCKERFILE)
        (d / "requirements.txt").write_text("requests==2.32.3\n")
        # Committed, because a body's working copy is a repository with history.
        subprocess.run(["git", "add", "-A"], cwd=d, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "-m", "the body"], cwd=d, check=True)

    for tb_id, name in (("python-dev", "Python Dev"),):
        d = root / "toolbelts" / tb_id
        d.mkdir(parents=True)
        (d / "toolbelt.toml").write_text(TOOLBELT_TOML.format(tb_id=tb_id, name=name))

    d = root / "toolbelts" / "no-lsp"
    d.mkdir(parents=True)
    (d / "toolbelt.toml").write_text(
        'id = "no-lsp"\nname = "No LSP"\nsupports = ["python:3.*"]\n'
        'capabilities = ["shell"]\npackages = ["bashInteractive", "coreutils", '
        '"python3", "util-linux", "libcap"]\n')

    for face_id, name, requires in (("backend-focus", "Backend Focus", '"lsp"'),
                                    ("writing", "Writing", "")):
        d = root / "faces" / face_id
        (d / "editor").mkdir(parents=True)
        (d / "editor" / "init.lua").write_text("-- test face\n")
        # A real face has a desktop half; fixtures without one leave every session test
        # blind to it.
        (d / "desktop").mkdir(parents=True)
        (d / "desktop" / "sway.conf").write_text("output * bg #202430 solid_color\n")
        (d / "face.toml").write_text(
            FACE_TOML.format(face_id=face_id, name=name, requires=requires))

    # A face with a desktop and no editor.
    d = root / "faces" / "minimal"
    (d / "desktop").mkdir(parents=True)
    (d / "desktop" / "sway.conf").write_text("output * bg #202430 solid_color\n")
    (d / "face.toml").write_text(
        'id = "minimal"\nname = "Minimal"\nrequires_toolbelt_capabilities = []\n\n'
        '[desktop]\ncompositor = "sway"\nconfig_dir = "desktop/"\napps = []\n')

    return SearchPaths(faces=(root / "faces",), toolbelts=(root / "toolbelts",),
                       bodies=(root / "bodies",))


def answering() -> threading.Event:
    """An `ApiServer`'s `ready`, for a server with no start to wait on."""
    ready = threading.Event()
    ready.set()
    return ready


class Harness:
    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.root = tmp_path
        self.search = build_tree(tmp_path / "repo")
        state = tmp_path / "state"
        for var, value in (("XDG_STATE_HOME", state / "state"),
                           ("XDG_DATA_HOME", state / "data"),
                           ("XDG_CONFIG_HOME", state / "config"),
                           ("RAIGOLMID_VIEW_SOCKET_DIR", state / "views"),
                           ("HOME", state / "home")):
            monkeypatch.setenv(var, str(value))
        self.paths = Paths.from_env()
        self.paths.ensure()
        # What the daemon's start writes before anything reads a setting.
        settings.install(self.paths.settings)
        # The user manager creates it on a real host; the runtime refuses to mount one that is not there.
        self.paths.runtime.mkdir(parents=True, exist_ok=True)
        self.paths.face_runtime.mkdir(exist_ok=True)
        # The agent image is built from shipped sources and started with a host-managed
        # credential; a harness without either could not open an agent tab at all.
        sources = tmp_path / "sources"
        (sources / "agents" / "claude").mkdir(parents=True)
        (sources / "agents" / "claude" / "Dockerfile").write_text("FROM scratch\n")
        (sources / "agents" / "guide").mkdir()
        # The door that publishes the active sandbox's ports is a host image too.
        (sources / "host" / "door").mkdir(parents=True)
        (sources / "host" / "door" / "Containerfile").write_text("FROM scratch\n")
        monkeypatch.setenv(hostimages.SOURCE_ENV, str(sources))
        self.paths.agent_credentials.parent.mkdir(parents=True, exist_ok=True)
        self.paths.agent_credentials.write_text("CLAUDE_CODE_OAUTH_TOKEN=test-token\n")
        self.paths.agent_credentials.chmod(0o600)

        self.runtime = FakeRuntime()
        self.runtime.add_image("registry.k8s.io/pause:3.9")
        back_faces(self.runtime)
        # Each face's own compositor, where every app it is asked to open maps a window.
        monkeypatch.setattr(faces_module.Faces, "_nested_compositor",
                            lambda _self, _state: NestedSway(self.runtime))
        self.compose = FakeCompose(self.runtime)
        self.launchers = FakeLaunchers(self.runtime)
        self.events = EventLog(self.paths.events, epoch=1)

        monkeypatch.setattr(views_module.Views, "client",
                            lambda _self, instance: self.launchers.client_for(instance))
        # `wait_until_usable` is NOT patched out. A real view builds a mount tree and
        # pivots, and the fakes cannot do that — but the waiting itself is production
        # logic, not Docker: it polls, notices a view container that vanished or exited,
        # pulls its log and raises ViewError (views.py:225-246), and instances.py:264-269
        # tears down and marks degraded on that. Replaced by a lambda, none of it ran in
        # any test, and a wait_until_usable that returned unconditionally passed the suite.
        # Everything it touches — Views.get, runtime.logs, client.alive — the fakes have.

        self.session = Session(self.runtime, self.paths, self.search, self.events,
                               epoch=1, compose_cli=self.compose)

    _served = itertools.count(1)

    def served(self, methods) -> ApiClient:
        """A method table as a caller meets it: the real `ApiServer` and `ApiClient` over a
        socket, so a reply goes through JSON and a refusal arrives as `ApiError`."""
        path = self.paths.api_socket.with_name(f"served-{next(self._served)}.sock")
        server = ApiServer(path, methods, self.events, ready=answering())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return ApiClient(path, timeout=20)

    def open_sandbox(self, body: str | None = "myapi", toolbelt: str = "python-dev") -> str:
        """The face tab's sandbox open, as its agent opens it: `body` selected, which
        opens its tab, or with None no body selected and the machine tab on `work`. The
        running body, view and ports a test of a sandbox needs. Returns the sandbox id."""
        if body is not None:
            self.session.select("body", body)
        else:
            self.session.deselect("body")
            self.session.ensure_tabs()
        self.session.sandbox_open(self.session.intent.face_tab().tab_id, toolbelt)
        return self.session.intent.focused_instance

    def tab(self, body: str | None = "myapi") -> str:
        """The id of `body`'s tab, or with None the machine tab's."""
        tab = (self.session.intent.body_tab(body) if body is not None
               else self.session.intent.machine_tab())
        assert tab is not None, f"no tab for {body or 'the machine'}"
        return tab.tab_id

    def event_types(self) -> list[str]:
        return [e.type for e in self.events.tail(500)]

    def events_of(self, type_: str) -> list:
        return [e for e in self.events.tail(500) if e.type == type_]

    def deliver_runtime_events(self) -> None:
        """The runtime's events so far through the daemon's own dispatch, and each exit then
        run on its unit's queue to the end."""
        from types import SimpleNamespace
        from raigolmid.daemon import Daemon
        from raigolmid.supervisor import unit_of

        daemon = SimpleNamespace(session=self.session, events=self.events)
        daemon._exit = lambda unit, container: Daemon._exit(daemon, unit, container)
        queues = set()
        for event in self.runtime.events():
            Daemon._on_runtime_event(daemon, event)
            attributes = event["Actor"]["Attributes"]
            unit = unit_of(attributes.get(labels.ROLE, ""), attributes)
            if event["Action"] == "die" and unit is not None:
                queues.add(unit.queue)
        for name in queues:
            self.session.queues.run(name, lambda: None, "delivered", timeout=60)


def greying(call, row: str, item_id: str, reason: str):
    """`call` with the daemon's `list_items` refusing one item. Faces and bodies never
    constrain each other, so no daemon state greys a selector row; this is how the
    selector's handling of a refusal is reached."""
    def dispatch(method: str, **params):
        answer = call(method, **params)
        if method == "list_items":
            for item in answer.get(row, []):
                if item["id"] == item_id:
                    item.update(selectable=False, reason=reason)
        return answer
    return dispatch


def judge_says(runtime: FakeRuntime, *results: str):
    """The judge's runs, in order, each printing `claude -p --output-format json`'s result
    object with `result` as its text; returns the prompts it was given. A run past the last
    is refused, as the fake refuses any one-shot nothing was scripted for."""
    from raigolmid import naming
    from raigolmid.runtime.base import ExecResult, RuntimeError_
    left, prompts = list(results), []

    def run(spec):
        prompts.append(spec.command[1])
        if not left:
            raise RuntimeError_("fake: the judge ran more often than the test scripted")
        return ExecResult(0, "a line on stderr\n" + json.dumps(
            {"type": "result", "is_error": False, "result": left.pop(0)}) + "\n")
    runtime.one_shot[naming.judge()] = run
    return prompts


def settle_judging(judge, questions) -> None:
    """What the daemon's judge and questions threads do, once, in this thread: every job the
    judge was offered is taken, and what it said reaches `questions`."""
    while True:
        jobs = [e for e in judge._sub.drain(timeout=0.05)
                if e.type in ("question.judging", "question.learn")]
        for event in jobs:
            judge.take(event)
        heard = questions._sub.drain(timeout=0.05)
        for event in heard:
            questions.on_event(event)
        if not jobs and not heard:
            return


def standalone(tmp_path: Path, monkeypatch):
    """`Questions` and the judge over one log, with the agent image and the credential the
    judge runs with, and nothing else of a daemon."""
    from raigolmid.judge import Judge
    from raigolmid.questions import Questions
    sources = tmp_path / "sources"
    (sources / "agents" / "claude").mkdir(parents=True)
    (sources / "agents" / "claude" / "Dockerfile").write_text("FROM scratch\n")
    (sources / "agents" / "guide").mkdir()
    monkeypatch.setenv(hostimages.SOURCE_ENV, str(sources))
    events = EventLog(tmp_path / "events.jsonl", epoch=1)
    runtime = FakeRuntime()
    runtime.add_image(hostimages.agent().tag())
    credentials = tmp_path / "agent-credentials"
    credentials.write_text("CLAUDE_CODE_OAUTH_TOKEN=test-token\n")
    credentials.chmod(0o600)
    paths = Paths(state=tmp_path, data=tmp_path, config=tmp_path / "raigolmid", runtime=tmp_path)
    # What the daemon's start writes before anything reads a setting.
    settings.install(paths.settings)
    questions = Questions(events, paths)
    broker = Broker(credentials, tmp_path / "proxy-secret", tmp_path / "proxy-ca", runtime)
    return events, questions, Judge(events, runtime, tmp_path / "preferences.md", broker,
                                    1), runtime


def launcher_broker(paths: Paths) -> "ApiServer":
    """raigolmid's `open_launcher` on `paths.api_socket`, served by the real `ApiServer` and
    handed over as the daemon hands it (`api.Handoff`), for launchers a test runs itself on
    `paths.launcher_socket`. The name is resolved to the socket as `Session.open_launcher`
    does; which instances exist is `Session`'s and is held by its own tests. Stop it with
    `shutdown()` and `server_close()`."""
    import threading

    from raigolmid.api import ApiServer, Handoff
    from raigolmid.launcher.client import LauncherClient, LauncherUnreachable
    from raigolmid.session import SessionError

    def open_launcher(instance: str) -> Handoff:
        try:
            sock = LauncherClient(paths.launcher_socket(instance)).connect(timeout=None)
        except LauncherUnreachable as exc:
            raise SessionError(f"{instance}'s toolbelt does not answer: {exc}") from exc
        return Handoff({"instance": instance}, sock)

    server = ApiServer(paths.api_socket, {"open_launcher": open_launcher},
                       EventLog(paths.events, epoch=1), ready=answering())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
