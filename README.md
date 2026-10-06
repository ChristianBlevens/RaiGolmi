<!-- purpose: the public face: what RaiGolmi is and how a user installs, starts and uses it, casual reader first, then the narrated uses, then the reference
not-here: design (ARCHITECTURE.md), settled calls (docs/standing-rulings.md), development state (SESSION-START.md), measurements (docs/findings.md)
shape: bounded
audited: 23901 2026-10-02
-->
# RaiGolmi

RaiGolmi is a Linux distro that comes with nothing. There's no desktop, no apps, no editor,
no theme, nobody's opinion about how your computer should look. What it does have is an AI
agent that can build all of that for you, and change it whenever you want, down to anything
you can describe.

You can use it as a completely personal desktop that you shape by asking for things. You can
use it as a place where AI agents work on your code with you, with their own sandboxes, tools
and procedures. Or you can hand the agents a goal, tell them when to stop, and leave them
working on their own. Whatever the agents change, a small core underneath stays put, so
there's always a way back.

On Windows it runs in a normal app window, with the whole OS inside a VM. It can also go on
its own drive and boot on a real PC.

## Getting started

### Install it (Windows)

You need Windows 10 or 11, 16 GB of RAM (the VM takes 8), some disk space that grows as you
use it (up to 60 GB), and a Claude account.

```
git clone https://github.com/ChristianBlevens/RaiGolmi
cd RaiGolmi
setup.bat
```

(Or download the repository as a ZIP from GitHub, unzip it and double-click `setup.bat`.)

`setup.bat` checks for what it needs and asks once before installing anything that's
missing, then downloads the app and its disk (about 2 GB). When it's done there's a
`RaiGolmi` shortcut next to `setup.bat`. Open it, and close the window when you're done.
Closing it shuts the machine down properly.

To update, run `setup.bat` again. It downloads the new app, and your machine downloads its
new system and **upgrades in place**: your files, layers and the agents' work all stay. It
closes and reopens the app to start on the new system. The old one stays in the boot menu
until the new one has started properly, then it's removed to save space.

### The first start

The terminal at the bottom of the screen asks you for three things, in order:

1. **Your Claude Code token.** Run `claude setup-token` somewhere, then paste it in with a
   right-click. This is the only one you have to give it.
2. **A GitHub sign-in**, so agents can push to your repos and you can share things in the
   catalog.
3. **A claude.ai sign-in**, so you can answer agents from your phone when they're working
   on their own.

Press Ctrl+C to skip either sign-in. They'll be offered again later.

### Getting around

The screen has three edges with a small tab on each. Put your mouse on a tab and it slides
open, and move away to close it.

- **Left, the selector.** Pick which desktop (face) you're in and which project (body) you're
  working on. The catalog opens from here too.
- **Bottom, the AI terminal.** This is where the agents are, one tab each. You'll spend most
  of your time talking to the one called **machine**.
- **Top, the history.** Everything the agents and the machine have done.

