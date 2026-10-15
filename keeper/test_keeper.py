"""The keeper's decisions (keeper.py's module docstring), against doubles for the clock, the
VM, Claude Code and ntfy."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import keeper as k  # noqa: E402

T0 = 1_800_000_000.0
HEALTHY = {"reachable": True, "daemon": "active", "held": False, "hold": None,
           "run_active": True, "last_event": {"type": "agent.busy", "age_s": 30}}


class World:
    def __init__(self, tmp: Path) -> None:
        self.paths = k.Paths(work=tmp / "RaiGolmi", launcher_home=tmp / "launcher",
                             key=tmp / "root" / "keeper_ed25519")
        self.paths.launcher_home.mkdir(parents=True)
        (self.paths.launcher_home / "settings.ini").write_text("memory=8\nntfy-topic=raigolmi-test\n")
        self.now = T0
        self.vm = dict(HEALTHY)
        self.sessions: list[str] = []
        self.verdict = "KEEPER: not fixed: still down"
        self.signed_in = True
        self.pushes: list[tuple[str, str, str]] = []
        self.keeper = self.make()

    def make(self) -> k.Keeper:
        return k.Keeper(self.paths, lambda: self.now, lambda: self.vm, self.run,
                        lambda: (self.signed_in, "" if self.signed_in else "Claude Code is not signed in"),
                        lambda topic, title, message: self.pushes.append((topic, title, message)))

    def run(self, prompt: str, session_id: str, log: Path) -> dict:
        self.sessions.append(prompt)
        return {"verdict": self.verdict, "api_error": None}

    def launcher(self, state: str, written: float | None = None) -> None:
        """The launcher in `state`, writing launcher.json at every check as a live one does,
        or, given `written`, last writing it then."""
        self.state, self.written = state, written
        self._write()

    def _write(self) -> None:
        k.write_json(self.paths.launcher_json, {
            "state": self.state, "pid": 4242,
            "at": k.iso(self.now if self.written is None else self.written),
            "why": None, "recoveries": 0, "last_request": None})

    def check(self, after: float = 0) -> dict:
        self.now += after
        self._write()
        self.keeper.check()
        return json.loads(self.paths.keeper_json.read_text())

    def inbox(self) -> list[dict]:
        return k.pending_requests(self.paths)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_healthy_does_nothing(world: World) -> None:
    world.launcher("running")
    assert world.check()["state"] == "healthy"
    assert world.sessions == [] and world.pushes == [] and world.inbox() == []


def test_closed_is_respected_even_when_stale(world: World) -> None:
    world.launcher("closed", written=T0 - 3 * 86400)
    world.vm = {"reachable": False, "error": "refused"}
    for _ in range(30):
        assert world.check(after=k.CHECK_SECONDS)["state"] == "closed"
    assert world.sessions == [] and world.pushes == [] and world.inbox() == []


def test_usage_hold_does_nothing(world: World) -> None:
    world.launcher("running")
    world.vm = {**HEALTHY, "held": True, "last_event": {"type": "account.probed", "age_s": 9000}}
    for _ in range(30):
        assert world.check(after=k.CHECK_SECONDS)["state"] == "held"
    assert world.sessions == [] and world.pushes == []


def test_stale_launcher_is_asked_to_start_then_a_session(world: World) -> None:
    world.launcher("running", written=T0 - 10 * k.LAUNCHER_STALE_SECONDS)
    world.check()
    assert [r["request"] for r in world.inbox()] == ["start"]
    world.check(after=k.CHECK_SECONDS)
    assert len(world.inbox()) == 1, "one start request, not one per check"
    assert world.sessions == []
    world.check(after=k.START_GRACE_SECONDS)
    assert len(world.sessions) == 1


def test_gave_up_starts_a_session_at_once(world: World) -> None:
    world.launcher("gave_up")
    report = world.check()
    assert len(world.sessions) == 1
    assert "gave up" in world.sessions[0]
    assert report["incident"]["sessions"][0]["resume"].endswith(report["incident"]["sessions"][0]["id"])
    world.check(after=k.CHECK_SECONDS)
    assert len(world.sessions) == 1, "the second session waits for the first's work to take"


def test_failure_waits_its_grace_before_a_session(world: World) -> None:
    world.launcher("running")
    world.vm = {**HEALTHY, "daemon": "failed"}
    world.check()
    assert world.sessions == []
    world.check(after=k.FAILING_SECONDS)
    assert len(world.sessions) == 1


def test_silent_events_during_a_run_fail_but_not_outside_one(world: World) -> None:
    world.launcher("running")
    quiet = {"type": "agent.idle", "age_s": k.EVENTS_SILENT_SECONDS + 60}
    world.vm = {**HEALTHY, "run_active": False, "last_event": quiet}
    assert world.check()["state"] == "healthy"
    world.vm = {**HEALTHY, "run_active": True, "last_event": quiet}
    assert world.check()["state"] == "failing"


def test_repeated_failure_alerts_once_then_daily_then_recovers(world: World) -> None:
    world.launcher("gave_up")
    world.check()
    for _ in range(3):
        world.check(after=k.FAILING_SECONDS)
    assert len(world.sessions) == k.SESSIONS_PER_INCIDENT
    assert "Earlier keeper sessions" in world.sessions[1]
    assert len(world.pushes) == 1
    assert world.pushes[0][:2] == ("raigolmi-test", k.ALERT_NOT_FIXED[0])
    for _ in range(10):
        world.check(after=k.CHECK_SECONDS)
    assert len(world.pushes) == 1
    world.check(after=k.REALERT_SECONDS)
    assert len(world.pushes) == 2
    world.launcher("running")
    report = world.check(after=k.CHECK_SECONDS)
    assert report["state"] == "healthy" and report["incident"] is None
    assert world.pushes[-1][1] == k.ALERT_RECOVERED[0]
    assert len(world.sessions) == k.SESSIONS_PER_INCIDENT


def test_not_signed_in_alerts_without_a_session(world: World) -> None:
    world.signed_in = False
    world.launcher("gave_up")
    world.check()
    world.check(after=k.CHECK_SECONDS)
    assert world.sessions == []
    assert len(world.pushes) == 1 and "not signed in" in world.pushes[0][2]


def test_incident_survives_a_keeper_restart(world: World) -> None:
    world.launcher("gave_up")
    world.check()
    world.check(after=k.FAILING_SECONDS)
    world.keeper = world.make()
    world.check(after=k.FAILING_SECONDS)
    assert len(world.sessions) == k.SESSIONS_PER_INCIDENT and len(world.pushes) == 1


def test_verdict_read_from_stream_json(tmp_path: Path) -> None:
    log = tmp_path / "s.log"
    log.write_text("\n".join([
        json.dumps({"type": "system", "subtype": "init"}),
        "not json",
        json.dumps({"type": "result", "is_error": False,
                    "result": "Restarted the daemon.\nKEEPER: fixed"})]))
    assert k.read_outcome(log) == {"verdict": "KEEPER: fixed", "api_error": None}
    log.write_text(json.dumps({"type": "result", "is_error": True, "api_error_status": 429,
                               "result": "limit reached"}))
    assert k.read_outcome(log)["api_error"].startswith("429")


def test_key_is_appended_once(world: World, tmp_path: Path) -> None:
    world.paths.authorized_keys.parent.mkdir(parents=True)
    world.paths.authorized_keys.write_text("ssh-ed25519 AAAAother dev")
    k.ensure_key(world.paths)
    k.ensure_key(world.paths)
    lines = world.paths.authorized_keys.read_text().splitlines()
    assert lines[0] == "ssh-ed25519 AAAAother dev" and len(lines) == 2
    assert lines[1].endswith("raigolmi-keeper")
