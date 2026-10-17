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
    building  healthy, no fault waiting, and a feature the owner asked for waits
    failing   anything else, with the reason

and acts on a failure by running one headless Claude Code session (KEEPER.md is its appended
system prompt) with the evidence it gathered. The rules:

- launcher.json `gave_up` (five restarts in a row failed) is acted on at once;
- launcher.json unchanged for `LAUNCHER_STALE_SECONDS` of the keeper's own clock, its state not
  `closed`, is the launcher gone: one `start` request goes into keeper/inbox/ (the launcher's
  scheduled door takes it), and the failure is acted on only if the launcher is still gone
  `START_GRACE_SECONDS` later;
- while a launcher that gave up or is gone gets no session (its incident's sessions are spent,
  or Claude Code cannot run), a `start` request is written again every
  `START_RETRY_SECONDS`: the door ignores `gave_up`, so nothing else would start it;
- a launcher stuck in one of its transitional states for `TRANSITION_SECONDS` is failing;
- the VM unreachable by ssh, raigolmid not active or restarted by systemd `DAEMON_RESTARTS` times
  within `DAEMON_RESTART_WINDOW` (a crash loop `is-active` does not show), or a run in progress
  with a tab not paused or held for the user whose event log has been silent for
  `EVENTS_SILENT_SECONDS`, while the launcher says `running`,
  is failing;
- each probe also says the keeper looked (`rai keeper seen`, raigolmid's faults.py), which is how
  the machine tab knows a keeper is there, and is answered with the faults in RaiGolmi the machine
  tab filed for it, having paused the tabs each affects. An open fault is failing at once and is
  its own incident: the keeper takes it (`rai keeper take`), hands the session its text, and
  answers it (`rai keeper answer`) with the verdict, so the machine tab resumes its tabs:
  `fixed`; or `not_fixed` with the verdict's words once the incident's sessions are spent or
  Claude Code cannot run. The answer ends the incident; one the VM did not take is sent again at
  each check until it is. A fault never alerts the person near the machine: the run goes on
  around it, so the answer is the machine tab's alone;
- a session is paced as every tab is (`pace`, from the plan raigolmid's `limits.plan_view`
  reads): a window on course past its allowance has it work narrower, never wait. Only the
  account's usage limit holds a fault, as it holds every agent: it is answered `deferred`, so it
  is not filed again, and taken up once the limit lifts;
- a feature the owner asked for through the machine tab (`request_feature`, a record of kind
  `feature` in the same list) is taken only when every check above passes and no fault waits,
  and goes through a fault's incident, sessions and answer, but sends no alert: a feature not
  built is the owner's news, never the person's by the PC;
- any other failure is acted on once it has lasted `FAILING_SECONDS`, and again that long after
  each session, so what a session started (an upgrade, a restart) has time to take;
- an incident is the stretch from the first failing check to the next healthy, closed or held
  one. It gets at most `SESSIONS_PER_INCIDENT` sessions, each after the first handed the logs of
  those before. A session that never had an answer from Claude (an API error, the network) does not count; one
  that the API refused for its sign-in stops sessions until the sign-in changes;
- a session's prompt says where it may push (`push_targets`, from the container's GitHub
  sign-in): UPSTREAM's repositories when the sign-in is UPSTREAM, its fork of each when it has one
  it can push to, and nowhere otherwise, so a machine with no fork keeps its fixes in its clone;
- Claude Code runs on the machine's own agent credential (`MACHINE_CREDENTIAL`), copied to
  `Paths.credential` at each probe that reaches the VM, so it follows the owner's sign-in there
  and outlasts a VM that does not answer; the keeper has no sign-in of its own;
- the person near the machine gets an ntfy alert when sessions cannot fix a machine that is down (spent, the sign-in ran out, Claude Code cannot run, or Claude unreachable for a day), each
  alert title at most once in `REALERT_SECONDS` across incidents, and "working again" once the
  machine is healthy after one. None is sent while raigolmid's line out has already told them
  a cause (`line-out.json`) during the incident: theirs is the word that stands.

