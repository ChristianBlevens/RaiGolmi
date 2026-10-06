---
name: write-a-layer
description: How a face, a toolbelt or a body is written on RaiGolmi — its definition file, what it can reach, how it is checked. Load it before creating, editing or repairing anything under /definitions/faces, /definitions/toolbelts or /definitions/bodies (face.toml, toolbelt.toml, body.toml, a body's Dockerfile, a face's apps or editor).
---

# Writing a layer

The guide is `/guide`, read-only in every tab, and it is the one authority; this skill only
sends you to the part you need.

1. Read `/guide/README.md` for what the three layers are and where each lives.
2. Then the one for the layer in hand, whole, before writing it:
   - `/guide/faces.md` — a face: compositor, editor, apps, what it reaches. Only the machine
     tab edits faces.
   - `/guide/toolbelts.md` — a toolbelt: its packages, Nixery and the flake escape hatch.
   - `/guide/bodies.md` — a body: the project as it deploys, and its working copy.
3. A definition is re-read as soon as it changes, and a mistake in one is reported by `status`
   under `definition_errors`, with the reason. Read that before guessing, and check a change
   by using the layer (open the sandbox, try the face) rather than by reading it back.
4. Keep the layer's `LAYER.md` current: its design and how it is built.
