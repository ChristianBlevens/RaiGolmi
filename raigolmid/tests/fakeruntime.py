"""An in-memory `ContainerRuntime` for tests.

It exists so the reference-counting, queue, build-lock, and reconciliation logic can be
tested as pure Python — those are where the design's correctness lives, and none of them
are about Docker. It models exactly the behaviours the daemon reasons about, including
the ones that are easy to forget: a namespace that cannot be joined unless its owner is
running, a removal refused while something still holds mounts, and an image that exists
only because something built it.

**The rule this file is held to: it must refuse what Docker refuses.** A double that
answers an unrecognised call with success is worse than no double, because a green suite
is read as evidence. Every refusal here is one Docker certainly makes; nothing is
modelled on a guess about the API.
"""
from __future__ import annotations

import dataclasses
import hashlib

import itertools
import json
import re
import subprocess
import tarfile
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from raigolmid import labels
from raigolmid.runtime.base import (
    BuildResult,
    ContainerInfo,
    ContainerRuntime,
    ContainerSpec,
    DiskUsage,
    ExecResult,
    ImageInfo,
    ImageInUse,
    RemoveBusy,
    RuntimeError_,
    require_bind_sources,
)


# References this project builds and publishes nowhere: the agent image
# (`agents/claude/Dockerfile`) and every body image (`naming.build_tag`). Docker cannot
# pull one, so a caller that never built it gets a pull failure rather than a container.
LOCAL_ONLY_IMAGES = ("raigolmi/", "raigolmid/body-")


