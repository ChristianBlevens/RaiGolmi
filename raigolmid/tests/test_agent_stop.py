"""A turn ending with background commands running, on the `Stop` hook input Claude
Code 2.1.278 sends."""
from __future__ import annotations


from raigolmid.agent_stop import Memory, decide


def _stop(*running: str, active: bool = False, said: str = "Done.") -> dict:
    return {
        "hook_event_name": "Stop", "stop_hook_active": active,
        "last_assistant_message": said, "session_crons": [],
        "background_tasks": [{"id": i, "type": "shell", "status": "running",
                              "description": f"task {i}", "command": f"sleep {i}"}
                             for i in running],
    }


def test_a_running_command_not_yet_asked_about_refuses_the_stop():
    d = decide(_stop("b1"), Memory())
    assert d.report is None, "a refused stop must not report: the prompt's busy stands"
    assert "b1" in d.refusal and "sleep b1" in d.refusal and "waiting:" in d.refusal


def test_an_agent_waiting_on_a_command_stays_busy_until_it_finishes():
    asked = decide(_stop("b1"), Memory()).memory
    d = decide(_stop("b1", active=True, said="It runs the tests.\nwaiting: b1"), asked)
    assert d.report == "busy"
    # It finished: the notification's turn ends with nothing running.
    assert decide(_stop(), d.memory).report == "idle"


def test_a_command_left_running_on_purpose_is_asked_about_once():
    asked = decide(_stop("server"), Memory()).memory
    d = decide(_stop("server", active=True, said="The server stays up.\nwaiting: none"),
               asked)
    assert d.report == "idle"
    later = decide(_stop("server"), d.memory)
    assert (later.report, later.refusal) == ("idle", None)


def test_only_the_named_commands_keep_the_tab():
    asked = decide(_stop("server", "tests"), Memory()).memory
    d = decide(_stop("server", "tests", active=True, said="waiting: `tests`"), asked)
    assert d.report == "busy"
    assert decide(_stop("server"), d.memory).report == "idle"


def test_a_new_command_after_an_answer_is_asked_about():
    settled = decide(_stop("server", active=True, said="waiting: none"),
                     decide(_stop("server"), Memory()).memory).memory
    d = decide(_stop("server", "build"), settled)
    assert d.refusal is not None and "build" in d.refusal and "server" not in d.refusal


def test_an_unanswered_refusal_keeps_the_tab():
    """No `waiting:` line settles nothing. Idle would close the tab with a command the
    agent may need, so it is kept as an interrupted agent is, and asked again next time."""
    asked = decide(_stop("b1"), Memory()).memory
    d = decide(_stop("b1", active=True, said="Started it."), asked)
    assert d.report == "busy"
    assert decide(_stop("b1"), d.memory).refusal is not None




def test_a_running_task_that_is_not_a_command_is_asked_about_by_what_it_is():
    """Only a shell task carries a command; an MCP task names its server and tool."""
    hook = _stop()
    hook["background_tasks"] = [{"id": "m1", "type": "MCP task", "status": "running",
                                 "description": "try the face", "server": "plugin:raigolmi",
                                 "tool": "try_face"}]
    d = decide(hook, Memory())
    assert "m1: try the face (MCP task, `plugin:raigolmi try_face`)" in d.refusal
