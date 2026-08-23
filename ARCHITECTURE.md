# JarvisLite Architecture

Quick orientation for working in this repo without having to explore it from
scratch. JarvisLite is "LIRA" — a voice assistant with three switchable
personalities (Jarvis / Friday / LIRA), Vosk speech recognition, Kokoro/macOS
TTS, a Flask+SocketIO web HUD, and an Electron desktop shell.

## Process layout

Three long-running Python processes, plus the Electron shell:

- **`jarvis.py`** — the actual assistant: starts the mic listener
  (`core/listener.py`), the Flask/SocketIO app (`core/server.py`), and a
  watchdog file-watcher that hot-reloads `core/` modules on save during
  development (see `_MODULE_MAP` — every `core/*.py` file that should
  hot-reload must have an entry there, or an edit needs a full restart).
- **`launcher.py`** — process controller on port 8079. Spawns/monitors/
  restarts `jarvis.py` as a child process, serves `ui/` as static files,
  requests mic permission, exposes health/status/build endpoints the
  Electron shell and the HUD's Ajustes tab poll.
- **`scripts/reflective_mode.py`** — standalone entry point for
  `core/sleep.py`'s 8-phase Sleep System and `core/reflective.py`'s
  lightweight idle consolidation, runnable via `launchd` even while the app
  is fully closed, or spawned as a child process by `jarvis.py` when idle.
- **`electron/`** — desktop shell (`main.js`/`preload.js`) that starts
  `launcher.py` and loads its served page in a window. `ui/index.html` and
  `ui/sw.js` are NOT bundled into the app — they're served live from this
  repo checkout on every load (see `main.js`'s comments and `launcher.py`'s
  `GET /api/version`).

## `core/` — backend modules

