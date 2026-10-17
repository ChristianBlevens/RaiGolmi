"""The keeper's decisions (keeper.py's module docstring), against doubles for the clock, the
VM, Claude Code and ntfy."""
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import keeper as k  # noqa: E402

T0 = 1_800_000_000.0
HEALTHY = {"reachable": True, "daemon": "active", "restarts": 0, "line_out": None,
           "held": False, "hold": None,
           "run_working": True, "last_event": {"type": "agent.busy", "age_s": 30},
           "faults": [], "faults_error": None, "plan": None}
FAULT = {"id": "fault-1", "title": "jobs of an earlier tab no tab can stop",
         "body": "job_stop: no such job", "affects": ["tab-2"], "status": "open"}


class World:
    def __init__(self, tmp: Path) -> None:
        self.paths = k.Paths(work=tmp / "RaiGolmi", launcher_home=tmp / "launcher",
                             key=tmp / "root" / "keeper_ed25519",
                             credential=tmp / "root" / ".keeper-credential")
        self.paths.launcher_home.mkdir(parents=True)
        (self.paths.launcher_home / "settings.ini").write_text("memory=8\nntfy-topic=raigolmi-test\n")
        self.now = T0
        self.vm = dict(HEALTHY)
        self.sessions: list[str] = []
        self.verdict = "KEEPER: not fixed: still down"
        # What read_outcome says beyond the verdict.
        self.outcome = {"reached": True, "signed_out": False, "unreached": None}
        self.signed_in = True
        self.refused: list[str | None] = []
        self.pushes: list[tuple[str, str, str]] = []
        self.told: list[list[str]] = []
        self.vm_answers = True
        self.keeper = self.make()

    def make(self) -> k.Keeper:
        return k.Keeper(self.paths, lambda: self.now, lambda: self.now, lambda: self.vm, self.run,
                        self.ready,
                        lambda topic, title, message: self.pushes.append((topic, title, message)),
                        self.tell, lambda: "Push nowhere.\n\n")

    def ready(self, refused: str | None) -> tuple[str, str]:
        self.refused.append(refused)
        if refused == "token-1":
            return "signed_out", "Claude refused its sign-in"
        return ("ready", "") if self.signed_in else ("signed_out", "it has no sign-in")

    def tell(self, args: list[str]) -> dict:
        """raigolmid's faults.py as `rai keeper` reaches it: a taken or deferred fault is still
        listed, one answered otherwise is not."""
        self.told.append(args)
        if not self.vm_answers:
            return {"ok": False, "error": "TimeoutError: timed out"}
        if args[0] == "take":
            self.vm["faults"] = [{**f, "status": "taken"} if f["id"] == args[1] else f
                                 for f in self.vm["faults"]]
        if args[0] == "answer" and args[2] == "deferred":
            self.vm["faults"] = [{**f, "status": "deferred"} if f["id"] == args[1] else f
                                 for f in self.vm["faults"]]
        elif args[0] == "answer":
            self.vm["faults"] = [f for f in self.vm["faults"] if f["id"] != args[1]]
        return {"ok": True, "result": {}}

    def run(self, prompt: str, session_id: str, log: Path) -> dict:
        self.sessions.append(prompt)
        return {"verdict": self.verdict, **self.outcome, "token": "token-1"}

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


def test_keeper_json_names_the_keeper_py_it_runs(world: World) -> None:
    world.launcher("running")
    source = hashlib.sha256(Path(k.__file__).read_bytes()).hexdigest()
    assert world.check()["source"] == source


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
    assert world.inbox() == [], "unchanged is measured from when the keeper first read it"
    world.check(after=k.LAUNCHER_STALE_SECONDS + 1)
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
    world.vm = {**HEALTHY, "run_working": False, "last_event": quiet}
    assert world.check()["state"] == "healthy"
    world.vm = {**HEALTHY, "run_working": True, "last_event": quiet}
    assert world.check()["state"] == "failing"


def test_a_run_whose_every_tab_waits_on_the_user_is_not_working() -> None:
    def intent(*tabs: dict, ended: float | None = None) -> dict:
        return {"run": {"ended": ended},
                "tabs": {"tab-1": {"managed": False, "status": "running", "held": None},
                         **{f"tab-{n}": t for n, t in enumerate(tabs, 2)}}}
    running = {"managed": True, "status": "running", "held": None}
    paused = {"managed": True, "status": "paused", "held": None}
    held = {"managed": True, "status": "running", "held": {"until": None}}
    assert k.run_working(intent(running, paused))
    assert not k.run_working(intent(paused))
    assert not k.run_working(intent(paused, held))
    assert not k.run_working(intent())
    assert not k.run_working(intent(running, ended=T0))
    assert not k.run_working({"run": None, "tabs": {}})
    assert not k.run_working(None)


