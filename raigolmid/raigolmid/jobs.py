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

A job starts with a prediction and is defended when it runs past it. A job that takes far
longer than its tab expected is a fault until shown otherwise, and a tab waiting on it only
says "still running"; so `start` takes `expect` (seconds) and a `basis` for it, and handed back
are the body's last rows for the same command from its ledger. When `expect` runs out the
watch pauses the job (SIGSTOP to every process it started, so its work so far is kept) and tells the
tab; `wait` and `list` then say OVERRUN, never a status, until `defend` names a cause and the
evidence it is progressing, beside the daemon's own reading of the job (CPU seconds and bytes
printed since the last defence), and gives a new prediction — or `stop` ends it. A later
defence repeating an earlier cause carries `confirmed`. The job and its defence are the tab's
that started it, and no one else's. Three overruns end it: a task whose prediction was wrong
three times is not the task its tab thinks it is, so the third is struck out and the
tab told to find out what it is before starting it again. A turn that ends with a job
undefended is said in the tab's history. Each job's end writes a row
to its body's ledger (`Paths.job_ledger`), a run under a third of its prediction included,
since a prediction far too long hides a fault as well as one too short. The end is kept on the
job as first seen, so a toolbelt recreated later never turns an exit into "gone", and it
withdraws an overrun notice still waiting in the tab's channel (`withdraws`).

A job ends with its tab (`close`): the tab's sandbox is its own and stops with it, so a job never
passes to the tab that takes its work over. One still running is ended, each end not
yet seen is a ledger row saying why, and the successor is told how each ended.

A machine restart ends every job, so one the keeper plans for an upgrade waits on them: the
launcher asks `restarting` until none runs, and each ask holds new starts for `HOLD_SECONDS`,
long enough for the upgrade to reach its shutdown. The cause is kept for the stop record
(`Session.close`), so the history says why the machine restarted.

