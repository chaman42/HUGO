// bootstrap-auth.js — Backend/launcher config, DOM element refs, top-level state, and the device-auth rejection/launcher-socket bootstrap.
// ════════════════════════════════════════════════════════════════════════════
// ESC — HTML-escape helper, used across nearly every later-loaded file's
// template rendering. Placed here (the first-loaded file) rather than in
// chat-render.js (its original single-file home) so it's guaranteed to be
// defined before any earlier-loading file's own top-level init code that
// calls it immediately (e.g. diamond-text-launcher.js's bare
// _renderAppLauncherRow() call) — a bare call to a function only defined
// in a later <script> throws ReferenceError, unlike the same call in the
// original single-file app.js where function-declaration hoisting made
// call order irrelevant. Pure refactor bug fix, no behavior change.
// ════════════════════════════════════════════════════════════════════════════
function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

// ════════════════════════════════════════════════════════════════════════════
// CONFIG
// ════════════════════════════════════════════════════════════════════════════
// Connection order: Tailscale IP → serving hostname → mDNS .local
// Each attempt has a 5 s timeout; on failure the next URL in the list is tried.
const BACKEND_URLS = [
  'http://100.124.252.100:8080',
  `http://${window.location.hostname}:8080`,
  'http://MacBook-Pro-de-Joan.local:8080',
]
let JARVIS_URL   = BACKEND_URLS[0]
let JARVIS_API   = JARVIS_URL
const LAUNCHER_API = ''                 // same origin as this page (8079)

// ════════════════════════════════════════════════════════════════════════════
// DOM REFS
// ════════════════════════════════════════════════════════════════════════════
const bootOverlay    = document.getElementById('bootOverlay')
const powerBtn       = document.getElementById('powerBtn')
const bootMsg        = document.getElementById('bootMsg')
const mainUI         = document.getElementById('mainUI')
const titleEl        = document.getElementById('titleEl')
const logEl          = document.getElementById('log')
const statusEl       = document.getElementById('statusLabel')
const textInput      = document.getElementById('textInput')
const sendBtn        = document.getElementById('sendBtn')
const statusPowerBtn = document.getElementById('statusPowerBtn')
const modeBtn        = document.getElementById('modeBtn')
const muteBtn        = document.getElementById('muteBtn')
const ttsMuteBtn     = document.getElementById('ttsMuteBtn')
const restartBtn     = document.getElementById('restartBtn')
const settingsClose  = document.getElementById('settingsClose')
const settingsBody   = document.getElementById('settingsBody')
const offlineBanner  = document.getElementById('offlineBanner')
const personalityFlash = document.getElementById('personalityFlash')
// Maintenance log refs (IDs now live inside #section-maintenance)
const sysPanelClose    = document.getElementById('sysPanelClose')
const maintLog         = document.getElementById('maintLog')
const maintCount       = document.getElementById('maintCount')
const maintClear       = document.getElementById('maintClear')
// Unread badge on the Sistema nav item
const navMaintBadge    = document.getElementById('navMaintBadge')
// Response timer fixed element
const responseTimerEl  = document.getElementById('responseTimer')

// ════════════════════════════════════════════════════════════════════════════
// STATE
// ════════════════════════════════════════════════════════════════════════════
// Boot safety-valve timeout (see setBootState('starting') below) — if
// jarvis_ready hasn't fired within this long of the boot animation
// starting, give up waiting and show the error/retry state instead of
// staying stuck forever. Spec asked for 30s, based on older
// logs/launcher.log entries showing ~22-28s boots — but live-testing this
// exact fix (npm start, dev mode) just measured two fresh, completely
// normal boots at 32.6s and 34.8s, both already over 30s on this same
// machine. 30s would therefore false-trigger on an ordinary boot, not just
// a genuinely stuck one — bumped to 45s for real headroom over what's
// actually been observed, while still being dramatically tighter than the
// original 120s. A legitimately-slow-but-still-working boot that trips it
// anyway just means one extra "Reintentar" click, not a lost session,
// since the retry path itself is now a full, reliable kill+reboot (see
// _enterBootSplashWait()'s retry handler further down).
const BOOT_TIMEOUT_MS = 45000

