"""Integration tests against real Docker.

**These do not run in the implementing agent's container** — it has no Docker. They are
written to be run on the user's WSL2 machine:

    pytest raigolmid/tests/test_integration_docker.py -m docker -v

Each case here is written to
fail with the actual cause rather than an assertion count, because the person running them
is diagnosing a machine, not reviewing a patch.

They are slow: several images are pulled and a Nixery closure is built. Budget ~15 minutes
on a first run, a couple of minutes after that.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from raigolmid import labels, naming
from raigolmid.anchors import Anchors
from raigolmid.compose import BodyPlacement, ComposeCLI, write as write_compose
from raigolmid.definitions import load_body, load_toolbelt
from raigolmid.events import EventLog
from raigolmid.paths import Paths
from raigolmid.runtime import RemoveBusy
from raigolmid.runtime.docker_runtime import DockerRuntime
from raigolmid.toolbelts import ToolbeltResolver
from raigolmid.views import ViewPlan, Views

pytestmark = pytest.mark.docker

# The five body kinds. The point of the last three is that a
# Nix closure does not depend on the body's libc, which is the only reason a toolbelt can
# attach to musl or distroless at all.
BODIES = {
    "slim": ("python:3.12-slim", []),
    "alpine": ("python:3.12-alpine", []),
    "distroless": ("gcr.io/distroless/python3-debian12", []),
    "readonly": ("python:3.12-slim", ["--read-only"]),
}


@pytest.fixture(scope="session")
def runtime():
    try:
        rt = DockerRuntime()
        rt.client.ping()
    except Exception as exc:                           # noqa: BLE001
        pytest.skip(f"Docker is not reachable: {exc}")
    yield rt
    rt.close()


@pytest.fixture(scope="session", autouse=True)
def clean_slate(runtime):
    """Remove anything a previous run of *this suite* left behind, before it starts.

    An interrupted run is ordinary — Ctrl-C, a timeout, a failure before a teardown — and it
    leaves containers named after its instances. The next run then fails on `a view container
    named … already exists`, which is raigolmid's ordering check doing its job and
    saying nothing about the code under test. A suite that cannot be run twice is a suite
    that reports its own leftovers as defects.

    Only this suite's own resources are touched: every instance id here begins `itest-`, and
    the names are derived through `naming`, so nothing a real session owns is matched.
    """
    def sweep() -> None:
        listed = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"],
                                capture_output=True, text=True)
        # Every instance id in this file begins `itest-`, and so does every container named
        # after one — whether raigolmid derived the name through `naming` or the test passed
        # `--name` itself.
        ours = [n for n in listed.stdout.split() if "itest-" in n]
        # Views first: they hold mounts into the bodies, and releasing those before the body
        # goes is the required ordering. Anchors last, since both join their
        # namespaces.
        def order(name: str) -> int:
            if "-view-" in name:
                return 0
            if "-anchor-" in name:
                return 2
            return 1
        for name in sorted(ours, key=order):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        projects = subprocess.run(["docker", "compose", "ls", "-aq"], capture_output=True,
                                  text=True)
        for project in projects.stdout.split():
            if "itest" in project:
                subprocess.run(["docker", "compose", "-p", project, "down", "-v"],
                               capture_output=True)

    sweep()
    yield
    sweep()


# The views here run pyright against the body's site-packages and git against /work, which
# a toolbelt with only the view's own needs does not carry, so the suite defines its own.
PYTHON_TOOLBELT = """\
id = "itest-python"
name = "Integration Python"
supports = ["python:3.*"]
capabilities = ["lsp", "debug", "shell"]
packages = ["bashInteractive", "coreutils", "python3", "findutils", "gnugrep", "gnused",
            "util-linux", "procps", "libcap", "socat", "git", "ripgrep", "fd", "tmux",
            "pyright", "python3Packages.debugpy", "gdb", "neovim"]