A job printing while it is waited on is said as `job.progressed`, at most once a
`PROGRESS_SECONDS`, which the stall watch reads as the tab's work moving (`stalls.py`); a
defence is not, so a tab defending a job that does nothing is still taken for spinning. The
jobs are written to `Paths.jobs`, so a daemon restart still knows them.
"""
from __future__ import annotations

import json
import re
import shlex
import signal
import threading
import time
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from .events import Event, EventLog, append_lines, read_lines
from .intent import load_json, save_json
from .launcher.client import LauncherError

if TYPE_CHECKING:
    from .session import Session

# The longest one `wait` holds its call, under the stall watch's `STALL_SECONDS`; a longer
# wait is the tab calling again. How often a waited job is read, and how often the watch
# looks for a job past its prediction. Neither decides an outcome.
WAIT_MOST = 300.0
POLL_SECONDS = 5.0
PROGRESS_SECONDS = 60.0
# How much of the end of what a job printed `wait` and `jobs` hand back.
TAIL_CHARS = 3000
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# The overrun that ends a job, and how many ledger rows `start` gives.
STRIKES = 3
SIMILAR_ROWS = 5
# A reason that says only that the job is going, which names no cause.
NO_CAUSE = re.compile(r"(it'?s |it is )?(still )?(running|building|compiling|working|"
                      r"progressing|in progress|going|busy|not done|not finished)"
                      r"( fine| normally| as expected| along)?[.! ]*", re.IGNORECASE)
SHELLS = frozenset({"sh", "bash", "zsh", "dash"})
# How long one `restarting` holds new jobs: past the upgrade's download, staging and shutdown.
HOLD_SECONDS = 20 * 60.0


class JobError(Exception):
    pass


def command_key(cmd: list[str]) -> list[str]:
    """The first two words of what runs: a shell's `-c` script is read for its own, and
    leading `NAME=value` assignments are skipped, so `bash -lc "cargo build"` is `cargo build`."""
    words = list(cmd)
    if len(words) >= 3 and PurePosixPath(words[0]).name in SHELLS \
            and words[1].startswith("-") and "c" in words[1]:
        try:
            words = shlex.split(words[2])
        except ValueError:
            words = words[2].split()
    while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
        words = words[1:]
    return [PurePosixPath(words[0]).name, *words[1:2]] if words else []


def _cause(reason: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", reason.lower()).split())


class Jobs:
    def __init__(self, session: "Session", events: EventLog) -> None:
        self.session = session
        self.events = events
        self.path = session.paths.jobs
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, dict[str, Any]]] = load_json(self.path, "jobs") or {}
        # The planned restart's cause and when its hold on new jobs ends (`restarting`).
        self._restart: tuple[str, float] | None = None
        # A job started with no prediction cannot be timed, and one with no body has no ledger
        # to end in (both written by an earlier daemon); its tab is told it is forgotten.
        for tab, named in self._jobs.items():
            for name in [n for n, job in named.items() if "expect" not in job or "body" not in job]:
                del named[name]
                self.events.emit("job.forgotten", tab=tab, name=name, deliver={
                    "content": f"Your job {name!r} was started by an earlier daemon and is no "
                               "longer tracked; it still runs in your toolbelt container",
                    "meta": {"from": "daemon"}})

    def _save(self) -> None:
        save_json(self.path, self._jobs)

    # --- the ledger --------------------------------------------------------------------

    def _body(self, tab_id: str) -> str | None:
        tab = self.session.intent.tabs.get(tab_id)
        return tab.body if tab is not None else None

    def similar(self, body: str | None, cmd: list[str]) -> list[dict[str, Any]]:
        if body is None:
            return []
        path = self.session.paths.job_ledger(body)
        if not path.exists():
            return []
        key = command_key(cmd)
        return [r for r in read_lines(path) if command_key(r["cmd"]) == key][-SIMILAR_ROWS:]

    def _end(self, tab_id: str, name: str, job: dict[str, Any], ended: str,
             exit_code: int | None = None, why: str | None = None, tail: str = "",
             printed: int = 0) -> None:
        """Once per job, when its end is first seen: kept on the job as it was then, and a row
        in its body's ledger."""
        with self._lock:
            if "end" in job:
                return
            seconds = round(time.time() - job["started"])
            job["end"] = {"ended": ended, "exit_code": exit_code, "why": why, "tail": tail,
                          "printed": printed, "seconds": seconds}
            self._save()
        row = {"name": name, "cmd": job["cmd"], "expect": job["expect"], "basis": job["basis"],
               "seconds": seconds, "defences": len(job["defences"]), "ended": ended,
               "exit_code": exit_code, "reasons": [d["reason"] for d in job["defences"]],
               "under": not job["defences"] and seconds < job["expect"] / 3,
               "at": round(time.time()), **({} if why is None else {"why": why})}
        if job["body"] is not None:
            path = self.session.paths.job_ledger(job["body"])
            path.parent.mkdir(parents=True, exist_ok=True)
            append_lines(path, [json.dumps(row)])
        self.events.emit("job.ended", tab=tab_id, instance=job["instance"], name=name,
                         ended=ended, exit_code=exit_code, withdraws=f"job {name}")
        if row["under"]:
            self.events.emit("job.under", tab=tab_id, instance=job["instance"], name=name,
                             expect=job["expect"], seconds=seconds)

    # --- starting ----------------------------------------------------------------------

    def start(self, tab_id: str, instance_id: str, name: str, cmd: list[str],
              expect: int, basis: str, cwd: str = "/work") -> dict[str, Any]:
        if not NAME.fullmatch(name):
            raise JobError(f"a job's name is letters, digits, '.', '_' and '-': {name!r}")
        if not cmd:
            raise JobError("a job runs a command")
        if not isinstance(expect, int) or expect <= 0:
            raise JobError("a job starts with `expect`: how many seconds you predict it takes")
        if not basis.strip():
            raise JobError("a job starts with `basis`: one line on why it takes `expect` — "
                           "its ledger rows, or what it runs")
        cause = self.restart_cause()
        if cause is not None:
            raise JobError(f"the machine restarts within minutes ({cause}), which would end "
                           f"{name!r}: start it once the machine is back")
        with self._lock:
            known = self._jobs.get(tab_id, {}).get(name)
        if known is not None and self._state(tab_id, name, known)["state"] in ("running",
                                                                               "OVERRUN"):
            raise JobError(f"job {name!r} is still running; wait on it, or give this one "
                           "another name")
        self.session.await_open(instance_id)
        if self.session.views.get(instance_id) is None:
            raise JobError(f"{instance_id}'s toolbelt container is not running, so there is "
                           f"nowhere to run {cmd[0]!r}")
        similar = self.similar(self._body(tab_id), cmd)
        proc, launcher = self.session.views.client(instance_id).start_job(cmd, cwd=cwd)
        now = time.time()
        job = {"instance": instance_id, "launcher": launcher, "proc": proc, "cmd": cmd,
               "cwd": cwd, "body": self._body(tab_id), "started": now, "expect": expect, "basis": basis,
               "until": now + expect, "overruns": 0, "overrun": None, "defences": [],
               "mark": {"cpu": 0.0, "printed": 0}}
        with self._lock:
            self._jobs.setdefault(tab_id, {})[name] = job
            self._save()
        self.events.emit("job.started", tab=tab_id, instance=instance_id, name=name, cmd=cmd,
                         expect=expect, basis=basis)
        return {"name": name, "state": "running", "expect": expect, "history": similar}

    # --- a planned restart ---------------------------------------------------------------

    def restarting(self, cause: str) -> dict[str, Any]:
        """The keeper's upgrade is about to restart the machine: new jobs are held for
        `HOLD_SECONDS` from now, and the jobs still running, which the restart would end, are
        handed back so the caller waits on them."""
        if not cause.strip():
            raise JobError("a restart is planned with its cause")
        with self._lock:
            first = self.restart_cause() is None
            self._restart = (cause, time.time() + HOLD_SECONDS)
            mine = [(tab, name, job) for tab, named in self._jobs.items()
                    for name, job in named.items()]
        running = []
        for tab_id, name, job in mine:
            state = self._state(tab_id, name, job)["state"]
            if state in ("running", "OVERRUN"):
                running.append({"tab": tab_id, "name": name, "state": state,
                                "seconds": self._seconds(job), "expect": job["expect"]})
        if first:
            self.events.emit("machine.restarting", cause=cause,
                             running=[f"{j['tab']} {j['name']}" for j in running])
        return {"running": running, "held_until": self._restart[1]}

    def restart_cause(self) -> str | None:
        """The cause of a planned restart whose hold has not run out."""
        with self._lock:
            if self._restart is None or self._restart[1] < time.time():
                return None
            return self._restart[0]

    def _job(self, tab_id: str, name: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(tab_id, {}).get(name)
        if job is None:
            raise JobError(f"no job {name!r}; `jobs` lists yours")
        return job

    # --- what a job is doing -----------------------------------------------------------

    def _known(self, job: dict[str, Any]) -> dict[str, Any] | str:
        """The launcher's record of the job, or why there is none."""
        instance = job["instance"]
        if self.session.views.get(instance) is None:
            return (f"{instance}'s toolbelt container is not running: the job ended with it, "
                    "and left no exit code")
        try:
            known = self.session.views.client(instance).process(job["launcher"], job["proc"])
        except LauncherError as exc:
            raise JobError(f"the toolbelt's launcher could not be asked about the job: {exc}"
                           ) from exc
        if known is None:
            return (f"{instance}'s toolbelt container was recreated since it started: the job "
                    "ended with the old one, and left no exit code")
        return known

    def _tail(self, job: dict[str, Any]) -> str:
        try:
            printed = self.session.views.client(job["instance"]).scrollback(job["proc"])
        except LauncherError as exc:
            raise JobError(f"the toolbelt's launcher could not give what the job printed: "
                           f"{exc}") from exc
        # Read back from a terminal, whose line ends are \r\n.
        return printed.replace("\r\n", "\n")[-TAIL_CHARS:]

    def _state(self, tab_id: str, name: str, job: dict[str, Any],
               tail: bool = False) -> dict[str, Any]:
        """running, OVERRUN (paused past its prediction, undefended), exited (with its code)
        or gone (no launcher knows it any more); an ended job as its end was first seen."""
        if "end" not in job:
            known = self._known(job)
            if isinstance(known, str):
                self._end(tab_id, name, job, "gone", why=known)
            elif known["exit"] is not None:
                self._end(tab_id, name, job, "struck out" if job.get("struck")
                          else "stopped" if job.get("stopped") else "exited",
                          known["exit"], tail=self._tail(job), printed=known["printed"])
            else:
                out = ({"state": "OVERRUN", **self._overrun_said(job)}
                       if job["overrun"] is not None else {"state": "running"})
                out["printed"] = known["printed"]
                if tail:
                    out["tail"] = self._tail(job)
                return out
        end = job["end"]
        if end["ended"] == "gone":
            return {"state": "gone", "why": end["why"]}
        out = {"state": "exited", "exit_code": end["exit_code"], "printed": end["printed"]}
        if job.get("stopped"):
            out["stopped"] = job["stopped"]
        if tail:
            out["tail"] = end["tail"]
        return out

    @staticmethod
    def _seconds(job: dict[str, Any]) -> int:
        """How long it ran: to its end once that is seen."""
        if "end" in job:
            return job["end"]["seconds"]
        return round(time.time() - job["started"])

    def _overrun_said(self, job: dict[str, Any]) -> dict[str, Any]:
        overrun = job["overrun"]
        said = {"overrun": overrun["count"], "predicted": overrun["predicted"],
                "daemon_reading": overrun["reading"],
                "to_do": ("paused: say why it is taking longer with `job_defend` (a cause, "
                          "evidence it is progressing, and a new prediction), or end it with "
                          "`job_stop`")}
        if overrun["count"] == STRIKES - 1:
            said["to_do"] += (f"; a prediction wrong {STRIKES} times ends it, so check what it "
                              "is really doing before you defend it again")
        return said

    # --- the timer ---------------------------------------------------------------------

    def _due(self, tab_id: str, name: str, job: dict[str, Any]) -> None:
        """Pauses the job if its prediction has run out and it is still running, and ends it
        at its third."""
        with self._lock:
            if job["overrun"] is not None or "end" in job or job.get("stopped") \
                    or time.time() < job["until"]:
                return
            known = self._known(job)
            if isinstance(known, str) or known["exit"] is not None:
                return
            struck = job["overruns"] + 1 >= STRIKES
            client = self.session.views.client(job["instance"])
            try:
                if struck:
                    client.end(job["proc"])
                else:
                    client.signal(job["proc"], signal.SIGSTOP, tree=True)
            except LauncherError as exc:
                known = self._known(job)
                if isinstance(known, str) or known["exit"] is not None:
                    return                  # it ended between the read and the signal
                raise JobError(f"job {name!r} ran past its prediction and could not be "
                               f"{'ended' if struck else 'paused'}: {exc}") from exc
            mark = job["mark"]
            cpu = known["cpu"]
            reading = {"cpu_seconds": (None if cpu is None
                                       else round(cpu - mark["cpu"], 1)),
                       "bytes_printed": known["printed"] - mark["printed"]}
            job["mark"] = {"cpu": cpu if cpu is not None else mark["cpu"],
                           "printed": known["printed"]}
            job["overruns"] += 1
            predicted = (job["defences"][-1]["expect_more"] if job["defences"]
                         else job["expect"])
            if struck:
                job["stopped"] = (f"struck out: its prediction was wrong {STRIKES} times, so "
                                  "it is not the task you think it is")
                job["struck"] = True
            else:
                job["overrun"] = {"count": job["overruns"], "at": time.time(),
                                  "predicted": predicted, "reading": reading}
            self._save()
        seconds = round(time.time() - job["started"])
        message = (f"Your job {name!r} ({' '.join(job['cmd'])}) has run {seconds} s against "
                   f"a prediction of {predicted} s. Since the last check it used "
                   f"{reading['cpu_seconds']} CPU seconds and printed "
                   f"{reading['bytes_printed']} bytes. ")
        if struck:
            message += (f"Its prediction was wrong {STRIKES} times, so it is ended: it is not "
                        "the task you think it is. Find out what it actually does — its tail, "
                        "the defences you gave — before you start it again.")
        else:
            message += "It is paused, its work kept: " + self._overrun_said(job)["to_do"] + "."
        self.events.emit("job.overrun", tab=tab_id, instance=job["instance"], name=name,
                         count=job["overruns"], seconds=seconds, predicted=predicted,
                         reading=reading, struck=struck,
                         deliver={"content": message,
                                  "meta": {"from": "daemon", "standing": f"job {name}"}})

    def tick(self) -> None:
        # A tab gone from the intent with its jobs still here closed while the daemon was
        # down, between `Session._close` and `close`.
        with self._lock:
            unclosed = self._jobs.keys() - set(list(self.session.intent.tabs))
        for tab_id in unclosed:
            try:
                self.close(tab_id, None)
            except Exception as exc:                       # noqa: BLE001
                # Said rather than raised: this thread ending would end the daemon.
                self.events.emit("job.watch_failed", tab=tab_id,
                                 error=f"{type(exc).__name__}: {exc}")
        with self._lock:
            mine = [(tab, name, job) for tab, named in self._jobs.items()
                    for name, job in named.items()]
        for tab_id, name, job in mine:
            try:
                self._due(tab_id, name, job)
            except Exception as exc:                       # noqa: BLE001
                # Said rather than raised: this thread ending would end the daemon.
                self.events.emit("job.pause_failed", tab=tab_id, name=name,
                                 error=f"{type(exc).__name__}: {exc}")

    def close(self, tab_id: str, successor: str | None) -> None:
        """The tab closes, or hands its work on to `successor`: its jobs end with it. One still
        running is ended, since it is the tab's alone and its sandbox stops with the tab; each
        end not yet seen is recorded with why, and said to the successor."""
        ended = "handed on" if successor is not None else "closed"
        why = (f"tab {tab_id} handed its work on to {successor}, and a job ends with the tab "
               "that started it" if successor is not None
               else f"tab {tab_id} closed, and a job ends with the tab that started it")
        said = []
        with self._lock:
            mine = self._jobs.pop(tab_id, {})
            for name, job in mine.items():
                if "end" not in job:
                    said.append({"name": name, "cmd": job["cmd"],
                                 "how": self._end_with_tab(tab_id, name, job, ended, why)})
            self._save()
        if not said:
            return
        told = {}
        if successor is not None:
            lines = "\n".join(f"- {j['name']!r} ({' '.join(j['cmd'])}): {j['how']}"
                              for j in said)
            told = {"deliver": {
                "content": f"Your predecessor {tab_id}'s jobs ended when it handed its work on "
                           f"to you, since a job is the tab's that started it and its sandbox "
                           f"stopped with it:\n{lines}\nEach is a row in the body's job "
                           "ledger, handed back when you start the same command.",
                "meta": {"from": "daemon"}}}
        self.events.emit("job.ended_with_tab", tab=successor or tab_id, closed=tab_id,
                         continued_by=successor, jobs=said, **told)

    def _end_with_tab(self, tab_id: str, name: str, job: dict[str, Any], ended: str,
                      why: str) -> str:
        """Under `_lock`: the job's end recorded as its tab closes, and how it ended, said."""
        try:
            known = self._known(job)
            if isinstance(known, str):
                self._end(tab_id, name, job, "gone", why=known)
                return f"gone before the hand-on: {known}"
            if known["exit"] is not None:
                self._state(tab_id, name, job)
                return f"exited with code {known['exit']} before the hand-on"
            self.session.views.client(job["instance"]).end(job["proc"])
        except (JobError, LauncherError) as exc:
            self._end(tab_id, name, job, ended,
                      why=f"{why}; it could not be ended ({exc}), so it ends as its sandbox "
                          "stops")
            return f"could not be ended ({exc}); it ends as the sandbox stops"
        self._end(tab_id, name, job, ended, why=why)
        return (f"ended after {self._seconds(job)} s of a predicted "
                f"{job['expect']} s")

    def on_event(self, event: Event) -> None:
        """A turn that ends with a job undefended."""
        if event.type != "agent.idle" or event.tab is None:
            return
        with self._lock:
            mine = dict(self._jobs.get(event.tab, {}))
        for name, job in mine.items():
            if job["overrun"] is not None:
                self.events.emit("job.undefended", tab=event.tab, instance=job["instance"],
                                 name=name, count=job["overrun"]["count"])

    def run(self, stop: threading.Event) -> None:
        sub = self.events.subscribe()
        try:
            next_tick = 0.0
            while not stop.is_set():
                for event in sub.drain(timeout=1.0):
                    try:
                        self.on_event(event)
                    except Exception as exc:               # noqa: BLE001
                        # Said rather than raised: this thread ending would end the daemon.
                        self.events.emit("job.watch_failed", tab=event.tab,
                                         error=f"{type(exc).__name__}: {exc}")
                if time.time() >= next_tick:
                    next_tick = time.time() + POLL_SECONDS
                    self.tick()
        finally:
            self.events.unsubscribe(sub)

    # --- the tab's verbs ---------------------------------------------------------------

    def defend(self, tab_id: str, name: str, reason: str, observed: str,
               expect_more: int) -> dict[str, Any]:
        job = self._job(tab_id, name)
        with self._lock:
            overrun = job["overrun"]
            if overrun is None:
                raise JobError(f"job {name!r} has not run past its prediction; nothing to "
                               "defend")
            if not reason.strip() or NO_CAUSE.fullmatch(reason.strip()):
                raise JobError("a defence names why it is taking longer than predicted — a "
                               "cause, not that it is still running")
            if not observed.strip():
                raise JobError("a defence carries `observed`: evidence it is progressing")
            if not isinstance(expect_more, int) or expect_more <= 0:
                raise JobError("a defence predicts again: `expect_more` seconds")
            confirmed = "confirmed" in reason.lower()
            if not confirmed and any(_cause(d["reason"]) == _cause(reason)
                                     for d in job["defences"]):
                raise JobError("that cause was given before and the job ran past it again: "
                               "name a different cause, or say `confirmed` with evidence the "
                               "first is real")
            defence = {"reason": reason, "observed": observed, "expect_more": expect_more,
                       "at": time.time(), "reading": overrun["reading"]}
            client = self.session.views.client(job["instance"])
            try:
                client.signal(job["proc"], signal.SIGCONT, tree=True)
            except LauncherError as exc:
                raise JobError(f"job {name!r} could not be continued: {exc}") from exc
            job["defences"].append(defence)
            job["overrun"] = None
            job["until"] = time.time() + expect_more
            self._save()
        self.events.emit("job.defended", tab=tab_id, instance=job["instance"], name=name,
                         reason=reason, observed=observed, expect_more=expect_more,
                         reading=defence["reading"], withdraws=f"job {name}")
        return {"name": name, "state": "running", "timer_until": round(job["until"]),
                "daemon_reading": defence["reading"]}

    def stop(self, tab_id: str, name: str, reason: str) -> dict[str, Any]:
        if not reason.strip():
            raise JobError("a job is stopped with a reason")
        job = self._job(tab_id, name)
        with self._lock:
            client = self.session.views.client(job["instance"])
            try:
                # Answered once nothing the job started runs, children that left its
                # session included.
                client.end(job["proc"])
            except LauncherError as exc:
                raise JobError(f"job {name!r} could not be stopped: {exc}") from exc
            job["stopped"], job["overrun"] = reason, None
            self._save()
        self.events.emit("job.stopped", tab=tab_id, instance=job["instance"], name=name,
                         reason=reason, withdraws=f"job {name}")
        return {"name": name, "state": "stopped"}

    def wait(self, tab_id: str, name: str, timeout: float = WAIT_MOST) -> dict[str, Any]:
        """Until the job exits, is gone or runs past its prediction, or `timeout` (at most
        `WAIT_MOST`) passes."""
        job = self._job(tab_id, name)
        deadline = time.monotonic() + min(max(timeout, 0.0), WAIT_MOST)
        said, printed = 0.0, None
        while True:
            self._due(tab_id, name, job)
            state = self._state(tab_id, name, job, tail=True)
            if state["state"] != "running" or time.monotonic() >= deadline:
                break
            if printed is not None and state["printed"] != printed \
                    and time.monotonic() - said >= PROGRESS_SECONDS:
                said = time.monotonic()
                self.events.emit("job.progressed", tab=tab_id, name=name)
            printed = state["printed"]
            time.sleep(min(POLL_SECONDS, max(deadline - time.monotonic(), 0.0)))
        state.pop("printed", None)
        return {"name": name, "seconds": self._seconds(job), **state}

    def list(self, tab_id: str) -> list[dict[str, Any]]:
        with self._lock:
            mine = dict(self._jobs.get(tab_id, {}))
        out = []
        for name, job in mine.items():
            self._due(tab_id, name, job)
            state = self._state(tab_id, name, job)
            state.pop("printed", None)
            out.append({"name": name, "cmd": job["cmd"], "cwd": job["cwd"],
                        "seconds": self._seconds(job),
                        "expect": job["expect"], "defences": len(job["defences"]), **state})
        return out

    def recorded(self, tab_id: str) -> list[dict[str, Any]]:
        """The tab's jobs as recorded, without asking a launcher: a defence repeating its cause
        with nothing moving in its reading is a tab waiting on nothing."""
        with self._lock:
            mine = dict(self._jobs.get(tab_id, {}))
        return [{"name": name, "cmd": job["cmd"], "expect": job["expect"],
                 "basis": job["basis"], "overruns": job["overruns"],
                 "paused": job["overrun"] is not None, "defences": job["defences"]}
                for name, job in mine.items()]

    def exec(self, tab_id: str, instance_id: str, cmd: list[str], cwd: str, expect: int,
             basis: str) -> dict[str, Any]:
        """An `exec` given a prediction: run as a job, since a call that blocks cannot be
        defended while it blocks, and waited on as `job_wait` would be."""
        name = f"exec-{int(time.time() * 1000)}"
        started = self.start(tab_id, instance_id, name, cmd, expect, basis, cwd)
        return {**self.wait(tab_id, name), "history": started["history"]}
