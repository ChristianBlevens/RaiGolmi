"""A face restarted for a change to it opens again the windows the user had open: each with the
command and environment it had, except what the face's own start-up opened again already and the
editor, which the daemon opens."""
from __future__ import annotations

import pytest

from raigolmid.faces import FaceQuiet, FaceRuntimeState, Faces
from raigolmid.runtime.base import ContainerSpec
from raigolmid.session import SessionError
from tests.harness import Harness

FACE = "raigolmid-face-writing"


class Compositor:
    """The face's sway: these windows, and no new one mapping while it is watched."""

    def __init__(self, views):
        self.views = views

    def tree(self):
        return {"id": 1, "nodes": [{"id": 10 + n, "pid": pid, "app_id": app, "name": app,
                                    "nodes": []} for n, (pid, app) in enumerate(self.views)]}

    def window_events(self, timeout):
        def each():
            raise FaceQuiet("quiet")
            yield
        return each()


@pytest.fixture()
def h(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    h.runtime.add_image("face-image")
    h.runtime.run(ContainerSpec(name=FACE, image="face-image"))
    monkeypatch.setattr(Faces, "current", lambda _self: FaceRuntimeState(
        face_id="writing", container=FACE, pid=1, wayland_display="wayland-3",
        fullscreen=True, runtime_dir=h.paths.runtime))
    return h


def _process(h, pid, argv, env):
    h.runtime.exec_results[f"cat /proc/{pid}/cmdline"] = (0, "\0".join(argv) + "\0")
    h.runtime.exec_results[f"cat /proc/{pid}/environ"] = (0, "\0".join(env) + "\0")


def test_what_the_face_did_not_open_itself_is_opened_again_as_it_was(h, monkeypatch):
    editor = ["foot", "nvim", "--listen", str(h.paths.editor_socket)]
    _process(h, 5, ["firefox"], ["GTK_THEME=Adwaita:dark", "WAYLAND_DISPLAY=wayland-1",
                                 "XDG_ACTIVATION_TOKEN=old"])
    _process(h, 6, ["foot", "-c", "/etc/face/foot.ini"], ["WAYLAND_DISPLAY=wayland-1"])
    _process(h, 7, editor, [])
    before = Compositor([(5, "firefox"), (6, "foot"), (7, "raigolmi-editor")])
    monkeypatch.setattr(Faces, "_nested_compositor", lambda _self, _s: before)
    open_ = h.session.faces.windows()
    assert [w["app"] for w in open_] == ["firefox", "foot", "raigolmi-editor"]

    # Restarted, the face's start-up has opened Firefox again by itself.
    _process(h, 50, ["firefox"], ["GTK_THEME=Adwaita:dark"])
    monkeypatch.setattr(Faces, "_nested_compositor",
                        lambda _self, _s: Compositor([(50, "firefox")]))
    assert h.session.faces.reopen(open_) == [["foot", "-c", "/etc/face/foot.ini"]]
    container, command, env = h.runtime.spawn_log[-1]
    assert container == FACE and command[-3:] == ["foot", "-c", "/etc/face/foot.ini"]
    assert env == {"WAYLAND_DISPLAY": "wayland-3"}


def test_only_the_machine_tab_restarts_the_face(h):
    h.session.select("body", "myapi")
    with pytest.raises(SessionError, match="only the machine tab"):
        h.session.restart_face(h.tab("myapi"))
