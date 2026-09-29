"""The selector is started once and moved through its socket, so the reserved key shows a
surface already drawn (raigolmid/hostsurfaces.py) and reports what the selector answered."""
from __future__ import annotations

import re
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from raigolmid import hostimages, settings                            # noqa: E402
from raigolmid.hostsurfaces import SELECTOR_CONTAINER, toggle_selector  # noqa: E402
from raigolmid.paths import Paths                                     # noqa: E402
from tests.fakeruntime import FakeRuntime                        # noqa: E402
from ui import surfaces                                              # noqa: E402
from ui.selector_native.visibility import hides_after                # noqa: E402
from ui.viewmodel import SelectorModel                               # noqa: E402


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    (tmp_path / "run").mkdir()
    source = tmp_path / "src"
    (source / "ui" / "selector_native").mkdir(parents=True)
    (source / "ui" / "selector_native" / "Containerfile").write_text("FROM fedora:44\n")
    monkeypatch.setenv(hostimages.SOURCE_ENV, str(source))
    runtime = FakeRuntime()
    runtime.add_image(hostimages.selector().tag())
    paths = Paths.from_env()
    # What the daemon's start writes before anything reads a setting.
    settings.install(paths.settings)
    return runtime, paths


def no_docker():
    raise AssertionError("a running selector is asked through its socket, never Docker")


def test_a_press_with_no_selector_starts_one_open(rig):
    runtime, paths = rig
    assert toggle_selector(lambda: runtime, paths) == "open"
    spec = runtime._record(SELECTOR_CONTAINER)["spec"]
    assert "--hidden" not in (spec.command or ())


def test_a_press_is_the_selectors_own_answer_and_needs_no_docker(rig):
    runtime, paths = rig
    state = {"open": False}

    def answer(verb: str) -> str:
        if verb == "toggle":
            state["open"] = not state["open"]
        return "open" if state["open"] else "closed"
    surfaces.serve(surfaces.SELECTOR, answer, paths.runtime)
    assert toggle_selector(no_docker, paths) == "open"
    assert toggle_selector(no_docker, paths) == "closed"


def test_a_selector_that_died_is_started_again(rig):
    """It leaves its socket file behind, and a file nobody answers is not a selector."""
    runtime, paths = rig
    dead = surfaces.socket_path(paths.runtime, surfaces.SELECTOR)
    dead.parent.mkdir()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.bind(str(dead))
    assert toggle_selector(lambda: runtime, paths) == "open"
    assert runtime._record(SELECTOR_CONTAINER)["spec"] is not None


def test_only_a_face_selected_takes_the_selector_off_screen():
    """The requests are the view-model's own, so a renamed kind cannot pass here and fail
    on the machine."""
    def item(id_, selected=False):
        return {"id": id_, "name": id_, "selected": selected, "selectable": True}
    model = SelectorModel(call=lambda method, **_: {})
    model.apply(({"faces": [item("sway"), item("tiled", selected=True)],
                  "bodies": [item("api")]}, {}))

    def request(row, id_):
        return model.toggle_request(row, id_)[1]

    assert hides_after(request("faces", "sway"), failed=False)
    assert not hides_after(request("faces", "sway"), failed=True)
    assert not hides_after(request("faces", "tiled"), failed=False)
    assert not hides_after(request("bodies", "api"), failed=False)


@pytest.mark.parametrize("surface", ["selector_native", "host_control", "notify_popup"])
def test_each_surface_image_carries_every_module_it_imports(surface):
    """The images copy modules by name, so a new import is a surface that fails to start on
    the machine and passes here — followed through `ui`'s own modules, which import each
    other (`ui/edge.py` imports `ui/slide.py`)."""
    containerfile = (ROOT / "ui" / surface / "Containerfile").read_text()
    copied = {f"raigolmid.{m}" for m in re.findall(r"raigolmid/raigolmid/(\w+)\.py", containerfile)}
    copied |= {f"ui.{m}" for m in re.findall(r"\bui/(\w+)\.py", containerfile)}
    copied |= {f"ui.{m}" for m in re.findall(r"\bui/(\w+)/ ", containerfile)}
    pending = list((ROOT / "ui" / surface).glob("*.py"))
    seen: set[str] = set()
    missing: set[str] = set()
    while pending:
        source = pending.pop()
        text = source.read_text()
        names = set(re.findall(r"^\s*from ((?:raigolmid|ui)\.\w+)", text, re.M))
        names |= {f"ui.{m}" for group in re.findall(r"^\s*from ui import ([\w, ]+)", text, re.M)
                  for m in group.replace(" ", "").split(",")}
        for name in names - seen:
            seen.add(name)
            if name not in copied:
                missing.add(name)
            elif name.startswith("ui.") and (ROOT / (name.replace(".", "/") + ".py")).exists():
                pending.append(ROOT / (name.replace(".", "/") + ".py"))
    assert seen and not missing, f"not copied into the {surface} image: {sorted(missing)}"
