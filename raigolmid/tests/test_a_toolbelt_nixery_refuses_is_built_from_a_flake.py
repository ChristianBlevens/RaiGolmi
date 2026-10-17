"""A toolbelt Nixery refuses — an unfree package is the case — is built on
the machine from a generated flake, pinned to the nixpkgs commit it was built from, and its
image is the toolbelt's from then on. A face's apps Nixery refuses are built so too."""
from __future__ import annotations

import io
import json
import re
import tarfile
from pathlib import Path

import pytest

from raigolmid import flakes, naming
from raigolmid.definitions import load_toolbelt
from raigolmid.faces import Faces
from raigolmid.runtime.base import ExecResult

from tests.harness import Harness

REV = "f9bce96a417afbb9c64725f8727e42efafeafc21"


@pytest.fixture
def h(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


def _nix(runs: list[str]):
    """The builder as nix behaves: `flake lock` writes the commit it resolved, and `build`
    streams an image named as the flake names it."""
    def run(spec) -> ExecResult:
        script = spec.command[-1]
        work = Path(next(m.source for m in spec.mounts if m.target == "/src"))
        runs.append(script)
        if "flake lock" in script:
            (work / "flake.lock").write_text(json.dumps(
                {"nodes": {"nixpkgs": {"locked": {"rev": REV}}}}))
        else:
            text = (work / "flake.nix").read_text()
            name = re.search(r'name = "(raigolmid/toolbelt-[^"]+)"', text).group(1)
            tag = re.search(r'tag = "([^"]+)"', text).group(1)
            assert f"nixpkgs/{REV}" in text, "the build names the commit it is pinned to"
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tar:
                data = json.dumps([{"RepoTags": [f"{name}:{tag}"]}]).encode()
                info = tarfile.TarInfo("manifest.json")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            (work / "image.tar").write_bytes(buf.getvalue())
        return ExecResult(exit_code=0, output="")
    return run


def test_a_refused_list_is_built_pinned_and_then_is_the_toolbelts(h, tmp_path,
                                                                  nixery_dev):
    d = tmp_path / "tb"
    d.mkdir()
    (d / "toolbelt.toml").write_text(
        'id = "infra"\npackages = ["bash", "coreutils", "libcap", "python3", "util-linux", '
        '"terraform"]\n')
    toolbelt = load_toolbelt(d)
    nixery_dev.unbuildable.add("terraform")
    h.runtime.add_image(flakes.BUILDER)
    runs: list[str] = []
    h.runtime.one_shot[naming.flake_build()] = _nix(runs)

    h.session.instances.fetch_toolbelt(toolbelt)

    assert [e.type for e in h.events_of("toolbelt.nixery_refused")] == ["toolbelt.nixery_refused"]
    (built,) = h.events_of("toolbelt.flake_built")
    assert built.data["nixpkgs"] == REV and h.runtime.image(built.data["image"]) is not None
    closure = h.session.resolver.resolve(toolbelt)
    assert (closure.method, closure.image, closure.nixpkgs) == ("flake", built.data["image"], REV)

    h.session.instances.fetch_toolbelt(toolbelt)
    assert len(runs) == 2, "the image it built is the toolbelt's; nothing is built again"


def test_nothing_a_toolbelt_names_is_nix_code(tmp_path):
    d = tmp_path / "tb"
    d.mkdir()
    (d / "toolbelt.toml").write_text('id = "x"\npackages = ["hello\\"; evil = \\"x"]\n')
    with pytest.raises(flakes.FlakeError, match="not a nixpkgs attribute path"):
        flakes.generate(load_toolbelt(d))


def test_a_faces_apps_nixery_refuses_are_built_from_a_flake_and_kept(h, tmp_path, monkeypatch,
                                                                     nixery_dev):
    monkeypatch.setattr(h.session.closures, "of_image_id", lambda image_id: tmp_path)
    face = h.session.catalogue.faces["minimal"]
    nixery_dev.unbuildable.add(Faces.apps(face).packages[-1])
    h.runtime.add_image(flakes.BUILDER)
    runs: list[str] = []
    h.runtime.one_shot[naming.flake_build()] = _nix(runs)

    image_id, _ = h.session.faces.apps_closure(face)

    (built,) = h.events_of("toolbelt.flake_built")
    assert h.session.faces.apps_reference(face) == built.data["image"]
    assert h.runtime.image(built.data["image"]).id == image_id
    h.session.collect_garbage()
    assert h.runtime.image(built.data["image"]) is not None, "a face's flake build is its apps"
