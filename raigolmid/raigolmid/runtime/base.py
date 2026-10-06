"""The runtime interface every container operation goes through."""
from __future__ import annotations

import abc
from collections.abc import Iterator
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def require_bind_sources(spec: "ContainerSpec") -> None:
    """Docker creates a missing bind source as an empty root-owned directory and mounts that,
    so a mount of a file or directory that is not there yet succeeds and hands the container
    nothing — and leaves a directory the daemon's user cannot remove. Refused instead."""
    missing = [m.source for m in spec.mounts
               if not m.volume and not os.path.lexists(m.source)]
    if missing:
        raise RuntimeError_(f"'{spec.name}' would bind-mount {missing}, which do not exist")


class RuntimeError_(Exception):
    """A container operation failed. Always carries what was attempted, because the
    caller's next move is usually to surface it to the user verbatim."""


class ImageInUse(RuntimeError_):
    """An image removal Docker refuses because a container, running or not, was created
    from it. Not a failure for a caller tidying old images: that one is still wanted."""


class RemoveBusy(RuntimeError_):
    """Removal refused because something still holds mounts into the container. This is
    the case reconciliation marks an instance degraded for; it is a distinct type because it is
    the one removal failure with a designed response rather than an error message."""


@dataclass(frozen=True, slots=True)
class Mount:
    source: str
    target: str
    read_only: bool = False
    # `source` names a Docker volume rather than a host path. An empty one is seeded from the
    # image's own files at `target` on first mount, which is how the nix builder's store
    # outlives it and still holds nix (`flakes.py`).
    volume: bool = False


@dataclass(frozen=True, slots=True)
class ContainerSpec:
    name: str
    image: str
    command: tuple[str, ...] | None = None
    entrypoint: tuple[str, ...] | None = None
    labels: dict[str, str] = field(default_factory=dict)
    environment: dict[str, str] = field(default_factory=dict)
    mounts: tuple[Mount, ...] = ()
    ports: dict[int, int] = field(default_factory=dict)   # container port -> host port
    pid_mode: str | None = None          # "container:<id>"
    network_mode: str | None = None      # "container:<id>"
    # A user-defined network the container joins (`ensure_network` first), and the names it
    # answers to there. Excludes `network_mode`: a container joins another's or its own.
    network: str | None = None
    aliases: tuple[str, ...] = ()
    cap_add: tuple[str, ...] = ()
    cap_drop: tuple[str, ...] = ()
    privileged: bool = False
    read_only: bool = False
    security_opt: tuple[str, ...] = ()
    working_dir: str | None = None
    # "<uid>:<gid>". The face compositor is the caller that needs it: the host's Wayland
    # socket belongs to the desktop user, so the client has to *be* that uid, and it
    # takes that identity from the host rather than from anything inside the image.
    user: str | None = None
    auto_remove: bool = False
    tmpfs: dict[str, str] = field(default_factory=dict)
    # Host device nodes passed through at the same path, read and written.
    devices: tuple[str, ...] = ()
    restart_policy: str | None = None
    # An interactive process reached with `docker attach` — the AI terminal's tabs — needs
    # a terminal and an open stdin, or it sees neither and runs as a batch job.
    tty: bool = False
    stdin_open: bool = False


@dataclass(frozen=True, slots=True)
class ContainerInfo:
    id: str
    name: str
    image: str
    status: str           # running | exited | created | dead | paused | restarting
    labels: dict[str, str]
    pid: int | None = None
    exit_code: int | None = None
    started_at: str | None = None
    # The image the container was created from, by content. `image` is the reference, which
    # a later pull can move to other content while this container keeps running the old one.
    image_id: str = ""
    # Its address on the default bridge; None for one that joins another's network.
    ip: str | None = None

    @property
    def running(self) -> bool:
        return self.status == "running"


@dataclass(frozen=True, slots=True)
class DiskUsage:
    """What the runtime holds on disk, in bytes: its images (each layer once), their build
    cache, and the containers' own writable layers — and of the first two, what nothing uses."""
    images: int
    images_unused: int
    build_cache: int
    build_cache_unused: int
    containers: int


@dataclass(frozen=True, slots=True)
class MemoryUse:
    """A running container's memory now: its working memory (anonymous pages — what it has
    allocated, never the page cache it reads through), and how many of its processes the
    kernel has killed for want of memory since it started."""
    working: int
    oom_kills: int


@dataclass(frozen=True, slots=True)
class ImageInfo:
    id: str
    tags: tuple[str, ...]
    labels: dict[str, str]
    config: dict[str, Any]
    # `repo@sha256:…` for each registry it was pulled from; none for an image built here.
    repo_digests: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExecResult:
    exit_code: int
    output: str


@dataclass(frozen=True, slots=True)
class BuildResult:
    image_id: str
    log: str
    succeeded: bool


