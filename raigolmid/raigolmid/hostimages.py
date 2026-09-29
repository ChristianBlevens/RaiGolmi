"""The machine's own images — its surfaces, the agent, the face mount, the door — and the
build of a face's compositor.

Each is tagged by a digest over its sources — the body's rule — so a current image is
recognised without a build, and a `bootc upgrade` that changes the sources gets a new tag.

The disk carries all of them as one archive under `/usr` (`ARCHIVE`), built from the very tree
the disk ships (`python -m raigolmid.hostimages` names them; `host/ci/build-local.sh`), so the
tags inside it are the tags asked for here. A missing tag loads the archive once, and only a tag
it does not carry is built: every face compositor, since the product ships no face. Not baked
into Docker's storage:
that is `/var`, which bootc seeds only at install, so every upgrade after the first would run
images older than the daemon; `/usr` is replaced wholesale.

A build needs the network (`fedora:44` and dnf), as a body's does.
"""
from __future__ import annotations

import hashlib
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import layerfiles, naming
from .queues import BuildLock, BuildOutcome
from .runtime.base import ContainerRuntime, ImageInUse

logger = logging.getLogger(__name__)

# One per process and keyed by tag: the boot prebuild and the first tab ask for the same
# image at once, and a tag names its content, so a second build of it is only waste.
_BUILDS = BuildLock()

# The host image copies `ui/` and `raigolmid/raigolmid/` here in the repository's own layout,
# so one Containerfile builds identically from a checkout and from the disk.
SOURCE_ENV = "RAIGOLMID_IMAGE_SOURCE_PATH"
DEFAULT_SOURCE = Path("/usr/share/raigolmi/src")

# The archive `host/ci/build-local.sh` bakes.
ARCHIVE_ENV = "RAIGOLMID_HOST_IMAGE_ARCHIVE"
DEFAULT_ARCHIVE = Path("/usr/share/raigolmi/host-images.tar")

class HostImageError(RuntimeError):
    pass


@dataclass(frozen=True)
class HostImage:
    name: str
    context: Path
    containerfile: Path

    def digest(self) -> str:
        """The Containerfile and every file it copies, by path and bytes: a change to what
        goes into the image is a new image, and nothing else is. Three of these build from
        the whole source tree, so hashing the context made any edit anywhere — a doc, a
        test — rebuild four images."""
        if not self.context.is_dir():
            raise HostImageError(
                f"the build context for {self.name} is {self.context}, which does not exist")
        h = hashlib.sha256()
        h.update(b"raigolmi-host-image-v2\0")
        h.update(self.name.encode() + b"\0")
        h.update(self.containerfile.read_bytes() + b"\0")
        for f in self._copied():
            h.update(str(f.relative_to(self.context)).encode() + b"\0")
            h.update(f.read_bytes() + b"\0")
        return "sha256:" + h.hexdigest()

    def _copied(self) -> list[Path]:
        try:
            return layerfiles.copied(self.containerfile, self.context)
        except layerfiles.LayerFilesError as exc:
            raise HostImageError(str(exc)) from exc

    def tag(self) -> str:
        return naming.host_image_tag(self.name, self.digest())


def source_root() -> Path:
    return Path(os.environ.get(SOURCE_ENV) or DEFAULT_SOURCE)


def archive_path() -> Path:
    return Path(os.environ.get(ARCHIVE_ENV) or DEFAULT_ARCHIVE)


def selector() -> HostImage:
    root = source_root()
    return HostImage("selector", root, root / "ui" / "selector_native" / "Containerfile")


def control() -> HostImage:
    root = source_root()
    return HostImage("host-control", root, root / "ui" / "host_control" / "Containerfile")


def notify() -> HostImage:
    root = source_root()
    return HostImage("notify", root, root / "ui" / "notify_popup" / "Containerfile")


def catalog() -> HostImage:
    root = source_root()
    return HostImage("catalog", root, root / "ui" / "catalog" / "Containerfile")


def agent() -> HostImage:
    """The agent container. The host carries no agent CLI, so the image is
    built here like the host's own surfaces; its Dockerfile copies `raigolmid/` and `ui/`, so
    the context is the whole source root."""
    root = source_root()
    return HostImage("claude", root, root / "agents" / "claude" / "Dockerfile")


def face_mount() -> HostImage:
    """The helper that mounts a face's `/body`, `/work` and `/nix/store` (`facemounts.py`)."""
    context = source_root() / "host" / "face-mount"
    return HostImage("face-mount", context, context / "Containerfile")


