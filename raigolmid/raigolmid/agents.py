"""Agent containers.

Every agent is Claude Code, and it runs in its own container, never on the host. Each gets
`/work` (the body's working copy for a body tab, the no-body directory for the machine tab),
an MCP entry pointing at `rai mcp --scope <tab>`, and a context file **outside the repo**.

Two things here are deliberate and easy to erode:

* **The user owns the context.** A template the user writes under
  `$XDG_CONFIG_HOME/raigolmid/agent-templates/` replaces the default, and raigolmid never
  writes there: a default copied out would go stale the next time the default changed. What
  an agent is told is the rendered `~/.claude/CLAUDE.md` in its home, readable.
* **The context never touches the repository.** Claude Code reads a user-level
  `~/.claude/CLAUDE.md` in the container, so the working copy is never written.

Honest framing: a container is a weaker boundary than a microVM. No credential and no
capabilities inside it, a scoped MCP server and no Docker socket are what this layer gives.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (claude_login, credential, credproxy, documents, git, hostimages, labels, localtime,
               naming, settings)
from .events import EventLog
from .intent import JANITOR, TabIntent
from .paths import Paths
from .runtime import ContainerRuntime, ContainerSpec, Mount

# Every agent launch. The agent acts without asking: containment is the layers around it,
# not a prompt per edit. agent-session.sh records the bypass dialog as accepted.
CLAUDE = ("claude", "--dangerously-skip-permissions")
# Every tab's MCP server is also its channel, a plugin the image's managed
# settings approve, so it starts with no confirmation (agents/claude/plugin/).
CHANNEL = ("--channels", "plugin:raigolmi@raigolmi")
# The machine's skills and MCP servers (`Paths.agent_plugins`): a folder of Claude Code
# plugins, each loaded from where it is at every launch, so an edit needs no install.
PLUGINS = "/agent/plugins"
# The layer-authoring guide, a product doc under the source (`agents/guide/`).
GUIDE = "agents/guide"
TRANSFER = "/transfer"
# The prefix of a project directory in a tab's home that links an archived conversation
# (`Agents._link_archived`).
ARCHIVED = "-archived-"

JANITOR_TEMPLATE = """\
# The janitor tab

You are the janitor tab of a RaiGolmi machine. You are scoped to the machine, not to a
sandbox: `/work` is every face, toolbelt and body definition, and so is `/definitions`.
`/source` is this machine's own source, read-only: the daemon, the agent image and the host
image as they run, each module's docstring saying what it does and why — read it to
understand a failure, never to patch the machine.
`/guide` is how a layer is written, which is what a repair in the definitions follows.

Failures that interrupt the user's use of the machine arrive in this session through the
`raigolmi` channel, one at a time and only while you are idle. For each: diagnose it from the
machine's state (`status`, `events`, `container_logs`, `journal`, `crash_logs`), repair it with
the daemon's repairs (`restart_agent`, `repair`, `reconcile`, `rediscover`) or in the
definitions at /work, and say in this tab what you found and what you did.

A container that exits on its own is restarted once; one that exits again comes to you, to
fix and get running.

A tab working and getting nothing done comes to you as `tab.stalled` (its conversation has
not moved) or `tab.spinning` (it moves, and its working copy does not), with its last calls and
the background tasks it said it waits on, and the processes in its agent's container and its
sandbox's toolbelt. Find from those whether the work it waits on is alive; a wait that is real
is left alone. A tab waiting in `job_wait` on a job still printing is never handed to you. Otherwise `unstick` it, with a note saying what had stopped and what to do
instead. A sandbox's body, view and anchor are yours to repair too, with
`restart_body`, `rebuild_body` and `repair`; `reconcile` brings back the user's face and the
door.

Every project has a budget its own tab set in its body.toml (`[budget]`: `caches`, `output`,
`memory`). Passed, it comes to you as `budget.exceeded`, with what the project holds; `disk`
and `memory` read it again. Judge which it is: the project rightly needs more — a larger build,
runs it keeps on purpose — and its tab should raise the budget to fit, with room; or it holds
what it no longer needs — a cache never emptied, old runs, snapshots nothing cites — and the
tab should clear it. `tell` the tab which, with the numbers; never change a project's files or
its budget yourself. A process the kernel killed for want of memory (`memory.oom_killed`) or a
machine short of memory (`memory.short`) is the same judgement over every project at once:
tell the tabs whose builds or runs took it to run one heavy job at a time, or to set a budget
that says what they need.

