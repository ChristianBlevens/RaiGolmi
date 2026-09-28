"""A selected face has to be on the screen, and re-picking it has to repair it when it is not.

The user picked their face and nothing happened. Measured on their machine: the selection was
already `minimal` from before a restart, nothing was nested, `rai select face minimal` exited
1 with `a session view needs either a toolbelt or a face's editor`, and a deselect followed by
the same select started `raigolmid-face-minimal` at once.

Two things were wrong and this pins both. The daemon never put the saved face back on the
screen, so `status` named a face over a screen with none. And `_apply_face` compared the new
selection with the *previous selection* rather than with what was actually running, so the one
gesture that could have repaired it — picking the face again — was the one it ignored.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from raigolmid.compatibility import Selection      # noqa: E402
from raigolmid.faces import FaceRuntimeState       # noqa: E402
from raigolmid.session import Session              # noqa: E402


class Faces:
    """⚠ `current()` answers from what is *running*, the way the real one reads Docker
    labels. A double that said "up" because a face was *selected* would be the defect under
    test."""

    def __init__(self, running: str | None = None, available: bool = True) -> None:
        self.host = SimpleNamespace(available=available)
        self.running = running
        self.switched: list[str] = []
        self.stopped = 0

    def current(self):
        if self.running is None:
            return None
        return FaceRuntimeState(face_id=self.running, container=f"raigolmid-face-{self.running}",
                                pid=1, wayland_display="wayland-9", fullscreen=True,
                                runtime_dir=Path("/run/user/1000"))

    def switch(self, face):
        self.switched.append(face.id)
        self.running = face.id
        return self.current()

    def stop(self, face_id: str | None = None) -> None:
        self.stopped += 1
        self.running = None


def a_session(selection: Selection, faces: Faces, faces_defined=("minimal",)) -> Session:
    """Only what the decision under test touches. Building a whole world here would test the
    world rather than the decision."""
    session = Session.__new__(Session)
    session.faces = faces
    session.events = SimpleNamespace(emit=lambda *a, **k: None)
    session.intent = SimpleNamespace(selection=selection)
    session.catalogue = SimpleNamespace(faces={
        face_id: SimpleNamespace(id=face_id, desktop=SimpleNamespace(compositor="sway"),
                                 editor=None)
        for face_id in faces_defined})
    session._face_failure = None
    session._editor_window_due = None
    return session


def test_picking_the_face_again_nests_it_when_nothing_is_nested():
    """The user's case exactly: the session says `minimal`, the screen has nothing on it."""
    faces = Faces(running=None)
    session = a_session(Selection(face="minimal"), faces)
    session._apply_face(Selection(face="minimal"))
    assert faces.switched == ["minimal"], "the one gesture that could repair it did nothing"
    assert faces.running == "minimal"


def test_a_restart_puts_the_saved_face_back_on_the_screen():
    faces = Faces(running=None)
    session = a_session(Selection(face="minimal"), faces)
    session._restore_face_desktop()
    assert faces.switched == ["minimal"]


def test_a_restart_leaves_a_face_that_is_already_up_alone():
    faces = Faces(running="minimal")
    session = a_session(Selection(face="minimal"), faces)
    session._restore_face_desktop()
    assert faces.switched == []


def test_a_sandbox_that_fails_to_start_leaves_nothing_running(tmp_path, monkeypatch):
    """The sandbox a failed `sandbox_open` asked for is released with the failure."""
    import pytest
    from tests.harness import Harness
    h = Harness(tmp_path, monkeypatch)
    from raigolmid import labels
    h.session.select("body", "myapi")
    tab = h.tab("myapi")
    h.runtime.build_should_fail = True
    with pytest.raises(Exception, match="did not build"):
        h.session.sandbox_open(tab, "python-dev")
    assert f"myapi@{tab}" not in h.session.intent.instances
    assert h.session.intent.focused_instance is None
    # The tabs' agents run; nothing of the sandbox does.
    assert [c.name for c in h.runtime.list()
            if c.labels.get(labels.ROLE) != str(labels.Role.AGENT)] == []


def test_a_face_with_an_editor_alone_has_no_instance_and_work_at_paths_work(tmp_path,
                                                                             monkeypatch):
    """A face alone gets no instance, and its `/work` is the one directory a toolbelt
    alone also works in, so adding a toolbelt later finds the same files."""
    from raigolmid import naming
    from tests.harness import Harness
    h = Harness(tmp_path, monkeypatch)
    h.session.select("face", "writing")
    assert h.session.intent.focused_instance is None
    assert h.session.intent.instances == {}
    assert h.session._face_work(None) == str(h.paths.work)

    h.open_sandbox(None, "python-dev")
    assert h.session.instances.all()[naming.WORK].working_copy == str(h.paths.work)
