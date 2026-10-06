"""The catalog's server: an index of the layers people share, each fetched alone, and a
way to offer one's own.

`Registry` is the whole of what the catalog asks of a server, so a self-hosted one replaces
`GitHubRegistry` without the catalog changing. GitHub serves it from one public repo:

    <kinds>/<id>/…          each entry's files, as `layerfiles.upload_choice` names them
    index.json              every entry, rebuilt by the repo's own workflow on each merge
    Release <kind>-<id>-v<n> one asset, `<kind>-<id>.tar.gz`, the entry's files at its root

A machine never calls GitHub's API to read: unauthenticated, that is 60 requests an hour per
address. It reads `index.json` and the one asset it wants, both plain downloads. Only an
upload uses the API, with the user's token: a fork, one blob per file, one tree, one commit,
one branch and a pull request, because a token cannot add a Release to a repo they do not own.
The workflow packs the asset once the pull request is merged, and the index's author is the
pull request's, so an entry cannot claim somebody else's name.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import stat
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .definitions import PLAIN_ID

KIND_DIRS = {"face": "faces", "toolbelt": "toolbelts", "body": "bodies"}
INDEX_VERSION = 1
# GitHub's limit on one blob; a larger file is refused by name, never truncated.
MAX_FILE_BYTES = 100 * 1024 * 1024
# The one registry, until a self-hosted server replaces it.
OWNER, REPO = "ChristianBlevens", "raigolmi-registry"
API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
FORK_WAIT_SECONDS = 60


class RegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class RegistryEntry:
    kind: str
    id: str
    name: str
    description: str | None
    author: str
    version: int
    asset: str
    sha256: str
    size: int
    downloads: int
    thumbnail: str | None


class Http(Protocol):
    def request(self, method: str, url: str, headers: dict[str, str],
                body: bytes | None = None) -> tuple[int, bytes]: ...


class _SameHostAuthorization(urllib.request.HTTPRedirectHandler):
    """urllib carries every header across a redirect, `Authorization` included, to whatever
    host it names; the user's GitHub token goes only to the host it was sent to."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).hostname != \
                urllib.parse.urlsplit(req.full_url).hostname:
            for name in [k for k in new.headers if k.lower() == "authorization"]:
                del new.headers[name]
        return new


class UrllibHttp:
    """Redirects are followed, which a Release asset's download needs (GitHub answers 302),
    with the credential dropped when one leaves its host."""

    def __init__(self, timeout: float = 60.0) -> None:
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_SameHostAuthorization)

    def request(self, method: str, url: str, headers: dict[str, str],
                body: bytes | None = None) -> tuple[int, bytes]:
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except urllib.error.URLError as exc:
            raise RegistryError(f"{method} {url} did not reach the server: {exc.reason}") from exc


class Registry(Protocol):
    def index(self) -> list[RegistryEntry]: ...
    def fetch(self, entry: RegistryEntry) -> bytes: ...
    def thumbnail(self, entry: RegistryEntry) -> bytes: ...
    def login(self) -> str: ...
    def upload(self, kind: str, layer_id: str, files: dict[str, Path]) -> str: ...


def _entry(raw: dict, where: str) -> RegistryEntry:
    try:
        entry = RegistryEntry(
            kind=raw["kind"], id=raw["id"], name=raw.get("name") or raw["id"],
            description=raw.get("description"), author=raw["author"],
            version=int(raw["version"]), asset=raw["asset"], sha256=raw["sha256"],
            size=int(raw["size"]), downloads=int(raw.get("downloads", 0)),
            thumbnail=raw.get("thumbnail"))
    except (KeyError, TypeError, ValueError) as exc:
        raise RegistryError(f"{where}: an entry is malformed ({exc!r}): {raw!r}") from exc
    if entry.kind not in KIND_DIRS:
        raise RegistryError(f"{where}: entry {entry.id!r} has kind {entry.kind!r}")
    if not isinstance(entry.id, str) or not PLAIN_ID.fullmatch(entry.id):
        raise RegistryError(f"{where}: entry id {entry.id!r} is not a plain layer id")
    return entry


