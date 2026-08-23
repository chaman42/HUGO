#!/bin/bash
#
# rebuild_app.sh — one-command rebuild + reinstall of HUGO.app from current
# source (git pull -> bump ui/sw.js cache key & push -> npm run build ->
# replace /Applications/HUGO.app).
#
# NOTE: whenever this script actually rebuilds (i.e. doesn't hit one of the
# early-exit guards below), it commits and pushes to origin/main on its own
# — see the "Cache bust" step. That's a real, permanent commit each time,
# not just a local edit.
#
# Bulletproofing (HUGO_FORCE_UPDATE=1, the "Actualizar Sistema" button):
# every guard below that can short-circuit the run — connectivity, "HUGO is
# running", "already built this commit" — is gated so FORCE bypasses ALL of
# them unconditionally. A forced run either completes the full
# pull/bump/build/install chain or fails LOUDLY via fail() (logged, exit 1,
# surfaced to the frontend as an error) — it can never silently report
# success without having actually rebuilt. The unattended 6-hourly
# LaunchAgent run (no FORCE) keeps every guard, since quietly skipping when
# there's nothing to do or no connectivity is exactly what that path wants.
#
# Used two ways:
#   - Manually from Terminal: ~/Desktop/JarvisLite/scripts/rebuild_app.sh
#   - Automatically every 6 hours by the com.joan.hugo.autoupdate LaunchAgent
#
# IMPORTANT: never `cd` into the project tree. A launchd-invoked process can
# hit a macOS TCC "Desktop Folder" permission wall doing that — this project
# already has a live example: the com.joan.jarvislite.autopush LaunchAgent
# runs as /bin/bash, which has no Desktop Folder TCC grant, so every git
# command it runs silently fails with "fatal: not a git repository" (see
# logs/autopush.log) even though the repo is right there. Confirmed via the
# TCC database (~/Library/Application Support/com.apple.TCC/TCC.db) that
# python3.11 and this project's own venv python DO have the grant, while
# /bin/bash does not — that's why the LaunchAgent for this script invokes it
# through python3.11 rather than bash directly. This script itself avoids
# `cd` regardless (GIT_DIR/GIT_WORK_TREE for git, `npm --prefix` for the
# build), as a second layer of defense, and works fine run directly from
# Terminal too (Terminal.app already has the TCC grant).

set -uo pipefail

# Bug fix ("npm: command not found" — see logs/launcher.log): this script is
# invoked from launcher.py's subprocess.run(), and launcher.py itself often
# runs under launchd/Electron with the bare default PATH
# (/usr/bin:/bin:/usr/sbin:/sbin — confirmed via `ps eww` on the live
# process), which never includes wherever Homebrew/Node actually installed
# npm. Prepend every common macOS npm/node location so this resolves
# regardless of the caller's environment, same category of launchd-
# environment gotcha as the Desktop Folder TCC issue documented above.
export PATH="/usr/local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"

REPO="$HOME/Desktop/JarvisLite"
ELECTRON_DIR="$REPO/electron"
BUILT_APP="$ELECTRON_DIR/dist/mac-universal/HUGO.app"
INSTALLED_APP="/Applications/HUGO.app"
# The commit hash last successfully INSTALLED to $INSTALLED_APP — written
# only at the very end, after every prior step (pull, cache bust, build,
# install) has confirmed success. Lives in electron/ (gitignored — see
# .gitignore) rather than the repo root so it reads naturally as "which
# version of the electron app is currently installed", not generic repo
# state.
APP_VERSION_FILE="$ELECTRON_DIR/.app_version"

export GIT_DIR="$REPO/.git"
export GIT_WORK_TREE="$REPO"
export GIT_TERMINAL_PROMPT=0