Tap **Super** to open the selector, or press **Super + `** for the terminal.

That's it. From here you just ask for what you want.

## Some ways to use it

### Your own desktop, from scratch

You don't have to be a developer for any of this. RaiGolmi works fine as a desktop you build
up yourself.

The first time you boot it there's nothing on the screen but the three tabs. You open the
terminal and tell the machine tab you want a dark tiling desktop with Firefox, a file
manager, and a clock in the corner. It writes the desktop, builds it, and a few minutes
later it shows up in the selector. You pick it and you're in.

The bar is too tall, so you say so, and it fixes it. You'd like the windows to have gaps
between them, so it adds them. A week later you want something completely different for
the weekend. You ask for a second desktop and switch between the two from the selector.
Your first one never changed. If a new one ever comes out broken, you just pick a different
one.

Each desktop is its own thing, and nothing about it is fixed. It can use any
wlroots-based compositor (sway, river, labwc and so on), any apps in nixpkgs,
set up however you like.

### Working on a project with an agent

You've got a Python API you've been meaning to speed up. You ask the machine tab to set it
up as a body. It writes one that builds and runs the project the same way it would deploy,
plus a toolbelt with Python and a language server.

You pick the body in the selector, and a new tab opens for it in the terminal. You tell that
tab the search endpoint is slow. It opens a sandbox, runs the project and its tests, finds
the slow query, fixes it, and opens the page in your desktop's browser so you can see it
working. Then it commits to the project's git repo as itself. You read the commit and merge
it if you like it.

If you've got three projects going, each one gets its own tab, and they can all work at the
same time.

### Letting it run on its own

It's late, and you've got a couple of projects with work left on them. You tell the machine
tab: *"Orchestrate the notes-api and myapi work. Stop if the API design needs my call, and
stop after 4 hours."* Then you go to bed.

The machine tab takes over both tabs, and they get a ◇ in the terminal. When they have
questions, they ask the machine tab instead of you. It decides everything you didn't keep
for yourself, and writes each decision down so you can overturn it. When a tab's conversation gets too long, its work is handed to a new tab that starts
from its `SESSION-START.md`. If the usage limit cuts one off, it resumes once the limit resets.

At 2am the notes-api tab hits the design question you said was yours. It stops and waits
for you, and since every tab is a Remote Control session, if you're up you can answer it from your phone.
Otherwise it's there in the morning. At the 4-hour mark the rest wrap up, and the machine
tab writes its report on the run. You read it in the catalog, under Documents, Runs.

### When something breaks

You're working, and your desktop's editor window crashes, or a container keeps dying. You
don't have to do anything. A **⚙** tab shows up at the bottom. This is the manager. It
reads the logs and the machine's state, works out what went wrong, fixes it, and tells you
in its tab what it found and what it did.

If the fix involves a choice about how you use the machine, it asks you first. It keeps
notes on every failure and what fixed it, so the next time the same thing happens on your
machine it already knows.

### Sharing desktops (and everything else)

You find a desktop someone shared, called `daybook`. You open the catalog, turn on
**Server**, download it, install it, and pick it in the selector. The font isn't quite your
taste, so you ask the machine tab to change it. It's your copy now.

Later you're happy with a desktop you built and want to share it. You press **Upload** on
it. The catalog shows you exactly which files will go, and you can untick any of them. The
upload goes up as a pull request from your GitHub account to the public
[registry](https://github.com/ChristianBlevens/raigolmi-registry). Toolbelts and bodies can
be shared the same way. A body that builds from your own project folder is the one thing
that's refused, so your code doesn't leave the machine by accident. You can only upload
what you made: a downloaded layer you've changed stays yours to keep. Share your project with
git instead.

## Everything else

### Where it came from

I saw [Omarchy](https://omarchy.org) and tried it out, and it didn't feel like it was really
aimed at developers, even though that was kind of the vibe behind it. So I started thinking
about how I'd design an OS that was actually useful for developers. That led to splitting
the machine into layers, with an AI agent able to control all of it except a small core
that stays up no matter what the AI changes.

Then I realized it could also be a harness. So I built it to run in a VM behind one window
on Windows, as an environment an agent can work in. The machine also gives agents the
procedures and guidance they need to be efficient and accurate. Since every desktop starts
from scratch and can be any kind of desktop at all, it seemed like it'd be cool to share
them, and then that any layer should be shareable. Last, I wanted to be able to let an agent
loose in there and have it work fully on its own as a real, reliable way to use it. That
became orchestration from the machine tab.

It started as a fun way to see what a truly non-opinionated distro could look like, if AI
could help anyone build their OS from nothing. It grew into the AI harness I wanted for
myself, and I'm sharing it in case other people find it useful too.

To be upfront about how it was made: my part was almost entirely design and testing. I
decided what it should be and how it should work, used it, and reported what was wrong. The
implementation itself was almost all written by AI. Only around 1% of the code is mine, and
that was minor tweaks. I do not wish to pretend that I have the ability to code this myself in only 2 weeks.

The name comes from *raise* a golem. *Golmi* is the unformed stuff it's made from.

### How it's put together

Everything you work with is made of three **layers**. Each is a folder of plain definition
files under `~/raigolmi/`, and you can swap any of them at any time without touching the
others.

| Layer | What it is | Its file |
|---|---|---|
| **face** | Your whole desktop: a compositor running fullscreen, its apps, and your editor. | `faces/<id>/face.toml` |
| **toolbelt** | The tools that work on a project, like compilers, language servers, debuggers and a shell, as a list of Nix packages. | `toolbelts/<id>/toolbelt.toml` |
| **body** | A project, exactly as it would deploy: its own image and its own command, with no dev tools in it. Its working copy is a git repo. | `bodies/<id>/body.toml` |

When an agent needs to run a project it opens a **sandbox**: the body running as it would
in production, with a toolbelt attached beside it. The toolbelt sees the body's files and
processes but never changes them, so the thing that runs is the thing that ships.

Under the layers is the **host**: an immutable Fedora image with the daemon (`raigolmid`),
the three edges and the AI terminal. Agents can't change it. It's only replaced by an update
or a build, and the previous one stays in the boot menu until the new one has started.

Here's roughly what the machine tab writes for the two examples above. A face:

```toml
# ~/raigolmi/faces/writing/face.toml
id = "writing"

