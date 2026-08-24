# JarvisLite

Multi-personality voice assistant — local Vosk STT, Groq LLM, macOS TTS — with an Electron desktop app (HUGO).

---

## Automated Release Pipeline

Every push to `main` triggers a GitHub Actions workflow (`.github/workflows/release.yml`) that:

1. Bumps the **patch version** in `electron/package.json` (e.g. `1.0.0 → 1.0.1`)
2. Builds a **universal macOS DMG + ZIP** (x64 + arm64, no code signing required)
3. Creates a **GitHub Release** tagged `v<version>` and uploads the DMG, ZIP, and `latest-mac.yml`
4. Commits the version bump back to `main` (with `[skip ci]` to avoid an infinite loop)

The installed HUGO app checks this repository's Releases on every launch via `electron-updater` (`electron/updater.js`) and installs updates silently in the background. This works end-to-end because the repo is **public** (`chaman42/HUGO`) — no GitHub credentials are needed to read public release assets. First confirmed working with `v1.0.1`; see "Getting the app" below for how a new install (e.g. Dani's) gets the app in the first place.

### Required secret

**`GITHUB_TOKEN`** — this secret is automatically available in every GitHub repository. No manual configuration is needed. The workflow uses it to create releases, upload assets, and push the version bump commit.

### Skipping code signing

The build runs with `CSC_IDENTITY_AUTO_DISCOVERY=false`, which skips Apple code signing. On first launch, macOS will show a Gatekeeper warning. Right-click → Open to bypass it once; subsequent launches are unaffected.

---

## Getting the App

New installs (including Dani's) come from the GitHub Release, not a local build:

**[github.com/chaman42/HUGO/releases/latest](https://github.com/chaman42/HUGO/releases/latest)** — always points at the newest DMG. Download it, drag HUGO to Applications, and on first launch bypass the Gatekeeper "unidentified developer" warning once (right-click → Open) — the build is intentionally unsigned, see "Skipping code signing" above. Every launch after that is unaffected, and updates from then on install themselves via `electron-updater` (see above) with no further action needed.

## Manual Rebuild (dev machine only)

For local development — testing a change before it's pushed, or rebuilding without waiting on CI — `scripts/rebuild_app.sh` pulls the latest `main`, builds the app locally (`npm run build -- --dir`, skipping DMG packaging), and replaces `/Applications/HUGO.app` in place on *this* machine. Skips cleanly (no error) if there's no internet connection or if HUGO is currently running. Run it manually:

```
~/Desktop/HUGO/scripts/rebuild_app.sh
```

This isn't needed to keep Dani's (or any downloaded) install current — that happens automatically through GitHub Releases + `electron-updater` instead. No LaunchAgent runs this on a schedule.

---

## Discord Bridge

HUGO answers DMs on Discord independently of the Electron app — you don't need HUGO.app open at all, just the Mac awake and logged in.

### Setup (one-time)

```
sudo pmset -a womp 1                              # allow network activity to wake the Mac
scripts/install_discord_launchd.sh                 # installs + starts the background service
```

`install_discord_launchd.sh` installs `com.hugo.discord` as a `launchd` LaunchAgent (`~/Library/LaunchAgents/com.hugo.discord.plist`, generated from the `scripts/com.hugo.discord.plist` template with the real repo path substituted in). It starts `core/discord_bridge.py` at login and restarts it automatically on crash (`KeepAlive`) or after the Mac wakes from sleep. Logs go to `logs/discord_bridge.log` / `logs/discord_bridge_error.log`.

To remove it: `scripts/uninstall_discord_launchd.sh`

### What `womp` actually does — and doesn't

`pmset womp` ("wake on network access") only wakes the Mac for **local-network** traffic — Wake-on-LAN magic packets, Bonjour/AFP/SMB probes from another device on the same LAN. It does **not** mean an incoming Discord DM can wake a fully-sleeping Mac: Discord's servers have no path to your machine's hardware once it's asleep, so a DM sent while the Mac is asleep just waits undelivered-to-the-bot until the Mac wakes up some other way (lid open, scheduled wake, a LAN wake packet from a device on the same network). `womp` matters here only insofar as it keeps the Mac reachable/wakeable on the LAN for the other tools in this repo — it's not sufficient on its own for "text HUGO and the Mac wakes up."
