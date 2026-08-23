#!/bin/bash
# uninstall_discord_launchd.sh — stops and removes the com.lira.discord
# launchd agent installed by install_discord_launchd.sh.
set -euo pipefail

PLIST_DEST="$HOME/Library/LaunchAgents/com.lira.discord.plist"

if [ -f "$PLIST_DEST" ]; then
    launchctl unload "$PLIST_DEST" 2>/dev/null || true
    rm -f "$PLIST_DEST"
    echo "Removed com.lira.discord"
else
    echo "com.lira.discord is not installed (nothing to do)"
fi