class ContainerRuntime(abc.ABC):
    """Nothing above this interface knows it is talking to Docker."""

    # --- containers ------------------------------------------------------------------
    @abc.abstractmethod
    def run(self, spec: ContainerSpec) -> ContainerInfo: ...

    @abc.abstractmethod
    def run_to_completion(self, spec: ContainerSpec, timeout: float) -> ExecResult:
        """Run a container until it exits, then remove it: its exit code and everything it
        printed. A container still running at `timeout` is killed and that is raised, with
        its output — never reported as an exit it did not make."""

    @abc.abstractmethod
    def inspect(self, name_or_id: str) -> ContainerInfo | None: ...

    @abc.abstractmethod
    def list(self, label_filter: dict[str, str] | None = None,
             all_states: bool = True) -> list[ContainerInfo]: ...

    @abc.abstractmethod
    def stop(self, name_or_id: str, timeout: int = 10) -> None: ...

    @abc.abstractmethod
    def remove(self, name_or_id: str, force: bool = False) -> None:
        """Raises `RemoveBusy` when something still holds mounts into the container.
        Never forces on its own: forcing is how a leaked storage layer happens."""


    @abc.abstractmethod
    def logs(self, name_or_id: str, tail: int = 100) -> str: ...

    @abc.abstractmethod
    def processes(self, name_or_id: str) -> str:
        """A running container's processes, one per line with its pid, parent, age and
        command, read from the host (`docker top`): nothing runs inside the container, so a
        view or a distroless one answers too. Raises `RuntimeError_` for one that is gone or
        not running."""

    def diagnostic_log(self, name_or_id: str, tail: int = 100) -> str:
        """The log for an error that explains a container's failure. Docker refuses logs for
        a container that is dead or marked for removal, and that refusal is the answer — so
        it is returned, never raised past the error it was read for."""
        try:
            return self.logs(name_or_id, tail=tail)
        except RuntimeError_ as exc:
            return f"(docker refused this container's logs: {exc})\n"

    @abc.abstractmethod
    def exec(self, name_or_id: str, cmd: list[str], *,
             environment: dict[str, str] | None = None,
             workdir: str | None = None) -> ExecResult:
        """For **bodies only**. Never for session views: `docker exec` restores the
        container's configured capabilities and joins its mount namespace without the
        view's root switch. Processes enter a view through its launcher."""

    @abc.abstractmethod
    def spawn(self, name_or_id: str, cmd: list[str], *,
              environment: dict[str, str] | None = None) -> str:
        """Start `cmd` in a running container and return without waiting: its exec id. For
        a face's apps, which live as long as the face does. Refuses a view, as `exec` does."""

    # --- images ----------------------------------------------------------------------
    @abc.abstractmethod
    def image(self, reference: str) -> ImageInfo | None: ...

    @abc.abstractmethod
    def pull(self, reference: str) -> ImageInfo: ...

    @abc.abstractmethod
    def registry_digest(self, reference: str) -> str:
        """The manifest digest the registry names `reference` by now. Raises `RuntimeError_`
        when the registry cannot answer."""

    @abc.abstractmethod
    def build(self, context: str, dockerfile: str, tag: str,
              target: str | None = None,
              buildargs: dict[str, str] | None = None, pull: bool = False) -> BuildResult:
        """`pull` fetches each `FROM` image's current version before building."""

    @abc.abstractmethod
    def load(self, archive: Path) -> list[str]:
        """Load a saved image archive (`docker load`); the references it named."""

    @abc.abstractmethod
    def list_images(self, label_filter: dict[str, str] | None = None) -> list[ImageInfo]: ...

    @abc.abstractmethod
    def remove_image(self, reference: str) -> None:
        """Untag `reference`, deleting the image when that was its last tag. Raises
        `ImageInUse` while any container was created from it; an absent one is gone already."""

    @abc.abstractmethod
    def prune_build_cache(self) -> int:
        """Removes the build cache no image holds — what a removed image's builds leave —
        and keeps what a present image was built from, which is what makes its next build a
        cache hit. The bytes reclaimed."""

    @abc.abstractmethod
    def memory(self, container_id: str) -> MemoryUse:
        """A running container's memory (`memory.py`)."""

    @abc.abstractmethod
    def disk_usage(self) -> DiskUsage:
        """What the runtime holds on disk (`disk.py`)."""

    # --- network ---------------------------------------------------------------------
    @abc.abstractmethod
    def bridge_gateway(self) -> str:
        """The host's address on the network a container joins by default: where a
        container reaches a service the host serves (the credential proxy, `credproxy.py`)."""

    @abc.abstractmethod
    def ensure_network(self, name: str, labels: dict[str, str]) -> None:
        """A bridge network `name` exists, with Docker's DNS for its members' aliases: made
        with `labels` when absent, left as it is when present."""

    # --- events ----------------------------------------------------------------------
    @abc.abstractmethod
    def events(self, label_filter: dict[str, str] | None = None,
               since: int | None = None) -> Iterator[dict[str, Any]]:
        """Container-runtime events, each with its `timeNano`: from `since` (ns since the
        epoch) where given, else from now. The daemon watches these to notice a body that
        restarted on its own, which triggers a restart."""

    @abc.abstractmethod
    def close(self) -> None: ...