def test_the_probe_script_compiles() -> None:
    compile(k.PROBE_SCRIPT, "probe", "exec")


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
    assert [p[1:] for p in world.pushes] == [k.ALERT_SIGNED_OUT]
    assert "restart" not in world.pushes[0][2].lower()


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
    assert k.read_outcome(log) == {"verdict": "KEEPER: not fixed: Claude never answered: "
                                              "Restarted the daemon.\nKEEPER: fixed",
                                   "reached": False, "signed_out": False,
                                   "unreached": "Claude never answered: Restarted the daemon.\nKEEPER: fixed"}
    log.write_text("\n".join([
        json.dumps({"type": "assistant", "message": {"content": []}}),
        json.dumps({"type": "result", "is_error": False,
                    "result": "Restarted the daemon.\nKEEPER: fixed"})]))
    assert k.read_outcome(log) == {"verdict": "KEEPER: fixed", "reached": True,
                                   "signed_out": False, "unreached": None}
    log.write_text(json.dumps({"type": "result", "is_error": True, "api_error_status": 529,
                               "result": "overloaded"}))
    assert k.read_outcome(log)["unreached"].startswith("the API answered 529")
    log.write_text(json.dumps({"type": "result", "is_error": True, "api_error_status": 401,
                               "result": "OAuth token has expired"}))
    outcome = k.read_outcome(log)
    assert outcome["signed_out"] and outcome["unreached"] is None


def test_key_is_appended_once(world: World, tmp_path: Path) -> None:
    world.paths.authorized_keys.parent.mkdir(parents=True)
    world.paths.authorized_keys.write_text("ssh-ed25519 AAAAother dev")
    k.ensure_key(world.paths)
    k.ensure_key(world.paths)
    lines = world.paths.authorized_keys.read_text().splitlines()
    assert lines[0] == "ssh-ed25519 AAAAother dev" and len(lines) == 2
    assert lines[1].endswith("raigolmi-keeper")


def test_a_fault_from_the_machine_tab_is_taken_worked_and_answered_fixed(world: World) -> None:
    world.launcher("running")
    world.vm = {**HEALTHY, "faults": [FAULT]}
    world.verdict = k.FIXED
    report = world.check()
    assert len(world.sessions) == 1, "a fault is acted on at once"
    assert "job_stop: no such job" in world.sessions[0] and "tab-2" in world.sessions[0]
    assert world.told[0][:2] == ["take", "fault-1"], "taken before the session"
    assert world.told[-1] == ["answer", "fault-1", "fixed"]
    assert report["incident"] is None and report["last_incident"]["ended_as"] == "answered fixed"
    assert world.check(after=k.CHECK_SECONDS)["state"] == "healthy"
    assert world.pushes == []


def test_a_fault_not_fixed_by_its_sessions_is_answered_with_words_and_never_alerted(world: World) -> None:
    world.launcher("running")
    world.vm = {**HEALTHY, "faults": [FAULT]}
    world.check()
    assert len(world.sessions) == 1 and not any(t[0] == "answer" for t in world.told), \
        "a session is left: the fault stays the keeper's"
    world.check(after=k.CHECK_SECONDS)
    assert len(world.sessions) == 1
    world.check(after=k.FAILING_SECONDS)
    assert len(world.sessions) == k.SESSIONS_PER_INCIDENT
    assert "Earlier keeper sessions" in world.sessions[1]
    assert world.told[-1] == ["answer", "fault-1", "not_fixed", "still down"]
    for _ in range(3):
        assert world.check(after=k.CHECK_SECONDS)["state"] == "healthy"
    assert world.pushes == [], "a fault is the machine tab's alone: the run goes on around it"


FEATURE = {"id": "feature-1", "kind": "feature", "title": "a pause button for the run",
           "body": "the owner wants to pause every tab at once", "affects": [], "status": "open"}


def test_a_feature_waits_behind_a_fault_and_is_built_without_an_alert(world: World) -> None:
    world.launcher("running")
    world.vm = {**HEALTHY, "faults": [FAULT, FEATURE]}
    world.verdict = k.FIXED
    world.check()
    assert world.told[0][:2] == ["take", "fault-1"], "the fault first"
    world.told.clear()
    world.verdict = "KEEPER: not fixed: needs a design call"
    report = world.check(after=k.CHECK_SECONDS)
    assert report["incident"]["reason"].startswith("the owner asked for a feature")
    assert world.told[0][:2] == ["take", "feature-1"]
    assert "pause every tab at once" in world.sessions[-1]
    assert world.check(after=k.CHECK_SECONDS)["state"] == "building"
    world.check(after=k.FAILING_SECONDS)
    assert world.told[-1] == ["answer", "feature-1", "not_fixed", "needs a design call"]
    assert world.pushes == [], "a feature not built is the owner's news, not the PC's"
    assert world.check(after=k.CHECK_SECONDS)["state"] == "healthy"


