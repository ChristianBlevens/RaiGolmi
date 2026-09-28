# Writing a layer

Every piece of work runs in three layers, and each is a directory under `/definitions` that you
write: a **face** (`faces/<id>/face.toml`), a **toolbelt** (`toolbelts/<id>/toolbelt.toml`) and a
**body** (`bodies/<id>/body.toml`). The machine ships none of them; the user has what an agent
builds.

- `faces.md`: everything the user sees and touches. Only the machine tab edits faces.
- `toolbelts.md`: the tools that act on a project.
- `bodies.md`: the project as it would deploy, and its working copy.

A definition is re-read as soon as it changes, and a mistake in one is reported by `status`
under `definition_errors`, with the reason. Read that before guessing.
