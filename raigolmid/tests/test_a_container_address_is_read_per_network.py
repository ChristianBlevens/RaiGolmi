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




def test_a_running_container_whose_init_is_gone_is_dead(monkeypatch):
    """Docker keeps a container `running` while a process in it cannot be killed, after its
    init has exited: the pid it reports names nothing, and the container runs nothing."""
    container = _container("bridge", {})
    container.attrs["State"] = {"Status": "running", "Pid": 2 ** 22 + 1}
    assert _info(container).status == "dead" and not _info(container).running
    import os
    container.attrs["State"] = {"Status": "running", "Pid": os.getpid()}
    assert _info(container).running


def test_a_container_that_will_not_die_names_where_its_threads_wait(tmp_path, monkeypatch):
    """Docker's `did not receive an exit event` is a process SIGKILL does not end: the error
    says each thread left, its state and the kernel function it waits in."""
    import os
    import pytest
    from docker.errors import APIError
    from raigolmid.runtime import docker_runtime
    from raigolmid.runtime.base import RuntimeError_

    group = tmp_path / "system.slice" / "docker-c1.scope"
    group.mkdir(parents=True)
    (group / "cgroup.procs").write_text(f"{os.getpid()}\n")
    monkeypatch.setattr(docker_runtime, "CGROUP", tmp_path)

    class Stuck:
        id = "c1"

        def remove(self, force, v):
            raise APIError("cannot remove container c1: could not kill container: tried to "
                           "kill container, but did not receive an exit event")
    runtime = docker_runtime.DockerRuntime.__new__(docker_runtime.DockerRuntime)
    runtime._cgroup_driver = "systemd"
    runtime._client = SimpleNamespace(containers=SimpleNamespace(get=lambda _n: Stuck()))
    with pytest.raises(RuntimeError_, match=rf"Still in it: {os.getpid()}/\d+ \S+ [RSD] in "):
        runtime.remove("raigolmid-face-x", force=True)
