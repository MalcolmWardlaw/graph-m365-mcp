#!/usr/bin/env fish
# Render org.wardlaw.graph-m365.plist from the Claude Desktop config and
# bootstrap it as a LaunchAgent in the GUI session.
#
#   fish launchd/install.fish            # install or reinstall
#   fish launchd/install.fish --remove   # bootout and delete the plist
#
# Every key in the Desktop config's mcpServers.graph-m365.env block becomes an
# EnvironmentVariables entry, so the daemon registers exactly the tools the
# Desktop host already expects (capability flags belong to the daemon, not to
# the shim). KEYCHAIN_BACKEND is dropped if present: the daemon runs on the
# file backend only. After installing, restart Claude Desktop and reconnect
# live Claude Code sessions (/mcp) so they pick up the shim path.

set -l label org.wardlaw.graph-m365
set -l repo (path resolve (path dirname (status filename))/..)
set -l dest "$HOME/Library/LaunchAgents/$label.plist"
set -l cfg "$HOME/Library/Application Support/Claude/claude_desktop_config.json"
set -l uv (command -v uv; or echo /opt/homebrew/bin/uv)

if contains -- --remove $argv
    launchctl bootout gui/(id -u)/$label 2>/dev/null
    rm -f $dest
    echo "removed $label"
    exit 0
end

if not test -f $cfg
    echo "no Desktop config at $cfg; set GRAPH_CLIENT_ID and GRAPH_TENANT_ID there first" >&2
    exit 1
end

python3 $repo/launchd/render_plist.py $repo/launchd/$label.plist $cfg $dest $repo $uv
or exit 1

launchctl bootout gui/(id -u)/$label 2>/dev/null
launchctl bootstrap gui/(id -u) $dest
and launchctl kickstart -k gui/(id -u)/$label
echo "bootstrapped $label; socket appears at ~/.config/graph-m365/mcp.sock"
echo "log: ~/Library/Logs/graph-m365.log"
