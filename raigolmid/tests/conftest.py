import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The repository root too, for the host surfaces under `ui/`: every test file finds them,
# not only those collected after one that happened to add it.
for path in (ROOT, ROOT.parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


import urllib.error
import urllib.request

import pytest

from tests import facedisplay


@pytest.fixture(autouse=True)
def the_default_look(tmp_path_factory):
    """The user's look as the daemon writes it from the shipped settings, loaded as every surface's
    `main` loads it before drawing (`ui/theme.py`)."""
    from raigolmid import settings
    from ui import theme
    path = tmp_path_factory.mktemp("look") / "look.json"
    path.write_text(json.dumps(settings.load(settings.SHIPPED).look))
    theme.load(path)


@pytest.fixture(autouse=True)
def runtime_dir(monkeypatch):
    """Each test's XDG_RUNTIME_DIR, short and outside `tmp_path`: a Unix socket's path is
    limited to 108 bytes, and one under a test's `tmp_path` grows with the test's name."""
    short = Path(tempfile.mkdtemp(prefix="rai-", dir="/tmp"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short))
    yield short
    shutil.rmtree(short)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """The suite reaches no network, as an offline machine does not: an answer a test needs
    from a registry is scripted."""
    def refused(request, *args, **kwargs):
        raise urllib.error.URLError("the test suite reaches no network")

    monkeypatch.setattr(urllib.request, "urlopen", refused)


@pytest.fixture(autouse=True)
def no_face_display_outlives_its_test():
    """A face's backing process (`facedisplay.py`) ends with its test, whether or not the
    test stopped the face."""
    yield
    facedisplay.end_all()