def test_an_answer_the_vm_did_not_take_is_sent_again_and_its_fault_not_reworked(world: World) -> None:
    world.launcher("running")
    world.vm = {**HEALTHY, "faults": [FAULT]}
    world.verdict = k.FIXED
    world.check()
    world.vm_answers = False
    world.told.clear()
    world.keeper.unanswered = {"fault-1": {"verdict": "fixed", "words": ""}}
    world.vm["faults"] = [FAULT]
    assert world.check(after=k.CHECK_SECONDS)["unanswered"] == {
        "fault-1": {"verdict": "fixed", "words": ""}}
    assert len(world.sessions) == 1, "a fault answered is not worked again"
    world.vm_answers = True
    world.keeper = world.make()
    report = world.check(after=k.CHECK_SECONDS)
    assert report["unanswered"] == {} and world.told[-1] == ["answer", "fault-1", "fixed"]
    assert len(world.sessions) == 1


def test_a_fault_while_claude_code_cannot_run_is_answered_not_fixed(world: World) -> None:
    world.signed_in = False
    world.launcher("running")
    world.vm = {**HEALTHY, "faults": [FAULT]}
    world.check()
    assert world.sessions == []
    assert world.told[-1][:3] == ["answer", "fault-1", "not_fixed"]
    assert "no sign-in" in world.told[-1][3] and world.pushes == []


def spend_sessions(world: World) -> None:
    """A gave_up incident through both its sessions."""
    world.check()
    world.check(after=k.FAILING_SECONDS)
    assert len(world.sessions) == k.SESSIONS_PER_INCIDENT


def test_gave_up_with_sessions_spent_keeps_asking_to_start(world: World) -> None:
    world.launcher("gave_up")
    spend_sessions(world)
    world.check(after=k.FAILING_SECONDS)
    assert [r["request"] for r in world.inbox()] == ["start"]
    for p in world.paths.inbox.glob("*.json"):
        p.unlink()                      # the door took it, and the launcher gave up again
    world.check(after=k.CHECK_SECONDS)
    assert world.inbox() == [], "spaced, not one per check"
    world.check(after=k.START_RETRY_SECONDS)
    assert [r["request"] for r in world.inbox()] == ["start"]
    assert len(world.sessions) == k.SESSIONS_PER_INCIDENT


def plan(course: float) -> dict:
    return {"status": "allowed", "read_at": T0, "windows": {
        "7d": {"used": 0.3, "resets_at": T0 + 86400, "passed": 0.5, "on_course_for": 0.6},
        "5h": {"used": 0.5, "resets_at": T0 + 3600, "passed": 0.5, "on_course_for": course}}}


def test_a_fault_past_the_plans_course_is_worked_narrower_never_held(world: World) -> None:
    world.launcher("running")
    world.vm = {**HEALTHY, "faults": [FAULT], "plan": plan(1.4)}
    world.verdict = k.FIXED
    world.check()
    assert len(world.sessions) == 1, "pacing never holds a repair back"
    assert "5h window is on course for 1.4" in world.sessions[0] and "narrower" in world.sessions[0]


def test_a_fault_under_the_usage_limit_is_deferred_once_then_taken_up(world: World) -> None:
    world.launcher("running")
    world.vm = {**HEALTHY, "faults": [FAULT], "held": True}
    assert world.check()["state"] == "held" and world.sessions == []
    assert world.told[-1][:3] == ["answer", "fault-1", "deferred"]
    told = len(world.told)
    world.check(after=k.CHECK_SECONDS)
    assert len(world.told) == told, "deferred once, not at every check"
    world.vm["held"] = False
    world.verdict = k.FIXED
    world.check(after=k.CHECK_SECONDS)
    assert len(world.sessions) == 1 and world.told[-1] == ["answer", "fault-1", "fixed"]
    assert "narrower" not in world.sessions[0]


def test_faults_are_not_bounded_by_a_day_count(world: World) -> None:
    world.launcher("running")
    world.verdict = k.FIXED
    for n in range(6):
        world.vm = {**HEALTHY, "faults": [{**FAULT, "id": f"fault-{n}"}]}
        world.check(after=k.CHECK_SECONDS)
    assert len(world.sessions) == 6




