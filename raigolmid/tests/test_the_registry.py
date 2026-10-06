"""The catalog's server — an index read without the API, one entry fetched and checked,
and an upload that is a pull request carrying exactly the entry's files."""
from __future__ import annotations

import hashlib
import io
import json
import tarfile

import pytest

from raigolmid import registry
from tests.fakegithub import FakeGitHub


def _registry(fake, token="t-bob"):
    return registry.GitHubRegistry(lambda: token, owner="reg", repo="layers", http=fake,
                                   sleep=lambda _: None)


def _index(fake, entries):
    fake.seed({"index.json": json.dumps({"version": 1, "entries": entries}).encode()})


def _files(tmp_path, names):
    out = {}
    for name, text in names.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        out[name] = path
    return out


def test_an_upload_is_a_pull_request_from_the_users_fork_carrying_exactly_the_entry(tmp_path):
    fake = FakeGitHub("reg", "layers", {"t-bob": "bob"})
    fake.seed({"bodies/web/body.toml": b"old", "bodies/web/stale.txt": b"gone",
               "faces/other/face.toml": b"kept"})
    _index(fake, [])
    files = _files(tmp_path, {"body.toml": 'id = "web"\n', "app/main.py": "print(1)\n"})

    url = _registry(fake).upload("body", "web", files)

    assert url == "https://github.com/reg/layers/pull/1"
    pull = fake.pulls[0]
    assert pull["user"] == "bob" and pull["head"].startswith("bob:catalog/body-web-")
    sent = fake.tree_of("bob/layers", f"heads/{pull['branch']}")
    assert {p: d for p, d in sent.items() if p.startswith("bodies/web/")} == {
        "bodies/web/body.toml": b'id = "web"\n', "bodies/web/app/main.py": b"print(1)\n"}
    assert sent["faces/other/face.toml"] == b"kept"
    assert fake.tree_of("reg/layers")["bodies/web/body.toml"] == b"old", "main is untouched"


def test_an_entry_on_the_server_that_is_someone_elses_is_refused_before_anything_is_sent(tmp_path):
    fake = FakeGitHub("reg", "layers", {"t-bob": "bob"})
    _index(fake, [{"kind": "body", "id": "web", "author": "carol", "version": 1,
                   "asset": "https://x/y.tar.gz", "sha256": "sha256:0", "size": 1}])
    files = _files(tmp_path, {"body.toml": ""})

    with pytest.raises(registry.RegistryError, match="carol's"):
        _registry(fake).upload("body", "web", files)
    assert not any(m == "POST" for m, _ in fake.calls)


def test_a_file_github_cannot_hold_is_refused_by_name(tmp_path, monkeypatch):
    fake = FakeGitHub("reg", "layers", {"t-bob": "bob"})
    files = _files(tmp_path, {"body.toml": "", "big.bin": "x" * 11})
    monkeypatch.setattr(registry, "MAX_FILE_BYTES", 10)

    with pytest.raises(registry.RegistryError, match="big.bin"):
        _registry(fake).upload("body", "web", files)
    assert fake.calls == []


def test_a_download_is_checked_against_the_index_and_unpacked_inside_its_directory(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b'id = "web"\n'
        info = tarfile.TarInfo("body.toml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    blob = buf.getvalue()
    fake = FakeGitHub("reg", "layers", {})
    fake.files["https://dl/web.tar.gz"] = blob
    good = {"kind": "body", "id": "web", "author": "bob", "version": 2,
            "asset": "https://dl/web.tar.gz", "size": len(blob),
            "sha256": "sha256:" + hashlib.sha256(blob).hexdigest()}
    _index(fake, [good])
    reg = _registry(fake, token="none")

    [entry] = reg.index()
    assert registry.unpack(reg.fetch(entry), tmp_path / "web") == ["body.toml"]
    assert (tmp_path / "web" / "body.toml").read_bytes() == data

    fake.files["https://dl/web.tar.gz"] = blob + b"tampered"
    with pytest.raises(registry.RegistryError, match="the index says"):
        reg.fetch(entry)


def test_an_entry_naming_a_path_outside_its_directory_is_refused(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("../escape")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))

    with pytest.raises(registry.RegistryError):
        registry.unpack(buf.getvalue(), tmp_path / "web")
    assert not (tmp_path / "escape").exists()
    assert not (tmp_path / "web").exists() and not (tmp_path / ".web.unpacking").exists()


def test_the_token_never_follows_a_redirect_to_another_host():
    """A Release asset's download is a redirect GitHub answers to its CDN; the user's token
    goes only to the host it was sent to."""
    import http.server
    import threading
    seen: dict[str, str | None] = {}

    class Answer(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen[f"{self.server.server_address[0]}{self.path}"] = self.headers.get("Authorization")
            if self.path == "/asset":
                self.send_response(302)
                self.send_header("Location", self.server.onward)
            else:
                self.send_response(200)
            self.end_headers()

        def log_message(self, *_):
            pass

    here = http.server.HTTPServer(("127.0.0.1", 0), Answer)
    elsewhere = http.server.HTTPServer(("127.0.0.2", 0), Answer)
    for server in (here, elsewhere):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for onward in (f"http://127.0.0.2:{elsewhere.server_port}/cdn",
                       f"http://127.0.0.1:{here.server_port}/same"):
            here.onward = onward
            status, _ = registry.UrllibHttp().request(
                "GET", f"http://127.0.0.1:{here.server_port}/asset", {"Authorization": "Bearer t"})
            assert status == 200
        assert seen == {"127.0.0.1/asset": "Bearer t", "127.0.0.2/cdn": None,
                        "127.0.0.1/same": "Bearer t"}
    finally:
        here.shutdown()
        elsewhere.shutdown()


def test_an_index_entry_whose_id_is_not_a_plain_id_is_refused():
    """The id names the directory a download unpacks into."""
    raw = {"kind": "face", "author": "a", "version": 1, "asset": "x", "sha256": "0", "size": 1}
    for bad in ("..", "../../.ssh", "a/b", ".hidden"):
        with pytest.raises(registry.RegistryError, match="plain layer id"):
            registry._entry({**raw, "id": bad}, "index.json")
    assert registry._entry({**raw, "id": "writing"}, "index.json").id == "writing"
