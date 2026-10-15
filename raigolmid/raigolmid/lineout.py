"""The phone line out of last resort: an ntfy push when nothing on the machine can go on.

Whoever reads it knows nothing about computers, so it says in plain words what is wrong and the
one physical thing to do, or that there is nothing to do. It is sent only for what neither an
agent nor the daemon can put right, each cause at most once a day, and only while `TOPIC_FILE`
names a topic. The topic is set on Windows (`ntfy-topic` in the launcher's settings.ini) and
handed in at boot as the credential `raigolmi.ntfy_topic`, which `systemd-tmpfiles-setup` writes
to `TOPIC_FILE` (host/tmpfiles/raigolmi.conf): a user manager does not see the system's
credentials, so the daemon reads the file. No credential, no file, and the line out is off.
The causes, each where the daemon already knows it:

- **no credential works**: a turn refused on the agent credential (`agent.turn_failed` with
  `claude_login.TURN_REFUSED`) while there is no claude.ai sign-in (`claude_login.lost` removed
  it, or there never was one) — no tab can work at all;
- **the disk is full**: little free (`disk.short`), the collection run for it
  (`garbage.collected`, which `garbage.py` runs on `disk.short`), and still little free;
- **the API refuses every tab for a day**: turns end on an API error continuing will not fix
  (`agent.turn_failed`), the last within `REFUSED_RECENTLY`, and no turn of any tab has ended
  without one, for `REFUSED_FOR` of time this daemon saw while the account was not held at its
  usage limit (`account.limited` to `account.resumed`): a held account is waiting, not refused,
  and time the daemon was down, or the VM suspended, is time nothing was tried;
- **the claude.ai sign-in is ending**: its refresh token does not slide with renewals, so it
  ends on the date it was issued with (`refreshTokenExpiresAt`), and only the owner signing in
  again on the machine brings a new one (`claude_login.py`). Within `LOGIN_LEAD` of that end it
  is said once per end; the run goes on past it on the agent credential (`remotecontrol.py`),
  but Remote Control and answering from the phone stop.

Windows being down, or this VM, cannot be said from inside it. When each cause last went out,
how long the API has refused and when it last did, and which sign-in end was said, are kept in
`Paths.line_out`, so a restart neither resends nor forgets.
"""
from __future__ import annotations

import http.client
import logging
import os
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

from . import claude_login
from .disk import SHORT_FRACTION
from .events import Event, EventLog
from .intent import load_json, save_json

if TYPE_CHECKING:
    from .paths import Paths

logger = logging.getLogger(__name__)

NTFY = "https://ntfy.sh"
TOPIC_FILE = Path("/run/raigolmi/ntfy-topic")
# ntfy's rule for a topic name; the launcher refuses any other (windows/Settings.cs).
TOPIC = re.compile(r"[A-Za-z0-9_-]{1,64}")
DAY = 24 * 3600.0
# How long every turn has been refused before it is the line out's; a day, the owner's word.
REFUSED_FOR = DAY
# A refusal older than this is not the API refusing now: a stalled run's machine tab is woken at
# least every four hours (`coordinator.IDLE_CEILING_SECONDS`), so a refusing API is met more
# often than this while anything is being tried.
REFUSED_RECENTLY = 6 * 3600.0
# A gap between ticks longer than this is time the daemon did not see (stopped, or the VM
# suspended); a tick is a second apart, a push that hangs at most `SEND_TIMEOUT`.
SEEN_GAP = 120.0
# How much refused time a restart may forget: the record is written this often while it grows.
SAVE_SECONDS = 600.0
# How long before the sign-in ends the owner is told: room for a weekend away and a day to get
# to the machine, which is the only place a new sign-in can be given.
LOGIN_LEAD = 3 * DAY
# A push that did not go is tried again this often while its cause holds; it sets how soon,
# never whether.
RETRY_SECONDS = 600.0
SEND_TIMEOUT = 30.0

NOTHING_TO_DO = "Nothing to do for you; the owner will fix it."
# cause -> (title, message). Titles stay ASCII: they travel as an HTTP header.
MESSAGES = {
    "no_credential": (
        "RaiGolmi has stopped: it cannot sign in",
        "The AI on the home computer has lost its sign-in to Claude, so it cannot do any work. "
        + NOTHING_TO_DO),
    "disk_full": (
        "RaiGolmi has stopped: its storage is full",
        "The home computer's AI has run out of storage space and cleaning up did not free "
        "enough, so its work has stopped. " + NOTHING_TO_DO),
    "api_refused": (
        "RaiGolmi has stopped: Claude refuses it",
        "Claude's service has refused the home computer's AI for a whole day, so no work is "
        "getting done. " + NOTHING_TO_DO),
    "login_ending": (
        "RaiGolmi needs the owner to sign in to Claude again",
        "The home computer's AI will lose its sign-in to Claude on {ends}. Before then the owner "
        "must sign in to Claude again on the home computer itself. If not, its work goes on, but "
        "it can no longer be watched or answered from the phone. " + NOTHING_TO_DO),
}


def topic() -> str | None:
    """The topic handed in at boot, or None when none was. Raises `ValueError` on a file that
    is not one topic name, which only a hand edit of /run can make."""
    try:
        text = TOPIC_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    name = text.strip()
    if not TOPIC.fullmatch(name):
        raise ValueError(f"{TOPIC_FILE} holds {name!r}, which is no ntfy topic: letters, "
                         f"digits, _ and -, at most 64")
    return name


