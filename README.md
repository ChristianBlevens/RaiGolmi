<!-- purpose: the public face: what RaiGolmi is and how a user installs, starts and uses it. Lead use first (AI agents developing your projects, attended or on their own), then the desktop as the second use, then the reference
not-here: design, settled calls, development state and measurements (development notes, kept outside the repository)
shape: bounded
audited: 32895 2026-10-08
-->
# RaiGolmi

RaiGolmi is a whole machine for AI agents to develop your projects on, with you or on their
own for hours. Each project gets an agent, a sandbox that runs it exactly as it deploys, the
tools to work on it, and, once you have a desktop, a real screen to test it on. You can hand
the agents your projects, tell them where to stop and for how long, and go to bed.

Agents get further when they have a place to work rather than a chat window that can only
suggest what you should go and do. RaiGolmi gives them a machine where they can run anything,
see and click through what they built, and recover when something breaks. The decisions you
keep for yourself wait for you, the ones they make are written down so you can overturn them,
and a small core underneath stays put whatever they change.

On Windows it's a normal app window: setup installs everything it needs, and you never have to
manage the VM the whole OS runs in. That VM is why the agents can be given everything: nothing
outside it is at risk. It can also go on its own drive and boot on a real PC.

Underneath, it's a Linux distro that comes with nothing, so the same agents can also build you
a desktop of your own from scratch, if you want one.

