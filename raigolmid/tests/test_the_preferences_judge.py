"""The preferences judge, with `Questions`, over one event log.

The judge's `claude -p` is the fake runtime's scripted one-shot, printing what the real run
prints: a stray line on stderr, then the result object. Everything a run could say that the
judge must not believe is scripted here too, because a judge that only ever met a good answer
would pass while believing anything.
"""
from __future__ import annotations


import pytest

from raigolmid import naming
from raigolmid.judge import parse_result
from raigolmid.questions import QuestionError

from tests.harness import judge_says, settle_judging, standalone

DOC = "# Git\n- Commits go straight to main; never open a branch.\n"


def of(events, type_: str) -> list:
    return [e for e in events.tail(200) if e.type == type_]


def sent(events) -> list[str]:
    return [e.data["deliver"]["content"] for e in events.tail(200) if e.data.get("deliver")]


@pytest.fixture()
def world(tmp_path, monkeypatch):
    events, questions, judge, runtime = standalone(tmp_path, monkeypatch)
    judge.doc.write_text(DOC)
    return events, questions, judge, runtime


def test_a_question_the_preferences_settle_is_answered_with_the_line_it_came_from(world):
    events, questions, judge, runtime = world
    prompts = judge_says(runtime, "ANSWER: main\nQUOTE: - Commits go straight to main; "
                                  "never open a branch.")
    questions.ask("tab-1", "Commit to main or open a branch?", ["main", "branch"])
    settle_judging(judge, questions)

    assert DOC in prompts[0] and "Commit to main or open a branch?" in prompts[0]
    assert not of(events, "question.referred"), "the user is never asked"
    (item,) = questions.items().values()
    assert (item["state"], item["by"], item["outcome"]) == ("answered", "preferences", "main")
    (message,) = sent(events)
    assert "never open a branch" in message and message.endswith("main")
    assert questions.tab_state("tab-1") is None


def test_a_question_they_do_not_settle_is_put_to_the_user(world):
    events, questions, judge, runtime = world
    judge_says(runtime, "UNCLEAR")
    id = questions.ask("tab-1", "Which colour?", ["blue", "green"])
    assert questions.pending() == [], "not the user's while the judge has it"
    assert questions.tab_state("tab-1") == "waiting"
    settle_judging(judge, questions)
    assert [i["id"] for i in questions.pending()] == [id]
    assert sent(events) == []


@pytest.mark.parametrize("says, why", [
    ("ANSWER: main\nQUOTE: - Always open a branch.", "not in the preferences"),
    ("ANSWER: rebase\nQUOTE: - Commits go straight to main; never open a branch.",
     "not one of the choices"),
    ("Main, I think.", "neither ANSWER/QUOTE"),
    ("ANSWER: main", "neither ANSWER/QUOTE"),
])
def test_a_verdict_it_cannot_stand_behind_is_a_failure_and_he_is_asked(world, says, why):
    events, questions, judge, runtime = world
    judge_says(runtime, says)
    id = questions.ask("tab-1", "Main or a branch?", ["main", "branch"])
    settle_judging(judge, questions)
    (failed,) = of(events, "judge.failed")
    assert why in failed.data["error"]
    assert [i["id"] for i in questions.pending()] == [id], "put to the user"
    assert (failed.data["id"], failed.data["stage"]) == (id, "judge")


@pytest.mark.parametrize("output, why", [
    ('{"type": "result", "is_error": true, "result": "Invalid API key"}', "reported an error"),
    ("Error: not logged in", "printed no result"),
])
def test_a_run_that_did_not_answer_is_a_failure_with_what_it_printed(output, why):
    with pytest.raises(Exception, match=why) as raised:
        parse_result(output)
    assert ("Invalid API key" if "API" in output else "not logged in") in str(raised.value)


