# RaiGolmi

A computer you build by talking to an AI.

It starts with nothing on it. No desktop, no editor, no tools. You tell the agent what you
want and it builds it: the desktop you work in, the tools your project needs, the project
itself. Don't like something? Ask for it different, or throw it out and start over. The
agent can do anything on this machine, because the machine is all it can touch.

Everything is made of three pieces, and you can swap any of them whenever you like:

- a **face** is your desktop: the look, the apps, the editor;
- a **body** is a project, in its own container;
- a **toolbelt** is the language and test tools an agent opens a project with.

When something breaks, an agent fixes it. You can share what you build, and grab what other
people have shared, from the catalog.

## Two ways to run it

**In a window on Windows.** The machine runs in a VM inside one app window. This is the easy
way, and it's what `build.bat` sets up.

**On its own computer (bare metal).** The same machine, booted directly on real hardware.
`build-disk.bat` builds just the disk for this.

### On Windows

You need Windows 10 or 11, 16 GB of RAM (the VM takes 8), a few GB of disk that grows as you
use it (up to 60), and a Claude account for Claude Code.

```
git clone <this repository>
cd <the clone>
build.bat
```

It checks what it needs (the Windows Hypervisor Platform, .NET 8, MSYS2 with QEMU and a
patched virglrenderer it compiles there, WSL with Ubuntu and podman) and asks before
installing anything missing. Say no and it tells you what to install yourself. If Ubuntu
can't look up the image registry, it points Ubuntu's DNS at 1.1.1.1 and sets
`generateResolvConf = false` in its `/etc/wsl.conf`, leaving the rest of that file alone. Then it builds the app and the disk. The first disk takes about 11
minutes.

When it's done there's an `RaiGolmi` shortcut next to `build.bat`. Run it from there or drag
it wherever you want.

Running `build.bat` again rebuilds the app. It only builds a new disk if you say yes, since
your machine lives on the old one.

### On bare metal

Run `build-disk.bat` and pick a disk:

- **raw**: write it straight to a drive (Rufus, `dd`) and boot from that drive;
- **installer ISO**: put it on a USB stick; it installs onto the first disk it finds and
  **wipes it**;
- **qcow2**: for running it in a VM of your own. This is the same file the Windows app
  boots, so it asks before replacing one that's there.

It only needs WSL with Ubuntu and podman, and the same DNS fix as above. The disk ends up in
`disk\`. A raw disk is 60 GB from the start; the qcow2 grows to that as it's used. On Linux,
run `host/ci/build-local.sh` with `TYPE=raw` or `TYPE=anaconda-iso` instead; it needs sudo
and writes to `~/raigolmi-build` (set `OUT` to change it).

## Using it

The first time, the terminal at the bottom asks for your Claude Code token (run `claude
setup-token` somewhere and paste it in with a right-click). Then it asks you to sign in to
GitHub, which is only needed for uploading to the catalog. Ctrl+C skips it.

### The edges

The screen's edges are how you get around. Each has a small tab. Move your mouse onto a tab
and it slides open. Move away and it closes.

- **Left: the drawer.** Pick a face and a body here. Click something to choose it, and
  click it again to drop it. Greyed-out items tell you why you can't pick them. At the
  bottom:
  - **Don't drive my current face** stops agents from clicking and typing on your screen;
  - **Catalog** opens the catalog.
- **Bottom: the AI terminal**, where the agents live. Move up out of it to close it.
- **Top: the history**, everything the agents have done.

Keyboard, if you'd rather: tap **Super** for the drawer, and **Super + `** for the terminal.
Both can be changed in the settings.

### The AI terminal

Each agent has its own tab along the bottom bar. Click a tab to switch to it.

- **`machine`** is the one to ask for anything about the machine itself, like a new face,
  a toolbelt, or a change to how things work. It's always there.
- **One tab per body** you've picked, working on that project.
- **`⚙`** is the manager. It shows up by itself when something breaks and fixes it. You
  don't need to do anything.

A **●** on a tab means it wants you: it's finished, or it's asking you something. Just type
your answer in that tab. When an agent needs permission for something, a menu pops up in
its tab with yes, no, and "always".

The **×** on a tab closes it. For the machine tab and a body's tab, that gives you a fresh
one with a clean slate.

Drag to select text (it's copied right away), and right-click to paste.

### The history

Move your mouse onto the top tab to see what's been happening: tabs finishing, agents
asking you things, builds, restarts, and anything that went wrong. The tab lights up when
there's something you haven't seen. When something goes wrong that's yours to deal with, it
pops open on its own for a few seconds. Entries stick around for a week.

### The catalog

Open it from the drawer. It lists faces, bodies and toolbelts, plus your settings and the
docs the agents keep. Turn on **Server** to see what other people have shared, then
**Download** and **Install**. Things you made have an **Upload** button, which shows you
exactly which files will go before anything is sent.

Every key, colour and size is in the **Settings** document here. Edit and save, and it
changes right away.

### Files and clipboard (Windows)

Copy and paste work between Windows and the machine. Drop a file on the window and it shows
up in `~/Transfer`. Put a file in `~/Transfer/out` and it lands in `Downloads\RaiGolmi`.
**Ctrl + Alt + R** redraws the window if it ever looks off.
