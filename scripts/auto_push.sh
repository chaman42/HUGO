#!/usr/bin/env bash
# Auto-sync for JarvisLite — invoked every 20 minutes by launchd.
# If there are uncommitted changes: git add -A, commit, push to origin/main.
# All output is captured by launchd into logs/autopush.log.
# Push failures (offline, auth timeout, etc.) are logged and silently ignored;
# the next interval will retry automatically.

REPO="$HOME/Desktop/JarvisLite"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

# GIT_DIR + GIT_WORK_TREE let git operate on the repo without chdir-ing into
# ~/Desktop, which avoids a macOS launchd getcwd() permission restriction.
export GIT_DIR="$REPO/.git"
export GIT_WORK_TREE="$REPO"

# Nothing staged or untracked — skip silently but leave a breadcrumb
if ! git status --porcelain | grep -q .; then
    echo "[$TIMESTAMP] Nothing to commit"
    exit 0
fi

# Stage everything (mirrors the manual workflow)
git add -A

# Commit — should never fail in normal circumstances, but guard anyway
if ! git commit --quiet -m "Auto-sync: $TIMESTAMP"; then
    echo "[$TIMESTAMP] ERROR: commit failed (see git output above)"
    exit 0
fi

# Push; on failure log the reason and exit cleanly so the interval continues
if git push --quiet origin main 2>&1; then
    echo "[$TIMESTAMP] Synced OK"
else
    echo "[$TIMESTAMP] Commit OK — push failed (offline or auth issue), will retry next interval"
fi