# Every step logs to BOTH stdout (still captured by whichever caller
# invoked this script — launcher.py's subprocess.run() into logs/
# launcher.log for the button, or the LaunchAgent's own redirect) AND
# directly to logs/autoupdate.log here, unconditionally — so the button
# path (which never wrote to autoupdate.log before) gets the same
# traceable, step-by-step record as the unattended path, in one place,
# regardless of who triggered the run.
AUTOUPDATE_LOG="$REPO/logs/autoupdate.log"
ts()  { date '+%Y-%m-%d %H:%M:%S'; }
log() {
    local line="[$(ts)] $1"
    echo "$line"
    mkdir -p "$(dirname "$AUTOUPDATE_LOG")" 2>/dev/null
    echo "$line" >> "$AUTOUPDATE_LOG" 2>/dev/null
}
fail() {
    local line="[$(ts)] ERROR: $1"
    echo "$line" >&2
    echo "$line" >> "$AUTOUPDATE_LOG" 2>/dev/null
    exit 1
}

FORCED=false
[ "${HUGO_FORCE_UPDATE:-0}" = "1" ] && FORCED=true

log "rebuild_app.sh starting (forced=$FORCED)"

# --- Auto-update toggle --------------------------------------------------
# Skipped when forced (the button) — same "never let the button no-op"
# reasoning as every other guard in this script (see the module comment
# above). 'auto_update_enabled' (Ajustes -> 'Auto-actualización') lets
# Joan turn off the unattended 6-hourly com.joan.hugo.autoupdate
# LaunchAgent run specifically, without touching the manual "Actualizar
# Sistema" button at all. Read directly from disk rather than via
# core.memory_flags — this script runs as its own process, independent of
# jarvis.py, so that module's in-memory cache doesn't exist here. Defaults
# to enabled (missing key / unreadable JSON / no python3 on PATH never
# blocks the update — fails open, same as every value core.memory_flags
# itself falls back to when a flag hasn't been set yet).
if [ "$FORCED" = false ]; then
    AUTO_UPDATE_ENABLED=$(python3 -c "
import json
try:
    with open('$REPO/data/feature_flags.json') as f:
        print('true' if json.load(f).get('auto_update_enabled', True) else 'false')
except Exception:
    print('true')
" 2>/dev/null)
    if [ "$AUTO_UPDATE_ENABLED" = "false" ]; then
        log "Auto-actualización desactivada en Ajustes — skipping this unattended run"
        exit 0
    fi
fi

# --- Concurrency lock ---------------------------------------------------
# Bug fix (root cause of "update button still not working reliably"): nothing
# previously stopped two rebuild_app.sh instances from running at once — e.g.
# the com.joan.hugo.autoupdate LaunchAgent fires every 6h (StartInterval,
# unconditionally, regardless of what else is happening) completely
# independently of the "Actualizar Sistema" button. If both land at the same
# moment, they'd race on the same `git stash`/`pull`/`commit`/`push` sequence
# and — worse — on `rm -rf "$INSTALLED_APP/Contents"` + `cp -R`, which can
# leave /Applications/HUGO.app half-overwritten and broken. macOS has no
# built-in `flock` (that's Linux-only), so this uses `mkdir` as the lock
# primitive instead — directory creation is atomic on every filesystem this
# app runs on, so it's a portable, dependency-free mutex.
#
# FORCED (the button) must never no-op, so it WAITS for the lock rather than
# skipping — it queues behind whoever's running and then does its own real
# rebuild, satisfying "impossible to make a no-op" without corrupting a
# concurrent run. The unattended LaunchAgent run keeps the "skip, don't
# fight for it" behavior of every other guard in this script, since forcing
# it to queue behind a manual update would just be pointless waiting for a
# background job nobody's watching.
LOCKDIR="$REPO/.rebuild.lock"
LOCK_MAX_WAIT=180     # seconds a forced run will queue — well under the 600s
                      # timeout launcher.py's subprocess.run() enforces, and
                      # far more than a real unattended run ever takes (~30-40s
                      # per logs/autoupdate.log).
LOCK_POLL_INTERVAL=3

acquire_lock() {
    local waited=0
    while true; do
        if mkdir "$LOCKDIR" 2>/dev/null; then
            echo $$ > "$LOCKDIR/pid"
            return 0
        fi
        local holder_pid=""
        [ -f "$LOCKDIR/pid" ] && holder_pid=$(cat "$LOCKDIR/pid" 2>/dev/null)
        # Stale lock from a crashed/killed prior run — its PID is gone, so it's
        # safe to reclaim instead of waiting out the full timeout for nothing.
        if [ -n "$holder_pid" ] && ! kill -0 "$holder_pid" 2>/dev/null; then
            log "Removing stale rebuild lock (pid $holder_pid no longer running)"
            rm -rf "$LOCKDIR"
            continue
        fi
        if [ "$FORCED" = false ]; then
            log "Another rebuild is already in progress (pid ${holder_pid:-unknown}) — skipping this unattended run"
            exit 0
        fi
        if [ "$waited" -ge "$LOCK_MAX_WAIT" ]; then
            fail "timed out after ${LOCK_MAX_WAIT}s waiting for a concurrent rebuild (pid ${holder_pid:-unknown}) to finish"
        fi
        log "Another rebuild is in progress (pid ${holder_pid:-unknown}) — waiting for it to finish (forced run never skips)…"
        sleep "$LOCK_POLL_INTERVAL"
        waited=$((waited + LOCK_POLL_INTERVAL))
    done
}
acquire_lock
# Runs on every exit path — normal completion, fail()'s `exit 1`, or an
# unexpected signal — so the lock can never outlive this process by accident.
trap 'rm -rf "$LOCKDIR"' EXIT

# --- Connectivity check ------------------------------------------------------
# Skipped when forced: bypassing it doesn't make the run succeed without
# internet (git pull/push still need it), it just means a forced run fails
# LOUDLY via fail() on the actual git operation below instead of quietly
# exiting 0 here — "no shortcuts, no skipping" for the button, while the
# unattended LaunchAgent still exits quietly rather than logging a failure
# every time the Mac happens to be offline.
if [ "$FORCED" = false ] && ! curl -s --max-time 5 -o /dev/null https://github.com; then
    log "No internet connection — skipping this run"
    exit 0
fi

# --- Don't disturb a session that's actively running ------------------------
# Skipped when HUGO_FORCE_UPDATE=1 (set by launcher.py's POST /api/update,
# i.e. the "Actualizar HUGO" button) — that's an explicit, user-initiated
# request made from inside the very session being updated, where "reinicia
# la app" is the whole point. Replacing Contents/ under a running process is
# safe on macOS/Unix (the running binary keeps executing from its already-
# mapped pages via unlink-while-open semantics; only the next launch sees the
# new files). The unattended 6-hourly LaunchAgent run leaves this check on.
if [ "$FORCED" = false ] && pgrep -f "$INSTALLED_APP/Contents/MacOS/HUGO" >/dev/null 2>&1; then
    log "HUGO is currently running — skipping this run to avoid disrupting an active session"
    exit 0
fi

# --- git pull -----------------------------------------------------------
# Stash first: the running app writes its own runtime data (e.g.
# data/concepts.json) into the working tree between commits, which would
# otherwise block a clean pull.
#
# Hardening: this used to pop the stash unconditionally after the pull and
# just log a WARNING if either the pull or the pop failed, then barrel on
# into the cache-bump/commit/build/install steps regardless. That silently
# stranded local changes in the stash TWICE in one real session — once
# because a flaky pull left the stash unpopped and the script pushed a
# cache-bust commit on top anyway, and once because the still-running app
# rewrote the same data/*.json files while a pull was hanging for ~90s, so
# the eventual pop refused ("your local changes would be overwritten by
# stash") and the WARNING scrolled by unnoticed. Invariants now enforced:
#   1. A failed pull -> restore the stash, then fail() (exit 1). Never
#      proceed to bump/commit/build on a tree that isn't actually pulled.
#   2. A failed restore AFTER a successful pull -> fail() immediately,
#      leaving BOTH the already-advanced pulled HEAD and the still-present
#      stash entry untouched (`git stash drop` only ever runs after a
#      confirmed-clean apply) — never silently lose either side.
#   3. A transient-looking pull failure (network blip) retries once after
#      5s before giving up.
#   4. Every stash push/apply/drop attempt and outcome is logged, so a
#      stranded stash is always traceable from logs/autoupdate.log alone
#      instead of needing manual `git reflog`/`git stash show` spelunking.
#
# --- Claude Code guard ---------------------------------------------------
# Bug fix: a real incident (2026-07-26) traced editing-session file
# corruption directly to this exact stash/pull dance below — it stashed a
# live Claude Code session's in-progress, uncommitted source edits
# (git can't tell those apart from the running HUGO app's own data/*.json
# writes, which is the only case this stash was ever designed for), then
# the restore failed (see invariant 2 above), leaving the edited files
# reverted to their last-committed state — indistinguishable from a plain
# revert. `pgrep -f 'claude'` matches the Claude Code CLI process
# case-sensitively, so it does NOT false-positive on the separate Claude
# desktop app (its processes are all under capitalized /Applications/
# Claude.app paths).
#
# Deliberately checked UNCONDITIONALLY — NOT gated behind
# `[ "$FORCED" = false ]` like the connectivity/"HUGO running" guards
# above, unlike every other guard in this script. Those exist to avoid a
# pointless no-op during an idle period; this one exists to avoid data
# loss, and HUGO_FORCE_UPDATE=1 (the "Actualizar Sistema" button) carries
# the exact same stash risk as the unattended timer if a Claude Code
# session happens to be active — there's no version of "force" that makes
# stashing someone's live edits safe. A forced run just fails to no-op
# this one time; the alternative is silently reverting active work.
#
# HUGO_SKIP_CLAUDE_GUARD=1 is the one deliberate exception: it's set only
# when the user re-confirms through a second, explicit "a Claude Code
# session is active, force anyway?" dialog (see the button's second
# confirm modal in ui/index.html / ui/app.js) — i.e. the user has already
# been told about the exact stash risk this guard exists for and chose to
# accept it for this one run, same as HUGO_FORCE_UPDATE's own confirm
# modal already does for the ordinary "HUGO will restart" risk.
if pgrep -f 'claude' >/dev/null 2>&1; then
    if [ "${HUGO_SKIP_CLAUDE_GUARD:-0}" = "1" ]; then
        log "Claude Code session active — proceeding anyway (user confirmed override)"
    else
        log "Skipped: Claude Code session active"
        exit 0
    fi
fi

log "Checking for local runtime-data changes to stash before pulling…"
STASHED=false
if ! git diff --quiet || ! git diff --cached --quiet; then
    log "Stashing local runtime-data changes before pull…"
    git stash push -u -m "autoupdate: local runtime data" || fail "git stash push failed — aborting before touching origin (working tree left as-is)"
    STASHED=true
    log "Stash push OK — local runtime data stashed as $(git stash list | head -1)"
fi

# Matches the network-failure classes actually seen in the wild (SSL
# syscall errors, DNS failures, timeouts, connection resets, 5xx from
# GitHub) — NOT git's own --ff-only rejection or an auth failure, which
# would just fail identically on a retry and shouldn't waste 5s pretending
# otherwise.
_is_transient_git_error() {
    echo "$1" | grep -qiE "SSL_ERROR_SYSCALL|Could not resolve host|Failed to connect|Connection timed out|Connection reset|Recv failure|Empty reply from server|Operation timed out|The requested URL returned error: 5|early EOF"
}

log "Pulling origin/main…"
PULL_OUTPUT=$(git pull --ff-only origin main 2>&1)
PULL_STATUS=$?

if [ $PULL_STATUS -ne 0 ] && _is_transient_git_error "$PULL_OUTPUT"; then
    log "git pull failed with a transient-looking error — retrying once in 5s… ($PULL_OUTPUT)"
    sleep 5
    PULL_OUTPUT=$(git pull --ff-only origin main 2>&1)
    PULL_STATUS=$?
    [ $PULL_STATUS -eq 0 ] && log "Retry succeeded"
fi

if [ $PULL_STATUS -ne 0 ]; then
    # Invariant 1: restore the stash (if any) so this run leaves the
    # working tree exactly as it found it, then fail loudly. Never drop the
    # stash and never proceed past this point on a failed pull.
    if [ "$STASHED" = true ]; then
        log "Pull failed — restoring stash before aborting…"
        if git stash pop >/dev/null 2>&1; then
            log "Stash restored cleanly after failed pull"
        else
            log "ERROR: could not restore stash after failed pull — stash entry LEFT IN PLACE (run 'git stash list' / 'git stash show -p stash@{0}' to inspect and recover manually)"
        fi
    fi
    fail "git pull failed: $PULL_OUTPUT"
fi
log "git pull: $PULL_OUTPUT"

if [ "$STASHED" = true ]; then
    log "Restoring stashed local runtime data…"
    if git stash apply >/dev/null 2>&1; then
        git stash drop >/dev/null 2>&1
        log "Stash applied and dropped cleanly"
    else
        # Invariant 2: never paper over a post-pull restore failure with a
        # warning-and-continue — the working tree may now hold conflict
        # markers, and proceeding into cache-bump/commit/build below could
        # commit that mess. Fail loudly instead; both the pulled HEAD
        # (already advanced above) and the untouched stash entry survive
        # this run for manual recovery.
        fail "could not restore stash after a successful pull — stash entry LEFT IN PLACE (run 'git stash list' / 'git stash show -p stash@{0}' to inspect; the pull itself succeeded, HEAD is already at the new commit). Working tree may have conflict markers — resolve manually before the next run."
    fi
fi

# --- Staleness check: has the INSTALLED app actually been built from HEAD? -
# Bug fix: this used to skip the rebuild whenever `git pull` reported "Already
# up to date". That only tells you local == origin — it says nothing about
# whether /Applications/HUGO.app was ever built from that commit. In this
# workflow, commits routinely land in this exact working tree via `git commit
# && git push` run directly here (not fetched from elsewhere), so local is
# essentially always "up to date" with origin even when dozens of unbuilt
# commits are sitting in HEAD. Track what commit was actually INSTALLED
# instead of inferring it from pull output.
CURRENT_COMMIT=$(git rev-parse HEAD)
LAST_BUILT_COMMIT=""
[ -f "$APP_VERSION_FILE" ] && LAST_BUILT_COMMIT=$(cat "$APP_VERSION_FILE")

# HUGO_FORCE_UPDATE=1 (the manual "Actualizar Sistema" button) always
# bypasses this check unconditionally, same as the guards above — the whole
# point of "bulletproof" is that clicking the button always performs a real
# rebuild, never a no-op, regardless of what we think is already installed.
if [ "$FORCED" = false ] && [ -n "$LAST_BUILT_COMMIT" ] && [ "$CURRENT_COMMIT" = "$LAST_BUILT_COMMIT" ]; then
    log "installed app already built from current commit ($CURRENT_COMMIT) — nothing to rebuild"
    echo "HUGO actualizada correctamente"
    exit 0
fi
log "Rebuild needed — current=$CURRENT_COMMIT last_installed=${LAST_BUILT_COMMIT:-none}"

# --- Cache bust: bump the Service Worker version before every real rebuild -
# Every rebuild this script actually performs means the installed app is
# about to change, so bump ui/sw.js's CACHE key first — its own activate
# handler purges any previously cached frontend once the version changes
# (see ui/sw.js). Commits and pushes immediately so the bump is permanent
# history, not just a local working-tree edit that could get swept up in
# the stash dance on the *next* run.
SW_JS="$REPO/ui/sw.js"
CACHE_DECL=$(grep -oE "const CACHE = 'jarvis-v[0-9]+'" "$SW_JS")
CURRENT_SW_VERSION=$(echo "$CACHE_DECL" | grep -oE "[0-9]+")
if [ -z "$CURRENT_SW_VERSION" ]; then
    fail "could not read current cache version from $SW_JS"
fi
NEW_SW_VERSION=$((CURRENT_SW_VERSION + 1))
sed -i '' "s/const CACHE = 'jarvis-v${CURRENT_SW_VERSION}'/const CACHE = 'jarvis-v${NEW_SW_VERSION}'/" "$SW_JS" \
    || fail "could not write new cache version to $SW_JS"
log "Cache bust: jarvis-v${CURRENT_SW_VERSION} -> jarvis-v${NEW_SW_VERSION}"

git add "$SW_JS"                                  || fail "git add ui/sw.js failed"
git commit -m "Auto: cache bust on update"        || fail "git commit (cache bust) failed"
log "Committed cache-bust"
git push origin main                              || fail "git push (cache bust) failed"
log "Pushed cache-bust to origin/main"

# HEAD just moved forward by the cache-bust commit above — re-capture it so
# APP_VERSION_FILE at the end of this script reflects what was ACTUALLY
# built, not the pre-bump commit. Getting this wrong would leave the
# unattended LaunchAgent run permanently seeing a mismatch and rebuilding
# (and re-bumping) again on every single check, forever.
CURRENT_COMMIT=$(git rev-parse HEAD)

# --- Build --------------------------------------------------------------
# --dir skips DMG/zip packaging (which needs a bare `python` binary this
# machine doesn't have) and just produces the .app bundle, which is all an
# in-place reinstall needs.
log "Running npm install…"
npm --prefix "$ELECTRON_DIR" install --no-audit --no-fund \
    || fail "npm install failed"
log "npm install complete"

log "Running npm run build…"
CSC_IDENTITY_AUTO_DISCOVERY=false npm --prefix "$ELECTRON_DIR" run build -- --dir \
    || fail "npm run build failed"
log "npm run build complete"

[ -f "$BUILT_APP/Contents/Info.plist" ] \
    || fail "build did not produce a valid app bundle at $BUILT_APP"
log "Build produced a valid app bundle at $BUILT_APP"

# --- Install --------------------------------------------------------------
# /Applications itself isn't writable by this user account (not in the admin
# group), but the HUGO.app directory already inside it is, since it was
# installed by this same user — so replace Contents/ in place rather than
# swapping the top-level bundle.
log "Installing to ${INSTALLED_APP}…"
if [ -d "$INSTALLED_APP" ]; then
    rm -rf "$INSTALLED_APP/Contents" || fail "could not remove old app contents"
    cp -R "$BUILT_APP/Contents" "$INSTALLED_APP/Contents" || fail "could not install new app contents"
else
    cp -R "$BUILT_APP" "$INSTALLED_APP" \
        || fail "could not install HUGO.app to /Applications (no existing bundle to update in place)"
fi
xattr -cr "$INSTALLED_APP" 2>/dev/null

# Confirmed-successful-install check before writing APP_VERSION_FILE — a
# minimal but real verification (not just trusting cp's exit code): the
# installed bundle must actually contain the Info.plist we just built.
[ -f "$INSTALLED_APP/Contents/Info.plist" ] \
    || fail "install did not produce a valid app bundle at $INSTALLED_APP — APP_VERSION_FILE left untouched"
log "Install confirmed — $INSTALLED_APP/Contents/Info.plist present"

# Written ONLY here, after every step above has confirmed success — this
# file is the single source of truth for "what commit is genuinely
# installed right now", read back by this script's own staleness check
# above on the next run.
echo "$CURRENT_COMMIT" > "$APP_VERSION_FILE" \
    || log "WARNING: could not save $APP_VERSION_FILE — next run may rebuild unnecessarily"
log "Recorded installed version: $CURRENT_COMMIT"

log "HUGO actualizada correctamente"
echo "HUGO actualizada correctamente"
