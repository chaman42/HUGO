# HUGO — Session Notes, 2026-08-23 → 2026-08-24 (late night)

Handoff doc for tomorrow. Read this before doing anything else — it covers what
changed, what's running right now, and what's still open.

---

## 0. Current running state (as of end of session)

- HUGO is running in **dev mode** via a terminal (`cd electron && npm start`),
  NOT the installed `/Users/joanreyes/Desktop/HUGO.app`. That packaged app still
  has last week's code baked into its `app.asar` — **do not double-click it**,
  it will hang on boot (stale Tailscale URL, old ports). See §4.
- Ports: launcher `8179`, backend `8180` (moved off LIRA's default `8079`/`8080`
  so both apps can run at once — confirmed working side by side tonight).
- If you restart your Mac / close the terminal, HUGO will NOT come back on its
  own. To relaunch: `cd ~/Desktop/HUGO/electron && npm start`.

---

## 1. Joan/Dani identity system (big one — re-architected tonight)

HUGO now assumes **Dani is the default user**, not Joan. Rationale: Joan built
this for Dani; Joan's own use is the admin/testing exception.

- Any device HUGO has never seen before now defaults to **Dani's** profile
  (not a generic "stranger", not Joan). Both `joan` and `dani` are seeded in
  the built-in defaults (`core/social.py`), so this holds on a **fresh
  install** too — i.e. the copy Dani will actually download.
- Joan must be explicit about his own devices: pre-register via the Personas
  tab, or speak/type the **identity override code** (currently **not
  configured** — see §7, action item).
- Saying the identity code once now also **permanently registers** that
  device as Joan's, so it doesn't need repeating.
- "Creator authority" — only Joan (not Dani, regardless of trust level) can
  make HUGO actually execute consequential actions (calendar, reminders,
  opening apps, starting investigations). Dani can ask; HUGO won't do it for
  him, and says so naturally instead of a permission-denied message.
- The Personas tab (`/api/social/*`) is now Joan-only server-side — Dani
  never sees trust levels or notes about himself or Joan.
- The autonomous sleep-time "exploraciones" (curiosity search) feature now
  **skips itself entirely when Joan is the identified speaker** — his testing
  chats won't pollute Dani's exploration history anymore. `data/explorations.json`
  was wiped clean tonight to remove Joan's existing test pollution.
- Full design rationale saved in Claude's memory
  (`project_hugo_joan_dani_identity`) for future sessions to pick up
  automatically.

**Known gap:** when Dani actually gets his own device, whatever device it
connects from will auto-become "his" the first time HUGO sees it (correct
behavior) — but if he ever tests from multiple browsers/devices before Joan
looks at the Personas tab, each *could* independently look like "Dani" since
they're all just folded into the one `dani` profile — this is intentional
(no per-device distinction within "Dani"), just flagging it's not multi-Dani
aware.

---

## 2. Voice: switched to edge-tts

- Primary TTS engine is now **Microsoft edge-tts**, voice `es-ES-AlvaroNeural`
  (male, Spain Spanish). Falls back to macOS `say` automatically on any
  failure (no network, package issue, timeout).
- **Requires internet** — this is a real trade-off vs. the old fully-offline
  `say`. Fallback covers outages gracefully but voice quality drops to the
  old system voice when offline.