class GitHubRegistry:
    def __init__(self, token: Callable[[], str], owner: str = OWNER, repo: str = REPO,
                 http: Http | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        self.owner, self.repo = owner, repo
        self._token = token
        self.http = http or UrllibHttp()
        self._sleep = sleep
        self._login: str | None = None

    # --- reading: plain downloads, no API ------------------------------------------------
    def index(self) -> list[RegistryEntry]:
        url = f"{RAW}/{self.owner}/{self.repo}/main/index.json"
        status, body = self.http.request("GET", url, {})
        if status != 200:
            raise RegistryError(f"the registry's index at {url} answered {status}: "
                                f"{body[:200].decode(errors='replace')}")
        try:
            raw = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RegistryError(f"the registry's index at {url} is not JSON: {exc}") from exc
        if raw.get("version") != INDEX_VERSION:
            raise RegistryError(f"the registry's index is version {raw.get('version')!r}; "
                                f"this machine reads {INDEX_VERSION}")
        return [_entry(e, url) for e in raw.get("entries", [])]

    def fetch(self, entry: RegistryEntry) -> bytes:
        status, body = self.http.request("GET", entry.asset, {})
        if status != 200:
            raise RegistryError(f"{entry.kind} '{entry.id}' could not be downloaded: "
                                f"{entry.asset} answered {status}")
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        if digest != entry.sha256 or len(body) != entry.size:
            raise RegistryError(
                f"{entry.kind} '{entry.id}' arrived as {len(body)} bytes {digest}; the index "
                f"says {entry.size} bytes {entry.sha256}")
        return body

    def thumbnail(self, entry: RegistryEntry) -> bytes:
        if entry.thumbnail is None:
            raise RegistryError(f"{entry.kind} '{entry.id}' has no thumbnail")
        status, body = self.http.request("GET", entry.thumbnail, {})
        if status != 200:
            raise RegistryError(f"{entry.kind} '{entry.id}''s thumbnail answered {status}")
        return body

    # --- writing: the API, with the user's token -----------------------------------------
    def _api(self, method: str, path: str, payload: dict | None = None,
             ok: tuple[int, ...] = (200, 201)) -> dict:
        headers = {"Authorization": f"Bearer {self._token()}",
                   "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        status, answer = self.http.request(method, f"{API}{path}", headers, body)
        if status not in ok:
            raise RegistryError(f"GitHub refused {method} {path} ({status}): "
                                f"{answer[:300].decode(errors='replace')}")
        return json.loads(answer) if answer else {}

    def login(self) -> str:
        if self._login is None:
            self._login = self._api("GET", "/user")["login"]
        return self._login

    def upload(self, kind: str, layer_id: str, files: dict[str, Path]) -> str:
        """A pull request carrying exactly `files` as the entry, and its address."""
        if kind not in KIND_DIRS:
            raise RegistryError(f"{kind!r} is not a layer kind")
        for name, path in files.items():
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                raise RegistryError(f"{name} is {size} bytes; GitHub takes at most "
                                    f"{MAX_FILE_BYTES} in one file, so it cannot be uploaded")
        login = self.login()
        for entry in self.index():
            if (entry.kind, entry.id) == (kind, layer_id) and entry.author != login:
                raise RegistryError(f"{kind} '{layer_id}' on the server is {entry.author}'s; "
                                    "give yours another id")
        upstream = f"{self.owner}/{self.repo}"
        target = upstream if login == self.owner else self._fork()
        base = self._api("GET", f"/repos/{target}/git/ref/heads/main")["object"]["sha"]
        tree = self._api("GET", f"/repos/{target}/git/commits/{base}")["tree"]["sha"]
        listing = self._api("GET", f"/repos/{target}/git/trees/{tree}?recursive=1")
        if listing.get("truncated"):
            raise RegistryError(f"{target}'s tree is too large to list in one request")
        prefix = f"{KIND_DIRS[kind]}/{layer_id}/"
        entries = []
        for name, path in sorted(files.items()):
            blob = self._api("POST", f"/repos/{target}/git/blobs", {
                "content": base64.b64encode(path.read_bytes()).decode(), "encoding": "base64"})
            executable = path.stat().st_mode & stat.S_IXUSR
            entries.append({"path": prefix + name, "mode": "100755" if executable else "100644",
                            "type": "blob", "sha": blob["sha"]})
        sent = {e["path"] for e in entries}
        entries += [{"path": item["path"], "mode": item["mode"], "type": "blob", "sha": None}
                    for item in listing.get("tree", [])
                    if item["type"] == "blob" and item["path"].startswith(prefix)
                    and item["path"] not in sent]
        new_tree = self._api("POST", f"/repos/{target}/git/trees",
                             {"base_tree": tree, "tree": entries})["sha"]
        commit = self._api("POST", f"/repos/{target}/git/commits", {
            "message": f"{kind} {layer_id}", "tree": new_tree, "parents": [base]})["sha"]
        branch = f"catalog/{kind}-{layer_id}-{int(time.time())}"
        self._api("POST", f"/repos/{target}/git/refs",
                  {"ref": f"refs/heads/{branch}", "sha": commit})
        head = branch if target == upstream else f"{login}:{branch}"
        pull = self._api("POST", f"/repos/{upstream}/pulls", {
            "title": f"{kind} {layer_id}", "head": head, "base": "main",
            "body": f"Uploaded from the catalog: {len(files)} files, "
                    "exactly what the layer's build reads."})
        return pull["html_url"]

    def _fork(self) -> str:
        """The user's fork, created if absent and brought level with the registry's main."""
        fork = self._api("POST", f"/repos/{self.owner}/{self.repo}/forks",
                         {"default_branch_only": True}, ok=(202,))["full_name"]
        waited = 0.0
        while True:
            status, _ = self.http.request("GET", f"{API}/repos/{fork}/git/ref/heads/main",
                                          {"Authorization": f"Bearer {self._token()}"})
            if status == 200:
                break
            if waited >= FORK_WAIT_SECONDS:
                raise RegistryError(f"GitHub had not finished forking into {fork} after "
                                    f"{FORK_WAIT_SECONDS} s")
            self._sleep(2.0)
            waited += 2.0
        self._api("POST", f"/repos/{fork}/merge-upstream", {"branch": "main"})
        return fork


def unpack(data: bytes, destination: Path) -> list[str]:
    """An entry's files into `destination`, which must not exist yet. Names that would land
    outside it, links and devices are refused by tarfile's `data` filter."""
    if destination.exists():
        raise RegistryError(f"{destination} already exists")
    staging = destination.with_name(f".{destination.name}.unpacking")
    if staging.exists():
        raise RegistryError(f"{staging} is left from an earlier download; remove it")
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            names = [m.name for m in tar.getmembers() if m.isfile()]
            tar.extractall(staging, filter="data")
    except (tarfile.TarError, OSError) as exc:
        if staging.exists():
            shutil.rmtree(staging)
        raise RegistryError(f"the entry for {destination.name} could not be unpacked: {exc}") from exc
    os.rename(staging, destination)
    return names
