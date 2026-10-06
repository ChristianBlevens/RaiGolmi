"""The agent's documents: a layer doc is stale by the layer's own files."""
from __future__ import annotations

import os
import subprocess

from raigolmid import documents


def _write(path, text, mtime):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))


def test_a_layer_doc_is_stale_by_what_changed_after_it_and_nothing_the_daemon_writes(tmp_path):
    layer = tmp_path / "python"
    _write(layer / "LAYER.md", "doc", 100)
    _write(layer / "toolbelt.toml", "old", 50)
    _write(layer / "toolbelt.lock", "resolved later", 200)
    _write(layer / ".cache" / "x", "hidden", 200)
    assert documents.changed_after(layer / "LAYER.md", layer) == []
    _write(layer / "config" / "init.lua", "edited", 150)
    assert documents.changed_after(layer / "LAYER.md", layer) == ["config/init.lua"]


def test_in_a_repository_ignored_output_is_not_the_layer(tmp_path):
    layer = tmp_path / "body"
    subprocess.run(["git", "init", "-q", str(layer)], check=True)
    _write(layer / ".gitignore", "build/\n", 50)
    _write(layer / "LAYER.md", "doc", 100)
    _write(layer / "build" / "out.o", "built", 200)
    assert documents.changed_after(layer / "LAYER.md", layer) == []
    _write(layer / "Dockerfile", "FROM x", 200)
    assert documents.changed_after(layer / "LAYER.md", layer) == ["Dockerfile"]


def test_a_dead_path_line_or_symbol_is_named_and_a_word_is_never_a_claim(tmp_path):
    layer = tmp_path / "python"
    _write(layer / "toolbelt.toml", "packages = []\n[tool]\nname = 1\n", 50)
    _write(layer / "LAYER.md",
           "Built from `toolbelt.toml` and `toolbelt.toml:3`, which sets `toolbelt.toml:name`.\n"
           "It once read `gone.nix`, `toolbelt.toml:9`, `toolbelt.toml:missing_key` and "
           "`/definitions/bodies/api/body.toml` and `/work/toolbelt.toml`, served from `/work`.\nCall `status`, run `index`, see "
           "`https://example.com/x.md`, `/usr/bin/env`, `*.py`.\n", 100)
    dead = documents.dead_references(layer / "LAYER.md", {"/definitions": tmp_path})
    assert dead == ["`gone.nix`: no such path", "`toolbelt.toml:9`: the file has 3 lines",
                    "`toolbelt.toml:missing_key`: `missing_key` is not in the file",
                    "`/definitions/bodies/api/body.toml`: no such path",
                    "`/work/toolbelt.toml`: `/work` is a different directory to each agent; "
                    "cite it relative to this doc's directory or under `/definitions`"]


def test_a_record_of_failures_is_held_to_its_budget_and_never_to_what_it_names(tmp_path):
    _write(tmp_path / "patterns.md", "<!-- purpose: failures\nnot-here: fixes in progress\n"
           "shape: log\naudited: 90 2026-10-02\n-->\n`python-version.nix` was missing\n", 100)
    assert documents.maintenance(tmp_path / "patterns.md", 1024, None) == []


def test_a_header_serves_only_whole_and_an_archive_never_grows_into_an_audit():
    head = "<!-- purpose: p\nnot-here: n\nshape: {shape}\naudited: {audited}\n-->\n"
    assert documents.header_reasons("# Title\n", 8) == [
        "header: it does not open with its purpose header"]
    assert documents.header_reasons("<!-- purpose: p\nshape: log\n-->", 30) == [
        "header: its header has no not-here, audited"]
    assert documents.header_reasons(head.format(shape="notes", audited="1 2026-10-02"), 60) == [
        "header: its shape `notes` is not one of bounded, log, archive"]
    assert documents.header_reasons(head.format(shape="log", audited="10000 2026-10-02"),
                                    14000) == []
    assert documents.header_reasons(head.format(shape="log", audited="10000 2026-10-02"),
                                    15000)[0].startswith("grown since its audit: from 10000")
    assert documents.header_reasons(head.format(shape="archive", audited="1 2026-10-02"),
                                    10 ** 6) == []
    skill = "---\nname: s\ndescription: d\n---\n"
    assert documents.header_reasons(skill + head.format(shape="bounded", audited="100 2026-10-05"),
                                    100) == [], "a skill's header follows its frontmatter"
    assert documents.header_reasons(skill + "# Title\n", 40) == [
        "header: it does not open with its purpose header"]


def test_a_slash_alone_is_no_claim_and_the_daemons_lock_is_named_before_it_exists(tmp_path):
    layer = tmp_path / "python"
    _write(layer / "src" / "main.go", "package main\n", 50)
    _write(layer / "LAYER.md",
           "Its branch is `agent/tab-2`; it imports `runtime/cgo`; the face runs "
           "`raigolmi/face-sway:latest` and keeps `btop/`. `toolbelt.lock` pins it. It builds "
           "`src/main.go`, once `src/old.go` and `src/gone/`, and `toolbelts/python.nix`.\n", 100)
    dead = documents.dead_references(layer / "LAYER.md", {"/definitions": tmp_path})
    assert dead == ["`src/old.go`: no such path", "`src/gone/`: no such path",
                    "`toolbelts/python.nix`: no such path"]