def test_a_daemon_crash_loop_is_failing_and_one_restart_is_not(world: World) -> None:
    world.launcher("running")
    world.check()
    world.vm = {**HEALTHY, "restarts": 1}
    assert world.check(after=k.CHECK_SECONDS)["state"] == "healthy"
    world.vm = {**HEALTHY, "restarts": 2}
    report = world.check(after=k.CHECK_SECONDS)
    assert report["state"] == "failing" and "restarted raigolmid 2 times" in report["why"]
    world.vm = {**HEALTHY, "restarts": 2}
    assert world.check(after=k.DAEMON_RESTART_WINDOW + 1)["state"] == "healthy"
    world.vm = {**HEALTHY, "restarts": 0}
    assert world.check(after=k.CHECK_SECONDS)["state"] == "healthy", "a guest booted again"


def test_sessions_claude_never_answered_do_not_count_and_a_day_of_them_is_told(world: World) -> None:
    world.outcome = {"reached": False, "signed_out": False, "unreached": "Claude never answered: offline"}
    world.launcher("gave_up")
    world.check()
    for _ in range(4):
        world.check(after=k.FAILING_SECONDS)
    assert len(world.sessions) == 5 and world.pushes == []
    for _ in range(int(k.UNREACHED_SECONDS // k.FAILING_SECONDS)):
        world.check(after=k.FAILING_SECONDS)
    assert [p[1:] for p in world.pushes] == [k.ALERT_UNREACHED]
    world.outcome = {"reached": True, "signed_out": False, "unreached": None}
    before = len(world.sessions)
    world.check(after=k.FAILING_SECONDS)
    world.check(after=k.FAILING_SECONDS)
    assert len(world.sessions) == before + 2, "both of the incident's sessions are still its own"


def test_staleness_is_by_the_keepers_clock_not_the_launchers(world: World) -> None:
    world.launcher("running")
    world.check()
    for n in range(5):                  # a launcher whose clock is hours behind, still writing
        world.written = T0 - 5 * 3600 + n
        assert world.check(after=k.CHECK_SECONDS * 2)["state"] == "healthy"
    report = world.check(after=k.LAUNCHER_STALE_SECONDS + 1)
    assert report["state"] == "failing" and "has not written" in report["why"]


def test_claude_ready_answers_rather_than_raises(world: World, monkeypatch) -> None:
    assert k.claude_ready(world.paths, None)[0] == "signed_out", "no credential copied"
    world.paths.credential.parent.mkdir(parents=True, exist_ok=True)
    world.paths.credential.write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-token\n")
    assert k.claude_ready(world.paths, k.fingerprint("sk-token")) == (
        "signed_out", "Claude refused the machine's agent credential")

    def hangs(*args, **kwargs):
        raise k.subprocess.TimeoutExpired(args[0], k.CLAUDE_STATUS_SECONDS)
    monkeypatch.setattr(k.subprocess, "run", hangs)
    assert k.claude_ready(world.paths, None)[0] == "cannot_run"


def test_the_loop_outlives_a_check_that_raises(world: World, monkeypatch) -> None:
    checks = []

    def check(self) -> None:
        checks.append(1)
        raise ValueError("launcher.json names state 'odd'")

    def sleep(seconds: float) -> None:
        if len(checks) == 3:
            raise KeyboardInterrupt
    monkeypatch.setattr(k.Keeper, "check", check)
    monkeypatch.setattr(k, "ensure_key", lambda paths: None)
    monkeypatch.setattr(k.time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        k.loop(world.paths)
    assert len(checks) == 3


def test_a_refused_sign_in_stops_sessions_and_says_so_plainly(world: World) -> None:
    world.outcome = {"reached": False, "signed_out": True, "unreached": None}
    world.launcher("gave_up")
    world.check()
    world.check(after=k.CHECK_SECONDS)
    assert len(world.sessions) == 1 and world.refused[-1] == "token-1"
    assert [p[1:] for p in world.pushes] == [k.ALERT_SIGNED_OUT]
    assert "Nothing to do; the owner will fix it when back." in world.pushes[0][2]


def test_one_alert_title_a_day_across_incidents_and_recovery_follows_a_superseded_one(world: World) -> None:
    world.launcher("gave_up")
    spend_sessions(world)
    world.check(after=k.FAILING_SECONDS)
    assert [p[1] for p in world.pushes] == [k.ALERT_NOT_FIXED[0]]
    world.launcher("running")
    world.vm = {**HEALTHY, "daemon": "failed"}
    world.check(after=k.CHECK_SECONDS)       # a new incident, the alerted one superseded
    world.check(after=k.FAILING_SECONDS)
    world.check(after=k.FAILING_SECONDS)
    world.check(after=k.FAILING_SECONDS)
    assert [p[1] for p in world.pushes] == [k.ALERT_NOT_FIXED[0]]
    world.vm = dict(HEALTHY)
    world.check(after=k.CHECK_SECONDS)
    assert world.pushes[-1][1] == k.ALERT_RECOVERED[0]


def test_working_again_only_when_healthy(world: World) -> None:
    world.launcher("gave_up")
    spend_sessions(world)
    world.check(after=k.FAILING_SECONDS)
    world.launcher("running")
    world.vm = {**HEALTHY, "held": True}
    assert world.check(after=k.CHECK_SECONDS)["state"] == "held"
    assert [p[1] for p in world.pushes] == [k.ALERT_NOT_FIXED[0]]
    world.vm = dict(HEALTHY)
    world.check(after=k.CHECK_SECONDS)
    assert world.pushes[-1][1] == k.ALERT_RECOVERED[0]


def test_the_line_outs_word_stands(world: World) -> None:
    world.launcher("running")
    world.vm = {**HEALTHY, "daemon": "failed", "line_out": {"sent": {"disk_full": T0 - 3600}}}
    world.check()
    for _ in range(4):
        world.check(after=k.FAILING_SECONDS)
    assert len(world.sessions) == k.SESSIONS_PER_INCIDENT, "it may still run its sessions"
    assert world.pushes == []
    world.vm = dict(HEALTHY)
    world.check(after=k.CHECK_SECONDS)
    assert world.pushes == [], "nothing was said, so no working again"



def test_the_machines_credential_is_copied_and_a_bad_read_keeps_the_last(world: World, monkeypatch) -> None:
    answers = iter([(0, "CLAUDE_CODE_OAUTH_TOKEN=sk-one\n", ""), (1, "", "No such file"),
                    (0, "CLAUDE_CODE_OAUTH_TOKEN=sk-onesk-one\nANTHROPIC_API_KEY=x\n", ""),
                    (0, "ANTHROPIC_API_KEY=sk-two\n", "")])
    monkeypatch.setattr(k, "vm_run", lambda client, command, timeout: next(answers))
    world.paths.credential.parent.mkdir(parents=True, exist_ok=True)
    for expected in ("sk-one", "sk-one", "sk-one", "sk-two"):
        k.copy_credential(world.paths, None)
        env, token = k.claude_env(world.paths)
        assert token == k.fingerprint(expected)
    assert env["ANTHROPIC_API_KEY"] == "sk-two" and "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert world.paths.credential.stat().st_mode & 0o777 == 0o600


def github(login: str | None, forks: dict[str, str]):
    """GitHub as gh reaches it: `login` signed in (None: no sign-in), and `forks` its fork of
    each repository it has one of, by name, as `full_name<TAB>push`."""
    def run(args: list[str]) -> tuple[int, str, str]:
        if login is None:
            return 4, "", "To get started with GitHub CLI, please run:  gh auth login"
        if args[1] == "user":
            return 0, login + "\n", ""
        repo = args[-3].split("/")[2]
        if args[1] == "--paginate":
            return 0, forks.get(repo, ""), ""
        return 0, f"{k.UPSTREAM}/{repo}\ttrue\n", ""
    return run


def test_a_sign_in_with_no_fork_is_told_to_push_nowhere() -> None:
    words = k.push_targets(github("someone", {"RaiGolmi": "someone/RaiGolmi\ttrue\n"}))
    assert "push `machine/someone` to `someone/RaiGolmi`" in words
    assert "raigolmi-packages: `someone` has no fork" in words
    assert f"to `{k.UPSTREAM}/" not in words


def test_a_fork_the_sign_in_cannot_push_to_is_no_fork() -> None:
    words = k.push_targets(github("someone", {"RaiGolmi": "someone/RaiGolmi\tfalse\n"}))
    assert "RaiGolmi: `someone` has no fork" in words


def test_the_owner_pushes_to_their_own_repositories() -> None:
    words = k.push_targets(github(k.UPSTREAM, {}))
    assert f"to `{k.UPSTREAM}/RaiGolmi`" in words and f"to `{k.UPSTREAM}/raigolmi-packages`" in words


def test_no_sign_in_pushes_nothing() -> None:
    words = k.push_targets(github(None, {}))
    assert "no GitHub sign-in" in words and "push nothing" in words


def test_a_sessions_prompt_says_where_it_may_push(world: World) -> None:
    world.launcher("gave_up")
    world.check()
    assert world.sessions and "Push nowhere." in world.sessions[0]
