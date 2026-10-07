"""A layer names paths only inside its own directory, and a body's working copy never overlaps
where the machine keeps its sign-ins and state: a downloaded layer cannot mount or build from
them."""
import os
from pathlib import Path

import pytest

from raigolmid.definitions import DefinitionError, load_body, load_face


def _body(tmp_path: Path, toml: str) -> Path:
    body = tmp_path / "bodies" / "b"
    body.mkdir(parents=True)
    (body / "Dockerfile").write_text("FROM scratch\n")
    (body / "body.toml").write_text('id = "b"\ndockerfile = "Dockerfile"\n' + toml)
    return body


@pytest.mark.parametrize("toml", [
    'context = "../.."\n',
    '[[develop.watch]]\npath = "../../secrets"\n',
])
def test_a_body_path_outside_its_directory_is_refused(tmp_path, toml):
    with pytest.raises(DefinitionError, match="outside"):
        load_body(_body(tmp_path, toml), ())


def test_a_link_out_of_the_directory_is_refused(tmp_path):
    body = _body(tmp_path, 'context = "out"\n')
    (body / "out").symlink_to(tmp_path)
    with pytest.raises(DefinitionError, match="outside"):
        load_body(body, ())


@pytest.mark.parametrize("where", ["home", "home/.config/raigolmi", "home/.config"])
def test_a_working_copy_overlapping_the_machines_own_is_refused(tmp_path, where):
    own = tmp_path / "home" / ".config" / "raigolmi"
    own.mkdir(parents=True)
    with pytest.raises(DefinitionError, match="machine's own"):
        load_body(_body(tmp_path, f'working_copy = "{tmp_path / where}"\n'), (own,))


@pytest.mark.parametrize("where", ["home/.config/systemd/user", "home/.ssh", "/run/raigolmid"])
def test_a_working_copy_where_the_machine_runs_things_is_refused(tmp_path, where):
    with pytest.raises(DefinitionError, match="machine's own"):
        load_body(_body(tmp_path, f'working_copy = "{tmp_path / where}"\n'), ())


def test_a_project_elsewhere_is_a_working_copy(tmp_path):
    own = tmp_path / "home" / ".config" / "raigolmi"
    own.mkdir(parents=True)
    project = tmp_path / "home" / "projects" / "api"
    project.mkdir(parents=True)
    (project / "Dockerfile").write_text("FROM scratch\n")
    body = load_body(_body(tmp_path, f'working_copy = "{project}"\n'), (own,))
    assert body.source_root == project


def test_a_project_named_relatively_is_in_the_bodys_own_directory(tmp_path):
    """A body made for a project keeps it beside its definition: `working_copy = "project"`."""
    body_dir = _body(tmp_path, 'working_copy = "project"\n')
    (body_dir / "project").mkdir()
    (body_dir / "project" / "Dockerfile").write_text("FROM scratch\n")
    assert load_body(body_dir, ()).source_root == body_dir / "project"


def test_a_face_config_dir_outside_its_directory_is_refused(tmp_path):
    face = tmp_path / "faces" / "f"
    face.mkdir(parents=True)
    (face / "face.toml").write_text(
        'id = "f"\n[desktop]\ncompositor = "sway"\nconfig_dir = "../../"\n')
    with pytest.raises(DefinitionError, match="outside"):
        load_face(face)


def test_a_working_copy_another_user_owns_is_refused(tmp_path):
    project = tmp_path / "theirs"
    project.mkdir()
    os.chown(project, os.getuid() + 1, os.getgid())
    with pytest.raises(DefinitionError, match="not this machine's user"):
        load_body(_body(tmp_path, f'working_copy = "{project}"\n'), ())
