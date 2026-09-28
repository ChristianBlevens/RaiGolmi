"""A toolbelt's package names are checked by Nixery itself.

Docker reports a name Nixery cannot build as `not found`, with no name; Nixery's registry
answers the manifest with `Could not find Nix packages: [name]`. A refused pull carries that
answer, and the index, a different channel from Nixery's pinned snapshot, refuses nothing.
"""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from raigolmid.definitions import Toolbelt
from raigolmid.runtime.base import RuntimeError_
from tests.fakeruntime import FakeRuntime
from raigolmid.toolbelts import ToolbeltResolver, nixery_says, pull_from_nixery

# Nixery's answer for a name it cannot build, as nixery.dev gives it.
REFUSED = {"errors": [{"code": "MANIFEST_UNKNOWN",
                       "message": "Could not find Nix packages: [notapackagexyz]"}]}


@pytest.fixture
def nixery(monkeypatch):
    asked: list[urllib.request.Request] = []

    def urlopen(request, timeout):
        asked.append(request)
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {},
                                     io.BytesIO(json.dumps(REFUSED).encode()))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return asked


def test_a_refused_pull_carries_nixerys_answer_naming_the_package(nixery):
    runtime = FakeRuntime()
    runtime.unbuildable.add("notapackagexyz")
    with pytest.raises(RuntimeError_) as refused:
        pull_from_nixery(runtime, "nixery.dev/shell/python3/notapackagexyz")
    assert "not found" in str(refused.value)
    assert "Nixery says: Could not find Nix packages: [notapackagexyz]" in str(refused.value)
    [request] = nixery
    assert request.full_url == "https://nixery.dev/v2/shell/python3/notapackagexyz/manifests/latest"


def test_a_locked_reference_is_asked_by_its_digest(nixery):
    nixery_says("nixery.dev/shell/python3@sha256:abc")
    assert nixery[0].full_url == "https://nixery.dev/v2/shell/python3/manifests/sha256:abc"


def test_the_resolver_refuses_no_name_nixery_has_not_answered(tmp_path):
    toolbelt = Toolbelt(id="t", name="t", directory=tmp_path, supports=(), capabilities=(),
                        packages=("libcap", "python3", "coreutils", "util-linux",
                                  "a-name-no-index-lists"))
    closure = ToolbeltResolver().resolve(toolbelt)
    assert closure.image.endswith("/a-name-no-index-lists")
