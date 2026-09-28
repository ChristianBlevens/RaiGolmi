"""The daemon's images are built by BuildKit.

The SDK's `images.build` names no builder, so the Engine builds with the classic one and
refuses `RUN --mount`. There is no Docker here to build with, so it is held statically: the
runtime's build goes through `docker build` and never through the SDK.
"""
from __future__ import annotations

import inspect

from raigolmid.runtime.docker_runtime import DockerRuntime


def test_the_runtime_builds_through_the_cli_and_never_the_sdk():
    source = inspect.getsource(DockerRuntime.build)
    assert '"docker", "build"' in source
    assert "images.build" not in inspect.getsource(DockerRuntime)
