"""Each face's apps keep their own config and state in the user's shared home, and the machine tab
may start one face's from another's."""
from __future__ import annotations

from pathlib import Path

import pytest

from raigolmid import faces as face_module
from raigolmid import naming
from raigolmid.definitions import DefinitionError, load_face
from raigolmid.faces import Faces, FaceRuntimeState
from raigolmid.session import SessionError
from tests.harness import Harness
from tests.test_faces import ScriptedHost, a_face, rig, windows_of_faces  # noqa: F401


def test_a_faces_apps_are_given_its_own_config_and_state_in_the_users_home(rig, tmp_path):
    runtime, paths, events = rig
    faces = Faces(runtime, paths, events, host=ScriptedHost(windows_of_faces(runtime)))
    faces.start(a_face(tmp_path))

    spec = runtime.spec_of(naming.face("writing"))
    home = next(m for m in spec.mounts if m.target == face_module.FACE_HOME)
    for var, leaf in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state")):
        inside = Path(spec.environment[var])
        # The path the face's apps are given is the one the daemon seeds on the host.
        assert Path(home.source) / inside.relative_to(face_module.FACE_HOME) \
            == faces.settings("writing") / leaf


@pytest.mark.parametrize("face_id", ["..", ".hidden", "a/b", ""])
def test_a_face_id_that_is_not_one_directory_name_is_refused(tmp_path, face_id):
    d = tmp_path / "face"
    (d / "desktop").mkdir(parents=True)
    (d / "desktop" / "sway.conf").write_text("")
    (d / "face.toml").write_text(
        f'id = "{face_id}"\n\n[desktop]\ncompositor = "sway"\nconfig_dir = "desktop/"\n')
    with pytest.raises(DefinitionError, match="is not letters, digits"):
        load_face(d)


@pytest.fixture()
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.session.ensure_tabs()
    return harness


def _two_faces(h) -> tuple[str, str]:
    ids = sorted(h.session.catalogue.faces)
    assert len(ids) >= 2
    return ids[0], ids[1]


def test_the_machine_tab_seeds_a_face_from_anothers_replacing_its_own(h):
    face, source = _two_faces(h)
    faces = h.session.faces
    (faces.settings(source) / "config" / "foot").mkdir(parents=True)
    (faces.settings(source) / "config" / "foot" / "foot.ini").write_text("font=mono:size=11\n")
    (faces.settings(face) / "state").mkdir(parents=True)
    (faces.settings(face) / "state" / "stale").write_text("old")

    machine = h.session.intent.machine_tab().tab_id
    assert h.session.seed_face_settings(machine, face, source) == {
        "face": face, "seeded_from": source}

    assert (faces.settings(face) / "config" / "foot" / "foot.ini").read_text() \
        == "font=mono:size=11\n"
    assert not (faces.settings(face) / "state" / "stale").exists()
    assert (faces.settings(source) / "config" / "foot" / "foot.ini").is_file()


def test_seeding_is_refused_where_it_cannot_be_what_was_asked(h, monkeypatch):
    face, source = _two_faces(h)
    faces = h.session.faces
    machine = h.session.intent.machine_tab().tab_id

    h.session.select("body", "myapi")
    with pytest.raises(SessionError, match="only the machine tab"):
        h.session.seed_face_settings(h.tab("myapi"), face, source)
    with pytest.raises(SessionError, match="no face 'nope'"):
        h.session.seed_face_settings(machine, face, "nope")
    with pytest.raises(SessionError, match="from itself"):
        h.session.seed_face_settings(machine, face, face)
    with pytest.raises(SessionError, match="has no settings to seed from"):
        h.session.seed_face_settings(machine, face, source)

    (faces.settings(source) / "config").mkdir(parents=True)
    monkeypatch.setattr(faces, "current", lambda: FaceRuntimeState(
        face_id=face, container=naming.face(face), pid=4242, wayland_display="wayland-2",
        fullscreen=True, runtime_dir=Path("/run")))
    with pytest.raises(SessionError, match="is on the user's screen"):
        h.session.seed_face_settings(machine, face, source)
    assert not faces.settings(face).exists()
