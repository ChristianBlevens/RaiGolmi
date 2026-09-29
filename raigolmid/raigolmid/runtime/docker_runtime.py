"""The Docker implementation of `ContainerRuntime`.

Bodies also run through `compose.py` as generated Compose projects; this class covers
everything the daemon creates directly — anchors, session views, agent containers — plus
image and event access.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import docker
import requests
from docker.errors import APIError, ImageNotFound, NotFound
from docker.models.containers import Container

from .base import (
    BuildResult,
    ContainerInfo,
    ContainerRuntime,
    ContainerSpec,
    ExecResult,
    ImageInfo,
    ImageInUse,
    RemoveBusy,
    RuntimeError_,
    require_bind_sources,
)


def _info(c: Container) -> ContainerInfo:
    attrs = c.attrs
    state = attrs.get("State", {})
    return ContainerInfo(
        id=c.id,
        name=c.name,
        image=(attrs.get("Config", {}) or {}).get("Image", ""),
        status=state.get("Status", "unknown"),
        labels=(attrs.get("Config", {}) or {}).get("Labels") or {},
        pid=state.get("Pid") or None,
        exit_code=state.get("ExitCode"),
        started_at=state.get("StartedAt"),
        image_id=attrs.get("Image", ""),
        ip=_ip(attrs),
    )


def _ip(attrs: dict) -> str | None:
    """The address on the network the container was started on. Docker 29 carries no
    top-level `NetworkSettings.IPAddress`, only one per network; a container sharing another's
    namespace (`container:…`) has none of its own."""
    mode = (attrs.get("HostConfig", {}) or {}).get("NetworkMode") or ""
    networks = (attrs.get("NetworkSettings", {}) or {}).get("Networks") or {}
    return (networks.get(mode) or {}).get("IPAddress") or None


def _image_info(img) -> ImageInfo:
    config = img.attrs.get("Config", {}) or {}
    return ImageInfo(
        id=img.id,
        tags=tuple(img.tags or ()),
        labels=config.get("Labels") or {},
        config=config,
        repo_digests=tuple(img.attrs.get("RepoDigests") or ()),
    )


class DockerRuntime(ContainerRuntime):
    def __init__(self, client: docker.DockerClient | None = None) -> None:
        self._client = client or docker.from_env()

    @property
    def client(self) -> docker.DockerClient:
        return self._client

    # --- containers ------------------------------------------------------------------
    def run(self, spec: ContainerSpec) -> ContainerInfo:
        require_bind_sources(spec)
        kwargs: dict[str, Any] = {
            "name": spec.name,
            "image": spec.image,
            "detach": True,
            "labels": spec.labels,
            "environment": spec.environment,
        }
        if spec.command is not None:
            kwargs["command"] = list(spec.command)
        if spec.entrypoint is not None:
            kwargs["entrypoint"] = list(spec.entrypoint)
        if spec.mounts:
            # A list, not the SDK's dict keyed by source: one directory bound at two targets
            # (the manager's /work and /definitions) is two mounts.
            kwargs["volumes"] = [
                f"{m.source}:{m.target}:{'ro' if m.read_only else 'rw'}"
                for m in spec.mounts
            ]
        if spec.tmpfs:
            kwargs["tmpfs"] = dict(spec.tmpfs)
        if spec.devices:
            kwargs["devices"] = [f"{d}:{d}:rw" for d in spec.devices]
        if spec.ports:
            kwargs["ports"] = {f"{c}/tcp": h for c, h in spec.ports.items()}
        if spec.pid_mode:
            kwargs["pid_mode"] = spec.pid_mode
        if spec.network_mode:
            kwargs["network_mode"] = spec.network_mode
        if spec.network:
            kwargs["network"] = spec.network
            kwargs["networking_config"] = {spec.network: self._client.api.create_endpoint_config(
                aliases=list(spec.aliases) or None)}
        if spec.cap_add:
            kwargs["cap_add"] = list(spec.cap_add)
        if spec.cap_drop:
            kwargs["cap_drop"] = list(spec.cap_drop)
        if spec.privileged:
            kwargs["privileged"] = True
        if spec.read_only:
            kwargs["read_only"] = True
        if spec.security_opt:
            kwargs["security_opt"] = list(spec.security_opt)
        if spec.working_dir:
            kwargs["working_dir"] = spec.working_dir
        if spec.tty:
            kwargs["tty"] = True
        if spec.stdin_open:
            kwargs["stdin_open"] = True
        if spec.user:
            kwargs["user"] = spec.user
        if spec.auto_remove:
            kwargs["auto_remove"] = True
        if spec.restart_policy:
            kwargs["restart_policy"] = {"Name": spec.restart_policy}
        try:
            container = self._client.containers.run(**kwargs)
        except ImageNotFound:
            self.pull(spec.image)
            container = self._client.containers.run(**kwargs)
        except APIError as exc:
            raise RuntimeError_(f"could not start '{spec.name}' from {spec.image}: {exc}") from exc
        container.reload()
        return _info(container)

    def run_to_completion(self, spec: ContainerSpec, timeout: float) -> ExecResult:
        info = self.run(spec)
        container = self._client.containers.get(info.id)
        try:
            try:
                status = container.wait(timeout=timeout)
            except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError):
                # docker-py documents ReadTimeout; over the unix socket it has also surfaced as
                # ConnectionError. The container's own state says which this was.
                container.reload()
                if container.status != "running":
                    raise
                container.kill()
                raise RuntimeError_(
                    f"'{spec.name}' was still running after {timeout}s and was killed; it "
                    f"printed: {container.logs().decode(errors='replace')!r}")
            return ExecResult(int(status["StatusCode"]),
                              container.logs().decode(errors="replace"))
        finally:
            container.remove(force=True)

    def inspect(self, name_or_id: str) -> ContainerInfo | None:
        try:
            c = self._client.containers.get(name_or_id)
        except NotFound:
            return None
        except APIError as exc:
            raise RuntimeError_(f"could not inspect '{name_or_id}': {exc}") from exc
        return _info(c)

    def list(self, label_filter: dict[str, str] | None = None,
             all_states: bool = True) -> list[ContainerInfo]:
        filters: dict[str, Any] = {}
        if label_filter:
            filters["label"] = [f"{k}={v}" for k, v in label_filter.items()]
        try:
            return [_info(c) for c in self._client.containers.list(all=all_states, filters=filters)]
        except APIError as exc:
            raise RuntimeError_(f"could not list containers: {exc}") from exc

    def stop(self, name_or_id: str, timeout: int = 10) -> None:
        try:
            self._client.containers.get(name_or_id).stop(timeout=timeout)
        except NotFound:
            return
        except APIError as exc:
            raise RuntimeError_(f"could not stop '{name_or_id}': {exc}") from exc

    def remove(self, name_or_id: str, force: bool = False) -> None:
        try:
            self._client.containers.get(name_or_id).remove(force=force, v=False)
        except NotFound:
            return
        except APIError as exc:
            message = str(exc)
            if "device or resource busy" in message.lower() or "is in use" in message.lower():
                raise RemoveBusy(
                    f"'{name_or_id}' cannot be removed: {message}. Something still holds "
                    "mounts into it — a session view whose teardown has not run."
                ) from exc
            raise RuntimeError_(f"could not remove '{name_or_id}': {message}") from exc

    def logs(self, name_or_id: str, tail: int = 100) -> str:
        try:
            raw = self._client.containers.get(name_or_id).logs(tail=tail)
        except NotFound:
            return ""
        except APIError as exc:
            raise RuntimeError_(f"could not read logs for '{name_or_id}': {exc}") from exc
        return raw.decode("utf-8", errors="replace")

    def _exec_target(self, name_or_id: str) -> Container:
        try:
            container = self._client.containers.get(name_or_id)
        except NotFound as exc:
            raise RuntimeError_(f"'{name_or_id}' is gone; cannot exec in it") from exc
        if container.labels.get("io.raigolmi.role") == "view":
            raise RuntimeError_(
                "refusing to docker exec into a session view: it would restore the "
                "container's capabilities and miss the view's root switch. "
                "Processes enter a view through its launcher."
            )
        return container

    def spawn(self, name_or_id: str, cmd: list[str], *,
              environment: dict[str, str] | None = None) -> str:
        container = self._exec_target(name_or_id)
        try:
            exec_id = self._client.api.exec_create(container.id, cmd,
                                                   environment=environment)["Id"]
            self._client.api.exec_start(exec_id, detach=True)
        except APIError as exc:
            raise RuntimeError_(f"could not start {cmd} in '{name_or_id}': {exc}") from exc
        return exec_id

    def exec(self, name_or_id: str, cmd: list[str], *,
             environment: dict[str, str] | None = None,
             workdir: str | None = None) -> ExecResult:
        container = self._exec_target(name_or_id)
        try:
            code, output = container.exec_run(cmd, environment=environment, workdir=workdir)
        except APIError as exc:
            # A container that is not running is the common one, and it arrives as a 409.
            # Nothing above this interface knows it is talking to Docker, so the reason
            # comes up as a `RuntimeError_` carrying what was attempted.
            raise RuntimeError_(
                f"could not exec {cmd} in '{name_or_id}': {exc}") from exc
        return ExecResult(exit_code=code, output=output.decode("utf-8", errors="replace"))

    # --- images ----------------------------------------------------------------------
    def image(self, reference: str) -> ImageInfo | None:
        try:
            return _image_info(self._client.images.get(reference))
        except ImageNotFound:
            return None
        except APIError as exc:
            raise RuntimeError_(f"could not inspect image '{reference}': {exc}") from exc

    def pull(self, reference: str) -> ImageInfo:
        try:
            return _image_info(self._client.images.pull(reference))
        except APIError as exc:
            raise RuntimeError_(f"could not pull '{reference}': {exc}") from exc

    def registry_digest(self, reference: str) -> str:
        try:
            return self._client.images.get_registry_data(reference).id
        except APIError as exc:
            raise RuntimeError_(f"the registry did not answer for '{reference}': {exc}") from exc

    def build(self, context: str, dockerfile: str, tag: str,
              target: str | None = None,
              buildargs: dict[str, str] | None = None, pull: bool = False) -> BuildResult:
        """Through the CLI, not the SDK: the SDK's build request carries no builder version,
        so the Engine builds it with the classic builder, which refuses `RUN --mount` and
        every other BuildKit syntax a Dockerfile written today uses. `docker build` is BuildKit, and with
        the default driver it lands in the image store as the SDK's build would."""
        with tempfile.TemporaryDirectory() as scratch:
            iidfile = Path(scratch) / "iid"
            argv = ["docker", "build", "--progress=plain", "--iidfile", str(iidfile),
                    "--file", os.path.join(context, dockerfile), "--tag", tag]
            if target is not None:
                argv += ["--target", target]
            for name, value in (buildargs or {}).items():
                argv += ["--build-arg", f"{name}={value}"]
            if pull:
                argv.append("--pull")
            argv.append(context)
            result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
            if result.returncode != 0:
                return BuildResult(image_id="", log=result.stdout, succeeded=False)
            return BuildResult(image_id=iidfile.read_text().strip(), log=result.stdout,
                               succeeded=True)

    def load(self, archive: Path) -> list[str]:
        """Through the CLI, not the SDK: the SDK's request carries the client's 60 s read
        timeout, and extracting one large layer can be silent for longer than that."""
        result = subprocess.run(["docker", "load", "--input", str(archive)],
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError_(f"could not load {archive}: {result.stderr.strip()}")
        return re.findall(r"(?m)^Loaded image: (\S+)$", result.stdout)

    def remove_image(self, reference: str) -> None:
        try:
            self._client.images.remove(reference, force=False)
        except NotFound:
            # docker-py makes a 404 `ImageNotFound` only for the message "No such image";
            # this Docker says `image "…": not found`, which comes as the bare `NotFound`.
            return
        except APIError as exc:
            if exc.status_code == 409:
                raise ImageInUse(f"'{reference}' is in use: {exc.explanation}") from exc
            raise RuntimeError_(f"could not remove image '{reference}': {exc}") from exc

    def prune_build_cache(self) -> int:
        """Not `all`: that also removes the records a present image shares, which would make
        its next build start from nothing."""
        try:
            return self._client.api.prune_builds()["SpaceReclaimed"]
        except APIError as exc:
            raise RuntimeError_(f"could not prune the build cache: {exc}") from exc

    def list_images(self, label_filter: dict[str, str] | None = None) -> list[ImageInfo]:
        filters: dict[str, Any] = {}
        if label_filter:
            filters["label"] = [f"{k}={v}" for k, v in label_filter.items()]
        try:
            listed = self._client.api.images(filters=filters)
        except APIError as exc:
            raise RuntimeError_(f"could not list images: {exc}") from exc
        # Each listed image is then inspected, and one removed in between — a build
        # replacing its tag, a prune — is gone, not an error: docker-py's `images.list`
        # fails the whole listing on it.
        images = []
        for row in listed:
            try:
                images.append(_image_info(self._client.images.get(row["Id"])))
            except NotFound:
                continue
            except APIError as exc:
                raise RuntimeError_(f"could not inspect listed image {row['Id']}: "
                                    f"{exc}") from exc
        return images

    # --- events ----------------------------------------------------------------------
    def bridge_gateway(self) -> str:
        configs = self._client.networks.get("bridge").attrs["IPAM"]["Config"]
        gateway = next((c["Gateway"] for c in configs if c.get("Gateway")), None)
        if gateway is None:
            raise RuntimeError_(f"Docker's bridge network names no gateway: {configs}")
        return gateway

    def ensure_network(self, name: str, labels: dict[str, str]) -> None:
        try:
            self._client.networks.get(name)
            return
        except NotFound:
            pass
        except APIError as exc:
            raise RuntimeError_(f"could not inspect network {name}: {exc}") from exc
        try:
            self._client.networks.create(name, driver="bridge", labels=labels)
        except APIError as exc:
            raise RuntimeError_(f"could not create network {name}: {exc}") from exc

    def events(self, label_filter: dict[str, str] | None = None,
               since: int | None = None) -> Iterator[dict[str, Any]]:
        filters: dict[str, Any] = {"type": "container"}
        if label_filter:
            filters["label"] = [f"{k}={v}" for k, v in label_filter.items()]
        # The Engine takes `since` as "seconds.nanoseconds", as `docker events --since` sends it.
        stamp = None if since is None else f"{since // 10**9}.{since % 10**9:09d}"
        for raw in self._client.events(since=stamp, filters=filters, decode=False):
            if isinstance(raw, bytes):
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError:
                    continue
            elif isinstance(raw, dict):
                yield raw

    def close(self) -> None:
        self._client.close()
