#!/usr/bin/env bash
# clear_pwa_cache.sh — Kill the Jarvis Dock PWA, nuke its WebKit caches, relaunch.
# Run with: bash clear_pwa_cache.sh
set -euo pipefail

# Bug fix: this used to be a hardcoded UUID (6872ADD0-D2E0-4EFC-B59A-
# 3AA64E3D17E7) that silently stopped matching the real running app at
# some point (re-adding Jarvis.app to the Dock mints a NEW container
# UUID) — every step below depends on this value, so the pkill in step 1
# was quietly matching nothing ("not running" every time, even though the
# app very much was), and steps 2-4 were clearing a container directory
# that didn't correspond to the live app at all. Net effect: this script
# had been a complete no-op against the actual running instance for an
# unknown stretch of time — every "cache cleared, relaunching" run still
# left the OLD in-memory JS/CSS running, un-killed, un-reloaded.
# Detected fresh every run now, from whichever Web App process is
# ACTUALLY running with --bundlepath pointing at this Jarvis.app (falls
# back to the last-known UUID only if nothing is currently running, purely
# so the cache-path steps below still have something to point at).
JARVIS_UUID="$(ps aux | grep "Web App.app/Contents/MacOS/Web App" | grep -- "--bundlepath ${HOME}/Applications/Jarvis.app" | grep -oE 'bundleidentifier com\.apple\.Safari\.WebApp\.[A-F0-9-]+' | head -1 | sed 's/.*WebApp\.//')"
if [ -z "${JARVIS_UUID}" ]; then
    JARVIS_UUID="6872ADD0-D2E0-4EFC-B59A-3AA64E3D17E7"
    echo "  (no running Jarvis Web App process found — falling back to last-known container UUID)"
else
    echo "  (detected live container UUID: ${JARVIS_UUID})"
fi
CONTAINER=~/Library/Containers/com.apple.Safari.WebApp/Data/Library/Containers/com.apple.Safari.WebApp.${JARVIS_UUID}
TOP_CACHE=~/Library/Containers/com.apple.Safari.WebApp/Data/Library/Caches/com.apple.Safari.WebApp
JARVIS_APP=~/Applications/Jarvis.app

echo "═══════════════════════════════════════════"
echo " Jarvis PWA cache nuke"
echo "═══════════════════════════════════════════"

# ── 1. Kill the running Web App process ─────────────────────────────────────
echo "[1/5] Killing Jarvis Web App process…"
pkill -f "bundleidentifier com.apple.Safari.WebApp.${JARVIS_UUID}" 2>/dev/null && echo "      killed" || echo "      not running"
sleep 1  # let macOS release file locks

# ── 2. Nuke the HTTP network cache (caches index.html, sw.js, etc.) ─────────
echo "[2/5] Clearing HTTP NetworkCache…"
if [ -d "${CONTAINER}/Library/Caches/WebKit/NetworkCache" ]; then
    rm -rf "${CONTAINER}/Library/Caches/WebKit/NetworkCache"
    echo "      done"
else
    echo "      (already empty)"
fi

# ── 3. Nuke the Service Worker Cache API storage (the jarvis-vN buckets) ────
echo "[3/5] Clearing Service Worker CacheStorage…"
find "${CONTAINER}/Library/WebKit/WebsiteData/Default" \
    -type d -name "CacheStorage" -exec rm -rf {} + 2>/dev/null && echo "      done" || echo "      (already empty)"

# ── 4. Nuke the Service Worker registration + scripts (forces re-install) ───
echo "[4/5] Clearing Service Worker registration…"
find "${CONTAINER}/Library/WebKit/WebsiteData/Default" \
    -type d -name "ServiceWorkers" -exec rm -rf {} + 2>/dev/null && echo "      done" || echo "      (already empty)"

# ── 5. Nuke the top-level shared Safari.WebApp HTTP cache ───────────────────
echo "[5/5] Clearing top-level Cache.db…"
if [ -f "${TOP_CACHE}/Cache.db" ]; then
    rm -f "${TOP_CACHE}/Cache.db" "${TOP_CACHE}/Cache.db-shm" "${TOP_CACHE}/Cache.db-wal"
    echo "      done"
else
    echo "      (already empty)"
fi

echo ""
echo "All caches cleared. Relaunching Jarvis PWA…"
sleep 0.5
open "${JARVIS_APP}"
echo "Done — the PWA will fetch fresh assets on startup."
