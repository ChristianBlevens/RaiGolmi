"""The machine takes Windows' zone, and the containers the user reads a clock in
take the host's by name, with its zone data, whatever their image carries."""
import dataclasses
from pathlib import Path

from raigolmid import compose, localtime
from raigolmid.definitions import Body


def test_a_host_with_no_zone_passes_nothing(tmp_path):
    assert localtime.environment(tmp_path / "absent") == {}
    assert localtime.mounts(tmp_path / "absent") == ()
    plain = tmp_path / "localtime"
    plain.write_bytes(b"TZif")
    assert localtime.environment(plain) == {}


def test_a_body_runs_in_the_users_zone_unless_it_names_its_own(tmp_path, monkeypatch):
    zoneinfo = tmp_path / "usr/share/zoneinfo"
    (zoneinfo / "Europe").mkdir(parents=True)
    (zoneinfo / "Europe/Berlin").write_bytes(b"TZif")
    link = tmp_path / "localtime"
    link.symlink_to(zoneinfo / "Europe/Berlin")
    monkeypatch.setattr(localtime, "ZONEINFO", zoneinfo)
    monkeypatch.setattr(localtime, "LOCALTIME", link)
    body = Body(id="api", name="api", directory=None, image="python:3.12", dockerfile_name=None,
                build_target=None, context_name=None, working_copy=None, command=None,
                ports=(), runtime=None, shell=None, read_only=False,
                environment={"PORT": "8000"})
    placement = compose.BodyPlacement(instance="api@tab-1", namespace_ref="container:a",
                                      working_copy=Path("/wc"), image="python:3.12",
                                      user="1000:1000")

    (service,) = compose.render(body, placement, 1, "d")["services"].values()
    assert service["environment"] == {"TZ": "Europe/Berlin", "TZDIR": str(zoneinfo),
                                      "PORT": "8000"}
    assert f"{zoneinfo}:{zoneinfo}:ro" in service["volumes"]

    own = dataclasses.replace(body, environment={"TZ": "UTC"})
    (service,) = compose.render(own, placement, 1, "d")["services"].values()
    assert service["environment"]["TZ"] == "UTC"