class FakeRuntime(ContainerRuntime):
    def __init__(self) -> None:
        self._containers: dict[str, dict[str, Any]] = {}
        # What `processes` lists beyond its header, per container, as a test stages it.
        self.staged_processes: dict[str, list[str]] = {}
        self._images: dict[str, ImageInfo] = {}
        # What the registry names a reference by now, when a test moves it; otherwise the
        # digest `pull` gives it. `registry_offline` is a registry that cannot be reached.
        self.registry: dict[str, str] = {}
        self.registry_offline = False
        # Package names Nixery cannot build: a pull naming one is refused as Docker refuses
        # it, `not found` with no name.
        self.unbuildable: set[str] = set()
        self.builds_pulled: list[bool] = []
        self._ids = itertools.count(1)
        self._pids = itertools.count(1000)
        self._events: list[dict[str, Any]] = []
        self._clock = 0
        # Networks made by `ensure_network`, with their labels.
        self.networks: dict[str, dict[str, str]] = {}
        # Set by a test to make the next removal of a name fail as a busy one does.
        self.busy_on_remove: set[str] = set()
        self.staged_logs: dict[str, str] = {}
        # Per-tag, because `build_should_fail` is global: with only that switch, "this
        # body's build failed while that one's succeeded" cannot be expressed, and a
        # coalesced build shared by two instances is exactly that case.
        self.build_failures: set[str] = set()
        self.build_should_fail = False
        self.build_count = 0
        self.cache_prunes = 0
        self.disk: DiskUsage | None = None
        self.exec_log: list[tuple[str, list[str]]] = []
        self.spawn_log: list[tuple[str, list[str], dict[str, str]]] = []
        # What a command run in a body answers, keyed on the joined argv. A command a
        # test has not declared is refused rather than answered with success: the fake
        # does not know what it would do, and inventing an exit 0 is how a readiness
        # check that asked nothing passed.
        self.exec_results: dict[str, tuple[int, str]] = {}
        # Container names whose command runs to completion rather than staying up — a
        # `rm -rf`, a one-shot sweep. Declared by the test, because whether a command
        # returns is a fact about the command and the fake cannot read it off the spec.
        self.exits_when_started: set[str] = set()
        # What a container's process does as it starts, from its spec — declared by the
        # test, by name.
        self.on_start: dict[str, Callable[[ContainerSpec], None]] = {}
        # What starts the real process behind a container of a role, by `labels.ROLE`, for
        # the callers that read the kernel's view of a container rather than the runtime's:
        # a face's display is found in `/proc/<pid>/net/unix`, which no invented pid has.
        # The container's pid is that process's, and it ends when the container does.
        self.backing: dict[str, Callable[[ContainerSpec], subprocess.Popen]] = {}
        # What a run-to-completion container does, by name. None scripted is a refusal: a
        # fake that invented an exit would pass a caller that never looked at one.
        self.one_shot: dict[str, Callable[[ContainerSpec], ExecResult]] = {}

    # --- test helpers ----------------------------------------------------------------
    def add_image(self, reference: str, labels: dict[str, str] | None = None,
                  config: dict[str, Any] | None = None,
                  image_id: str | None = None) -> ImageInfo:
        # Shaped as Docker's are: a hex digest, which callers use as a path component.
        info = ImageInfo(id=image_id or "sha256:" + hashlib.sha256(reference.encode()).hexdigest(),
                         tags=(reference,),
                         labels=labels or {}, config=config or {})
        previous = self._images.get(reference)
        self._images[reference] = info
        if (previous is not None and previous.id != info.id
                and not any(i.id == previous.id for i in self._images.values())):
            # As Docker does: the image a tag moved off stays, under no name, until removed.
            self._images[previous.id] = dataclasses.replace(previous, tags=())
        return info

    def pause(self, name: str) -> None:
        """A container that is not running and still has a pid.

        Every other way of stopping one drops the pid to None, so without this the fake
        cannot express the state where `status` is the only thing that says a container is
        not usable — and a caller checking the pid alone passes here and reports a paused
        face as the face that is on the screen.
        """
        rec = self._live(name)
        rec["status"] = "paused"

    def kill(self, name: str, exit_code: int = 137) -> None:
        """A container dying without the daemon asking — the case reconciliation exists
        for."""
        rec = self._live(name)
        if rec["status"] not in ("running", "paused"):
            raise RuntimeError_(f"fake: '{name}' is {rec['status']}; only a running container dies")
        self._end(rec, exit_code, ("die",))

    def _end(self, rec: dict[str, Any], exit_code: int, actions: tuple[str, ...]) -> None:
        """A container's process ending. Every container sharing its PID namespace
        (`--pid=container:<it>`) ends first: the kernel SIGKILLs a namespace's processes when
        its init exits, and init's exit completes only once they are gone (pid_namespaces(7))
        — a body and view going down with their anchor."""
        for joiner in list(self._containers.values()):
            ref = joiner["spec"].pid_mode
            if (ref in (f"container:{rec['id']}", f"container:{rec['name']}")
                    and joiner["status"] in ("running", "paused")):
                self._end(joiner, 137, ("die",))
        self._end_process(rec)
        rec["status"] = "exited"
        rec["exit_code"] = exit_code
        rec["pid"] = None
        for action in actions:
            self._emit(action, rec)

    def _begin_process(self, rec: dict[str, Any]) -> None:
        """A container's process starting: the real one its role is backed by, or else an
        invented pid."""
        starter = self.backing.get(rec["labels"].get(labels.ROLE, ""))
        if starter is None:
            rec["pid"] = next(self._pids)
            return
        rec["process"] = starter(rec["spec"])
        rec["pid"] = rec["process"].pid

    @staticmethod
    def _end_process(rec: dict[str, Any]) -> None:
        """A backing process ending with its container, so what it held in the kernel is
        gone when the container is, as it is when Docker stops one."""
        process = rec.pop("process", None)
        if process is None:
            return
        process.terminate()
        with process:  # closes its pipes and reaps it
            pass

    def start(self, name: str) -> None:
        """What `docker compose up -d` does to a service that stopped. Not part of
        `ContainerRuntime`: only the Compose double asks for it."""
        rec = self._live(name)
        if rec["status"] != "exited":
            raise RuntimeError_(f"fake: '{name}' is {rec['status']}; only a stopped container starts")
        rec["status"] = "running"
        rec["exit_code"] = None
        self._begin_process(rec)
        rec["started_at"] = datetime.now(UTC).isoformat()
        self._emit("start", rec)

    def mark_for_removal(self, name: str) -> None:
        """A container Docker is part-way through removing: it is still listed, and asking
        for its logs is refused, as for a view torn down under a reader."""
        self._live(name)["status"] = "removing"

    def _live(self, name: str) -> dict[str, Any]:
        rec = self._containers.get(name)
        if rec is None or rec["status"] == "removed":
            raise RuntimeError_(f"fake: no container '{name}'")
        return rec

    def _emit(self, action: str, rec: dict[str, Any]) -> None:
        # Docker's Attributes are the labels plus the name and image, in a dict of their
        # own: a caller that changed one could never reach the container's labels.
        attributes = {**rec["labels"], "name": rec["name"], "image": rec["image"]}
        if action == "die":
            attributes["exitCode"] = str(rec["exit_code"])
        # Strictly increasing, as the daemon skips an event no later than the last it handled.
        self._clock = max(time.time_ns(), self._clock + 1)
        self._events.append({"Action": action, "timeNano": self._clock,
                             "Actor": {"ID": rec["id"], "Attributes": attributes}})

    def _published(self) -> dict[int, str]:
        """Host port → the container holding it, across every container still running."""
        held: dict[int, str] = {}
        for name, rec in self._containers.items():
            if rec["status"] == "removed":
                continue
            for host in rec["spec"].ports.values():
                held[host] = name
        return held

    def _require_joinable(self, field: str, ref: str | None, joiner: str) -> None:
        """`--pid=container:<ref>` and `--network=container:<ref>` need that container
        running: a namespace dies with the process that holds it. So the anchor is up before the view and the body join it."""
        if not ref or not ref.startswith("container:"):
            return
        target = ref.split(":", 1)[1]
        info = self.inspect(target)
        if info is None:
            raise RuntimeError_(
                f"could not start '{joiner}': no such container '{target}' to join its "
                f"{field} namespace")
        if not info.running:
            raise RuntimeError_(
                f"could not start '{joiner}': cannot join {field} namespace of "
                f"non-running container '{target}' (it is {info.status})")

    def _record(self, name: str) -> dict[str, Any]:
        if name not in self._containers:
            raise RuntimeError_(f"no such container '{name}'")
        return self._containers[name]

    # --- containers ------------------------------------------------------------------
    def run(self, spec: ContainerSpec) -> ContainerInfo:
        require_bind_sources(spec)
        if spec.name in self._containers and self._containers[spec.name]["status"] != "removed":
            raise RuntimeError_(f"container name '{spec.name}' is already in use")
        # Same order as `DockerRuntime.run`: an image that is not here is pulled, and a
        # pull that cannot work fails the run. Without it every image exists, and a path
        # that forgot to build one passes here and fails only against real Docker.
        if spec.image.startswith("sha256:"):
            # An id names an image that is here or nothing: Docker pulls references only.
            if not any(i.id == spec.image for i in self._images.values()):
                raise RuntimeError_(f"No such image: {spec.image}")
        elif self.image(spec.image) is None:
            self.pull(spec.image)
        # What docker-py refuses before asking the daemon, and what the daemon refuses: both
        # kinds of network, aliases with no network to answer on, and a network nobody made.
        if spec.network and spec.network_mode:
            raise RuntimeError_('The options "network" and "network_mode" can not be used '
                                'together.')
        for field, ref in (("pid", spec.pid_mode), ("network", spec.network_mode)):
            self._require_joinable(field, ref, spec.name)
        if spec.aliases and not spec.network:
            raise RuntimeError_(f"could not start '{spec.name}': aliases {spec.aliases} "
                                "with no network to answer on")
        if spec.network and spec.network not in self.networks:
            raise RuntimeError_(f"network {spec.network} not found")
        # A host port is exclusive, so the fake refuses a second claim on one the way the
        # daemon does. Without this, N instances of one body publishing the same port pass
        # here and fail only against real Docker.
        for taken, holder in self._published().items():
            if taken in spec.ports.values():
                raise RuntimeError_(
                    f"driver failed programming external connectivity on endpoint "
                    f"{spec.name}: Bind for 0.0.0.0:{taken} failed: port is already "
                    f"allocated (held by '{holder}')")
        n = next(self._ids)
        rec = {
            "id": f"c{n:06d}",
            "name": spec.name,
            "image": spec.image,
            "image_id": (spec.image if spec.image.startswith("sha256:")
                         else self.image(spec.image).id),
            "status": "running",
            "labels": dict(spec.labels),
            "pid": None,
            "exit_code": None,
            "started_at": datetime.now(UTC).isoformat(),
            "ip": (None if spec.network_mode else
                   f"{'172.18' if spec.network else '172.17'}.{n // 250}.{n % 250 + 2}"),
            "spec": spec,
        }
        self._begin_process(rec)
        self._containers[spec.name] = rec
        self._emit("start", rec)
        if spec.name in self.exits_when_started:
            self._end(rec, 0, ("die",))
        elif spec.name in self.on_start:
            self.on_start[spec.name](spec)
        return self._to_info(rec)

    def run_to_completion(self, spec: ContainerSpec, timeout: float) -> ExecResult:
        if spec.name not in self.one_shot:
            raise RuntimeError_(f"fake: nothing scripted for one-shot container '{spec.name}'")
        self.run(spec)
        try:
            return self.one_shot[spec.name](spec)
        finally:
            self.remove(spec.name, force=True)

    @staticmethod
    def _to_info(rec: dict[str, Any]) -> ContainerInfo:
        return ContainerInfo(id=rec["id"], name=rec["name"], image=rec["image"],
                             status=rec["status"], labels=dict(rec["labels"]),
                             pid=rec["pid"], exit_code=rec["exit_code"],
                             started_at=rec["started_at"], image_id=rec["image_id"],
                             ip=rec["ip"])

    def spec_of(self, name_or_id: str) -> ContainerSpec:
        """The spec a container was started from. Only a test asks: it is how the arguments
        raigolmid passes into a view are checked without a Docker daemon to read them back
        from."""
        rec = self._record(name_or_id)
        return rec["spec"]

    def inspect(self, name_or_id: str) -> ContainerInfo | None:
        for rec in self._containers.values():
            if name_or_id in (rec["name"], rec["id"]) and rec["status"] != "removed":
                return self._to_info(rec)
        return None

    def list(self, label_filter: dict[str, str] | None = None,
             all_states: bool = True) -> list[ContainerInfo]:
        out = []
        for rec in self._containers.values():
            if rec["status"] == "removed":
                continue
            if not all_states and rec["status"] != "running":
                continue
            if label_filter and any(rec["labels"].get(k) != v for k, v in label_filter.items()):
                continue
            out.append(self._to_info(rec))
        return out

    def stop(self, name_or_id: str, timeout: int = 10) -> None:
        for rec in self._containers.values():
            # A paused container is stopped too: Docker signals it and unpauses it to die.
            if name_or_id in (rec["name"], rec["id"]) and rec["status"] in ("running", "paused"):
                # Docker reports a requested stop as kill, die, stop: a `die` alone does not
                # say whether anyone asked.
                self._end(rec, 0, ("kill", "die", "stop"))

    def remove(self, name_or_id: str, force: bool = False) -> None:
        for rec in list(self._containers.values()):
            # One already removed is Docker's NotFound, which `DockerRuntime` answers with
            # nothing: no refusal and no second `destroy`.
            if name_or_id not in (rec["name"], rec["id"]) or rec["status"] == "removed":
                continue
            if rec["name"] in self.busy_on_remove and not force:
                raise RemoveBusy(
                    f"'{rec['name']}' cannot be removed: device or resource busy. "
                    "Something still holds mounts into it."
                )
            if rec["status"] == "running" and not force:
                raise RuntimeError_(
                    f"could not remove '{name_or_id}': 409 Conflict: You cannot remove a "
                    f"running container {rec['id']}. Stop the container before "
                    "attempting removal or force remove")
            if rec["status"] == "paused" and not force:
                raise RuntimeError_(
                    f"could not remove '{name_or_id}': 409 Conflict: cannot remove container "
                    f"\"{rec['name']}\": container is paused and must be unpaused first")
            if rec["status"] in ("running", "paused"):
                self._end(rec, 137, ("die",))
            rec["status"] = "removed"
            rec["pid"] = None
            self._emit("destroy", rec)

    def processes(self, name_or_id: str) -> str:
        # Docker refuses `top` for a container it does not have and for one not running.
        info = self.inspect(name_or_id)
        if info is None:
            raise RuntimeError_(f"could not list the processes of '{name_or_id}': 404 Not "
                                f"Found: No such container: {name_or_id}")
        if not info.running:
            raise RuntimeError_(f"could not list the processes of '{name_or_id}': 409 "
                                f"Conflict: Container {name_or_id} is not running")
        return "\n".join(["PID\tPPID\tELAPSED\tCOMMAND",
                          *self.staged_processes.get(name_or_id, [])])

    def logs(self, name_or_id: str, tail: int = 100) -> str:
        # Docker has no logs for a container it does not have, and `DockerRuntime` turns
        # that into "". A double that invents a line for a name that was never created
        # hides exactly the case the log is read in: the container is already gone.
        info = self.inspect(name_or_id)
        if info is None:
            return ""
        if info.status == "removing":
            raise RuntimeError_(f"could not read logs for '{name_or_id}': 409 Conflict: can not "
                                "get logs from container which is dead or marked for removal")
        # A test stages the output a container actually produced; one that printed nothing
        # has an empty log. The errors that carry a log carry it because the reason is only
        # ever in there, so a test asserting the reason reached the user stages one.
        return self.staged_logs.get(name_or_id, "")

    def _exec_target(self, name_or_id: str) -> None:
        rec = self.inspect(name_or_id)
        if rec is None:
            raise RuntimeError_(f"'{name_or_id}' is gone; cannot exec in it")
        if rec.labels.get("io.raigolmi.role") == "view":
            raise RuntimeError_(
                "refusing to docker exec into a session view"
            )
        if not rec.running:
            # Docker answers 409 for this. The daemon execs in a body whose liveness it
            # believes it knows, so the case it must get right is the one where it is wrong.
            raise RuntimeError_(
                f"cannot exec in '{name_or_id}': the container is not running "
                f"(it is {rec.status})")

    def spawn(self, name_or_id: str, cmd: list[str], *,
              environment: dict[str, str] | None = None) -> str:
        self._exec_target(name_or_id)
        self.spawn_log.append((name_or_id, list(cmd), dict(environment or {})))
        return f"exec-{len(self.spawn_log)}"

    def exec(self, name_or_id: str, cmd: list[str], *,
             environment: dict[str, str] | None = None,
             workdir: str | None = None) -> ExecResult:
        self._exec_target(name_or_id)
        self.exec_log.append((name_or_id, list(cmd)))
        joined = " ".join(cmd)
        if joined not in self.exec_results:
            raise RuntimeError_(
                f"the fake runtime has no answer for `{joined}` in '{name_or_id}'. "
                "Declare it in `exec_results`: answering an undeclared command with exit "
                "0 is the double inventing a result the real command may not give.")
        code, output = self.exec_results[joined]
        return ExecResult(exit_code=code, output=output)

    # --- images ----------------------------------------------------------------------
    def image(self, reference: str) -> ImageInfo | None:
        found = self._images.get(reference)
        if found is None and reference.startswith("sha256:"):
            found = next((i for i in self._images.values() if i.id == reference), None)
        if found is None and "@" in reference:
            found = next((i for i in self._images.values() if reference in i.repo_digests), None)
        return found

    def pull(self, reference: str) -> ImageInfo:
        existing = self._images.get(reference)
        moved = reference in self.registry and (
            existing is None or existing.id != self.registry[reference])
        if existing is not None and not moved and not self.registry_offline:
            return existing
        if reference.startswith(LOCAL_ONLY_IMAGES):
            raise RuntimeError_(
                f"could not pull '{reference}': pull access denied, repository does not "
                "exist or may require authorisation. This image is built locally and "
                "published nowhere, so it has to exist before anything runs it.")
        if reference.startswith("nixery.dev/") and self.unbuildable & set(
                reference.partition("@")[0].split("/")[2:]):
            raise RuntimeError_(f"could not pull '{reference}': 404 Client Error: Not Found "
                                f'("failed to resolve reference \\"{reference}:latest\\": '
                                f'{reference}:latest: not found")')
        if self.registry_offline:
            raise RuntimeError_(f"could not pull '{reference}': dial tcp: lookup "
                                "registry-1.docker.io: no such host")
        # As a registry answers: the manifest's digest is the image's id and its repo digest
        # (Docker's containerd store).
        repo, _, digest = reference.partition("@")
        head, _, last = repo.rpartition("/")
        repo = f"{head}/{last.split(':')[0]}" if head else last.split(":")[0]
        info = self.add_image(reference)
        if not digest:
            digest = self.registry_digest(reference)
        info = dataclasses.replace(info, id=digest, repo_digests=(f"{repo}@{digest}",))
        self._images[reference] = info
        return info

    def registry_digest(self, reference: str) -> str:
        if self.registry_offline:
            raise RuntimeError_(f"the registry did not answer for '{reference}': dial tcp: "
                                "lookup registry-1.docker.io: no such host")
        if reference.startswith(LOCAL_ONLY_IMAGES):
            raise RuntimeError_(f"the registry did not answer for '{reference}': "
                                "repository does not exist or may require authorisation")
        return self.registry.get(
            reference, "sha256:" + hashlib.sha256(reference.encode()).hexdigest())

    def build(self, context: str, dockerfile: str, tag: str,
              target: str | None = None,
              buildargs: dict[str, str] | None = None, pull: bool = False) -> BuildResult:
        """Refuses the three things the daemon can get wrong and Docker would catch.

        `instances.py:155-160` computes `dockerfile` *relative to* `build_context` and
        passes `body.build_target`, and `Body.source_root` decides what the context even
        is (`definitions.py:200-212`) — the working copy when it resolves, the definition
        directory otherwise. Accepting all of that unread meant a context pointing at the
        wrong one of those two, a dockerfile path that does not resolve inside it, and a
        `target` naming no stage all built clean, and the image appeared either way.
        """
        self.build_count += 1
        self.builds_pulled.append(pull)
        if self.build_should_fail or tag in self.build_failures:
            return BuildResult(image_id="", log="fake build failure", succeeded=False)

        root = Path(context)
        if not root.is_dir():
            return BuildResult(image_id="", succeeded=False,
                               log=f"unable to prepare context: path {context!r} not found")
        containing = root / dockerfile
        if not containing.is_file():
            return BuildResult(
                image_id="", succeeded=False,
                log=f"unable to prepare context: unable to evaluate symlinks in Dockerfile "
                    f"path: lstat {containing}: no such file or directory")
        if target is not None:
            # `FROM x AS name`, and Dockerfile keywords are case-insensitive.
            stages = set(re.findall(r"(?im)^\s*FROM\s+\S+\s+AS\s+(\S+)",
                                    containing.read_text(encoding="utf-8")))
            if target not in stages:
                return BuildResult(
                    image_id="", succeeded=False,
                    log=f"failed to solve: target stage \"{target}\" could not be found")

        # As `docker build` does: a `FROM` image it lacks is fetched, and with `pull` every
        # one is fetched again; one that cannot be fetched fails the build.
        text = containing.read_text(encoding="utf-8")
        stages = {s.lower() for s in re.findall(r"(?im)^\s*FROM\s+\S+\s+AS\s+(\S+)", text)}
        for ref in re.findall(r"(?im)^\s*FROM\s+(?:--\S+\s+)*(\S+)", text):
            if ref.lower() in stages or ref == "scratch" or "$" in ref:
                continue
            if pull or self.image(ref) is None:
                try:
                    self.pull(ref)
                except RuntimeError_ as exc:
                    return BuildResult(image_id="", succeeded=False, log=f"failed to solve: {exc}")
        info = self.add_image(tag)
        return BuildResult(image_id=info.id, log=f"built {tag}\n", succeeded=True)

    def load(self, archive: Path) -> list[str]:
        """Reads the archive's own `manifest.json`, as `docker load` does: a path that is not
        a docker-archive is refused, and only the tags it names appear."""
        try:
            with tarfile.open(archive) as tar:
                member = tar.extractfile("manifest.json")
                manifest = json.load(member) if member else None
        except (OSError, KeyError, tarfile.TarError, json.JSONDecodeError) as exc:
            raise RuntimeError_(f"could not load {archive}: {exc}") from exc
        if not isinstance(manifest, list):
            raise RuntimeError_(f"could not load {archive}: invalid manifest.json")
        # Docker names what it loaded in its familiar form: `docker.io/` and `library/` go,
        # and any other registry — podman's `localhost/` included — stays.
        tags = []
        for entry in manifest:
            # An image's id is the digest of its config, so loading what is already here moves
            # nothing, and loading another build of it moves its tags.
            config = entry.get("Config")
            image_id = None
            if config:
                with tarfile.open(archive) as tar:
                    try:
                        member = tar.extractfile(config)
                    except KeyError as exc:
                        raise RuntimeError_(f"could not load {archive}: {exc}") from exc
                    image_id = "sha256:" + hashlib.sha256(member.read()).hexdigest()
            for tag in entry.get("RepoTags") or ():
                tag = re.sub(r"^docker\.io/(library/)?", "", tag)
                self.add_image(tag, image_id=image_id)
                tags.append(tag)
        return tags

    def remove_image(self, reference: str) -> None:
        info = self.image(reference)
        if info is None:
            return
        reference = next(t for t, i in self._images.items() if i is info)
        # As Docker does: a container created from it holds it, stopped or not.
        users = [rec["name"] for rec in self._containers.values()
                 if rec["status"] != "removed" and rec["image_id"] == info.id]
        if users and not [t for t, i in self._images.items() if i.id == info.id and t != reference]:
            raise ImageInUse(f"'{reference}' is in use by {', '.join(users)}")
        del self._images[reference]

    def prune_build_cache(self) -> int:
        self.cache_prunes += 1
        return 0

    def disk_usage(self) -> DiskUsage:
        """`disk` as a test sets it; a test that has not, asks of a runtime that cannot answer."""
        if self.disk is None:
            raise RuntimeError_("the fake runtime was given no disk usage")
        return self.disk

    def list_images(self, label_filter: dict[str, str] | None = None) -> list[ImageInfo]:
        out = []
        for info in self._images.values():
            if label_filter and any(info.labels.get(k) != v for k, v in label_filter.items()):
                continue
            out.append(info)
        return out

    def bridge_gateway(self) -> str:
        return "127.0.0.1"

    def ensure_network(self, name: str, labels: dict[str, str]) -> None:
        self.networks.setdefault(name, dict(labels))

    def events(self, label_filter: dict[str, str] | None = None,
               since: int | None = None) -> Iterator[dict[str, Any]]:
        # Drained rather than blocking, so a test reads what has happened so far; filtered as
        # the daemon's own subscription is, server side.
        while self._events:
            event = self._events.pop(0)
            attributes = event["Actor"]["Attributes"]
            if since is not None and event["timeNano"] < since:
                continue
            if not label_filter or all(attributes.get(k) == v for k, v in label_filter.items()):
                yield event

    def close(self) -> None:
        return
