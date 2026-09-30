# RaiGolmi

A computer you build by talking to an AI.

RaiGolmi is an operating system for developers and the agents that work for them. It starts
with **nothing** on it: no desktop, no editor, no language tools. You tell an agent what you
want and it builds it: the desktop you work in, the tools your project needs, the project
itself. You can change any piece whenever you like, or throw it away and start over. A small
core always stays up, whatever an agent does to the rest. The agent can do anything on the
machine because the machine is all it can touch.

On Windows it runs as an ordinary app window, with a whole OS inside a VM, so it doubles as
a **harness for AI agents**: a sealed environment where an agent can build, run, test and
ship a project, with the procedures and documents that keep its work efficient and accurate.
You can let it work fully on its own for hours.

The name: *raise* a golem, and *golmi* is the unformed substance it is made from.

## Why it exists

It started with [Omarchy](https://omarchy.org). Omarchy is sold on a developer vibe, but it
isn't really aimed at developers. RaiGolmi grew out of the question of how you'd design an
OS that is truly useful for them:

1. **Layers, and an AI that controls all of them except the core.** The machine is split
   into parts an agent can rewrite freely, around a core it cannot touch. That core stays up
   however badly a change goes, so a broken desktop is one click away from gone.
2. **A harness, not just a distro.** Running it in a VM behind one Windows window makes it a
   place for an agent to work in. The machine hands its agents their procedures and guidance:
   a primer, a session-start document, a thought doc, a layer-authoring guide.
3. **Non-opinionated to the root.** No face (desktop) comes with the machine, and a face has
   no limit on what kind of desktop it is. So faces are worth sharing, and then so is every
   other layer. Hence the catalog.
4. **Fully autonomous as a real mode, not a demo.** You can hand the agents a goal and a time
   limit and walk away. The machine tab orchestrates the rest.

It began as a fun test of what a truly non-opinionated distro could look like: AI helping
anyone build their OS from scratch, with literally nothing in it by default. It grew into a
better AI harness for its author, and it's shared in case others find it useful too.

## The pieces

Every piece of work runs in three **layers**. Each one is a directory of plain definition
files under `~/raigolmi/`, written by an agent, and each can be swapped independently:

| Layer | What it is | Where it lives |
|---|---|---|
| **face** | Your whole desktop: a compositor nested fullscreen, its apps, the editor. It never runs your project's tools. | `faces/<id>/face.toml` |
| **toolbelt** | The tools that act on a project (compilers, language servers, debuggers, a shell), as a list of Nix packages. It sees the body's files as its root and never changes the body. | `toolbelts/<id>/toolbelt.toml` |
| **body** | The project exactly as it would deploy: its own image and its own command, with no dev tools baked in. Its working copy is a git repo. | `bodies/<id>/body.toml` |

Underneath is the **host**: an immutable Fedora image with the daemon (`raigolmid`), three
screen edges, and the AI terminal. Agents can't change it. It's replaced only by building a
new image, and the previous one stays in the boot menu.

The **agents** are all Claude Code, each in its own tab:

- **`machine`** is always there. It builds faces, toolbelts and new bodies, and changes how
  the machine works. It's the only tab that edits faces.
- **One tab per body** works on that project. It opens a sandbox (the body plus a toolbelt),
  builds it, runs it, tests it, reads its logs, and shows you a file in your editor or a page
  in your browser. It commits to the project's git repo as itself, and you merge.
- **`⚙` the manager** appears on its own when something breaks and fixes it. You never have
  to diagnose the machine yourself.

Every tab keeps to its own work. Ask one for something outside it, and it tells you which
tab to ask.

## Ways to run it

| How | Build it with | What you get |
|---|---|---|
| **A window on Windows** (the easy way) | `build.bat` | `RaiGolmi.exe` boots the machine in QEMU in one window, with clipboard and files both ways |
| **Bare metal** | `build-disk.bat`: *raw* or *installer ISO* | The same machine booted directly on a PC |
| **Your own VM** | `build-disk.bat`: *qcow2* | The same disk the Windows app boots |
| **From Linux** | `host/ci/build-local.sh` with `TYPE=qcow2` (default), `raw` or `anaconda-iso` | The same disks, written to `~/raigolmi-build` (set `OUT` to change it); needs sudo and podman |

### On Windows

You need Windows 10 or 11, 16 GB of RAM (the VM takes 8 GB and every logical processor), a
few GB of disk that grows as you use it (up to 60), and a Claude account.

```
git clone https://github.com/ChristianBlevens/RaiGolmi
cd RaiGolmi
build.bat
```

It checks for what it needs and asks once before installing anything that's missing. Say no
and it prints the commands to install each piece yourself. It needs:

- the Windows Hypervisor Platform;
- the .NET 8 SDK;
- MSYS2;
- a patched QEMU and virglrenderer, downloaded from
  [raigolmi-packages](https://github.com/ChristianBlevens/raigolmi-packages): the stock ones
  can't show the boot screen or shrink the disk image;
- WSL with Ubuntu and podman.

If Ubuntu can't look up the image registry, it points Ubuntu's DNS at 1.1.1.1 and sets
`generateResolvConf = false` in its `/etc/wsl.conf`, leaving the rest of that file alone.
The first disk takes about 11 minutes.

When it's done there's a `RaiGolmi` shortcut next to `build.bat`. The window opens where and
as large as it last closed, and closing it shuts the machine down cleanly.

Running `build.bat` again rebuilds the app and **upgrades your disk in place**. The new
system goes on, and your files, layers and the agents' work stay. It closes and reopens the
app to start on it, and the old system stays in the boot menu if you need to go back. For a
fresh disk, delete `disk\raigolmi.qcow2` first. `build-launcher.bat` rebuilds only the app.

### On bare metal

Run `build-disk.bat` and pick a disk:

- **raw**: write it straight to a drive (Rufus, `dd`) and boot from that drive;
- **installer ISO**: put it on a USB stick. It installs onto the first disk it finds and
  **wipes it**;
- **qcow2**: for a VM of your own.

It needs only WSL with Ubuntu and podman, plus the same DNS fix. The disk ends up in `disk\`.
A raw disk is 60 GB from the start. If the disk you pick already exists, it builds an upgrade
for the machine you installed from it instead, and tells you how to apply it there.

## Ways to use it

- **From nothing.** A new machine has empty `~/raigolmi/{faces,toolbelts,bodies}`. Ask the
  machine tab for the desktop you want, and it's yours in one selection. A face that comes
  back broken costs one click to step away from.
- **Hands on, one project at a time.** Pick a body in the selector, and its tab opens and
  works on it with you. You watch and steer in the AI terminal, and review its commits with
  git.
- **Several projects at once.** Every selected body has its own tab, and they work in the
  background at the same time.
- **Unattended.** Hand tabs to the machine tab with a goal, a point to stop at, and hours to
  spend. It answers their questions, steers them, and starts a fresh conversation for any tab
  whose context fills up, itself included. A tab cut off by a usage limit or an API error is
  resumed. When a tab reaches your stop, it's held for you as a Claude Code **Remote Control**
  session, so you can answer from your phone. At the end the machine tab writes a report.
- **Shared.** Upload a face, toolbelt or body to the public
  [registry](https://github.com/ChristianBlevens/raigolmi-registry) from the catalog, and
  download what others have shared. A body built from your own project is never uploaded.

## Examples

### 1. An empty machine to a first desktop

In the `machine` tab:

> I want a sway desktop with firefox, and neovim as the editor, in a dark theme.

The agent writes the compositor's image and the face:

```
~/raigolmi/faces/_compositors/sway/Containerfile   # Fedora 44 + sway, GL drivers, fonts, cursor
~/raigolmi/faces/writing/face.toml
~/raigolmi/faces/writing/desktop/sway.conf
~/raigolmi/faces/writing/editor/init.lua
```

```toml
# faces/writing/face.toml
id = "writing"

[desktop]
compositor = "sway"
config_dir = "desktop/"
apps = ["firefox"]          # Nix packages
browser = "firefox"

[editor]
package = "neovim"
config_dir = "editor/"
command = ["foot", "--app-id=raigolmi-editor", "nvim", "--listen", "{socket}",
           "--cmd", "set rtp^={glue}", "-u", "{config}/init.lua"]
```

It shows up in the selector under Faces. The first start builds its image, which takes a
few minutes. After that, switching faces takes under a second. Don't like it? Ask for it
different, or pick another.

### 2. A project, with the tools it needs

> Make a body for ~/projects/myapi. It's a Python web API on port 8000.

```toml
# bodies/myapi/body.toml
id = "myapi"
dockerfile = "Dockerfile"
working_copy = "~/projects/myapi"
command = ["python", "-m", "myapi"]
ports = [8000]

[[develop.watch]]
path = "requirements.txt"
action = "rebuild"
```

```toml
# toolbelts/python/toolbelt.toml
id = "python"
supports = ["python*"]
capabilities = ["shell"]
packages = ["bashInteractive", "coreutils", "python3", "util-linux", "libcap", "curl",
            "pyright"]
```

Select `myapi`, and its tab opens. The agent opens a sandbox with the `python` toolbelt,
builds and runs the body, and runs the tests. When it wants you to look, it opens the page
at `http://myapi.<tab>:8000` in your face's browser. Go-to-definition on a dependency in your
editor opens exactly the bytes the body runs.

### 3. Let it run overnight

> Orchestrate the myapi and notes-api work. Stop when the API design needs my call, and stop
> after 4 hours.

The machine tab takes over both tabs, which are marked ◇ in the AI terminal. The history
records each handover, with its stop and its end time. Whatever they ask goes to the machine
tab, not to you. If a tab reaches your stop, it's held, and you can answer it from your phone
through Remote Control. When the time runs out, each tab finishes up and is given back. The
machine tab's report of the whole run is then in the catalog, under **Documents → Runs**.

### 4. Share it

Open the catalog from the selector and click **Upload** on your face. It shows you the exact
files that will go, and you can untick any of them. Someone else turns on **Server**, then
clicks **Download** and **Install**. The registry already carries a face, `daybook`, and a
toolbelt, `minimal`.

## Using it

### First start

While the machine builds its own screens, it shows a start-up screen. Then the terminal at
the bottom asks, in order:

1. **Your Claude Code token.** Run `claude setup-token` somewhere and paste the token in
   with a right-click. This is the only one that's required.
2. **A GitHub sign-in** (a one-time code at github.com). Agents use it for git, `gh` and the
   GitHub API, and the catalog uses it to upload. Agents never see the real token: the
   daemon's proxy swaps it in on GitHub's hosts only.
3. **A claude.ai sign-in**, used only for Remote Control of held tabs.

Ctrl+C skips either sign-in, and it's offered again later.

### The edges

The screen's edges are how you get around. Each has a small tab: move your mouse onto it and
it slides open, and move away to close it.

- **Left: the selector.** Pick a face and a body here. Click something to choose it, and
  click it again to drop it. Greyed-out items tell you why you can't pick them. At the
  bottom:
  - **Don't drive my current face** stops agents from clicking and typing on your screen;
  - **Catalog** opens the catalog.
- **Bottom: the AI terminal**, where the agents live. Move up out of it to close it.
- **Top: the history**, everything the agents have done.

From the keyboard, tap **Super** for the selector and press **Super + `** for the terminal.
You can change both in the settings.

### The AI terminal

The bar along the bottom holds, in order:

- **`≡`**, a menu of every tab;
- **`raigolmi`**, a shell with the machine's live status above it;
- the manager, when there is one;
- `machine`;
- your selected body's tab, then the others.

Click a tab to switch to it.

- **●** means a tab wants you: it's finished, or it's asking you something. Type your answer
  in that tab.
- **◇** means the machine tab is managing it.
- When an agent needs permission, a menu pops up in its tab: yes or no, just this once,
  always for this project, or always everywhere.
- The **×** on a tab archives its conversation. For `machine` and your selected body, that
  gives you a fresh tab with a clean slate.

Drag to select text (it's copied right away), right-click to paste, and use **Ctrl+Enter**
for a new line.

### The history

Move your mouse onto the top tab to see what's been happening:

- tabs finishing, and agents asking you things;
- handovers, held tabs, and runs ending;
- builds, restarts, and anything that went wrong.

The tab lights up when there's something you haven't seen. When something goes wrong that's
yours to deal with, it pops open on its own for a few seconds. Entries stick around for a
week.

### The catalog

Open it from the selector. It lists:

- faces, bodies and toolbelts;
- your settings;
- the documents the agents keep: their thought docs, the machine's and each layer's docs,
  the manager's incidents, and run reports.

Turn on **Server** to see what other people have shared, then **Download** and **Install**.
Things you made have an **Upload** button, and anything installed can be deleted.

Every key, colour and size is in the **Settings** document here. Edit and save, and the
change takes effect right away. A save that doesn't parse is refused with the reason.

### Files and clipboard (Windows)

Copy and paste work between Windows and the machine. Drop a file on the window and it shows
up in `~/Transfer`. Put a file in `~/Transfer/out` and it lands in `Downloads\RaiGolmi`.
**Ctrl + Alt + R** redraws the window if it ever looks off.

### The `rai` command

The same machine, from a shell (the `raigolmi` tab in the AI terminal has one):

| Command | What it does |
|---|---|
| `rai status [--follow]` | What's selected and running |
| `rai list` | The layers, and whether each can be picked |
| `rai select` / `rai deselect` | Change what's selected |
| `rai terminal` | Open a shell in a sandbox's toolbelt |
| `rai exec` | Run a command in a sandbox's toolbelt |
| `rai rebuild` / `rai repair` | Rebuild a body, or tear a sandbox down and rebuild it |
| `rai events -f` | Follow the event log, even with the daemon down |
| `rai diagnose` | Bundle logs and state into one file to send |
| `rai credential --set`, `rai registry-token --login`, `rai claude-login --login` | Redo the three first-start steps |

## How it keeps you safe

- **The machine runs in a VM** (or on its own hardware), so nothing outside it is at risk.
- **The host is immutable.** Nothing at runtime writes to it, and the bare host is always
  there to recover from.
- **Each agent runs in its own container**, with no capabilities and no Docker socket. It
  sees only its work, the layer definitions, and a control socket scoped to its own tab.
- **Containers can't reach the host**, apart from the daemon's credential proxy. Your Claude
  token and sign-ins never enter a tab: tabs hold placeholders, and the proxy swaps in the
  real values.
- **Agents commit as themselves** (`Claude (<tab>)`), and you decide what to merge.

## What it's made of

- **Host:** Fedora 44 bootc (upgrades and rollback by `bootc`), sway, foot and tmux.
- **Daemon:** `raigolmid`, in Python 3.14. It runs as a systemd user service and speaks MCP
  to the agents.
- **Layers:** Docker and Compose run them. Toolbelts and a face's apps are Nix closures from
  Nixery, with a generated flake when Nixery can't build one. Faces are wlroots compositors,
  with sway the one built so far.
- **Windows app:** a .NET 8 launcher that drives QEMU from MSYS2 with virgl GPU acceleration
  and draws its frames through D3D11.
- **Agents:** Claude Code, every one.

## License

MIT