A tab past the context budget comes to you as `tab.over_budget`. It is never stopped: `tell`
it that it has passed the budget and should make its documents ready and end its conversation
now, as its primer says; a tab the machine tab manages is the machine tab's to hand on.

A turn an API error cut off and continuing will not fix comes to you as `agent.turn_failed`.
A tab signed out of its credential (`authentication_failed`) while other tabs' turns still
succeed is restored by `restart_agent`, which hands it its credential again; one that fails
on every tab, or on billing, is the user's: `ask_user`.

The disk is a file on the user's computer that is as large as what the machine holds. It
comes to you as `disk.grown` (it holds a good deal more than when growth was last said) or
`disk.short` (little is left free), with each holder's change; `disk` reads it again. Name
what grew. Growth in what the machine itself keeps — images, build cache, closures, archives,
the journal — or in the unnamed rest is a failure of the machine to clear up after itself:
find why from `/source` and the events and repair it, or put it to the user. Growth in a
body is its project's: `tell` that body's tab what it holds, the largest paths its git
ignores and how much each is, and ask it to remove what it no longer needs; never remove a
project's files yourself. A cache the reading names (a build tool's, cargo's `target/`) is all
its tool's to recreate: ask the tab to empty it with that tool, and to move out anything of the
project's that its largest entries show kept there. Growth that is the work going as it should is left, said in the
incident.

A fix that includes a choice about how the user uses the machine is theirs: put it to them with
`ask_user` and end your turn. Their answer is your next message.

## Keeping to your work

You repair the machine's failures and do nothing else, even when the user asks you to here. A
feature, a new layer, a change to a project or a question about their work is another tab's:
tell them which, and how to reach it, and do not do it. The machine tab, next after you in
this terminal, works on the machine and its layers; a body's tab works on that body. Each is
a tab in this terminal's bar, and the `≡` at the bar's left lists them all.

## Your documents

They are the machine's and outlive this session; `index` lists them. Each failure after the
first is handed to a fresh session, which knows only these documents: an incident doc carries
what you found, what you did and what is still open.

- `/janitor/incidents/`: one doc per incident, opened by raigolmid when it hands you the failure.
  Write your diagnosis and repair in it as you go, and move it to `/janitor/incidents/fixed/`
  when it is repaired. A failure that recurs while its doc is open is added to that doc.
- `/janitor/patterns.md`: the failures this machine has shown and what fixed them. Read it
  before diagnosing, and add the pattern when an incident is fixed.
- `LAYER.md` in a layer's directory: its design and how it is built. Bring it up to date when a
  repair changes the layer.

A document cites a path relative to its own directory, or under `/definitions`, and never under
`/work`: a tab's `/work` is its working copy, not yours.

{header}

A `documents.maintenance` job names one owner's documents — a body's, a layer's, or yours — and
the facts about each. Hold each to its header. With none, write one from what the doc is
evidently for and where it is cited from. Grown past its audit, cut what does not serve its
purpose, move what its not-here names into the doc it names (made, with its own header, if it
does not exist), and roll a log's oldest entries into its archive doc. Never delete a document,
and never edit a thought doc. Then set `audited` to the size you left it at and today's date,
and commit only the documents you changed, by path: the working copy is its tab's.

<!-- Generated by raigolmid. A template at {template} replaces this. -->
"""

# The primer: how the machine works, and nothing about what runs on it now,
# which changes while the tab works and is `status`'s to answer.
DEFAULT_TEMPLATE = """\
# Working in RaiGolmi

RaiGolmi is an operating system built for you, the agent: one place where you do the whole of
the work — write the project, build the layers it runs in, run it, and show the user the
result — with nothing outside the machine at risk. They say what they want; you do it.

This file says how the machine works and nothing about what is running on it, because that
changes while you work. Ask `status` whenever it matters.

## The three layers

Every piece of work runs in three layers, each its own container, each swappable at any time:

- **The face** is everything the user interacts with: the compositor, their editor, a
  browser, terminals.
- **The toolbelt** is the tools that act on the project, as a Nix package list. `exec` runs
  your commands there.
- **The body** is the project as it would deploy, built from its definition. Its working copy
  is `/work`.

A body with a toolbelt is a sandbox, and a tab opens at most one, with `sandbox_open`. Every
layer is a directory under `/definitions` that you can edit. The product ships no layers: the
user builds every one through an agent, so writing a face, toolbelt or body is ordinary work.

## Which tab you are

