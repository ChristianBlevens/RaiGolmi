"""Test doubles for the two things that need a real machine.

`FakeRuntime` (`tests/fakeruntime.py`) models Docker. These two model the
Compose CLI and a session view's launcher, so the session's own logic — reference
counting, the swap sequence, reconciliation, the queues — is testable as pure Python.

They model the behaviours the daemon *reasons about*, not Docker's full surface: a
Compose project whose body joins an anchor, and a launcher that answers or does not.
"""
from __future__ import annotations

import json
import re
import threading
import zlib
from pathlib import Path

from raigolmid import naming
from raigolmid.compose import ComposeError
from raigolmid.runtime.base import ContainerSpec, ExecResult, RemoveBusy, RuntimeError_

# compose-go's template: an escaped `$`, a variable, or a `$` it refuses.
_TEMPLATE = re.compile(r"\$\$|\$(?:(?P<name>[_A-Za-z][_A-Za-z0-9]*)|\{(?P<braced>[^}]*)\}|)")
from tests.fakeruntime import FakeRuntime


class FakeCompose:
    """Runs the generated project's single service as a container in the FakeRuntime, so
    the body really does appear with the labels, name and namespace the real one would
    give it."""

    def __init__(self, runtime: FakeRuntime) -> None:
        self.runtime = runtime
        self.up_calls: list[tuple[str, bool]] = []
        self.fail_next_up = False

    def available(self) -> bool:
        return True

    @staticmethod
    def _service_name(project: str) -> str:
        return f"{project}-body-1"

    def config(self, project: str, file: Path) -> str:
        return file.read_text(encoding="utf-8")

    def up(self, project: str, file: Path, force_recreate: bool = False) -> None:
        self.up_calls.append((project, force_recreate))
        if self.fail_next_up:
            self.fail_next_up = False
            raise ComposeError(f"docker compose -p {project} up failed (fake)")
        name = self._service_name(project)
        generated = self._read_generated(file)
        existing = self.runtime.inspect(name)
        if existing is not None:
            # `up -d` leaves a running service whose config is unchanged, starts one that
            # stopped, and recreates one whose image changed.
            if not force_recreate and existing.image == generated["image"]:
                if existing.status != "running":
                    self.runtime.start(name)
                return
            self._remove(project, name, stopped_first=True)
        try:
            self.runtime.run(ContainerSpec(
                name=name,
                image=generated["image"],
                # Every label the service carries, as compose applies them: reconciliation
                # reads some back (the body's ports), so dropping one here would
                # test a body nobody writes.
                labels=self._read_labels(file),
                # The body joins the anchor's namespaces and the generated file
                # says so. Dropping them here would mean no test ever asks whether the
                # anchor is up first, which is the whole of the startup ordering.
                pid_mode=generated["pid"] or None,
                network_mode=generated["network_mode"] or None,
            ))
        except RuntimeError_ as exc:
            # The CLI's only channel is an exit code, so everything Docker refuses
            # arrives at the daemon as a ComposeError.
            raise ComposeError(f"docker compose -p {project} up failed: {exc}") from exc

    @staticmethod
    def _load(file: Path) -> dict:
        """The generated project as Compose reads it: parsed (the emitter writes JSON, which
        is YAML), then interpolated — `$$` is a literal `$`, `$NAME` or `${NAME}` an
        environment variable, unset here and so empty as Compose makes it, and a `$` before
        anything else a template Compose refuses."""
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ComposeError(f"the generated project at {file} does not parse: {exc}") \
                from exc

        def interpolate(value):
            if isinstance(value, str):
                def one(m: re.Match) -> str:
                    if m.group(0) == "$$":
                        return "$"
                    if m.group("name") is None and m.group("braced") is None:
                        raise ComposeError(f"Invalid template: {value!r} in {file}")
                    return ""
                return _TEMPLATE.sub(one, value)
            if isinstance(value, dict):
                return {k: interpolate(v) for k, v in value.items()}
            if isinstance(value, list):
                return [interpolate(v) for v in value]
            return value
        return interpolate(data)

    @classmethod
    def _read_generated(cls, file: Path) -> dict:
        """The service the fake has to honour, read back from the file rather than passed
        in, so what runs is what was written. Real `docker compose up -d --no-build` exits
        non-zero on a service with no image, and the CLI's only channel is that exit code,
        so the refusal arrives as a ComposeError here too."""
        service = cls._load(file).get("services", {}).get("body") or {}
        missing = sorted(k for k in ("image", "pid", "network_mode") if not service.get(k))
        if missing:
            raise ComposeError(
                f"the generated project at {file} has no {', '.join(missing)}. "
                f"compose.render writes all of these unconditionally, so this is the "
                f"emitter, not the fake. docker compose would refuse it.")
        # The working copy is what /work is, and nothing else mounts it. The fake
        # runtime does not model mounts, so this is the only place the emitter's volume is
        # checked at all — without it, a body with no /work starts clean in every test.
        if not any(str(v).endswith(":/work") for v in service.get("volumes", [])):
            raise ComposeError(
                f"the generated project at {file} mounts no working copy at /work")
        return service

    @classmethod
    def _read_labels(cls, file: Path) -> dict[str, str]:
        return dict(cls._read_generated(file).get("labels", {}))

    def down(self, project: str, file: Path, volumes: bool = False) -> None:
        name = self._service_name(project)
        if self.runtime.inspect(name) is not None:
            self._remove(project, name, stopped_first=True)

    def _remove(self, project: str, name: str, stopped_first: bool) -> None:
        """Compose stops a service and then removes it the ordinary way — it has no
        `--force`, so a removal something still holds mounts into fails, and the CLI's
        nonzero exit is a ComposeError. Forcing here would make the busy-removal failure
        unreachable through the path bodies actually take."""
        if stopped_first:
            self.runtime.stop(name)
        try:
            self.runtime.remove(name)
        except RemoveBusy as exc:
            raise ComposeError(
                f"docker compose -p {project} down exited 1\nError response from daemon: "
                f"{exc}") from exc

    def ps(self, project: str, file: Path) -> list[dict]:
        info = self.runtime.inspect(self._service_name(project))
        return [] if info is None else [{"Name": info.name, "State": info.status}]

    def container_id(self, project: str, file: Path, service: str) -> str | None:
        # `ps -q <service>` lists running containers only.
        info = self.runtime.inspect(self._service_name(project))
        return info.id if info is not None and info.status == "running" else None


