"""A toolbelt Nixery refuses — an unfree package is the case — is built on
the machine from a generated flake, pinned to the nixpkgs commit it was built from, and its
image is the toolbelt's from then on."""
from __future__ import annotations

import io
import json
import re
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from raigolmid import flakes, naming
from raigolmid.definitions import load_toolbelt
from raigolmid.runtime.base import ExecResult

from tests.harness import Harness

REV = "f9bce96a417afbb9c64725f8727e42efafeafc21"


@pytest.fixture
def h(tmp_path, monkeypatch):
    def urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 500, "Error", {}, io.BytesIO(
            json.dumps({"errors": [{"message": "image build failure"}]}).encode()))
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
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


def test_a_refused_list_is_built_pinned_and_then_is_the_toolbelts(h, tmp_path):
    d = tmp_path / "tb"
    d.mkdir()
    (d / "toolbelt.toml").write_text(
        'id = "infra"\npackages = ["bash", "coreutils", "libcap", "python3", "util-linux", '
        '"terraform"]\n')
    toolbelt = load_toolbelt(d)
    h.runtime.unbuildable.add("terraform")
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
