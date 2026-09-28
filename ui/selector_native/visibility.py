"""When the selector closes on its own account, kept apart from GTK so the suite can drive it."""
from __future__ import annotations


def hides_after(request: tuple[str, dict], failed: bool) -> bool:
    """Whether a daemon call that has landed takes the selector off screen: only a face
    selected. The face is fullscreened under the selector, and choosing it is choosing what to
    look at; the body is set-up, and a failure is read on the selector's detail line."""
    method, params = request
    return not failed and method == "select" and params["kind"] == "face"
