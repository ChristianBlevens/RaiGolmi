"""A gated action waits for the user's yes: a toolbelt swap of the sandbox the face is on.

The tab's call raises a permission and returns; `Permissions` carries the swap out on a yes
and tells the tab what came of it on its channel. Everything here is the real path — the
tab's scope, `Questions`, `Channels` and `Permissions` over one event log — so an answer
that reaches nothing, or a swap done without one, fails here.
"""
from __future__ import annotations

import threading
import time

import pytest

from raigolmid.channel import Channels
from raigolmid.permissions import Permissions
from raigolmid import settings
from raigolmid.questions import NO_ANSWER, QuestionError, Questions
from raigolmid.scopes import build_tab_methods

from tests.harness import Harness


@pytest.fixture()
def world(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    questions, channels = Questions(h.events, h.paths), Channels(h.session, h.events)
    permissions = Permissions(h.session, h.events)
    stop = threading.Event()
    thread = threading.Thread(target=permissions.run, args=(stop,), daemon=True)
    thread.start()
    h.open_sandbox("myapi", "python-dev")
    h.session.agent_session_started(h.tab("myapi"))
    try:
        yield h, questions, channels, build_tab_methods(h.session, questions, channels,
                                                        h.tab("myapi"))
    finally:
        stop.set()
        thread.join(5)


def settled(h, id: str) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        for e in h.events.tail(500):
            if e.type in ("permission.settled", "permission.failed") and e.data["id"] == id:
                return e.data
        time.sleep(0.02)
    raise AssertionError(f"permission {id} was never settled")


def mail(h, channels) -> dict:
    """The tab's next message. An event reaches the log before its subscribers, so the
    outcome is waited for on the channel, as the tab's MCP server polls for it."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        message = channels.take(h.tab("myapi"))
        if message is not None:
            return message
        time.sleep(0.02)
    raise AssertionError("the outcome never reached the tab's channel")


def toolbelt(h) -> str:
    return h.session.intent.instances[f"myapi@{h.tab('myapi')}"].toolbelt


def test_the_face_tab_asks_and_nothing_changes_until_he_answers(world):
    h, questions, _, tab = world
    asked = tab["toolbelt_swap"](toolbelt="no-lsp")
    assert asked["status"] == "asked"
    [item] = questions.pending()
    assert item["kind"] == "permission" and item["tab"] == h.tab("myapi")
    assert tuple(item["choices"]) == ("yes", "no")
    assert toolbelt(h) == "python-dev"
    with pytest.raises(QuestionError, match="yes or no"):
        questions.answer(item["id"], "only on weekdays")


def test_a_yes_swaps_and_the_tab_is_told(world):
    h, questions, channels, tab = world
    id = tab["toolbelt_swap"](toolbelt="no-lsp")["permission"]
    questions.answer(id, "yes")

    outcome = settled(h, id)
    assert outcome["answer"] == "yes"
    assert toolbelt(h) == "no-lsp"
    assert "it is done" in mail(h, channels)["content"]


@pytest.mark.parametrize("settle", ["no", "lapse"])
def test_no_answer_but_yes_leaves_the_toolbelt_and_says_so(world, settle):
    h, questions, channels, tab = world
    id = tab["toolbelt_swap"](toolbelt="no-lsp")["permission"]
    if settle == "no":
        questions.answer(id, "no")
    else:
        questions.lapse(now=questions._items[id].asked_at
                        + settings.load(h.paths.settings).lapse_seconds)

    outcome = settled(h, id)
    assert outcome["answer"] == ("no" if settle == "no" else NO_ANSWER)
    assert toolbelt(h) == "python-dev"
    assert "did not allow" in mail(h, channels)["content"]


def test_a_tab_whose_sandbox_the_face_is_not_on_swaps_without_asking(world):
    """The user works in the sandbox the face is on; another tab's is its own to change."""
    h, questions, channels, tab = world
    other = h.open_sandbox("webui", "python-dev")
    assert h.session.intent.focused_instance == other

    tab["toolbelt_swap"](toolbelt="no-lsp")
    assert questions.pending() == []
    assert toolbelt(h) == "no-lsp"

    webui = build_tab_methods(h.session, questions, channels, h.tab("webui"))
    assert webui["toolbelt_swap"](toolbelt="no-lsp")["status"] == "asked"
    assert h.session.intent.instances[other].toolbelt == "python-dev"


def test_an_always_yes_swaps_the_next_time_without_asking_the_user(world):
    h, questions, channels, tab = world
    first = tab["toolbelt_swap"](toolbelt="no-lsp")["permission"]
    questions.answer(first, "yes", always="project")
    settled(h, first)

    again = tab["toolbelt_swap"](toolbelt="python-dev")
    assert again["status"] == "answered_always"
    assert questions.pending() == []
    outcome = settled(h, again["permission"])
    assert outcome["answer"] == "yes"
    assert toolbelt(h) == "python-dev"
    assert "The user's standing answer (always) allowed it" in outcome["deliver"]["content"]
    # Done in the user's name, so it is in the history they read.
    item = questions.items()[again["permission"]]
    assert (item["state"], item["outcome"], item["by"]) == ("answered", "yes", "always")


def test_an_always_no_refuses_without_asking_the_user(world):
    h, questions, channels, tab = world
    first = tab["toolbelt_swap"](toolbelt="no-lsp")["permission"]
    questions.answer(first, "no", always="everywhere")
    settled(h, first)

    again = tab["toolbelt_swap"](toolbelt="no-lsp")
    assert again["status"] == "answered_always"
    assert settled(h, again["permission"])["answer"] == "no"
    assert toolbelt(h) == "python-dev"


def test_an_always_for_one_project_asks_again_in_another_and_everywhere_does_not(world):
    _, questions, _, _ = world
    action = {"do": "toolbelt_swap", "toolbelt": "no-lsp"}
    id, _ = questions.ask_permission("tab-1", "m", action, project="myapi")
    questions.answer(id, "yes", always="project")

    _, always = questions.ask_permission("tab-1", "m", action, project="other")
    assert always is None and len(questions.pending()) == 1

    questions.answer(questions.pending()[0]["id"], "yes", always="everywhere")
    _, always = questions.ask_permission("tab-1", "m", action, project="third")
    assert always is not None and always.project is None


def test_the_users_permissions_doc_is_what_the_next_ask_reads_and_keeps_the_users_lines(world):
    h, questions, _, tab = world
    questions.answer(tab["toolbelt_swap"](toolbelt="no-lsp")["permission"], "no",
                     always="project")
    assert "- no toolbelt_swap in myapi" in h.paths.permissions.read_text()

    h.paths.permissions.write_text("# mine\n")
    assert tab["toolbelt_swap"](toolbelt="no-lsp")["status"] == "asked"
    questions.answer(questions.pending()[0]["id"], "yes", always="everywhere")
    assert h.paths.permissions.read_text() == "# mine\n- yes toolbelt_swap everywhere\n"


def test_an_always_outlives_the_daemon(world):
    h, questions, _, tab = world
    questions.answer(tab["toolbelt_swap"](toolbelt="no-lsp")["permission"], "no",
                     always="project")
    restarted = Questions(h.events, h.paths)
    _, always = restarted.ask_permission("tab-1", "m", {"do": "toolbelt_swap"},
                                         project="myapi")
    assert always is not None and always.answer == "no"