const STATUS_LABELS = { listening: 'Listening', processing: 'Processing', speaking: 'Speaking' }
// LIRA is the only personality now — kept as a labeled constant (not an
// inlined string at each of its 2 call sites in chat-render.js/
// core-tabs-sleep-panel.js) since both already read it by name.
const PERSONALITY_LABEL = '🤖  lira'

const currentPersonality = 'lira'   // never switches anymore — see personality-switch.js's own header comment
let currentStatus       = 'listening'
let lastMsgType         = null
let _flashTimer         = null
let jarvisSocket        = null
let _bootTimeoutId      = null   // safety timeout while in 'starting' state
let _isMuted            = false
let _isTtsMuted         = false        // voice OUTPUT mute — independent of mic mute above
let _listenMode         = 'wake_word'   // 'wake_word' | 'conversation'
let _hasShownMainUI     = false         // true once the main UI has been shown at least once
let _jarvisOnline       = false         // true when jarvisSocket is connected
let _pendingPanelData   = null          // armed by 'show_panel', consumed on the next 'speaking' transition — see CONTEXTUAL PANELS

// Index into BACKEND_URLS — advances on each connect_error, reset to 0 on fresh connect.
let _urlIndex           = 0

// Reload-after-restart flag — set by the restart button, consumed by jarvis_ready
let _pendingReloadAfterRestart = false

// Whether health polling is currently active (guards _doPollHealth re-entry)
let _isHealthPolling           = false

// Response timer state
let _timerInterval  = null   // setInterval handle for the ticking timer
let _timerStart     = null   // Date.now() when timer started
let _timerFadeTimer = null   // setTimeout handle for auto-hiding the 'done' state

// Maintenance log state
let _sysCount       = 0      // total system messages received this session

// Health polling state
let _healthPollTimer    = null
let _connectAttempts    = 0      // for exponential backoff on SocketIO connect
let _connectTimer       = null   // pending backoff retry timer

// Consecutive /api/health polls seen with jarvis_running:false since this
// polling run started — see _doPollHealth()'s own comment on the real
// incident this guards against: launcher.py can end up healthy and
// listening with jarvis.py never started at all, because Electron's own
// autoStartJarvis() is a ONE-SHOT call tied to a specific launcher spawn;
// if launcher.py restarts through any OTHER path (its own respawn-on-crash
// logic racing with Electron's, a stale-process cleanup on the next boot,
// anything external), nothing then reissues that POST — the launcher just
// sits there, healthy and doing nothing, until this catches it.
let _jarvisNotRunningPolls = 0

// ════════════════════════════════════════════════════════════════════════════
// DEVICE AUTH  — fingerprint generation + rejection page
// ════════════════════════════════════════════════════════════════════════════

// Device fingerprint computed once by _generateFingerprint(); sent with every
// server request so the backend can verify the device is registered.
let _deviceFingerprint = ''

// Generate (or retrieve) a permanent per-device UUID stored in localStorage.
// Unlike the previous hash-based fingerprint, this ID never changes across
// reloads, browser updates, or screen/timezone changes — it is created once
// and persisted forever, giving each device a truly stable identity.
// Auth is currently disabled; this function is ready to enable when needed.
async function _generateFingerprint() {
  const DEVICE_ID_KEY = 'jarvis_device_id'
  let id = localStorage.getItem(DEVICE_ID_KEY)
  if (!id) {
    // crypto.randomUUID() is available on all secure contexts (HTTPS / localhost).
    // Fallback uses getRandomValues for older browsers that lack randomUUID.
    id = typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : ([1e7] + -1e3 + -4e3 + -8e3 + -1e11).replace(/[018]/g, c =>
          (c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> c / 4).toString(16))
    localStorage.setItem(DEVICE_ID_KEY, id)
  }
  return id
}

