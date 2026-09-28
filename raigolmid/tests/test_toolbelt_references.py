"""A toolbelt must resolve to an image reference Docker will accept.

A nixpkgs attribute may contain uppercase — `bashInteractive`, for one — and a Docker
repository name may not. The failure is not a bad image but a rejected reference, so it
lands as "invalid reference format" from `docker run` with no container created at all:
nothing pulls, nothing starts, and every view in every instance is unreachable.
FakeRuntime accepts any string, so only a check on the reference itself catches it.
"""
from __future__ import annotations

import re
from pathlib import Path

from raigolmid.definitions import load_toolbelt
from raigolmid.toolbelts import nixery_reference

# distribution/reference: a path component is lowercase alphanumerics with single
# separators (.  _  __  -) between them. The registry host is stripped before matching.
COMPONENT = re.compile(r"^[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*$")


def test_reference_is_a_legal_docker_repository(tmp_path: Path) -> None:
    directory = tmp_path / "toolbelts" / "mixed-case"
    directory.mkdir(parents=True)
    (directory / "toolbelt.toml").write_text(
        'id = "mixed-case"\nname = "Mixed case"\nsupports = ["*"]\ncapabilities = ["shell"]\n'
        'packages = ["bashInteractive", "coreutils", "python3", "util-linux", "libcap"]\n')
    toolbelt = load_toolbelt(directory)
    reference = nixery_reference(toolbelt.packages)
    path = reference.split("/", 1)[1]
    for component in path.split("/"):
        assert COMPONENT.match(component), (
            f"{reference!r} is not a reference Docker will accept — "
            f"component {component!r} is illegal, so `docker run` rejects it outright"
        )
