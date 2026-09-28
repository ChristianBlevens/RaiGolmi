"""`wait_until_usable` is production logic, and these tests enter it: a harness that
replaces it with a lambda makes every "the view came up" assertion mean only that
`Views.create` returned.

What it actually does (views.py:225-246) is poll, notice a view container that vanished or
exited, pull its container log, and raise `ViewError` carrying that log —
`instances.py:264-269` then tears the view down and marks the instance degraded. The log is
the whole value: a view that exits before its launcher binds is the failure shape this
project produces most readily, and the reason is only ever in the container's output.
"""
import pytest

from raigolmid import naming
from raigolmid.views import ViewError

from .harness import Harness


@pytest.fixture()
def h(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


def _select_full_session(h):
    return h.open_sandbox("myapi", "python-dev")


def test_a_view_that_exits_before_its_launcher_raises_with_the_container_log(h):
    instance = _select_full_session(h)
    view = naming.view(instance)
    h.runtime.staged_logs[view] = "pivot_root: Operation not permitted\n"
    h.runtime.kill(view, exit_code=1)

    with pytest.raises(ViewError) as caught:
        h.session.views.wait_until_usable(instance, timeout=2)

    message = str(caught.value)
    assert "exited before its launcher came up" in message
    assert "pivot_root: Operation not permitted" in message, \
        "the container log is the reason, and an error without it says nothing"


def test_a_view_whose_launcher_never_answers_times_out_with_the_log(h):
    """The container is up and the launcher is not. That is neither of the two above, and
    it is the case the timeout exists for."""
    instance = _select_full_session(h)
    view = naming.view(instance)
    h.runtime.staged_logs[view] = "launcher: bind: Address already in use\n"
    h.launchers.unreachable.add(instance)

    with pytest.raises(ViewError) as caught:
        h.session.views.wait_until_usable(instance, timeout=1)

    message = str(caught.value)
    assert "did not answer" in message
    assert "Address already in use" in message


def test_a_view_docker_is_removing_fails_as_a_view_error_not_a_refused_log(h):
    """Docker refuses logs for a container marked for removal. That refusal is the answer
    and must come back inside the `ViewError` `Instances.start_view` catches — raised on its
    own it escapes that handler and the instance is never marked degraded."""
    instance = _select_full_session(h)
    h.runtime.mark_for_removal(naming.view(instance))

    with pytest.raises(ViewError, match="docker refused this container's logs"):
        h.session.views.wait_until_usable(instance, timeout=1)
