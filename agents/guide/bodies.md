<!-- purpose: how a body is written: its body.toml, its working copy, and the budget its tab sets
not-here: faces (faces.md), toolbelts (toolbelts.md)
shape: bounded
audited: 2614 2026-10-05
-->
# Writing a body

A body is the project as it would deploy: an image, built from its definition, running the
project's own command. Its working copy is `/work` in the body, the toolbelt, the face and the
body's tab.

## The definition

`bodies/<id>/body.toml`. An unknown key is an error.

```toml
id = "myapi"
name = "My API"
dockerfile = "Dockerfile"          # or image = "python:3.12-slim"; one of the two
working_copy = "~/projects/myapi"  # the project; without it, the definition directory
command = ["python", "-m", "myapi"]
ports = [8000]                     # published while its sandbox is the active one
environment = { MODE = "dev" }

[develop]
[[develop.watch]]
path = "requirements.txt"
action = "rebuild"
```

Also accepted: `target` and `context` (the Docker build's), `runtime`, `shell`, `read_only`.

`[budget]` is the range a regular run of the project stays within with nothing duplicated or
stale, each a size like `"2G"`: `caches` (its build tools' caches — every directory holding
`CACHEDIR.TAG`, as cargo's `target/` does), `output` (the rest of what its git ignores), and
`memory` (the peak working memory of its sandbox). It is a tripwire that keeps the tab on top of
what accumulates, not a ceiling: set it close to what a regular run keeps, after clearing what
is stale. The tab is asked for it when the project first builds or keeps output, and told each
time one is passed; then it checks whether the excess is what the work now regularly needs (and
sets that range) or leftovers (and clears back within it).

```toml
[budget]            # a regular run: this build's caches, the runs it cites, its largest build
caches = "2G"
output = "500M"
memory = "3G"
```
A `dockerfile` is resolved against the working copy when one is set, otherwise against the
definition directory.

The body runs as the owner of its working copy, not as its image's user, so what it writes
in `/work` is the user's. An image whose program needs root at run time (a port under 1024, writes
to its own system paths) has to be written not to.

## A body is git

The working copy is a git repository: tabs commit there as themselves
(`Claude (<tab>) <agent@raigolmi.local>`), and the user reads and merges their work with git.
Make a new body's working copy a repository (`git init`, a first commit) when you create it. Its
`.git/hooks` and `.git/config` are read-only in the toolbelt, the face and every tab.

## Running it

When the user selects it, its own tab opens and works on it; that tab opens its sandbox with
`sandbox_open` and a toolbelt, which builds and starts the body. A page it serves on a `ports`
entry reaches the user's face at `http://<body>.<tab>:<port>` (`myapi.tab-2`), whichever sandbox is
active; the active one's `ports` also reach the host through the door. The face is `face` to
a body. A build's log is in `history`.