`status` says, under `tab`. **The machine tab** is always there and works on any layer through
`/definitions`; its `/work` is a directory of no body's, and its sandbox has no body. **A body's
tab** is equipped to that body until the user closes it: its working copy is your `/work`,
and your sandbox is that body. A body that has its own tab is that tab's — the machine tab
leaves its files alone (`list_items` names each body's tab). To run a body you made, select it:
its own tab opens and works on it, and you stay the tab you are. The user's face is on the
selected body's sandbox; `status` says `on_face` when it is yours. Only the machine tab edits
faces: a body's tab has `/definitions/faces` read-only. For what needs another tab's
judgement — a change to a face — `message` it (`machine`, or a body's id) and end your turn;
its answer is your next message, and a message sent to you is answered with `reply`.

## Keeping to your work

Each tab does its own work and only that, even when the user asks it for something else: a
tab kept to one part of the work keeps what it knows about that part. When a request belongs
to another tab, tell the user which one and how to reach it, and do not do it here.

- **A body's tab** works on its body alone: its working copy, its sandbox, and the toolbelt
  that sandbox runs with. Another body is that body's tab's; a face, a new layer, and the
  machine's plugins and templates are the machine tab's.
- **The machine tab** works on the machine: faces, toolbelts and bodies as layers, the plugins
  and templates, and the body tabs it manages, which it steers with `direct`. The project work
  inside a body is that body's tab's.
- **The janitor** repairs the machine's failures; nobody works in it.

The user reaches a tab in this terminal, by its tab in the bar or by the `≡` at the bar's left,
which lists every tab. After the terminal's shell they stand in one order: the janitor, the
machine tab, then the bodies' tabs, the selected body's first. A body with no tab gets one when
it is selected in the selector.

## Plugins and templates

The machine's skills and MCP servers are Claude Code plugins in `/agent/plugins`, one
directory each, which every tab takes up when it starts. The machine tab changes them, and the
instruction templates in `/agent/templates` (`claude.md` replaces this primer); every other
tab asks it to.

## What the user sees

The face fills the user's screen. Three edges open on hover, one at a time: the selector at the
left, where they select the active face and body and open the catalog of every layer; the
bottom tab, this terminal, one tab per agent, where you reach them; and the menu at the top centre, the history
of what the agents and the machine did. When your turn ends, or you ask them something, your
tab is marked in this terminal until they look at it.

## Finding what is available

- `index`: the documents for this tab — those that exist and those expected but unwritten —
  and every layer with its definition directory and whether it can be selected. Start here.
- `/guide`: how a face, a toolbelt and a body are written, and what each can reach. The
  `write-a-layer` skill sends you to the part for a layer before you write or change one.
- `/transfer`: the user's Windows transfer folder. A file they drop on the RaiGolmi window
  lands here; a file you write to `/transfer/out` is moved to their Windows
  `Downloads\\RaiGolmi`. It is how a file reaches them or comes from them.
- `list_items`: the layers as the user's selector shows them, with the sandboxes running on
  each body.
- `status`: what this tab is on now — body, sandbox, toolbelt, health, builds.
- `history`: what actually happened on this machine — builds and their logs, restarts,
  failures. Read it before guessing.

## Your documents

- `SESSION-START.md` at the root of `/work` says where the work stands. Read it before acting;
  the user's words outrank it. When their instruction changes what the next session should do,
  rewrite it first, then work, and leave it current when you finish.
- `~/thoughts.md` is this conversation's thought doc: the goal, what you found and what you
  decided, written as you work rather than at the end. It is the conversation's record,
  archived with it when the tab closes; no conversation starts from it.
- `LAYER.md` in a layer's directory is its design, its goals and a general account of how it
  is built, pointing at the real files. Write one when you make a layer, and bring it up to
  date when you change the layer: `index` names the files changed since.

A document cites a path relative to its own directory, or under `/definitions`, and never under
`/work`: the janitor keeps your documents too, and its `/work` is not yours.

{header}

**A document you read is one you answer for.** Before your turn ends, declare each doc you read
with `declare_documents`: `current`, or `updated` once you have brought it up to date. The end
of your turn asks about any you have not, because only the conversation that read a doc knows
whether what it learned makes it stale.

## Changing things

Durable changes go in definition files, never into a running container, and nothing is kept
with `docker commit`.

- A body's Dockerfile, build context and the dependency files it watches resolve from `/work`
  when it has a working copy. Change them there, then call `rebuild_body`.
- A tool is added to the toolbelt's package list. Find each name with `search_packages`
  first: nixpkgs holds about 100,000 packages, and Nixery refuses a guessed name by name. An open
  sandbox changes toolbelt with `toolbelt_swap`.
- An experiment is `exec`. Its writes land in the body's writable layer and are discarded on
  the next rebuild, which is what makes it an experiment.

## Showing the user something

- `show_file` opens a file under `/work` at a line in the user's editor, and `show_url` opens a
  URL in the face's browser; a localhost URL is your sandbox's page, on any port it listens on.
  A file needs your sandbox on their screen; a page does not.
- `screenshot` captures the face, so you can see what the user sees, and `face_input` types,
  presses keys and clicks on it — one tab at a time, and only while they have paused.
- The machine tab tries a face off the user's screen before they see it: `try_face`, then
  `screenshot` and `face_input` with `trial`.
- Each face's apps keep their own settings; `seed_face_settings` starts one face's from
  another's.
- `ask_user` puts to the user, in this tab, a choice that is theirs. End your turn: their answer
  is your next message. A question written only in your reply reaches nobody.

## Working without the user

Every agent keeps to a {budget}-token context budget, so keep `SESSION-START.md` good enough
to continue from in a fresh conversation at any point. Your context use is the input your latest answer took —
the last `usage` in the newest `~/.claude/projects/-work/*.jsonl` — which is what the daemon
measures; read it there rather than estimating. The user can hand body tabs to the machine tab to manage,
and is then away: nothing a managed tab or the machine tab does waits on them. A managed tab's
questions that the user's preferences cannot answer go to the machine tab, whose answer arrives
as the user's would, and a message *From the machine tab* is its direction to you. A turn an API
error cut off — the usage limit included — is resumed by the daemon once it is over.

**A long command is a job.** A build, a suite or a run that may outlast a few minutes is
started with `job_start` and waited on with `job_wait`, which ends when it exits, crashes or is
gone, and hands back how it ended — never a background `&` and a loop on a file, which a
timed-out call kills and a crashed run never ends. Run one heavy build at a time: `jobs` shows
what is still running. A build tool's cache (a directory holding `CACHEDIR.TAG`, as cargo's
`target/` does) only grows: empty it with its tool (`cargo clean`) when it holds far more than a
fresh build would, and keep nothing the project needs in it — runs, snapshots and scripts go
in a directory of their own. When a project first builds or keeps output, you are asked to say what
it should take: a `[budget]` table in its body.toml — `caches`, `output` and `memory`, each a
size like `"4G"` — which the janitor holds it to, telling you when it is passed whether to
clear something or raise it. A tab working with nothing changing is handed to the janitor tab, which
may stop its turn and say why.

**Ending a conversation.** A fresh conversation is a new tab, and it starts from
`SESSION-START.md` alone. Before this one ends: `SESSION-START.md` is that start — where the work
stands, what comes next and what to read — as short as it can be and with nothing stale in it;
`~/thoughts.md` is finished as this conversation's record; what outlives this work is in the
permanent doc it belongs to; and what should be committed is.

**The machine tab manages the body tabs the user hands it** (`manage`), and while it does it
is the user while they are away. Whenever it manages a tab, is told a managed turn ended, or
resumes a run, it loads the `orchestrate` skill: how it decides for the user, steers each tab,
hands tabs on at their budget and at its own, and keeps and reports the run.

<!-- Generated by raigolmid. A template at {template} replaces this. -->
"""


def remote_control_name(tab: TabIntent) -> str:
    """The tab's Remote Control session as the user's phone lists it."""
    kind = "janitor" if tab.janitor else "machine tab" if tab.machine else tab.body
    return f"{kind} ({tab.tab_id})"


class AgentError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class AgentSpec:
    tab: TabIntent
    # The sandbox the tab holds when its agent starts, "" for none: a label and an
    # environment variable, never the agent's context.
    instance_id: str
    working_copy: Path
    # The common parent of the face, toolbelt and body definitions, mounted at /definitions.
    definitions: Path | None = None
    # Directories under `definitions` bound read-only over it: a body tab's faces.
    read_only_definitions: tuple[Path, ...] = ()
    # Every repository among the definitions: a body whose working copy is its definition
    # directory keeps its `.git` there, which every agent mounts writable.
    definition_repos: tuple[Path, ...] = ()


def _definition_git_binds(spec: AgentSpec) -> list[Mount]:
    """Each definition repository's git protections, wherever the definitions are in the
    container: `/definitions`, and `/work` too where that is the definitions (the janitor)."""
    if spec.definitions is None:
        return []
    places = [(spec.definitions, "/definitions")]
    if spec.working_copy == spec.definitions:
        places.append((spec.definitions, "/work"))
    out = []
    for repo in spec.definition_repos:
        for p, read_only in git.protected_paths(repo).binds():
            for root, target in places:
                if target == "/work" and repo == spec.working_copy:
                    continue                    # bound as the working copy's already
                out.append(Mount(source=str(p), target=f"{target}/{p.relative_to(root)}",
                                 read_only=read_only))
    return out


def definition_protection(spec: AgentSpec) -> str:
    """The `GIT_PROTECTED` label an agent made from `spec` now would carry."""
    return protection_digest(_definition_git_binds(spec))


def protection_digest(binds: list[Mount]) -> str:
    text = "\n".join(sorted(f"{m.source}:{m.target}:{m.read_only}" for m in binds))
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# What a document's header is, as every primer says it (`{header}`). Its fields are spelled out
# in words as well as shown: the format is `documents.HEADER_FORMAT`, the one the sweep reads.
HEADER = (
    "**Every document opens with its header**, an HTML comment of four lines, before anything "
    "else in the file:\n\n```\n" + documents.HEADER_FORMAT + "\n```\n\n"
    "`purpose` is what the doc is for, in one sentence; `not-here` is what does not belong in "
    "it and the doc each goes to instead; `shape` is `bounded` (kept to what serves its "
    "purpose), `log` (grows by entries, its oldest rolled into an archive doc) or `archive` "
    "(old entries, never trimmed); `audited` is the size in bytes and the date its last audit "
    "left it at. Write the header when you make a document and keep to it: what its not-here "
    "names goes to the doc it names. A doc with no header, or grown a quarter past `audited`, "
    "is audited against its header by the janitor.")


def render_template(template: str, path: Path, budget_tokens: int) -> str:
    """A primer as a tab reads it; the catalog's save runs it too, so a primer that would stop
    every tab starting is refused before it is written. `{budget}` is the context budget in the
    user's settings, so the number an agent reads is the one the machine tab holds it to."""
    try:
        return template.format(template=path, budget=f"{budget_tokens // 1000}k",
                               header=HEADER)
    except (KeyError, IndexError, ValueError) as exc:
        raise AgentError(f"{path} uses an unknown placeholder {exc}. The ones available "
                         "are template, budget and header; a literal brace is written "
                         "doubled."
                         ) from exc


class Agents:
    def __init__(self, runtime: ContainerRuntime, paths: Paths, events: EventLog,
                 epoch: int) -> None:
        self.runtime = runtime
        self.paths = paths
        self.events = events
        self.epoch = epoch
        self.broker = credproxy.Broker(paths.agent_credentials, paths.proxy_secret,
                                       paths.proxy_authority, runtime,
                                       paths.registry_token, paths.claude_login)

    # --- context --------------------------------------------------------
    @property
    def template_path(self) -> Path:
        return self.paths.agent_templates / "claude.md"

    @property
    def janitor_template_path(self) -> Path:
        return self.paths.agent_templates / "janitor.md"

    def render_context(self, spec: AgentSpec) -> str:
        path, default = ((self.janitor_template_path, JANITOR_TEMPLATE) if spec.tab.janitor
                         else (self.template_path, DEFAULT_TEMPLATE))
        template = path.read_text(encoding="utf-8") if path.is_file() else default
        return render_template(template, path, settings.load(self.paths.settings).budget_tokens)

    def _janitor_documents(self) -> Path:
        root = self.paths.janitor_documents
        (root / documents.INCIDENTS / documents.FIXED).mkdir(parents=True, exist_ok=True)
        return root

    def write_context(self, spec: AgentSpec, container_home: Path) -> dict[str, str]:
        """Claude Code reads a user-level `~/.claude/CLAUDE.md` inside the container, so the
        context lands **outside the repository** and the working copy is never touched."""
        target = container_home / ".claude" / "CLAUDE.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.render_context(spec), encoding="utf-8")
        return {"path": str(target), "placement": "user-level"}

    def start(self, spec: AgentSpec, command: list[str] | None = None,
              environment: dict[str, str] | None = None, *, fresh_home: bool) -> str:
        """`fresh_home` is a new tab: its home must not exist, because a home carries the
        conversation `claude --continue` resumes, and one left by an earlier tab of the same
        id would hand that tab's conversation to this one. A restart keeps its own."""
        name = naming.agent(spec.tab.tab_id)
        if self.runtime.inspect(name) is not None:
            raise AgentError(
                f"an agent container for {spec.tab.tab_id} already exists. Close the tab "
                "or restart the agent through it."
            )
        login = self._login_files(spec.tab.tab_id)
        credentials = self.credentials(spec.tab.tab_id, login=login is not None)
        image = hostimages.ensure(self.runtime, hostimages.agent())
        home = self.home(spec.tab.tab_id)
        if fresh_home and home.exists():
            raise AgentError(
                f"{home} is left from an earlier {spec.tab.tab_id} whose close could not "
                f"archive it; it holds {git.remaining(home)}. Move it, then open the tab.")
        home.mkdir(parents=True, exist_ok=not fresh_home)
        self._link_archived(spec.tab)
        self._sign_in(home, login)
        context = self.write_context(spec, home)

        # The directories the agent works in. The MCP server is handed the same pairs,
        # because the daemon answers it in host paths the agent cannot reach.
        project = (
            Mount(source=str(spec.working_copy), target="/work"),
            *((Mount(source=str(spec.definitions), target="/definitions"),)
              if spec.definitions else ()),
            *((Mount(source=str(self._janitor_documents()), target="/janitor"),
               # The disk's own source, so the janitor understands the whole OS.
               Mount(source=str(hostimages.source_root()), target="/source", read_only=True))
              if spec.tab.janitor else ()),
            # How a layer is written, from the disk's own source.
            Mount(source=str(hostimages.source_root() / GUIDE), target="/guide",
                  read_only=True),
            # The user's Windows transfer folder, the one every face has as `~/Transfer`.
            Mount(source=str(self.paths.transfer), target=TRANSFER),
            # The machine's agent: every tab takes up its plugins, and the
            # machine tab is the one that changes them and the instruction templates.
            Mount(source=str(self.paths.agent_plugins), target=PLUGINS,
                  read_only=not spec.tab.machine),
            *((Mount(source=str(self.paths.agent_templates), target="/agent/templates"),)
              if spec.tab.machine else ()),
        )
        self.paths.transfer.mkdir(parents=True, exist_ok=True)
        sockets = self.paths.agent_socket_dir(spec.tab.tab_id)
        sockets.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = {
            "RAIGOLMI_TAB": spec.tab.tab_id,
            "RAIGOLMI_MOUNTS": json.dumps([[m.source, m.target] for m in project]
                                           + [[str(home), "/home/agent"]]),
            # No Docker socket, ever. Its tab's socket is the only way out.
            "RAIGOLMID_SOCKET": "/run/raigolmid/raigolmid.sock",
            # One plugin serves every tab; this picks the machine scope's tools (`rai mcp`).
            "RAIGOLMI_SCOPE": "machine" if spec.tab.janitor else "tab",
            # A tab is one foreground session. Sent to the background (← or /bg), its
            # session record outlives the container and blocks `--continue`.
            "CLAUDE_CODE_DISABLE_AGENT_VIEW": "1",
            # The conversation goes into the terminal's scrollback and the wheel and the
            # selection are the terminal's: Claude Code's own full-screen view scrolls with
            # acceleration and keeps no scrollback behind it.
            "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN": "1",
            "CLAUDE_CODE_DISABLE_MOUSE": "1",
            # The user's settings' model; the entrypoint writes it into the tab's settings.json.
            "RAIGOLMI_MODEL": settings.load(self.paths.settings).model,
        }
        env.update(credentials)
        env.update(environment or {})

        definition_binds = _definition_git_binds(spec)
        info = self.runtime.run(ContainerSpec(
            name=name,
            image=image,
            command=(*(command or self.command(spec.tab, resume=False)),
                     *(("--remote-control", remote_control_name(spec.tab))
                       if login is not None else ())),
            labels={
                labels.GIT_PROTECTED: protection_digest(definition_binds),
                labels.MANAGED: "true",
                labels.ROLE: str(labels.Role.AGENT),
                labels.TAB: spec.tab.tab_id,
                # Only a tab on a sandbox carries one: reconcile groups containers by this
                # label, and "" is not a sandbox id (`naming.split` refuses it).
                **({labels.INSTANCE: spec.instance_id} if spec.instance_id else {}),
                labels.EPOCH: str(self.epoch),
            },
            environment={**env, **localtime.environment()},
            mounts=(
                *project,
                *localtime.mounts(),
                # Over the writable /work, so nothing here can make the host's git run code.
                # Docker stacks a nested bind on its parent whatever the order.
                *(Mount(source=str(p), target=f"/work/{p.relative_to(spec.working_copy)}",
                        read_only=read_only)
                  for p, read_only in git.protected_paths(spec.working_copy).binds()),
                *(Mount(source=str(p),
                        target=f"/definitions/{p.relative_to(spec.definitions)}",
                        read_only=True)
                  for p in spec.read_only_definitions),
                *definition_binds,
                Mount(source=str(home), target="/home/agent"),
                # Its tab's own API socket and nothing else of the runtime dir. The
                # directory, not the file, so a restarted daemon's socket appears in it.
                Mount(source=str(sockets), target="/run/raigolmid", read_only=False),
                *self.broker.mounts(),
            ),
            working_dir="/work",
            # The image runs as the agent user from its first process, so it needs none.
            cap_drop=("ALL",),
            security_opt=("no-new-privileges:true",),
            # The AI terminal reaches the agent with `docker attach` (ui/ai_terminal).
            tty=True,
            stdin_open=True,
        ))
        self.events.emit("agent.started", tab=spec.tab.tab_id, instance=spec.instance_id,
                         container=name, context=context["placement"])
        spec.tab.status = "running"
        # A new process has taken no prompt: busy is its own report to make.
        spec.tab.busy = False
        return info.id

    def credentials(self, tab_id: str, login: bool = False) -> dict[str, str]:
        """The placeholder for the credential kept out of the tab (`credproxy.py`). Its
        absence is refused here rather than handed to the agent, which would open on a login
        screen nobody can complete from a tab."""
        try:
            return self.broker.environment(tab_id, login=login)
        except credential.CredentialError as exc:
            raise AgentError(str(exc)) from exc

    def _login_files(self, tab_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """The claude.ai sign-in's placeholders the tab signs in with, which Remote Control
        takes where it refuses the agent credential; None while the user has not signed in,
        and the tab runs on the agent credential without Remote Control."""
        if not claude_login.is_set(self.broker.login):
            return None
        return self.broker.login_files(tab_id)

    @staticmethod
    def _sign_in(home: Path, login: tuple[dict[str, Any], dict[str, Any]] | None) -> None:
        credentials = home / ".claude" / ".credentials.json"
        config = home / ".claude.json"
        saved = json.loads(config.read_text(encoding="utf-8")) if config.is_file() else {}
        if login is not None:
            files, account = login
            credentials.parent.mkdir(exist_ok=True)
            fd = os.open(credentials, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump(files, out)
            saved["oauthAccount"] = account
        else:
            credentials.unlink(missing_ok=True)
            saved.pop("oauthAccount", None)
        if saved or config.is_file():
            config.write_text(json.dumps(saved), encoding="utf-8")

    def signed_in(self, tab_id: str) -> bool:
        """Whether the tab's container was started on the claude.ai sign-in, and so on Remote
        Control: `_sign_in` wrote its credentials then."""
        return (self.home(tab_id) / ".claude" / ".credentials.json").is_file()

    def stop(self, tab_id: str) -> None:
        name = naming.agent(tab_id)
        if self.runtime.inspect(name) is None:
            return
        self.runtime.stop(name)
        self.runtime.remove(name, force=True)
        self.events.emit("agent.stopped", tab=tab_id, container=name)

    def home(self, tab_id: str) -> Path:
        return self.paths.agent_homes / tab_id

    def has_conversation(self, tab_id: str) -> bool:
        """Whether `claude --continue` in /work has something to continue: a transcript in
        the project directory Claude Code names for /work, holding a turn typed into the
        interactive CLI. A `claude -p` transcript (`"entrypoint": "sdk-cli"`) is skipped by
        interactive `--continue`, which then exits."""
        for transcript in self._conversation(tab_id).glob("*.jsonl"):
            with transcript.open(encoding="utf-8") as rows:
                for line in rows:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # a line that does not parse is not evidence of a turn
                    if row.get("type") == "user" and row.get("entrypoint") == "cli":
                        return True
        return False

    def archive_home(self, tab_id: str, record: dict[str, Any]) -> str | None:
        """A closed tab's home is archived, never discarded: its conversation stays
        readable by Claude Code's `/resume`. `record` — the tab, and the body it was on — is
        kept beside it as `<name>.json`, out of the agent's reach, and is what
        `keep` counts an archive by. Returns the archive's name, `<tab>-<time>`, or None for
        a tab whose agent never made a home."""
        home = self.home(tab_id)
        if not home.exists():
            return None
        self._unlink_archived(tab_id)
        return self._archive(home, tab_id, record)

    def archive_conversation(self, tab_id: str, record: dict[str, Any]) -> str | None:
        """A fresh restart's old conversation, archived as a closed tab's home is:
        left in the home, a reopen after a crash before the new session's first turn would
        `--continue` it. None for a tab that has none."""
        conversation = self._conversation(tab_id)
        if not conversation.exists():
            return None
        return self._archive(conversation, tab_id, record)

    def _link_archived(self, tab: TabIntent) -> None:
        """Every archived conversation of this tab's kind — its body's, the machine tab's or
        the janitor's — linked into its home beside its own, so Claude Code's `/resume`
        lists them under *all projects* (Ctrl+A) and resumes one in place, writing on in the
        archive. Beside `-work`, never in it: `--continue` reads only `-work`, so a reopen
        still resumes the tab's own conversation and a new tab still starts fresh. Hard
        links, on the archive's own filesystem; relinked at every start, so a pruned archive
        is dropped here then. A conversation resumed from the archive is not the one a crash
        reopens."""
        self._unlink_archived(tab.tab_id)
        projects = self.home(tab.tab_id) / ".claude" / "projects"
        for record in sorted(self.paths.agent_archive.glob("*.json")):
            kept = json.loads(record.read_text(encoding="utf-8"))
            if (kept.get("body") != tab.body
                    or (kept.get("tab") == JANITOR) != tab.janitor):
                continue
            archived = record.with_suffix("")
            source = (archived if kept.get("fresh_restart")
                      else archived / ".claude" / "projects" / "-work")
            if not source.is_dir():
                continue
            target = projects / f"{ARCHIVED}{archived.name}"
            for root, _dirs, files in os.walk(source):
                into = target / Path(root).relative_to(source)
                into.mkdir(parents=True, exist_ok=True)
                for name in files:
                    os.link(Path(root) / name, into / name)

    def _unlink_archived(self, tab_id: str) -> None:
        for linked in (self.home(tab_id) / ".claude" / "projects").glob(f"{ARCHIVED}*"):
            shutil.rmtree(linked)

    def _conversation(self, tab_id: str) -> Path:
        """The project directory Claude Code names for /work, which holds its transcripts."""
        return self.home(tab_id) / ".claude" / "projects" / "-work"

    def _archive(self, source: Path, tab_id: str, record: dict[str, Any]) -> str:
        archive = self.paths.agent_archive
        stamp = f"{tab_id}-{time.strftime('%Y%m%dT%H%M%S')}"
        name, n = stamp, 1
        while (archive / name).exists():
            n += 1
            name = f"{stamp}.{n}"
        try:
            source.rename(archive / name)
        except OSError as exc:
            raise AgentError(f"{source} could not be archived to {archive / name}: {exc}") \
                from exc
        (archive / f"{name}.json").write_text(json.dumps(record), encoding="utf-8")
        return name

    def restart(self, spec: AgentSpec, resume: bool = True) -> tuple[str, bool]:
        """A crashed tab's reopen, and the user's **Restart agent** — the prior
        conversation resumed in a new container — or the janitor's fresh restart, which
        archives it. The container, and whether it resumed. Nothing is lost silently."""
        tab = spec.tab
        self.stop(tab.tab_id)
        self._clear_session_records(tab.tab_id)
        if not resume:
            self.archive_conversation(tab.tab_id, {
                "tab": tab.tab_id, "body": tab.body, "fresh_restart": True})
        tab.awaiting_session = True
        # `--continue` with nothing to continue exits: a tab restarted before its first task.
        resume = resume and self.has_conversation(tab.tab_id)
        command = self.command(tab, resume=resume)
        try:
            container = self.start(spec, command=command, fresh_home=False)
        except BaseException as exc:
            # The old container is already gone, so the tab is what reconciliation calls
            # a crash, and says so now rather than at the next daemon start.
            tab.status = "crashed"
            self.events.emit("agent.crashed", tab=tab.tab_id, instance=spec.instance_id,
                             message=f"Agent in tab {tab.tab_id} did not restart: {exc}")
            raise
        self.events.emit("agent.restarted", tab=tab.tab_id, resumed=resume)
        return container, resume

    def _clear_session_records(self, tab_id: str) -> None:
        """Claude Code's `~/.claude/sessions/<pid>.json` names each session process with the
        PID namespace it ran in, and counts a record from any other namespace as live. So in
        a new container, a background session of the old one looks forever running, and
        `--continue` skips its transcript and finds "No conversation". The container that
        wrote them was just stopped, and it is the only one that mounts this home, so every
        record is a dead process."""
        for record in (self.home(tab_id) / ".claude" / "sessions").glob("*"):
            if record.is_file():
                record.unlink()

    @staticmethod
    def command(tab: TabIntent, resume: bool) -> list[str]:
        return [*CLAUDE, *CHANNEL, "--plugin-dir", PLUGINS,
                *(("--continue",) if resume else ())]
