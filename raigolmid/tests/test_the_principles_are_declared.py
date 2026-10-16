"""A body that names a principles doc has each new commit declared against it by its tab, and the
machine tab is asked at its cadence to check the declarations (`principles.py`)."""
from __future__ import annotations

import dataclasses
import subprocess

import pytest

from raigolmid.definitions import DefinitionError, load_body
from raigolmid.history import SAYS
from raigolmid.principles import CHECK_TURNS, Principles, PrinciplesError

from tests.harness import Harness
from tests.test_the_machine_tab_coordinates import BODY, MACHINE, Machine


@pytest.fixture()
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.session.select("body", "myapi")
    assert (harness.tab(None), harness.tab("myapi")) == (MACHINE, BODY)
    return harness


def _name_principles(h) -> None:
    bodies = h.session.catalogue.bodies
    root = bodies["myapi"].source_root
    (root / "PRINCIPLES.md").write_text("Judge a feature by what it is for.\n")
    bodies["myapi"] = dataclasses.replace(bodies["myapi"], principles="PRINCIPLES.md")


def _commit(h, message: str) -> str:
    repo = h.session.working_copy(BODY)
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", message], cwd=repo, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def test_a_body_naming_no_doc_is_never_asked(h):
    p = Principles(h.session, h.events)
    _commit(h, "work")
    assert p.owed(BODY) is None
    with pytest.raises(PrinciplesError, match="names no principles doc"):
        p.declare(BODY, [])
    with pytest.raises(PrinciplesError, match="names no principles doc"):
        p.ledger("myapi")
    # A body that is not defined is not one without a doc: it is a wrong name.
    with pytest.raises(PrinciplesError, match="no body 'gone' is defined"):
        p.ledger("gone")
    with pytest.raises(PrinciplesError, match="no body 'gone' is defined"):
        p.checked("gone", tabs="", drift="none")


def test_each_commit_since_the_first_reading_is_owed_until_declared(h):
    _name_principles(h)
    p = Principles(h.session, h.events)
    assert p.owed(BODY) is None, "the first reading is where declarations start"
    first, second = _commit(h, "people move by what they see"), _commit(h, "hire set aside")
    owed = p.owed(BODY)
    assert [c["commit"] for c in owed["commits"]] == [first, second]
    assert owed["doc"].endswith("PRINCIPLES.md") and owed["left"] == []

    with pytest.raises(PrinciplesError, match="says in `note` what comes next"):
        p.declare(BODY, [{"commit": first, "for": "x", "judged_by": "y", "holds": False}])
    with pytest.raises(PrinciplesError, match="not one commit owed"):
        p.declare(BODY, [{"commit": "0000000", "for": "x", "judged_by": "y", "holds": True}])
    p.declare(BODY, [{"commit": first[:8], "for": "people move by what they see",
                      "judged_by": "crossings follow a dearth seen at home", "holds": True}])
    assert [c["commit"] for c in p.owed(BODY)["commits"]] == [second]
    p.declare(BODY, [{"commit": second, "for": "hire", "judged_by": "deaths alone",
                      "holds": False, "note": "judge it by who is hired next"}])
    assert p.owed(BODY) is None
    (drift,) = [e for e in h.events_of("principles.declared") if e.data["drift"]]
    assert "judge it by who is hired next" in SAYS["principles.declared"](drift)


def test_commits_head_no_longer_holds_are_declared_set_aside(h):
    _name_principles(h)
    p = Principles(h.session, h.events)
    p.owed(BODY)
    _commit(h, "kept")
    base = _commit(h, "base")
    p.declare(BODY, [{"commit": c["commit"], "for": "f", "judged_by": "j", "holds": True}
                     for c in p.owed(BODY)["commits"]])
    subprocess.run(["git", "reset", "-q", "--hard", "HEAD~1"],
                   cwd=h.session.working_copy(BODY), check=True)
    assert p.owed(BODY)["left"] == [base]
    with pytest.raises(PrinciplesError, match="set_aside"):
        p.declare(BODY, [])
    p.declare(BODY, [], set_aside=[{"what": "base", "why": "it judged by deaths alone"}])
    assert p.owed(BODY) is None


def test_the_machine_tab_is_asked_to_check_only_when_there_are_declarations(h):
    _name_principles(h)
    m = Machine(h)
    m.tab(MACHINE)["manage"](tab=BODY)
    p = Principles(h.session, h.events)
    p.owed(BODY)
    for _ in range(CHECK_TURNS):
        p.on_event(h.events.emit("agent.idle", tab=BODY, done=False, error=None))
    assert not h.events_of("principles.check_asked"), "nothing moved, so nothing is asked"

    sha = _commit(h, "work")
    p.declare(BODY, [{"commit": sha, "for": "f", "judged_by": "j", "holds": True}])
    m.tab(MACHINE)["direct"](tab=BODY, content="judge it by what it is for")
    for event in h.events_of("coordinator.directed"):
        p.on_event(event)
    for _ in range(CHECK_TURNS):
        p.on_event(h.events.emit("agent.idle", tab=BODY, done=False, error=None))
    (asked,) = h.events_of("principles.check_asked")
    assert asked.tab == MACHINE and asked.data["bodies"] == ["myapi"]

    ledger = p.ledger("myapi")
    assert [r["kind"] for r in ledger["since_check"]] == ["commit", "direction"]
    assert ledger["doc"].startswith("Judge a feature")
    with pytest.raises(PrinciplesError, match="direction"):
        p.checked("myapi", BODY, "hire judged by deaths")
    p.checked("myapi", BODY, "none")
    assert p.ledger("myapi")["since_check"] == []
    for _ in range(CHECK_TURNS):
        p.on_event(h.events.emit("agent.idle", tab=BODY, done=False, error=None))
    assert len(h.events_of("principles.check_asked")) == 1


def test_a_principles_doc_must_be_a_file_in_the_project(h):
    directory = h.session.catalogue.bodies["myapi"].directory
    toml = directory / "body.toml"
    toml.write_text(toml.read_text() + '\n[principles]\ndoc = "MISSING.md"\n')
    with pytest.raises(DefinitionError, match="not a file"):
        load_body(directory, h.paths.private)
