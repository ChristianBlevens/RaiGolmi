<!-- purpose: how someone reading or changing RaiGolmi's code finds their way and checks a change
not-here: using RaiGolmi (README.md), reporting a security problem (SECURITY.md)
shape: bounded
audited: 3413 2026-10-05
-->
# Working on RaiGolmi

RaiGolmi is a personal project shared as it is. Issues and pull requests are welcome, and are
read by one person, so a small, focused change with its reason goes furthest. Every module
opens with a docstring saying what it is for and why it's built the way it is; start there.

## How it fits together

The machine is a Fedora bootc host running a daemon, `raigolmid`, which runs everything else
in containers. What a user sees is built from three kinds of *layer*, each in its own
container and swappable on its own: a **face** (a desktop: a compositor and its apps), a
**toolbelt** (the tools an agent and an editor use) and a **body** (a project, built and run
as it would deploy). The agents are Claude Code, one per tab, and reach the machine only
through a socket the daemon serves them.

| Where | What |
|---|---|
| `raigolmid/raigolmid/daemon.py` | The daemon's threads and what starts them; `session.py` holds the machine's state and most verbs |
| `definitions.py` | Reading a layer's toml into a face, toolbelt or body |
| `faces.py`, `toolbelts.py`, `instances.py`, `views.py`, `compose.py` | Running each layer: a face's compositor, a toolbelt's Nix closure from Nixery (`flakes.py` for what Nixery can't build), a body's containers, and the view that joins a toolbelt to a body |
| `agents.py`, `credproxy.py`, `credential.py`, `mcp_server.py`, `scopes.py`, `api.py` | The agent tabs: their containers, the credential and the proxy that swaps it in, the tools, and the socket each tab reaches the daemon through (`scopes.py`; `api.py` is the full API the host's own clients use) |
| `channel.py`, `jobs.py`, `stalls.py`, `supervisor.py` | Words and notices into a working tab, a tab's long jobs, the watch on a stalled one, and keeping each tab's agent running |
| `claude_login.py`, `remotecontrol.py` | The claude.ai sign-in and its renewal, and every tab on Remote Control while it is set |
| `intent.py`, `reconcile.py` | What should be running, kept on disk, and bringing the machine back to it |
| `hostimages.py`, `hostsurfaces.py`, `settings.py` | The host's own images and surfaces, and your `settings.toml` |
| `lineout.py`, `machinecopy.py` | The phone alerts, and `rai export` / `rai import` |
| `janitor.py`, `coordinator.py`, `questions.py`, `judge.py` | The janitor tab that repairs the machine, the machine tab managing others, and the questions agents ask you |
| `catalog.py`, `registry.py`, `layerfiles.py` | Sharing layers through [raigolmi-registry](https://github.com/ChristianBlevens/raigolmi-registry) |
| `raigolmid/raigolmid/launcher/` | What runs inside a view and starts processes there for the daemon |
| `raigolmid/rai/`, `ui/` | The `rai` command, and the host's own surfaces: the selector, the AI terminal, the history menu, the catalog window |
| `agents/` | The agent image (`claude/`) and the layer-writing guide every tab reads (`guide/`) |
| `host/` | The host image (`Containerfile`), its firewall, units and CI build |
| `faults.py` | Bugs in RaiGolmi the machine tab hands the keeper outside the VM, and the keeper's answers |
| `windows/` | The Windows app: a .NET 8 launcher that runs QEMU and draws its display; its per-machine state (settings, keys, `disk.txt` naming the disk it boots, `release.txt` and `channel.txt` for the release) goes in `state/`, never committed |
| `keeper/` | The keeper: a Claude Code agent in Docker Desktop beside the VM that repairs what the machine cannot (`keeper.py` its loop, `KEEPER.md` its brief) |
| `*.ps1`, `*.bat` | Installing (`setup`), building (`build*`) and upgrading on Windows |

## Checking a change

The daemon's tests run on Linux with Python 3.14, and run one file at a time for what a
change touches rather than the whole suite:

```
pip install pytest pytest-asyncio pytest-timeout docker mcp==2.2.0 watchfiles brotli cryptography pyyaml
PYTHONPATH=raigolmid:. python -m pytest -c raigolmid/pyproject.toml raigolmid/tests/<the file>
```

The keeper's own tests are `keeper/test_keeper.py`, run the same way.

A test stays only while it guards something; one that only shows a change landed isn't kept.
Most of what matters is seen on a running machine rather than in a test: build a disk with
`build.bat`, or on Linux with `host/ci/build-local.sh`.

Comments say what is true of the code now and why, never what it used to be. Errors are
raised with their reason rather than swallowed or given a default.
