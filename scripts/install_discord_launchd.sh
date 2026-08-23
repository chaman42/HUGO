#!/bin/bash
# install_discord_launchd.sh — installs com.hugo.discord as a launchd
# LaunchAgent so the Discord bridge (core/discord_bridge.py) runs
# independently of the Electron app: starts at login, restarts on crash
# (KeepAlive), and comes back after the Mac wakes from sleep.
#
# Uses the project's own venv/bin/python3, not /usr/bin/python3 — the
# system Python has neither discord.py nor python-dotenv installed, so
# pointing launchd at it would crash-loop forever under KeepAlive.
set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
PLIST_SRC="$REPO_ROOT/scripts/com.hugo.discord.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/com.hugo.discord.plist"
VENV_PYTHON="$REPO_ROOT/venv/bin/python3"

if [ ! -x "$VENV_PYTHON" ]; then
    echo "error: $VENV_PYTHON not found — set up the project venv first (see requirements.txt)." >&2
    exit 1
fi

mkdir -p "$REPO_ROOT/logs"

sed "s#/PATH/TO/REPO#$REPO_ROOT#g" "$PLIST_SRC" > "$PLIST_DEST"

# Any older single-daemon agent from before this script existed — avoid a
# second Gateway connection on the same bot token, which would answer
# every Discord DM twice.
OLD_LABEL="com.jarvislite.discordbridge"
OLD_PLIST="$HOME/Library/LaunchAgents/$OLD_LABEL.plist"
if [ -f "$OLD_PLIST" ]; then
    launchctl unload "$OLD_PLIST" 2>/dev/null || true
    rm -f "$OLD_PLIST"
    echo "Removed older agent: $OLD_LABEL"
fi

launchctl unload "$PLIST_DEST" 2>/dev/null || true
launchctl load "$PLIST_DEST"

echo "Installed and started com.hugo.discord"
echo "  plist:  $PLIST_DEST"
echo "  python: $VENV_PYTHON"
echo "  logs:   $REPO_ROOT/logs/discord_bridge.log"
echo
echo "Check status with: launchctl list | grep com.hugo.discord"
