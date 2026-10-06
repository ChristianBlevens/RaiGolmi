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
| `agents.py`, `credproxy.py`, `mcp_server.py`, `api.py` | The agent tabs: their containers, the proxy that swaps in the real credentials, and the tools and socket they reach the daemon through |
| `janitor.py`, `coordinator.py`, `questions.py`, `judge.py` | The janitor tab that repairs the machine, the machine tab managing others, and the questions agents ask you |
| `catalog.py`, `registry.py`, `layerfiles.py` | Sharing layers through [raigolmi-registry](https://github.com/ChristianBlevens/raigolmi-registry) |
| `raigolmid/raigolmid/launcher/` | What runs inside a view and starts processes there for the daemon |
| `raigolmid/rai/`, `ui/` | The `rai` command, and the host's own surfaces: the selector, the AI terminal, the history menu, the catalog window |
| `agents/` | The agent image (`claude/`) and the layer-writing guide every tab reads (`guide/`) |
| `host/` | The host image (`Containerfile`), its firewall, units and CI build |
| `windows/` | The Windows app: a .NET 8 launcher that runs QEMU and draws its display |
| `*.ps1`, `*.bat` | Installing (`setup`), building (`build*`) and upgrading on Windows |

## Checking a change

The daemon's tests run on Linux with Python 3.14, and run one file at a time for what a
change touches rather than the whole suite:

```
pip install pytest pytest-asyncio pytest-timeout docker mcp==2.2.0 watchfiles brotli cryptography pyyaml
PYTHONPATH=raigolmid:. python -m pytest -c raigolmid/pyproject.toml raigolmid/tests/<the file>
```

A test stays only while it guards something; one that only shows a change landed isn't kept.
Most of what matters is seen on a running machine rather than in a test: build a disk with
`build.bat`, or on Linux with `host/ci/build-local.sh`.

Comments say what is true of the code now and why, never what it used to be. Errors are
raised with their reason rather than swallowed or given a default.
