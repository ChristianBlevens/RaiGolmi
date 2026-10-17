<!-- purpose: how a body is written: its body.toml, its working copy, the budget its tab sets, and the principles doc it names
not-here: faces (faces.md), toolbelts (toolbelts.md)
shape: bounded
audited: 2614 2026-10-05
-->
# Writing a body

A body is the project as it would deploy: an image, built from its definition, running the
project's own command. Its working copy is `/work` in the body, the toolbelt, the face and the
body's tab.

## The definition

`bodies/<id>/body.toml`. An unknown key is an error. `description` and `author` (text) are accepted on every layer, and a `thumbnail.png` beside the file; the catalog shows them.

```toml
id = "myapi"
name = "My API"
dockerfile = "Dockerfile"          # or image = "python:3.12-slim"; one of the two
working_copy = "project"           # the project, in this directory; without it, the directory
command = ["python", "-m", "myapi"]
ports = [8000]                     # published while its sandbox is the active one
environment = { MODE = "dev" }

[develop]
[[develop.watch]]
path = "requirements.txt"
action = "rebuild"
```

Also accepted: `target` and `context` (the Docker build's), `runtime`, `shell`, `read_only`.

## Making a body for a project

The user asking to work on a project is asking for its body: a link to a repository, a folder
they put in `/transfer`, or a project that does not exist yet. Make `bodies/<id>/` with the
project inside it at `project/`, and everything else the body needs, in the same turn:

- the project: `git clone <url> /definitions/bodies/<id>/project` (their GitHub sign-in reaches
  their private repositories), a copy of what they dropped, or `git init` and a first commit
  for a new one;
- `body.toml` beside it, with `working_copy = "project"`, built as the project deploys: its own
  Dockerfile where it has one, and otherwise one written into the project and committed;
- the toolbelt its work needs, an existing one when one fits;
- `LAYER.md`, citing the project's files as `project/<path>`, and `project/SESSION-START.md`,
  which carries what the user asked for, since its tab starts from it.

The project's own documents (its README and the rest it came with) are its readers', and are
left as they are: only what an agent writes carries a header.

Then select the body: its tab opens on the project and takes the work from there.

`[budget]` is the range a regular run of the project stays within with nothing duplicated or
stale, each a size like `"2G"`: `caches` (its build tools' caches — every directory holding
`CACHEDIR.TAG`, as cargo's `target/` does), `output` (the rest of what its git ignores), and
`memory` (the peak working memory of its sandbox); and `cpu`, its share of the machine's CPU
over a minute, like `"50%"`, which is the whole machine unless set lower for projects run side
by side. Nothing limits a sandbox: it is a tripwire that keeps the tab on top of
what accumulates, not a ceiling: set it close to what a regular run keeps, after clearing what
is stale. The tab is asked for it when the project first builds or keeps output, and told each
time one is passed; then it checks whether the excess is what the work now regularly needs (and
sets that range) or leftovers (and clears back within it).

```toml
[budget]            # a regular run: this build's caches, the runs it cites, its largest build
caches = "2G"
output = "500M"
memory = "3G"
cpu = "50%"         # only when another project runs beside it
```

`[principles]` names the doc the project's work is judged against, a file in the project. When
it is set, each commit's tab is asked at its turn's end to declare it: what it was for, what it
was judged by, whether that holds; and any work set aside or reverted, with why. A project
that names none is never asked.

```toml
[principles]
doc = "PRINCIPLES.md"
```
A `dockerfile` is resolved inside the build's `context` (default `.`), itself relative to the
working copy when one is set, otherwise to the definition directory.

The body runs as the owner of its working copy, not as its image's user, so what it writes
in `/work` is the user's. An image whose program needs root at run time (a port under 1024, writes
to its own system paths) has to be written not to.

## The GPU and sound

A body has the GPU's render node (`/dev/dri/renderD128`), not its display node: it renders
headless with GL (EGL, surfaceless) through the Mesa drivers its image installs. The GPU is a
virtual one (virgl), so the work lands on the user's real GPU through QEMU.

virgl offers GL 2.1 and ES 2.0 by default: the user's GPU is reached through ANGLE on
Direct3D 11, whose 12 constant buffers per stage leave Mesa 11 uniform blocks, one short of
what it asks before enabling uniform buffers. Mesa can be told to enable them anyway, which
is enough for wgpu (its GLES limits ask for 11):

```
WGPU_BACKEND=gl
MESA_EXTENSION_OVERRIDE=+GL_ARB_uniform_buffer_object
MESA_GLES_VERSION_OVERRIDE=3.0    # 3.1 adds compute shaders
```

With these, wgpu 29 headless picks `virgl (ANGLE (…GPU name…))` and renders and dispatches
compute on the user's GPU. For Bevy keep 3.0: at 3.1 wgpu claims `Rgba16Float` storage
GLES does not have, and Bevy's compute passes fail to compile; at 3.0 Bevy turns them off.
wgpu's GL backend has no view formats, so a render target is created in the format it is
viewed in. A program that asks for 12 uniform blocks is refused its device.

With the launcher's Venus on, a body's Vulkan reaches the user's GPU directly (wgpu takes it
over GL). A window on it needs Mesa 25.2 or later in the body's image: the host has no dma-buf,
and older Venus then offers no swapchain, where 25.2 presents through a CPU copy. Debian
trixie's Mesa is 25.0 — install `mesa-vulkan-drivers` from `trixie-backports` — and Fedora's is
current. Headless Vulkan works on any.

A body has no speakers and no sound server. Its ALSA default output (the machine's
`/etc/asound.conf`, which replaces the image's own) opens for any program built on the image's
libasound and takes the audio into nothing, or as a WAV into the file `RAIGOLMI_AUDIO_FILE`
names: `RAIGOLMI_AUDIO_FILE=/tmp/run.wav ./game`. It takes audio as fast as it is written,
not in real time, so a program paced by its audio runs unpaced, and one that writes whenever
there is room keeps a CPU busy while it plays. A program that speaks only PulseAudio or
PipeWire finds no server; point it at ALSA (SDL: `SDL_AUDIODRIVER=alsa`).

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
