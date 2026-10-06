"""Host-side paths.

Every path the daemon uses is derived here, from the XDG variables, so a test can point
the whole daemon at a temporary directory by setting them.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP = "raigolmid"
PROJECT = "raigolmi"


def _xdg(var: str, default: Path) -> Path:
    raw = os.environ.get(var)
    return Path(raw).expanduser() if raw else default


# In a face's runtime dir from its first sync on: its store is in, so its apps and the fonts and
# libraries they use resolve. Removed as the face starts; a face's own startup waits on it
# (agents/guide/faces.md § Apps).
APPS_READY = "raigolmid-apps-ready"


@dataclass(frozen=True)
class Paths:
    state: Path
    data: Path
    config: Path
    runtime: Path

    @classmethod
    def from_env(cls) -> "Paths":
        home = Path(os.environ.get("HOME", "/root")).expanduser()
        state = _xdg("XDG_STATE_HOME", home / ".local" / "state") / APP
        data = _xdg("XDG_DATA_HOME", home / ".local" / "share") / APP
        config = _xdg("XDG_CONFIG_HOME", home / ".config")
        runtime = _xdg("XDG_RUNTIME_DIR", Path(f"/run/user/{os.getuid()}"))
        return cls(state=state, data=data, config=config / APP, runtime=runtime)

    # --- state -------------------------------------------------------------------
    @property
    def intent(self) -> Path:
        return self.state / "intent.json"

    @property
    def lock(self) -> Path:
        return self.state / "lock"

    @property
    def events(self) -> Path:
        return self.state / "events.jsonl"

    @property
    def questions(self) -> Path:
        """Questions and permissions, and those answered *always* (`questions.py`)."""
        return self.state / "questions.json"

    @property
    def body_pulls(self) -> Path:
        """Each image pulled as a body's, and the body it was pulled for (`Instances`), so one
        no body names any more is collected (`Session.collect_garbage`)."""
        return self.state / "body-pulls.json"

    @property
    def messages(self) -> Path:
        """What agent tabs asked each other and how each was settled (`messages.py`)."""
        return self.state / "messages.json"

    @property
    def supervisor(self) -> Path:
        """The container each unit's one restart started (`supervisor.py`)."""
        return self.state / "supervisor.json"

    @property
    def protection(self) -> Path:
        """Each sandbox's protected git files as its agent found them (`Instances.protect`)."""
        return self.state / "protection.json"

    @property
    def jobs(self) -> Path:
        """Each tab's long jobs and the launcher that runs each (`jobs.py`)."""
        return self.state / "jobs.json"

    @property
    def budget_asks(self) -> Path:
        """The bodies whose tabs were asked to set a budget (`budgets.py`)."""
        return self.state / "budget-asks.json"

    @property
    def disk(self) -> Path:
        """The disk reading growth is measured from (`disk.py`)."""
        return self.state / "disk.json"

    @property
    def channels(self) -> Path:
        """What is on its way into each tab's session (`channel.py`)."""
        return self.state / "channels.json"

    @property
    def permissions(self) -> Path:
        """The user's permissions answered always, a document they edit (`questions.py`)."""
        return self.state / "permissions.md"

    @property
    def history(self) -> Path:
        """The user's history, and what of it they have seen (`history.py`)."""
        return self.state / "history.json"

    @property
    def viewing(self) -> Path:
        """What the user has viewed in the AI terminal (`viewing.py`)."""
        return self.state / "viewing.json"

    @property
    def janitor_documents(self) -> Path:
        """The janitor's incidents and this machine's failure patterns
        (`documents.py`), mounted at /janitor in the janitor's container and no other."""
        return self.state / "janitor"

    @property
    def preferences(self) -> Path:
        """The user's preferences doc, written by the judge (`judge.py`) and by them in the
        catalog.
        Not under the definitions, which every agent mounts writable."""
        return self.state / "preferences.md"

    # --- data --------------------------------------------------------------------
    @property
    def work(self) -> Path:
        """`/work` when no body is selected: one directory outside every definition, so
        adding or dropping a toolbelt keeps what is in it. Empty at first."""
        return self.data / "work"

    @property
    def closures(self) -> Path:
        """Nix closures copied out of their images, one directory per image id (`closures.py`)."""
        return self.data / "closures"

    @property
    def agent_homes(self) -> Path:
        return self.data / "agent-home"

    @property
    def crashes(self) -> Path:
        """The exit code and output of each container that exited on its own, kept past the
        restart that removes it (`supervisor.py`): what the janitor diagnoses from."""
        return self.data / "crashes"

    @property
    def agent_archive(self) -> Path:
        """Closed tabs' homes, one directory each, kept for their conversations."""
        return self.data / "agent-archive"

    @property
    def runs(self) -> Path:
        """The machine tab's report on each run it managed, with its record of the run."""
        return self.data / "runs"

    # --- config ------------------------------------------------------------------
    @property
    def agent_templates(self) -> Path:
        return self.config / "agent-templates"

    @property
    def agent_plugins(self) -> Path:
        """The machine's skills and MCP servers, one Claude Code plugin per directory, which
        every tab takes up."""
        return self.config / "agent-plugins"

    @property
    def look(self) -> Path:
        """The user's `[look]` as the host surfaces read it (`look.py`, `ui/theme.py`): in
        the runtime dir, the one directory every surface's container has."""
        return self.runtime / "raigolmid-look.json"

    @property
    def settings(self) -> Path:
        """The user's settings (`settings.py`). Owned by the project rather than the daemon: the
        host compositor's keys are among them."""
        return self.config.parent / PROJECT / "settings.toml"

    @property
    def proxy_secret(self) -> Path:
        """The key the credential proxy's placeholders are signed with (`credproxy.py`)."""
        return self.state / "proxy-secret"

    @property
    def proxy_authority(self) -> Path:
        """The certificate authority an agent trusts for Anthropic's API through the proxy."""
        return self.state / "proxy-ca"

    @property
    def agent_credentials(self) -> Path:
        # A host-managed secret: held by raigolmid, which gives each agent container a
        # placeholder for it (`credproxy.py`); never written into an image or a tab's home.
        return self.config.parent / PROJECT / "agent-credentials"

    @property
    def registry_token(self) -> Path:
        # The user's GitHub sign-in: a catalog upload's, and every agent's through the
        # credential proxy, which alone reads it (`credproxy.py`).
        return self.config.parent / PROJECT / "registry-token"

    @property
    def claude_login(self) -> Path:
        # The user's claude.ai sign-in, which Remote Control needs and the agent credential
        # cannot give: held and refreshed by raigolmid alone (`claude_login.py`).
        return self.config.parent / PROJECT / "claude-login.json"

    @property
    def registry_state(self) -> Path:
        # Where each downloaded layer came from, kept out of the layer's own directory.
        return self.state / "registry.json"

    # --- runtime -----------------------------------------------------------------
    @property
    def ai_ready_lock(self) -> Path:
        """Held by whoever makes sure an agent is ready (`ui.ai_terminal.terminal.show_agent`,
        the daemon at start), so two of them never open the same next tab."""
        return self.runtime / "raigolmi-ai-ready.lock"

    @property
    def api_socket(self) -> Path:
        # Set inside an agent container, where its tab's own socket directory is mounted at
        # /run/raigolmid (`agent_socket_dir`).
        override = os.environ.get("RAIGOLMID_SOCKET")
        return Path(override) if override else self.runtime / "raigolmid.sock"

    def agent_socket_dir(self, tab_id: str) -> Path:
        """The one directory of the runtime dir an agent container is given, holding
        only its tab's `raigolmid.sock`. The runtime dir itself holds the host compositor's
        IPC socket, D-Bus and the user's systemd, each of which runs host commands."""
        return self.runtime / APP / "agents" / tab_id

    @property
    def view_sockets(self) -> Path:
        """Launcher sockets. On the host rather than in the view, so they survive a daemon
        restart and a view can be re-adopted.

        `/run/raigolmid/views` when usable: `raigolmid` runs as the desktop user, who cannot
        create a directory under `/run`, so the host image ships a `tmpfiles.d` rule that
        creates it 0700 owned by that user. Otherwise (a host without that rule) the user's
        runtime directory.

        The directory must not be root's: the view's uid is read from its owner.

        The choice is deterministic for a given user, so `raigolmid`, `rai` and the
        selectors all resolve the same path without agreeing on one out of band.
        """
        if d := os.environ.get("RAIGOLMID_VIEW_SOCKET_DIR"):
            return Path(d)
        system = Path("/run") / APP / "views"
        if _usable_dir(system):
            return system
        return self.runtime / APP / "views"

    @property
    def host_keys_include(self) -> Path:
        """The generated sway binding include, which `host/sway/config` reads.

        `/run/raigolmid/host-keys.conf`, created empty by a `tmpfiles.d` rule so a boot where
        `raigolmid` never starts still loads a valid config and keeps the static fallback
        bindings — the recovery state, not a nicety.

        Resolved as `view_sockets` is: the system path when usable, the user's runtime
        directory otherwise, so a host without the rule and the tests need no `/run/raigolmid`.
        """
        if d := os.environ.get("RAIGOLMID_HOST_KEYS_INCLUDE"):
            return Path(d)
        system = Path("/run") / APP / "host-keys.conf"
        if _usable_dir(system.parent):
            return system
        return self.runtime / APP / "host-keys.conf"

    def launcher_socket(self, instance_id: str) -> Path:
        return self.view_sockets / f"{_safe(instance_id)}.sock"

    @property
    def face_runtime(self) -> Path:
        """The user's face's runtime dir (`Faces.start`): its display, its sway's IPC socket,
        its editor's socket and its apps' logs. Its own rather than the user's, which holds the
        host compositor's IPC socket, D-Bus and the user's systemd, each of which runs host
        commands. Beyond the host's display, the face reaches the host only through
        `face_socket_dir`'s socket."""
        return self.runtime / "raigolmid-face"

    def face_runtimes(self) -> dict[str, Path]:
        """Each face's runtime dir, by the role its container is labelled with."""
        from . import labels
        return {str(labels.Role.FACE): self.face_runtime,
                str(labels.Role.FACE_TRIAL): self.face_trial_runtime}

    @property
    def face_trial_runtime(self) -> Path:
        """The runtime dir of the face tried off the user's screen (`Faces.start_trial`), apart
        from theirs, so nothing of theirs reaches it and nothing of it is taken for their face's."""
        return self.runtime / "raigolmid-face-trial"

    def face_socket_dir(self, trial: bool) -> Path:
        """The directory holding a face's own `raigolmid.sock` (`scopes.FaceSockets`), mounted
        into the face at /run/raigolmid. Outside the face's runtime dir, which a trial's stop
        removes, so the socket is served once for the daemon's life."""
        return self.runtime / APP / "face" / ("trial" if trial else "face")

    @property
    def face_home(self) -> Path:
        """The user's things: the HOME of every face on their screen, so what they keep
        outlives the face and a switch. Not a body's working copy."""
        return self.data / "face-home"

    @property
    def transfer(self) -> Path:
        """The guest's `~/Transfer`, where the Windows launcher drops files and whose `out`
        it empties to Windows. In the user's face's home, so a file from Windows is in every
        face."""
        return Path(os.environ.get("HOME", "/root")).expanduser() / "Transfer"

    @property
    def editor_window_log(self) -> Path:
        """The user's face's editor window's output, in the face's runtime dir, so the host reads
        what the window wrote."""
        return self.face_runtime / "raigolmid-editor-window.log"

    @property
    def browser_log(self) -> Path:
        """What the face's browser wrote when an agent last opened a page in it
        (`Faces.show_url`). In the face's runtime dir for `editor_window_log`'s reason."""
        return self.face_runtime / "raigolmid-browser.log"

    @property
    def editor_socket(self) -> Path:
        """Where the face's editor may listen (`{socket}` in its command): the channel into
        the editor the user is looking at."""
        return self.face_runtime / "raigolmid-editor.sock"

    @property
    def editor_request(self) -> Path:
        """The file to show and its line, as JSON, for a face's `[editor] open` that reads
        them rather than taking them as arguments (`{request}`)."""
        return self.face_runtime / "raigolmid-editor-request.json"

    @property
    def focused_view(self) -> Path:
        """The view the face's apps reach when they name no sandbox: `<instance>
        <view container id>`, or empty when the focused instance has none. Beside the user's
        face's socket, so the face's one mount carries both (`RAIGOLMID_FOCUSED` inside it);
        replaced by rename, so a watcher sees one change per view."""
        if f := os.environ.get("RAIGOLMID_FOCUSED"):
            return Path(f)
        return self.face_socket_dir(False) / "focused"


    def ensure(self) -> None:
        for d in (self.state, self.data, self.work, self.closures, self.crashes,
                  self.agent_archive, self.config, self.agent_templates, self.agent_plugins,
                  self.view_sockets, self.face_socket_dir(False)):
            d.mkdir(parents=True, exist_ok=True)
        # Every launcher's socket is in this, so it is what keeps a launcher private to the
        # daemon's user. The mode is set every time, because a directory made by a looser
        # umask is exactly the case that would leak.
        self.view_sockets.chmod(0o700)


def _usable_dir(path: Path) -> bool:
    """Whether this process can actually create and write into `path`. Checked rather than
    assumed, because the failure otherwise lands as a PermissionError during daemon
    startup, which reads as a bug rather than as "you are not root"."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return os.access(path, os.W_OK | os.X_OK)


def _safe(instance_id: str) -> str:
    """`myapi@session` is a fine instance id and a poor filename component."""
    return instance_id.replace("@", "-").replace("/", "-")
