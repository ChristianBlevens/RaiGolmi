# Writing a face

A face is a complete desktop: a compositor nested fullscreen in the host's, with the apps the
user uses. It is one container, and it is their whole screen apart from three thin edges that
belong to the host.

## The definition

`faces/<id>/face.toml`. An unknown key is an error.

```toml
id = "writing"
name = "Writing"                       # defaults to id
requires_toolbelt_capabilities = []    # checked against the focused toolbelt's `capabilities`

[desktop]
compositor = "sway"                    # built from faces/_compositors/<compositor>/
config_dir = "desktop/"                # mounted read-only at /etc/face
apps = ["firefox"]                     # Nix package names, from Nixery
browser = "firefox"                    # what `show_url` runs; without it show_url refuses

[editor]
package = "neovim"
config_dir = "editor/"                 # mounted read-only at /etc/face-editor
command = ["foot", "--app-id=raigolmi-editor", "nvim", "--listen", "{socket}",
           "--cmd", "set rtp^={glue}", "-u", "{config}/init.lua"]
open = ["nvim", "--server", "{socket}", "--remote-send",
        '<C-\><C-N><Cmd>lua require("raigolmi").show("{request}")<CR>']
```

A face needs `[desktop]`, `[editor]` or both; one with no `[desktop]` has no container.

**`<compositor>.conf`.** The compositor image starts `sway -c /etc/face/sway.conf`, so
`config_dir` must hold a file named after the compositor — `sway.conf` for `sway`. A face with
`compositor = "sway"` and no `sway.conf` in its `config_dir` is refused with exactly that
reason. With no `config_dir` the image's own plain config is used. The directory replaces
`/etc/face` whole, so a theme, a bar config and includes go beside the `.conf`.

**Compositors.** A compositor is an image built on the machine from
`faces/_compositors/<compositor>/Containerfile`, beside the faces; the machine ships none, so
the first face to name one writes it. Editing that directory rebuilds the image, and an
upload of the face carries it. A new compositor must be wlroots-based (below). The image:

- runs as uid 1000 (the daemon passes `--user`), with `HOME` set in the image, since
  `--user` reads no `/etc/passwd`;
- starts its compositor with the config `/etc/face/<compositor>.conf`, and carries a plain
  one there for a face with no `config_dir`;
- installs the GL drivers by name (on Fedora `mesa-dri-drivers mesa-libEGL mesa-libgbm`,
  with `install_weak_deps=False` they are otherwise dropped), a UTF-8 locale, a font and a
  cursor theme — without the last the face draws no pointer and reads as taking no input;
- carries `sh`, which a startup `exec` waits on the apps with (below).

```dockerfile
FROM docker.io/library/fedora:42
RUN dnf install -y --setopt=install_weak_deps=False sway foot \
        mesa-dri-drivers mesa-libEGL mesa-libgbm glibc-langpack-en \
        dejavu-sans-mono-fonts dejavu-sans-fonts adwaita-cursor-theme && dnf clean all
RUN useradd --uid 1000 --create-home face
USER 1000:1000
COPY sway.conf /etc/face/sway.conf
ENV HOME=/home/face XDG_CACHE_HOME=/home/face/.cache LANG=C.UTF-8 \
    XCURSOR_THEME=Adwaita XCURSOR_SIZE=24
ENTRYPOINT ["sway", "-c", "/etc/face/sway.conf"]
```

**Apps.** `python3`, the editor's `package` and `apps` become one Nix closure whose `bin` is
first on `PATH` (`/.raigolmid/apps/bin`). Find a name with `search_packages` before using it; Nixery names one it cannot build.
There is no XWayland: an X11-only program does not run.

⚠ **The apps are not there when the compositor starts.** Its `/nix/store` is filled a moment
later, so a program its config `exec`s at startup would find nothing — python3 and the fonts
included. `$XDG_RUNTIME_DIR/raigolmid-apps-ready` exists once they are; wait on it with the
image's own `sh`: `exec sh -c 'until [ -e "$XDG_RUNTIME_DIR/raigolmid-apps-ready" ]; do sleep
0.1; done; exec foot my-app'`. A key binding runs later and needs no wait.