"""


@pytest.fixture(scope="session")
def toolbelt_image(runtime, tmp_path_factory):
    directory = tmp_path_factory.mktemp("toolbelt")
    (directory / "toolbelt.toml").write_text(PYTHON_TOOLBELT)
    toolbelt = load_toolbelt(directory)
    closure = ToolbeltResolver().resolve(toolbelt)
    runtime.pull(closure.image)
    return closure.image


@pytest.fixture()
def env(tmp_path, monkeypatch, runtime):
    for var, value in (("XDG_STATE_HOME", tmp_path / "state"),
                       ("XDG_DATA_HOME", tmp_path / "data"),
                       ("XDG_CONFIG_HOME", tmp_path / "config"),
                       ("XDG_RUNTIME_DIR", tmp_path / "run"),
                       ("RAIGOLMID_VIEW_SOCKET_DIR", tmp_path / "views")):
        monkeypatch.setenv(var, str(value))
    paths = Paths.from_env()
    paths.ensure()
    # Deliberately no chmod here. `ensure()` sets this directory to 0700 and that is the
    # mode the daemon runs in; the daemon takes the view's uid from the directory's owner, so a
    # view's uid is the owner by construction and 0700 already reaches the socket. Widening
    # it would exercise a mode the daemon never produces, and the mode is the only thing
    # keeping the launcher socket private (paths.py `ensure`).
    events = EventLog(paths.events, epoch=1)
    yield paths, events


@pytest.fixture()
def instance(request, runtime, toolbelt_image, env, tmp_path):
    """Anchor → body → view, torn down in reverse order."""
    paths, events = env
    kind = getattr(request, "param", "slim")
    image, extra = BODIES[kind]
    instance_id = f"itest-{kind}@tab-1"
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    # The git protections are bind-mounted at view creation, so the repository has to
    # exist before the view is built or there is nothing to protect.
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=work, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit",
                    "-q", "--allow-empty", "-m", "x"], cwd=work, check=True)

    anchors = Anchors(runtime, epoch=1)
    views = Views(runtime, paths, epoch=1)
    anchors.ensure(instance_id)

    body_name = f"itest-body-{kind}"
    subprocess.run(["docker", "rm", "-f", body_name], capture_output=True)
    cmd = ["docker", "run", "-d", "--name", body_name,
           "--pid", f"container:{naming.anchor(instance_id)}",
           "--network", f"container:{naming.anchor(instance_id)}",
           "--label", f"{labels.MANAGED}=true",
           "--label", f"{labels.ROLE}=body",
           "--label", f"{labels.INSTANCE}={instance_id}",
           "-v", f"{work}:/work", *extra]
    if kind == "readonly":
        cmd += ["--tmpfs", "/tmp"]
    if kind == "distroless":
        cmd += [image, "-c", "import time; time.sleep(3600)"]
    else:
        cmd += [image, "sleep", "3600"]
    started = subprocess.run(cmd, capture_output=True, text=True)
    if started.returncode != 0:
        pytest.skip(f"could not start the {kind} body: {started.stderr.strip()}")

    body = runtime.inspect(body_name)
    views.create(ViewPlan(instance=instance_id, body_container_id=body.id,
                          anchor_ref=anchors.namespace_ref(instance_id),
                          toolbelt_image=toolbelt_image, working_copy=work,
                          generation=1))
    try:
        client = views.wait_until_usable(instance_id, timeout=90)
    except Exception as exc:                           # noqa: BLE001
        views.teardown(instance_id)
        subprocess.run(["docker", "rm", "-f", body_name], capture_output=True)
        anchors.remove(instance_id)
        pytest.fail(f"the {kind} view never became usable: {exc}")

    yield instance_id, client, views, work, body_name

    views.teardown(instance_id)                        # mounts released BEFORE the body
    subprocess.run(["docker", "rm", "-f", body_name], capture_output=True)
    anchors.remove(instance_id)


# --- the session view -------------------------------------------------------------------

@pytest.mark.parametrize("instance", list(BODIES), indirect=True)
def test_every_launcher_child_is_unprivileged(instance):
    """The entrypoint drops all capabilities and sets no_new_privs before starting
    the launcher, so everything it starts inherits that."""
    _, client, *_ = instance
    result = client.exec(["/.toolbelt/bin/cat", "/proc/self/status"], cwd="/")
    fields = dict(
        line.split(":", 1) for line in result.stdout.splitlines() if ":" in line)
    assert fields["CapEff"].strip() == "0000000000000000", \
        f"launcher children hold capabilities: {fields['CapEff']}"
    assert fields["NoNewPrivs"].strip() == "1"


@pytest.mark.parametrize("instance", list(BODIES), indirect=True)
def test_docker_exec_into_a_view_is_refused(instance, runtime):
    """The refusal is in the code as well as the design, because the whole capability
    argument collapses if anything takes this path."""
    instance_id, *_ = instance
    from raigolmid.runtime import RuntimeError_
    with pytest.raises(RuntimeError_, match="refusing to docker exec into a session view"):
        runtime.exec(naming.view(instance_id), ["/bin/sh", "-c", "true"])


@pytest.mark.parametrize("instance", ["slim", "alpine", "distroless"], indirect=True)
def test_the_view_sees_the_bodys_real_filesystem(instance):
    _, client, _, _, body_name = instance
    marker = subprocess.run(
        ["docker", "exec", body_name, "sh", "-c", "echo hi > /tmp/from-body"],
        capture_output=True, text=True)
    if marker.returncode == 0:
        seen = client.exec(["/.toolbelt/bin/cat", "/tmp/from-body"], cwd="/")
        assert seen.stdout.strip() == "hi"
        return

    # A distroless body cannot write the marker, having no shell — but skipping here would
    # leave the claim untested on the one body kind the toolbelt closure exists to serve.
    # Asked the other way round instead, with no cooperation from the body: the view reads
    # something only the body's filesystem has. The toolbelt's own Python lives under
    # /nix/store, so a stdlib at the body's Debian path can only be the body's.
    found = client.exec(["/.toolbelt/bin/sh", "-c", "ls -d /usr/lib/python3*/os.py"], cwd="/")
    assert found.ok and "os.py" in found.stdout, (
        "the view cannot read the body's own Python stdlib, so its root is not the body's "
        f"filesystem: {found.stdout!r} {found.stderr!r}"
    )


@pytest.mark.parametrize("instance", ["readonly"], indirect=True)
def test_a_read_only_body_still_gets_a_working_view(instance):
    """Nothing is created in the body, so the view works; writes into the body's own tree
    simply fail, which is what read-only means."""
    _, client, *_ = instance
    assert client.ping()["ok"]
    refused = client.exec(["/.toolbelt/bin/sh", "-c", "touch /usr/should-fail"], cwd="/")
    assert refused.exit_code != 0


def test_pyright_resolves_the_bodys_installed_package(instance, runtime):
    """The editor and the language server share the
    body's filesystem, so go-to-definition opens the library source."""
    _, client, _, work, body_name = instance
    installed = subprocess.run(
        ["docker", "exec", body_name, "pip", "install", "--no-cache-dir", "requests"],
        capture_output=True, text=True)
    if installed.returncode != 0:
        pytest.skip(f"could not install into the body: {installed.stderr[-300:]}")

    # python:3.12-slim's own interpreter, which the view reaches at the body's path.
    interpreter = "/usr/local/bin/python3"

    (work / "probe.py").write_text("import requests\nrequests.get('http://x')\n")
    # --pythonpath names the body's interpreter. Without it pyright analyses against the
    # toolbelt's, which is first on PATH, and reports the project's own dependencies as
    # unresolved — a working environment that looks broken.
    result = client.exec(["pyright", "--pythonpath", interpreter, "--outputjson",
                          "probe.py"], cwd="/work", timeout=300)
    payload = result.stdout
    assert "could not be resolved" not in payload, \
        f"pyright cannot see the body's site-packages:\n{payload[:1500]}"

    where = client.exec(
        ["/.toolbelt/bin/sh", "-c",
         "ls /usr/local/lib/python3*/site-packages/requests/__init__.py"], cwd="/")
    assert where.exit_code == 0, \
        "the library source is not at the body's own path inside the view"


