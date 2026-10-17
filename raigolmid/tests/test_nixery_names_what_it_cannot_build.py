"""A toolbelt's package names are checked by Nixery itself.

Docker reports a name Nixery cannot build as `not found`, with no name; Nixery's registry
answers the manifest with `Could not find Nix packages: [name]`. A refused pull carries that
answer, and the index, a different channel from Nixery's pinned snapshot, refuses nothing.
"""
from __future__ import annotations

import hashlib
import io
import json
import urllib.error
import urllib.request

import pytest

from raigolmid.definitions import Toolbelt
from raigolmid.runtime.base import RuntimeError_
from tests.fakeruntime import FakeRuntime
from raigolmid.toolbelts import (NixeryRefused, ToolbeltResolver, nixery_says,
                                 pull_from_nixery)

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
    with pytest.raises(NixeryRefused) as refused:
        pull_from_nixery(runtime, "nixery.dev/shell/python3/notapackagexyz")
    assert "Nixery says: Could not find Nix packages: [notapackagexyz]" in str(refused.value)
    [request] = nixery
    assert request.full_url == "https://nixery.dev/v2/shell/python3/notapackagexyz/manifests/latest"
    # Cloudflare in front of nixery.dev refuses urllib's own User-Agent (its error 1010).
    assert request.get_header("User-agent") == "raigolmid"


class _Served:
    """nixery.dev's answer for a manifest it serves: HTTP 200, the manifest's bytes."""
    status = 200
    manifest = b'{"schemaVersion":2}'

    def read(self):
        return self.manifest

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_tag_is_pulled_by_the_digest_of_the_manifest_nixery_serves(monkeypatch):
    # Docker refuses the tag's manifest itself: nixery.dev sends it with no Content-Length.
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: _Served())
    runtime = FakeRuntime()
    pulled: list[str] = []
    pull = runtime.pull
    monkeypatch.setattr(runtime, "pull", lambda reference: pulled.append(reference) or pull(
        reference))
    info = pull_from_nixery(runtime, "nixery.dev/shell/grim")
    digest = "sha256:" + hashlib.sha256(_Served.manifest).hexdigest()
    assert pulled == [f"nixery.dev/shell/grim@{digest}"]
    assert runtime.image("nixery.dev/shell/grim") == info
    assert runtime.image("nixery.dev/shell/grim:latest") == info


def test_a_locked_reference_is_asked_by_its_digest(nixery):
    nixery_says("nixery.dev/shell/python3@sha256:abc")
    assert nixery[0].full_url == "https://nixery.dev/v2/shell/python3/manifests/sha256:abc"


def test_the_resolver_refuses_no_name_nixery_has_not_answered(tmp_path):
    toolbelt = Toolbelt(id="t", name="t", directory=tmp_path, supports=(), capabilities=(),
                        packages=("libcap", "python3", "coreutils", "util-linux",
                                  "a-name-no-index-lists"))
    closure = ToolbeltResolver().resolve(toolbelt)
    assert closure.image.endswith("/a-name-no-index-lists")
