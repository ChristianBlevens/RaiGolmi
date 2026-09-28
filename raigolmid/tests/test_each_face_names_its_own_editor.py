"""Each face names its own editor: what its window runs, and what shows it a file.
The product assumes no editor."""
from __future__ import annotations

from pathlib import Path

import pytest

from raigolmid.definitions import DefinitionError, load_face


def _face(tmp_path: Path, editor: str, config_dir: bool = False) -> Path:
    d = tmp_path / "face"
    (d / "editor").mkdir(parents=True)
    (d / "face.toml").write_text(
        'id = "helix"\n\n[editor]\npackage = "helix"\n'
        + ('config_dir = "editor/"\n' if config_dir else "") + editor)
    return d


@pytest.mark.parametrize("editor, names", [
    ('command = ["hx", "{path}"]\n', "{path}"),
    ('command = ["hx", "-c", "{config}/c.toml"]\n', "{config}"),
    ('command = ["hx"]\nopen = ["hx-open", "{glue}"]\n', "{glue}"),
])
def test_a_place_the_command_is_not_given_is_refused(tmp_path, editor, names):
    with pytest.raises(DefinitionError, match=f"names {names}"):
        load_face(_face(tmp_path, editor))


def test_a_brace_that_names_no_place_is_its_own_text(tmp_path):
    face = load_face(_face(tmp_path, 'command = ["nvim", "--cmd", "lua t = {}"]\n'))
    assert face.editor.command[-1] == "lua t = {}"


