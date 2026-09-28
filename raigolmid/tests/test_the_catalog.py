"""The catalog — a layer's state here, delete taking the images before the definition
and refusing either while something uses it, and a download that is somebody else's."""
from __future__ import annotations

import hashlib
import io
import json
import tarfile

import pytest

from raigolmid import registry
from raigolmid.catalog import QUEUE, Catalog, CatalogError
from tests.fakegithub import FakeGitHub
from tests.harness import Harness


def _settle(h) -> None:
    h.session.queues.run(QUEUE, lambda: None, "settle", timeout=25)


def _state(h, kind, layer_id, server=False):
    for e in h.session.catalog.listing(server)["entries"]:
        if (e["kind"], e["id"]) == (kind, layer_id):
            return e
    return None


def test_delete_takes_the_images_then_the_definition_and_never_what_is_in_use(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    catalog = h.session.catalog
    assert _state(h, "body", "webui")["state"] == "downloaded"

    catalog.install("body", "webui")
    _settle(h)
    assert "catalog.installed" in h.event_types()
    assert _state(h, "body", "webui")["state"] == "installed"

    removed = catalog.delete("body", "webui")["removed"]
    assert removed and all(h.runtime.image(ref) is None for ref in removed)
    assert _state(h, "body", "webui")["state"] == "downloaded"

    directory = h.session.catalogue.bodies["webui"].directory
    catalog.delete("body", "webui")
    assert not directory.exists() and _state(h, "body", "webui") is None

    h.open_sandbox("myapi")
    with pytest.raises(CatalogError, match="in use"):
        catalog.delete("body", "myapi")
    assert _state(h, "body", "myapi")["state"] == "installed"


def _entry_tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_a_download_is_somebody_elses_until_it_is_the_users_and_is_never_uploaded_as_the_users(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    blob = _entry_tar({"toolbelt.toml": b'id = "shared"\npackages = ["bash"]\n'
                                        b'description = "a shell"\n'})
    fake = FakeGitHub("reg", "layers", {"t-bob": "bob"})
    fake.files["https://dl/shared.tar.gz"] = blob
    fake.seed({"index.json": json.dumps({"version": 1, "entries": [{
        "kind": "toolbelt", "id": "shared", "name": "Shared", "description": "a shell",
        "author": "carol", "version": 3, "asset": "https://dl/shared.tar.gz",
        "size": len(blob), "sha256": "sha256:" + hashlib.sha256(blob).hexdigest(),
        "downloads": 12}]}).encode()})
    h.session.catalog = Catalog(h.session, lambda token: registry.GitHubRegistry(
        lambda: "t-bob", owner="reg", repo="layers", http=fake))

    assert _state(h, "toolbelt", "shared") is None
    assert _state(h, "toolbelt", "shared", server=True)["state"] == "server"

    h.session.catalog.download("toolbelt", "shared")

    entry = _state(h, "toolbelt", "shared", server=True)
    assert (entry["state"], entry["author"], entry["authored"], entry["downloads"]) == (
        "downloaded", "carol", False, 12)
    with pytest.raises(CatalogError, match="carol"):
        h.session.catalog.upload_files("toolbelt", "shared")
    assert _state(h, "toolbelt", "python-dev")["authored"] is True