# --- git ---------------------------------------------------------------------

def test_git_works_in_a_session_view_and_protected_files_are_read_only(instance):
    instance_id, client, _, work, _ = instance
    status = client.exec(["/.toolbelt/bin/git", "-c", "safe.directory=/work",
                          "status", "--short"], cwd="/work")
    assert status.exit_code == 0, f"git does not work in the view: {status.stderr}"

    hooked = client.exec(
        ["/.toolbelt/bin/sh", "-c", "echo '#!/bin/sh\\nexit 1' > /work/.git/hooks/pre-commit"],
        cwd="/work")
    assert hooked.exit_code != 0, \
        ".git/hooks is bound read-only over /work and must refuse a write"
    assert not (work / ".git" / "hooks" / "pre-commit").exists(), \
        "the hook reached the host's repository"

    config = client.exec(
        ["/.toolbelt/bin/sh", "-c", "echo x >> /work/.git/config"], cwd="/work")
    assert config.exit_code != 0, \
        ".git/config is bound read-only over /work and must refuse a write"


def test_the_view_writes_a_work_that_is_not_world_writable(instance):
    """The view runs as the working copy's owner, so an ordinary mode is enough.
    A world-writable /work would let any uid write and hide that binding entirely."""
    _, client, _, work, _ = instance
    mode = work.stat().st_mode & 0o777
    assert not mode & 0o002, f"the working copy is world-writable ({mode:#o})"

    written = client.exec(
        ["/.toolbelt/bin/sh", "-c", "echo hello > /work/written-by-the-view"], cwd="/work")
    assert written.exit_code == 0, f"the view cannot write /work: {written.stderr}"
    assert (work / "written-by-the-view").read_text() == "hello\n"


