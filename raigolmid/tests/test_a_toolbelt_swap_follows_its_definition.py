"""A toolbelt is its package list as defined now. A swap to the toolbelt a sandbox already
runs recreates its view when the list has changed since the view was made, and a lock whose
image Nixery no longer holds is resolved again rather than failing every pull."""
from __future__ import annotations

import io
import json
import urllib.request

import pytest

from raigolmid.runtime.base import RuntimeError_
from raigolmid.toolbelts import NixeryLost, nixery_reference, pull_from_nixery

from tests.harness import Harness


@pytest.fixture
def h(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


def _views(h, sandbox):
    return [e for e in h.events_of("view.created") if e.instance == sandbox]


def test_a_swap_to_the_same_toolbelt_rebuilds_on_a_changed_package_list(h):
    sandbox = h.open_sandbox("myapi", "python-dev")
    tab = h.session.intent.face_tab().tab_id
    assert not h.session.check_toolbelt_swap(tab, "python-dev")
    h.session.toolbelt_swap(tab, "python-dev")
    assert len(_views(h, sandbox)) == 1, "an unchanged toolbelt's view was recreated"

    toml = h.session.catalogue.toolbelts["python-dev"].directory / "toolbelt.toml"
    toml.write_text(toml.read_text().replace('"neovim"]', '"neovim", "valgrind"]'))
    h.session.rediscover()
    toolbelt = h.session.catalogue.toolbelts["python-dev"]

    assert h.session.check_toolbelt_swap(tab, "python-dev")
    h.session.toolbelt_swap(tab, "python-dev")

    assert _views(h, sandbox)[-1].data["image"] == nixery_reference(toolbelt.packages)
    lock = json.loads(toolbelt.lock_path.read_text())
    assert lock["package_digest"] == toolbelt.package_digest
    assert "valgrind" in lock["packages"]
    assert not h.session.check_toolbelt_swap(tab, "python-dev")


class _Empty(io.BytesIO):
    """nixery.dev's answer for a digest it no longer keeps: HTTP 200, an empty body."""
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_digest_nixery_no_longer_holds_is_said_so(monkeypatch):
    from tests.fakeruntime import FakeRuntime
    runtime = FakeRuntime()
    monkeypatch.setattr(runtime, "pull", lambda reference: (_ for _ in ()).throw(
        RuntimeError_("unsupported manifest media type and no default available")))
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: _Empty(b""))
    with pytest.raises(NixeryLost, match="no longer holds the manifest"):
        pull_from_nixery(runtime, "nixery.dev/shell/python3@sha256:" + "0" * 64)


def test_a_lock_nixery_no_longer_holds_is_resolved_again(h, monkeypatch):
    sandbox = h.open_sandbox("myapi", "python-dev")
    toolbelt = h.session.catalogue.toolbelts["python-dev"]
    lost = f"{nixery_reference(toolbelt.packages)}@sha256:{'1' * 64}"
    lock = json.loads(toolbelt.lock_path.read_text())
    toolbelt.lock_path.write_text(json.dumps({**lock, "image": lost}))
    pull = h.runtime.pull

    def pulled(reference):
        if reference == lost:
            raise RuntimeError_(f"could not pull '{reference}': 500 Server Error: "
                                "unsupported manifest media type and no default available")
        return pull(reference)

    monkeypatch.setattr(h.runtime, "pull", pulled)
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: _Empty(b""))

    h.session.repair(sandbox)

    (gone,) = h.events_of("toolbelt.lock_lost")
    assert gone.data["image"] == lost and "no longer holds" in gone.data["reason"]
    assert _views(h, sandbox)[-1].data["image"] == nixery_reference(toolbelt.packages)
    relocked = json.loads(toolbelt.lock_path.read_text())
    assert relocked["image"] != lost
    assert relocked["image"].startswith(nixery_reference(toolbelt.packages) + "@sha256:")
