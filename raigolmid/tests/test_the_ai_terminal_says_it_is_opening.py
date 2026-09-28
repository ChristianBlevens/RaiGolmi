"""Opening a tab is this machine's longest wait.

The agent image is built once per machine and the first tab either runs that build or waits
on the one the daemon started at boot.
The daemon client waits ten minutes before giving up, so a silent terminal is
indistinguishable from a broken one for longer than anyone will sit. What has to hold is that
a slow open says so and a quick one stays quiet.
"""
from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stdout

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ui.ai_terminal.terminal import while_it_opens


def test_the_daemon_s_refusal_is_raised_and_never_swallowed():
    """⚠ The worker runs the call, so an exception on it is one the caller would never see
    unless it is carried back. A tab that failed to open and reported nothing is the defect
    this whole function exists to remove, wearing different clothes."""
    class Refused(Exception):
        pass

    def refuse():
        raise Refused("raigolmid said no")

    with redirect_stdout(io.StringIO()):
        with pytest.raises(Refused, match="raigolmid said no"):
            while_it_opens("Opening tab-1.", refuse)