# --- the rebuild ordering -----------------------------------------------

def test_removing_a_body_under_a_live_view_and_after_teardown(runtime, toolbelt_image,
                                                              env, tmp_path):
    """The ordering rule, measured: with the view still holding mounts, and then after it
    has been torn down. If the first succeeds on this Docker, the ordering is still
    enforced — the view's mounts would point at a filesystem that is going away."""
    paths, _ = env
    instance_id = "itest-order@tab-1"
    anchors = Anchors(runtime, epoch=1)
    views = Views(runtime, paths, epoch=1)
    work = tmp_path / "order-work"
    work.mkdir()
    anchors.ensure(instance_id)
    name = "itest-order-body"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(["docker", "run", "-d", "--name", name,
                    "--pid", f"container:{naming.anchor(instance_id)}",
                    "--network", f"container:{naming.anchor(instance_id)}",
                    "-v", f"{work}:/work", "python:3.12-slim", "sleep", "3600"],
                   check=True, capture_output=True)
    body = runtime.inspect(name)
    views.create(ViewPlan(instance=instance_id, body_container_id=body.id,
                          anchor_ref=anchors.namespace_ref(instance_id),
                          toolbelt_image=toolbelt_image, working_copy=work,
                          generation=1))
    try:
        views.wait_until_usable(instance_id, timeout=90)

        runtime.stop(name)
        busy = False
        try:
            runtime.remove(name)
        except RemoveBusy:
            busy = True
        print(f"\nremoval with the view still mounted: "
              f"{'refused (busy)' if busy else 'succeeded'}")

        views.teardown(instance_id)
        if busy:
            runtime.remove(name)          # must succeed now
    finally:
        # Whatever happened above, this test's view must not outlive it: the next run would
        # inherit it and fail on the ordering check instead of on its own question.
        views.teardown(instance_id)
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        anchors.remove(instance_id)


