#!/bin/bash
#
# build_ios.sh — build LIRA's Capacitor-wrapped frontend into a sideloadable
# .ipa (for SideStore) from the current ui/ source.
#
# Pipeline: npx cap sync ios -> xcodebuild archive -> xcodebuild -exportArchive
# Output:   ios/App/ipa/LIRA.ipa
#
# NOTE ON PROJECT LAYOUT: this Capacitor version (7.x) generates the iOS
# platform using Swift Package Manager, not CocoaPods — there is no
# ios/App/App.xcworkspace, only ios/App/App.xcodeproj. Every xcodebuild call
# below therefore uses -project, not -workspace. If a future `cap sync`
# regenerates a CocoaPods-based workspace instead (e.g. after adding a
# Cordova-only plugin that requires it), switch back to -workspace here.
#
# Used two ways: manually from Terminal, or via launcher.py's
# POST /api/build_ios (the Ajustes "Compilar para iPhone" button) — see
# core/server.py for that endpoint's subprocess.run() call into this script.

set -uo pipefail

export PATH="/usr/local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"

REPO="$HOME/Desktop/JarvisLite"
IOS_DIR="$REPO/ios/App"
PROJECT="$IOS_DIR/App.xcodeproj"
SCHEME="App"
ARCHIVE_PATH="$IOS_DIR/LIRA.xcarchive"
EXPORT_PATH="$IOS_DIR/ipa"
EXPORT_OPTIONS="$REPO/ios/ExportOptions.plist"
IPA_OUT="$EXPORT_PATH/LIRA.ipa"

export GIT_DIR="$REPO/.git"
export GIT_WORK_TREE="$REPO"

BUILD_LOG="$REPO/logs/ios_build.log"
ts()  { date '+%Y-%m-%d %H:%M:%S'; }
log() {
    local line="[$(ts)] $1"
    echo "$line"
    mkdir -p "$(dirname "$BUILD_LOG")" 2>/dev/null
    echo "$line" >> "$BUILD_LOG" 2>/dev/null
}
fail() {
    local line="[$(ts)] ERROR: $1"
    echo "$line" >&2
    echo "$line" >> "$BUILD_LOG" 2>/dev/null
    exit 1
}

log "build_ios.sh starting"

# --- Preflight: a full Xcode install (not just Command Line Tools) is
# required for -exportArchive/archive to work at all. Fail fast with a clear
# message instead of letting xcodebuild's own cryptic error surface first. ---
if ! xcodebuild -version >/dev/null 2>&1; then
    fail "xcodebuild is not usable — this machine's active developer directory is Command Line Tools, not full Xcode. Install Xcode from the App Store, open it once to accept the license, then run: sudo xcode-select -s /Applications/Xcode.app/Contents/Developer"
fi

[ -d "$PROJECT" ] || fail "$PROJECT not found — run 'npx cap add ios' from $REPO first"

# --- ExportOptions.plist — created here (not committed as static state)
# so it always reflects EXPORT_PATH/method/signingStyle below in one place. ---
log "Writing $EXPORT_OPTIONS…"
mkdir -p "$(dirname "$EXPORT_OPTIONS")"
cat > "$EXPORT_OPTIONS" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>method</key>
	<string>development</string>
	<key>signingStyle</key>
	<string>automatic</string>
</dict>
</plist>
PLIST

# --- 1. Sync the current ui/ build into the native project -----------------
log "Running npx cap sync ios…"
( cd "$REPO" && npx cap sync ios ) || fail "cap sync ios failed"
log "cap sync ios complete"

# --- 2. Archive ---------------------------------------------------------
rm -rf "$ARCHIVE_PATH"
log "Archiving (xcodebuild archive)… this can take a few minutes"
xcodebuild \
    -project "$PROJECT" \
    -scheme "$SCHEME" \
    -configuration Release \
    -archivePath "$ARCHIVE_PATH" \
    archive >> "$BUILD_LOG" 2>&1 \
    || fail "xcodebuild archive failed — see $BUILD_LOG for the full log"
log "Archive complete: $ARCHIVE_PATH"

# --- 3. Export .ipa -------------------------------------------------------
mkdir -p "$EXPORT_PATH"
log "Exporting .ipa…"
xcodebuild -exportArchive \
    -archivePath "$ARCHIVE_PATH" \
    -exportPath "$EXPORT_PATH" \
    -exportOptionsPlist "$EXPORT_OPTIONS" >> "$BUILD_LOG" 2>&1 \
    || fail "xcodebuild -exportArchive failed — see $BUILD_LOG for the full log (often a signing/provisioning-profile issue: open ios/App/App.xcworkspace or App.xcodeproj in Xcode once, sign in with your Apple ID under Signing & Capabilities, and register this device)"

[ -f "$IPA_OUT" ] || fail "export reported success but $IPA_OUT is missing"
log "LIRA.ipa ready at $IPA_OUT"
echo "$IPA_OUT"
