"""The keeper: notices when the machine cannot recover itself, and has it repaired.

It runs in its own container on the Windows machine, outside the VM (compose.yml). The VM's
janitor repairs what fails inside it and the launcher restarts a crashed or hung QEMU; the
keeper is the line after both. Its loop decides by reading, never by inference: every
`CHECK_SECONDS` it reads the launcher's `keeper/outbox/launcher.json` and probes the VM over
ssh, and names the machine one of

    healthy   the launcher runs the VM, its daemon is up, and a run in progress is writing events
    closed    the user closed the window on purpose: respected, never repaired
    held      the account is on its usage limit (`account.limited` after any `account.resumed`
              in raigolmid's event log, `raigolmid/limits.py`): nothing to do until it lifts
    waiting   the launcher is starting, restarting or upgrading the machine: its turn
    failing   anything else, with the reason

and acts on a failure by running one headless Claude Code session (KEEPER.md is its appended
system prompt) with the evidence it gathered. The rules:

- launcher.json `gave_up` (five restarts in a row failed) is acted on at once;
- launcher.json not written for `LAUNCHER_STALE_SECONDS`, its state not `closed`, is the launcher
  gone: one `start` request goes into keeper/inbox/ (the launcher's scheduled door takes it), and
  the failure is acted on only if the launcher is still gone `START_GRACE_SECONDS` later;
- a launcher stuck in one of its transitional states for `TRANSITION_SECONDS` is failing;
- the VM unreachable by ssh, raigolmid not active, or a run in progress whose event log has been
  silent for `EVENTS_SILENT_SECONDS`, while the launcher says `running`, is failing;
- each probe also says the keeper looked (`rai keeper seen`, raigolmid's faults.py), which is how
  the machine tab knows a keeper is there, and is answered with the faults in RaiGolmi the machine
  tab filed for it, having paused the tabs each affects. An open fault is failing at once and is
  its own incident: the keeper takes it (`rai keeper take`), hands the session its text, and
  answers it (`rai keeper answer`) with the verdict, so the machine tab resumes its tabs:
  `fixed`; or `not_fixed` with the verdict's words once the incident's sessions are spent (with
  the alert below) or Claude Code cannot run. The answer ends the incident; one the VM did not
  take is sent again at each check until it is;
- any other failure is acted on once it has lasted `FAILING_SECONDS`, and again that long after
  each session, so what a session started (an upgrade, a restart) has time to take;
- an incident is the stretch from the first failing check to the next healthy, closed or held
  one. It gets at most `SESSIONS_PER_INCIDENT` sessions, each after the first handed the logs of
  those before. When they are spent, or Claude Code cannot run (not signed in, or its API refused
  the session), the person near the machine gets ONE ntfy alert for the incident, and another
  only after `REALERT_SECONDS` more of it. An incident that ends after an alert says so.

keeper/outbox/keeper.json is written each check: the state, the open incident with its sessions
(each resumable from the keeper's terminal by `claude --resume <id>` in /work/RaiGolmi), and the
last alert. It is also the keeper's memory across a restart of its container.

    python3 keeper.py                         the loop (the container's entrypoint)
    python3 keeper.py vm '<command>' [secs]   one command on the VM, with the keeper's key
    python3 keeper.py request <start|restart|upgrade> '<why>'   a request to the launcher
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# How often the machine is looked at: a failure is acted on in minutes, and one look is one ssh
# login, which the guest does not notice at this rate.
CHECK_SECONDS = 60
# The launcher writes launcher.json every 10 s; six writes missed is a launcher that is gone,
# not one that is slow.
LAUNCHER_STALE_SECONDS = 60
# The launcher's scheduled door takes a request every 5 minutes and the machine then boots;
# three door runs with no launcher is a start that was not taken.
START_GRACE_SECONDS = 15 * 60
# The longest a launcher state between two runs lasts is an upgrade downloading the disk; past
# this it is stuck rather than slow.
TRANSITION_SECONDS = 2 * 3600
# A failure is the launcher's hang recovery's and the guest janitor's first; a boot through to
# the daemon is a few minutes. One that outlasts them is the keeper's.
FAILING_SECONDS = 15 * 60
# The coordinator wakes a run that stops moving after 20 minutes (raigolmid coordinator.py
# IDLE_SECONDS), which writes to the event log; three such stretches with nothing written is a
# daemon no longer running the run.
EVENTS_SILENT_SECONDS = 3 * 20 * 60
# A second session sees the first's log; a third would repeat what two could not do, so the
# person is told instead.
SESSIONS_PER_INCIDENT = 2
# A session may wait on a release build and the upgrade it is installed by; one running past
# this is stuck, and is ended.
SESSION_SECONDS = 4 * 3600
# An alert is repeated only for a machine still down a day later, so the phone is not a pager.
REALERT_SECONDS = 24 * 3600
# The longest one ssh login and the probe it runs take on a guest that answers at all.
SSH_SECONDS = 30
# What a session's verdict line starts with (KEEPER.md).
VERDICT = "KEEPER:"
FIXED = "KEEPER: fixed"
LAUNCHER_STATES = {"starting", "running", "restarting", "gave_up", "upgrading", "closed"}
TRANSITIONAL = {"starting", "restarting", "upgrading"}
REQUESTS = {"start", "restart", "upgrade"}

VM_HOST, VM_USER = "host.docker.internal", "agent"
NTFY = "https://ntfy.sh/"


@dataclass(frozen=True)
class Paths:
    work: Path = Path("/work/RaiGolmi")
    launcher_home: Path = Path("/launcher")
    key: Path = Path("/root/.ssh/keeper_ed25519")

    @property
    def keeper(self) -> Path:
        return self.work / "keeper"

    @property
    def inbox(self) -> Path:
        return self.keeper / "inbox"

    @property
    def outbox(self) -> Path:
        return self.keeper / "outbox"

    @property
    def sessions(self) -> Path:
        return self.outbox / "sessions"

    @property
    def launcher_json(self) -> Path:
        return self.outbox / "launcher.json"

    @property
    def keeper_json(self) -> Path:
        return self.outbox / "keeper.json"

    @property
    def system_prompt(self) -> Path:
        return self.keeper / "KEEPER.md"

    @property
    def qemu_log(self) -> Path:
        return self.work / "windows" / "qemu.log"

    @property
    def authorized_keys(self) -> Path:
        return self.work / "windows" / "authorized_keys"

    @property
    def settings(self) -> Path:
        return self.launcher_home / "settings.ini"


@dataclass(frozen=True)
class Seen:
    """What one check found: `kind` is a state of the module docstring's list."""
    kind: str
    why: str | None = None
    evidence: dict[str, Any] | None = None
    act_now: bool = False


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def write_json(path: Path, data: Any) -> None:
    """Written whole or not at all: a reader never sees half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.partial")
    partial.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(partial, path)


def write_request(paths: Paths, request: str, why: str, now: float) -> Path:
    """A request in keeper/inbox/ for the launcher, or its door, to take and delete."""
    if request not in REQUESTS:
        raise ValueError(f"{request!r} is not a launcher request; one of {sorted(REQUESTS)}")
    path = paths.inbox / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now))}-{uuid.uuid4().hex[:8]}.json"
    write_json(path, {"request": request, "why": why, "at": iso(now)})
    logger.info("asked the launcher to %s: %s (%s)", request, why, path.name)
    return path


def pending_requests(paths: Paths) -> list[dict[str, Any]]:
    if not paths.inbox.is_dir():
        return []
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(paths.inbox.glob("*.json"))]


def ntfy_topic(paths: Paths) -> str:
    """The launcher's `ntfy-topic=` (windows/Settings.cs); empty is no alerts."""
    if not paths.settings.exists():
        return ""
    for raw in paths.settings.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line.startswith((";", "#")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "ntfy-topic":
            return value.strip()
    return ""


# --- the VM ---------------------------------------------------------------------------------

def vm_port(paths: Paths) -> int:
    """The launcher forwards ssh on 2222 when free and another port when not; qemu.log's first
    line, QEMU's command line, names the one this boot took."""
    first = paths.qemu_log.read_text(encoding="utf-8", errors="replace").split("\n", 1)[0]
    found = re.search(r"hostfwd=tcp:127\.0\.0\.1:(\d+)-:22", first)
    if found is None:
        raise ValueError(f"{paths.qemu_log}'s first line names no ssh forward")
    return int(found.group(1))


def ensure_key(paths: Paths) -> None:
    """The keeper's own key, made once on the persistent home, and its public half in the
    launcher's authorized_keys, which the guest is handed at boot: it lets the keeper in from
    the launcher's next boot."""
    if not paths.key.exists():
        paths.key.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "raigolmi-keeper",
                        "-f", str(paths.key)], check=True)
        logger.info("made the keeper's ssh key %s", paths.key)
    public = paths.key.with_name(paths.key.name + ".pub").read_text(encoding="utf-8").strip()
    body = public.split()[1]
    existing = paths.authorized_keys.read_text(encoding="utf-8") if paths.authorized_keys.exists() else ""
    if any(len(line.split()) > 1 and line.split()[1] == body for line in existing.splitlines()):
        return
    lead = "" if existing == "" or existing.endswith("\n") else "\n"
    with paths.authorized_keys.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(f"{lead}{public}\n")
    logger.info("added the keeper's key to %s; the launcher's next boot lets it in", paths.authorized_keys)