def test_a_body_restart_does_not_kill_view_processes(instance):
    """The whole reason the anchor exists."""
    instance_id, client, _, _, body_name = instance
    proc_id = client.start_detached(["/.toolbelt/bin/sleep", "600"], cwd="/")
    subprocess.run(["docker", "restart", "-t", "2", body_name], check=True,
                   capture_output=True)
    time.sleep(3)
    procs = {p["proc"]: p for p in client.list()}
    assert proc_id in procs and procs[proc_id]["exit"] is None, \
        "the body restart killed a session-view process — the view is in the body's PID " \
        "namespace rather than the anchor's"


# A body built from its own Dockerfile whose command is busybox `httpd`.
MINIMAL_BODY = """\
id = "minimal"
name = "Minimal"
dockerfile = "Dockerfile"
context = "."
runtime = "busybox"
shell = "/bin/sh"
ports = [8000]
command = ["httpd", "-f", "-v", "-p", "8000", "-h", "/work"]
"""
MINIMAL_DOCKERFILE = """\
FROM busybox:1.36
WORKDIR /work
CMD ["httpd", "-f", "-v", "-p", "8000", "-h", "/work"]
"""


def minimal_body(directory: Path) -> Path:
    directory.mkdir(parents=True)
    (directory / "body.toml").write_text(MINIMAL_BODY)
    (directory / "Dockerfile").write_text(MINIMAL_DOCKERFILE)
    return directory


# --- Compose -----------------------------------------------------------------------

def test_compose_accepts_the_anchors_namespaces(runtime, env, tmp_path):
    """If this fails, raigolmid runs bodies through the Docker SDK."""
    paths, _ = env
    cli = ComposeCLI()
    if not cli.available():
        pytest.skip("docker compose is not installed")
    instance_id = "itest-compose@tab-1"
    anchors = Anchors(runtime, epoch=1)
    anchors.ensure(instance_id, ports={8000: 18124})
    body = load_body(minimal_body(tmp_path / "minimal"), ())
    work = tmp_path / "compose-work"
    work.mkdir()

    # The body's own image, built from its own Dockerfile the way instances.py does it. A
    # substituted image is not a shortcut here: the definition's command is busybox `httpd`,
    # so an image without it cannot start, and Compose's handling of the anchor's
    # namespaces is unmeasurable when the container never runs.
    assert body.dockerfile is not None and body.build_context is not None
    built = runtime.build(
        context=str(body.build_context),
        dockerfile=str(body.dockerfile.relative_to(body.build_context)),
        tag="raigolmi-itest-minimal:1",
        target=body.build_target,
    )
    if not built.succeeded:
        pytest.fail(f"the minimal body would not build, so Compose cannot be asked about "
                    f"its namespaces:\n{built.log[-2000:]}")

    path = write_compose(
        body,
        BodyPlacement(instance=instance_id,
                      namespace_ref=anchors.namespace_ref(instance_id),
                      working_copy=work, image="raigolmi-itest-minimal:1",
                      user=f"{os.getuid()}:{os.getgid()}"),
        epoch=1, definition_digest="sha256:test", directory=tmp_path / "project")
    project = naming.compose_project(instance_id)
    try:
        rendered = cli.config(project, path)
        assert "container:" in rendered
        cli.up(project, path)
        container = cli.container_id(project, path, "body")
        assert container, "Compose reported no container for the body service"
        # `up` returning cleanly is not the body running: a container that exits immediately
        # is started and gone, and then nothing has been learned about the namespaces.
        time.sleep(2)
        state = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", container],
                               capture_output=True, text=True)
        assert state.stdout.strip() == "true", (
            "the Compose-run body is not running, so whether Compose applied the anchor's "
            "namespaces cannot be read from it: "
            + subprocess.run(["docker", "logs", "--tail", "20", container],
                             capture_output=True, text=True).stderr.strip()
        )
    finally:
        try:
            cli.down(project, path)
        finally:
            anchors.remove(instance_id)
