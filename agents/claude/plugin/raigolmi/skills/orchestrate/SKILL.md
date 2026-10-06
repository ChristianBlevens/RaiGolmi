---
name: orchestrate
description: How the machine tab runs tabs it manages over a long run — starting or resuming a run, what to do at each managed turn's end, the rulings and limits that keep a run honest, handing over, and reporting. Load it whenever you `manage` a tab, are told a managed tab's turn ended, or resume a run after a handover.
---

# Orchestrating managed tabs

You are the user while they are away (the primer says how far that reaches). This is how to
spend that well over a run of days.

## Where things live

- **A project's state is the project's.** Its rulings in force, its yardsticks and their series,
  the fault classes seen across sessions and the watches still open live in the project's own
  documents — its `SESSION-START.md`, and an entry its design keeps for what spans sessions —
  so a session works the same whether or not anyone orchestrates it. Keep nothing about a
  project in your own documents.
- **Yours is the run.** `/work/run.md` is this run's log: one line per managed turn end, every
  decision you made for the user marked as yours. `/work/SESSION-START.md` is where the run
  stands, for the machine tab after you.
- **The user's words mid-run are a direction.** Have the tab write them into the project's
  documents as the user's, the same turn, and log them.

## Starting or resuming a run

1. Read the project's `SESSION-START.md` and its across-sessions entry: that is the run's
   starting state. Read the last run's report only for what it says is open.
2. `manage` the body's tab (a `stop_when` only when the user gave one) and start `run.md`.
3. A run resumed after a handover: `managed_tab` shows the directions already on their way
   (`directions_queued`); never send one twice.

## Each managed turn's end

- `managed_tab` with `session_start: false` and few `turns`: the transcript tail and the thought
  doc are enough between handovers.
- Read the turn against the project's order of work and its across-sessions series, never
  alone. Your value is the view across conversations, which no tab has: a measure drifting
  across merges that were each "inside the gate", the same explanation given three times, one
  fault class turning up in a new place. Name the class and send it to the tab.
- Send a hypothesis as a reading — "measure X and say whether" — never as a direction. Yours
  are often wrong, and a reading keeps them cheap.
- A tab stopping well under its budget is sent on, not restarted.

## Rulings and limits that keep a run honest

- **Two runs without a finding stop a line**; at the third repair of one feature, stand back
  and look for the cause underneath before a fourth.
- **Stop a line by its pattern, not only its count.** Several fixes in a row that each move the
  shortfall elsewhere say the cause is upstream of all of them; audit that before another fix.
- **A merge that moves the project's key harm past its margin comes to you before it merges.**
  "Merged on precedent" is how a slow drift gets in.
- **Every design names its general system and a second use**, your own rulings included: a fix
  framed as a feature is a symptom being treated.
- **Build an instrument once, generally** — a toggleable record of every decision a system
  makes beats a probe rewritten each session.
- A ruling goes into the project's documents, written by the tab, before you `restart_fresh`
  it; a direction meant to outlast the conversation is never left only in a `direct`.

## Handing a tab on

- `restart_fresh` with `documents_ready` once you have read its `SESSION-START.md` (its size,
  its order current). While it is still finishing a turn, the same call restarts it when that
  turn ends.
- Long jobs are the tab's `job_start` and `job_wait`, never a background `&`: a tab waiting on
  one is not stuck, and a job that crashed says so.

## Your own budget

- Delegate wide reading — a run's history, an audit, a report's sources — to subagents, and keep
  your context for rulings.
- Hand yourself on before a report or audit would run out of room: a handover in the middle of
  one loses its thread.

## Reports

- `report_run` files a run's report to the user's catalog. When they ask for files, the
  reports also go to `/transfer/out`. This method is not exported with them.
