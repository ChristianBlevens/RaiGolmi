"""The boot prebuild and a tab opening ask `hostimages.ensure` for the same image at once;
the second attaches to the first's build rather than running its own, and shares its
failure when it fails."""
from __future__ import annotations

import threading
import time
from pathlib import Path


from raigolmid import hostimages
from raigolmid.hostimages import HostImage, HostImageError
from raigolmid.runtime.base import BuildResult, ImageInfo


class _Store:
    """The two calls `ensure` makes, with the image store's one property that matters
    here: a tag is absent until a build of it finishes."""

    def __init__(self, succeeds: bool) -> None:
        self.succeeds = succeeds
        self.builds: list[str] = []
        self.built: set[str] = set()
        self.entered = threading.Event()
        self.release = threading.Event()

    def image(self, tag: str):
        return ImageInfo(id=f"sha256:{tag}", tags=(tag,), labels={}, config={}) \
            if tag in self.built else None

    def list_images(self, label_filter=None) -> list[ImageInfo]:
        assert label_filter is None
        return [ImageInfo(id=f"sha256:{t}", tags=(t,), labels={}, config={})
                for t in sorted(self.built)]

    def remove_image(self, reference: str) -> None:
        self.built.discard(reference)

    def build(self, context: str, dockerfile: str, tag: str, target=None,
              buildargs=None) -> BuildResult:
        self.builds.append(tag)
        self.entered.set()
        assert self.release.wait(10)
        if not self.succeeds:
            return BuildResult(image_id="", log="dnf: no network", succeeded=False)
        self.built.add(tag)
        return BuildResult(image_id="sha256:0", log="", succeeded=True)


def _image(tmp_path: Path) -> HostImage:
    (tmp_path / "Containerfile").write_text("FROM fedora:44\n")
    return HostImage("claude", tmp_path, tmp_path / "Containerfile")


def _race(store: _Store, image: HostImage) -> list[object]:
    """Two callers, the second arriving while the first's build is running."""
    results: list[object] = [None, None]

    def call(i: int) -> None:
        try:
            results[i] = hostimages.ensure(store, image)
        except HostImageError as exc:
            results[i] = exc

    first = threading.Thread(target=call, args=(0,))
    first.start()
    assert store.entered.wait(10)
    second = threading.Thread(target=call, args=(1,))
    second.start()
    tag = image.tag()
    deadline = time.monotonic() + 10
    while hostimages._BUILDS.state().get(tag, {}).get("waiters") != 1:
        assert time.monotonic() < deadline, "the second caller never attached"
        time.sleep(0.01)
    store.release.set()
    first.join(10)
    second.join(10)
    return results


def test_two_callers_share_one_build(tmp_path):
    store = _Store(succeeds=True)
    image = _image(tmp_path)
    results = _race(store, image)
    assert store.builds == [image.tag()]
    assert results == [image.tag(), image.tag()]


def test_a_failed_build_fails_every_caller_with_its_log(tmp_path):
    store = _Store(succeeds=False)
    results = _race(store, _image(tmp_path))
    assert len(store.builds) == 1
    for r in results:
        assert isinstance(r, HostImageError)
        assert "dnf: no network" in str(r)
