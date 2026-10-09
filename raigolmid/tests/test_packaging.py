"""What an install actually contains.

Two things are easy to lose and expensive to lose: `ui/`, which lives beside `raigolmid/`
in the repo layout and is pulled in with a `package-dir` mapping, and the session
view's entrypoint, which is mounted into every view from the installed package.
Losing either produces a working test suite and a broken machine — `rai ai` with no module
and views that die in their entrypoint.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent


@pytest.fixture(scope="module")
def wheel(tmp_path_factory) -> zipfile.ZipFile:
    out = tmp_path_factory.mktemp("wheel")

    # Built from a copy, because setuptools writes `raigolmid.egg-info` beside the sources it
    # is given. This checkout lives on a Windows drive over drvfs, where a directory created
    # by one user cannot have its timestamp updated by another: the artefact this test left
    # behind made `pip install -e` fail for the user with "Cannot update time stamp of
    # directory". A test must leave the tree it reads exactly as it found it.
    source = tmp_path_factory.mktemp("source") / "raigolmid"
    shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns(
        "*.egg-info", "build", "dist", "__pycache__", ".pytest_cache"))
    shutil.copytree(REPO / "ui", source.parent / "ui",
                    ignore=shutil.ignore_patterns("__pycache__"))

    built = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(out), str(source)],
        capture_output=True, text=True)
    if built.returncode != 0:
        pytest.skip(f"could not build a wheel here: {built.stderr[-400:]}")
    wheels = list(out.glob("*.whl"))
    assert wheels, "pip reported success but produced no wheel"
    return zipfile.ZipFile(wheels[0])


def test_the_selector_and_ai_terminal_are_installed(wheel):
    names = set(wheel.namelist())
    for module in ("ui/viewmodel/model.py", "ui/ai_terminal/terminal.py", "ui/theme.py"):
        assert module in names, f"{module} is missing from the wheel; `rai ai` would fail"


def test_the_view_entrypoint_is_installed(wheel):
    assert "raigolmid/launcher/view/viewinit.py" in wheel.namelist(), \
        "the session view's entrypoint is not installed, so every view would die at start"


def test_the_settings_document_is_installed(wheel):
    assert "raigolmid/settings.toml" in wheel.namelist(), \
        "the shipped settings are not installed, so the daemon cannot write the user's at its start"


def test_the_launcher_has_no_dependencies_outside_the_standard_library(wheel):
    """It ships in a closure mounted into every view, so a dependency would have to be in
    that closure too."""
    stdlib = set(sys.stdlib_module_names)
    for member in ("raigolmid/launcher/server.py", "raigolmid/launcher/protocol.py"):
        source = wheel.read(member).decode()
        imports = re.findall(r"^(?:import|from)\s+([A-Za-z_][\w.]*)", source, re.MULTILINE)
        outside = {i.split(".")[0] for i in imports} - stdlib - {"raigolmid", ""}
        assert not outside, f"{member} imports {outside}, which the view's closure lacks"


def test_the_agent_image_copies_everything_the_install_packages_and_no_more(wheel, monkeypatch):
    """The agent image installs from what its Dockerfile copies, and its digest is those
    files: a packaged module left out is an `rai` that fails in every tab, and a file copied
    that the install never reads rebuilds the image for nothing."""
    from raigolmid import hostimages
    monkeypatch.setenv(hostimages.SOURCE_ENV, str(REPO))
    copied = {p.relative_to(REPO) for p in hostimages.agent(None)._copied()}
    packaged = {Path(m) if m.startswith("ui/") else Path("raigolmid") / m
                for m in wheel.namelist() if ".dist-info/" not in m}
    missing = packaged - copied
    assert not missing, f"the agent image does not copy {sorted(map(str, missing))}"
    unused = {p for p in copied
              if p.parts[0] in ("raigolmid", "ui") and p not in packaged
              and p != Path("raigolmid/pyproject.toml")}
    assert not unused, f"the agent image copies what the install never reads: {sorted(map(str, unused))}"