Split for token efficiency: `commands.py` used to be one ~4500-line file;
it's now four files with a one-directional import graph
(`commands.py` → `personality.py`/`memory.py`/`intent.py`; the reverse
direction only ever happens through a function-local
`import core.commands as commands`, to dodge a circular import — see each
file's own module docstring).

| File | Owns | Key functions |
|---|---|---|
| `commands.py` | Main dispatch loop, Groq completion calls, conversation history buffer, intent→action→response pipeline, web search, static offline fallback, proactive messages, reminders, the Sleep System's idle-trigger thread, HUD co-pilot. | `dispatch_command()`, `_dispatch_command_impl()`, `_groq_complete()`, `_execute_action()`, `_format_response()`, `_static_fallback()`, `on_user_activity()` |
| `memory.py` | Four-layer memory persistence: Layer 1/2 facts (`data/memory_shared.json`, `data/memory_<personality>.json`), Layer 3 instructions (`data/memory_instructions.json`), feature flags, episodic memory, concepts/armor summaries, memory health + weekly consolidation. | `_load_fact_file()`/`_upsert_fact()`, `_select_relevant_facts()`, `_extract_and_save_memory()`, `_extract_episodes_for_session()`, `get_feature_flags()`/`set_feature_flag()`, `get_memory_stats()`/`clean_all_memory()`/`get_active_memory()` |
| `personality.py` | `PERSONALITIES` character definitions, active-personality state (`_personality`), personality switching, and `_build_system_prompt()` — the single place every context layer (instructions, live data, temporal gap, memory, episodes, tone, sleep insights) gets assembled into one prompt string. | `_build_system_prompt()`, `_switch_personality()`, `_detect_personality_switch()`, `_build_contexto_temporal()` |
| `intent.py` | Local-regex intent detection (no Groq call): time/date, volume, open-app, Calendar read/write/confirm, listen-mode switch, diamond-move commands, web-search gating, tone detection, implicit-context inference. | `_detect_intent()`, `_detect_tone()`, `_infer_implicit_context()`, `_parse_event_date()`/`_parse_event_time()` |
| `server.py` | Flask + SocketIO app (port 8080). Every HTTP/socket endpoint the frontend calls. | `emit_status()`, `emit_diamond_move()`, route handlers under `/api/*` |
| `listener.py` | Vosk mic capture loop, wake-word detection, personality-name routing, conversation mode. | `listen()`, `_match_wake_word()`, `_run_dispatch()` |
| `voice.py` | TTS output — Kokoro (per-personality voices) with macOS `say` fallback, breath-pause insertion, cooldown after speaking. | `speak_kokoro()`/`speak_kokoro_lira()`/`speak_kokoro_friday()`, `speak()` |
| `tools.py` | Live data only — time/date/location/weather/volume/apps/Calendar/web search/math. Never returns cached or trained-knowledge data. | `get_weather()`, `get_location()`, `search_web()`, `evaluate_math()` |
| `sleep.py` | The Sleep System: 8-phase autonomous maintenance (memory cleanup, fact promotion, insight generation, mind-map updates). Dependency-light on purpose (no `core.commands`/`core.voice`) so it can run standalone via `launchd`. | `run_continuous_sleep()`, `get_sleep_summary()`, `get_status()` |
| `reflective.py` | Lightweight idle-time consolidation pass, shared by both the "app open" idle trigger and the standalone `launchd` job. | `run_reflective_session()` |
| `speaker.py` | Speaker verification (SpeechBrain) — currently disabled (`SPEAKER_VERIFICATION_ENABLED = False`); loads with zero cost when off. | — |

**Hot-reload note:** `jarvis.py`'s `_MODULE_MAP` lists which `core/*.py`
stems get `importlib.reload()`'d on save. Cross-module references inside
`core/` therefore go through the *module object* (`memory.foo()`, not
`from core.memory import foo`) so a reload is picked up live instead of
leaving another module holding a stale function reference — same pattern
already used for `from core import tools`.

## `ui/` — frontend (single-page app, no build step)

Split out of one ~11,600-line `index.html` for the same token-efficiency
reason as `core/commands.py`:

- **`index.html`** — HTML structure only; links `styles.css` and `app.js`.
- **`styles.css`** — all CSS, already organized into `═══` banner sections
  (variables, boot splash, main HUD, modals, etc.).
- **`app.js`** — all JavaScript, already organized into `═══` banner
  sections (config, launcher socket, boot state machine, dispatch, HUD
  tabs, mind map, diamond positioning, ...).
- **`sw.js`** — service worker. Network-first for navigation (`/`, i.e.
  `index.html`) so an update is never served stale; cache-first for
  `styles.css`/`app.js`/icons/manifest (bump the `CACHE` version constant
  whenever their content changes, same as `rebuild_app.sh` already does on
  every build).

Served by both `launcher.py` and `core/server.py` via
`Flask(static_folder="ui", static_url_path="")` — any file added under
`ui/` is automatically available at `/<filename>`.

## Data flow (one voice command, happy path)

1. `core/listener.py` — Vosk transcribes audio, detects the wake word /
   active personality, hands the transcript to `dispatch_command()`.
2. `core/commands.py: dispatch_command()` — bookkeeping (busy flag, last-
   interaction time, pending reminders), then `_dispatch_command_impl()`.
3. Mode-switch / diamond-move / personality-switch checks
   (`core/intent.py`, `core/personality.py`) — short-circuit if matched.
4. `core/intent.py: _detect_intent()` — local regex classification.
5. `core/personality.py: _build_system_prompt()` — assembles the full
   prompt: persona + Layer 3 instructions (`core/memory.py`) + live data
   (`core/tools.py`) + temporal gap + relevant facts/episodes
   (`core/memory.py`) + implicit context (`core/intent.py`) + tone.
6. `core/commands.py: _groq_complete()` — walks `GROQ_MODEL_CHAIN`,
   streams the reply; `_static_fallback()` if every tier fails.
7. History (`_add_history`), memory extraction
   (`core/memory.py: _extract_and_save_memory()`), reminder detection all
   run; `core/voice.py` speaks the reply.
8. `core/server.py` broadcasts status/events to `ui/app.js` over SocketIO
   throughout, so the HUD reflects state (listening/processing/speaking,
   panels, thinking feed, diamond position) in near-real-time.

## Persisted state (`data/*.json`)

Runtime data the app reads/writes as it operates — not configuration you'd
normally hand-edit except `memory_instructions.json` (Layer 3, meant to be
curated) and `feature_flags.json`/`mode_config.json` (toggled via the HUD).
Everything else (`memory_shared.json`, `memory_<personality>.json`,
`episodes.json`, `session_state.json`, `sleep_budget.json`,
`sleep_insights.json`, `concepts.json`, `mind_map_connections.json`,
`reminders.json`, `reflective_budget.json`) is written by the app itself
during normal use — expect these to show as modified in `git status` simply
from having run the assistant.

## Running/restarting the app during development

**The real app is the Electron shell — `LIRA.app`** (a copy lives at both
`~/Desktop/LIRA.app` and `/Applications/LIRA.app`; either is fine, they're
the same build). It has the correct gold-diamond icon and is what spawns
`launcher.py` → `jarvis.py` as child processes on open.

There used to *also* be a `~/Applications/Jarvis.app` — a leftover
Safari "Add to Dock" web-app shortcut pointed at `localhost:8079`, from
an early dev workflow, with a generic/wrong icon and no relation to the
real Electron build. It caused real confusion (two similarly-purposed
apps, one broken-looking) and was removed. If something like it
reappears, delete it — `LIRA.app` is the only app that should exist for
this project.

**Editing `ui/` files does not update a window that's already open.**
`LIRA.app`'s window loads `http://localhost:8079` once at launch and
then behaves like any browser tab — it keeps running whatever JS/CSS it
already fetched, same as a page you haven't refreshed. The *server*
serves new files immediately (confirm with
`curl -s http://localhost:8079/js/whatever.js | grep ...` or similar),
but the *running window* won't reflect them until it actually reloads:

- Lightest: the in-app **Sistema → Reload** button (bumps
  `sw_cache_increment` in `localStorage` then does a hard reload) —
  works if you can click inside the app.
- From the shell, without Accessibility permission for
  keystroke-injection (`osascript`/System Events sending Cmd+R will
  fail with "not permitted to send keystrokes" otherwise): fully
  restart the process —
  `pkill -f "LIRA.app/Contents/MacOS/LIRA"` (this also kills
  `launcher.py`/`jarvis.py`, its children) then
  `open ~/Desktop/LIRA.app` (or the `/Applications` copy) to relaunch
  everything fresh. Wait ~20s for `jarvis.py` to report ready in
  `logs/launcher.log` before assuming something's broken.

`clear_pwa_cache.sh` (repo root) targets the OLD Safari-WebApp shortcut
above and is no longer relevant now that it's gone — don't reach for it
to "restart the app"; use the process-restart steps above instead.

If a change genuinely doesn't seem to be taking effect, check this before
assuming the code is wrong — a stale already-open window has been the
actual cause more than once.
