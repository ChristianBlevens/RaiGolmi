"""The view must be exempted from *both* of the runtime's sandboxes, not one.

Assembling the view's root needs mount, setns, open_tree and pivot_root, and two separate
mechanisms refuse them for different reasons. AppArmor's container profile blocks mount and
pivot_root outright. Docker's builtin seccomp profile is the one that is easy to miss: it
gates mount, setns and open_tree on CAP_SYS_ADMIN, which the view is given, but never lists
pivot_root at all — so the pivot alone falls to that profile's default EPERM *after* every
mount has already succeeded. A capability present for one syscall and absent for the next
is the signature, and it points at a syscall filter rather than at privilege.

The two are independent: an AppArmor host applies one, a SELinux host the other, and
loosening either alone leaves a view that dies at `pivot_root` with errno 1.

Asserted on the spec the runtime is handed, because no double can refuse a syscall the way
a kernel does — a fake that accepted one option and ignored the other would pass either way.
Enforcement is the kernel's to demonstrate, and it does.
"""
from __future__ import annotations

import pytest

from raigolmid import naming

from tests.harness import Harness


@pytest.fixture()
def h(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


def test_the_view_is_exempted_from_apparmor_and_seccomp(h):
    # A body on its own builds no session view: the view is the toolbelt's container.
    h.open_sandbox("myapi", "python-dev")
    spec = h.runtime.spec_of(naming.view(h.session.intent.focused_instance))
    assert "apparmor=unconfined" in spec.security_opt
    assert "seccomp=unconfined" in spec.security_opt


def test_the_view_still_asks_for_only_the_two_setup_capabilities(h):
    """Relaxing the profiles must not become a reason to widen the capability set: the
    boundary is the entrypoint's drop, and it can only drop what was named here."""
    # A body on its own builds no session view: the view is the toolbelt's container.
    h.open_sandbox("myapi", "python-dev")
    spec = h.runtime.spec_of(naming.view(h.session.intent.focused_instance))
    assert tuple(spec.cap_add) == ("SYS_ADMIN", "SYS_PTRACE")
