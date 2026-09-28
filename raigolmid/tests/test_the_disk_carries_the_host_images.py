"""The disk carries the host's own images as one archive, and a first start loads it rather than
building them (`hostimages`, `host/ci/bake-host-images.sh`).

The tags inside the archive are computed at disk build by the same `HostImage.tag` the daemon
calls, so a tag the archive carries is recognised with no build; one it does not — an edited
face compositor — is still built.
"""
from __future__ import annotations

import io
import json
import re
import tarfile
from pathlib import Path


from raigolmid import hostimages
from raigolmid.hostimages import HostImage
from tests.fakeruntime import FakeRuntime

ROOT = Path(__file__).resolve().parents[2]


def _archive(path: Path, tags: list[str]) -> Path:
    manifest = json.dumps([{"Config": "c.json", "RepoTags": tags, "Layers": []}]).encode()
    with tarfile.open(path, "w") as tar:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))
    return path


def _image(root: Path, name: str) -> HostImage:
    context = root / name
    context.mkdir()
    (context / "Containerfile").write_text("FROM docker.io/library/fedora:42\n")
    return HostImage(name, context, context / "Containerfile")


def test_a_carried_image_is_loaded_not_built(tmp_path, monkeypatch):
    image = _image(tmp_path, "selector")
    monkeypatch.setenv(hostimages.ARCHIVE_ENV,
                       str(_archive(tmp_path / "a.tar", [f"docker.io/{image.tag()}"])))
    runtime = FakeRuntime()
    assert hostimages.ensure(runtime, image) == image.tag()
    assert runtime.build_count == 0


def test_the_archive_is_read_once(tmp_path, monkeypatch):
    first, second = _image(tmp_path, "selector"), _image(tmp_path, "host-control")
    monkeypatch.setenv(hostimages.ARCHIVE_ENV, str(_archive(tmp_path / "a.tar", [])))
    runtime = FakeRuntime()
    loads = []
    real = runtime.load
    runtime.load = lambda archive: loads.append(archive) or real(archive)
    hostimages.ensure(runtime, first)
    hostimages.ensure(runtime, second)
    assert len(loads) == 1


def test_a_podman_local_name_is_not_the_tag_asked_for(tmp_path):
    """Why the bake tags `docker.io/…`: podman files an unqualified name under `localhost/`,
    and Docker loads it under that name, which no `raigolmi/…` lookup finds."""
    runtime = FakeRuntime()
    loaded = runtime.load(_archive(tmp_path / "a.tar", ["localhost/raigolmi/selector:0",
                                                         "docker.io/raigolmi/control:0"]))
    assert loaded == ["localhost/raigolmi/selector:0", "raigolmi/control:0"]
    assert runtime.image("raigolmi/selector:0") is None


def test_the_disk_carries_the_machines_images_and_no_face_compositor():
    """The product ships no face, so no compositor is baked; a face's is built on first use."""
    images = hostimages.shipped()
    assert {"selector", "host-control", "notify", "claude"} <= {i.name for i in images}
    assert not any(i.context.parent.name == "_compositors" for i in images)


def _copies(containerfile: Path) -> dict[str, str]:
    text = re.sub(r"\\\n", " ", containerfile.read_text(encoding="utf-8"))
    return {w[1]: w[-1] for w in (line.split() for line in text.splitlines())
            if w and w[0] == "COPY"}


def test_the_disk_puts_the_archive_where_hostimages_reads_it():
    assert _copies(ROOT / "host" / "Containerfile.images").get("host-images.tar") == str(
        hostimages.DEFAULT_ARCHIVE)
