"""`rai diagnose` is made to be attached to a public issue: a body's environment values and
command arguments are replaced, and it never writes through a link where the outbox should be."""
import json
from types import SimpleNamespace

import pytest

from rai.__main__ import _redacted_compose, cmd_diagnose


def test_environment_values_and_command_arguments_are_replaced():
    project = {"services": {"body": {"environment": {"API_KEY": "sk-secret"},
                                     "command": ["serve", "--token=abc", "8000"]}}}
    service = json.loads(_redacted_compose(json.dumps(project)))["services"]["body"]
    assert service["environment"] == {"API_KEY": "<redacted>"}
    assert service["command"] == ["serve", "<redacted>", "<redacted>"]


def test_a_link_in_place_of_the_outbox_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "Transfer").mkdir()
    (tmp_path / "Transfer" / "out").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(SystemExit, match="is a link"):
        cmd_diagnose(SimpleNamespace(output=None))
