"""The agent shows the user a file in the face's editor, and sees the face.

What is asserted is what reaches the face and what comes back: the keys the editor is sent,
and the capture the agent is handed. Which display is the face's is `test_faces.py`'s.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from raigolmid import naming
from raigolmid.faces import FaceError, FaceRuntimeState, Faces
from raigolmid.runtime.base import ContainerSpec, ExecResult
from raigolmid.session import SessionError

from tests.facedisplay import NestedSway
from tests.harness import Harness

FACE = "raigolmid-face-writing"
# The harness opens the machine tab first, so selecting myapi opens tab-2.
SANDBOX = "myapi@tab-2"


@pytest.fixture()
def h(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    # Only something to exec in: `Faces.current` is what names it the face, and a container
    # labelled as one would be synced as a face the fake cannot start.
    h.runtime.add_image("face-image")
    h.runtime.run(ContainerSpec(name=FACE, image="face-image"))
    monkeypatch.setattr(Faces, "current", lambda _self: FaceRuntimeState(
        face_id="writing", container=FACE, pid=1, wayland_display="wayland-2",
        fullscreen=True, runtime_dir=h.paths.runtime))
    return h


def _editor_command(h) -> list[str]:
    return ["nvim", "--server", str(h.paths.editor_socket), "--remote-send",
            f'<C-\\><C-N><Cmd>lua require("raigolmi").show("{h.paths.editor_request}")<CR>']


def _on_the_face(h) -> None:
    h.open_sandbox("myapi", "python-dev")
    h.session.select("face", "writing")


def test_a_file_is_opened_at_its_line_by_what_the_face_names(h):
    """The path goes in the request file, never into the editor's keys, where a quote in it
    would end the string and the rest run as an editor command."""
    h.paths.editor_socket.touch()
    _on_the_face(h)
    cmd = _editor_command(h)
    h.runtime.exec_results[" ".join(cmd)] = (0, "")

    assert h.session.show_file(SANDBOX, "/work/app/it's <here>.py", 12) == {
        "shown": "/work/app/it's <here>.py", "line": 12}
    assert h.runtime.exec_log[-1] == (FACE, cmd)
    assert json.loads(h.paths.editor_request.read_text()) == {
        "path": "/work/app/it's <here>.py", "line": 12}


def test_a_sandbox_the_face_is_not_showing_is_refused(h):
    h.paths.editor_socket.touch()
    _on_the_face(h)
    with pytest.raises(SessionError, match="not the sandbox on the face"):
        h.session.show_file("webui@tab-3", "/work/a.py", 1)
    with pytest.raises(SessionError, match="not a file under /work"):
        h.session.show_file(SANDBOX, "/definitions/bodies/x.toml", 1)
    assert not h.runtime.exec_log


def test_a_file_the_editor_refused_is_an_error_with_its_reason(h):
    h.paths.editor_socket.touch()
    _on_the_face(h)
    h.runtime.exec_results[" ".join(_editor_command(h))] = (1, "E247: no server running")
    with pytest.raises(FaceError, match="E247"):
        h.session.show_file(SANDBOX, "/work/a.py", 1)


def test_a_screenshot_of_the_faces_own_display_lands_in_the_tabs_home(h):
    h.session.ensure_tabs()
    seen: list[ContainerSpec] = []

    def grim(spec: ContainerSpec) -> ExecResult:
        seen.append(spec)
        out = next(m.source for m in spec.mounts if m.target == "/out")
        Path(out, spec.command[1].removeprefix("/out/")).write_bytes(b"\x89PNG")
        return ExecResult(exit_code=0, output="")

    h.runtime.one_shot[naming.screenshot()] = grim
    result = h.session.screenshot("tab-1")

    (spec,) = seen
    assert spec.image == "nixery.dev/shell/grim"
    assert spec.environment["WAYLAND_DISPLAY"] == "wayland-2", \
        "the face's nested display, never the host's with the AI terminal over it"
    home = h.session.agents.home("tab-1")
    assert result["path"].startswith(str(home / "screenshots") + "/")
    assert result["path"].endswith(".png")


def _on_screen(h, monkeypatch) -> list:
    shown: list = []
    monkeypatch.setattr(Faces, "show_url", lambda _self, face, url: shown.append((face.id, url)))
    assert h.open_sandbox("myapi", "python-dev") == SANDBOX
    h.session.select("face", "writing")
    return shown


def test_a_local_page_is_the_asking_sandboxs_by_its_name_on_the_network(h, monkeypatch):
    """The face's localhost is its own; the sandbox is on the machine's network by name
    (`naming.host`), so any port its body or toolbelt listens on is reached."""
    shown = _on_screen(h, monkeypatch)
    assert h.session.show_url(SANDBOX, "http://localhost:8000/docs?x=1") == {
        "shown": "http://myapi.tab-2:8000/docs?x=1"}
    assert shown == [("writing", "http://myapi.tab-2:8000/docs?x=1")]
    h.session.show_url(SANDBOX, "http://127.0.0.1:9999/")
    assert shown[-1] == ("writing", "http://myapi.tab-2:9999/"), "an undeclared port too"
    h.session.show_url(SANDBOX, "https://example.com/")
    assert shown[-1] == ("writing", "https://example.com/"), "a remote URL is left alone"


def _browser_face(tmp_path):
    from raigolmid.definitions import Face, FaceDesktop
    return Face(id="w", name="W", directory=tmp_path, requires_toolbelt_capabilities=(),
                editor=None, desktop=FaceDesktop(compositor="sway", config_dir=None,
                                                 apps=("firefox",), browser="firefox"))


def test_the_browser_is_opened_on_the_faces_own_display(h, tmp_path, monkeypatch):
    monkeypatch.setattr(Faces, "_nested_compositor", lambda _self, _s: NestedSway(h.runtime))
    h.session.faces.show_url(_browser_face(tmp_path), "http://myapi.tab-2:8000/")
    container, cmd, env = h.runtime.spawn_log[-1]
    assert container == FACE and env == {"WAYLAND_DISPLAY": "wayland-2"}
    assert "firefox http://myapi.tab-2:8000/" in cmd[-1]


def test_a_browser_that_opens_no_window_is_a_refusal_with_its_output(h, tmp_path, monkeypatch):
    """Epiphany in a face dies at start (WebKit's sandbox needs user namespaces), and the
    call must not answer "shown" all the same."""
    monkeypatch.setattr(Faces, "_nested_compositor", lambda _self, _s: NestedSway(h.runtime, maps=lambda c: False))
    monkeypatch.setattr("raigolmid.faces.START_TIMEOUT", 0.3)
    h.paths.browser_log.write_text("bwrap: No permissions to create a new namespace\n")
    with pytest.raises(FaceError, match="(?s)opened no window.*bwrap: No permissions"):
        h.session.faces.show_url(_browser_face(tmp_path), "http://myapi.tab-2:8000/")

