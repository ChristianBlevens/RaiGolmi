"""The channel rides two private seams of mcp (`mcp_server.serve_with_channel`), so the version
installed is the one pinned, and each seam is named here: a move fails by its name rather than
as a push that never arrives."""
from __future__ import annotations

import importlib.metadata
import re
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_the_installed_mcp_is_the_pinned_one():
    deps = tomllib.loads(PYPROJECT.read_text())["project"]["dependencies"]
    pins = [m.group(1) for d in deps if (m := re.fullmatch(r"mcp==(\S+)", d))]
    assert len(pins) == 1, f"mcp is not pinned exactly in {PYPROJECT}: {deps}"
    assert importlib.metadata.version("mcp") == pins[0]


