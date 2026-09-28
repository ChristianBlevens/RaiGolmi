# The agent image, based on the Claude Code reference dev container.
#
# What it is *not*: it has no Docker socket, no access to the host, and no path to any
# instance but its own. Everything it can do to the machine goes through the scoped MCP
# server, which is `rai mcp --scope <tab>` talking to raigolmid over one Unix socket.
#
# Honest framing: a container is a weaker boundary than a microVM. No credential (the
# proxy in raigolmid holds it), no capabilities, a scoped MCP server and no Docker socket are
# what this layer gives; `ContainerRuntime` keeps a stronger runtime available later.
#
# raigolmid builds it on the machine from the shipped sources (`hostimages.agent`), with the
# repository root as context, so every COPY below is repo-relative.
FROM docker.io/library/node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c

ARG USERNAME=agent
ARG UID=1000
ARG GID=1000

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git gnupg jq less \
        procps python3 python3-pip ripgrep \
    && rm -rf /var/lib/apt/lists/*

# GitHub's CLI, as the user's GitHub account through the credential proxy (`GH_TOKEN` is a
# placeholder). The version and checksum are `host/gh/Containerfile`'s.
ARG GH_VERSION=2.101.0
ARG GH_SHA256=9bca2d1c16825f109907a23307628a2f0698fbf99662b73a5cf0b020293072b8
RUN curl -fsSL -o /tmp/gh.tgz \
        "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" \
    && echo "${GH_SHA256}  /tmp/gh.tgz" | sha256sum -c - \
    && tar -xzf /tmp/gh.tgz -C /usr/local/bin --strip-components=2 "gh_${GH_VERSION}_linux_amd64/bin/gh" \
    && rm /tmp/gh.tgz && gh --version

# The pinned base image already owns 1000: `node:22-bookworm-slim` ships a `node` user and
# group at that id. 1000 is not negotiable here — it has to match the host user across the
# /work bind mount — so the occupant is renamed and renumbered rather than duplicated.
# Renaming fails loudly if a future base image stops shipping `node`.
RUN groupmod -g "${GID}" -n "${USERNAME}" node \
    && usermod -u "${UID}" -g "${GID}" -l "${USERNAME}" -d "/home/${USERNAME}" -m node

# Pinned beside `mcp` (raigolmid/pyproject.toml): the channel rests on how this version speaks
# the handshake era, so a new one is taken deliberately: the image is rebuilt on the machine
# whenever raigolmid changes, and an unpinned install takes whatever is newest that day.
RUN npm install -g @anthropic-ai/claude-code@2.1.283

# `rai` is how the agent reaches raigolmid, and the only way it reaches anything. Exactly what
# the install packages (`pyproject.toml`'s `packages`), because the image's digest is every file
# copied here, and copying the trees whole would rebuild it for an edit to a test.
COPY raigolmid/pyproject.toml /opt/raigolmid/
COPY raigolmid/raigolmid /opt/raigolmid/raigolmid
COPY raigolmid/rai /opt/raigolmid/rai
COPY ui/__init__.py ui/copyable.py ui/edge.py ui/hostevents.py ui/hostipc.py ui/layershell.py \
     ui/slide.py ui/surfaces.py ui/theme.py /opt/ui/
COPY ui/viewmodel /opt/ui/viewmodel
COPY ui/ai_terminal /opt/ui/ai_terminal
RUN pip install --no-cache-dir --break-system-packages /opt/raigolmid

WORKDIR /work
# The install is root's under /usr/local and the agent runs as `agent`, so an update could only
# fail, and the version is the pin's to change.
ENV HOME=/home/${USERNAME} \
    RAIGOLMID_SOCKET=/run/raigolmid/raigolmid.sock \
    DISABLE_AUTOUPDATER=1

# Every tab's MCP server is a plugin so it can be its channel: a plugin on the managed
# allowlist starts with `--channels` and no confirmation, where a bare server needs the
# development flag and a dialog nothing can pre-accept. Installed into
# $HOME at start: $HOME is the tab's home mounted from the host, which hides the image's.
COPY agents/claude/plugin /usr/local/share/raigolmi/plugin
COPY agents/claude/plugin/managed-settings.json /etc/claude-code/managed-settings.json
COPY agents/claude/agent-session.sh /usr/local/bin/agent-session.sh

# The agent user from the first process, so the container needs no capability at all.
USER ${USERNAME}
# No CMD: raigolmid passes every launch's command (agents.CLAUDE).
ENTRYPOINT ["/bin/bash", "/usr/local/bin/agent-session.sh"]
