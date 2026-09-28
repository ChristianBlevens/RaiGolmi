"""Nix closures on the host, each store path copied once out of whichever image first carries it.

A face shows two closures at its `/nix/store`: its own apps and the focused toolbelt's.
Neither can be the face's image — the compositor image is built on the machine — and a mount
taken from a running toolbelt container would hold that container's filesystem, so it could
not be removed until the face let go, on every path that ends a view. Copied out, a closure belongs to no container: `facemounts.py` binds its store paths
into the face one by one.

A store path's name is its content's hash, so one shared store serves every image, and a
toolbelt that differs by one package copies one path. An image is a list of the paths it
holds, keyed by image id rather than reference: a Nixery tag can be pulled again to other
content while a view keeps running the old one, and the face must hold the store paths the
view's server actually names.

    <closures>/store/<path>            every store path copied so far
    <closures>/<image id>/paths        the image's store paths, one name per line
    <closures>/<image id>/bin          the image's /bin, whose entries point into the store

Every write lands under a temporary name and is renamed into place once complete, so a name
in the store is always a whole store path, and an image directory under its final name always
lists paths that are all present.
"""
from __future__ import annotations

import os
import threading
import shutil
import stat
from pathlib import Path

from . import labels, naming
from .events import EventLog
from .paths import Paths
from .runtime.base import ContainerRuntime, ContainerSpec, Mount
from .toolbelts import pull_from_nixery

STORE_OUT = "/out/store"
IMAGE_OUT = "/out/image"
# A closure is hundreds of megabytes on a first copy.
TIMEOUT = 600.0
# In the image's own shell. A path already in the store is not copied again; one whose copy
# died is found by its temporary name and removed, after its read-only modes are opened up.
COPY_SCRIPT = f"""set -eu
incoming={STORE_OUT}/.incoming
mkdir -p "$incoming"
for p in /nix/store/*; do
  n=${{p##*/}}
  echo "$n" >> {IMAGE_OUT}/paths
  # -L too: a store path can be a link into /nix/store, which does not resolve on the host.
  if [ -e "{STORE_OUT}/$n" ] || [ -L "{STORE_OUT}/$n" ]; then continue; fi
  if [ -L "$incoming/$n" ]; then rm -f "$incoming/$n"
  elif [ -e "$incoming/$n" ]; then chmod -R u+w "$incoming/$n"; rm -rf "$incoming/$n"; fi
  cp -a "$p" "$incoming/$n"
  # Nix's own store is read-only, its directories too (Nixery's images happen not to be),
  # and a directory moves to another parent only with write on itself, for its `..`: given
  # it for the move and its mode put back.
  if [ -d "$incoming/$n" ] && [ ! -L "$incoming/$n" ]; then
    mode=$(stat -c %a "$incoming/$n")
    chmod u+w "$incoming/$n"
    mv "$incoming/$n" "{STORE_OUT}/$n"
    chmod "$mode" "{STORE_OUT}/$n"
  else
    mv "$incoming/$n" "{STORE_OUT}/$n"
  fi
done
cp -a /bin {IMAGE_OUT}/bin
"""


class ClosureError(RuntimeError):
    """A closure could not be copied out of its image."""


class Closures:
    def __init__(self, runtime: ContainerRuntime, paths: Paths, events: EventLog,
                 epoch: int) -> None:
        self.runtime = runtime
        self.paths = paths
        self.events = events
        self.epoch = epoch
        # A copy is one container name, one `.partial` per image and one shared store, and a
        # collection may not run while a copy is about to list its paths: one at a time.
        self._lock = threading.Lock()

    @property
    def store(self) -> Path:
        return self.paths.closures / "store"

    def of_image(self, reference: str) -> tuple[str, Path]:
        """The id of the image `reference` names now, pulled if it is not here, and its
        closure."""
        info = self.runtime.image(reference) or pull_from_nixery(self.runtime, reference)
        return info.id, self.of_image_id(info.id)

    @staticmethod
    def store_paths(closure: Path) -> list[str]:
        return (closure / "paths").read_text(encoding="utf-8").split()

    def of_image_id(self, image_id: str) -> Path:
        final = self.paths.closures / image_id.removeprefix("sha256:")
        if final.is_dir():
            return final
        with self._lock:
            return final if final.is_dir() else self._copy(image_id, final)

    def _copy(self, image_id: str, final: Path) -> Path:
        partial = final.with_name(final.name + ".partial")
        if partial.exists():
            # Residue of a copy that did not finish; its /bin copy carries the store's modes.
            _remove_store_copy(partial)
        partial.mkdir(parents=True)
        self.store.mkdir(exist_ok=True)
        name = naming.closure_copy()
        if self.runtime.inspect(name) is not None:
            self.runtime.remove(name, force=True)
        result = self.runtime.run_to_completion(ContainerSpec(
            name=name,
            image=image_id,
            command=("bash", "-c", COPY_SCRIPT),
            labels={labels.MANAGED: "true", labels.ROLE: str(labels.Role.CLOSURE_COPY),
                    labels.EPOCH: str(self.epoch)},
            mounts=(Mount(source=str(self.store), target=STORE_OUT),
                    Mount(source=str(partial), target=IMAGE_OUT)),
            user=f"{os.getuid()}:{os.getgid()}",
        ), TIMEOUT)
        if result.exit_code != 0:
            raise ClosureError(f"copying the closure of {image_id} exited {result.exit_code}: "
                               f"{result.output.strip()}")
        if not (partial / "paths").is_file() or not (partial / "bin").exists():
            raise ClosureError(f"the copy of {image_id} exited 0 without listing its store "
                               f"paths and its /bin; the image has no /nix/store or no /bin")
        missing = [p for p in self.store_paths(partial)
                   if not os.path.lexists(self.store / p)]
        if missing:
            raise ClosureError(f"the copy of {image_id} exited 0 and {len(missing)} of its "
                               f"store paths are not in {self.store}: {missing[:3]}")
        partial.rename(final)
        self.events.emit("closure.copied", image=image_id, path=str(final),
                         store_paths=len(self.store_paths(final)))
        return final


    def collect(self, keep: set[str]) -> tuple[int, int]:
        """Removes each image's closure whose id is not in `keep`, then each store path no
        kept closure lists. Returns how many of each went.

        Never while a copy runs: a store path it is about to list would go.
        """
        with self._lock:
            return self._collect(keep)

    def _collect(self, keep: set[str]) -> tuple[int, int]:
        if not self.paths.closures.is_dir():
            return 0, 0
        kept_ids = {image_id.removeprefix("sha256:") for image_id in keep}
        closures = 0
        listed: set[str] = set()
        for directory in self.paths.closures.iterdir():
            if directory == self.store or directory.name.endswith(".partial"):
                continue
            if directory.name in kept_ids:
                listed.update(self.store_paths(directory))
                continue
            _remove_store_copy(directory)
            closures += 1
        paths = 0
        if self.store.is_dir():
            for path in self.store.iterdir():
                if path.name in listed or path.name == ".incoming":
                    continue
                if path.is_dir() and not path.is_symlink():
                    _remove_store_copy(path)
                else:
                    path.unlink()
                paths += 1
        return closures, paths


def _remove_store_copy(root: Path) -> None:
    for directory, _, _ in os.walk(root):
        os.chmod(directory, os.stat(directory).st_mode | stat.S_IRWXU)
    shutil.rmtree(root)
