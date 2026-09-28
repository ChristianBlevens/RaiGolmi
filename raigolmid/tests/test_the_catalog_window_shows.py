"""The catalog window, without a screen: a collapsed section is not part of
a search, the server's popular entries show only while nothing is searched, and download
becomes install becomes delete, with upload on what the user authored."""
from __future__ import annotations

from ui.catalog import model


def _e(kind, name, state, downloads=None, authored=False, description=""):
    return {"kind": kind, "id": name.lower(), "name": name, "description": description,
            "author": "carol", "state": state, "downloads": downloads, "authored": authored}


def test_a_collapsed_section_is_left_out_of_the_search():
    entries = [_e("face", "Calm", "installed"), _e("body", "Calm API", "downloaded")]

    shown = model.sections(entries, "calm", server=False, collapsed={"face"})

    assert shown["face"] == [] and [e["name"] for e in shown["body"]] == ["Calm API"]


def test_the_server_shows_its_most_downloaded_until_a_search_looks_through_all(monkeypatch):
    monkeypatch.setattr(model, "POPULAR", 2)
    entries = [_e("toolbelt", "Mine", "downloaded"),
               _e("toolbelt", "Rust", "server", downloads=5),
               _e("toolbelt", "Go", "server", downloads=50),
               _e("toolbelt", "Zig", "server", downloads=1, description="a compiler")]

    idle = model.sections(entries, "", server=True, collapsed=set())["toolbelt"]
    searched = model.sections(entries, "compiler", server=True, collapsed=set())["toolbelt"]
    hidden = model.sections(entries, "", server=False, collapsed=set())["toolbelt"]

    assert [e["name"] for e in idle] == ["Mine", "Go", "Rust"]
    assert [e["name"] for e in searched] == ["Zig"]
    assert [e["name"] for e in hidden] == ["Mine"]


def test_download_becomes_install_and_upload_is_only_on_the_users_own():
    assert model.buttons(_e("body", "A", "server")) == ("download",)
    assert model.buttons(_e("body", "A", "downloaded")) == ("install", "delete")
    assert model.buttons(_e("body", "A", "downloaded", authored=True)) == (
        "install", "delete", "upload")
    assert model.buttons(_e("body", "A", "installed", authored=True)) == ("delete", "upload")
    assert model.buttons({**_e("body", "A", "downloaded"),
                          "activity": {"what": "installing"}}) == ()
