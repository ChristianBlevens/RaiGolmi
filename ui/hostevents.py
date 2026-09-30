"""A host surface following the daemon's events, for when to read its state again.

The stream says only when to look; what is drawn is read from the daemon each time, so a
change missed between two streams — a daemon restart ends the stream — is read once the next
one is acknowledged. Runs for good, on its own thread.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from raigolmid.client import ApiClient, ApiError

logger = logging.getLogger(__name__)

# A restarting daemon is back within a few seconds, and nothing is lost meanwhile.
RECONNECT_SECONDS = 2.0


def follow(client: ApiClient, prefixes: tuple[str, ...], fetch: Callable[[], None],
           lost: Callable[[str], None] | None = None) -> None:
    """`fetch()` once each stream is acknowledged, and on every event whose type starts with
    one of `prefixes`; `lost(why)` each time a stream ends or cannot be opened."""
    while True:
        subscribed = threading.Event()

        def acknowledged() -> None:
            subscribed.wait()
            fetch()

        threading.Thread(target=acknowledged, name="subscribed", daemon=True).start()
        try:
            for event in client.subscribe(subscribed):
                if event["type"].startswith(prefixes):
                    fetch()
        except (ApiError, OSError) as exc:
            why = str(exc)
            logger.warning("the daemon's event stream ended: %s; following it again in "
                           "%.0fs", exc, RECONNECT_SECONDS)
        else:
            why = "the daemon closed the event stream"
            logger.warning("the daemon closed the event stream; following it again")
        if lost is not None:
            lost(why)
        subscribed.set()
        time.sleep(RECONNECT_SECONDS)
