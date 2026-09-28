"""A closure copied out of its image (`closures.py`): complete under its final name or absent."""
from __future__ import annotations

import os
import stat

import pytest

from raigolmid import closures as closures_module
from raigolmid import naming
from raigolmid.closures import ClosureError, Closures
from raigolmid.events import EventLog
from raigolmid.paths import Paths
from raigolmid.runtime.base import ExecResult
from tests.fakeruntime import FakeRuntime

from tests.fakes import ClosureCopies

IMAGE = "nixery.dev/shell/python3/neovim"


@pytest.fixture()
def rig(tmp_path):
    paths = Paths(state=tmp_path / "state", data=tmp_path / "data",
                  config=tmp_path / "config", runtime=tmp_path / "run")
    runtime = FakeRuntime()
    copies = ClosureCopies(runtime)
    runtime.one_shot[naming.closure_copy()] = copies
    return runtime, copies, Closures(runtime, paths, EventLog(tmp_path / "events.jsonl"), 1)


def test_a_closure_is_copied_once_per_image_id(rig):
    runtime, copies, closures = rig
    image_id, path = closures.of_image(IMAGE)
    assert image_id == runtime.image(IMAGE).id
    assert (path / "bin" / "neovim").is_symlink()
    assert copies.store_name("neovim") in closures.store_paths(path)
    assert os.readlink(path / "bin" / "neovim").startswith("/nix/store/")
    assert closures.of_image(IMAGE) == (image_id, path)
    assert copies.copies == [image_id]


def test_a_failed_copy_leaves_nothing_under_the_final_name(rig):
    runtime, _, closures = rig
    runtime.one_shot[naming.closure_copy()] = lambda spec: ExecResult(1, "cp: No space left")
    with pytest.raises(ClosureError, match="No space left"):
        closures.of_image(IMAGE)
    image_id = runtime.image(IMAGE).id
    assert not (closures.paths.closures / image_id.removeprefix("sha256:")).exists()


def test_the_residue_of_a_dead_copy_is_removed_though_the_store_made_it_read_only(rig):
    runtime, copies, closures = rig
    runtime.pull(IMAGE)
    image_id = runtime.image(IMAGE).id
    partial = closures.paths.closures / (image_id.removeprefix("sha256:") + ".partial")
    stuck = partial / "bin" / "half"
    stuck.mkdir(parents=True)
    (stuck / "file").write_text("")
    os.chmod(stuck, stat.S_IRUSR | stat.S_IXUSR)
    os.chmod(partial / "bin", stat.S_IRUSR | stat.S_IXUSR)
    _, path = closures.of_image(IMAGE)
    assert not partial.exists() and not (path / "bin" / "half").exists()


def test_a_listed_path_missing_from_the_store_is_refused(rig):
    runtime, copies, closures = rig

    def loses_a_path(spec):
        result = copies(spec)
        image = {m.target: m for m in spec.mounts}[closures_module.IMAGE_OUT].source
        with open(os.path.join(image, "paths"), "a") as f:
            f.write("0000-never-copied\n")
        return result

    runtime.one_shot[naming.closure_copy()] = loses_a_path
    with pytest.raises(ClosureError, match="0000-never-copied"):
        closures.of_image(IMAGE)


def test_a_bind_mount_of_a_source_that_is_not_there_is_refused(tmp_path):
    """Docker would create it as an empty root-owned directory and mount that: the container
    gets nothing, and the daemon's user cannot remove what is left."""
    from raigolmid.runtime.base import ContainerSpec, Mount, RuntimeError_
    with pytest.raises(RuntimeError_, match="do not exist"):
        FakeRuntime().run(ContainerSpec(name="x", image="busybox", mounts=(
            Mount(source=str(tmp_path / "absent" / "paths"), target="/paths"),)))


def test_a_second_ask_for_an_image_being_copied_waits_for_that_copy(rig):
    """The user's face's apps and a view's toolbelt can ask at once: a second copy under the one
    container name would force-remove the first and write into its `.partial`."""
    import threading
    runtime, copies, closures = rig
    image_id = runtime.pull(IMAGE).id
    started, release = threading.Event(), threading.Event()

    def slow(spec):
        started.set()
        release.wait(5)
        return copies(spec)

    runtime.one_shot[naming.closure_copy()] = slow
    first = threading.Thread(target=closures.of_image_id, args=(image_id,))
    first.start()
    assert started.wait(5)
    second: list = []
    other = threading.Thread(target=lambda: second.append(closures.of_image_id(image_id)))
    other.start()
    other.join(0.3)
    assert other.is_alive(), "the second ask did not wait for the copy under way"
    release.set()
    first.join(5)
    other.join(5)
    assert copies.copies == [image_id]
    assert second == [closures.paths.closures / image_id.removeprefix("sha256:")]
