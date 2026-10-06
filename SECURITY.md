<!-- purpose: how to report a security problem in RaiGolmi, and what counts as one
not-here: what RaiGolmi protects and doesn't (README.md § How it keeps you safe), ordinary bugs (GitHub issues)
shape: bounded
audited: 1392 2026-10-05
-->
# Security

RaiGolmi is a personal project shared as it is, kept by one person for their own use. Reports
are welcome, but there's no team, no response deadline and no promise of a fix behind it.

## What counts

Anything that breaks what the README's *How it keeps you safe* promises, for example:

- a container reaching what listens only on your Windows PC itself, or the machine's host
  outside what it serves them (the local network is reachable by design);
- a tab getting hold of a real credential rather than its placeholder, or the credential
  proxy sending a credential somewhere other than its own hosts;
- a download from the catalog writing outside its own layer folder;
- the Windows app or `setup.bat` running something it didn't check.

What the README already says agents can do (full control inside their containers, using your
GitHub sign-in, layers from the catalog running their author's code) isn't a vulnerability
on its own.

## How to report one

There's no private channel. Open an ordinary issue with what you did, what happened, and the
release you run (`%LOCALAPPDATA%\RaiGolmi\release.txt`) or the commit you built from. A fix
may or may not come from here; a fork or a pull request that fixes it is just as welcome.
