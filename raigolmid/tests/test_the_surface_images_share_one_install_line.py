"""The three host surface images install their GTK stack with one byte-identical line, so every
build after the first is a cache hit."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_the_surfaces_install_their_stack_with_the_same_line():
    """The wait between power-on and a usable screen is mostly these builds, and all but the
    first are free only while the line is identical: Docker keys a layer on the instruction
    and its parent, so one changed package name costs a fresh machine another 70-second
    `dnf` — and costs it silently, because both images still build."""
    def install_line(surface: str) -> str:
        text = (ROOT / "ui" / surface / "Containerfile").read_text()
        start = text.index("RUN dnf install")
        return text[start:text.index("dnf clean all", start)]

    assert install_line("selector_native") == install_line("host_control") \
        == install_line("notify_popup")
