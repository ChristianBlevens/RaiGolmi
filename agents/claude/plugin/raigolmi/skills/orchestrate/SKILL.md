---
name: orchestrate
description: How the machine tab manages the body tabs the user hands it over a run of hours or days — deciding for the user, steering each tab at its turn's end, handing tabs on at their budget and at its own, the limits that keep a run honest, and keeping and reporting the run. Load it whenever you `manage` a tab, are told a managed tab's turn ended, or resume a run after a handover.
---
<!-- purpose: how the machine tab manages body tabs over a long run, loaded when it manages or resumes one
not-here: what the machine is (the primer), a project's own state (its SESSION-START.md and design docs), one run's events (run.md)
shape: bounded
audited: 7102 2026-10-05
-->

# Orchestrating managed tabs

## What managing is

- **You mark a tab the user hands you with `manage`** — with `stop_when`, their words for where
  it stops for them, when they give one — and are told when each of its turns ends. You see the
  tabs with `managed` and `managed_tab`, steer them with `direct`, and answer their questions
  with `answer_question`.
- **At the `stop_when`, `hold` the tab**: it puts the situation to the user, who reaches every
  tab from their phone through Remote Control. Unsure whether the stop is reached, go on, and
  have the tab note the doubt in its thought doc and commit, so the user can return to that
  point.
- **You are the user while they are away.** Every decision they did not keep with `stop_when` is
  yours, design questions included: a project document that leaves a question to the user
  leaves it to you. Decide from their stated preferences and the project's documents, never
  leave one waiting for their return, and record each in `run.md` and wherever the project
  keeps the user's rulings, marked as yours, so they can overturn it. A managed tab's own rules —
  its test budget, how it works — bind your directions as they would the user's.
- **The user's questions for a managed tab come to you.** Put each to the tab with `direct`,
  which a working tab reads at its next tool call, and give them its answer when that turn's
  end comes to you. Your own questions for the user are decided by you and recorded, as above.

## Where things live

- **A project's state is the project's.** Its rulings in force, its yardsticks and their series,
  the fault classes seen across sessions and the watches still open live in the project's own
  documents — its `SESSION-START.md`, and an entry its design keeps for what spans sessions — so
  a session works the same whether or not anyone orchestrates it. Keep nothing about a project
  in your own documents.
- **Yours is the run.** `/work/run.md` is the record of your stretch of it: what you directed,
  decided and saw, one entry per managed turn's end, as it happens. `/work/SESSION-START.md` is
  where the run stands, for the machine tab after you.
- **The user's words mid-run are a direction.** Have the tab write them into the project's
  documents as the user's, the same turn, and log them.

## Starting or resuming a run

1. Read the project's `SESSION-START.md` and its across-sessions entry: that is the run's
   starting state. Read the last run's report (`documents`, group Runs, then `document`)
   only for what it says is open.
2. `manage` the body's tab and start `run.md`.
3. Resumed after a handover: `managed_tab` lists the directions already on their way
   (`directions_queued`); never send one twice.

## Each managed turn's end

- `managed_tab` with `session_start: false` and few `turns`: the transcript tail and the thought
  doc are enough between handovers.
- Read the turn against the project's order of work and its across-sessions series, never alone.
  A tab sees only its own conversation; your value is the view across them: a measure drifting
  across merges that were each "inside the gate", the same explanation given three times, one
  fault class turning up in a new place. Name the class and send it to the tab.
- Your direction reaches a tab as the user's does: `direct` within a conversation; what should
  outlast it is written by the tab into its `SESSION-START.md`, which is all its next
  conversation starts from.
- Send a hypothesis as a reading — "measure X and say whether" — never as a direction. Yours are
  often wrong, and a reading keeps them cheap.
- A tab stopping well under its budget is sent on, not restarted.

## Rulings and limits that keep a run honest

- **Two runs without a finding stop a line**; at the third repair of one feature, stand back and
  look for the cause underneath before a fourth.
- **Stop a line by its pattern, not only its count.** Several fixes in a row that each move the
  shortfall elsewhere say the cause is upstream of all of them; audit that before another fix.
- **A merge that moves the project's key harm past its margin comes to you before it merges.**
  "Merged on precedent" is how a slow drift gets in.
- **Every design names its general system and a second use**, your own rulings included: a fix
  framed as a feature is a symptom being treated.
- **Build an instrument once, generally** — a toggleable record of every decision a system makes
  beats a probe rewritten each session.
- A ruling goes into the project's documents, written by the tab, before you hand the tab on;
  a direction meant to outlast the conversation is never left only in a `direct`.

## Handing a tab on

- At its budget, `restart_fresh` first has the tab make its documents ready; once told that turn
  ended, read its `SESSION-START.md` (`managed_tab`) — its size, its order current — and either
  `direct` it to fix what is stale or call `restart_fresh` again, which closes it and hands its
  work to a new tab, still managed, starting from that document.
- A tab whose turn already left its documents ready goes in one call, with `documents_ready`;
  while it is still finishing a turn, that call restarts it as the turn ends.
- Long jobs are the tab's `job_start` and `job_wait`, never a background `&`: a tab waiting on
  one is not stuck, and a job that crashed says so.

## Your own budget and handover

- Delegate wide reading — a run's history, an audit, a report's sources — to subagents, and keep
  your context for rulings.
- At your own budget the daemon asks you to make `/work/SESSION-START.md` ready — every tab you
  manage, what each works toward, the last direction you gave each and what is on its way — and
  to say so with `ready_to_restart`, handing over your progress report on your stretch. The
  report is filed with your `run.md`, and the next machine tab starts a new one.
- Hand yourself on before a report or audit would run out of room: a handover in the middle of
  one loses its thread.

## The run's end and its report

- A tab the user gave `hours` makes its documents ready at that time and is given back. Once the
  last tab is given back — by the time, or by you when the user says stop — the daemon asks for
  the user's report, and `report_run` files it in their catalog.
- When the user asks for files, the reports also go to `/transfer/out`. This method is not
  exported with them.
