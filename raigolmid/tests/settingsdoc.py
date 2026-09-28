"""The user's settings document for a test: the shipped one, with the values a test names
changed."""
from __future__ import annotations

import json
from pathlib import Path

from raigolmid import settings


def text(**changes: dict[str, object]) -> str:
    """`text(look={"accent": "#ff0000"})`: every other line is the shipped document's."""
    pending = {(section, key): value for section, values in changes.items()
               for key, value in values.items()}
    lines, section = [], None
    for line in settings.SHIPPED.read_text(encoding="utf-8").splitlines():
        if line.startswith("["):
            section = line.strip("[]")
        key = line.split("=")[0].strip() if "=" in line and not line.startswith("#") else None
        if (section, key) in pending:
            line = f"{key} = {json.dumps(pending.pop((section, key)))}"
        lines.append(line)
    assert not pending, f"not in the shipped document: {sorted(pending)}"
    return "\n".join(lines) + "\n"


def write(path: Path, **changes: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text(**changes), encoding="utf-8")
    return path
