"""The phone line out of last resort: an ntfy push when nothing on the machine can go on.

Whoever reads it knows nothing about computers, so it says in plain words what is wrong and the
one physical thing to do, or that there is nothing to do. It is sent only for what neither an
agent nor the daemon can put right, each cause at most once a day, and only while
`[notify] ntfy_topic` names a topic (`settings.toml`). The causes, each where the daemon already
knows it:

- **no credential works**: a turn refused on the agent credential (`agent.turn_failed` with
  `claude_login.TURN_REFUSED`) while there is no claude.ai sign-in (`claude_login.lost` removed
  it, or there never was one) — no tab can work at all;
- **the disk is full**: little free (`disk.short`), the collection run for it
  (`garbage.collected`, which `garbage.py` runs on `disk.short`), and still little free;
- **the API refuses every tab for a day**: a turn ended on an API error continuing will not fix
  (`agent.turn_failed`), and no turn of any tab has ended without one since, for `REFUSED_FOR`.

Windows being down, or this VM, cannot be said from inside it. When each cause last went out,
and since when the API has refused, are kept in `Paths.line_out`, so a restart neither resends
nor forgets.
"""
from __future__ import annotations

import http.client
import logging
import os
import threading
import time
import urllib.request
from typing import TYPE_CHECKING

from . import claude_login, settings
from .disk import SHORT_FRACTION
from .events import Event, EventLog
from .intent import load_json, save_json

if TYPE_CHECKING:
    from .paths import Paths

logger = logging.getLogger(__name__)

NTFY = "https://ntfy.sh"
DAY = 24 * 3600.0
# How long every turn has been refused before it is the line out's; a day, the owner's word.
REFUSED_FOR = DAY
# A push that did not go is tried again this often while its cause holds; it sets how soon,
# never whether.
RETRY_SECONDS = 600.0
SEND_TIMEOUT = 30.0

NOTHING_TO_DO = "Nothing to do; the owner will fix it when back."
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
}


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
        self._refused_since: float | None = kept.get("refused_since")
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
            if self._refused_since is not None:
                self._refused_since = None
                self._save()
        elif event.type == "agent.turn_failed":
            if self._refused_since is None:
                self._refused_since = event.ts
                self._save()
            if (event.data.get("error") == claude_login.TURN_REFUSED
                    and not claude_login.is_set(self.paths.claude_login)):
                self.raise_cause("no_credential", event.ts)
        elif event.type == "disk.short":
            self._short = True
        elif event.type == "garbage.collected" and self._short:
            self._short = False
            if self._holds("disk_full", event.ts):
                self.raise_cause("disk_full", event.ts)

    def tick(self, now: float) -> None:
        if "api_refused" not in self._pending and self._holds("api_refused", now):
            self.raise_cause("api_refused", now)
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
        return (self._refused_since is not None
                and now - self._refused_since >= REFUSED_FOR)

    def raise_cause(self, cause: str, now: float) -> None:
        """Sent unless it went in the last day or no topic is set. A push that does not go is
        said (`lineout.failed`) and tried again while its cause holds, never raised: the line
        out exists for when the machine is already stuck, and must not stop it further."""
        if now - self._sent.get(cause, 0.0) < DAY:
            self._pending.pop(cause, None)
            return
        topic = settings.in_force(self.paths).ntfy_topic
        if not topic:
            self._pending.pop(cause, None)
            return
        title, message = MESSAGES[cause]
        try:
            send(topic, title, message)
        except (OSError, http.client.HTTPException) as exc:
            self._pending[cause] = now + RETRY_SECONDS
            self.events.emit("lineout.failed", cause=cause, error=str(exc))
            logger.warning("the line out could not send %s: %s", cause, exc)
            return
        self._pending.pop(cause, None)
        self._sent[cause] = now
        self._save()
        self.events.emit("lineout.sent", cause=cause, title=title)

    def _save(self) -> None:
        save_json(self.paths.line_out, {"sent": self._sent,
                                        "refused_since": self._refused_since})
