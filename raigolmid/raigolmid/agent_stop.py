"""Whether an agent whose turn is ending is still working.

Claude Code's `Stop` hook fires when a turn ends, but a turn can end with background commands
running, and an agent may be waiting on one. Its answer is what decides, so a turn that ends
with a running command it has not been asked about is refused once, and the agent names the
ones it is waiting on. The tab stays busy while any of those runs; each one finishing wakes
the agent and the stop is decided again. A command it is not waiting on (a server left up for
the user) is asked about once and never keeps the tab.

A turn also ends only once every document the agent read and can change is declared current or
brought up to date (`declare_documents`): the conversation that read a doc is the one that knows
whether what it learned makes the doc stale. One that is not declared is asked about once per
turn; a turn that answers nothing still ends, and the next turn's end asks again.

`decide` is pure. The hook's input, the documents still undeclared and the tab's memory of what
it has asked are the inputs, and the report, the refusal and the new memory are the outputs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

_WAITING = re.compile(r"^\s*waiting:\s*(.*?)\s*$", re.IGNORECASE | re.MULTILINE)


@dataclass
class Memory:
    """What this tab's stops have already settled, kept in the agent's home."""
    asked: set[str] = field(default_factory=set)
    waiting: set[str] = field(default_factory=set)
    documents: set[str] = field(default_factory=set)    # asked about in this turn's stops

    def to_json(self) -> dict[str, list[str]]:
        return {"asked": sorted(self.asked), "waiting": sorted(self.waiting),
                "documents": sorted(self.documents)}

    @classmethod
    def from_json(cls, data: dict[str, list[str]]) -> Memory:
        # A memory written before documents were asked about has asked about none.
        return cls(asked=set(data["asked"]), waiting=set(data["waiting"]),
                   documents=set(data.get("documents", ())))


@dataclass
class Decision:
    report: Literal["busy", "idle"] | None    # None: refused, and the prompt's busy stands
    refusal: str | None
    memory: Memory


def _running(hook: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {t["id"]: t for t in hook["background_tasks"] if t["status"] == "running"}


# What each kind of task carries besides its description (Claude Code 2.1.283's `Stop` input:
# `command` for a shell task, `agent_type` for a subagent, `server` and `tool` for an MCP task
# or a monitor, `name` for a workflow; an unknown kind carries none of them).
_DETAIL = ("command", "agent_type", "tool", "name")


def _line(task: dict[str, Any]) -> str:
    detail = next((task[key] for key in _DETAIL if key in task), None)
    if "tool" in task and "server" in task:
        detail = f"{task['server']} {task['tool']}"
    shown = f"{task['type']}, `{detail}`" if detail else task["type"]
    return f"  - {task['id']}: {task['description']} ({shown})"


def _ask(unasked: list[dict[str, Any]]) -> str:
    lines = "\n".join(_line(t) for t in unasked)
    return (
        "Your turn is ending with background tasks still running:\n"
        f"{lines}\n"
        "For each, decide whether it will finish and its result still matters to your task. "
        "Stop any you no longer need. Then end your reply with one line, `waiting: <id> ...` "
        "naming the ones you are waiting on, or `waiting: none`. The tab stays busy while one "
        "you name is running, and you are woken when each finishes.")


def _declare(documents: list[str]) -> str:
    lines = "\n".join(f"  - {doc}" for doc in documents)
    return (
        "Your turn is ending with documents you read and have not declared:\n"
        f"{lines}\n"
        "For each, decide whether what you learned or changed this turn makes it stale. Bring "
        "any that is stale up to date, holding it to the purpose and not-here in its header, then "
        "call `declare_documents` with each path and `current` or `updated`.")


def decide(hook: dict[str, Any], memory: Memory,
           undeclared: Sequence[str] = ()) -> Decision:
    running = _running(hook)
    if hook["stop_hook_active"]:
        unasked_documents = [doc for doc in undeclared if doc not in memory.documents]
        answers = _WAITING.findall(hook["last_assistant_message"] or "")
        if answers:
            named = {w.strip("`") for w in answers[-1].replace(",", " ").split()} - {"none"}
            memory = Memory(asked=memory.asked | set(running),
                            waiting=named & set(running), documents=memory.documents)
    else:
        unasked = [t for i, t in running.items() if i not in memory.asked]
        if unasked:
            return Decision(None, _ask(unasked), memory)
        memory = Memory(asked=memory.asked, waiting=memory.waiting & set(running))
        unasked_documents = list(undeclared)
    if unasked_documents:
        return Decision(None, _declare(unasked_documents),
                        Memory(memory.asked, memory.waiting,
                               memory.documents | set(unasked_documents)))
    if hook["stop_hook_active"] and not answers and not set(running) <= memory.asked:
        # Unanswered, so nothing is settled: kept rather than closed, as an interrupt is.
        return Decision("busy" if running else "idle", None, memory)
    return Decision("busy" if memory.waiting & set(running) else "idle", None, memory)