class FakeLaunchers:
    """Stands in for the launcher sockets in every session view.

    Installed by monkeypatching `Views.client`. `alive` is the property reconciliation
    turns on, so a test makes a view unreachable by naming it here.
    """

    def __init__(self, runtime: FakeRuntime) -> None:
        # A launcher answers only while its view container runs: it is the view's pid 1's
        # child, so a view torn down or dead takes the socket with it.
        self.runtime = runtime
        self.unreachable: set[str] = set()
        self.started: list[tuple[str, list[str]]] = []
        self.exec_results: dict[str, tuple[int, str]] = {}
        # The far end of each connection `connect` handed out, by instance.
        self.connections: list[tuple[str, object]] = []
        # What `ls -1 /nix/store` finds in the view — the closure `toolbelt.lock` records.
        # Names, not paths, because that is what the command prints.
        self.store_names: list[str] = ["8xk3v1q7-bashInteractive-5.2",
                                       "b2m9zr04-coreutils-9.5",
                                       "q7n1d5wc-neovim-0.10.2"]
        # Each view's jobs as its launcher knows them, by the view container that runs it:
        # a recreated view is a new launcher, which never knew the old one's jobs.
        self.jobs: dict[str, dict[int, dict]] = {}
        self._lock = threading.Lock()

    def launcher(self, instance: str) -> str:
        return self.runtime.inspect(naming.view(instance)).id

    def print_job(self, instance: str, proc: int, text: str) -> None:
        self.jobs[self.launcher(instance)][proc]["printed"] += text

    def end_job(self, instance: str, proc: int, code: int) -> None:
        self.jobs[self.launcher(instance)][proc]["exit"] = code

    def client_for(self, instance: str) -> "FakeLauncherClient":
        return FakeLauncherClient(self, instance)