**The editor window.** With `[editor]`, the face runs `command` once it starts: any editor,
wrapped in `foot` if it is a terminal one. Quitting it closes the window until the face
starts again. `open` is what `show_file` runs in the face; without it `show_file` refuses. The
daemon fills in `{socket}` (a path the editor may listen on; if `open` names it and it is
gone, the window is taken as closed), `{config}` (`/etc/face-editor`), `{glue}` (the nvim
helper below), `{path}` and `{line}`, and `{request}`: a JSON file holding `path` and `line`,
for an editor whose command quoting a path could break (nvim's keys). Any other `{…}` is text.

A language server runs in the focused toolbelt, rooted at `/work`, when the editor starts it
as `python3 -m rai lsp <server command>`. For nvim the glue does that and restarts servers when
the focused view changes; name them in `init.lua`:

```lua
require('raigolmi').setup({
  pyright = { cmd = {'pyright-langserver', '--stdio'}, filetypes = {'python'} },
})
```

**Terminals.** `foot python3 -m rai terminal` opens a shell in the focused toolbelt; bind it
in the `.conf` (`bindsym $mod+Return exec foot python3 -m rai terminal`).

**The pointer is an arrow everywhere**: all text is selectable, so the text I-beam says
nothing. foot draws it over any program that does not track the mouse; such a program
prints `\e]22;default\e\\` (OSC 22) once as it starts, as `rai terminal` does.

A face on the user's screen keeps what it started with. A changed definition takes effect when
the face next starts: have them switch away and back, or try it off their screen (below).

## What costs the user: every frame crosses to Windows

The machine's GPU is a virtual one (virgl): GL calls go to the user's real GPU through QEMU, and a
face is handed the render node, so its compositor renders with GL and advertises
`zwp_linux_dmabuf_v1`. A face whose compositor finds no render node renders in software.
wlroots compositors are what has run here; Hyprland (aquamarine) needs dmabuf, which is
there, but no Hyprland face has been started.

Every drawn frame still travels from the guest to the window on Windows, so a face that
animates constantly costs the whole machine, the user's typing included. Keep a face still when
nothing is happening: no infinite CSS animations or `requestAnimationFrame` loops, no
decorative motion.

Browsers: Firefox runs. Chromium needs `--ozone-platform=wayland --no-sandbox --disable-gpu`.
WebKit browsers die (their sandbox cannot create a namespace here).

A face's first start builds its image and closure, which can take minutes with the user's
screen blank; after that a switch is under a second.

## The host's edges and keys

Three edges are the host's, each a 120×24 px tab that opens on hover: the **left** edge's
middle (the drawer: faces, bodies, the catalog), the **bottom** centre (the AI terminal, which
slides up over the lower part of the screen), and the **top** centre (the history menu). A face
is not told where they are, so put nothing the user needs under those three strips.

The host keeps a bare **Super** tap (the drawer) and **Super+`** (the AI terminal); every other
`Super+<key>` is the face's. Do not bind those two.

## Copy and paste

The machine's one convention: **selecting text copies it, and right-click pastes**, with
ctrl+shift+c and ctrl+shift+v as well; ctrl+c is never copy. The machine carries every copy
between the user's face, the host and Windows, and sets both the clipboard and the primary
selection each time, so a terminal whose right-click pastes the primary pastes the last copy from
anywhere. For foot: `selection-target=both`, and under `[mouse-bindings]`
`select-extend=none` and `primary-paste=BTN_MIDDLE BTN_RIGHT`.
An app that takes the mouse itself (an editor's `mouse=a`) takes this away from the user.

## What a face reaches

- **The focused sandbox**: `/work` read-write, the body's filesystem read-only at `/body`, and
  the toolbelt's closure in `/nix/store`. Toolbelt programs are reached only through `rai
  terminal` and `rai lsp`, which run them in the toolbelt; they are not on the face's `PATH`.
- **Every sandbox by name**: a page at `http://<body>.<tab>:<port>` (`myapi.tab-2`), any port,
  focused or not; a body reaches the face as `face`. `rai terminal <body>@<tab>` opens a shell
  in any sandbox's toolbelt; with no name, the focused one's.
- **The user's things**: `$HOME` (`/home/face`) is the user's, shared by every face, and
  outlives them. Their Windows transfer folder is `~/Transfer`: what they drop from Windows
  lands there, and a file written to `~/Transfer/out` goes to their Windows downloads. Each
  face's apps keep their own config and state (`$XDG_CONFIG_HOME`, `$XDG_STATE_HOME`, under
  `~/.faces/<id>/`), so two faces' versions of one app never share settings; the machine tab
  starts a new face's from an existing one's with
  `seed_face_settings(face="<id>", source="<id>")`. Everything else under `$HOME` is shared.
- **The machine**: `python3 -m rai status`, `rai list` and `rai events` answer in a face, from
  the face's own socket (`/run/raigolmid/raigolmid.sock`). Of what changes it, the face does
  what the user does themselves and nothing more:
  - `rai ask [--tab <tab>] <words>` — the user's words as an agent tab's next message, headed
    *From the user's face*: the tab named, else the one they view, else the machine tab.
    Follow it with `rai ai --show` so they see the answer.
  - `rai select face|body <id>`, `rai deselect face|body` — the user's selection, as the drawer.
  - `rai exec <sandbox> <cmd…>` — one command in a sandbox, for a face that shows its result.
  A face tried off the user's screen is refused all three.
- **The AI terminal**: `python3 -m rai ai --show` brings it out (for a button or a key in the
  face). Only the host puts it away.

Nothing else of the host is there.

## Trying a face off the user's screen

The machine tab runs a face without touching the user's: `try_face(face="<id>")` starts it
headless at their screen's size, with its editor window; `screenshot(trial=true)` shows it, and
`face_input(action, …, trial=true)` types (`type`, `key` like `ctrl+s`) and points (`move`,
`click` with `x`, `y`). `stop_trial()` removes it. One trial at a time; a new one replaces the
old. A trial has no sandbox (no `/work`, `/body` or toolbelt), none of the user's things, and
asking it for the AI terminal answers "not shown". Try a face this way before they see it.

## Showing the user things on their face

Any tab: `show_file(path, line)` opens a `/work` file in the user's editor when your sandbox is
the one their face shows; `show_url(url)` opens it in the face's `browser`; `screenshot()` is
what they see; `face_input` drives it, waiting until they have left the keyboard and pointer
alone for 2.5 s, and refused outright when they have turned on "don't drive my current face" in
the drawer.
