#!/bin/bash
# The entrypoint, run as the agent user: everything that writes into $HOME, which is the tab's
# home mounted from the host and owned by the agent user.
set -euo pipefail

# The tab names the agent's git identity below. Its scope is the socket it is given.
: "${RAIGOLMI_TAB:?RAIGOLMI_TAB is required}"

# Claude Code keeps user-scope MCP servers and first-run state in ~/.claude.json; it reads
# no ~/.claude/mcp.json. Merged, not written, so a restarted tab keeps its own state.
#  - onboarding marked done: an interactive first run ignores
#    CLAUDE_CODE_OAUTH_TOKEN and opens a browser login no tab can complete;
#  - /work trusted: otherwise every tab opens on Claude Code's folder-trust question. The
#    key is the one Claude Code's own refusal names (`projects[<dir>].hasTrustDialogAccepted`).
config="$HOME/.claude.json"
[ -f "$config" ] || echo '{}' > "$config"
jq '.hasCompletedOnboarding = true | .projects["/work"].hasTrustDialogAccepted = true' \
   "$config" > "$config.new"
mv "$config.new" "$config"

: "${RAIGOLMI_SCOPE:?RAIGOLMI_SCOPE is required: tab or machine}"
# The tab's server is its channel, so it comes as the plugin the image's managed settings
# approve (agents/claude/plugin/), and `rai mcp` takes its scope from RAIGOLMI_SCOPE. A
# user-scope server of the same name would be a second, unapproved one, so a home that has
# one loses it. Both commands are no-ops on a home that already has the plugin.
jq 'del(.mcpServers.raigolmi)' "$config" > "$config.new"
mv "$config.new" "$config"
claude plugin marketplace add /usr/local/share/raigolmi/plugin >/dev/null
claude plugin install raigolmi@raigolmi >/dev/null

# Every launch bypasses permissions (agents.CLAUDE); the dialog accepting that mode is
# answered here, where no tab could answer it.
settings="$HOME/.claude/settings.json"
mkdir -p "$HOME/.claude"
[ -f "$settings" ] || echo '{}' > "$settings"
# SessionStart is the evidence a restarted agent came up: one that dies before it is a
# crash reopening cannot fix. The other hooks tell raigolmid when it is working; no tab
# closes itself when it is done. The prompt
# hook reads its input, which says whether the prompt is a channel push or the user's. An
# interrupt fires no Stop, so an interrupted agent stays busy — kept rather than closed with
# its work. A turn ending with background commands running is decided by `agent-activity stop` (raigolmid/agent_stop.py).
# A turn an API error ended fires StopFailure instead of Stop, and is idle with its error.
report='RAIGOLMID_SOCKET=/run/raigolmid/raigolmid.sock rai agent-activity'
# The model is the user's settings' (raigolmid/settings.py), set on every start so a change there
# reaches every tab.
: "${RAIGOLMI_MODEL:?raigolmid hands every tab its model}"
jq --arg session "$report session" --arg busy "$report busy" --arg stop "$report stop" \
   --arg failed "$report failed" \
   --arg model "$RAIGOLMI_MODEL" \
   '.skipDangerousModePermissionPrompt = true
    | .model = $model
    | .hooks.SessionStart = [{hooks: [{type: "command", command: $session}]}]
    | .hooks.UserPromptSubmit = [{hooks: [{type: "command", command: $busy}]}]
    | .hooks.Stop = [{hooks: [{type: "command", command: $stop}]}]
    | .hooks.StopFailure = [{hooks: [{type: "command", command: $failed}]}]' \
   "$settings" > "$settings.new"
mv "$settings.new" "$settings"

git config --global safe.directory /work
# An agent commits as itself, one identity per tab: `git log` says which tab wrote a commit,
# and the user is the one who merges it.
git config --global user.name "Claude (${RAIGOLMI_TAB})"
git config --global user.email "agent@raigolmi.local"

# The daemon serves this tab's socket once it hears the tab open, which can be just after
# this container starts; the hooks and the MCP server must not run before it answers.
waited=0
until said=$(python3 -c 'from raigolmid.client import ApiClient; from raigolmid.paths import Paths
ApiClient(Paths.from_env().api_socket).call("version")' 2>&1); do
    if [ "$waited" -ge 60 ]; then
        echo "agent-session: this tab's socket $RAIGOLMID_SOCKET never answered: $said" >&2
        exit 1
    fi
    sleep 0.5
    waited=$((waited + 1))
done

exec "$@"
