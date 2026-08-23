# JarvisLite

Multi-personality voice assistant — local Vosk STT, Groq LLM, macOS TTS — with an Electron desktop app (LIRA).

---

## Automated Release Pipeline

Every push to `main` triggers a GitHub Actions workflow (`.github/workflows/release.yml`) that:

1. Bumps the **patch version** in `electron/package.json` (e.g. `1.0.0 → 1.0.1`)
2. Builds a **universal macOS DMG + ZIP** (x64 + arm64, no code signing required)
3. Creates a **GitHub Release** tagged `v<version>` and uploads the DMG, ZIP, and `latest-mac.yml`
4. Commits the version bump back to `main` (with `[skip ci]` to avoid an infinite loop)

The installed LIRA app *attempts* to check this repository's Releases on every launch via `electron-updater`, but since this repo is **private** and the shipped app carries no GitHub credentials, that check always fails with a 404 — the built-in updater can never actually succeed here. See "Local auto-update" below for how the installed app is actually kept current.

### Required secret

**`GITHUB_TOKEN`** — this secret is automatically available in every GitHub repository. No manual configuration is needed. The workflow uses it to create releases, upload assets, and push the version bump commit.

### Skipping code signing

The build runs with `CSC_IDENTITY_AUTO_DISCOVERY=false`, which skips Apple code signing. On first launch, macOS will show a Gatekeeper warning. Right-click → Open to bypass it once; subsequent launches are unaffected.

---

## Local Auto-Update

Because `electron-updater` can't reach this private repo's Releases (see above), `/Applications/LIRA.app` is kept current by a local rebuild instead:

- **`scripts/rebuild_app.sh`** — pulls the latest `main`, builds the app locally (`npm run build -- --dir`, skipping DMG packaging), and replaces `/Applications/LIRA.app` in place. Skips cleanly (no error) if there's no internet connection or if LIRA is currently running, so it never disrupts an active session. Prints `LIRA actualizada correctamente` on success.
- **`com.joan.lira.autoupdate`** — a LaunchAgent (`~/Library/LaunchAgents/com.joan.lira.autoupdate.plist`) that runs the script every 6 hours. It only fires while the Mac is awake (launchd timers don't tick during sleep) and only does real work when there's an internet connection (checked inside the script). Output is logged to `logs/autoupdate.log`.

  It invokes the script through `python3.11` rather than `/bin/bash` directly — `/bin/bash` has no macOS TCC grant for the Desktop folder, which is why the *other* LaunchAgent in this repo (`com.joan.jarvislite.autopush`, `logs/autopush.log`) has been silently failing on every run with `fatal: not a git repository`. `python3.11` already holds that grant on this machine, and a bash child process it spawns inherits it.

To trigger an update manually at any time (e.g. right after pushing a fix), just run:

```
~/Desktop/JarvisLite/scripts/rebuild_app.sh
```

---

## Discord Bridge

LIRA answers DMs on Discord independently of the Electron app — you don't need LIRA.app open at all, just the Mac awake and logged in.

### Setup (one-time)

```
sudo pmset -a womp 1                              # allow network activity to wake the Mac
scripts/install_discord_launchd.sh                 # installs + starts the background service
```

`install_discord_launchd.sh` installs `com.lira.discord` as a `launchd` LaunchAgent (`~/Library/LaunchAgents/com.lira.discord.plist`, generated from the `scripts/com.lira.discord.plist` template with the real repo path substituted in). It starts `core/discord_bridge.py` at login and restarts it automatically on crash (`KeepAlive`) or after the Mac wakes from sleep. Logs go to `logs/discord_bridge.log` / `logs/discord_bridge_error.log`.

To remove it: `scripts/uninstall_discord_launchd.sh`

### What `womp` actually does — and doesn't

`pmset womp` ("wake on network access") only wakes the Mac for **local-network** traffic — Wake-on-LAN magic packets, Bonjour/AFP/SMB probes from another device on the same LAN. It does **not** mean an incoming Discord DM can wake a fully-sleeping Mac: Discord's servers have no path to your machine's hardware once it's asleep, so a DM sent while the Mac is asleep just waits undelivered-to-the-bot until the Mac wakes up some other way (lid open, scheduled wake, a LAN wake packet from a device on the same network). `womp` matters here only insofar as it keeps the Mac reachable/wakeable on the LAN for the other tools in this repo — it's not sufficient on its own for "text LIRA and the Mac wakes up."