def connect(paths: Paths):
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(VM_HOST, port=vm_port(paths), username=VM_USER, key_filename=str(paths.key),
                   look_for_keys=False, allow_agent=False, timeout=SSH_SECONDS,
                   banner_timeout=SSH_SECONDS, auth_timeout=SSH_SECONDS)
    return client


def vm_run(client, command: str, timeout: float, stdin: str | None = None) -> tuple[int, str, str]:
    channel_in, out, err = client.exec_command(command, timeout=timeout)
    if stdin is not None:
        channel_in.write(stdin)
        channel_in.channel.shutdown_write()
    text, errors = out.read().decode(errors="replace"), err.read().decode(errors="replace")
    return out.channel.recv_exit_status(), text, errors


# Run on the guest by its own python3, so the ages are by the guest's clock, which wrote them.
PROBE_SCRIPT = r'''
import json, subprocess, time
from pathlib import Path
state = Path.home() / ".local/state/raigolmid"
logs = [str(p) for p in (state / "events.jsonl.1", state / "events.jsonl") if p.exists()]
daemon = subprocess.run(["systemctl", "--user", "is-active", "raigolmid"],
                        capture_output=True, text=True).stdout.strip()
def rows(text):
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue          # a line torn by a power cut or a killed writer
        if isinstance(row, dict) and "type" in row:
            yield row
last = None
if logs:
    tail = subprocess.run(["tail", "-q", "-n", "3", *logs], capture_output=True, text=True).stdout
    for row in rows(tail):
        last = row
hold = None
if logs:
    found = subprocess.run(["grep", "-h", "-E", '"type": "account\\.(limited|resumed)"', *logs],
                           capture_output=True, text=True).stdout
    for row in rows(found):
        hold = row
intent = state / "intent.json"
run = json.loads(intent.read_text()).get("run") if intent.exists() else None
seen = subprocess.run(["rai", "keeper", "seen"], capture_output=True, text=True)
print(json.dumps({
    "daemon": daemon,
    "faults": json.loads(seen.stdout)["faults"] if seen.returncode == 0 else None,
    "faults_error": None if seen.returncode == 0 else (seen.stdout + seen.stderr).strip()[-2000:],
    "last_event": None if last is None else {"type": last["type"], "age_s": round(time.time() - last["ts"])},
    "held": hold is not None and hold["type"] == "account.limited",
    "hold": hold,
    "run_active": run is not None and run.get("ended") is None,
}))
'''