def test_what_he_types_in_the_tab_is_learned_and_nothing_is_sent(world):
    """The user's prompt was the answer and has already started the tab's turn."""
    events, questions, judge, runtime = world
    prompts = judge_says(runtime, "UNCLEAR", DOC + "- Buttons are blue.")
    questions.ask("tab-1", "Which colour?", ["blue", "green"])
    settle_judging(judge, questions)

    questions.answered_in_terminal("tab-1", "blue")
    assert questions.tab_state("tab-1") is None, "the user's typing is not held"
    settle_judging(judge, questions)
    assert "<user-answer>blue</user-answer>" in prompts[1] and DOC in prompts[1]
    assert judge.doc.read_text() == DOC + "- Buttons are blue.\n", "and no stderr in it"
    assert sent(events) == []


def test_an_answer_the_judge_cannot_learn_is_a_failure_said_as_one(world):
    events, questions, judge, runtime = world
    judge_says(runtime, "UNCLEAR", "```\n- Buttons are blue.\n```")
    id = questions.ask("tab-1", "Which colour?")
    settle_judging(judge, questions)
    questions.answered_in_terminal("tab-1", "blue")
    settle_judging(judge, questions)
    assert judge.doc.read_text() == DOC
    (failed,) = of(events, "judge.failed")
    assert (failed.data["id"], failed.data["stage"]) == (id, "learn")


def test_an_overturn_is_sent_as_a_correction_and_learned(world):
    events, questions, judge, runtime = world
    prompts = judge_says(runtime,
                         "ANSWER: main\nQUOTE: - Commits go straight to main; never open a branch.",
                         "# Git\n- Commits go to main, except a release goes on a branch.")
    id = questions.ask("tab-1", "This is the release: main or a branch?", ["main", "branch"])
    settle_judging(judge, questions)
    with pytest.raises(QuestionError, match="needs the user's answer"):
        questions.overturn(id, " ")

    questions.overturn(id, "branch")
    assert len(sent(events)) == 1, "held until it is learned"
    settle_judging(judge, questions)
    assert "overturned" in prompts[1] and "'main'" in prompts[1]
    assert "release goes on a branch" in judge.doc.read_text()
    correction = sent(events)[-1]
    assert "overturned" in correction and correction.endswith("branch")
    item = questions.items()[id]
    assert (item["by"], item["outcome"], item["overturned"]) == ("overturn", "branch", "main")
    with pytest.raises(QuestionError, match="not answered from the preferences"):
        questions.overturn(id, "main")


def test_the_users_overturn_for_a_closed_tab_is_learned_and_sent_nowhere(world):
    events, questions, judge, runtime = world
    judge_says(runtime, "ANSWER: main\nQUOTE: - Commits go straight to main; never open a branch.",
               DOC + "- A release goes on a branch.")
    id = questions.ask("tab-1", "This is the release: main or a branch?", ["main", "branch"])
    settle_judging(judge, questions)
    questions.overturn(id, "branch")
    events.emit("tab.closed", tab="tab-1")
    settle_judging(judge, questions)
    assert "A release goes on a branch" in judge.doc.read_text()
    assert len(sent(events)) == 1, "only the preferences' answer, sent before it closed"


def test_dropped_events_are_made_good_by_offering_again(world):
    events, questions, judge, runtime = world
    judge_says(runtime, "UNCLEAR")
    id = questions.ask("tab-1", "Which colour?")
    judge._sub.drain(timeout=0.05)                     # the judge never hears it
    events.emit("judge.events_dropped", count=1)
    settle_judging(judge, questions)
    assert [i["id"] for i in questions.pending()] == [id]


def test_a_failure_of_any_kind_ends_the_job_and_he_is_asked(world):
    """The worker runs every question after this one, so no failure may end it: one Docker
    raised past the listed errors left every later question `judging` for good."""
    events, questions, judge, runtime = world

    def broken(spec):
        raise ConnectionError("the Docker socket reset")
    runtime.one_shot[naming.judge()] = broken
    id = questions.ask("tab-1", "Main?")
    settle_judging(judge, questions)
    (failed,) = of(events, "judge.failed")
    assert "ConnectionError: the Docker socket reset" in failed.data["error"]
    assert [i["id"] for i in questions.pending()] == [id], "put to the user instead"