keeper/outbox/keeper.json is written each check: the state, the keeper.py it runs (`SOURCE`), the open incident with its sessions
(each resumable from the keeper's terminal by the `resume` command it records), the sessions of
and the alerts. It is also the keeper's memory across a restart of its container.
The loop's log goes to keeper/outbox/keeper.log as well as stderr.

    python3 keeper.py                         the loop (the container's entrypoint)
    python3 keeper.py vm '<command>' [secs]   one command on the VM, with the keeper's key
    python3 keeper.py request <start|restart|upgrade> '<why>'   a request to the launcher
"""
from __future__ import annotations

import hashlib
import http.client
import inspect
import json
import logging
import logging.handlers
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
# not one that is slow. Measured on the keeper's own clock, as the launcher's is another.
LAUNCHER_STALE_SECONDS = 60
# The launcher's scheduled door takes a request every 5 minutes and the machine then boots;
# three door runs with no launcher is a start that was not taken.
START_GRACE_SECONDS = 15 * 60
# A start that fails is at most five restarts of up to half an hour each before the launcher
# gives up again, and costs no plan usage; what stops a machine and then clears on its own (a
# Windows update, a host disk freed, the network back) clears in hours. One start each such
# stretch retries without running the launcher's restarts back to back.
START_RETRY_SECONDS = 3 * 3600
# The longest a launcher state between two runs lasts is an upgrade downloading the disk; past
# this it is stuck rather than slow.
TRANSITION_SECONDS = 2 * 3600
# A failure is the launcher's hang recovery's and the guest janitor's first; a boot through to
# the daemon is a few minutes. One that outlasts them is the keeper's.
FAILING_SECONDS = 15 * 60
# The coordinator wakes a run that stops moving after 20 minutes (raigolmid coordinator.py
# IDLE_SECONDS), which writes to the event log; three such stretches with nothing written is a
# daemon no longer running the run. It wakes only a run with a managed tab neither paused nor
# held for the user (`run_working`), so a run whose every tab waits on them is quiet for hours.
EVENTS_SILENT_SECONDS = 3 * 20 * 60
# systemd restarting raigolmid once is its repair working; again within the half hour is that
# repair not holding (StartLimitIntervalSec=0 restarts it forever, and `is-active` reads
# `active` between crashes).
DAEMON_RESTARTS = 2
DAEMON_RESTART_WINDOW = 30 * 60
# A second session sees the first's log; a third would repeat what two could not do, so the
# person is told instead.
SESSIONS_PER_INCIDENT = 2
DAY = 24 * 3600
# A session that never had an answer from Claude spent nothing, and an outage or a network
# down for less than a day passes on its own; a day of them is told (raigolmid lineout.py's
# REFUSED_FOR, the owner's word).
UNREACHED_SECONDS = DAY
# raigolmid's line out sends each cause at most once a day, so a cause that holds was sent
# within the day before an incident it caused.
LINE_OUT_COVERS_SECONDS = DAY
# The longest `claude auth status` takes when Claude Code works at all.
CLAUDE_STATUS_SECONDS = 60
# A session may wait on a release build and the upgrade it is installed by; one running past
# this is stuck, and is ended.
SESSION_SECONDS = 4 * 3600
# An alert is repeated only for a machine still down a day later, so the phone is not a pager.
REALERT_SECONDS = DAY
# The longest one ssh login and the probe it runs take on a guest that answers at all.
SSH_SECONDS = 30
# The newest Opus the installed Claude Code knows (the owner's call, 2026-10-16), and that Claude
# Code brought to its newest before each session, so a new Opus reaches the keeper by itself.
MODEL = "opus"
UPDATE_SECONDS = 300
# What a session's verdict line starts with (KEEPER.md).
VERDICT = "KEEPER:"
FIXED = "KEEPER: fixed"
LAUNCHER_STATES = {"starting", "running", "restarting", "gave_up", "upgrading", "closed"}
TRANSITIONAL = {"starting", "restarting", "upgrading"}
REQUESTS = {"start", "restart", "upgrade"}

VM_HOST, VM_USER = "host.docker.internal", "agent"
# The machine's agent credential, as raigolmid's `Paths.agent_credentials` places it and
# `credential.py` writes it: one `KEY=value` line, KEY one of CREDENTIAL_KEYS.
MACHINE_CREDENTIAL = "${XDG_CONFIG_HOME:-$HOME/.config}/raigolmi/agent-credentials"
CREDENTIAL_KEYS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
NTFY = "https://ntfy.sh/"
# The sha256 of this file as the loop loaded it, written to keeper.json: the launcher carries
# the keeper's files to a project folder that is not a git checkout and recreates the container
# while this differs from the keeper.py it carries (RaiGolmi.cs Keeper.Deliver).
SOURCE = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


@dataclass(frozen=True)
class Paths:
    work: Path = Path("/work/RaiGolmi")
    # The launcher's state, <P>\state (compose.yml): its settings, keys, disk record, UEFI vars.
    launcher_home: Path = Path("/launcher")
    key: Path = Path("/root/.ssh/keeper_ed25519")
    # The machine's agent credential as last copied (MACHINE_CREDENTIAL), on the persistent home.
    credential: Path = Path("/root/.keeper-credential")

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
    def log(self) -> Path:
        return self.outbox / "keeper.log"

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
    # The launcher gave up or is gone: only a `start` request runs the machine again.
    launcher_down: bool = False


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def write_json(path: Path, data: Any) -> None:
    """Written whole or not at all, across a power cut too: the data is synced before the rename,
    or NTFS can keep the rename and the size while the data never reached the disk (a file of NULs)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.partial")
    with open(partial, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, indent=2) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
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


def run_working(intent: dict | None) -> bool:
    """Whether raigolmid's intent.json holds a run the coordinator keeps moving: one not ended
    with a managed tab that is neither paused nor held for the user (coordinator.py `tick`'s
    `run_idle`). Its source is part of PROBE_SCRIPT, so it runs on the guest."""
    if intent is None or intent.get("run") is None or intent["run"].get("ended") is not None:
        return False
    return any(tab["managed"] and tab["status"] != "paused" and tab["held"] is None
               for tab in intent["tabs"].values())


# Run on the guest by its own python3, so the ages are by the guest's clock, which wrote them.
PROBE_SCRIPT = inspect.getsource(run_working) + r'''
import json, subprocess, time
from pathlib import Path
state = Path.home() / ".local/state/raigolmid"
logs = [str(p) for p in (state / "events.jsonl.1", state / "events.jsonl") if p.exists()]
daemon = subprocess.run(["systemctl", "--user", "is-active", "raigolmid"],
                        capture_output=True, text=True).stdout.strip()
restarts = int(subprocess.run(["systemctl", "--user", "show", "-p", "NRestarts", "--value", "raigolmid"],
                              capture_output=True, text=True, check=True).stdout)
line_out = state / "line-out.json"
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
seen = subprocess.run(["rai", "keeper", "seen"], capture_output=True, text=True)
print(json.dumps({
    "daemon": daemon,
    "restarts": restarts,
    "line_out": json.loads(line_out.read_text()) if line_out.exists() else None,
    "faults": json.loads(seen.stdout)["faults"] if seen.returncode == 0 else None,
    "plan": json.loads(seen.stdout)["plan"] if seen.returncode == 0 else None,
    "faults_error": None if seen.returncode == 0 else (seen.stdout + seen.stderr).strip()[-2000:],
    "last_event": None if last is None else {"type": last["type"], "age_s": round(time.time() - last["ts"])},
    "held": hold is not None and hold["type"] == "account.limited",
    "hold": hold,
    "run_working": run_working(json.loads(intent.read_text()) if intent.exists() else None),
}))
'''


def parse_credential(text: str) -> tuple[str, str]:
    """(KEY, value) of a credential file's one line, or ValueError saying what is wrong."""
    lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if len(lines) != 1:
        raise ValueError(f"the machine's credential has {len(lines)} lines, not one")
    key, sep, value = lines[0].partition("=")
    key, value = key.strip(), value.strip()
    if not sep or key not in CREDENTIAL_KEYS or not value:
        raise ValueError(f"the machine's credential is not KEY=value with KEY one of {', '.join(CREDENTIAL_KEYS)}")
    return key, value


def copy_credential(paths: Paths, client) -> None:
    """The machine's agent credential into `Paths.credential` (0600) when it differs. A machine
    with none, or one that cannot be read, leaves the last copy, logged: the copy is what a
    session runs on while the VM does not answer."""
    import paramiko
    try:
        status, out, err = vm_run(client, f"cat {MACHINE_CREDENTIAL}", SSH_SECONDS)
    except (OSError, paramiko.SSHException) as exc:
        logger.warning("the machine's credential was not read: %s: %s", type(exc).__name__, exc)
        return
    if status != 0:
        logger.warning("the machine's credential was not read (exit %d): %s", status, err.strip()[-300:])
        return
    try:
        key, value = parse_credential(out)
    except ValueError as exc:
        logger.warning("%s; the last copy stays", exc)
        return
    line = f"{key}={value}\n"
    if paths.credential.exists() and paths.credential.read_text(encoding="utf-8") == line:
        return
    partial = paths.credential.with_name(f".{paths.credential.name}.partial")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(line)
    os.replace(partial, paths.credential)
    logger.info("copied the machine's agent credential (%s, %s)", key, fingerprint(value))


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
        client.close()
        return {"reachable": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        copy_credential(paths, client)
    finally:
        client.close()
    if status != 0:
        return {"reachable": True, "probe_failed": f"exit {status}: {err.strip()[-2000:]}"}
    try:
        return {"reachable": True, **json.loads(out)}
    except ValueError:
        return {"reachable": True, "probe_failed": f"it printed no JSON: {out.strip()[-2000:]}"}


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
    try:
        return {"ok": True, "result": json.loads(out)}
    except ValueError:
        return {"ok": False, "error": f"it printed no JSON: {out.strip()[-2000:]}"}


# --- Claude Code ----------------------------------------------------------------------------

def fingerprint(token: str) -> str:
    """Names a token in keeper.json without writing the token there."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def claude_env(paths: Paths) -> tuple[dict[str, str], str]:
    """The environment Claude Code runs in, with the copied credential as its sign-in, and that
    credential's fingerprint."""
    key, value = parse_credential(paths.credential.read_text(encoding="utf-8"))
    env = {k: v for k, v in os.environ.items() if k not in CREDENTIAL_KEYS}
    return {**env, key: value}, fingerprint(value)


def claude_ready(paths: Paths, refused: str | None) -> tuple[str, str]:
    """("ready", ""), ("signed_out", why) when the sign-in is missing or the token
    the API refused (`refused`, its fingerprint), or ("cannot_run", why) when Claude Code does
    not answer `claude auth status`. Whether the API takes a token is learnt from a session,
    where a refusal costs no plan usage; asking it here would spend some each check."""
    if not paths.credential.exists():
        return "signed_out", "it has not yet copied the machine's agent credential"
    env, token = claude_env(paths)
    if token == refused:
        return "signed_out", "Claude refused the machine's agent credential"
    try:
        done = subprocess.run(["claude", "auth", "status", "--json"], capture_output=True, text=True,
                              env=env, timeout=CLAUDE_STATUS_SECONDS)
    except subprocess.TimeoutExpired:
        return "cannot_run", f"Claude Code did not answer within {CLAUDE_STATUS_SECONDS} s"
    except OSError as exc:
        return "cannot_run", f"Claude Code did not start: {exc}"
    try:
        status = json.loads(done.stdout)
    except ValueError:
        return "cannot_run", f"`claude auth status` answered: {(done.stdout + done.stderr).strip()[:300]}"
    if not status.get("loggedIn"):
        return "signed_out", "Claude Code does not take its sign-in"
    return "ready", ""


def read_outcome(log: Path) -> dict[str, Any]:
    """A session's verdict from its stream-json log: the last line starting `KEEPER:` of the
    final `result` message. `reached`: Claude answered at least once, so the session spent
    plan usage. `signed_out`: the API refused the sign-in (401). `unreached`: the session ended
    on an API error, or never had an answer (the network), so it did no repair."""
    result, reached = None, False
    if log.exists():
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("type") == "assistant":
                reached = True
            if isinstance(row, dict) and row.get("type") == "result":
                result = row
    text = "" if result is None else str(result.get("result") or "")
    status = None if result is None or not result.get("is_error") else result.get("api_error_status")
    outcome = {"reached": reached, "signed_out": status == 401, "unreached": None}
    if status == 401:
        return {**outcome, "verdict": f"KEEPER: not fixed: Claude refused the keeper's sign-in ({text[:300]})"}
    if status is not None:
        outcome["unreached"] = f"the API answered {status}: {text[:300]}"
    elif not reached:
        outcome["unreached"] = f"Claude never answered: {text[:300] or 'the session ended without a result'}"
    if outcome["unreached"] is not None:
        return {**outcome, "verdict": f"KEEPER: not fixed: {outcome['unreached']}"}
    verdicts = [line.strip() for line in text.splitlines() if line.strip().startswith(VERDICT)]
    return {**outcome, "verdict": verdicts[-1] if verdicts else "KEEPER: not fixed: the session gave no verdict"}


def run_claude(paths: Paths, prompt: str, session_id: str, log: Path) -> dict[str, Any]:
    """One headless session on `MODEL` with every permission, as the keeper's container is the sandbox
    (IS_SANDBOX). Its stream-json transcript is the log; the conversation itself is kept by
    Claude Code under /root, so `claude --resume <session_id>` reopens it. `token` names the
    sign-in it ran with."""
    update = subprocess.run(["claude", "update"], capture_output=True, text=True,
                            timeout=UPDATE_SECONDS)
    if update.returncode != 0:
        # A repair is not held back for the updater; the session runs on the installed version.
        logger.warning("claude update exited %d: %s", update.returncode,
                       (update.stdout + update.stderr).strip()[-2000:])
    command = ["claude", "-p", "--model", MODEL, "--dangerously-skip-permissions", "--verbose",
               "--output-format", "stream-json", "--session-id", session_id,
               "--append-system-prompt", paths.system_prompt.read_text(encoding="utf-8"), prompt]
    env, token = claude_env(paths)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as fh:
        process = subprocess.Popen(command, cwd=paths.work, stdout=fh, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, env=env, start_new_session=True)
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
                               "and was ended", "reached": True, "signed_out": False, "unreached": None,
                    "token": token}
    return {**read_outcome(log), "token": token}


# --- GitHub --------------------------------------------------------------------------------

# The repositories a keeper's fixes go to (KEEPER.md), and whose they are.
UPSTREAM = "ChristianBlevens"
REPOSITORIES = ("RaiGolmi", "raigolmi-packages")
GH_SECONDS = 60
# A request that had no answer is asked again: the container's DNS flaps and a retry succeeds.
GH_ATTEMPTS = 3


def gh(args: list[str]) -> tuple[int, str, str]:
    """`gh <args>` on the container's GitHub sign-in: exit code, stdout, stderr."""
    try:
        done = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=GH_SECONDS)
    except subprocess.TimeoutExpired:
        return -1, "", f"gh did not answer within {GH_SECONDS} s"
    return done.returncode, done.stdout, done.stderr


def ask_github(run: Callable[[list[str]], tuple[int, str, str]],
               args: list[str]) -> tuple[str, str]:
    """("answered", stdout), ("signed_out", why) when gh has no sign-in or GitHub refuses it
    (gh's exit 4, or a 401), or ("unasked", why) when no attempt had an answer."""
    for _ in range(GH_ATTEMPTS):
        code, out, err = run(args)
        if code == 0:
            return "answered", out.strip()
        why = err.strip() or f"gh exited {code}"
        if code == 4 or "HTTP 401" in err:
            return "signed_out", why
    return "unasked", why


def push_targets(run: Callable[[list[str]], tuple[int, str, str]]) -> str:
    """Where a session may push, as its prompt says it. A sign-in pushes to a repository of
    REPOSITORIES only where GitHub says it can: UPSTREAM's own when it is UPSTREAM, otherwise its
    fork of it. Without a fork the session's fixes stay in its clone, because pushing is never
    made possible by forking for them (KEEPER.md)."""
    nowhere = ("Commit your fixes on `machine/<login>` in your clone, push nothing, and release "
               "nothing (KEEPER.md § What you may do).")
    kind, login = ask_github(run, ["api", "user", "--jq", ".login"])
    if kind == "signed_out":
        return f"This container has no GitHub sign-in (`gh api user`: {login}). {nowhere}\n\n"
    if kind == "unasked":
        return f"GitHub could not be asked who this container signs in as ({login}). {nowhere}\n\n"
    branch = f"machine/{login.lower()}"
    lines = [f"GitHub signs this container in as `{login}`; your branch is `{branch}`."]
    for repo in REPOSITORIES:
        row = "[.full_name, (.permissions.push | tostring)] | @tsv"
        if login.lower() == UPSTREAM.lower():
            query = ["api", f"repos/{UPSTREAM}/{repo}", "--jq", row]
        else:
            query = ["api", "--paginate", f"repos/{UPSTREAM}/{repo}/forks", "--jq",
                     f'.[] | select(.owner.login == "{login}") | {row}']
        kind, found = ask_github(run, query)
        pushable = [r.split("\t")[0] for r in found.splitlines() if r.endswith("\ttrue")]
        if kind != "answered":
            lines.append(f"- {repo}: whether `{login}` can push to it could not be asked ({found}): "
                         "commit on the branch in your clone and push nothing there.")
        elif pushable:
            lines.append(f"- {repo}: push `{branch}` to `{pushable[0]}`, and release from it.")
        else:
            lines.append(f"- {repo}: `{login}` has no fork of `{UPSTREAM}/{repo}` it can push to: "
                         "commit on the branch in your clone, push nothing and release nothing there.")
    return "\n".join(lines) + "\n\n"


def push_ntfy(topic: str, title: str, message: str) -> None:
    request = urllib.request.Request(NTFY + topic, data=message.encode("utf-8"), method="POST",
                                     headers={"Title": title})
    with urllib.request.urlopen(request, timeout=30) as answer:
        answer.read()


# --- the loop -------------------------------------------------------------------------------

NOTHING_TO_DO = "Nothing to do; the owner will fix it when back."
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
ALERT_SIGNED_OUT = (
    "RaiGolmi helper's sign-in ran out",
    "RaiGolmi's repair helper cannot work: the helper's sign-in ran out. " + NOTHING_TO_DO)
ALERT_UNREACHED = (
    "RaiGolmi helper cannot reach Claude",
    "RaiGolmi's repair helper has not been able to reach Claude for a day, so it cannot fix "
    "what went wrong. " + NOTHING_TO_DO)
ALERT_RECOVERED = ("RaiGolmi is working again", "RaiGolmi is working again. Nothing to do.")


def pace(plan: dict[str, Any] | None) -> str:
    """How widely a session works, as every tab is told (raigolmid's `limits.plan_view`): the
    plan's window furthest on course past its allowance narrows it; none, or no reading, leaves
    it as wide as the repair needs. A repair is never held back for it."""
    over = [] if plan is None else [
        (w["on_course_for"], name, w["resets_at"]) for name, w in plan["windows"].items()
        if w.get("on_course_for") is not None and w["on_course_for"] > 1]
    if not over:
        return ""
    course, name, resets_at = max(over)
    return (f"The plan's {name} window is on course for {course} of its allowance before it "
            f"resets at {iso(resets_at)}, and the run spends the same plan: work narrower — one "
            "thing at a time, the cheapest check that settles it first, no speculative work — "
            "but do the repair.\n\n")


def is_feature(fault: dict[str, Any] | None) -> bool:
    """A record the machine tab filed with `request_feature`; a daemon that files no kind files
    only faults."""
    return fault is not None and fault.get("kind", "fault") == "feature"


def counted(incident: dict[str, Any]) -> int:
    """The incident's sessions that count toward SESSIONS_PER_INCIDENT: each one Claude answered
    without an API error. One kept from before `counted` was recorded counts."""
    return sum(1 for s in incident["sessions"] if s.get("counted", True))


class Keeper:
    """The decision loop. Everything that reaches outside it is handed in, so a test runs it
    against doubles: `clock` (epoch seconds), `monotonic` (the keeper's own clock, for how long
    something stayed the same), `probe` (the VM), `runner` (one Claude Code session: prompt, id,
    log -> read_outcome's dict and `token`), `ready` (refused token -> claude_ready's answer),
    `push` (one ntfy alert), `tell` (`rai keeper <args>` on the VM -> {ok, result or error}), and
    `targets` (push_targets' words for a session's prompt)."""

    def __init__(self, paths: Paths, clock: Callable[[], float], monotonic: Callable[[], float],
                 probe: Callable[[], dict[str, Any]],
                 runner: Callable[[str, str, Path], dict[str, Any]],
                 ready: Callable[[str | None], tuple[str, str]],
                 push: Callable[[str, str, str], None],
                 tell: Callable[[list[str]], dict[str, Any]],
                 targets: Callable[[], str]) -> None:
        self.paths, self.clock, self.monotonic, self.probe = paths, clock, monotonic, probe
        self.runner, self.ready, self.push, self.tell = runner, ready, push, tell
        self.targets = targets
        self.state, self.why = "starting", None
        self.incident: dict[str, Any] | None = None
        self.last_incident: dict[str, Any] | None = None
        self.last_alert: dict[str, Any] | None = None
        # Alert title -> when it last reached ntfy, over all incidents.
        self.alerted: dict[str, float] = {}
        # A machine-down alert went out and "working again" has not followed it.
        self.owed_recovery = False
        # The fingerprint of the token the API refused; no session runs on it again.
        self.refused_token: str | None = None
        # The launcher's transitional state and when the keeper first saw it.
        self.transition: tuple[str, float] | None = None
        # launcher.json's `at` and when (monotonic) the keeper first read it.
        self.launcher_at: tuple[str, float] | None = None
        # (monotonic, raigolmid's NRestarts) of the probes within DAEMON_RESTART_WINDOW.
        self.daemon_restarts: list[tuple[float, int]] = []
        # Answers to faults the VM has not yet taken: fault id -> {verdict, words}.
        self.unanswered: dict[str, dict[str, str]] = {}
        if paths.keeper_json.exists():
            kept = json.loads(paths.keeper_json.read_text(encoding="utf-8"))
            self.incident = kept["incident"]
            self.last_incident = kept["last_incident"]
            self.last_alert = kept["last_alert"]
            self.unanswered = kept["unanswered"]
            # A keeper.json from before these were kept has none of them, which is what it says.
            self.alerted = kept.get("alerted", {})
            self.owed_recovery = kept.get("owed_recovery", False)
            self.refused_token = kept.get("refused_token")
            if self.incident is not None:
                self.incident.setdefault("unreached_since", None)
                self.incident.setdefault("line_out", None)

    # --- seeing ---------------------------------------------------------------------------
    def observe(self, now: float) -> Seen:
        if not self.paths.launcher_json.exists():
            return Seen("gone", "the launcher has never written launcher.json", launcher_down=True)
        launcher = json.loads(self.paths.launcher_json.read_text(encoding="utf-8"))
        state = launcher["state"]
        if state not in LAUNCHER_STATES:
            raise ValueError(f"launcher.json names state {state!r}, not one of {sorted(LAUNCHER_STATES)}")
        evidence = {"launcher": launcher}
        if state == "closed":
            return Seen("closed", launcher.get("why"))
        if state == "gave_up":
            return Seen("failing", f"the launcher gave up after five restarts in a row: {launcher.get('why')}",
                        evidence, act_now=True, launcher_down=True)
        mono = self.monotonic()
        if self.launcher_at is None or self.launcher_at[0] != launcher["at"]:
            self.launcher_at = (launcher["at"], mono)
        unchanged = mono - self.launcher_at[1]
        if unchanged > LAUNCHER_STALE_SECONDS:
            return Seen("gone", f"the launcher has not written launcher.json for {round(unchanged)} s "
                                f"(last as {state}, at {launcher['at']})",
                        {**evidence, "inbox": pending_requests(self.paths)}, launcher_down=True)
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
            self.daemon_restarts = []
            return Seen("failing", f"the VM does not answer ssh ({vm['error']})", evidence)
        if "probe_failed" in vm:
            return Seen("failing", f"the probe on the VM failed ({vm['probe_failed']})", evidence)
        if vm["daemon"] != "active":
            return Seen("failing", f"raigolmid is {vm['daemon'] or 'unknown'} on the VM", evidence)
        restarts = self._daemon_restarted(mono, vm["restarts"])
        if restarts >= DAEMON_RESTARTS:
            return Seen("failing", f"systemd has restarted raigolmid {restarts} times in the last "
                                   f"{DAEMON_RESTART_WINDOW // 60} minutes", evidence)
        if vm["faults_error"] is not None:
            return Seen("failing", f"raigolmid did not answer `rai keeper seen` ({vm['faults_error']})",
                        evidence)
        if vm["held"]:
            # Every agent waits the usage limit out, the keeper too; a fault waiting is told
            # why, so nobody files it again, and is taken up once the limit lifts.
            for fault in vm["faults"]:
                if fault["id"] not in self.unanswered:
                    self._defer(fault)
            return Seen("held", "the account is on its usage limit")
        last = vm["last_event"]
        if vm["run_working"] and last is not None and last["age_s"] > EVENTS_SILENT_SECONDS:
            return Seen("failing", f"a run is in progress and raigolmid has written no event for "
                                   f"{round(last['age_s'] / 60)} minutes (the last: {last['type']})",
                        evidence)
        faults = [f for f in vm["faults"] if not is_feature(f)]
        unanswered = [f for f in faults if f["id"] not in self.unanswered]
        if unanswered:
            fault = unanswered[0]
            return Seen("failing", f"the machine tab reported a fault in RaiGolmi: {fault['title']}",
                        {**evidence, "fault": fault}, act_now=True)
        # A fault whose answer the VM has not taken yet still comes before a feature.
        features = [f for f in vm["faults"] if is_feature(f) and f["id"] not in self.unanswered]
        if features and not faults:
            feature = features[0]
            return Seen("building", f"the owner asked for a feature in RaiGolmi: {feature['title']}",
                        {**evidence, "fault": feature}, act_now=True)
        return Seen("healthy")

    def _daemon_restarted(self, mono: float, restarts: int) -> int:
        """How many times systemd restarted raigolmid within DAEMON_RESTART_WINDOW, from its
        NRestarts at each probe; a count that fell is a guest that booted again."""
        if self.daemon_restarts and restarts < self.daemon_restarts[-1][1]:
            self.daemon_restarts = []
        self.daemon_restarts = [(t, n) for t, n in self.daemon_restarts
                                if mono - t <= DAEMON_RESTART_WINDOW] + [(mono, restarts)]
        return restarts - self.daemon_restarts[0][1]

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
            if seen.kind == "healthy" and self.owed_recovery:
                self._alert(*ALERT_RECOVERED, now)
        elif seen.kind == "waiting":
            self.state = "waiting"
        else:
            self.state = "building" if seen.kind == "building" else "failing"
            self._fail(seen, now)
        self.report(now)

    def _close(self, now: float) -> None:
        if self.incident is None:
            return
        logger.info("the incident since %s is over: %s", iso(self.incident["since"]), self.state)
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
            logger.warning("%s: %s", seen.kind, seen.why)
            self.incident = {"since": now, "reason": seen.why, "evidence": seen.evidence,
                             "act_at": now + FAILING_SECONDS, "start_requested_at": None,
                             "sessions": [], "unreached_since": None, "line_out": None,
                             "fault": fault}
        incident = self.incident
        incident["reason"], incident["evidence"] = seen.why, seen.evidence
        self._read_line_out(incident, seen)
        if seen.kind == "gone" and incident["start_requested_at"] is None:
            self._request_start(incident, seen.why, now)
            incident["act_at"] = now + START_GRACE_SECONDS
            return
        # `act_now` is a failure the launcher has already given up on; after a session even it
        # waits for what that session started to take.
        if now < incident["act_at"] and not (seen.act_now and not incident["sessions"]):
            return
        if counted(incident) >= SESSIONS_PER_INCIDENT:
            # A fault's answer goes with its last session, so only a machine down is here.
            self._cannot_fix(incident, seen, ALERT_NOT_FIXED, "", now)
            return
        kind, why = self.ready(self.refused_token)
        if kind == "signed_out":
            self._cannot_fix(incident, seen, ALERT_SIGNED_OUT, f"the keeper's Claude Code cannot run: {why}", now)
            return
        if kind != "ready":
            title, message = ALERT_CANNOT_RUN
            self._cannot_fix(incident, seen, (title, message.format(why=why)),
                             f"the keeper's Claude Code cannot run: {why}", now)
            return
        self._session(incident, seen, now)

    def _cannot_fix(self, incident: dict[str, Any], seen: Seen, alert: tuple[str, str], words: str,
                    now: float) -> None:
        """No session can run on the incident: a fault is answered so the machine tab resumes its
        tabs; a machine that is down is told to the person, and its launcher asked to start."""
        fault = incident["fault"]
        if fault is not None:
            self._answer(fault["id"], "not_fixed", words, now)
        else:
            self._alert(*alert, now)
            self._retry_start(incident, seen, now)

    def _request_start(self, incident: dict[str, Any], why: str, now: float) -> None:
        if not any(r["request"] == "start" for r in pending_requests(self.paths)):
            write_request(self.paths, "start", why, now)
        incident["start_requested_at"] = now

    def _retry_start(self, incident: dict[str, Any], seen: Seen, now: float) -> None:
        last = incident["start_requested_at"]
        if seen.launcher_down and (last is None or now - last >= START_RETRY_SECONDS):
            self._request_start(incident, seen.why, now)

    def _read_line_out(self, incident: dict[str, Any], seen: Seen) -> None:
        """The cause raigolmid's line out sent during the incident, or within the day before it
        (it sends each at most once a day): its words stand, and the keeper adds none."""
        line_out = (((seen.evidence or {}).get("vm") or {}).get("line_out") or {}).get("sent") or {}
        for cause, at in line_out.items():
            if at >= incident["since"] - LINE_OUT_COVERS_SECONDS and incident["line_out"] is None:
                logger.info("raigolmid's line out sent %s at %s: the keeper sends no alert for "
                            "this incident", cause, iso(at))
                incident["line_out"] = cause

    def _session(self, incident: dict[str, Any], seen: Seen, now: float) -> None:
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
        counts = outcome["unreached"] is None and not outcome["signed_out"]
        incident["sessions"].append({
            "id": session_id, "started": iso(now), "ended": iso(ended), "log": str(log),
            "notes": str(notes), "verdict": outcome["verdict"], "counted": counts,
            "resume": f"cd /work/RaiGolmi && env $(cat {self.paths.credential}) "
                      f"claude --resume {session_id}"})
        incident["act_at"] = ended + FAILING_SECONDS
        logger.info("session %s ended: %s", session_id, outcome["verdict"])
        if outcome["signed_out"]:
            # The next check finds the sign-in refused, and says so.
            self.refused_token = outcome["token"]
            incident["act_at"] = ended
            return
        if outcome["unreached"] is not None:
            if incident["unreached_since"] is None:
                incident["unreached_since"] = now
            if ended - incident["unreached_since"] >= UNREACHED_SECONDS:
                self._cannot_fix(incident, seen, ALERT_UNREACHED,
                                 f"Claude could not be reached for a day ({outcome['unreached']})", ended)
            return
        incident["unreached_since"] = None
        if fault is None:
            return
        fixed = outcome["verdict"] == FIXED
        if fixed or counted(incident) >= SESSIONS_PER_INCIDENT:
            words = (outcome["verdict"].removeprefix(VERDICT).strip()
                     .removeprefix("not fixed:").strip() or outcome["verdict"])
            self._answer(fault["id"], "fixed" if fixed else "not_fixed", "" if fixed else words,
                         ended)

    def _defer(self, fault: dict[str, Any]) -> None:
        """A fault waiting out the account's usage limit stays filed, and the machine tab is
        told so; said once, and again only if the VM did not take it."""
        if fault["status"] == "deferred":
            return
        told = self.tell(["answer", fault["id"], "deferred",
                          "the account is on its usage limit, which holds every agent; the keeper "
                          "takes it up itself the moment the limit lifts."])
        if told["ok"]:
            logger.info("deferred fault %s: the usage limit", fault["id"])
        else:
            logger.warning("fault %s's deferral did not reach the VM; said again next check: %s",
                           fault["id"], told["error"])

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
        earlier = "".join(f"\n- session {s['id']} ({s['started']}): {s['verdict']}; its log is "
                          f"{s['log']}, its notes {s['notes']}" for s in incident["sessions"])
        if is_feature(incident["fault"]):
            feature = incident["fault"]
            return (
                f"The owner asked, through the machine tab, for a feature in RaiGolmi "
                f"({feature['id']}): {feature['title']}\n\n{feature['body']}\n\n"
                "The machine is healthy and no fault waits. Build it as you would a fix: on this "
                "machine's branch, released and upgraded to, without putting the run at risk. "
                "Your verdict line is the machine tab's answer: `fixed` when it runs on this "
                "machine; `not fixed: <words>` says why it was not built.\n\n"
                + pace(((incident["evidence"] or {}).get("vm") or {}).get("plan"))
                + self.targets()
                + (f"Earlier keeper sessions on this feature did not finish it:{earlier}\n"
                   "Read their logs and notes before acting.\n\n" if earlier else "")
                + f"You are session {session_id}. Write what you find and do into {notes}. "
                "End with the verdict line.")
        qemu_tail = ""
        if self.paths.qemu_log.exists():
            qemu_tail = "\n".join(self.paths.qemu_log.read_text(
                encoding="utf-8", errors="replace").splitlines()[-40:])
        return (
            f"The keeper found the machine failing since {iso(incident['since'])}: "
            f"{incident['reason']}.\n\nWhat the check gathered:\n"
            f"{json.dumps(incident['evidence'], indent=2)}\n\n"
            f"The last lines of windows/qemu.log:\n{qemu_tail}\n\n"
            + pace(((incident["evidence"] or {}).get("vm") or {}).get("plan"))
            + self.targets()
            + (f"Earlier keeper sessions on this incident did not fix it:{earlier}\n"
               "Read their logs and notes before acting.\n\n" if earlier else "")
            + (f"The machine tab paused the tabs this fault affects "
               f"({', '.join(incident['fault']['affects']) or 'none named'}) until it is "
               "answered: your verdict line is its answer, and `fixed` resumes them.\n\n"
               if incident["fault"] else "")
            + f"You are session {session_id}. Write what you find and do into {notes}. "
            f"Get the machine running again, then find the root cause. End with the verdict line.")

    def _alert(self, title: str, message: str, now: float) -> None:
        """Each title at most once in REALERT_SECONDS, unless "working again" went out since
        it: then it is news. None while the line out's word stands for the incident."""
        recovery = title == ALERT_RECOVERED[0]
        if not recovery and self.incident is not None and self.incident["line_out"] is not None:
            return
        last = self.alerted.get(title)
        recovered_since = (not recovery and last is not None
                           and self.alerted.get(ALERT_RECOVERED[0], 0) > last)
        if last is not None and now - last < REALERT_SECONDS and not recovered_since:
            return
        topic = ntfy_topic(self.paths)
        if not topic:
            outcome = "no ntfy topic is set in settings.ini"
        else:
            try:
                self.push(topic, title, message)
                outcome = "sent"
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                # Not marked alerted: the next check tries again.
                logger.error("the alert '%s' did not reach ntfy.sh: %s", title, exc)
                self.last_alert = {"at": iso(now), "title": title, "message": message,
                                   "outcome": f"failed: {exc}"}
                return
        logger.warning("alert '%s': %s", title, outcome)
        self.last_alert = {"at": iso(now), "title": title, "message": message, "outcome": outcome}
        self.alerted[title] = now
        self.owed_recovery = not recovery

    def report(self, now: float) -> None:
        write_json(self.paths.keeper_json, {
            "at": iso(now), "state": self.state, "why": self.why, "source": SOURCE,
            "incident": self.incident,
            "last_incident": self.last_incident, "last_alert": self.last_alert,
            "alerted": self.alerted, "owed_recovery": self.owed_recovery,
            "refused_token": self.refused_token,
            "unanswered": self.unanswered})


def loop(paths: Paths) -> int:
    """Checks every CHECK_SECONDS for as long as the container runs. A check that raises is
    logged with its traceback and the next one runs: under `restart: unless-stopped` a raise
    would restart the container into the same error, a crash loop nobody near the machine sees,
    where this keeps every check that can still be made."""
    keeper: Keeper | None = None
    logger.info("keeping %s, every %d s", paths.work, CHECK_SECONDS)
    while True:
        try:
            if keeper is None:
                ensure_key(paths)
                paths.inbox.mkdir(parents=True, exist_ok=True)
                keeper = Keeper(paths, time.time, time.monotonic, lambda: probe_vm(paths),
                                lambda prompt, sid, log: run_claude(paths, prompt, sid, log),
                                lambda refused: claude_ready(paths, refused), push_ntfy,
                                lambda args: rai_keeper(paths, args), lambda: push_targets(gh))
            keeper.check()
        except Exception:
            logger.exception("the check failed; the next is in %d s", CHECK_SECONDS)
        time.sleep(CHECK_SECONDS)


# The loop's log in the outbox, so it is read where the rest of the keeper's state is, without
# `docker logs`. A session reads it whole, so it is kept to what one can: a megabyte and the one
# before it.
LOG_BYTES = 1 << 20
LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format=LOG_FORMAT)
    paths = Paths()
    if not argv:
        # The loop alone writes the file: with one writer, a rollover never renames a log
        # another process holds open.
        paths.outbox.mkdir(parents=True, exist_ok=True)
        to_file = logging.handlers.RotatingFileHandler(paths.log, maxBytes=LOG_BYTES, backupCount=1,
                                                       encoding="utf-8")
        to_file.setFormatter(logging.Formatter(LOG_FORMAT))
        logging.getLogger().addHandler(to_file)
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
