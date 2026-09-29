"""What nothing names is collected when the daemon starts (`Session.collect_garbage`).

Each edit to a package list makes a new Nixery image and a new closure; the roots are the
daemon's containers, every toolbelt's lock and package list, and every face's apps.
"""
from __future__ import annotations

import json

import pytest

from raigolmid import flakes, labels, naming
from raigolmid.closures import Closures
from raigolmid.events import EventLog
from raigolmid.paths import Paths
from tests.fakeruntime import FakeRuntime
from raigolmid.toolbelts import nixery_reference

from tests.fakes import ClosureCopies
from tests.harness import Harness

KEPT = "nixery.dev/shell/python3/neovim"
DROPPED = "nixery.dev/shell/python3/ripgrep"


@pytest.fixture()
def rig(tmp_path):
    paths = Paths(state=tmp_path / "state", data=tmp_path / "data",
                  config=tmp_path / "config", runtime=tmp_path / "run")
    runtime = FakeRuntime()
    copies = ClosureCopies(runtime)
    runtime.one_shot[naming.closure_copy()] = copies
    return runtime, copies, Closures(runtime, paths, EventLog(tmp_path / "events.jsonl"), 1)


def test_a_closure_nothing_keeps_goes_and_the_paths_it_shared_stay(rig):
    _, copies, closures = rig
    kept_id, kept = closures.of_image(KEPT)
    dropped_id, dropped = closures.of_image(DROPPED)

    assert closures.collect({kept_id}) == (1, 1)

    assert kept.is_dir() and not dropped.exists()
    store = {path.name for path in closures.store.iterdir()} - {".incoming"}
    assert store == set(closures.store_paths(kept))
    assert copies.store_name("python3") in store
    assert copies.store_name("ripgrep") not in store
    # What is left is whole: the kept closure copies nothing again.
    assert closures.of_image(KEPT) == (kept_id, kept)


@pytest.fixture()
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.runtime.one_shot[naming.closure_copy()] = ClosureCopies(harness.runtime)
    return harness


def test_only_what_no_definition_or_container_names_is_collected(h):
    sandbox = h.open_sandbox()
    view_image = next(c.image_id for c in h.runtime.list()
                      if c.labels.get(labels.INSTANCE) == sandbox
                      and c.image.startswith("nixery.dev/"))
    named = {tb_id: h.runtime.pull(nixery_reference(tb.packages)).id
             for tb_id, tb in h.session.catalogue.toolbelts.items()}
    apps = {face_id: h.runtime.pull(h.session.faces.apps_reference(face)).id
            for face_id, face in h.session.catalogue.faces.items()}
    old = h.runtime.pull("nixery.dev/shell/python3/an-edited-away-package")
    elsewhere = h.runtime.add_image("docker.io/library/busybox:latest")
    tb_id, toolbelt = next(iter(h.session.catalogue.toolbelts.items()))
    flake = h.runtime.add_image(f"{flakes.IMAGE_PREFIX}{tb_id}:sha256-current")
    flake_old = h.runtime.add_image(f"{flakes.IMAGE_PREFIX}{tb_id}:sha256-edited-away")
    record = h.session.resolver.flakes / tb_id / "built.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(flakes.Built(
        image=flake.tags[0], nixpkgs="0" * 40, package_digest=toolbelt.package_digest).to_dict()))
    old_closure = h.session.closures.of_image_id(old.id)
    kept_closure = h.session.closures.of_image_id(view_image)

    h.session.collect_garbage()

    present = {image.id for image in h.runtime.list_images()}
    assert old.id not in present and flake_old.id not in present
    assert {view_image, elsewhere.id, flake.id, *named.values(), *apps.values()} <= present
    assert not old_closure.exists() and kept_closure.is_dir()
    [said] = [e.data for e in h.events.tail(1000) if e.type == "garbage.collected"]
    assert said["images"] == 2 and said["closures"] == 1
    assert h.runtime.cache_prunes == 1
