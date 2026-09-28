"""Docker 29 reports a container's address only under `NetworkSettings.Networks`; the door
forwards to that address, so reading the removed top-level field leaves every anchor without one
and the door never opens."""
from types import SimpleNamespace

from raigolmid.runtime.docker_runtime import _info


def _container(mode: str, networks: dict) -> SimpleNamespace:
    return SimpleNamespace(id="c1", name="raigolmid-anchor-notes-api-tab-3", attrs={
        "State": {"Status": "running"},
        "Config": {"Image": "anchor", "Labels": {}},
        "HostConfig": {"NetworkMode": mode},
        "NetworkSettings": {"Networks": networks},
    })


def test_the_address_is_the_one_on_the_network_it_was_started_on():
    info = _info(_container("bridge", {"bridge": {"IPAddress": "172.17.0.7"}}))
    assert info.ip == "172.17.0.7"


