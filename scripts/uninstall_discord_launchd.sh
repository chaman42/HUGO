#!/bin/bash
# uninstall_discord_launchd.sh — stops and removes the com.hugo.discord
# launchd agent installed by install_discord_launchd.sh.
set -euo pipefail

PLIST_DEST="$HOME/Library/LaunchAgents/com.hugo.discord.plist"

if [ -f "$PLIST_DEST" ]; then
    launchctl unload "$PLIST_DEST" 2>/dev/null || true
    rm -f "$PLIST_DEST"
    echo "Removed com.hugo.discord"
else
    echo "com.hugo.discord is not installed (nothing to do)"
fi
