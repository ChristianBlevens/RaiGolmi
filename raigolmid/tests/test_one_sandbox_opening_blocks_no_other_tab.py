"""The session lock guards intent and never spans a sandbox's container work (instances
run in parallel). A body build or view start takes minutes; held under the lock,
it would stall every other tab's hooks, the selector and every close for as long."""
from __future__ import annotations

import threading

from tests.harness import Harness


def test_another_tabs_hook_answers_while_a_sandbox_is_opening(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    h.session.select("body", "myapi")
    body_tab, machine_tab = h.tab("myapi"), h.tab(None)

    entered, release = threading.Event(), threading.Event()
    create = h.session.instances.create

    def slow_create(*args, **kwargs):
        entered.set()
        release.wait(20)
        return create(*args, **kwargs)

    monkeypatch.setattr(h.session.instances, "create", slow_create)
    opening = threading.Thread(target=h.session.sandbox_open, args=(body_tab, "python-dev"))
    opening.start()
    try:
        assert entered.wait(10), "the sandbox's create never started"
        answered = threading.Event()
        hook = threading.Thread(target=lambda: (
            h.session.agent_activity(machine_tab, busy=True), answered.set()))
        hook.start()
        assert answered.wait(5), "a hook from another tab waited on this sandbox's create"
    finally:
        release.set()
        opening.join(20)
    assert h.session.intent.sandbox_of(body_tab) is not None


def test_a_tab_closed_while_its_sandbox_opens_leaves_nothing_running(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    h.session.select("body", "myapi")
    body_tab = h.tab("myapi")

    entered, release = threading.Event(), threading.Event()
    create = h.session.instances.create

    def slow_create(*args, **kwargs):
        entered.set()
        release.wait(20)
        return create(*args, **kwargs)

    monkeypatch.setattr(h.session.instances, "create", slow_create)
    errors: list[BaseException] = []

    def open_it() -> None:
        try:
            h.session.sandbox_open(body_tab, "python-dev")
        except BaseException as exc:                   # noqa: BLE001
            errors.append(exc)

    opening = threading.Thread(target=open_it)
    opening.start()
    assert entered.wait(10)
    closing = threading.Thread(target=h.session.close_tab, args=(body_tab,))
    closing.start()
    release.set()
    opening.join(20)
    closing.join(20)
    assert errors and "closed while its sandbox opened" in str(errors[0])
    assert h.session.instances.all() == {}, "the sandbox of a closed tab is still running"