It's a personal project, shared as it is. It works well on the PC it was built on, and it
hasn't met many others yet, so if something goes wrong, see
[When it goes wrong for you](#when-it-goes-wrong-for-you).

## Getting started

### Install it (Windows)

You need 64-bit Windows 10 or 11 on an Intel or AMD (x86-64) PC, not an ARM one, with
virtualization turned on in its BIOS/UEFI (most ship with it on, and setup tells you if yours
doesn't), 16 GB of RAM (the VM takes 8; setup refuses below 12), a graphics card with
Direct3D 11, about 60 GB free for a disk that grows as you use it, and a paid Claude plan
(Pro or Max; Team and Enterprise work too). It doesn't run inside another VM unless that VM
passes virtualization through. Windows asks for administrator rights for each system part
setup installs (up to four times on a fresh PC), and needs a restart the first time it turns
on its hypervisor. Turning that hypervisor on can upset older VirtualBox or VMware versions
and some games' anti-cheat.

```
git clone https://github.com/ChristianBlevens/RaiGolmi
cd RaiGolmi
.\setup.bat
```

(Or download the repository as a ZIP from GitHub, right-click it and choose *Extract All*,
then double-click `setup.bat` in the extracted folder. Run from inside the ZIP, it can't find
the files next to it. If Windows says it protected your PC, choose *More info*, then *Run
anyway*: the scripts aren't signed.)

`setup.bat` checks for what it needs and asks once before installing anything that's
missing, then downloads the app and its disk (about 2.5 GB) and starts it. Next time, open the
`RaiGolmi` shortcut it leaves next to `setup.bat`, and close the window when you're done.
Closing it shuts the machine down properly.

To update, get the newest scripts first (`git pull` in the folder, or download the ZIP
again), then run `setup.bat`. It downloads the new app, and your machine downloads its
new system and **upgrades in place**: your files, layers and the agents' work all stay. It
closes and reopens the app to start on the new system. The old one stays in the boot menu
until the new one has started properly, then it's removed to save space.

### The first start

The screen welcomes you and says what each of the small tabs at its edges holds. Put your
mouse on the bottom one: the terminal there walks you through three sign-ins. Each happens in
a browser, and whatever you need there is put on your clipboard first. In the window on
Windows, that's your browser on Windows. On a computer running RaiGolmi on its own, get one
first: open the selector at the left edge, press **Catalog**, turn on **Server**, search for
`sign-in`, **Download** *Sign-in Browser*, then click it under Faces. It's only Firefox, full
screen, and the bottom tab brings the terminal back over it.

1. **Your Claude Code token.** Claude Code makes it here: its sign-in address is on your
   clipboard. Open it, sign in, and paste the code the page shows back with a right-click.
   This is the only one you have to give.
2. **A GitHub sign-in**, so agents can push to your repos and you can share things in the
   catalog. Its one-time code is on your clipboard: paste it at github.com/login/device.
3. **A claude.ai sign-in**, so you can answer agents from your phone when they're working
   on their own. Its address is on your clipboard: open it, sign in, and paste the code it
   shows back with a right-click.

Press Ctrl+C to skip either sign-in. They'll be offered again later.

### Getting around

The screen has three edges with a small tab on each. Put your mouse on a tab and it slides
open, and move away to close it.

- **Bottom, the AI terminal.** This is where the agents are, one tab each. You'll spend most
  of your time here, starting with the tab called **machine**.
- **Left, the selector.** Pick which project (a *body*: your project, set up to build and run
  exactly as it would deploy) you're working on, and which desktop (a *face*) you're in. The
  catalog opens from here too.
- **Top, the history.** Everything the agents and the machine have done.

Tap **Super** to open the selector, or press **Super + `** for the terminal.

That's it. From here you just ask for what you want.

## Some ways to use it

### Working on a project with an agent

You've got a Python API on GitHub you've been meaning to speed up. You give the machine tab
its link and say you want to work on it. It clones it into a new body, which builds and runs
the project the same way it would deploy, and makes a *toolbelt*, the tools the agent and your
editor work on it with: here, Python and a language server. A brand new project works the same
way: describe it, and it starts one.

A new tab opens for the body in the terminal, already on the project. You tell that tab the
search endpoint is slow. It opens a sandbox, runs the project and its tests, finds the slow
query, fixes it, and opens the page in your desktop's browser so you can see it working. Then
it commits to the project's git repo as itself. You read the commit and merge it if you like
it.

Each project gets one tab, which keeps knowing that project well; Claude Code's own subagents
can split up the work inside it when that helps. If you've got three projects going, each one
gets its own tab, and they can all work at the same time.

### Letting it run on its own

It's late, and you've got a couple of projects with work left on them. You tell the machine
tab: *"Orchestrate the notes-api and myapi work. Stop if the API design needs my call, and
stop after 4 hours."* Then you go to bed.

The machine tab takes over both tabs, and they get a ◇ in the terminal. When they have
questions, they ask the machine tab instead of you. It decides everything you didn't keep
for yourself, and writes each decision down so you can overturn it. When a tab's
conversation gets too long, its work is handed to a new tab that starts from its
`SESSION-START.md`. If the usage limit cuts one off, it resumes once the limit resets. If
something breaks along the way, the janitor fixes it (see below).

At 2am the notes-api tab hits the design question you said was yours. It stops and waits
for you, and since every tab is a Remote Control session (Claude's way of carrying on a
session from the Claude app), if you're up you can answer it from your phone. Otherwise
it's there in the morning. At the 4-hour mark the rest wrap up, and the machine
tab writes its report on the run. You read it in the catalog, under Documents, Runs.

### Testing on a real screen

Agents don't stop at passing tests. Once you have a desktop (a *face*: download one from the
catalog, or ask the machine tab for one), a body's tab can open its project's page in that
desktop's browser, take screenshots to see what you'd see, and, once you've stepped away from
the keyboard, click and type through it the way you would, one tab at a time. Before
showing you a desktop it built, the machine tab tries it off your screen first.

If you'd rather they kept their hands off, turn on **Don't drive my current face** at the top
of the selector. They can still take screenshots.

### When something breaks

A container keeps dying, a tab gets stuck, or your editor window crashes. You don't have to do
anything. A **⚙** tab shows up at the bottom. This is the janitor. It reads the logs and the
machine's state, works out what went wrong, fixes it, and tells you in its tab what it found
and what it did.

If the fix involves a choice about how you use the machine, it asks you first. It keeps
notes on every failure and what fixed it, so the next time the same thing happens on your
machine it already knows.

### Your own desktop, from scratch

RaiGolmi comes with no desktop, no apps, no editor and no theme, so the desktop you work in
is yours to describe, and you don't have to be a developer to do it. It works fine as a
personal desktop you build up by asking.

![A desktop built in RaiGolmi: a day page that keeps what you write, with verbs that open a
browser, an editor, notes, files, a shell or Claude](.github/screenshot.png)

*One desktop someone built from nothing by asking for it, from the catalog. The small tabs at
the edges are RaiGolmi's own: the selector, the AI terminal and the history.*

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
wlroots-based compositor (the program that draws your windows: sway, river, labwc and so
on), any apps in nixpkgs (Nix's huge package collection), set up however you like.

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

### The path to now

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
that was minor tweaks. I do not wish to pretend that I have the ability to code this myself.

The name comes from *raise* a golem. *Golmi* is the unformed stuff it's made from.

### How it's put together

Everything you work with is made of three **layers**. Each is a folder of plain definition
files under `~/raigolmi/`, and you can swap any of them at any time without touching the
others.

| Layer | What it is | Its file |
|---|---|---|
| **body** | A project, exactly as it would deploy: its own image and its own command, with no dev tools in it. Its working copy is a git repo. | `bodies/<id>/body.toml` |
| **toolbelt** | The tools that work on a project, like compilers, language servers, debuggers and a shell, as a list of Nix packages. | `toolbelts/<id>/toolbelt.toml` |
| **face** | Your whole desktop: a compositor running fullscreen, its apps, and your editor. | `faces/<id>/face.toml` |

When an agent needs to run a project it opens a **sandbox**: the body running as it would
in production, with a toolbelt attached beside it. The toolbelt sees the body's files and
processes but never changes them, so the thing that runs is the thing that ships.

Under the layers is the **host**: an immutable Fedora image with the daemon (`raigolmid`),
the three edges and the AI terminal. Agents can't change it. It's only replaced by an update
or a build, and the previous one stays in the boot menu until the new one has started.

Here's roughly what the machine tab writes for the examples above. A body and a toolbelt for
it:

```toml
# ~/raigolmi/bodies/myapi/body.toml
id = "myapi"
dockerfile = "Dockerfile"
working_copy = "project"    # the project itself, in this body's directory
command = ["python", "-m", "myapi"]
ports = [8000]

[[develop.watch]]
path = "requirements.txt"
action = "rebuild"

[budget]                    # set by the project's tab: what a regular run keeps
caches = "4G"
output = "2G"
memory = "3G"
```

```toml
# ~/raigolmi/toolbelts/python/toolbelt.toml
id = "python"
supports = ["python*"]
capabilities = ["shell"]
packages = ["bashInteractive", "coreutils", "python3", "util-linux", "libcap", "curl",
            "pyright"]
```

A face:

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
- **`machine`** is for the machine itself. Go here to start a new body, make a toolbelt, make
  or change a face, change how the machine works, or hand other tabs over to run on their own.
  It can even change the plugins and instructions the other agents start with.
- **A body's tab** is for that one project. It works only on that project's code, its
  sandbox and its toolbelt. It opens when you select the body.
- **`⚙` the janitor** is for fixing the machine when something breaks. It opens by itself,
  and you don't work in it (see below).

The **`≡`** at the left of the bar lists every tab. A **●** on a tab means it wants you,
because it's finished or it's asking you something. Just type your answer in that tab. A
**◇** means the machine tab is managing it.

Before a question reaches you, it's checked against your **Preferences** document, and if
that already settles it, it's answered from there. Every answer you give is written back into
it, so the same question won't need you twice. A question you leave for 30 minutes gets
"no answer" and the agent decides for itself; a permission left that long is a no.

When an agent needs permission for something the machine does for it, a menu pops up in
its tab. You can say yes or no just this once, for this project, or everywhere. Inside its
own container an agent doesn't ask: it runs whatever commands it likes there. The **×** on a
tab archives its conversation. For `machine` and your selected body, that gives you a fresh
tab with a clean slate. To pick an old conversation back up, type `/resume` in a tab of the
same project and press **Ctrl+A**: every archived one is listed there.

Drag to select text: it's copied right away and stays selected where it is. The wheel
scrolls with it, and a key or a click ends it. Right-click to paste, and press Ctrl+Enter for
a new line.

### Working on its own, in more detail

You hand tabs over to the machine tab in plain words. You can give it a point where you want
a tab to stop for you, a number of hours, or both. From then on:

- a managed tab's questions go to the machine tab, which answers them and steers the work.
  It stands in for you on every decision you didn't keep with your stop, design questions
  included, and records each one in the run's record so you can overturn it;
- to ask a managed tab something, ask the machine tab: it passes your question on, the tab
  reads it at its next step even mid-task, and the machine tab brings you the answer;
- when a tab's conversation reaches its budget, it's handed to a new tab that starts from
  the project's `SESSION-START.md`, and the machine tab does the same for itself at its own,
  lower budget, since it wakes only now and then and each wake re-reads its whole context;
- a tab cut off by a usage limit or an API error is resumed;
- every tab, the machine tab and the janitor included, is a Claude Code Remote Control
  session named after its project, so you can follow any of them from your phone;
- a tab that reaches your stop is held for you, and typing in the tab takes it back;
- when the time runs out, each tab finishes up and is given back;
- each time the machine tab hands itself over, it files a progress report on that stretch,
  so a run of any length keeps its whole record;
- once the last tab is back, the machine tab writes a report, which you'll find in the
  catalog under Documents, Runs, next to the progress reports.

Every handover, hold and ending shows up in the history.

### The janitor, in more detail

The janitor takes any failure that gets in the way of using the machine: a container that
dies again after its one automatic restart, a desktop or editor that won't start, one of the
host's own screens failing, the clipboard bridge dropping, or another tab that stops
responding. It also catches a tab that's stuck: one working for ten minutes with nothing in its
conversation moving, or for half an hour without anything in its work changing. It looks at
what the tab was running, leaves a real long wait alone, and otherwise stops the tab's turn and
tells it what went wrong. A turn that fails in a way retrying won't fix, like a tab that lost
its sign-in, comes to it too.

Each project's tab also keeps a budget: how much disk and memory a regular run of its project
takes, with nothing stale. It isn't a limit. When the project goes past it, the tab is told and
checks: either the work really needs more now and the budget moves, or it has piled up
leftovers and cleans them up.

The janitor keeps an eye on the machine's resources too. When the whole disk grows by a couple
of GB or runs low, or the machine runs short of memory, it works out what took it, telling build
caches like Rust's `target/` (which only ever grow) apart from a project's other output. The
machine's own leftovers it treats as a failure and fixes; anything a project holds it puts to
that project's tab, which knows what it still needs. It never deletes a project's files itself.
It also notices a tab that has run past its context budget and tells it to wrap up; nothing is
ever cut off.

It also keeps the agents' documents in shape: one that's grown past its size, names something
that no longer exists, or describes a layer that has since changed. The point is that you're
never the one who has to take a failure to an AI.

It reads the logs, the journal and the machine's state. It repairs things with the daemon's
own tools (restarting an agent, rebuilding or repairing a sandbox, bringing your desktop
back) or by fixing the layer definitions themselves. Each failure gets its own incident
note, which is closed when it's fixed, and what worked goes into a running list of patterns
for your machine. It only repairs things. Ask it for a feature and it'll send you to the
right tab.

### The history

Put your mouse on the top tab to see tabs finishing, agents asking you things, handovers,
builds, restarts and anything that went wrong. The tab lights up when there's something you
haven't seen. When something goes wrong that's yours to deal with, it pops open on its own
for a few seconds. Entries stay for a week.

A question answered from your preferences shows the line it came from. If that's not what
you'd have said, pick another choice or type your own answer and press Enter. The agent gets
your answer, and your preferences are updated with it.

### The catalog and settings

Open the catalog from the selector. It lists your bodies, toolbelts and faces, plus
**Documents** you can edit: your settings, your preferences, the permissions you answered
"always" (edit it to take one back), the instructions every tab starts with, each
project's `SESSION-START.md`, each layer's doc, the janitor's incidents, and run reports.
**Thoughts** has the agents' thought docs, to read. Turn on **Server** to see what other
people have shared. Use **Download** and **Install** to get something, **Upload** to share
something you made, and **Delete** to remove it.

Every key, colour and size is in the **Settings** document there, along with the keyboard,
the display scale, the model the agents run and their conversation budgets. Edit it and save.
Keys and looks change right away, and the model and budget at a tab's next start. If a save
doesn't parse, it's refused and you're told why.

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
  stock ones can't show the boot screen or give disk space back. Before installing them it
  brings MSYS2 itself up to date, since MSYS2 only supports updating everything at once.

If you say no, it prints the commands to install each one yourself. The app and disk come
from [the `raigolmi` package](https://github.com/ChristianBlevens/RaiGolmi/pkgs/container/raigolmi)
on GitHub's container registry. For a fresh disk, delete `disk\raigolmi.qcow2` and run it
again. Keep the RaiGolmi folder out of OneDrive: the disk grows to 60 GB and changes all the
time, so `setup.bat` won't put one there.

### Removing it

There's no uninstaller yet. To take it all off, close the window, then delete:

- the RaiGolmi folder you cloned or unzipped, which holds the disk;
- `%LOCALAPPDATA%\RaiGolmi` (the app's settings and keys) and `Downloads\RaiGolmi` (files the
  machine sent you);
- MSYS2, if nothing else of yours uses it: uninstall it from Windows' settings, or delete
  `C:\msys64`. To keep it, remove just the patched packages' `IgnorePkg` lines from
  `C:\msys64\etc\pacman.conf` and the next `pacman -Syu` puts the stock ones back;
- the .NET 8 Desktop Runtime, from Windows' installed apps, if nothing else needs it;
- Windows' OpenSSH client, from Settings → Optional features, if nothing else needs it;
- the Windows Hypervisor Platform, as administrator:
  `dism /online /disable-feature /featurename:HypervisorPlatform`, then restart.

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

- Agents commit as `Claude (<tab>)`, and merging is up to you.
- Tabs left to work on their own spend your Claude plan's usage while they do.
- A face's first start builds its image and takes a few minutes, with a blank screen. After
  that, switching faces takes under a second.
- There's no XWayland, so X11-only programs don't run in a face. WebKit-based browsers don't
  work either. Firefox does, and so does Chromium with a few flags.
- The clipboard between Windows and the machine carries text only.
- The app isn't signed, and it watches the keyboard so keys like Super reach the machine.
  That combination can make antivirus software suspicious of it. It's built by the
  `publish` workflow from this repository's source, which you can read or build yourself.

### When it goes wrong for you

Open an [issue on GitHub](https://github.com/ChristianBlevens/RaiGolmi/issues) and say what
you did and what happened. If the machine is running, run `rai diagnose` in the `raigolmi`
tab's shell and attach the file it makes, which lands in `Downloads\RaiGolmi`: it bundles the
logs, the journal and state, with your projects' environment values left out. It's worth a
read before you post it, since issues are public. If the window itself
won't start, attach `windows\qemu.log` from the RaiGolmi folder.

### Privacy

RaiGolmi itself sends nothing anywhere. What leaves your PC is what the parts it runs send:

- the agents are Claude Code, so your code and conversations go to Anthropic, and with the
  claude.ai sign-in, sessions show up in your Claude app through Remote Control;
- Claude Code's own usage reporting is left as it ships;
- toolbelts and a face's apps are downloaded from [Nixery](https://nixery.dev), and the images
  faces and bodies build on come from their own registries (Fedora's, Docker Hub and so on);
- updates come from GitHub, and the catalog talks to GitHub when you use it.

### How it keeps you safe

- The whole machine runs in a VM, or on its own hardware, so nothing outside it is at risk.
- The host is read-only at runtime, and the bare host is always there to fall back to.
- Each agent runs in its own container, with no capabilities and no Docker socket. It sees
  its own work, the layer definitions, `/transfer`, the layer-writing guide, and a control
  socket for its own tab, and nothing else.
- Containers can't reach the host except through the daemon's credential proxy, or anything
  that listens only on your Windows PC itself. They do get the whole internet, and with it
  your local network, the way any device on it does: what your PC or router shares there,
  they can reach.
- Your Claude token and sign-ins never go into a tab. Tabs hold placeholders, and the proxy
  swaps in the real ones on the way out, with GitHub's only ever sent to GitHub.

What it doesn't protect you from, so you know what you're handing over:

- Agents have full control inside the VM's containers. They run with Claude Code's
  permission prompts turned off, so the permission menu covers what the machine does for
  them, not every command they run.
- Every tab can use your GitHub sign-in through the proxy, for anything that sign-in can
  do, such as pushing to any of your repos. Skip the sign-in if you'd rather they couldn't.
- A layer you download from the catalog is someone else's code, and it runs on your machine
  with the same reach as one you wrote.
- Tabs left to work on their own make decisions for you and spend your Claude plan's usage
  while they do.

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
| `rai claude-update [--pinned]` | Move every agent to the newest Claude Code, or back to the one this release pins. Idle tabs restart on their conversations right away, and working ones as their turns end |
| `rai diagnose` | Bundle the logs and state into one file you can send |
| `rai credential --set`, `rai registry-token --login`, `rai claude-login --login` | Redo the three first-start steps (`--api-key` with `--set` takes a Console API key instead, billed per token, and Remote Control doesn't work with one) |

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
- **Agents:** Claude Code, every one of them. It comes on the disk, in the agent image, under
  Anthropic's own terms (see [NOTICE](NOTICE)). Each release pins a version that's been run with
  RaiGolmi; `rai claude-update`, or asking the machine tab, takes a newer one without waiting
  for the next release, and a later release that pins past it takes over.

## License

MIT: see [LICENSE](LICENSE). What the published release carries from others is listed in
[NOTICE](NOTICE).
