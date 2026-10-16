"""A malformed definition is refused naming its file, and every other definition still loads:
one bad layer never freezes the catalogue or stops the daemon starting."""
from pathlib import Path

import pytest

from raigolmid.definitions import SearchPaths, discover

PACKAGES = 'packages = ["libcap", "python3", "coreutils", "util-linux"]\n'


def _search(tmp_path: Path) -> SearchPaths:
    return SearchPaths(faces=(tmp_path / "faces",), toolbelts=(tmp_path / "toolbelts",),
                       bodies=(tmp_path / "bodies",), private=())


def _write(tmp_path: Path, kind: str, name: str, marker: str, text: str | bytes) -> Path:
    directory = tmp_path / kind / name
    directory.mkdir(parents=True)
    path = directory / marker
    path.write_bytes(text if isinstance(text, bytes) else text.encode())
    return path


@pytest.mark.parametrize("bad", [
    b'id = "bad"\nname = "After \xff\xfe"\n' + PACKAGES.encode(),
    'id = 5\n' + PACKAGES,
    'id = "Bad Id"\n' + PACKAGES,
    'id = "../escape"\n' + PACKAGES,
    'id = "bad"\npackages = "libcap"\n',
    'id = "bad"\ncapabilities = "shell"\n' + PACKAGES,
    'id = "bad"\nname = 5\n' + PACKAGES,
])
def test_a_bad_toolbelt_is_refused_and_the_rest_load(tmp_path, bad):
    bad_path = _write(tmp_path, "toolbelts", "a-bad", "toolbelt.toml", bad)
    _write(tmp_path, "toolbelts", "good", "toolbelt.toml", 'id = "good"\n' + PACKAGES)
    cat = discover(_search(tmp_path))
    assert set(cat.toolbelts) == {"good"}
    assert len(cat.errors) == 1 and cat.errors[0].startswith(str(bad_path))


@pytest.mark.parametrize("bad", [
    'ports = ["http"]\n',
    'ports = [0]\n',
    'ports = [70000]\n',
    'ports = [true]\n',
    'ports = 8000\n',
    'environment = "MODE=dev"\n',
    'environment = { A = 1 }\n',
    'environment = { D = { x = 1 } }\n',
    'command = 5\n',
    'command = "python"\n',
    'read_only = "yes"\n',
    'develop = "x"\n',
    '[develop]\nwatch = ["requirements.txt"]\n',
    'working_copy = 5\n',
    'dockerfile = "Dockerfile"\n',
    'id = "Bad Id"\n',
])
def test_a_bad_body_is_refused_and_the_rest_load(tmp_path, bad):
    toml = bad if bad.startswith("id") else 'id = "bad"\n' + bad
    image = '' if "dockerfile" in bad else 'image = "python:3.12-slim"\n'
    # A top-level key written after a table header would belong to the table.
    bad_path = _write(tmp_path, "bodies", "a-bad", "body.toml",
                      image + toml if bad.startswith("[") else toml + image)
    _write(tmp_path, "bodies", "good", "body.toml",
           'id = "good"\nimage = "python:3.12-slim"\nports = [8000]\n'
           'environment = { MODE = "dev" }\n')
    cat = discover(_search(tmp_path))
    assert set(cat.bodies) == {"good"}
    assert cat.bodies["good"].ports == (8000,)
    assert len(cat.errors) == 1 and cat.errors[0].startswith(str(bad_path))


@pytest.mark.parametrize("bad", [
    'editor = "vim"\n',
    '[desktop]\ncompositor = "sway"\napps = "firefox"\n',
    'requires_toolbelt_capabilities = "shell"\n[desktop]\ncompositor = "sway"\n',
])
def test_a_bad_face_is_refused_and_the_toolbelts_load(tmp_path, bad):
    bad_path = _write(tmp_path, "faces", "bad", "face.toml", 'id = "bad"\n' + bad)
    _write(tmp_path, "toolbelts", "good", "toolbelt.toml", 'id = "good"\n' + PACKAGES)
    cat = discover(_search(tmp_path))
    assert cat.faces == {} and set(cat.toolbelts) == {"good"}
    assert len(cat.errors) == 1 and cat.errors[0].startswith(str(bad_path))