[desktop]
compositor = "sway"         # built from faces/_compositors/sway/Containerfile
config_dir = "desktop/"     # holds sway.conf, the bar, the theme
apps = ["firefox"]          # Nix packages
browser = "firefox"

[editor]
package = "neovim"
config_dir = "editor/"
command = ["foot", "--app-id=raigolmi-editor", "nvim", "--listen", "{socket}",
           "--cmd", "set rtp^={glue}", "-u", "{config}/init.lua"]
```

A body and a toolbelt for it:

```toml
# ~/raigolmi/bodies/myapi/body.toml
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
# ~/raigolmi/toolbelts/python/toolbelt.toml
id = "python"
supports = ["python*"]
capabilities = ["shell"]
packages = ["bashInteractive", "coreutils", "python3", "util-linux", "libcap", "curl",
            "pyright"]
```

The full rules for writing each one are in [`agents/guide/`](agents/guide/). It's the same
guide every agent reads before it writes a layer.

### The tabs, and what each one is for

Every agent is Claude Code, and each one has its own tab in the AI terminal. Each tab sticks
to its own job. If you ask one for something that belongs to another, it tells you which tab
to ask instead. That's on purpose: a tab that stays on one part of the work keeps knowing
that part well.

- **`raigolmi`** isn't an agent. It's a shell with the machine's live status at the top:
  which face, body and toolbelt are selected, the running sandboxes, and which agents are
  busy. The `rai` command works here.
- **`machine`** is for the machine itself. Go here to make or change a face, make a
  toolbelt or a new body, change how the machine works, or hand other tabs over to run on
  their own. It's the tab that makes and changes faces. It can even change the plugins and
  instructions the other agents start with.
- **A body's tab** is for that one project. It works only on that project's code, its
  sandbox and its toolbelt. It opens when you select the body.
- **`⚙` the manager** is for fixing the machine when something breaks. It opens by itself,
  and you don't work in it (see below).

The **`≡`** at the left of the bar lists every tab. A **●** on a tab means it wants you,
because it's finished or it's asking you something. Just type your answer in that tab. A
**◇** means the machine tab is managing it.

Before a question reaches you, it's checked against your **Preferences** document, and if
that already settles it, it's answered from there. Every answer you give is written back into
it, so the same question won't need you twice. A question you leave for 30 minutes gets
"no answer" and the agent decides for itself; a permission left that long is a no.

When an agent needs permission for something, a menu pops up in its tab. You can say yes or
no just this once, for this project, or everywhere. The **×** on a tab archives its
conversation. For `machine` and your selected body, that gives you a fresh tab with a clean
slate. To pick an old conversation back up, type `/resume` in a tab of the same project and
press **Ctrl+A**: every archived one is listed there.

Drag to select text: it's copied right away and stays selected where it is. The wheel
scrolls with it, and a key or a click ends it. Right-click to paste, and press Ctrl+Enter for
a new line.

### The manager, in more detail

The manager takes any failure that gets in the way of using the machine: a container that
dies again after its one automatic restart, a desktop or editor that won't start, one of the
host's own screens failing, the clipboard bridge dropping, or another tab that stops
responding. It also catches a tab that's stuck: one working for ten minutes with nothing in its
conversation moving, or for half an hour without anything in its work changing. It looks at
what the tab was running, leaves a real long wait alone, and otherwise stops the tab's turn and
tells it what went wrong. It watches the disk too: when the machine holds a couple of GB more
than when it last looked, or free space runs low, it works out what grew. The machine's own
leftovers piling up it treats as a failure and fixes; a project's build or run output it points
out to that project's tab, which knows what it still needs. A turn that fails on something waiting won't fix, like a tab
that lost its sign-in, comes to it as well. It also keeps the agents' documents in shape: one that's grown past its size,
names something that no longer exists, or describes a layer that has since changed. The point is that you're never the one who has to take a failure to an AI.

It reads the logs, the journal and the machine's state. It repairs things with the daemon's
own tools (restarting an agent, rebuilding or repairing a sandbox, bringing your desktop
back) or by fixing the layer definitions themselves. Each failure gets its own incident
note, which is closed when it's fixed, and what worked goes into a running list of patterns
for your machine. It only repairs things. Ask it for a feature and it'll send you to the
right tab.

### Working on its own, in more detail

You hand tabs over to the machine tab in plain words. You can give it a point where you want
a tab to stop for you, a number of hours, or both. From then on:

- a managed tab's questions go to the machine tab, which answers them and steers the work.
  It stands in for you on every decision you didn't keep with your stop, design questions
  included, and records each one in the run's record so you can overturn it;
- when a tab's conversation reaches its budget, it's handed to a new tab that starts from
  the project's `SESSION-START.md`, and the machine tab does the same for itself;
- a tab cut off by a usage limit or an API error is resumed;
- every tab, the machine tab and the manager included, is a Claude Code Remote Control
  session named after its project, so you can follow any of them from your phone;
- a tab that reaches your stop is held for you, and typing in the tab takes it back;
- when the time runs out, each tab finishes up and is given back;
- each time the machine tab hands itself over, it files a progress report on that stretch,
  so a run of any length keeps its whole record;
- once the last tab is back, the machine tab writes a report, which you'll find in the
  catalog under Documents, Runs, next to the progress reports.

Every handover, hold and ending shows up in the history.

### The history

Put your mouse on the top tab to see tabs finishing, agents asking you things, handovers,
builds, restarts and anything that went wrong. The tab lights up when there's something you
haven't seen. When something goes wrong that's yours to deal with, it pops open on its own
for a few seconds. Entries stay for a week.

A question answered from your preferences shows the line it came from. If that's not what
you'd have said, pick another choice or type your own answer and press Enter. The agent gets
your answer, and your preferences are updated with it.

### The catalog and settings

Open the catalog from the selector. It lists your faces, bodies and toolbelts, plus
**Documents** you can edit: your settings, your preferences, the permissions you answered
"always" (edit it to take one back), the instructions every tab starts with, each
project's `SESSION-START.md`, each layer's doc, the manager's incidents, and run reports.
**Thoughts** has the agents' thought docs, to read. Turn on **Server** to see what other people have shared. Use
**Download** and **Install** to get something, **Upload** to share something you made, and
**Delete** to remove it.

Every key, colour and size is in the **Settings** document there, along with the keyboard,
the display scale, the model the agents run and their conversation budget. Edit it and save.
Keys and looks change right away, and the model and budget at a tab's next start. If a save
doesn't parse, it's refused and you're told why.

At the top of the selector, **Don't drive my current face** stops agents from clicking and
typing on your screen. With it off, an agent can use your desktop the way you would, once
you've stepped away from the keyboard, and one tab at a time. Agents can take screenshots
either way.

### Files and clipboard on Windows

Copy and paste works both ways between Windows and the machine. Drop a file on the window
and it shows up in `~/Transfer`. Put a file in `~/Transfer/out` and it lands in
`Downloads\RaiGolmi`. Every agent tab has the same folder at `/transfer`: it reads what you
drop there, and sends you a file by writing it to `/transfer/out`.
**Ctrl + Alt + R** redraws the window if it ever looks wrong.

### What `setup.bat` installs

It checks for and offers to install:

- the Windows Hypervisor Platform;
- the .NET 8 Desktop Runtime;
- Windows' OpenSSH client, which the clipboard, `~/Transfer` and updates go through;
- MSYS2;
- a patched QEMU and virglrenderer, downloaded from
  [raigolmi-packages](https://github.com/ChristianBlevens/raigolmi-packages), because the
  stock ones can't show the boot screen or give disk space back.

If you say no, it prints the commands to install each one yourself. The app and disk come
from [the `raigolmi` package](https://github.com/ChristianBlevens/RaiGolmi/pkgs/container/raigolmi)
on GitHub's container registry. For a fresh disk, delete `disk\raigolmi.qcow2` and run it
again.

### Building it yourself

`build.bat` builds the same app and disk from your checkout instead of downloading them, for
when you change the code. On top of what `setup.bat` installs, it needs the .NET 8 SDK and
WSL with Ubuntu and podman, and offers to install them. The first build takes about 11
minutes. Running it again rebuilds the app and upgrades your disk in place, the same way an
update does. If Ubuntu can't look up the image registry, the build alone uses 1.1.1.1 and
8.8.8.8; your Ubuntu's own DNS settings are never changed. `build-launcher.bat` rebuilds just
the app.

A release is published by running the `publish` workflow on GitHub (Actions → publish → Run
workflow); `setup.bat` then offers it to everyone.

### Other ways to run it

`build-disk.bat` builds only the disk, and only needs WSL with Ubuntu and podman. Pick one:

- **raw**: write it straight to a drive (Rufus, `dd`) and boot a PC from it. It's 60 GB
  from the start.
- **installer ISO**: put it on a USB stick. It asks which disk to install onto. **This
  installer is untested**: nobody has booted one yet.
- **qcow2**: for a VM of your own. It's the same disk the Windows app boots.

If the disk you pick already exists, it builds an upgrade for the machine you installed from
it instead, and tells you how to apply it there. On Linux, run `host/ci/build-local.sh` with
`TYPE=qcow2` (the default), `raw` or `anaconda-iso`. It needs sudo and podman, and writes to
`~/raigolmi-build` unless you set `OUT`.

### Good to know

- A face's first start builds its image and takes a few minutes, with a blank screen. After
  that, switching faces takes under a second.
- There's no XWayland, so X11-only programs don't run in a face. WebKit-based browsers don't
  work either. Firefox does, and so does Chromium with a few flags.
- The clipboard between Windows and the machine carries text only.
- Agents commit as `Claude (<tab>)`, and merging is up to you.

### How it keeps you safe

- The whole machine runs in a VM, or on its own hardware, so nothing outside it is at risk.
- The host is read-only at runtime, and the bare host is always there to fall back to.
- Each agent runs in its own container, with no capabilities and no Docker socket. It sees
  its own work, the layer definitions, `/transfer`, the layer-writing guide, and a control
  socket for its own tab, and nothing else.
- Containers can't reach the host except through the daemon's credential proxy. Your Claude
  token and sign-ins never go into a tab. Tabs hold placeholders, and the proxy swaps in the
  real ones on the way out, with GitHub's only ever sent to GitHub.

### The `rai` command

Run it from the `raigolmi` tab's shell.

| Command | What it does |
|---|---|
| `rai status [--follow]` | What's selected and running |
| `rai list` | The layers, and whether each can be picked |
| `rai select` / `rai deselect` | Change what's selected |
| `rai terminal` | A shell in a sandbox's toolbelt |
| `rai exec` | Run a command in a sandbox's toolbelt |
| `rai rebuild` / `rai repair` | Rebuild a body, or tear a sandbox down and rebuild it |
| `rai events [-f]` | The event log, read from disk if the daemon is down; `-f` follows it |
| `rai ai restart <tab>` / `rai ai kill <tab>` | Restart a tab's agent, or stop it |
| `rai diagnose` | Bundle the logs and state into one file you can send |
| `rai credential --set`, `rai registry-token --login`, `rai claude-login --login` | Redo the three first-start steps (`--api-key` with `--set` takes a Console API key instead) |

### What it's made of

- **Host:** Fedora 44 as a bootc image (upgrades and rollback through `bootc`), sway, foot
  and tmux.
- **Daemon:** `raigolmid`, in Python 3.14, running as a systemd user service. It talks to
  the agents over MCP.
- **Layers:** Docker and Compose. Toolbelts and a face's apps are Nix closures from Nixery,
  with a generated flake for anything Nixery can't build. Faces are wlroots compositors, and
  sway is the one that's been built so far.
- **Windows app:** a .NET 8 launcher that runs QEMU from MSYS2 with virgl GPU acceleration
  and draws it with D3D11.
- **Agents:** Claude Code, every one of them.

## License

MIT
