"""What Docker answers is read by status, not by its wording (`runtime/docker_runtime.py`)."""
from __future__ import annotations

from types import SimpleNamespace

import requests
from docker.errors import create_api_error_from_http_exception

from raigolmid.runtime.docker_runtime import DockerRuntime


def _404(message: str):
    response = requests.Response()
    response.status_code = 404
    response._content = ('{"message": "%s"}' % message).encode()
    try:
        create_api_error_from_http_exception(requests.HTTPError(response=response))
    except Exception as exc:          # noqa: BLE001 — the error docker-py makes of it
        return exc


def test_an_image_already_gone_is_removed_whatever_docker_calls_it():
    """Measured on the VM: `image "docker.io/…": not found` is a bare `NotFound`, which
    aborted an agent restart while the image it named was already gone."""
    for message in ('No such image: raigolmi/claude:x',
                    'image "docker.io/raigolmi/claude:x": not found'):
        error = _404(message)

        def remove(reference, force):
            raise error
        runtime = DockerRuntime(client=SimpleNamespace(images=SimpleNamespace(remove=remove)))
        runtime.remove_image("raigolmi/claude:x")


def test_an_image_removed_while_the_images_are_listed_is_not_listed():
    """Measured on the VM: a `select` that opened a tab failed on `could not list images: 404
    … No such image`, raised by docker-py inspecting a listed image that a build had just
    replaced — and the face never followed the selection."""
    kept = SimpleNamespace(id="sha256:kept", tags=["a:1"], attrs={"Config": {"Labels": {}}})

    def get(image_id):
        if image_id == "sha256:gone":
            raise _404(f"No such image: {image_id}")
        return kept
    runtime = DockerRuntime(client=SimpleNamespace(
        api=SimpleNamespace(images=lambda filters: [{"Id": "sha256:gone"},
                                                    {"Id": "sha256:kept"}]),
        images=SimpleNamespace(get=get)))

    assert [i.id for i in runtime.list_images()] == ["sha256:kept"]
