"""`rai export` / `rai import` (`machinecopy.py`): what is this machine's own lands on another,
no credential ever travels, a body comes back from its origin with its unpushed commits and
RaiGolmi's own files, and the machine tab there starts with this one's memory."""
import json
import socket
import subprocess
import tarfile
from pathlib import Path

import pytest

from raigolmid import machinecopy, settings
from raigolmid.definitions import SearchPaths
from raigolmid.paths import Paths
from tests.harness import Harness

@pytest.fixture()
def h(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


SECRETS = ("agent-credentials", "registry-token", "claude-login.json", "proxy-secret",
           "proxy-ca", ".credentials.json", "id_ed25519")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                           "-c", "init.defaultBranch=main", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def machine(root: Path) -> tuple[Paths, SearchPaths]:
    paths = Paths(state=root / "state", data=root / "data",
                  config=root / "config" / "raigolmid", runtime=root / "run")
    layers = root / "raigolmi"
    return paths, SearchPaths(faces=(layers / "faces",), toolbelts=(layers / "toolbelts",),
                              bodies=(layers / "bodies",), private=())


def write(path: Path, text: str = "x\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def origin_with_a_body(tmp_path: Path, bodies: Path) -> tuple[Path, str]:
    """A bare "origin" holding one pushed commit, and the body cloned from it with a commit
    on no remote, RaiGolmi's files ignored by `.git/info/exclude`, an untracked file and
    build output its `.gitignore` names. Returns the origin and the body's HEAD."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    seed = tmp_path / "seed"
    git(tmp_path, "init", "-q", str(seed))
    write(seed / ".gitignore", "target/\n")
    write(seed / "README.md", "pushed\n")
    git(seed, "add", ".")
    git(seed, "commit", "-qm", "pushed")
    git(seed, "push", "-q", str(origin), "main")
    body = bodies / "proj"
    bodies.mkdir(parents=True)
    git(bodies, "clone", "-q", str(origin), "proj")
    write(body / "src.txt", "unpushed\n")
    git(body, "add", "src.txt")
    git(body, "commit", "-qm", "unpushed")
    with (body / ".git" / "info" / "exclude").open("a") as fh:
        fh.write("body.toml\nLAYER.md\n.wt/\ntarget/\n")
    write(body / "body.toml", 'id = "proj"\n')
    write(body / "LAYER.md", "the layer\n")
    write(body / ".wt" / "scratch.py", "print()\n")
    write(body / "notes.txt", "untracked\n")
    write(body / "target" / "debug" / "out", "built\n")
    return origin, git(body, "rev-parse", "HEAD")


def a_source(tmp_path: Path) -> tuple[Paths, SearchPaths, Path, str]:
    paths, search = machine(tmp_path / "src")
    write(paths.preferences, "prefers short answers\n")
    write(paths.permissions, "always: git\n")
    write(paths.history, '{"entries": []}')
    write(paths.messages, "[]")
    write(paths.settings, settings.SHIPPED.read_text(encoding="utf-8"))
    write(paths.agent_templates / "claude.md", "template\n")
    write(paths.agent_plugins / "mine" / "plugin.json", "{}")
    write(paths.janitor_documents / "patterns.md", "patterns\n")
    write(paths.janitor_documents / "incidents" / "fixed" / "one.md", "fixed\n")
    write(paths.job_ledger("proj"), '{"job": 1}\n')
    write(paths.principles_ledger("proj"), '{"commit": 1}\n')
    write(paths.principles, '{"bodies": {}}')
    write(paths.work / "SESSION-START.md", "the direction\n")
    write(paths.work / "archive" / "old.md", "old plan\n")
    write(paths.runs / "run-1.md", "a run\n")
    # Credentials: where they live, and planted inside directories that are carried.
    for secret in (paths.agent_credentials, paths.registry_token, paths.claude_login,
                   paths.proxy_secret, paths.proxy_authority):
        write(secret, "secret\n")
    write(paths.work / ".ssh" / "id_ed25519", "secret\n")
    # The live machine tab, and a closed body tab in the archive.
    write(paths.intent, json.dumps({"tabs": {"tab-7": {"tab_id": "tab-7", "body": None}}}))
    live = paths.agent_homes / "tab-7"
    write(live / "thoughts.md", "machine thoughts\n")
    write(live / ".claude" / "projects" / "-work" / "memory" / "MEMORY.md", "- direction\n")
    write(live / ".claude" / "projects" / "-work" / "memory" / ".credentials.json", "secret\n")
    write(live / ".claude" / "projects" / "-work" / "conversation.jsonl", "{}\n")
    write(live / ".claude" / ".credentials.json", "secret\n")
    write(live / ".cache" / "big", "cache\n")
    closed = paths.agent_archive / "tab-3-20261001T100000"
    write(closed / "thoughts.md", "body thoughts\n")
    write(closed / ".claude" / "projects" / "-work" / "memory" / "MEMORY.md", "- body\n")
    write(closed / ".claude" / "projects" / "-work" / "old.jsonl", "{}\n")
    write(paths.agent_archive / "tab-3-20261001T100000.json",
          json.dumps({"tab": "tab-3", "body": "proj"}))
    # The face home: the user's things, beside caches and sign-ins.
    write(paths.face_home / "Daybook" / "today.md", "notes\n")
    user = paths.face_home / ".faces" / "work" / "config" / "VSCodium" / "User"
    write(user / "settings.json", "{}")
    write(user / "globalStorage" / "state.vscdb", "secret\n")
    write(paths.face_home / ".faces" / "work" / "config" / "mozilla" / "logins.json", "secret\n")
    write(paths.face_home / ".cache" / "fontconfig" / "x", "cache\n")
    write(paths.face_home / ".pki" / "nssdb" / "cert9.db", "secret\n")
    (paths.face_home / "Transfer").mkdir()
    write(search.faces[0] / "work" / "face.toml", 'id = "work"\n')
    write(search.toolbelts[0] / "rust" / "toolbelt.toml", 'id = "rust"\n')
    origin, head = origin_with_a_body(tmp_path, search.bodies[0])
    return paths, search, origin, head


def test_everything_its_own_lands_and_no_credential_travels(tmp_path):
    paths, search, origin, head = a_source(tmp_path)
    made = machinecopy.export(paths, search, tmp_path / "Transfer" / "out")
    assert made.parent == tmp_path / "Transfer" / "out"
    assert made.name.startswith("raigolmi-machine-") and made.name.endswith("Z.tar.gz")
    assert [p.name for p in made.parent.parent.iterdir()] == ["out"], "made beside, moved in"
    with tarfile.open(made) as tar:
        names = tar.getnames()
    assert not [n for n in names if any(s in n for s in SECRETS)], names
    assert not [n for n in names if n.endswith(".jsonl") and "ledgers" not in n
                and "principles" not in n], "no transcript"
    manifest = json.loads(tarfile.open(made).extractfile("manifest.json").read())
    assert {w["path"] for w in manifest["withheld"]} >= {
        "data/work/.ssh", "data/face-home/.cache", "data/face-home/.pki",
        "data/face-home/Transfer"}
    assert manifest["bodies"]["proj"]["origin"] == str(origin)
    assert manifest["bodies"]["proj"]["head"] == head

    target, there = machine(tmp_path / "dst")
    done = machinecopy.import_archive(target, there, made)

    for source, landed in ((paths.preferences, target.preferences),
                           (paths.permissions, target.permissions),
                           (paths.history, target.history), (paths.messages, target.messages),
                           (paths.settings, target.settings)):
        assert landed.read_text() == source.read_text()
    assert (target.agent_templates / "claude.md").is_file()
    assert (target.agent_plugins / "mine" / "plugin.json").is_file()
    assert (target.janitor_documents / "incidents" / "fixed" / "one.md").is_file()
    assert target.job_ledger("proj").is_file() and target.principles_ledger("proj").is_file()
    assert not target.principles.exists(), "a machine's own counters stay with it"
    assert (target.work / "SESSION-START.md").read_text() == "the direction\n"
    assert (target.work / "archive" / "old.md").is_file()
    assert (target.runs / "run-1.md").is_file()
    assert (there.faces[0] / "work" / "face.toml").is_file()
    assert (there.toolbelts[0] / "rust" / "toolbelt.toml").is_file()
    assert (target.face_home / "Daybook" / "today.md").is_file()
    assert (target.face_home / ".faces" / "work" / "config" / "VSCodium" / "User"
            / "settings.json").is_file()
    for absent in (".cache", ".pki", "Transfer", ".faces/work/config/mozilla",
                   ".faces/work/config/VSCodium/User/globalStorage"):
        assert not (target.face_home / absent).exists(), absent

    # Lessons: the archived tab as it was, the live one as the entry its close would make.
    archived = target.agent_archive / "tab-3-20261001T100000"
    assert (archived / "thoughts.md").read_text() == "body thoughts\n"
    assert (archived / ".claude" / "projects" / "-work" / "memory" / "MEMORY.md").is_file()
    assert not (archived / ".claude" / "projects" / "-work" / "old.jsonl").exists()
    record = json.loads((target.agent_archive / "tab-3-20261001T100000.json").read_text())
    assert record["body"] == "proj" and record["imported"]["machine"]
    [live] = target.agent_archive.glob("tab-7-*.json")
    assert json.loads(live.read_text())["body"] is None
    assert (live.with_suffix("") / "thoughts.md").read_text() == "machine thoughts\n"
    assert done["machine_memory"] is None, "no machine tab here to give it to yet"

    # The body: cloned from its origin, the unpushed commit from the bundle, its own files.
    body = there.bodies[0] / "proj"
    assert done["bodies"]["proj"] == {"restored": "cloned"}
    assert git(body, "rev-parse", "HEAD") == head
    assert git(body, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git(body, "rev-parse", "--abbrev-ref", "main@{upstream}") == "origin/main"
    for own in ("body.toml", "LAYER.md", ".wt/scratch.py", "notes.txt", "src.txt"):
        assert (body / own).is_file(), own
    assert not (body / "target").exists(), "build output the project ignores stays behind"
    assert not git(body, "status", "--porcelain", "--untracked-files=no")
    untracked = git(body, "status", "--porcelain").splitlines()
    assert all(".wt" not in line and "body.toml" not in line for line in untracked), \
        "the body's own files are still the machine's, not the project's"

    for secret in SECRETS:
        assert not list((tmp_path / "dst").rglob(secret)), secret


def test_a_body_whose_origin_cannot_be_reached_is_said_with_its_files_written(tmp_path):
    paths, search, origin, _ = a_source(tmp_path)
    made = machinecopy.export(paths, search, tmp_path / "out")
    subprocess.run(["rm", "-rf", str(origin)], check=True)
    target, there = machine(tmp_path / "dst")
    done = machinecopy.import_archive(target, there, made)
    assert "error" in done["bodies"]["proj"]
    assert (there.bodies[0] / "proj" / "body.toml").is_file()


def test_import_refuses_while_the_daemon_answers(tmp_path, runtime_dir):
    paths, search, _, _ = a_source(tmp_path)
    made = machinecopy.export(paths, search, tmp_path / "out")
    target, there = machine(tmp_path / "dst")
    target = Paths(state=target.state, data=target.data, config=target.config,
                   runtime=runtime_dir)
    with socket.socket(socket.AF_UNIX) as listening:
        listening.bind(str(target.api_socket))
        listening.listen()
        with pytest.raises(machinecopy.MachineCopyError, match="raigolmid answers"):
            machinecopy.import_archive(target, there, made)
    assert not target.preferences.exists()


def test_the_machine_tab_there_starts_with_the_imported_ones_memory(tmp_path, monkeypatch):
    paths, search, _, _ = a_source(tmp_path / "from")
    made = machinecopy.export(paths, search, tmp_path / "out")
    h = Harness(tmp_path / "here", monkeypatch)
    there = SearchPaths(faces=(tmp_path / "layers" / "faces",),
                        toolbelts=(tmp_path / "layers" / "toolbelts",),
                        bodies=(tmp_path / "layers" / "bodies",), private=())
    machinecopy.import_archive(h.paths, there, made)
    h.session.ensure_tabs()
    memory = h.session.agents.home(h.tab(None)) / ".claude" / "projects" / "-work" / "memory"
    assert (memory / "MEMORY.md").read_text() == "- direction\n"
    assert not (memory / ".credentials.json").exists()

    # An import with a machine tab already open gives the memory to it.
    (memory / "MEMORY.md").write_text("- its own\n")
    done = machinecopy.import_archive(h.paths, there, made)
    assert done["machine_memory"] == str(memory)
    assert (memory / "MEMORY.md").read_text() == "- direction\n"

    # Once this machine has closed a machine tab of its own, a new one starts afresh.
    machine_tab = h.tab(None)
    h.session.close_tab(machine_tab)
    h.session.ensure_tabs()
    fresh = h.session.agents.home(h.tab(None))
    assert h.tab(None) != machine_tab
    assert not (fresh / ".claude" / "projects" / "-work" / "memory").exists()


def test_the_machine_tab_exports_into_the_transfer_folder_and_no_other_tab_can(h):
    from raigolmid.channel import Channels
    from raigolmid.questions import Questions
    from raigolmid.scopes import build_tab_methods
    from raigolmid.session import SessionError
    h.session.select("body", "myapi")
    h.session.ensure_tabs()
    questions, channels = Questions(h.events, h.paths), Channels(h.session, h.events)
    with pytest.raises(SessionError):
        build_tab_methods(h.session, questions, channels, h.tab("myapi"))["export_machine"]()
    made = build_tab_methods(h.session, questions, channels, h.tab(None))["export_machine"]()
    path = h.paths.transfer / "out" / made["file"]
    assert path.stat().st_size == made["bytes"]