- Fixed a real bug along the way: HUGO used to speak each sentence as it
  streamed from the LLM (great for `say`, near-instant local synthesis).
  edge-tts needs a network round trip per sentence, so that turned every
  sentence boundary into several seconds of dead air ("breathing pause way
  too long"). Fixed by only streaming per-sentence for engines that actually
  benefit from it (`core.voice.supports_chunked_streaming()` — False for
  edge-tts); edge-tts replies are now synthesized and spoken once, complete.
- **New: "repeat that" replay button.** Every edge-tts reply's audio is now
  cached (`data/tts_cache/`, capped at 30 most recent) instead of deleted —
  a small 🔊 button appears next to each HUGO chat bubble once ready, click
  to replay that exact audio instantly. (No button for `say`-fallback
  replies — no persisted audio for that engine.)
- Added `edge-tts`/`tabulate` to `requirements.txt`.

---

## 3. Personality: academic teaching mode + Dani awareness

- HUGO now has a real inclination toward academic help — but more
  specifically, when a concept has actual substance (not a one-line
  definition), **HUGO now teaches like a good teacher**: breaks the
  explanation into digestible parts, gives concrete examples, stops to ask a
  genuine comprehension-check question, and **actually waits for the answer**
  before continuing (no state machine needed — this just works through
  normal conversation turns/history). Verified live multiple times tonight
  (recursion, trigonometry) — works well, including recovering gracefully
  when the answer is wrong.
- Added an explicit "Dani" paragraph to HUGO's personality — same character
  with Dani as with Joan, just less accumulated-familiarity warmth, and
  never fabricates shared history/inside references that don't exist with
  him.
- **Bug fixed**: `core/personalities/hugo.py` and `base.py` live in a
  subdirectory the hot-reload watcher wasn't actually watching
  (`recursive=False`) — personality edits silently needed a full restart.
  Fixed (now `recursive=True` + correct reload chain hugo → base →
  personality → commands).

---

## 4. Packaged `.app` is stale — needs a rebuild

`/Users/joanreyes/Desktop/HUGO.app` still has pre-tonight code. To fix
properly:

```
cd ~/Desktop/HUGO/electron && npm run build
```

then replace the old `HUGO.app` with the freshly built one (check
`electron/dist/` for the output). This wasn't done tonight — deferred as
lower priority than getting features working in dev mode. **Do this before
relying on double-click-to-launch again.**

---

## 5. Removed dead UI

- The **CONTROL** tab (placeholder, "Estadísticas, ADN de personalidades...
  Próximamente" — never built) is gone from the app launcher entirely.
- A stray **"DISEÑANDO..."** indicator was always-visible in the bottom bar —
  leftover from the already-removed Armor Design Studio subsystem, referenced
  a JS file that no longer existed so nothing ever hid it. Removed.
- Bumped `ui/sw.js`'s cache version twice for these (`v181`, `v182`) — if you
  ever see stale UI after an edit, that's the mechanism: bump `CACHE` in
  `ui/sw.js`, or just fully quit+relaunch.

---

## 6. Two "run both HUGO and LIRA at once" fixes

- Ports moved to 8179/8180 (was clashing with LIRA's 8079/8080) — confirmed
  both running simultaneously tonight, no clash.
- **Real bug found and fixed**: HUGO's own startup/shutdown cleanup
  (`core/port_cleanup.py`, and Electron's `restart-backend` handler) used to
  match processes **by filename only** (`jarvis.py`, `launcher.py`) — since
  LIRA runs identically-named scripts, HUGO's own cleanup would have killed
  LIRA's process on every HUGO startup even with the ports fixed. Now scoped
  to this repo's own absolute path only.

---

## 7. Action items for tomorrow (or whenever)

1. **Set the identity override code** — currently unconfigured, meaning
   there's no way for Joan to prove he's "Joan" from a device that isn't
   pre-registered (e.g. sitting at Dani's computer):
   ```
   curl -X POST localhost:8180/api/social/identity_code -H 'Content-Type: application/json' -d '{"code":"your phrase here"}'
   ```
2. **Rebuild the packaged `.app`** (§4) so normal double-click launching
   works again.
3. **Remote update infrastructure** — deferred to a future session (see
   Claude's memory `project_remote_update_setup`), but tonight turned up more
   detail worth knowing: `.github/workflows/release.yml` and
   `scripts/rebuild_app.sh` already exist and look fully built out — but
   `git remote -v` in this repo returns **nothing at all**, and
   `gh repo view chaman42/HUGO` 404s even though `gh` is properly
   authenticated as `chaman42` with repo-write scope. So the GitHub repo
   this all assumes either was never created or no longer exists. The
   README's "Local Auto-Update" LaunchAgent
   (`com.joan.hugo.autoupdate.plist`) also isn't actually installed on this
   machine, and no install script/plist template for it exists in
   `scripts/` (unlike the Discord bridge, which has one). In short: the
   whole update pipeline is designed and coded but was never actually wired
   up to a live remote. First real step whenever this gets picked up:
   confirm whether `chaman42/HUGO` should be created fresh or if it's
   supposed to exist under a different name.
4. Minor/non-urgent: `core/ollama_control.py` still references a
   `POST /api/designs/autopilot-start` route that doesn't exist anywhere
   (dead code left over from the same removed Armor Design Studio
   subsystem as §5) — harmless, just noise if you go looking.
5. Minor/non-urgent: tried to purge ~191 stale "exploration" embeddings from
   the Chroma vector index (`data/chroma/`) to match the wiped
   `explorations.json`, but hit a pre-existing `numpy`/`chromadb` circular
   import bug in the venv when running a standalone script — unrelated to
   tonight's changes, unresolved. The JSON source is clean either way, so
   this is just stale semantic-search noise, not a functional bug.

---

## 8. Files touched tonight (for reference)

Backend: `core/social.py`, `core/commands.py`, `core/voice.py`,
`core/server.py`, `core/routes_control.py`, `core/routes_social.py`,
`core/personalities/hugo.py`, `core/personalities/base.py`,
`core/sleep_curiosity_search.py`, `core/port_cleanup.py`,
`core/process_manager.py`, `core/api_routes.py`, `jarvis.py`, `launcher.py`,
`requirements.txt`.

Frontend: `ui/index.html`, `ui/js/bootstrap-auth.js`, `ui/js/chat-render.js`,
`ui/js/connection.js`, `ui/js/clock-boot-splash-wiring.js`,
`ui/js/core-tabs-sleep-panel.js`, `ui/js/diamond-text-launcher.js`,
`ui/css/chat.css`, `ui/css/concepts.css`, `ui/sw.js`, `ui/manifest.json`.

Electron: `electron/main.js`, `electron/backend_process.js`,
`electron/tray.js`, `electron/preload.js`.

Data: `data/social_profiles.json` (joan + dani seeded, devices registered),
`data/explorations.json` (wiped), `capacitor.config.json`,
`ios/App/App/Info.plist`.

Nothing has been committed to git yet — everything above is uncommitted
working-tree changes.

---

Sleep well. 🌙
