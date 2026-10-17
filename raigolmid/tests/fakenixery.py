"""nixery.dev's registry as the suite has it, answering `urllib.request.urlopen`.

A tag's manifest is served, its bytes standing for the list, as nixery.dev serves one; a list
naming a package in `unbuildable` is refused in Nixery's words. Anything else is the network
the suite does not reach.
"""
from __future__ import annotations

import io
import json
import re
import urllib.error

TAG = re.compile(r"https://nixery\.dev/v2/(?P<repository>[^@]+)/manifests/(?P<tag>[^:]+)$")


class _Manifest:
    status = 200

    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeNixery:
    def __init__(self) -> None:
        self.unbuildable: set[str] = set()

    def urlopen(self, request, *args, **kwargs):
        asked = TAG.match(request.full_url)
        if asked is None:
            raise urllib.error.URLError("the test suite reaches no network")
        packages = asked["repository"].split("/")[1:]
        refused = sorted(self.unbuildable & set(packages))
        if refused:
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, io.BytesIO(
                json.dumps({"errors": [{"code": "MANIFEST_UNKNOWN", "message":
                    f"Could not find Nix packages: [{' '.join(refused)}]"}]}).encode()))
        return _Manifest(json.dumps({"schemaVersion": 2, "list": asked["repository"],
                                     "tag": asked["tag"]}).encode())