def door() -> HostImage:
    """The door that forwards the active sandbox's ports (`doors.py`)."""
    context = source_root() / "host" / "door"
    return HostImage("door", context, context / "Containerfile")


def gh() -> HostImage:
    """GitHub's CLI, which the machine's GitHub sign-in runs (`rai registry-token --login`)."""
    context = source_root() / "host" / "gh"
    return HostImage("gh", context, context / "Containerfile")


def face_compositor(faces_root: Path, compositor: str) -> HostImage:
    """`faces/_compositors/<name>/` sits beside the faces and is not one: a face that names
    it brings it (a download places it there), and an edit there rebuilds the image."""
    context = faces_root / "_compositors" / compositor
    return HostImage(f"face-{compositor}", context, context / "Containerfile")


def shipped() -> list[HostImage]:
    """Every image the disk carries: the machine's own. A face's compositor is the face's,
    built on the machine when it first starts (`face_compositor`)."""
    return [selector(), control(), notify(), catalog(), agent(), face_mount(), door(), gh()]


# Loaded at most once per process: the archive holds every image, and a second load of it
# rewrites nothing but costs the whole read again.
_LOAD = threading.Lock()
_LOADED: set[Path] = set()


def _load_archive(runtime: ContainerRuntime) -> bool:
    """Load the disk's archive if there is one and it has not been loaded. Whether one exists."""
    archive = archive_path()
    if not archive.is_file():
        return False
    with _LOAD:
        if archive not in _LOADED:
            logger.info("loading the host images from %s", archive)
            started = time.monotonic()
            loaded = runtime.load(archive)
            _LOADED.add(archive)
            logger.info("loaded %s from %s in %.0fs", ", ".join(loaded) or "nothing",
                        archive, time.monotonic() - started)
    return True


def present(runtime: ContainerRuntime, image: HostImage) -> bool:
    """Whether this image is already on the machine, so a caller can tell a build that is
    about to happen from one that is not — which is the difference between a first start and
    every other one."""
    return runtime.image(image.tag()) is not None


def ensure(runtime: ContainerRuntime, image: HostImage) -> str:
    """The image's tag, built first if it is not present."""
    tag = image.tag()
    if runtime.image(tag) is not None:
        return tag
    if _load_archive(runtime):
        if runtime.image(tag) is not None:
            _drop_older(runtime, tag)
            return tag
        if image.context.is_relative_to(DEFAULT_SOURCE):
            # Its sources are the disk's own, so the archive was built from them: this is a
            # disk whose archive and tree disagree, and the build below only hides it.
            logger.warning("%s carries no %s although its sources are the disk's own; "
                           "building it", archive_path(), tag)
    if not image.containerfile.is_file():
        raise HostImageError(
            f"{image.containerfile} does not exist, so {image.name} cannot be built")

    def work() -> BuildOutcome:
        # A first boot builds every image here, minutes with nothing else on the journal.
        logger.info("building %s from %s", tag, image.context)
        started = time.monotonic()
        result = runtime.build(context=str(image.context),
                               dockerfile=str(image.containerfile.relative_to(image.context)),
                               tag=tag)
        return BuildOutcome(digest=tag, succeeded=result.succeeded, image=tag,
                            log=result.log, duration=time.monotonic() - started)

    outcome = _BUILDS.build(tag, tag, work)
    if not outcome.succeeded:
        raise HostImageError(
            f"building {tag} from {image.context} failed:\n{outcome.log[-4000:]}")
    logger.info("%s %s, a %.0fs build", "waited on" if outcome.coalesced else "built",
                tag, outcome.duration)
    _drop_older(runtime, tag)
    return tag


def _drop_older(runtime: ContainerRuntime, tag: str) -> None:
    """Every other tag of this image's repository, once this one is here: each change to the
    sources makes a new one, and nothing else ever removes the last (gigabytes within an
    hour of edits). One a container was created from is still wanted and stays;
    Docker refuses it, and that refusal is the answer."""
    repository = tag.rsplit(":", 1)[0]
    for info in runtime.list_images():
        for old in info.tags:
            if old.rsplit(":", 1)[0] != repository or old == tag:
                continue
            try:
                runtime.remove_image(old)
                logger.info("removed %s, replaced by %s", old, tag)
            except ImageInUse:
                logger.info("kept %s: a container was created from it", old)


def main() -> int:
    """`tag<TAB>context<TAB>containerfile` per shipped image, for the disk build to bake."""
    for image in shipped():
        sys.stdout.write(f"{image.tag()}\t{image.context}\t{image.containerfile}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