def probe_vm(paths: Paths) -> dict[str, Any]:
    """The guest as the loop judges it. ssh failing is an answer (`reachable` False), not an
    error: it is the commonest thing the keeper is here to see."""
    import paramiko
    try:
        client = connect(paths)
    except (OSError, ValueError, paramiko.SSHException) as exc:
        return {"reachable": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        status, out, err = vm_run(client, "python3 -", SSH_SECONDS, stdin=PROBE_SCRIPT)
    except (OSError, paramiko.SSHException) as exc:
        return {"reachable": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        client.close()
    if status != 0:
        return {"reachable": True, "probe_failed": f"exit {status}: {err.strip()[-2000:]}"}
    return {"reachable": True, **json.loads(out)}


def rai_keeper(paths: Paths, args: list[str]) -> dict[str, Any]:
    """`rai keeper <args>` on the guest: {"ok": True, "result"}, or {"ok": False, "error"} when
    ssh or raigolmid did not answer or raigolmid refused, which the caller says and outlives."""
    import paramiko
    try:
        client = connect(paths)
        try:
            status, out, err = vm_run(client, shlex.join(["rai", "keeper", *args]), SSH_SECONDS)
        finally:
            client.close()
    except (OSError, ValueError, paramiko.SSHException) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if status != 0:
        return {"ok": False, "error": f"exit {status}: {(out + err).strip()[-2000:]}"}
    return {"ok": True, "result": json.loads(out)}


# --- Claude Code ----------------------------------------------------------------------------

def claude_ready() -> tuple[bool, str]:
    """Whether Claude Code is signed in (`claude auth status --json`'s `loggedIn`)."""
    done = subprocess.run(["claude", "auth", "status", "--json"], capture_output=True, text=True,
                          timeout=60)
    try:
        status = json.loads(done.stdout)
    except ValueError:
        return False, f"`claude auth status` answered: {(done.stdout + done.stderr).strip()[:300]}"
    if not status.get("loggedIn"):
        return False, "Claude Code is not signed in"
    return True, ""


def read_outcome(log: Path) -> dict[str, Any]:
    """A session's verdict from its stream-json log: the last line starting `KEEPER:` of the
    final `result` message; `api_error` when the API refused the session itself."""
    result = None
    if log.exists():
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("type") == "result":
                result = row
    if result is None:
        return {"verdict": "KEEPER: not fixed: the session ended without a result", "api_error": None}
    text = str(result.get("result") or "")
    if result.get("is_error") and result.get("api_error_status") is not None:
        return {"verdict": f"KEEPER: not fixed: Claude Code's API refused the session ({text[:300]})",
                "api_error": f"{result.get('api_error_status')}: {text[:300]}"}
    verdicts = [line.strip() for line in text.splitlines() if line.strip().startswith(VERDICT)]
    return {"verdict": verdicts[-1] if verdicts else "KEEPER: not fixed: the session gave no verdict",
            "api_error": None}


def run_claude(paths: Paths, prompt: str, session_id: str, log: Path) -> dict[str, Any]:
    """One headless session with every permission, as the keeper's container is the sandbox
    (IS_SANDBOX). Its stream-json transcript is the log; the conversation itself is kept by
    Claude Code under /root, so `claude --resume <session_id>` reopens it."""
    command = ["claude", "-p", "--dangerously-skip-permissions", "--verbose",
               "--output-format", "stream-json", "--session-id", session_id,
               "--append-system-prompt", paths.system_prompt.read_text(encoding="utf-8"), prompt]
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as fh:
        process = subprocess.Popen(command, cwd=paths.work, stdout=fh, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            process.wait(timeout=SESSION_SECONDS)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            return {"verdict": f"KEEPER: not fixed: the session ran past {SESSION_SECONDS // 3600} hours "
                               "and was ended", "api_error": None}
    return read_outcome(log)


def push_ntfy(topic: str, title: str, message: str) -> None:
    request = urllib.request.Request(NTFY + topic, data=message.encode("utf-8"), method="POST",
                                     headers={"Title": title})
    with urllib.request.urlopen(request, timeout=30) as answer:
        answer.read()


# --- the loop -------------------------------------------------------------------------------

ALERT_NOT_FIXED = (
    "RaiGolmi needs you",
    "RaiGolmi on the computer has stopped and I could not fix it myself. Please restart the "
    "computer: Start menu, Power, Restart. After it comes back, sign in to Windows as usual. "
    "Nothing else to do.")
ALERT_CANNOT_RUN = (
    "RaiGolmi helper is stuck",
    "RaiGolmi on the computer has stopped, and its repair helper cannot work right now ({why}). "
    "Please restart the computer: Start menu, Power, Restart. After it comes back, sign in to "
    "Windows as usual. Nothing else to do.")
ALERT_RECOVERED = ("RaiGolmi is working again", "RaiGolmi is working again. Nothing to do.")


class Keeper:
    """The decision loop. Everything that reaches outside it is handed in, so a test runs it
    against doubles: `clock` (epoch seconds), `probe` (the VM), `runner` (one Claude Code
    session: prompt, id, log -> {verdict, api_error}), `ready` (Claude Code signed in), `push`
    (one ntfy alert), and `tell` (`rai keeper <args>` on the VM -> {ok, result or error})."""

    def __init__(self, paths: Paths, clock: Callable[[], float],
                 probe: Callable[[], dict[str, Any]],
                 runner: Callable[[str, str, Path], dict[str, Any]],
                 ready: Callable[[], tuple[bool, str]],
                 push: Callable[[str, str, str], None],
                 tell: Callable[[list[str]], dict[str, Any]]) -> None:
        self.paths, self.clock, self.probe = paths, clock, probe
        self.runner, self.ready, self.push, self.tell = runner, ready, push, tell
        self.state, self.why = "starting", None
        self.incident: dict[str, Any] | None = None
        self.last_incident: dict[str, Any] | None = None
        self.last_alert: dict[str, Any] | None = None
        # The launcher's transitional state and when the keeper first saw it.
        self.transition: tuple[str, float] | None = None
        # Answers to faults the VM has not yet taken: fault id -> {verdict, words}.
        self.unanswered: dict[str, dict[str, str]] = {}
        if paths.keeper_json.exists():
            kept = json.loads(paths.keeper_json.read_text(encoding="utf-8"))
            self.incident = kept["incident"]
            self.last_incident = kept["last_incident"]
            self.last_alert = kept["last_alert"]
            self.unanswered = kept["unanswered"]

    # --- seeing ---------------------------------------------------------------------------
    def observe(self, now: float) -> Seen:
        if not self.paths.launcher_json.exists():
            return Seen("gone", "the launcher has never written launcher.json")
        launcher = json.loads(self.paths.launcher_json.read_text(encoding="utf-8"))
        state = launcher["state"]
        if state not in LAUNCHER_STATES:
            raise ValueError(f"launcher.json names state {state!r}, not one of {sorted(LAUNCHER_STATES)}")
        evidence = {"launcher": launcher}
        if state == "closed":
            return Seen("closed", launcher.get("why"))
        if state == "gave_up":
            return Seen("failing", f"the launcher gave up after five restarts in a row: {launcher.get('why')}",
                        evidence, act_now=True)
        age = now - parse_iso(launcher["at"])
        if age > LAUNCHER_STALE_SECONDS:
            return Seen("gone", f"the launcher last wrote launcher.json {round(age)} s ago, as {state}",
                        {**evidence, "inbox": pending_requests(self.paths)})
        if state in TRANSITIONAL:
            if self.transition is None or self.transition[0] != state:
                self.transition = (state, now)
            stuck = now - self.transition[1]
            if stuck > TRANSITION_SECONDS:
                return Seen("failing", f"the launcher has been {state} for {round(stuck / 60)} minutes",
                            evidence)
            return Seen("waiting", f"the launcher is {state}")
        self.transition = None
        vm = self.probe()
        evidence["vm"] = vm
        if not vm["reachable"]:
            return Seen("failing", f"the VM does not answer ssh ({vm['error']})", evidence)
        if "probe_failed" in vm:
            return Seen("failing", f"the probe on the VM failed ({vm['probe_failed']})", evidence)
        if vm["daemon"] != "active":
            return Seen("failing", f"raigolmid is {vm['daemon'] or 'unknown'} on the VM", evidence)
        if vm["faults_error"] is not None:
            return Seen("failing", f"raigolmid did not answer `rai keeper seen` ({vm['faults_error']})",
                        evidence)
        if vm["held"]:
            return Seen("held", "the account is on its usage limit")
        last = vm["last_event"]
        if vm["run_active"] and last is not None and last["age_s"] > EVENTS_SILENT_SECONDS:
            return Seen("failing", f"a run is in progress and raigolmid has written no event for "
                                   f"{round(last['age_s'] / 60)} minutes (the last: {last['type']})",
                        evidence)
        faults = [f for f in vm["faults"] if f["id"] not in self.unanswered]
        if faults:
            fault = faults[0]
            return Seen("failing", f"the machine tab reported a fault in RaiGolmi: {fault['title']}",
                        {**evidence, "fault": fault}, act_now=True)
        return Seen("healthy")

    # --- acting ---------------------------------------------------------------------------
    def check(self) -> None:
        now = self.clock()
        if self.unanswered:
            self._send_answers()
        seen = self.observe(now)
        self.why = seen.why
        if seen.kind in ("healthy", "closed", "held"):
            self.state = seen.kind
            self._close(now)
        elif seen.kind == "waiting":
            self.state = "waiting"
        else:
            self.state = "failing"
            self._fail(seen, now)
        self.report(now)

    def _close(self, now: float) -> None:
        if self.incident is None:
            return
        logger.info("the incident since %s is over: %s", iso(self.incident["since"]), self.state)
        if self.incident["alerted_at"] is not None:
            self._alert(*ALERT_RECOVERED, now, repeat=True)
        self.last_incident = {**self.incident, "ended": iso(now), "ended_as": self.state}
        self.incident = None

    def _fail(self, seen: Seen, now: float) -> None:
        fault = (seen.evidence or {}).get("fault")
        same = (self.incident["fault"] or {}).get("id") if self.incident is not None else None
        if self.incident is not None and same != (fault or {}).get("id"):
            # A fault is its own incident, with its own sessions and its own answer.
            self.last_incident = {**self.incident, "ended": iso(now), "ended_as": "superseded"}
            self.incident = None
        if self.incident is None:
            logger.warning("failing: %s", seen.why)
            self.incident = {"since": now, "reason": seen.why, "evidence": seen.evidence,
                             "act_at": now + FAILING_SECONDS, "start_requested_at": None,
                             "sessions": [], "alerted_at": None, "fault": fault}
        incident = self.incident
        incident["reason"], incident["evidence"] = seen.why, seen.evidence
        if seen.kind == "gone" and incident["start_requested_at"] is None:
            if not any(r["request"] == "start" for r in pending_requests(self.paths)):
                write_request(self.paths, "start", seen.why, now)
            incident["start_requested_at"] = now
            incident["act_at"] = now + START_GRACE_SECONDS
            return
        # `act_now` is a failure the launcher has already given up on; after a session even it
        # waits for what that session started to take.
        if now < incident["act_at"] and not (seen.act_now and not incident["sessions"]):
            return
        if len(incident["sessions"]) >= SESSIONS_PER_INCIDENT:
            self._alert(*ALERT_NOT_FIXED, now)
            return
        ready, why = self.ready()
        if not ready:
            title, message = ALERT_CANNOT_RUN
            self._alert(title, message.format(why=why), now)
            if fault is not None:
                self._answer(fault["id"], "not_fixed", f"the keeper's Claude Code cannot run: {why}",
                             now)
            return
        self._session(incident, now)

    def _session(self, incident: dict[str, Any], now: float) -> None:
        session_id = str(uuid.uuid4())
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
        log = self.paths.sessions / f"{stamp}.log"
        notes = self.paths.sessions / f"{stamp}.md"
        fault = incident["fault"]
        # Said to the daemon, so the machine tab knows a keeper is there while its loop does
        # not look; a fault is taken, and a fault the daemon will not let be taken gets no session.
        told = self.tell([*(["take", fault["id"]] if fault else ["seen"]),
                          "--busy", str(SESSION_SECONDS)])
        if not told["ok"]:
            logger.warning("the VM did not take the keeper's word before a session: %s",
                           told["error"])
            if fault is not None:
                return
        self.state = "repairing"
        self.report(now)
        logger.info("session %s on: %s (log %s)", session_id, incident["reason"], log)
        outcome = self.runner(self.prompt(incident, session_id, notes), session_id, log)
        ended = self.clock()
        incident["sessions"].append({
            "id": session_id, "started": iso(now), "ended": iso(ended), "log": str(log),
            "notes": str(notes), "verdict": outcome["verdict"],
            "resume": f"cd /work/RaiGolmi && claude --resume {session_id}"})
        incident["act_at"] = ended + FAILING_SECONDS
        logger.info("session %s ended: %s", session_id, outcome["verdict"])
        if outcome["api_error"] is not None:
            title, message = ALERT_CANNOT_RUN
            self._alert(title, message.format(why="its account was refused"), ended)
        if fault is None:
            return
        fixed = outcome["verdict"] == FIXED
        spent = len(incident["sessions"]) >= SESSIONS_PER_INCIDENT
        if fixed or spent or outcome["api_error"] is not None:
            if not fixed and outcome["api_error"] is None:
                self._alert(*ALERT_NOT_FIXED, ended)
            words = (outcome["verdict"].removeprefix(VERDICT).strip()
                     .removeprefix("not fixed:").strip() or outcome["verdict"])
            self._answer(fault["id"], "fixed" if fixed else "not_fixed", "" if fixed else words,
                         ended)

    def _answer(self, fault_id: str, verdict: str, words: str, now: float) -> None:
        """The fault's answer to the machine tab, which ends its incident."""
        self.unanswered[fault_id] = {"verdict": verdict, "words": words}
        self.last_incident = {**self.incident, "ended": iso(now), "ended_as": f"answered {verdict}"}
        self.incident = None
        self._send_answers()

    def _send_answers(self) -> None:
        for fault_id, answer in list(self.unanswered.items()):
            told = self.tell(["answer", fault_id, answer["verdict"],
                              *([answer["words"]] if answer["words"] else [])])
            if told["ok"]:
                logger.info("answered fault %s: %s", fault_id, answer["verdict"])
                del self.unanswered[fault_id]
            else:
                logger.warning("fault %s's answer did not reach the VM; sent again next check: %s",
                               fault_id, told["error"])

    def prompt(self, incident: dict[str, Any], session_id: str, notes: Path) -> str:
        qemu_tail = ""
        if self.paths.qemu_log.exists():
            qemu_tail = "\n".join(self.paths.qemu_log.read_text(
                encoding="utf-8", errors="replace").splitlines()[-40:])
        earlier = "".join(f"\n- session {s['id']} ({s['started']}): {s['verdict']}; its log is "
                          f"{s['log']}, its notes {s['notes']}" for s in incident["sessions"])
        return (
            f"The keeper found the machine failing since {iso(incident['since'])}: "
            f"{incident['reason']}.\n\nWhat the check gathered:\n"
            f"{json.dumps(incident['evidence'], indent=2)}\n\n"
            f"The last lines of windows/qemu.log:\n{qemu_tail}\n\n"
            + (f"Earlier keeper sessions on this incident did not fix it:{earlier}\n"
               "Read their logs and notes before acting.\n\n" if earlier else "")
            + (f"The machine tab paused the tabs this fault affects "
               f"({', '.join(incident['fault']['affects']) or 'none named'}) until it is "
               "answered: your verdict line is its answer, and `fixed` resumes them.\n\n"
               if incident["fault"] else "")
            + f"You are session {session_id}. Write what you find and do into {notes}. "
            f"Get the machine running again, then find the root cause. End with the verdict line.")

    def _alert(self, title: str, message: str, now: float, repeat: bool = False) -> None:
        incident = self.incident
        if (not repeat and incident is not None and incident["alerted_at"] is not None
                and now - incident["alerted_at"] < REALERT_SECONDS):
            return
        topic = ntfy_topic(self.paths)
        if not topic:
            outcome = "no ntfy topic is set in settings.ini"
        else:
            try:
                self.push(topic, title, message)
                outcome = "sent"
            except (urllib.error.URLError, OSError) as exc:
                # Not marked alerted: the next check tries again.
                logger.error("the alert '%s' did not reach ntfy.sh: %s", title, exc)
                self.last_alert = {"at": iso(now), "title": title, "message": message,
                                   "outcome": f"failed: {exc}"}
                return
        logger.warning("alert '%s': %s", title, outcome)
        self.last_alert = {"at": iso(now), "title": title, "message": message, "outcome": outcome}
        if incident is not None:
            incident["alerted_at"] = now

    def report(self, now: float) -> None:
        write_json(self.paths.keeper_json, {
            "at": iso(now), "state": self.state, "why": self.why, "incident": self.incident,
            "last_incident": self.last_incident, "last_alert": self.last_alert,
            "unanswered": self.unanswered})


def loop(paths: Paths) -> int:
    ensure_key(paths)
    paths.inbox.mkdir(parents=True, exist_ok=True)
    keeper = Keeper(paths, time.time, lambda: probe_vm(paths),
                    lambda prompt, sid, log: run_claude(paths, prompt, sid, log),
                    claude_ready, push_ntfy, lambda args: rai_keeper(paths, args))
    logger.info("keeping %s, every %d s", paths.work, CHECK_SECONDS)
    while True:
        keeper.check()
        time.sleep(CHECK_SECONDS)


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")
    paths = Paths()
    if not argv:
        return loop(paths)
    verb, args = argv[0], argv[1:]
    if verb == "vm":
        client = connect(paths)
        try:
            status, out, err = vm_run(client, args[0], float(args[1]) if len(args) > 1 else 60)
        finally:
            client.close()
        sys.stdout.write(out)
        sys.stderr.write(err)
        return status
    if verb == "request":
        write_request(paths, args[0], args[1], time.time())
        return 0
    raise SystemExit(f"unknown verb {verb!r}: none, `vm '<command>' [secs]`, or "
                     "`request <start|restart|upgrade> '<why>'`")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
