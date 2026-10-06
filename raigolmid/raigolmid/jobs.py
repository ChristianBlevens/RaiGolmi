"""A tab's long jobs: a command started under a name in its sandbox's toolbelt, outliving the
call that started it, and waited on with a deadline that also ends when the job does.

`exec` stops everything a call started when the call ends, so a job meant to outlive it had to
be detached by hand, and a wait on it written by hand; each was a trap a long run kept falling
into — a job killed with its call, a wait with no deadline, a poll that matched itself, a wait
on a line a crashed run never printed. A job is the launcher's own child, started like a
language server (`LauncherClient.start_job`), so it outlives the call, the agent and the
daemon; its exit code and the tail of what it printed are the launcher's to say
(`LauncherClient.process`, `scrollback`). `wait` ends on the first of: the job exiting, the job
gone — the toolbelt container recreated or the machine restarted, which leave no exit code to
read and are said as such — or its deadline, which is kept under the stall watch's so a tab
waiting on a live job is never taken for a hung one.

A job printing while it is waited on is said as `job.progressed`, at most once a
`PROGRESS_SECONDS`, which the stall watch reads as the tab's work moving (`stalls.py`). The
jobs are written to `Paths.jobs`, so a daemon restart still knows them.
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import TYPE_CHECKING, Any

from .events import EventLog
from .launcher.client import LauncherError

if TYPE_CHECKING:
    from .session import Session

# The longest one `wait` holds its call, under the stall watch's `STALL_SECONDS`; a longer
# wait is the tab calling again. How often a waited job is read. Neither decides an outcome.
WAIT_MOST = 300.0
POLL_SECONDS = 5.0
PROGRESS_SECONDS = 60.0
# How much of the end of what a job printed `wait` and `jobs` hand back.
TAIL_CHARS = 3000
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class JobError(Exception):
    pass


class Jobs:
    def __init__(self, session: "Session", events: EventLog) -> None:
        self.session = session
        self.events = events
        self.path = session.paths.jobs
        self._lock = threading.Lock()
        try:
            self._jobs: dict[str, dict[str, dict[str, Any]]] = json.loads(self.path.read_text())
        except FileNotFoundError:
            self._jobs = {}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._jobs))
        tmp.replace(self.path)

    def start(self, tab_id: str, instance_id: str, name: str, cmd: list[str],
              cwd: str = "/work") -> dict[str, Any]:
        if not NAME.fullmatch(name):
            raise JobError(f"a job's name is letters, digits, '.', '_' and '-': {name!r}")
        if not cmd:
            raise JobError("a job runs a command")
        with self._lock:
            known = self._jobs.get(tab_id, {}).get(name)
        if known is not None and self._state(known)["state"] == "running":
            raise JobError(f"job {name!r} is still running; wait on it, or give this one "
                           "another name")
        if self.session.views.get(instance_id) is None:
            raise JobError(f"{instance_id}'s toolbelt container is not running, so there is "
                           f"nowhere to run {cmd[0]!r}")
        proc, launcher = self.session.views.client(instance_id).start_job(cmd, cwd=cwd)
        job = {"instance": instance_id, "launcher": launcher, "proc": proc, "cmd": cmd,
               "cwd": cwd, "started": time.time()}
        with self._lock:
            # A closed tab's jobs are no one's to wait on.
            for gone in self._jobs.keys() - self.session.intent.tabs.keys():
                del self._jobs[gone]
            self._jobs.setdefault(tab_id, {})[name] = job
            self._save()
        self.events.emit("job.started", tab=tab_id, instance=instance_id, name=name, cmd=cmd)
        return {"name": name, "state": "running"}

    def _job(self, tab_id: str, name: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(tab_id, {}).get(name)
        if job is None:
            raise JobError(f"no job {name!r}; `jobs` lists yours")
        return job

    def _state(self, job: dict[str, Any], tail: bool = False) -> dict[str, Any]:
        """running, exited (with its code) or gone (no launcher knows it any more)."""
        instance = job["instance"]
        if self.session.views.get(instance) is None:
            return {"state": "gone", "why": f"{instance}'s toolbelt container is not running: "
                                            "the job ended with it, and left no exit code"}
        client = self.session.views.client(instance)
        try:
            known = client.process(job["launcher"], job["proc"])
        except LauncherError as exc:
            raise JobError(f"the toolbelt's launcher could not be asked about the job: {exc}"
                           ) from exc
        if known is None:
            return {"state": "gone", "why": f"{instance}'s toolbelt container was recreated "
                                            "since it started: the job ended with the old one, "
                                            "and left no exit code"}
        out = ({"state": "running"} if known["exit"] is None
               else {"state": "exited", "exit_code": known["exit"]})
        if tail:
            # Read back from a terminal, whose line ends are \r\n.
            printed = client.scrollback(job["proc"]).replace("\r\n", "\n")
            out["printed"] = len(printed)
            out["tail"] = printed[-TAIL_CHARS:]
        return out

    def wait(self, tab_id: str, name: str, timeout: float = WAIT_MOST) -> dict[str, Any]:
        """Until the job exits or is gone, or `timeout` (at most `WAIT_MOST`) passes."""
        job = self._job(tab_id, name)
        deadline = time.monotonic() + min(max(timeout, 0.0), WAIT_MOST)
        said, printed = 0.0, None
        while True:
            state = self._state(job, tail=True)
            if state["state"] != "running" or time.monotonic() >= deadline:
                break
            if printed is not None and state["printed"] != printed \
                    and time.monotonic() - said >= PROGRESS_SECONDS:
                said = time.monotonic()
                self.events.emit("job.progressed", tab=tab_id, name=name)
            printed = state["printed"]
            time.sleep(min(POLL_SECONDS, max(deadline - time.monotonic(), 0.0)))
        if state["state"] != "running":
            self.events.emit("job.ended", tab=tab_id, name=name, state=state["state"],
                             exit_code=state.get("exit_code"))
        state.pop("printed", None)
        return {"name": name, "seconds": round(time.time() - job["started"]), **state}

    def list(self, tab_id: str) -> list[dict[str, Any]]:
        with self._lock:
            mine = dict(self._jobs.get(tab_id, {}))
        return [{"name": name, "cmd": job["cmd"], "cwd": job["cwd"],
                 "seconds": round(time.time() - job["started"]), **self._state(job)}
                for name, job in mine.items()]