// [AUTH] Replace the entire document with the rejection page.
// Uses document.open/write so ALL existing scripts and listeners are destroyed —
// there is no in-page way to dismiss or bypass this once called.
function _showRejectionPage() {
  document.open()
  document.write(
    '<!DOCTYPE html><html lang="es"><head>' +
    '<meta charset="UTF-8">' +
    '<meta name="viewport" content="width=device-width,initial-scale=1">' +
    '<style>' +
    '*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}' +
    'html,body{height:100%;overflow:hidden;background:#0a0a0a;' +
    'display:flex;align-items:center;justify-content:center}' +
    'p{color:#f0c040;font-size:clamp(1.5rem,5vw,3rem);' +
    "font-family:'Courier New',Courier,monospace;text-align:center;user-select:none}" +
    '</style></head><body>' +
    '<p>Los cojones ves esto</p>' +
    '</body></html>'
  )
  document.close()
}

// ════════════════════════════════════════════════════════════════════════════
// LAUNCHER SOCKET  (always-on, port 8079 = same as this page)
// Created by _initLauncherSocket() only after device auth passes.
// ════════════════════════════════════════════════════════════════════════════
// [AUTH] launcher is declared here but created only after auth passes.
let launcher = null

// [AUTH] Wire up the launcher socket and all its event handlers.
// Called from the auth gate IIFE at the bottom of this script.
function _initLauncherSocket() {
  // [AUTH] Pass fingerprint as query param so the server can reject unregistered devices.
  launcher = io({ transports: ['websocket', 'polling'], reconnection: true, query: { fp: _deviceFingerprint } })

launcher.on('connect', async () => {
  // Bug fix (real incident — "stuck on a loading screen after Mac sleep/
  // wake"): this launcher socket has reconnection:true, so it silently
  // auto-reconnects on its own after any brief network drop (e.g. macOS
  // suspending the renderer's network during display sleep) — completely
  // independent of whatever the jarvis socket (port 8080) is doing. Unlike
  // 'jarvis_status'/'jarvis_restart' below, this handler had no
  // _hasShownMainUI guard, so a routine reconnect mid-session called
  // setBootState('starting') and threw the full-screen boot overlay back
  // up over an already-working main UI. It then never came back down: the
  // jarvis socket's own reconnect handler (connection.js) only calls
  // applyPowerState('online') once _hasShownMainUI is true, never
  // showMainUI()/setBootState('running') — it assumes the overlay was
  // never re-shown in the first place. Once main UI is up, reconnect/
  // offline state is already fully owned by the jarvis socket's own
  // connect/disconnect handlers — this one has nothing left to do.
  if (_hasShownMainUI) return
  try {
    const res  = await fetch(`${LAUNCHER_API}/api/health`)
    const data = await res.json()
    if (data.jarvis_ready) {
      // Already ready — connect immediately
      setBootState('starting')
      _doConnectJarvis()
    } else if (data.jarvis_running) {
      setBootState('starting')
      setBootMsg('LOADING MODELS…')
      startHealthPolling()
    } else {
      setBootState('idle')
      // Poll even in idle so we auto-transition when jarvis auto-starts.
      // This handles the race where jarvis_status was emitted before our
      // socket connected, or the health fetch returned before the 2-s
      // auto-start delay completed.
      startHealthPolling()
    }
  } catch {
    // Health fetch failed — keep polling until launcher responds.
    setBootState('idle')
    startHealthPolling()
  }
})

launcher.on('jarvis_status', ({ running }) => {
  if (running) {
    if (_hasShownMainUI) {
      // Backend restarted while UI was already visible — poll without showing boot overlay
      startHealthPolling()
    } else {
      setBootState('starting')
      setBootMsg('STARTING…')
      startHealthPolling()
    }
  } else {
    stopHealthPolling()
    disconnectJarvis()
    if (_hasShownMainUI) {
      // Stay in main UI — show offline state via power button, not boot overlay
      applyPowerState('offline')
    } else {
      setBootState('idle')
    }
  }
})

// Emitted by launcher once /api/ready confirms jarvis is fully up
launcher.on('jarvis_ready', ({ ready }) => {
  if (!ready) return
  // If the health check returned stale data (e.g. SW cached a stale response)
  // and boot state is still 'idle', auto-transition here so the event is not
  // silently dropped by the guard below.
  if (!_hasShownMainUI && bootOverlay.dataset.state === 'idle') {
    setBootState('starting')
  }
  if (!(bootOverlay.dataset.state === 'starting' || _hasShownMainUI)) return

  stopHealthPolling()

  // ── Restart-triggered reload ─────────────────────────────────────────────
  // If the user clicked the restart button we perform a hard page reload here
  // instead of reconnecting in-place.  This ensures the browser loads the
  // latest Service Worker cache (new sw.js version) and the freshest HTML,
  // rather than re-using the already-running page with stale assets.
  if (_pendingReloadAfterRestart) {
    console.log('[Launcher] jarvis_ready after restart — reloading page for fresh assets.')
    window.location.reload(true)
    return   // reload is underway; nothing else to do
  }

  if (!_hasShownMainUI) setBootMsg('READY — CONNECTING…')
  _doConnectJarvis()
})

// jarvis.py crash-loop reporting — emitted by launcher.py's own
// _monitor_loop() every time jarvis.py dies unexpectedly during boot and
// gets auto-restarted (up to _MAX_RETRIES), and once more with failed:true
// the moment it gives up after exhausting all of them. Previously this
// event had NO listener at all here — the boot animation just sat showing
// generic "STARTING…"/"LOADING MODELS…" (from health polling, which only
// ever reports jarvis_running:false once retries are exhausted, with
// nothing to distinguish "about to retry" from "gave up entirely") until
// the plain BOOT_TIMEOUT_MS safety valve eventually caught it — which is
// almost certainly what "stuck on the boot animation after restart" in
// practice was: a genuine crash-loop with no real-time feedback, only a
// blind wait. Only relevant while actually booting (never shown once the
// main UI is up — that scenario is jarvis going offline mid-session,
// handled by applyPowerState('offline') via jarvis_status instead).
launcher.on('jarvis_restart', (data) => {
  if (_hasShownMainUI) return
  if (data && data.failed) {
    console.warn('[Launcher] jarvis.py exhausted all restart attempts:', data.message)
    stopHealthPolling()
    setBootMsg('ERROR — MAX RETRIES')     // contains 'ERROR' — matches _enterBootSplashWait()'s own detection regex
    powerBtn.disabled = false
    bootOverlay.dataset.state = 'idle'
  } else if (data && data.attempt) {
    setBootMsg(`REINICIANDO (${data.attempt}/${data.max})…`)
  }
})

// Force-reload event — emitted by launcher /api/reload after a frontend deployment.
// The launcher socket is always-on so this fires even if jarvis.py is down.
launcher.on('force_reload', () => {
  console.log('[Launcher] force_reload received — reloading page.')
  window.location.reload(true)
})

// Microphone permission events
launcher.on('mic_status', ({ status }) => {
  if (status === 'denied') {
    setBootMsg('MIC ACCESS DENIED')
    // Critical — system can't function without mic; stays in chat as brief line
    addMessage('error', 'Micrófono denegado — actívalo en Preferencias y reinicia')
  } else if (status === 'not_determined') {
    addMessage('system', 'Microphone permission not yet granted — the OS will prompt shortly.')
  }
})

// Real boot progress, stages 1-3 (launcher.py's own socket — see
// _applyBootProgress() and its own comment further down). Stages 4-5-7
// arrive on jarvisSocket instead, once jarvis.py's server exists — see
// _attemptConnect() below.
launcher.on('boot_progress', _applyBootProgress)

// Real update progress — emitted by launcher.py's api_update() as it
// streams scripts/rebuild_app.sh's own output (see emit_update_progress()
// there and _applyUpdateProgress() further down). `launcher` is always-on
// (independent of jarvis.py), matching where this event actually
// originates.
launcher.on('update_progress', _applyUpdateProgress)

// Real iOS-build progress — emitted by launcher.py's api_build_ios() as it
// streams scripts/build_ios.sh's own output (see emit_build_ios_progress()
// there and _applyBuildIosProgress() further down). Same rationale as
// update_progress above.
launcher.on('build_ios_progress', _applyBuildIosProgress)
}   // end _initLauncherSocket()

