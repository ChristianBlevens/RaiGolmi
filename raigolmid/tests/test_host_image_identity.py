"""The host image must give the launcher socket directory to the user `raigolmid`
runs as, and that user must not be root.

This is the other half of `test_view_runs_as_the_working_copy_owner`. That one asserts the
view reads its uid from the socket directory's owner; this one asserts the host image
makes that owner the daemon's user. The two facts live in four files — a `sysusers.d`
entry, a `tmpfiles.d` entry, a systemd unit and a Containerfile — and nothing at runtime
compares them: a root-owned directory here produces a view whose launcher is root with no
capabilities, which answers `ping`, reads as healthy, and cannot write a byte of `/work`.

Read rather than run, like its sibling: the image cannot be built in this container.
"""
from __future__ import annotations

from pathlib import Path

HOST = Path(__file__).resolve().parents[2] / "host"
SYSUSERS = (HOST / "sysusers" / "raigolmi.conf").read_text(encoding="utf-8")
TMPFILES = (HOST / "tmpfiles" / "raigolmi.conf").read_text(encoding="utf-8")
UNIT = (HOST / "systemd" / "raigolmid.service").read_text(encoding="utf-8")
CONTAINERFILE = (HOST / "Containerfile").read_text(encoding="utf-8")


def _lines(text: str) -> list[list[str]]:
    return [line.split() for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def _desktop_user() -> str:
    for fields in _lines(SYSUSERS):
        if fields[0] == "u":
            return fields[1]
    raise AssertionError("sysusers.conf declares no user")


def _tmpfiles_entry(path: str) -> list[str]:
    for fields in _lines(TMPFILES):
        if len(fields) > 1 and fields[1] == path:
            return fields
    raise AssertionError(f"tmpfiles.conf has no entry for {path}")


def test_the_desktop_user_is_not_root():
    assert _desktop_user() != "root"


def test_the_launcher_socket_directory_belongs_to_that_user():
    """The view's uid is read from this directory's owner."""
    kind, _, mode, owner, group, *_ = _tmpfiles_entry("/run/raigolmid/views")
    assert kind == "d"
    assert (owner, group) == (_desktop_user(), _desktop_user())
    assert mode == "0700", "the socket inside is mode-open; the directory is the privacy"


def test_raigolmid_is_a_user_unit_and_names_no_other_identity():
    """A `User=` here would be a second source for the identity the socket directory
    already carries, and the two would drift."""
    assert "User=" not in UNIT
    assert "/usr/lib/systemd/user/raigolmid.service" in CONTAINERFILE
    assert "/usr/lib/systemd/system/raigolmid.service" not in CONTAINERFILE


SUDOERS = (HOST / "sudoers-raigolmi").read_text(encoding="utf-8")


def test_the_desktop_user_can_become_root_without_a_password():
    """The account ships with no password, so a sudo prompt is unanswerable rather than
    inconvenient — and `passwd` refuses a passwordless account trying to set its own.
    Without this the machine cannot be administered from its own console at all, which
    is a recovery failure rather than a hardening choice."""
    rule = _lines(SUDOERS)[0]
    assert rule[0] == "%wheel"
    assert "NOPASSWD:" in rule
    assert ["m", _desktop_user(), "wheel"] in _lines(SYSUSERS), \
        "the sudoers rule targets wheel, so the user must be in it"


def test_the_sudoers_drop_in_is_installed_and_checked_at_build_time():
    """A malformed sudoers file disables sudo outright. Finding that out on the booted
    machine means finding it out with no way to repair it."""
    assert "/etc/sudoers.d/raigolmi" in CONTAINERFILE
    assert "visudo -c -f /etc/sudoers.d/raigolmi" in CONTAINERFILE


def test_an_image_changes_with_what_it_copies_and_nothing_else(tmp_path):
    from raigolmid.hostimages import HostImage
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("print(1)\n")
    (tmp_path / "docs.md").write_text("notes\n")
    (tmp_path / "Containerfile").write_text("FROM fedora:42\nCOPY --chown=1:1 app/ \\\n  /opt/app/\n")
    image = HostImage("x", tmp_path, tmp_path / "Containerfile")
    before = image.digest()
    (tmp_path / "docs.md").write_text("other notes\n")
    assert image.digest() == before, "a file the image does not copy rebuilt it"
    (tmp_path / "app" / "main.py").write_text("print(2)\n")
    assert image.digest() != before


def test_a_new_tag_removes_the_old_ones_nothing_was_created_from(tmp_path, monkeypatch):
    from raigolmid import hostimages
    from raigolmid.runtime.base import ContainerSpec
    from tests.fakeruntime import FakeRuntime
    monkeypatch.setenv(hostimages.ARCHIVE_ENV, str(tmp_path / "none.tar"))
    runtime = FakeRuntime()
    for old in ("raigolmi/claude:aaa", "raigolmi/claude:bbb", "raigolmi/notify:ccc"):
        runtime.add_image(old)
    runtime.run(ContainerSpec(name="tab-1", image="raigolmi/claude:bbb"))
    (tmp_path / "Containerfile").write_text("FROM fedora:42\n")
    image = hostimages.HostImage("claude", tmp_path, tmp_path / "Containerfile")
    tag = hostimages.ensure(runtime, image)
    tags = {t for i in runtime.list_images() for t in i.tags}
    assert tag in tags
    assert "raigolmi/claude:aaa" not in tags
    assert "raigolmi/claude:bbb" in tags, "a tab's container was created from it"
    assert "raigolmi/notify:ccc" in tags, "another image's tags are not this one's"


def test_nothing_updates_the_os_on_a_timer():
    """The base image's timer runs `bootc upgrade --apply`, which reboots unannounced; the OS
    updates only when the user asks. `is-enabled` reads `disabled`
    while it still fires, so only a mask stops it."""
    assert "systemctl mask bootc-fetch-apply-updates.timer" in CONTAINERFILE
