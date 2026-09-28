"""A GitHub for `registry.GitHubRegistry` that refuses what GitHub refuses: an API call without a
token, an endpoint it does not serve, a ref or object that does not exist, a pull request from
a branch that does not exist, and a tree deleting a path its base does not hold. Objects are
kept as git keeps them, so a commit's tree is what a later read of the repo finds."""
from __future__ import annotations

import base64
import hashlib
import json
import re

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"


def _sha(kind: str, payload) -> str:
    return hashlib.sha1(f"{kind}:{json.dumps(payload, sort_keys=True, default=bytes.hex)}".encode()).hexdigest()


class FakeGitHub:
    def __init__(self, owner: str, repo: str, users: dict[str, str]) -> None:
        self.owner, self.repo = owner, repo
        self.users = users                      # token -> login
        self.objects: dict[str, dict] = {}
        self.repos: dict[str, dict[str, str]] = {}      # full name -> refs
        self.files: dict[str, bytes] = {}               # url -> body, for plain downloads
        self.pulls: list[dict] = []
        self.calls: list[tuple[str, str]] = []
        root = self._commit({}, [])
        self.repos[f"{owner}/{repo}"] = {"heads/main": root}

    # --- git ---------------------------------------------------------------------------
    def _put(self, kind: str, payload) -> str:
        sha = _sha(kind, payload)
        self.objects[sha] = {"kind": kind, "payload": payload}
        return sha

    def _commit(self, files: dict[str, tuple[str, str]], parents: list[str]) -> str:
        tree = self._put("tree", files)
        return self._put("commit", {"tree": tree, "parents": parents})

    def tree_of(self, full: str, ref: str = "heads/main") -> dict[str, bytes]:
        commit = self.objects[self.repos[full][ref]]["payload"]
        files = self.objects[commit["tree"]]["payload"]
        return {p: self.objects[sha]["payload"]["data"] for p, (_, sha) in files.items()}

    def seed(self, files: dict[str, bytes]) -> None:
        full = f"{self.owner}/{self.repo}"
        head = self.objects[self.repos[full]["heads/main"]]["payload"]
        tree = dict(self.objects[head["tree"]]["payload"])
        tree.update({p: ("100644", self._put("blob", {"data": d})) for p, d in files.items()})
        self.repos[full]["heads/main"] = self._commit(tree, [self.repos[full]["heads/main"]])

    # --- HTTP --------------------------------------------------------------------------
    def request(self, method, url, headers, body=None):
        self.calls.append((method, url))
        if url in self.files and method == "GET":
            return 200, self.files[url]
        if url.startswith(RAW):
            owner, repo, ref, path = url[len(RAW) + 1:].split("/", 3)
            full = f"{owner}/{repo}"
            if full not in self.repos:
                return 404, b"404: Not Found"
            found = self.tree_of(full, f"heads/{ref}").get(path)
            return (200, found) if found is not None else (404, b"404: Not Found")
        if not url.startswith(API):
            return 404, b"not found"
        auth = headers.get("Authorization", "")
        login = self.users.get(auth.removeprefix("Bearer "))
        if login is None:
            return 401, b'{"message":"Bad credentials"}'
        path = url[len(API):]
        payload = json.loads(body) if body else None
        for pattern, handler in self._routes():
            m = re.fullmatch(pattern, f"{method} {path}")
            if m:
                return handler(login, payload, *m.groups())
        return 404, json.dumps({"message": f"Not Found: {method} {path}"}).encode()

    def _routes(self):
        r = r"([^/]+/[^/]+)"
        return [
            (r"GET /user", lambda login, _: (200, json.dumps({"login": login}).encode())),
            (rf"POST /repos/{r}/forks", self._fork),
            (rf"GET /repos/{r}/git/ref/heads/(.+)", self._get_ref),
            (rf"POST /repos/{r}/merge-upstream", self._merge_upstream),
            (rf"GET /repos/{r}/git/commits/(\w+)", self._get_commit),
            (rf"GET /repos/{r}/git/trees/(\w+)\?recursive=1", self._get_tree),
            (rf"POST /repos/{r}/git/blobs", self._blob),
            (rf"POST /repos/{r}/git/trees", self._tree),
            (rf"POST /repos/{r}/git/commits", self._new_commit),
            (rf"POST /repos/{r}/git/refs", self._new_ref),
            (rf"POST /repos/{r}/pulls", self._pull),
        ]

    def _missing(self, what):
        return 404, json.dumps({"message": f"Not Found: {what}"}).encode()

    def _fork(self, login, _, full):
        if full not in self.repos:
            return self._missing(full)
        fork = f"{login}/{full.split('/')[1]}"
        self.repos.setdefault(fork, dict(self.repos[full]))
        return 202, json.dumps({"full_name": fork}).encode()

    def _get_ref(self, login, _, full, name):
        sha = self.repos.get(full, {}).get(f"heads/{name}")
        if sha is None:
            return self._missing(f"{full} heads/{name}")
        return 200, json.dumps({"object": {"sha": sha}}).encode()

    def _merge_upstream(self, login, payload, full):
        if full.split("/")[0] != login:
            return 403, b'{"message":"not your fork"}'
        upstream = f"{self.owner}/{self.repo}"
        self.repos[full][f"heads/{payload['branch']}"] = self.repos[upstream][f"heads/{payload['branch']}"]
        return 200, b'{"merge_type":"fast-forward"}'

    def _get_commit(self, login, _, full, sha):
        obj = self.objects.get(sha)
        if full not in self.repos or obj is None or obj["kind"] != "commit":
            return self._missing(sha)
        return 200, json.dumps({"sha": sha, "tree": {"sha": obj["payload"]["tree"]}}).encode()

    def _get_tree(self, login, _, full, sha):
        obj = self.objects.get(sha)
        if obj is None or obj["kind"] != "tree":
            return self._missing(sha)
        items = [{"path": p, "mode": mode, "type": "blob", "sha": b}
                 for p, (mode, b) in obj["payload"].items()]
        return 200, json.dumps({"sha": sha, "tree": items, "truncated": False}).encode()

    def _writable(self, login, full):
        return full in self.repos and full.split("/")[0] == login

    def _blob(self, login, payload, full):
        if not self._writable(login, full):
            return 403, b'{"message":"Resource not accessible"}'
        if payload.get("encoding") != "base64":
            return 422, b'{"message":"encoding"}'
        sha = self._put("blob", {"data": base64.b64decode(payload["content"])})
        return 201, json.dumps({"sha": sha}).encode()

    def _tree(self, login, payload, full):
        if not self._writable(login, full):
            return 403, b'{"message":"Resource not accessible"}'
        base = self.objects.get(payload.get("base_tree"), {"kind": "tree", "payload": {}})
        files = dict(base["payload"])
        for e in payload["tree"]:
            if e["mode"] not in ("100644", "100755") or e["type"] != "blob":
                return 422, b'{"message":"bad tree entry"}'
            if e["sha"] is None:
                if e["path"] not in files:
                    return 422, b'{"message":"GitRPC::BadObjectState"}'
                del files[e["path"]]
            elif e["sha"] not in self.objects:
                return 422, b'{"message":"tree.sha is not a valid blob"}'
            else:
                files[e["path"]] = (e["mode"], e["sha"])
        return 201, json.dumps({"sha": self._put("tree", files)}).encode()

    def _new_commit(self, login, payload, full):
        if not self._writable(login, full):
            return 403, b'{"message":"Resource not accessible"}'
        if payload["tree"] not in self.objects or any(p not in self.objects for p in payload["parents"]):
            return 422, b'{"message":"invalid tree or parent"}'
        sha = self._put("commit", {"tree": payload["tree"], "parents": payload["parents"]})
        return 201, json.dumps({"sha": sha}).encode()

    def _new_ref(self, login, payload, full):
        if not self._writable(login, full):
            return 403, b'{"message":"Resource not accessible"}'
        name = payload["ref"].removeprefix("refs/")
        if name in self.repos[full] or payload["sha"] not in self.objects:
            return 422, b'{"message":"Reference already exists or bad sha"}'
        self.repos[full][name] = payload["sha"]
        return 201, json.dumps({"ref": payload["ref"]}).encode()

    def _pull(self, login, payload, full):
        owner, _, branch = payload["head"].rpartition(":")
        source = f"{owner or full.split('/')[0]}/{full.split('/')[1]}"
        if f"heads/{branch}" not in self.repos.get(source, {}):
            return 422, b'{"message":"Validation Failed: head"}'
        number = len(self.pulls) + 1
        self.pulls.append({"user": login, "head": payload["head"], "source": source,
                           "branch": branch, "title": payload["title"]})
        return 201, json.dumps({"html_url": f"https://github.com/{full}/pull/{number}"}).encode()
