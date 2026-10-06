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


def test_a_document_read_and_not_declared_is_asked_about_once_a_turn():
    """The conversation that read a doc knows whether it is stale; it is asked before its turn
    ends, once — a turn answering nothing still ends — and again at the next turn's end."""
    d = decide(_stop(), Memory(), {"/work/DESIGN.md": ["lines 61-80"]})
    assert d.report is None and "/work/DESIGN.md: lines 61-80" in d.refusal
    assert "declare_documents" in d.refusal and "do not read the rest" in d.refusal
    again = decide(_stop(active=True, said="Done."), d.memory, {"/work/DESIGN.md": ["lines 61-80"]})
    assert (again.report, again.refusal) == ("idle", None)
    assert decide(_stop(), again.memory, {"/work/DESIGN.md": ["lines 61-80"]}).refusal is not None


def test_documents_are_asked_about_after_the_commands_are_answered():
    asked = decide(_stop("b1"), Memory(), {"/work/DESIGN.md": ["lines 61-80"]})
    assert "b1" in asked.refusal and "DESIGN" not in asked.refusal
    d = decide(_stop("b1", active=True, said="waiting: b1"), asked.memory, {"/work/DESIGN.md": ["lines 61-80"]})
    assert "/work/DESIGN.md" in d.refusal
    done = decide(_stop("b1", active=True, said="Declared."), d.memory, {})
    assert done.report == "busy", "the answer before the documents still holds"


def test_a_read_is_undeclared_until_a_declaration_after_it_and_names_what_was_read(tmp_path):
    """Read by the Read tool or by a reading shell command, from the directory the shell is in;
    a write, a listing or a failed call is no read, and a declaration answers for what was read
    before it."""
    from raigolmid.transcript import undeclared_documents
    work = tmp_path / "work"
    (work / "design").mkdir(parents=True)
    for name in ("A.md", "B.md", "C.md", "run.md", "design/09-social.md", "design/10-x.md"):
        (work / name).write_text("text\n", encoding="utf-8")
    w = str(work)

    def call(i, name, given):
        return {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": i, "name": name, "input": given}]}}

    def result(i, error=False):
        return {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": i, "is_error": error}]}}

    declare = "mcp__plugin_raigolmi_raigolmi__declare_documents"
    rows = [call("1", "Read", {"file_path": f"{w}/A.md", "offset": 61, "limit": 20}), result("1"),
            call("2", "Read", {"file_path": f"{w}/main.py"}), result("2"),
            call("3", "Bash", {"command": f"cd {w}; cat B.md; wc -c C.md; ls design"}),
            result("3"),
            call("4", declare, {"documents": [{"path": f"{w}/A.md", "state": "current"},
                                              {"path": f"{w}/B.md", "state": "updated"}]}),
            result("4"),
            call("5", "Bash", {"command": "sed -n 61,80p design/09-social.md && "
                                          "cat > run.md <<'EOF'\ncat A.md\nEOF"}), result("5"),
            call("6", "Read", {"file_path": f"{w}/C.md"}), result("6", error=True),
            call("7", "mcp__plugin_raigolmi_raigolmi__exec",
                 {"cmd": ["bash", "-c", "grep -n '^## ' design/1*.md"], "cwd": w}), result("7"),
            call("8", declare, {"documents": [{"path": f"{w}/B.md", "state": "current"}]}),
            result("8", error=True),
            # A pattern or a script is no file, unless an option gave it and the operand is one.
            call("9", "Bash", {"command": "ls | grep A.md; grep -e C.md B.md; awk '/x/' run.md"}),
            result("9")]
    assert undeclared_documents(rows, tmp_path) == {
        f"{w}/design/09-social.md": ["`sed -n 61,80p design/09-social.md`"],
        f"{w}/design/10-x.md": ["`grep -n '^## ' design/1*.md`"],
        f"{w}/B.md": ["`grep -e C.md B.md`"], f"{w}/run.md": ["`awk '/x/' run.md`"]}
    assert undeclared_documents(rows[:2], tmp_path) == {f"{w}/A.md": ["lines 61-80"]}