class FakeLauncherClient:
    def __init__(self, parent: FakeLaunchers, instance: str) -> None:
        self.parent = parent
        self.instance = instance
        self.socket_path = Path(f"/run/raigolmid/views/{instance}.sock")

    def alive(self, timeout: float = 2.0) -> bool:
        view = self.parent.runtime.inspect(naming.view(self.instance))
        return (view is not None and view.status == "running"
                and self.instance not in self.parent.unreachable)

    def _require_reachable(self) -> None:
        """Every op below opens the socket first, so every op fails when it cannot.

        The real client routes `exec` through `_connect`, which raises `LauncherUnreachable` when the socket is missing or
        refusing (`launcher/client.py:46-56`). A fake where only `ping` knows about
        unreachability lets a view that reconciliation has just declared undrivable go on
        running commands — and makes the production handlers for that case unreachable
        code that no test can enter.
        """
        if not self.alive():
            from raigolmid.launcher.client import LauncherUnreachable
            raise LauncherUnreachable(f"{self.socket_path}: not answering")

    def ping(self, timeout: float = 2.0) -> dict:
        self._require_reachable()
        return {"ok": True, "generation": 1, "instance": self.instance}

    def connect(self, timeout: float | None = None):
        """A real socket, as the real client's is, whose far end is kept in `connections`:
        what `Session.open_launcher` hands over is a connection, and a test reads what
        arrives on the other side."""
        import socket
        self._require_reachable()
        near, far = socket.socketpair()
        self.parent.connections.append((self.instance, far))
        return near

    def start_job(self, cmd: list[str], *, cwd: str = "/work") -> tuple[int, str]:
        self._require_reachable()
        launcher = self.parent.launcher(self.instance)
        mine = self.parent.jobs.setdefault(launcher, {})
        proc = len(mine) + 1
        mine[proc] = {"cmd": list(cmd), "cwd": cwd, "exit": None, "printed": ""}
        return proc, launcher

    def process(self, launcher: str, proc_id: int) -> dict | None:
        """As the real one: None from another launcher, and a KeyError for a proc this one
        never issued."""
        self._require_reachable()
        if launcher != self.parent.launcher(self.instance):
            return None
        job = self.parent.jobs[launcher][proc_id]
        return {"proc": proc_id, "exit": job["exit"]}

    def scrollback(self, proc: int, timeout: float = 5.0) -> str:
        self._require_reachable()
        return self.parent.jobs[self.parent.launcher(self.instance)][proc]["printed"]

    def exec(self, cmd: list[str], *, cwd: str = "/work",
             env: dict | None = None, stdin: str | None = None,
             timeout: float = 300.0):
        from raigolmid.launcher.client import ExecOutput
        self._require_reachable()
        with self.parent._lock:
            self.parent.started.append((self.instance, list(cmd)))
        joined = " ".join(cmd)
        if cmd[:1] == ["/.toolbelt/bin/ls"] and cmd[-1] == "/nix/store":
            assert "-1" in cmd, (
                "the store listing must be one path per line; without -1 the real `ls` "
                "columnates onto a tty and every name parsed out of it is wrong")
            return ExecOutput(exit_code=0,
                              stdout="".join(f"{n}\n" for n in self.parent.store_names),
                              stderr="")
        if joined not in self.parent.exec_results:
            raise AssertionError(
                f"the fake launcher has no answer for `{joined}`. Declare it in "
                "`exec_results`: a double that answers an unrecognised command with exit "
                "0 cannot fail on the thing the command is there to check.")
        code, out = self.parent.exec_results[joined]
        return ExecOutput(exit_code=code, stdout=out, stderr="")


class ClosureCopies:
    """The closure-copy helper (`closures.py`): `COPY_SCRIPT` in an image, as its shell runs it.

    An image's store is derived from its Nixery reference, one path per package, named by
    the package alone — so two closures carrying the same package share the path, as two
    closures from one nixpkgs pin do. A path already in the shared store is not copied again.
    Refuses what the script would: another command, or a missing mount.
    """

    def __init__(self, runtime: FakeRuntime) -> None:
        self.runtime = runtime
        self.copies: list[str] = []
        self.paths_copied: list[str] = []

    @staticmethod
    def store_name(package: str) -> str:
        return f"{zlib.crc32(package.encode()):08x}-{package}"

    def packages(self, image_id: str) -> list[str]:
        for info in self.runtime.list_images():
            if info.id == image_id:
                return ["bash", "coreutils", *info.tags[0].split("/shell/", 1)[1].split("/")]
        raise AssertionError(f"no image {image_id}")

    def __call__(self, spec: ContainerSpec) -> ExecResult:
        from raigolmid import closures
        if spec.command != ("bash", "-c", closures.COPY_SCRIPT):
            return ExecResult(1, f"unexpected command {spec.command}")
        mounts = {m.target: Path(m.source) for m in spec.mounts}
        for target in (closures.STORE_OUT, closures.IMAGE_OUT):
            if target not in mounts or not mounts[target].is_dir():
                return ExecResult(1, f"mkdir: cannot create directory '{target}/.incoming'")
        store, image = mounts[closures.STORE_OUT], mounts[closures.IMAGE_OUT]
        (image / "bin").mkdir()
        names = []
        for package in dict.fromkeys(self.packages(spec.image)):
            name = self.store_name(package)
            names.append(name)
            (image / "bin" / package).symlink_to(f"/nix/store/{name}/bin/{package}")
            if (store / name).exists():
                continue
            (store / name / "bin").mkdir(parents=True)
            (store / name / "bin" / package).write_text("#!/bin/sh\n")
            self.paths_copied.append(name)
        (image / "paths").write_text("".join(n + "\n" for n in names))
        self.copies.append(spec.image)
        return ExecResult(0, "")
