"""A hand-on claims the successor's sandbox at once and brings it up off the caller's thread
(`Session.succeed_tab`), so the new agent can call its tools before the sandbox is up. They
wait for the open rather than answer from a sandbox held in the intent with no containers."""
from __future__ import annotations

import threading

from raigolmid import naming
from raigolmid.session import SessionError
from tests.harness import Harness


def test_a_successors_command_waits_for_its_carried_sandbox(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    h.open_sandbox("myapi", "python-dev")
    h.launchers.exec_results["true"] = (0, "")
    entered, release = threading.Event(), threading.Event()
    create = h.session.instances.create

    def slow_create(*args, **kwargs):
        entered.set()
        release.wait(20)
        return create(*args, **kwargs)

    monkeypatch.setattr(h.session.instances, "create", slow_create)
    successor = h.session.succeed_tab(h.tab("myapi"), by="self").tab_id
    carried = naming.instance_id("myapi", successor)
    assert entered.wait(10), "the carried sandbox's create never started"
    assert h.session.opening(carried)

    answers: list[object] = []

    def run() -> None:
        try:
            answers.append(h.session.exec(carried, ["true"]))
        except SessionError as exc:
            answers.append(exc)

    caller = threading.Thread(target=run)
    caller.start()
    caller.join(0.5)
    assert caller.is_alive(), f"exec answered while the sandbox opened: {answers}"
    release.set()
    caller.join(20)
    assert not h.session.opening(carried)
    [answer] = answers
    assert isinstance(answer, dict) and answer["exit_code"] == 0, answer
