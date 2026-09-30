# Writing a toolbelt

A toolbelt is the tools that act on a project — compilers, language servers, debuggers, a
shell — as a list of Nix packages. It runs in its own container beside the body, sharing the
body's processes and network and seeing the body's filesystem as its root; it never changes
the body. `exec`, `rai terminal` and a face's language servers run there.

## The definition

`toolbelts/<id>/toolbelt.toml`. An unknown key is an error.

```toml
id = "python"
name = "Python"
supports = ["python*"]        # which bodies' runtimes it fits (globs)
capabilities = ["shell"]      # what a face's `requires_toolbelt_capabilities` checks
packages = ["bashInteractive", "coreutils", "python3", "util-linux", "libcap", "curl",
            "pyright"]
```

`packages` are Nix attribute names; find each with `search_packages` first (a hint: Nixery's answer when the image is pulled is the check). An empty list is
refused, and so is one missing any of `libcap`, `python3`, `coreutils` and `util-linux`, which
the view's launcher runs on.

The daemon writes `toolbelt.lock` beside the definition once the toolbelt runs: the image it
resolved and the store paths. It is reused while `packages` is unchanged. Do not edit it.

## What the view holds of it

The toolbelt image's `/bin` is `/.toolbelt/bin`, **last** on `PATH`: a command finds the body's
program first (its `python` sees its packages) and the toolbelt's only where the body has none.
Each directory its entries
link into relatively (`/bin/go -> ../share/go/bin/go`) sits beside it, so such a link resolves.
The store paths behind them are at their real `/nix/store` paths. With a body, nothing else of
the image is there: the view's root is the body's.

## In the face

The face does not get the toolbelt's `bin`. It reaches the tools through `rai terminal` and
`rai lsp`, which run them in the toolbelt; its `/nix/store` holds the focused toolbelt's closure
so paths a server names resolve.