def send(topic: str, title: str, message: str) -> None:
    """One push to `topic` (https://docs.ntfy.sh/publish/: the body is the message, `Title`
    and `Priority` are headers). Raises `OSError` or `HTTPException` when it did not go;
    urllib raises any answer but a 2xx."""
    request = urllib.request.Request(
        f"{NTFY}/{topic}", data=message.encode("utf-8"), method="POST",
        headers={"Title": title, "Priority": "high", "Tags": "warning"})
    with urllib.request.urlopen(request, timeout=SEND_TIMEOUT) as answer:
        answer.read()


class LineOut:
    """Subscribed at construction, so a cause between the daemon's start and `run` is heard."""

    def __init__(self, paths: "Paths", events: EventLog) -> None:
        self.paths = paths
        self.events = events
        self._sub = events.subscribe()
        kept = load_json(paths.line_out, "the line out's record") or {}
        self._sent: dict[str, float] = dict(kept.get("sent", {}))
        # Seconds of seen, unheld time every turn has been refused, and when one last was.
        self._refused_for: float | None = kept.get("refused_for")
        self._refused_at: float = kept.get("refused_at", 0.0)
        self._saved_for = self._refused_for or 0.0
        # The sign-in end (`refreshTokenExpiresAt`, seconds) already said.
        self._login_said: float | None = kept.get("login_said")
        self._limited = False
        self._ticked: float | None = None
        self._short = False
        # Causes whose push did not go: cause -> when it is tried again.
        self._pending: dict[str, float] = {}

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            for event in self._sub.drain(timeout=1.0):
                self.on_event(event)
            self.tick(time.time())

    def on_event(self, event: Event) -> None:
        if event.type == "agent.idle" and event.data.get("error") is None:
            if self._refused_for is not None:
                self._refused_for = None
                self._save()
        elif event.type == "agent.turn_failed":
            self._refused_at = max(self._refused_at, event.ts)
            if self._refused_for is None:
                self._refused_for = 0.0
            self._save()
            if (event.data.get("error") == claude_login.TURN_REFUSED
                    and not claude_login.is_set(self.paths.claude_login)):
                self.raise_cause("no_credential", event.ts)
        elif event.type == "account.limited":
            self._limited = True
        elif event.type == "account.resumed":
            self._limited = False
        elif event.type == "disk.short":
            self._short = True
        elif event.type == "garbage.collected" and self._short:
            self._short = False
            if self._holds("disk_full", event.ts):
                self.raise_cause("disk_full", event.ts)

    def tick(self, now: float) -> None:
        seen = None if self._ticked is None else now - self._ticked
        self._ticked = now
        if (self._refused_for is not None and seen is not None and 0 < seen <= SEEN_GAP
                and not self._limited and now - self._refused_at <= REFUSED_RECENTLY):
            self._refused_for += seen
            if self._refused_for >= self._saved_for + SAVE_SECONDS:
                self._save()
        for cause in ("api_refused", "login_ending"):
            if cause not in self._pending and self._holds(cause, now):
                self.raise_cause(cause, now)
        for cause, at in list(self._pending.items()):
            if not self._holds(cause, now):
                del self._pending[cause]
            elif now >= at:
                self.raise_cause(cause, now)

    def _holds(self, cause: str, now: float) -> bool:
        if cause == "no_credential":
            return not claude_login.is_set(self.paths.claude_login)
        if cause == "disk_full":
            fs = os.statvfs(self.paths.data)
            return fs.f_bavail < SHORT_FRACTION * fs.f_blocks
        if cause == "login_ending":
            ends = self._login_ends()
            return (ends is not None and ends != self._login_said
                    and ends - LOGIN_LEAD <= now < ends)
        return (self._refused_for is not None and self._refused_for >= REFUSED_FOR
                and not self._limited and now - self._refused_at <= REFUSED_RECENTLY)

    def _login_ends(self) -> float | None:
        """When the sign-in's refresh token ends, in seconds; None with no sign-in, or one
        whose token endpoint named no end. Read each time: a new sign-in given while one is
        set is said by no event (`daemon.py` says only one given where there was none)."""
        try:
            ends = claude_login.read(self.paths.claude_login)["claudeAiOauth"].get(
                "refreshTokenExpiresAt")
        except claude_login.LoginError:
            return None
        return None if ends is None else ends / 1000

    def raise_cause(self, cause: str, now: float) -> None:
        """Sent unless it went in the last day or no topic is set. A push that does not go is
        said (`lineout.failed`) and tried again while its cause holds, never raised: the line
        out exists for when the machine is already stuck, and must not stop it further."""
        if now - self._sent.get(cause, 0.0) < DAY:
            self._pending.pop(cause, None)
            return
        title, message = MESSAGES[cause]
        ends = None
        if cause == "login_ending":
            ends = self._login_ends()
            if ends is None:            # signed out since it was found
                self._pending.pop(cause, None)
                return
            message = message.format(ends=time.strftime("%A %d %B", time.localtime(ends)))
        try:
            name = topic()
            if name is None:
                self._pending.pop(cause, None)
                return
            send(name, title, message)
        except (OSError, ValueError, http.client.HTTPException) as exc:
            self._pending[cause] = now + RETRY_SECONDS
            self.events.emit("lineout.failed", cause=cause, error=str(exc))
            logger.warning("the line out could not send %s: %s", cause, exc)
            return
        self._pending.pop(cause, None)
        self._sent[cause] = now
        if ends is not None:
            self._login_said = ends
        self._save()
        self.events.emit("lineout.sent", cause=cause, title=title)

    def _save(self) -> None:
        self._saved_for = self._refused_for or 0.0
        save_json(self.paths.line_out, {"sent": self._sent, "refused_for": self._refused_for,
                                        "refused_at": self._refused_at,
                                        "login_said": self._login_said})
