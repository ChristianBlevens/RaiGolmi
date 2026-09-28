"""The file `compose.py` hands Compose must parse back, by a YAML parser, to exactly what
the daemon built once Compose has read `$$` as `$`.

The real check is `docker compose config`, which needs a daemon and so runs only in
`test_integration_docker.py`. A mismatch here does not fail loudly: Compose reads a file
that means something else — a port that became a number, a value whose `$HOME` it filled
in — and the body comes up wrong rather than not at all.
"""
from __future__ import annotations

import pytest

yaml = pytest.importorskip(
    "yaml", reason="pyyaml checks the emitted YAML; install it to check the emitter")

from raigolmid.compose import _compose_file  # noqa: E402


def _as_compose_reads(value):
    if isinstance(value, str):
        return value.replace("$$", "$")
    if isinstance(value, dict):
        return {k: _as_compose_reads(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_as_compose_reads(v) for v in value]
    return value


def round_trip(data):
    """What Compose reads out of what the emitter wrote."""
    return _as_compose_reads(yaml.safe_load(_compose_file(data)))


@pytest.mark.parametrize("value", [
    "plain",
    "with space",
    "has:colon",
    "has#hash",
    "trailing ",
    " leading",
    "",
    "123",              # numeric-looking: must come back a string, not an int
    "1.5",
    "0755",
    "yes", "no", "on", "off", "true", "false", "null", "~",   # YAML reserved words
    'quote"inside',
    "back\\slash",
    "a, b",
    "[bracketed]",
    "{braced}",
    "*anchor",
    "&ref",
    "- dashed",
    "$HOME", "${HOME}", "a$$b", "cost: $5",     # Compose interpolates every value
    "\x1f", "\x7f", "\x85", "line\nbreak", "carriage\rreturn", "tab\there",
    "sep\u2028arator", "para\u2029graph", "caf\u00e9", "\U0001f600",
])
def test_a_string_survives_the_emitter_as_the_same_string(value):
    assert round_trip({"k": value}) == {"k": value}


def test_types_are_preserved(): 
    data = {"n": 8000, "f": 1.5, "t": True, "f2": False}
    assert round_trip(data) == data


def test_a_rendered_body_parses_to_exactly_what_render_returned():
    """The real shape, not a synthetic one: the service Compose is handed must be the dict
    the daemon built, key for key."""
    rendered = {
        "services": {
            "body": {
                "image": "raigolmi/body-myapi:sha256-abc123",
                "pid": "container:raigolmi-anchor-myapi-session",
                "network_mode": "container:raigolmi-anchor-myapi-session",
                "labels": {
                    "io.raigolmi.managed": "true",
                    "io.raigolmi.instance": "myapi@tab-2",
                    "io.raigolmi.definition_digest": "0123456789abcdef",
                    "io.raigolmi.epoch": "1",
                },
                "volumes": ["/home/agent/projects/myapi:/work"],
                "working_dir": "/work",
                "restart": "no",
                "command": ["uvicorn", "app.main:app", "--port", "8000"],
                "environment": {"PYTHONUNBUFFERED": "1", "TZ": "Etc/UTC"},
                "read_only": True,
                "tmpfs": ["/tmp"],
                "develop": {"watch": [{"path": "requirements.txt", "action": "rebuild"}]},
            }
        }
    }
    assert round_trip(rendered) == rendered


def test_the_bodys_user_stays_a_string():
    """`uid:gid` with a gid under 60 is a YAML 1.1 base-60 integer when bare: `1000:10` would
    reach Compose as 60010."""
    rendered = {"services": {"body": {"user": "1000:10"}}}
    assert round_trip(rendered) == rendered
