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
const attachBtn       = document.getElementById('attachBtn')
const attachFileInput = document.getElementById('attachFileInput')
const attachPreview   = document.getElementById('attachPreview')
const statusPowerBtn = document.getElementById('statusPowerBtn')
const modeBtn        = document.getElementById('modeBtn')
const muteBtn        = document.getElementById('muteBtn')
const ttsMuteBtn     = document.getElementById('ttsMuteBtn')
const restartBtn     = document.getElementById('restartBtn')
const settingsClose  = document.getElementById('settingsClose')
const settingsBody   = document.getElementById('settingsBody')
const offlineBanner  = document.getElementById('offlineBanner')
const personalityFlash = document.getElementById('personalityFlash')
// Personality switcher buttons (in #personalityModal — same .personality-btn class)
const personalityBtns = document.querySelectorAll('.personality-btn')
// Maintenance log refs (IDs now live inside #section-maintenance)
const sysPanelClose    = document.getElementById('sysPanelClose')
const maintLog         = document.getElementById('maintLog')
const maintCount       = document.getElementById('maintCount')
const maintClear       = document.getElementById('maintClear')
// Unread badge on the Sistema nav item
const navMaintBadge    = document.getElementById('navMaintBadge')
// Personality indicator in persistent bar
const personalityIndicator = document.getElementById('personalityIndicator')
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
const PERSONALITY_LABEL = { jarvis: '🤖  jarvis', friday: '🤖  friday', lira: '🤖  lira' }

let currentPersonality  = 'lira'
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

// ════════════════════════════════════════════════════════════════════════════
// HEALTH POLLING  — polls /api/health every 2 s while booting
// ════════════════════════════════════════════════════════════════════════════
function startHealthPolling() {
  stopHealthPolling()       // clear any existing timer first
  _isHealthPolling = true
  _jarvisNotRunningPolls = 0
  _doPollHealth()
}

function stopHealthPolling() {
  _isHealthPolling = false
  clearTimeout(_healthPollTimer)
  _healthPollTimer = null
}

async function _doPollHealth() {
  if (!_isHealthPolling) return

  try {
    const res  = await fetch(`${LAUNCHER_API}/api/health`)
    const data = await res.json()

    if (data.jarvis_ready) {
      stopHealthPolling()
      setBootMsg('READY — CONNECTING…')
      _doConnectJarvis()
      return
    }

    // Auto-transition idle → starting when jarvis begins starting/running
    if (data.jarvis_running && bootOverlay.dataset.state === 'idle') {
      setBootState('starting')
    }

    // Self-healing nudge — real incident (see _jarvisNotRunningPolls' own
    // comment): launcher.py can be fully healthy with jarvis.py never
    // started, because whatever was SUPPOSED to fire the initial
    // POST /api/start (Electron's autoStartJarvis(), a one-shot call tied
    // to one specific launcher spawn) didn't end up targeting the launcher
    // instance that's actually alive right now. After ~8s of a healthy
    // launcher reporting jarvis_running:false with no retry_count activity
    // of its own (i.e. it's not already mid-crash-loop-retry — that path
    // already has its own recovery, see the RESTARTING message below), ask
    // it to start directly. POST /api/start is idempotent — a harmless
    // no-op if something else already got it going in the meantime.
    if (data.jarvis_running || (data.retry_count || 0) > 0) {
      _jarvisNotRunningPolls = 0
    } else {
      _jarvisNotRunningPolls++
      if (_jarvisNotRunningPolls === 4) {
        console.warn('[Launcher] jarvis_running still false after 8s of a healthy launcher — nudging POST /api/start.')
        fetch(`${LAUNCHER_API}/api/start`, { method: 'POST' }).catch(() => {})
      }
    }

    // Show meaningful progress
    if (data.mic_status === 'denied') {
      setBootMsg('MIC ACCESS DENIED')
    } else if (data.jarvis_running) {
      setBootMsg('LOADING MODELS…')
    } else {
      setBootMsg('STARTING…')
    }

    if (data.retry_count > 0) {
      setBootMsg(`RESTARTING (${data.retry_count}/${data.max_retries})…`)
    }
  } catch {
    // Launcher unreachable — just keep waiting
  }

  _healthPollTimer = setTimeout(_doPollHealth, 2000)
}

// ════════════════════════════════════════════════════════════════════════════
// JARVIS SOCKET  (port 8080 — only connect AFTER health confirms ready)
// ════════════════════════════════════════════════════════════════════════════
function _doConnectJarvis() {
  if (jarvisSocket && jarvisSocket.connected) return

  // Destroy any stale socket
  if (jarvisSocket) {
    jarvisSocket.removeAllListeners()
    jarvisSocket.disconnect()
    jarvisSocket = null
  }

  clearTimeout(_connectTimer)
  _connectAttempts = 0
  // Always start from the primary (Tailscale) URL on a fresh connect sequence.
  _urlIndex  = 0
  JARVIS_URL = BACKEND_URLS[0]
  JARVIS_API = JARVIS_URL
  _attemptConnect()
}

function _attemptConnect() {
  // Allow reconnect when UI is already shown (after jarvis went offline)
  if (bootOverlay.dataset.state !== 'starting' && !_hasShownMainUI) return
  if (jarvisSocket && jarvisSocket.connected) return

  // Tear down failed socket from previous attempt
  if (jarvisSocket) {
    jarvisSocket.removeAllListeners()
    jarvisSocket.disconnect()
    jarvisSocket = null
  }

  _connectAttempts++
  const _targetURL = BACKEND_URLS[_urlIndex] ?? BACKEND_URLS[BACKEND_URLS.length - 1]
  console.log(`[Jarvis] SocketIO connect attempt ${_connectAttempts} → ${_targetURL} (url ${_urlIndex + 1}/${BACKEND_URLS.length})`)

  // [AUTH] Fingerprint sent as query param so the Jarvis server (port 8080) can
  // reject unregistered devices at the SocketIO level, independent of the
  // launcher-side check.
  jarvisSocket = io(_targetURL, {
    transports: ['websocket', 'polling'],
    reconnection: false,   // we handle retries manually
    timeout: 5000,         // 5 s timeout: detect failure fast and switch to mDNS quickly
    query: { fp: _deviceFingerprint },
  })

  jarvisSocket.on('connect', () => {
    clearTimeout(_bootTimeoutId)
    clearTimeout(_connectTimer)
    _connectAttempts = 0
    offlineBanner.classList.remove('show')
    if (_hasShownMainUI) {
      // Reconnect after offline — stay in main UI, just re-enable controls
      applyPowerState('online')
      // [CHANGE 8] After reconnect, emit request_state so the backend can re-sync
      // personality, mode, and mute without requiring a full page reload.
      jarvisSocket.emit('request_state')
    } else {
      showMainUI()   // first connect — transitions boot overlay → main UI
    }
    addMessage('system', 'Jarvis online')
    setStatus('listening')
    setInputEnabled(true)
    updateSettingsInfo()
    restartBtn.classList.remove('restarting')
    fetch(`${JARVIS_API}/api/mute_state`)
      .then(r => r.json())
      .then(({ muted }) => applyMuteState(muted))
      .catch(() => {})
    fetch(`${JARVIS_API}/api/tts_mute_state`)
      .then(r => r.json())
      .then(({ muted }) => applyTtsMuteState(muted))
      .catch(() => {})
    _loadFeatureFlags()
    // Part 2: sync mode button to backend state on every connect
    fetch(`${JARVIS_API}/api/mode`)
      .then(r => r.json())
      .then(({ mode }) => applyModeState(mode))
      .catch(() => {})
    // Re-pull the Conceptuales list from the backend on every (re)connect —
    // by now JARVIS_API points at the confirmed-reachable backend URL, so
    // this catches cases where the very first _fetchConcepts() call (fired
    // at script load, before the backend URL was confirmed) fell back to
    // localStorage.
    _fetchConcepts()
  })

  jarvisSocket.on('connect_error', (err) => {
    console.warn(`[Jarvis] connect_error (attempt ${_connectAttempts}):`, err.message)

    // Advance to the next URL in BACKEND_URLS on each failure.
    if (_urlIndex < BACKEND_URLS.length - 1) {
      _urlIndex++
      JARVIS_URL = BACKEND_URLS[_urlIndex]
      JARVIS_API = JARVIS_URL
      console.log(`[Jarvis] Failed — trying next URL (${_urlIndex + 1}/${BACKEND_URLS.length}): ${JARVIS_URL}`)
    }

    // [CHANGE 10] Show "Reconectando…" in the persistent bar during every retry attempt.
    statusEl.textContent = 'Reconectando…'
    if (!_hasShownMainUI) setBootMsg(`RECONECTANDO… (${_connectAttempts})`)

    // [CHANGE 11] Fixed 3 s retry interval — reconnect quickly after WiFi drops or
    // device wakes from sleep.  Exponential backoff removed because it could delay
    // reconnection for up to 30 s which is noticeable to the user.
    if (bootOverlay.dataset.state === 'starting' || _hasShownMainUI) {
      _connectTimer = setTimeout(_attemptConnect, 3000)
    }
  })

  jarvisSocket.on('disconnect', (reason) => {
    setInputEnabled(false)
    setStatus('listening')
    _resetMicIndicator()
    _clearResponseTimer()   // cancel any pending response timer on disconnect
    console.warn('[Jarvis] disconnected:', reason)
    // [CHANGE 12] Always show "Reconectando…" in the persistent bar so the user
    // knows a reconnect attempt is in progress.
    statusEl.textContent = 'Reconectando…'
    if (_hasShownMainUI) {
      // Stay in main UI — power button shows offline state; no banner needed
      applyPowerState('offline')
      // [CHANGE 13] Auto-retry every 3 s indefinitely after a WiFi drop or
      // device sleep.  The retry uses the current _useMdnsFallback state so
      // mDNS is immediately tried if it was already the working URL.
      clearTimeout(_connectTimer)
      _connectTimer = setTimeout(_attemptConnect, 3000)
    } else {
      offlineBanner.classList.add('show')
    }
  })

  jarvisSocket.on('log', ({ type, message }) => {
    addMessage(type, message)
    // Unified floating diamond — genuine wake-word acceptance (not the
    // "detected but previous dispatch still in progress — ignored" case,
    // which also matches the same 'Wake word ... detected' prefix) drives
    // the diamond's transient 'wake' attention state. No new backend event
    // needed — this reuses the existing 'log' stream core.listener already
    // sends (see _triggerDiamondWake's own comment further down).
    if (typeof _triggerDiamondWake === 'function' &&
        /Wake word '.+?' detected/.test(message) && !message.includes('ignored')) {
      _triggerDiamondWake()
    }
  })
  jarvisSocket.on('status', ({ status }) => setStatus(status))
  // Contextual panels (weather/time/...) — see "CONTEXTUAL PANELS" below.
  // Just arms the pending panel here; the actual reveal is synced to the
  // 'speaking' status transition inside setStatus().
  jarvisSocket.on('show_panel', (data) => { _pendingPanelData = data })
  jarvisSocket.on('personality_change', ({ personality, display_name }) => {
    applyPersonality(personality, display_name)
    updateSettingsInfo()
  })
  jarvisSocket.on('mute_state', ({ muted }) => applyMuteState(muted))
  jarvisSocket.on('tts_mute_state', ({ muted }) => applyTtsMuteState(muted))
  // LIRA CORE's Estado tab — additive listeners (existing ones above are
  // untouched) that just refresh Estado's own display if it's the
  // currently visible tab, on top of whatever each event already does.
  // See _renderCoreEstado() / "LIRA CORE" section further down.
  const _coreEstadoRefresh = () => {
    if (typeof _currentSection !== 'undefined' && _currentSection === 'core' &&
        typeof _currentCoreSub !== 'undefined' && _currentCoreSub === 'estado') {
      _renderCoreEstado()
    }
  }
  jarvisSocket.on('status', _coreEstadoRefresh)
  jarvisSocket.on('personality_change', _coreEstadoRefresh)
  jarvisSocket.on('mute_state', _coreEstadoRefresh)
  jarvisSocket.on('tts_mute_state', _coreEstadoRefresh)
  jarvisSocket.on('connect', _coreEstadoRefresh)
  jarvisSocket.on('disconnect', _coreEstadoRefresh)
  // Sleep phase progress — core.server.emit_sleep_phase_update(), pushed by
  // core/commands.py's own background watcher while continuous sleep is
  // running (see that module's _sleep_phase_watch_loop() for why sleep
  // itself, a separate subprocess, can't emit this directly). Refreshes
  // Estado's "ÚLTIMO SUEÑO" section in near-real-time, and Pensamiento's
  // sleep-question/reflection lists too — a phase just completed, so new
  // ones may have appeared.
  jarvisSocket.on('sleep_phase_update', () => {
    _coreEstadoRefresh()
    if (typeof _currentSection !== 'undefined' && _currentSection === 'core' &&
        typeof _currentCoreSub !== 'undefined' && _currentCoreSub === 'pensamiento' &&
        typeof _loadSleepInsights === 'function') {
      _loadSleepInsights()
    }
  })
  // LIRA CORE's Pensamiento tab — see _onLiraThinking() further down.
  jarvisSocket.on('lira_thinking', (data) => { if (typeof _onLiraThinking === 'function') _onLiraThinking(data) })
  // Unified floating diamond — live partial transcript (what Joan is
  // saying, mid-recognition) shown above the diamond while she's actively
  // listening and still processing. Additional listener alongside the
  // existing one below (setPartialTranscript for Chat's own
  // #partialTranscript) — never modifies or replaces it.
  jarvisSocket.on('partial_transcript', ({ text }) => {
    if (typeof _diamondEligible === 'function' && _diamondEligible() &&
        typeof currentStatus !== 'undefined' && currentStatus === 'processing' && text) {
      _showDiamondText(text)
    }
  })
  jarvisSocket.on('feature_flags_state', (flags) => { _featureFlags = flags; _renderFeatureToggles() })
  jarvisSocket.on('mic_status', ({ status }) => {
    if (status === 'denied') {
      // Critical — system can't function without mic; stays in chat as brief line
      addMessage('error', 'Micrófono denegado — actívalo en Preferencias y reinicia')
    }
  })
  jarvisSocket.on('mic_active',   () => setMicActive(true))
  jarvisSocket.on('mic_inactive', () => setMicActive(false))
  jarvisSocket.on('mic_level',    ({ level }) => setMicLevel(level))
  jarvisSocket.on('heartbeat', () => {})
  jarvisSocket.on('partial_transcript', ({ text }) => setPartialTranscript(text))
  // Part 2: update mode button whenever mode changes (voice command or button click)
  jarvisSocket.on('mode_change', ({ mode }) => applyModeState(mode))
  // Force-reload event — secondary path via jarvis socket (launcher socket is primary)
  jarvisSocket.on('force_reload', () => {
    console.log('[Jarvis] force_reload received — reloading page.')
    window.location.reload(true)
  })
  // Real boot progress, stages 4-5-7 — jarvis.py's own socket, once it
  // exists. Stages 1-3 arrive on `launcher` instead — see
  // _initLauncherSocket() above. See _applyBootProgress() for the shared
  // handler both sockets feed.
  jarvisSocket.on('boot_progress', _applyBootProgress)

  // User-commanded diamond move — 'diamond_move' from core/commands.py's
  // _detect_diamond_move()/emit_diamond_move() (see core/server.py). Always
  // honored immediately, bypassing the ambient cooldown entirely — a direct
  // request from Joan is never "annoying drift", so none of the anti-
  // annoyance gating in the diamond's own positioning code applies to it.
  //
  // Bug fix (real incident — this crashed the boot sequence on EVERY page
  // load): this used to be a bare top-level statement further down in the
  // file, registered against `jarvisSocket` directly — but that variable is
  // `null` until THIS function assigns it a real connection a few lines up.
  // Executing a top-level `jarvisSocket.on(...)` before this function ever
  // ran threw an uncaught TypeError that aborted the rest of the script,
  // including the auth-gate IIFE that calls _initLauncherSocket() (further
  // down still) — so the launcher socket never even connected, health
  // polling never started, and _playBootSplash() never got the chance to
  // wire itself to the real boot state. That's "stuck on a black screen
  // with a gold line" exactly: the CSS-only initial paint, with zero
  // JS-driven boot logic ever having run. Registering it here instead,
  // alongside every other jarvisSocket.on(...) call, is the fix — it now
  // only ever runs once jarvisSocket is a real, connected socket.
  jarvisSocket.on('diamond_move', ({ region }) => {
    if (!_diamondEligible()) return
    const { top, left } = _bestRegionPosition(region)
    _glideDiamondTo(top, left)
  })
}

function disconnectJarvis() {
  clearTimeout(_connectTimer)
  stopHealthPolling()
  setInputEnabled(false)
  _resetMicIndicator()
  if (jarvisSocket) {
    jarvisSocket.removeAllListeners()
    jarvisSocket.disconnect()
    jarvisSocket = null
  }
}

// ════════════════════════════════════════════════════════════════════════════
// BOOT / SHUTDOWN
// ════════════════════════════════════════════════════════════════════════════
function setBootMsg(msg) {
  bootMsg.textContent = msg
}

function setBootState(state) {
  // 'idle' | 'starting' | 'running'
  clearTimeout(_bootTimeoutId)
  bootOverlay.dataset.state = state

  if (state === 'idle') {
    setBootMsg('OFFLINE')
    powerBtn.disabled = false
    bootOverlay.classList.remove('hidden')
    // [CHANGE 4] Do NOT hide mainUI — it stays visible at all times so the clock
    // always ticks regardless of backend connection state.  The boot overlay
    // (opaque, z-index 9000) covers it until Jarvis connects.
  } else if (state === 'starting') {
    setBootMsg('INITIALIZING…')
    powerBtn.disabled = true
    bootOverlay.classList.remove('hidden')
    // Safety valve: revert to idle if jarvis never becomes ready within
    // BOOT_TIMEOUT_MS of entering 'starting' — see that constant's own
    // comment. Also triggered early, before this timer even fires, if
    // launcher.py itself reports it gave up (see the 'jarvis_restart'
    // handler below) — no reason to sit out the rest of the budget once
    // the backend has already told us it's not coming up on its own.
    _bootTimeoutId = setTimeout(() => {
      if (bootOverlay.dataset.state === 'starting') {
        console.warn(`[Launcher] boot timeout — jarvis did not become ready in ${BOOT_TIMEOUT_MS / 1000}s`)
        stopHealthPolling()
        setBootMsg('TIMEOUT — RETRY')
        powerBtn.disabled = false
        bootOverlay.dataset.state = 'idle'
      }
    }, BOOT_TIMEOUT_MS)
  } else if (state === 'running') {
    bootOverlay.classList.add('hidden')
  }
}

function showMainUI() {
  _hasShownMainUI = true
  setBootState('running')
  mainUI.classList.add('visible')
  offlineBanner.classList.remove('show')   // clear any stale banner from a prior disconnect
  applyPowerState('online')
  _startSessionTimer()
}

// ════════════════════════════════════════════════════════════════════════════
// POWER STATE  — manages the status-bar power button and body.jarvis-offline.
// Three states:
//   'online'        — jarvis running; button is a dim red "stop" hint
//   'offline'       — jarvis stopped; button glows green as a "start" prompt
//   'transitioning' — starting or stopping; button blinks, non-interactive
// ════════════════════════════════════════════════════════════════════════════
function applyPowerState(state) {
  _jarvisOnline = (state === 'online')
  statusPowerBtn.classList.remove('power-online', 'power-offline', 'power-transitioning')
  statusPowerBtn.classList.add(`power-${state}`)

  if (state === 'online') {
    statusPowerBtn.title = 'Stop Jarvis'
    document.body.classList.remove('jarvis-offline')
  } else if (state === 'offline') {
    statusPowerBtn.title = 'Start Jarvis'
    document.body.classList.add('jarvis-offline')
  } else {
    // transitioning
    statusPowerBtn.title = 'Working…'
    document.body.classList.add('jarvis-offline')
  }
  _updateMMSysStrip()
}

// Status-bar power button — toggles start / stop
statusPowerBtn.addEventListener('click', async () => {
  if (_jarvisOnline) {
    // Online → stop jarvis
    applyPowerState('transitioning')
    try {
      await fetch(`${LAUNCHER_API}/api/stop`, { method: 'POST' })
      // launcher will emit jarvis_status:{running:false} which calls applyPowerState('offline')
    } catch {
      applyPowerState('online')   // restore on failure
    }
  } else {
    // Offline → start jarvis
    applyPowerState('transitioning')
    try {
      const res = await fetch(`${LAUNCHER_API}/api/start`, { method: 'POST' })
      if (!res.ok) throw new Error(`start failed: ${res.status}`)
      // Health polling / launcher events will drive the rest of the reconnect flow
      startHealthPolling()
    } catch (e) {
      console.error('[Launcher] start failed:', e)
      applyPowerState('offline')
    }
  }
})

powerBtn.addEventListener('click', async () => {
  if (bootOverlay.dataset.state === 'starting') return
  setBootState('starting')
  setBootMsg('STARTING…')
  try {
    const res = await fetch(`${LAUNCHER_API}/api/start`, { method: 'POST' })
    if (!res.ok) throw new Error(`start failed: ${res.status}`)
    // Health polling will take over from here — launcher emits jarvis_status + jarvis_ready
    startHealthPolling()
  } catch (e) {
    console.error('[Launcher] start failed:', e)
    setBootMsg('ERROR — RETRY')
    powerBtn.disabled = false
    bootOverlay.dataset.state = 'idle'
  }
})


// ════════════════════════════════════════════════════════════════════════════
// MUTE / UNMUTE
// ════════════════════════════════════════════════════════════════════════════
function applyMuteState(muted) {
  _isMuted = muted
  if (muted) {
    muteBtn.textContent = '🔇'
    muteBtn.title       = 'Unmute microphone'
    muteBtn.classList.add('muted')
  } else {
    muteBtn.textContent = '🎤'
    muteBtn.title       = 'Mute microphone'
    muteBtn.classList.remove('muted')
  }
  _updateMMSysStrip()
}

muteBtn.addEventListener('click', async () => {
  const endpoint = _isMuted ? '/api/unmute' : '/api/mute'
  try {
    const res  = await fetch(`${JARVIS_API}${endpoint}`, { method: 'POST' })
    const data = await res.json()
    applyMuteState(data.muted)
  } catch {
    // Connectivity error from UI control — goes to system panel, not chat
    addMaintMessage('Error: mute toggle — Jarvis no responde')
  }
})

// TTS (voice output) mute — mirrors the mic mute block above, but hits
// /api/tts_mute /api/tts_unmute (core/server.py → core.voice.set_tts_muted).
// The mic itself is never touched: LIRA keeps listening and replying in
// chat, she just stops speaking.
function applyTtsMuteState(muted) {
  _isTtsMuted = muted
  if (muted) {
    ttsMuteBtn.textContent = '🔈'
    ttsMuteBtn.title       = "Unmute LIRA's voice"
    ttsMuteBtn.classList.add('muted')
  } else {
    ttsMuteBtn.textContent = '🔊'
    ttsMuteBtn.title       = "Mute LIRA's voice"
    ttsMuteBtn.classList.remove('muted')
  }
  // Keep the main-menu mirror button (#mmToggleTts) in sync — same pattern
  // as applyModeState() syncing #mmToggleMode.
  _updateMMSysStrip()
}

ttsMuteBtn.addEventListener('click', async () => {
  const endpoint = _isTtsMuted ? '/api/tts_unmute' : '/api/tts_mute'
  try {
    const res  = await fetch(`${JARVIS_API}${endpoint}`, { method: 'POST' })
    const data = await res.json()
    applyTtsMuteState(data.muted)
  } catch {
    addMaintMessage('Error: TTS mute toggle — Jarvis no responde')
  }
})

// ════════════════════════════════════════════════════════════════════════════
// LISTEN MODE TOGGLE  (Part 2)
// applyModeState() is called on connect (to sync initial state), on button
// click, and on 'mode_change' socket events (voice-activated switches).
// ════════════════════════════════════════════════════════════════════════════
function applyModeState(mode) {
  _listenMode = mode
  const isConv = mode === 'conversation'
  modeBtn.textContent = isConv ? 'Conv Mode' : 'Wake Word'
  modeBtn.title       = isConv
    ? 'Active: Conversation Mode — click to switch to Wake Word'
    : 'Active: Wake Word Mode — click to switch to Conversation'
  modeBtn.classList.toggle('mode-conversation', isConv)
  modeBtn.classList.toggle('mode-wake',         !isConv)
  _updateMMSysStrip()
}

modeBtn.addEventListener('click', async () => {
  const nextMode = _listenMode === 'wake_word' ? 'conversation' : 'wake_word'
  try {
    const res  = await fetch(`${JARVIS_API}/api/mode`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ mode: nextMode }),
    })
    const data = await res.json()
    if (data.mode) applyModeState(data.mode)
  } catch {
    // Connectivity error from UI control — goes to system panel, not chat
    addMaintMessage('Error: mode toggle — Jarvis no responde')
  }
})

// ════════════════════════════════════════════════════════════════════════════
// RESTART
// ════════════════════════════════════════════════════════════════════════════
restartBtn.addEventListener('click', async () => {
  restartBtn.classList.add('restarting')
  addMessage('system', 'Restarting Jarvis…')

  // Signal that when jarvis_ready fires we should do a hard page reload instead
  // of reconnecting in-place.  This ensures the browser fetches the latest
  // frontend assets (respecting the bumped Service Worker cache key).
  _pendingReloadAfterRestart = true

  try {
    await fetch(`${LAUNCHER_API}/api/restart`, { method: 'POST' })
  } catch {
    // Launcher unreachable — clear the flag so we don't reload on the next
    // unrelated jarvis_ready event
    _pendingReloadAfterRestart = false
    // System-level failure from a maintenance action — goes to system panel, not chat
    addMaintMessage('Error: restart fallido — launcher no responde')
    restartBtn.classList.remove('restarting')
    return
  }
  // The launcher will emit jarvis_status events then jarvis_ready.
  // jarvis_ready handler checks _pendingReloadAfterRestart and reloads.
  // Safety valve: if jarvis never comes back in 120 s, clear the flag so
  // a later manual start does a normal connect (not an unexpected reload).
  setTimeout(() => {
    if (_pendingReloadAfterRestart) {
      console.warn('[Restart] jarvis_ready never fired in 120 s — clearing reload flag.')
      _pendingReloadAfterRestart = false
      restartBtn.classList.remove('restarting')
    }
  }, 120000)
})


// ════════════════════════════════════════════════════════════════════════════
// DIGIT-FLIP  — brief "odometer tick" animation for numeric readouts (timer,
// latency). Restarts a CSS animation by removing the class, forcing a reflow,
// then re-adding it — cheap, no library, no per-character rigging.
// ════════════════════════════════════════════════════════════════════════════
function _flipDigits(el, newText) {
  if (!el || el.textContent === newText) return
  el.textContent = newText
  el.classList.remove('digit-flip')
  void el.offsetWidth   // force reflow so the animation restarts on repeated calls
  el.classList.add('digit-flip')
}

// ════════════════════════════════════════════════════════════════════════════
// RESPONSE TIMER  — drives #responseTimer, a fixed UI element near the input.
// Never injects into chat message rows.
// ════════════════════════════════════════════════════════════════════════════
function _startResponseTimer() {
  clearInterval(_timerInterval)
  clearTimeout(_timerFadeTimer)

  _timerStart = Date.now()
  responseTimerEl.className = 'running'
  responseTimerEl.textContent = '0s…'

  _timerInterval = setInterval(() => {
    const sec = Math.floor((Date.now() - _timerStart) / 1000)
    _flipDigits(responseTimerEl, `${sec}s…`)
    if (sec > 120) _clearResponseTimer()   // safety: auto-cancel after 2 min
  }, 1000)
}

function _stopResponseTimer() {
  const elapsedMs = _timerStart ? Date.now() - _timerStart : null
  const elapsed   = elapsedMs !== null ? Math.floor(elapsedMs / 1000) : null

  clearInterval(_timerInterval)
  _timerInterval = null
  _timerStart = null

  if (elapsed !== null) {
    responseTimerEl.className = 'done'
    responseTimerEl.textContent = `Responded in ${elapsed}s`
    _timerFadeTimer = setTimeout(() => {
      responseTimerEl.className = ''
      responseTimerEl.textContent = ''
    }, 3000)
  }

  // Mirror precise (millisecond) latency into the main-menu orb's HUD readout.
  const hudLatEl = document.getElementById('mmHudLatency')
  if (hudLatEl && elapsedMs !== null) _flipDigits(hudLatEl, `${elapsedMs}ms`)
}

function _clearResponseTimer() {
  clearInterval(_timerInterval)
  clearTimeout(_timerFadeTimer)
  _timerInterval  = null
  _timerFadeTimer = null
  _timerStart     = null
  responseTimerEl.className   = ''
  responseTimerEl.textContent = ''
}

// ════════════════════════════════════════════════════════════════════════════
// USER ACTIVITY — reports what Joan is doing in the HUD (section
// navigation, concept-form typing, opening an armor model, going idle)
// over the existing jarvisSocket connection, so LIRA can act as a co-pilot
// noticing what's happening in the interface itself — see
// core/server.py's 'user_activity' socket handler and core/commands.py's
// on_user_activity() / ACTIVIDAD ACTUAL system-prompt block. Every call
// site below also calls _markUiInteraction() so the idle watch (below)
// never fires while something else just happened.
// ════════════════════════════════════════════════════════════════════════════
function _emitUserActivity(section, action, context) {
  if (!jarvisSocket || !jarvisSocket.connected) return
  jarvisSocket.emit('user_activity', { section, action, context: context || {} })
}

// ════════════════════════════════════════════════════════════════════════════
// HUD CONTEXT — precise, full-detail state (exactly which armor or concept
// is on screen right now, and which section of it), separate from USER
// ACTIVITY above. USER ACTIVITY is a lightweight "what's happening" signal
// for co-pilot commentary; HUD CONTEXT carries the full object (armor
// specs/innovations/limitations, concept description) so LIRA can answer
// specific questions about whatever's on screen without asking which one —
// see core/server.py's 'hud_context' socket handler and
// core/commands.py's PANTALLA ACTUAL system-prompt block. Fires on every
// meaningful state change (opening an armor/concept, scrolling to a
// different section within one, navigating away), not just navigation.
// ════════════════════════════════════════════════════════════════════════════
function _emitHudContext(payload) {
  if (!jarvisSocket || !jarvisSocket.connected) return
  jarvisSocket.emit('hud_context', payload)
}

// Internal nav-section id → the vocabulary LIRA's prompt actually uses.
const _ACTIVITY_SECTION_MAP = { home: 'main', chat: 'chat', maintenance: 'system', armor: 'armor', settings: 'settings' }

// ── Idle detection — "hasn't touched the HUD in a while", independent of
// core/commands.py's own 30-min voice-inactivity proactive check (this one
// is about visual/HUD idleness, a much shorter timescale — noticing Joan
// is just sitting on a screen, not that he hasn't spoken). Any click or
// keydown anywhere resets the clock; a periodic check fires ONE 'idle'
// event per idle stretch (not repeatedly) once the threshold passes.
const _ACTIVITY_IDLE_THRESHOLD_MS = 75000   // ~75s without any interaction
let _lastUiInteractionAt        = Date.now()
let _idleEventSentForThisStretch = false

function _markUiInteraction() {
  _lastUiInteractionAt = Date.now()
  _idleEventSentForThisStretch = false
}
document.addEventListener('click',   _markUiInteraction, { capture: true })
document.addEventListener('keydown', _markUiInteraction, { capture: true })

setInterval(() => {
  if (_idleEventSentForThisStretch) return
  if (Date.now() - _lastUiInteractionAt < _ACTIVITY_IDLE_THRESHOLD_MS) return
  _idleEventSentForThisStretch = true
  _emitUserActivity(_ACTIVITY_SECTION_MAP[_currentSection] || _currentSection, 'idle', {})
}, 15000)

// ════════════════════════════════════════════════════════════════════════════
// SECTION SWITCHING — drives the 4-section bottom-nav layout
// ════════════════════════════════════════════════════════════════════════════
const _navItems  = document.querySelectorAll('.nav-item')
const _appSections = document.querySelectorAll('.app-section')
let _currentSection = 'home'

// Public entry point — every nav click, section-changing button, etc. call
// THIS, not _performSwitchSection() directly. Guards against navigating
// away while the concept edit modal has unsaved changes (see
// _conceptFormHasUnsavedChanges/_showUnsavedConceptDialog, near the concept
// modal code): if there ARE unsaved changes, the actual switch is deferred
// until the user picks Guardar/Descartar in that dialog — Cancelar just
// stays put, nothing navigates. No unsaved changes ⇒ identical to before,
// switches immediately.
function switchSection(name) {
  if (typeof _conceptFormHasUnsavedChanges === 'function' && _conceptFormHasUnsavedChanges()) {
    _showUnsavedConceptDialog(name)
    return
  }
  _performSwitchSection(name)
}

function _performSwitchSection(name) {
  const _prevSection = _currentSection
  _currentSection = name
  _markUiInteraction()
  _emitUserActivity(_ACTIVITY_SECTION_MAP[name] || name, 'navigate', {})
  _appSections.forEach(s => s.classList.toggle('active', s.id === `section-${name}`))
  _navItems.forEach(b => b.classList.toggle('active', b.dataset.section === name))

  // Dramatic orb shrink/fade when navigating away from the main menu; the
  // reverse "power up" pop plays automatically (same transition, reversed)
  // the moment the class is removed on the way back in.
  const mmOrb = document.getElementById('mmOrbWrap')
  if (mmOrb) {
    if (_prevSection === 'home' && name !== 'home') mmOrb.classList.add('mm-orb-leaving')
    else if (name === 'home') mmOrb.classList.remove('mm-orb-leaving')
  }

  // Navigating to maintenance: clear unread badge and scroll log to bottom
  if (name === 'maintenance') {
    _sysCount = 0
    maintCount.textContent = ''
    if (navMaintBadge) navMaintBadge.textContent = ''
    maintLog.scrollTo({ top: maintLog.scrollHeight })
  }

  // Navigating to armor bay: ensure the active sub-tab content is rendered
  if (name === 'armor') {
    _switchSubTab(_currentSub)
  }
  // Leaving armor bay: close the detail view so coming back later always
  // shows the grid fresh, not a stale detail page from before
  if (_prevSection === 'armor' && name !== 'armor') {
    _closeDetailView()
  }

  // Navigating to Ajustes: refresh system info (was triggered by the old
  // gearBtn's click handler before this became a full nav section)
  if (name === 'settings') {
    updateSettingsInfo()
  } else if (_prevSection === 'settings' && typeof _stopSleepPoll === 'function') {
    // Leaving Ajustes — stop polling GET /api/sleep/status in the
    // background; updateSettingsInfo() re-establishes it (if a session is
    // still running) the next time Ajustes is actually opened again.
    _stopSleepPoll()
  }

  // Unified floating diamond — shown on every section except Main/Chat,
  // per spec ("fully removed when navigating to Main or Chat, fully shown
  // on all other sections"). See _updateDiamondVisibility() further down.
  _updateDiamondVisibility()

  // Section-aware recompute — re-home to whatever's now the best
  // low-density spot for the NEW section's content, right away, but ONLY
  // if she's just resting (idle). If she's mid-attention-state
  // (wake/processing/speaking), that state owns the position instead — a
  // section switch mid-turn must never yank her out of it (see
  // _applyDiamondState's own comment). Not gated by the ambient cooldown —
  // a section change is one of the explicit recalculation triggers per
  // spec, same as a state change.
  if (typeof _liraDiamondState !== 'undefined' && _liraDiamondState === 'idle' &&
      typeof _glideDiamondTo === 'function') {
    const { top, left } = _bestIdlePosition()
    _glideDiamondTo(top, left)
  }

  // Main menu floating text — hide immediately when leaving Main, so it
  // doesn't awkwardly reappear stale when coming back.
  if (_prevSection === 'home' && name !== 'home') {
    clearTimeout(_mmFloatingHideTimer)
    mmFloatingText.classList.remove('visible')
  }

  // LIRA CORE — render whichever sub-tab is already active on entry (so
  // switching back to CORE later shows fresh data, not a stale render from
  // last time), and only run Estado's polling fallback while CORE itself
  // is the visible section.
  if (name === 'core') {
    _switchCoreSubTab(_currentCoreSub)
    _startCoreEstadoPoll()
  } else if (_prevSection === 'core') {
    _stopCoreEstadoPoll()
  }
}

_navItems.forEach(btn => {
  btn.addEventListener('click', () => switchSection(btn.dataset.section))
})

// sysPanelClose (✕ button in maintenance section header) → back to chat
sysPanelClose.addEventListener('click', () => switchSection('chat'))

// Clear maintenance log
maintClear.addEventListener('click', () => {
  maintLog.innerHTML = ''
  _sysCount = 0
  maintCount.textContent = ''
  if (navMaintBadge) navMaintBadge.textContent = ''
})

// Reload button — bumps SW cache key then forces a full reload
document.getElementById('maintReload').addEventListener('click', () => {
  const key = 'sw_cache_increment'
  const next = (parseInt(localStorage.getItem(key) || '0', 10) + 1).toString()
  localStorage.setItem(key, next)
  window.location.reload(true)
})

// ════════════════════════════════════════════════════════════════════════════
// PERSONALITY MODAL — opened by #personalityIndicator in the persistent bar
// ════════════════════════════════════════════════════════════════════════════
const personalityModal         = document.getElementById('personalityModal')
const personalityModalBackdrop = document.getElementById('personalityModalBackdrop')

function _openPersonalityModal() {
  personalityModal.classList.add('open')
  personalityModalBackdrop.classList.add('open')
}
function _closePersonalityModal() {
  personalityModal.classList.remove('open')
  personalityModalBackdrop.classList.remove('open')
}

personalityIndicator.addEventListener('click', () => {
  personalityModal.classList.contains('open') ? _closePersonalityModal() : _openPersonalityModal()
})
personalityModalBackdrop.addEventListener('click', _closePersonalityModal)

// ════════════════════════════════════════════════════════════════════════════
// MIC INDICATOR
// ════════════════════════════════════════════════════════════════════════════
const micDot = document.getElementById('micDot')

function setMicActive(active) {
  if (active) {
    micDot.classList.add('mic-active')
    micDot.title = 'Microphone active'
  } else {
    micDot.classList.remove('mic-active')
    micDot.style.removeProperty('--mic-level')
    micDot.title = 'Microphone inactive'
  }
}

function setMicLevel(level) {
  // level is 0.0–1.0 (log-scaled). Drive the CSS custom property.
  micDot.style.setProperty('--mic-level', level.toFixed(4))
}

// Reset mic indicator when jarvis disconnects
function _resetMicIndicator() {
  setMicActive(false)
}

// ════════════════════════════════════════════════════════════════════════════
// BODY CLASS MANAGEMENT
// Use classList so status and personality classes never clobber each other.
// ════════════════════════════════════════════════════════════════════════════
const _STATUS_CLASSES = ['listening', 'processing', 'speaking']

function _syncBodyClasses() {
  _STATUS_CLASSES.forEach(c => document.body.classList.remove(c))
  document.body.classList.add(currentStatus)
}

// ════════════════════════════════════════════════════════════════════════════
// CONTEXTUAL PANELS — main-menu side panels that slide in beside the orb
// while LIRA speaks about a specific topic (weather, time, ...). Backend
// trigger: core/commands.py's _maybe_emit_panel() emits a 'show_panel'
// socket event (via core/server.py's emit_show_panel()) right after intent
// detection, before the reply is generated. The frontend just arms
// _pendingPanelData when 'show_panel' arrives (listener above) and
// reveals/hides the panel in sync with the 'speaking' status transition
// (see setStatus() below) — not the raw socket event's arrival — so the
// panel appears exactly when she starts actually talking about it, and
// disappears the moment she stops.
//
// Extensible by design: PANEL_RENDERERS maps a panel `type` to a function
// returning the inner HTML for #mmContextPanel. Adding a new panel type
// (news, armor, music, ...) later is just one more entry here plus its own
// CSS block (see .panel-weather-*/.panel-time-* above for the pattern) —
// nothing else in this file needs to change.
// ════════════════════════════════════════════════════════════════════════════
const WEATHER_ICONS = {
  sunny:  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4.5"/><line x1="12" y1="1.5" x2="12" y2="4"/><line x1="12" y1="20" x2="12" y2="22.5"/><line x1="1.5" y1="12" x2="4" y2="12"/><line x1="20" y1="12" x2="22.5" y2="12"/><line x1="4.5" y1="4.5" x2="6.2" y2="6.2"/><line x1="17.8" y1="17.8" x2="19.5" y2="19.5"/><line x1="4.5" y1="19.5" x2="6.2" y2="17.8"/><line x1="17.8" y1="6.2" x2="19.5" y2="4.5"/></svg>',
  cloudy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6.5 17.5a4 4 0 0 1-.5-7.97 5 5 0 0 1 9.6-1.9A4.5 4.5 0 0 1 17.5 17.5h-11z"/></svg>',
  rainy:  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6.5 14.5a4 4 0 0 1-.5-7.97 5 5 0 0 1 9.6-1.9A4.5 4.5 0 0 1 17.5 14.5h-11z"/><line x1="8" y1="17" x2="7" y2="20"/><line x1="12" y1="17" x2="11" y2="20"/><line x1="16" y1="17" x2="15" y2="20"/></svg>',
  stormy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6.5 13.5a4 4 0 0 1-.5-7.97 5 5 0 0 1 9.6-1.9A4.5 4.5 0 0 1 17.5 13.5h-11z"/><path d="M13 13.5l-3 5h3l-2 5 5-6.5h-3l2-3.5z"/></svg>',
  foggy:  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="8" x2="20" y2="8"/><line x1="2" y1="12" x2="22" y2="12"/><line x1="4" y1="16" x2="20" y2="16"/><line x1="6" y1="20" x2="18" y2="20"/></svg>',
}

function _escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]))
}

const PANEL_RENDERERS = {
  weather: (d) => {
    const icon  = WEATHER_ICONS[d.icon] || WEATHER_ICONS.cloudy
    const temp  = Math.round(d.temp)
    const feels = Math.round(d.feels_like)
    const wind  = Math.round(d.wind)
    return `
      <div class="panel-weather-icon">${icon}</div>
      <div class="panel-weather-temp">${temp}°</div>
      <div class="panel-weather-condition">${_escapeHtml(d.condition)}</div>
      <div class="panel-weather-feels">Sensación ${feels}°</div>
      <div class="panel-weather-details">
        <div class="panel-row"><span class="panel-label">Humedad</span><span class="panel-val">${_escapeHtml(d.humidity)}%</span></div>
        <div class="panel-row"><span class="panel-label">Viento</span><span class="panel-val">${wind} km/h</span></div>
      </div>
    `
  },
  time: (d) => `
    <div class="panel-time-clock">${_escapeHtml(d.time)}</div>
    <div class="panel-time-date">${_escapeHtml(d.date)}</div>
  `,
}

function _renderContextPanel(data) {
  const renderer = data && PANEL_RENDERERS[data.type]
  if (!renderer) return false
  const el = document.getElementById('mmContextPanel')
  if (!el) return false
  try {
    el.innerHTML = renderer(data)
  } catch (e) {
    console.error('[Panel] render failed:', e)
    return false
  }
  return true
}

function _showContextPanel(data) {
  if (!_renderContextPanel(data)) return
  const area = document.getElementById('mmOrbArea')
  if (area) area.classList.add('panel-active')
}

let _panelClearTimer = null
function _hideContextPanel() {
  const area = document.getElementById('mmOrbArea')
  if (!area) return
  area.classList.remove('panel-active')
  // Clear content only after the fade-out transition finishes, so a fast
  // re-trigger never flashes stale content mid-animation.
  clearTimeout(_panelClearTimer)
  _panelClearTimer = setTimeout(() => {
    if (!area.classList.contains('panel-active')) {
      const el = document.getElementById('mmContextPanel')
      if (el) el.innerHTML = ''
    }
  }, 550)
}

// ════════════════════════════════════════════════════════════════════════════
// STATUS
// ════════════════════════════════════════════════════════════════════════════
const MM_STATUS_LABELS = { listening: 'Escuchando', processing: 'Procesando', speaking: 'Hablando' }

// #mmStatus's "alive typography" — typed in at 40ms/char instead of an
// instant textContent swap, plus a subtle idle "breath" (dim to 30%, back)
// every 8-12s while parked on Escuchando with nothing changing. State kept
// in module-level `let`s (declared here, before setStatus() below is ever
// called) rather than data-* attributes, same reasoning as _mmLiraQuoteIdx
// above — cheaper and this is the only place that reads them.
let _mmStatusTypeTimer  = null
let _mmStatusBreathTimer = null
let _mmLastStatusText   = ''

function _clearMMStatusBreath() {
  clearTimeout(_mmStatusBreathTimer)
  _mmStatusBreathTimer = null
  const el = document.getElementById('mmStatus')
  if (el) el.classList.remove('mm-status-breathe')
}

// Re-schedules itself after every breath so it keeps running for as long as
// status stays 'listening' — the currentStatus check inside is what makes
// it stop reacting (not stop rescheduling) the moment status changes away,
// so there's no separate teardown call needed from setStatus().
function _scheduleMMStatusBreath() {
  clearTimeout(_mmStatusBreathTimer)
  const delay = 8000 + Math.random() * 4000 // 8-12s, per spec
  _mmStatusBreathTimer = setTimeout(() => {
    if (currentStatus === 'listening') {
      const el = document.getElementById('mmStatus')
      if (el) {
        el.classList.add('mm-status-breathe')
        setTimeout(() => el.classList.remove('mm-status-breathe'), 650)
      }
      _scheduleMMStatusBreath()
    }
  }, delay)
}

function _typeMMStatus(text) {
  const el = document.getElementById('mmStatus')
  if (!el) return
  clearInterval(_mmStatusTypeTimer)
  _clearMMStatusBreath()
  if (_mmLastStatusText === text) {
    // Redundant call with the same label (e.g. a re-emitted socket event) —
    // nothing to retype, just make sure the idle breath is (re)armed.
    if (currentStatus === 'listening') _scheduleMMStatusBreath()
    return
  }
  _mmLastStatusText = text
  el.textContent = ''
  let i = 0
  _mmStatusTypeTimer = setInterval(() => {
    i += 1
    el.textContent = text.slice(0, i)
    if (i >= text.length) {
      clearInterval(_mmStatusTypeTimer)
      _mmStatusTypeTimer = null
      if (currentStatus === 'listening') _scheduleMMStatusBreath()
    }
  }, 40)
}

function setStatus(status) {
  const prevStatus = currentStatus
  currentStatus = status
  _syncBodyClasses()
  applyPersonality(currentPersonality)
  statusEl.textContent = STATUS_LABELS[status] ?? status
  // Main menu status label (Spanish) — typed in, see _typeMMStatus() above.
  _typeMMStatus(MM_STATUS_LABELS[status] ?? status)
  if (status !== 'processing') setPartialTranscript('')

  // Contextual panels — reveal in sync with LIRA actually starting to
  // speak (not the moment 'show_panel' arrived, which is earlier, while
  // she's still processing), and hide the moment she stops. See
  // "CONTEXTUAL PANELS" section below.
  if (status === 'speaking' && prevStatus !== 'speaking' && _pendingPanelData) {
    _showContextPanel(_pendingPanelData)
    _pendingPanelData = null
  } else if (status !== 'speaking' && prevStatus === 'speaking') {
    _hideContextPanel()
  }

  // Unified floating diamond — state class (and, via _applyDiamondState,
  // position) always reflects live status while eligible to show, EXCEPT
  // the one transition owned by the post-speech hold instead:
  // speaking → idle. Per spec ("after speaking: waits 3 seconds... then
  // glides back to corner"), that specific return-to-corner is deferred to
  // _scheduleDiamondTextHide()'s timeout below, not applied immediately
  // here — unless a NEW turn (processing/speaking) starts before the hold
  // elapses, which takes over immediately same as any other transition.
  if (_diamondEligible()) {
    const _diamondTargetState = status === 'speaking' ? 'speaking' : status === 'processing' ? 'processing' : 'idle'
    if (!(prevStatus === 'speaking' && _diamondTargetState === 'idle')) {
      _applyDiamondState(_diamondTargetState)
    }
  }
  if (status !== 'speaking' && prevStatus === 'speaking' && _diamondEligible()) {
    _scheduleDiamondTextHide()
  }

  // Main menu floating text — her reply's hold timer starts once she
  // actually STOPS speaking (not from whenever the text first arrived via
  // addMessage), so it stays visible for the full spoken duration plus the
  // grace period after. No-op if nothing is currently showing.
  if (status !== 'speaking' && prevStatus === 'speaking' && _currentSection === 'home') {
    _scheduleMMFloatingTextHide()
  }
}

let _partialClearTimer = null
function setPartialTranscript(text) {
  const el = document.getElementById('partialTranscript')
  if (!el) return
  clearTimeout(_partialClearTimer)
  if (text) {
    el.textContent = text
    el.style.opacity = '1'
  } else {
    el.style.opacity = '0'
    _partialClearTimer = setTimeout(() => { el.textContent = '' }, 400)
  }
}

// ════════════════════════════════════════════════════════════════════════════
// UNIFIED FLOATING LIRA DIAMOND — replaces the old Siri-style overlay
// entirely (was scoped to Armaduras/Sistema/Ajustes only; this one shows
// on every section except Main/Chat, including LIRA CORE and CONTROL).
// Autonomous — LIRA controls her own position, never draggable, no
// localStorage. Driven entirely by the existing 'log'/'status'/
// 'partial_transcript' socket events — no new backend events. See
// #liraDiamond's own HTML comment for the full design rationale, and
// .mm-floating-text's HTML comment further down for Main's own (separate,
// untouched) equivalent.
// ════════════════════════════════════════════════════════════════════════════
const liraDiamond       = document.getElementById('liraDiamond')
const liraDiamondOrb    = document.getElementById('liraDiamondOrb')
const liraDiamondText   = document.getElementById('liraDiamondText')
const liraDiamondBubble = document.getElementById('liraDiamondBubble')
const liraDiamondBubbleText = document.getElementById('liraDiamondBubbleText')
const liraDiamondInput  = document.getElementById('liraDiamondInput')
const mmFloatingText    = document.getElementById('mmFloatingText')
const mmFloatingInput   = document.getElementById('mmFloatingInput')

let _lastJarvisReply = ''   // tracked unconditionally in addMessage() below — the bubble's "last response" line

const LIRA_DIAMOND_EXCLUDED_SECTIONS = new Set(['home', 'chat'])
function _diamondEligible() { return !LIRA_DIAMOND_EXCLUDED_SECTIONS.has(_currentSection) }

function _updateDiamondVisibility() {
  liraDiamond.classList.toggle('visible', _diamondEligible())
  if (!_diamondEligible()) _closeDiamondBubble()   // never leave the bubble open behind on Main/Chat
}

// ── Autonomous positioning — dynamic density-scored grid ─────────────────
// LIRA picks her own position by dividing the CURRENT viewport into a grid,
// scoring every cell by how much UI content overlaps it (empty space scores
// high, nav bars/panels/inputs/card grids score low), and choosing the
// best-scoring cell — biased toward "far from where she already is" as a
// tiebreaker, per spec. Nothing here is hardcoded pixels: the grid, the
// margins, and every "safe zone" are all fractions of window.innerWidth/
// innerHeight, recomputed fresh on every call. See _diamondGridCells(),
// _diamondProtectedRects(), _scoreCell(), _bestPositionInRegions().
//
// Every move is still expressed as explicit top+left PIXEL values (never
// bottom/right, never the literal 'auto' — see _setDiamondPosition's own
// comment for why), through the SAME move-queue/transition machinery this
// system always used, so the diamond never teleports.
const DIAMOND_GRID_COLS   = 6
const DIAMOND_GRID_ROWS   = 4
const DIAMOND_GRID_MARGIN = 24   // px kept clear of the literal viewport edge, for every grid cell

// Named regions as viewport FRACTIONS (0..1), not pixels — used both to
// bias idle toward corners and to resolve a user's "muévete a la derecha"/
// "ve a la esquina" request (see the 'diamond_move' socket handler below)
// to a specific area before density-scoring picks the best cell within it.
const DIAMOND_REGIONS = {
  'top-left':      { xMin: 0,    xMax: 0.5,  yMin: 0,    yMax: 0.5  },
  'top-right':     { xMin: 0.5,  xMax: 1,    yMin: 0,    yMax: 0.5  },
  'bottom-left':   { xMin: 0,    xMax: 0.5,  yMin: 0.5,  yMax: 1    },
  'bottom-right':  { xMin: 0.5,  xMax: 1,    yMin: 0.5,  yMax: 1    },
  'top':           { xMin: 0,    xMax: 1,    yMin: 0,    yMax: 0.35 },
  'bottom':        { xMin: 0,    xMax: 1,    yMin: 0.65, yMax: 1    },
  'left':          { xMin: 0,    xMax: 0.35, yMin: 0,    yMax: 1    },
  'right':         { xMin: 0.65, xMax: 1,    yMin: 0,    yMax: 1    },
  'center':        { xMin: 0.3,  xMax: 0.7,  yMin: 0.3,  yMax: 0.7  },
  // Wake/speaking's "center-bottom, drawing attention without blocking":
  // a band above the persistent bar/nav, not literally glued to the edge.
  'bottom-center': { xMin: 0.25, xMax: 0.75, yMin: 0.5,  yMax: 0.85 },
}
const DIAMOND_CORNER_REGIONS = ['top-left', 'top-right', 'bottom-left', 'bottom-right']

function _diamondSize() {
  // Fallback matches .lira-diamond-orb's premium-pass 32px (was 42px).
  return { w: liraDiamond.offsetWidth || 32, h: liraDiamond.offsetHeight || 32 }
}

function _currentDiamondTopLeft() {
  return { top: parseFloat(liraDiamond.style.top) || 0, left: parseFloat(liraDiamond.style.left) || 0 }
}

// The grid itself — cell CENTERS (cx, cy), plus each cell's own w/h for
// scoring. Recomputed fresh from window.innerWidth/innerHeight on every
// call (never cached), so a resize is automatically correct next time
// anything asks for a position — no separate "did the viewport change"
// bookkeeping needed.
function _diamondGridCells() {
  const vw = window.innerWidth, vh = window.innerHeight
  const cellW = (vw - 2 * DIAMOND_GRID_MARGIN) / DIAMOND_GRID_COLS
  const cellH = (vh - 2 * DIAMOND_GRID_MARGIN) / DIAMOND_GRID_ROWS
  const cells = []
  for (let r = 0; r < DIAMOND_GRID_ROWS; r++) {
    for (let c = 0; c < DIAMOND_GRID_COLS; c++) {
      cells.push({
        cx: DIAMOND_GRID_MARGIN + cellW * (c + 0.5),
        cy: DIAMOND_GRID_MARGIN + cellH * (r + 0.5),
      })
    }
  }
  return { cells, cellW, cellH }
}

// Rects LIRA should never sit on top of: nav bars/toolbars (per spec),
// whatever the CURRENT section's own visible interactive content is
// (panels/text inputs/card grids — scoped selectors only, never '*', so
// this stays cheap even on content-heavy sections), and whatever the user
// happens to be hovering right now (never block a button they're about to
// click). Recomputed fresh every time a position decision is made — never
// cached, since focus/hover/section content change constantly.
const DIAMOND_PROTECTED_SELECTOR = [
  '#bottomNav', '#persistentBar', '#appLauncherBtn',
  '.concept-confirm-overlay.open', '#sleepConfirmModal.open', '#updateConfirmModal.open',
  'input:focus', 'textarea:focus',
].join(', ')

function _diamondProtectedRects() {
  const rects = []
  const add = (el) => {
    if (!el) return
    const r = el.getBoundingClientRect()
    if (r.width > 0 && r.height > 0) rects.push(r)
  }
  document.querySelectorAll(DIAMOND_PROTECTED_SELECTOR).forEach(add)

  const activeSection = document.querySelector('.section.active')
  if (activeSection) {
    activeSection.querySelectorAll(
      'input, textarea, select, button, table, .card, .info-row, .concept-card, .armor-card, .settings-toggles'
    ).forEach(add)
  }

  // Deepest element currently under the pointer — if it's interactive,
  // protect it (per spec: "never block ... buttons the user is hovering").
  const hovered = document.querySelectorAll(':hover')
  const deepest = hovered[hovered.length - 1]
  const hoveredInteractive = deepest && deepest.closest && deepest.closest('button, a, input, textarea, [role="button"]')
  if (hoveredInteractive) add(hoveredInteractive)

  return rects
}

function _rectOverlapArea(a, b) {
  const x = Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left))
  const y = Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top))
  return x * y
}

// 0 (fully covered by protected content) .. 1 (completely empty) — scored
// against the cell's own full grid footprint (cellW x cellH), not just the
// diamond's small final size, so "how busy is this general AREA" is what
// gets compared, not just the handful of pixels she'd literally occupy.
function _scoreCell(cell, rects, cellW, cellH) {
  const cellRect = {
    left: cell.cx - cellW / 2, right: cell.cx + cellW / 2,
    top:  cell.cy - cellH / 2, bottom: cell.cy + cellH / 2,
  }
  let overlap = 0
  for (const r of rects) overlap += _rectOverlapArea(cellRect, r)
  return 1 - Math.min(1, overlap / (cellW * cellH))
}

function _regionCells(regionKey, allCells) {
  const region = DIAMOND_REGIONS[regionKey]
  if (!region) return allCells
  const vw = window.innerWidth, vh = window.innerHeight
  return allCells.filter(cell =>
    cell.cx >= region.xMin * vw && cell.cx <= region.xMax * vw &&
    cell.cy >= region.yMin * vh && cell.cy <= region.yMax * vh
  )
}

// The actual algorithm (per spec): score every candidate cell by content
// density, break ties/nudge toward whichever is also FARTHER from her
// current position (so she doesn't just reshuffle within the same few
// pixels), and return the winning cell as explicit {top, left} pixels for
// her actual (diamond-sized) footprint.
function _bestPositionInRegions(regionKeys) {
  const { cells, cellW, cellH } = _diamondGridCells()
  const rects = _diamondProtectedRects()
  let pool = []
  for (const key of regionKeys) pool = pool.concat(_regionCells(key, cells))
  if (!pool.length) pool = cells   // degenerate/tiny viewport — fall back to the whole grid rather than finding nothing

  const { top: curTop, left: curLeft } = _currentDiamondTopLeft()
  const maxDist = Math.hypot(window.innerWidth, window.innerHeight) || 1
  let best = null, bestScore = -Infinity
  for (const cell of pool) {
    const density = _scoreCell(cell, rects, cellW, cellH)
    const dist = Math.hypot(cell.cy - curTop, cell.cx - curLeft) / maxDist
    const score = density + dist * 0.35   // density dominates; distance only breaks ties/nudges away from "already there"
    if (score > bestScore) { bestScore = score; best = cell }
  }
  const { w, h } = _diamondSize()
  return { top: best.cy - h / 2, left: best.cx - w / 2 }
}

function _bestIdlePosition()      { return _bestPositionInRegions(DIAMOND_CORNER_REGIONS) }   // "lowest-density corner" per spec
function _bestAttentionPosition() { return _bestPositionInRegions(['bottom-center']) }         // wake/speaking
function _bestRegionPosition(regionKey) {
  // 'away' (from a bare "muévete"/"quítate de ahí" with no direction) —
  // just get out of the way, same algorithm as an idle re-home.
  if (!regionKey || regionKey === 'away' || !DIAMOND_REGIONS[regionKey]) return _bestIdlePosition()
  return _bestPositionInRegions([regionKey])
}

// ── Move queue ────────────────────────────────────────────────────────────
// "If the diamond is already animating, queue the next position and
// animate to it after the current animation completes" (per spec) — rather
// than letting rapid section-switching retarget the CSS transition
// mid-flight on every call, an in-flight move always finishes first; only
// the LATEST position requested during that time is kept (an older,
// superseded request is simply dropped — no reason to visit a spot the
// caller has already moved past before the diamond ever got there).
//
// Bug fix (the actual "never a teleport" fix): every position here is
// expressed as explicit top+left PIXEL values, never bottom/right, and
// never the literal string 'auto'. CSS transitions cannot interpolate
// between 'auto' and a length (a hard CSS limitation — the same reason
// "animate height to auto" famously doesn't work), so an EARLIER
// bottom/right/auto-toggling version of this code silently SNAPPED
// instead of gliding on whichever axis flipped which side was "active".
// Anchoring everything to top+left in pixels makes every move a pure
// length→length interpolation, which .lira-diamond's transition (500ms,
// cubic-bezier(0.23, 1, 0.32, 1) — see its own CSS comment) can always
// animate.
let _diamondMoving        = false   // true while a move's CSS transition is in flight
let _diamondMoveSettled   = true    // guards against double-handling top's AND left's own separate transitionend events for the same move
let _diamondQueuedMove    = null    // {top, left} of the most recent request made mid-animation
let _diamondMoveFallbackTimer = null
let _diamondPositionSetAt = Date.now()   // last time a REAL position change was applied — anti-annoyance cooldown clock (see _diamondAmbientMoveAllowed)

function _onDiamondMoveSettled() {
  if (_diamondMoveSettled) return   // the other property's transitionend (or the fallback timer) already handled this move
  _diamondMoveSettled = true
  _diamondMoving = false
  clearTimeout(_diamondMoveFallbackTimer)
  if (_diamondQueuedMove) {
    const next = _diamondQueuedMove
    _diamondQueuedMove = null
    _setDiamondPosition(next.top, next.left)
    return   // still mid-glide (e.g. the 2nd leg of an arc) — the bounce plays once only, on the FINAL settle below
  }
  _playDiamondArrivalBounce()
}
liraDiamond.addEventListener('transitionend', ev => {
  if (ev.target === liraDiamond && (ev.propertyName === 'top' || ev.propertyName === 'left')) _onDiamondMoveSettled()
})

function _setDiamondPosition(top, left) {
  top = Math.round(top)
  left = Math.round(left)
  if (_diamondMoving) {
    _diamondQueuedMove = { top, left }
    return
  }
  // Already there — no property actually changes, so no transition (and
  // therefore no transitionend) will fire; treat as a no-op instead of
  // flipping _diamondMoving to true and waiting forever for an event that
  // will never come.
  const currentTop  = parseFloat(liraDiamond.style.top)  || 0
  const currentLeft = parseFloat(liraDiamond.style.left) || 0
  if (currentTop === top && currentLeft === left) return

  _diamondMoving      = true
  _diamondMoveSettled = false
  _diamondPositionSetAt = Date.now()
  liraDiamond.style.top  = `${top}px`
  liraDiamond.style.left = `${left}px`
  // Fallback safety net — if transitionend never fires for some reason
  // (e.g. the element becomes momentarily unrenderable), this guarantees
  // _diamondMoving can never get stuck true forever, which would silently
  // freeze all future diamond movement.
  clearTimeout(_diamondMoveFallbackTimer)
  _diamondMoveFallbackTimer = setTimeout(_onDiamondMoveSettled, 700)
}

// Arrival overshoot — "slight overshoot on arrival, spring feel" (per
// spec). The overshoot comes from the TIMING CURVE, not the keyframe
// values (0% -> 100% is a perfectly ordinary scale(1) -> scale(1.045)
// change): cubic-bezier(0.34, 1.56, 0.64, 1)'s Y control points exceed 1,
// so the interpolated value transiently spikes past 1.045 mid-animation
// before settling exactly there at 100% — that spike IS the "spring".
// Deliberately a temporary class + keyframe animation rather than a second
// permanent transform owner: every element here already has its own
// state-driven transform (.lira-diamond.wake, .lira-diamond-orb/-glow's
// per-state scale rules) — once this animation ends (see the
// 'animationend' listener below) and the class is removed, .lira-diamond's
// own EXISTING transform transition (same spring curve, already used for
// the wake-state grow) smoothly glides back down to whatever the current
// state's resting scale actually is, so this never fights or overrides it.
function _playDiamondArrivalBounce() {
  liraDiamond.classList.remove('arrived')
  void liraDiamond.offsetWidth   // force reflow so re-adding immediately restarts the animation
  liraDiamond.classList.add('arrived')
}
liraDiamond.addEventListener('animationend', ev => {
  if (ev.animationName === 'lira-diamond-arrival-bounce') liraDiamond.classList.remove('arrived')
})

// ── Arc glide — "never a straight line" for a far move ───────────────────
// Short hops go straight to the target (a straight line over a small
// distance doesn't read as mechanical). A long-distance move instead bows
// out through one perpendicular-offset midpoint first — chained through
// the SAME move queue above (calling _setDiamondPosition twice back to
// back queues the second leg automatically once the first's transitionend
// fires, see _onDiamondMoveSettled) — so it's always a genuine two-leg
// glide, never a single straight interpolation end to end.
const DIAMOND_ARC_DISTANCE_THRESHOLD = 260   // px — below this, straight is fine
const DIAMOND_ARC_BOW_FRACTION       = 0.16  // how far the midpoint bows off the straight line, as a fraction of travel distance

function _glideDiamondTo(top, left) {
  const { top: curTop, left: curLeft } = _currentDiamondTopLeft()
  const dx = left - curLeft, dy = top - curTop
  const distance = Math.hypot(dx, dy)
  if (distance < DIAMOND_ARC_DISTANCE_THRESHOLD) {
    _setDiamondPosition(top, left)
    return
  }
  const midX = curLeft + dx / 2, midY = curTop + dy / 2
  const nx = -dy / distance, ny = dx / distance   // unit vector perpendicular to the straight line
  const bow = distance * DIAMOND_ARC_BOW_FRACTION * (Math.random() < 0.5 ? -1 : 1)
  _setDiamondPosition(midY + ny * bow, midX + nx * bow)
  _setDiamondPosition(top, left)
}

// ── Anti-annoyance — gates AMBIENT repositioning only (resize/mutation-
// triggered idle re-homes) ─────────────────────────────────────────────
// State transitions (wake/processing/speaking/idle-after-speaking),
// section changes, and user-commanded moves are NEVER gated by this —
// per spec, the cooldown only applies to LIRA's own passive drifting, and
// explicitly does not apply "when triggered by state change". 8s alone
// satisfies both stated rules ("never more than once every 8s" and "if
// she's been somewhere less than 5s, don't move her again", since 8 > 5).
const DIAMOND_AMBIENT_COOLDOWN_MS = 8000

function _diamondUserIsTyping() {
  const active = document.activeElement
  if (!active) return false
  return (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA') && active.offsetParent !== null
}

function _diamondAmbientMoveAllowed() {
  if (!_diamondEligible()) return false
  if (_liraDiamondState !== 'idle') return false          // wake/processing/speaking own their own position already
  if (Date.now() - _diamondPositionSetAt < DIAMOND_AMBIENT_COOLDOWN_MS) return false
  if (_diamondUserIsTyping()) return false                // "never move during active conversation"
  return true
}

function _diamondAmbientRecompute() {
  if (!_diamondAmbientMoveAllowed()) return
  const { top, left } = _bestIdlePosition()
  _glideDiamondTo(top, left)
}

// Recalculation triggers (per spec): window resize and "any DOM mutation
// that adds/removes significant content" — both debounced (content tends
// to change in bursts, e.g. a whole card grid rendering at once) and
// scoped to #sectionContainer only (the diamond's own style/text mutations
// live outside it, so this can't self-trigger a feedback loop).
let _diamondResizeTimer = null
window.addEventListener('resize', () => {
  clearTimeout(_diamondResizeTimer)
  _diamondResizeTimer = setTimeout(_diamondAmbientRecompute, 350)
})

let _diamondMutationTimer = null
const _diamondMutationObserver = new MutationObserver(() => {
  clearTimeout(_diamondMutationTimer)
  _diamondMutationTimer = setTimeout(_diamondAmbientRecompute, 500)
})
{
  const _sectionContainerEl = document.getElementById('sectionContainer')
  if (_sectionContainerEl) _diamondMutationObserver.observe(_sectionContainerEl, { childList: true, subtree: true })
}

// (the 'diamond_move' socket handler itself now lives inside
// _attemptConnect(), registered against the real jarvisSocket connection —
// see that function's own comment on the boot-crashing bug this fixes)

// ── State machine — idle / wake / processing / speaking ──────────────────
// Position only changes on entering 'idle' (best low-density corner) or
// 'wake'/'speaking' (best spot in the bottom-center region); 'processing'
// deliberately never repositions — "stays in current position" per spec,
// spinning its perimeter arc wherever wake/idle already left it. State
// transitions always move immediately, never gated by the ambient cooldown
// above (per spec: "unless triggered by state change").
let _liraDiamondState = 'idle'

function _applyDiamondState(state) {
  if (state === _liraDiamondState) {
    // Already in this state — 'idle' still re-homes to the CURRENT best
    // spot, since _currentSection (and the DOM around her) can change
    // while status stays 'listening' the whole time (see switchSection's
    // own hook, which calls this same path).
    if (state === 'idle') { const { top, left } = _bestIdlePosition(); _glideDiamondTo(top, left) }
    return
  }
  _liraDiamondState = state
  liraDiamond.classList.remove('idle', 'wake', 'processing', 'speaking')
  liraDiamond.classList.add(state)

  if (state === 'idle') {
    const { top, left } = _bestIdlePosition()
    _glideDiamondTo(top, left)
  } else if (state === 'wake' || state === 'speaking') {
    const { top, left } = _bestAttentionPosition()
    _glideDiamondTo(top, left)
  }
}

// Wake word detected (see the 'log' handler above) — a transient attention
// state: grows + pulses + glides to the bottom-center region immediately,
// then either settles into 'processing' (a real command followed — same
// position, _applyDiamondState('processing') is a no-op move) or, if
// nothing followed within the hold window (false alarm / cooldown-ignored
// trigger), quietly returns to 'idle' at its best corner.
const LIRA_DIAMOND_WAKE_HOLD_MS = 3000
let _diamondWakeTimer = null

function _triggerDiamondWake() {
  if (!_diamondEligible()) return
  clearTimeout(_diamondWakeTimer)
  _applyDiamondState('wake')
  _diamondWakeTimer = setTimeout(() => {
    if (currentStatus !== 'processing' && currentStatus !== 'speaking') _applyDiamondState('idle')
  }, LIRA_DIAMOND_WAKE_HOLD_MS)
}

// Initial position, before any status/section event has fired — instant
// (no glide/arc/bounce needed before she's even visible for the first time).
{ const { top, left } = _bestIdlePosition(); _setDiamondPosition(top, left) }

// ── Organic reveal/dissolve — see .lira-organic-word's own CSS comment for
// the full rationale and why this is separate from _typewriterReveal().
// Shared by every LIRA-own response text surface (_showDiamondText below,
// _showMMFloatingText, _openDiamondBubble). ──
function _organicReveal(el, text) {
  el.innerHTML = ''
  // Bug fix: whitespace segments used to get wrapped in their OWN
  // .lira-organic-word span (display:inline-block) same as real words —
  // but a display:inline-block element whose entire content is a single
  // whitespace character has that whitespace collapsed to zero width by
  // the browser's normal text-layout rules (leading/trailing whitespace
  // trimming applies within the span's own isolated inline-block content,
  // and since the space IS that content, it collapses to nothing). That
  // silently ate every space between words — "Estás en la pantalla"
  // rendered as "Estásenlapantalla". Whitespace segments are now appended
  // as plain text nodes instead, which flow normally in the surrounding
  // text and are never subject to that isolated-collapse — only real
  // words get their own animated span.
  const parts = String(text || '').split(/(\s+)/).filter(p => p.length)
  let wordIndex = 0
  parts.forEach(part => {
    if (/^\s+$/.test(part)) {
      el.appendChild(document.createTextNode(part))
      return
    }
    const span = document.createElement('span')
    span.className = 'lira-organic-word'
    span.textContent = part
    span.style.animationDelay = `${wordIndex * 45}ms`
    el.appendChild(span)
    wordIndex++
  })
}

// Plays the "dissolves outward like dissipating energy" exit on every word
// already in `el`, then calls onDone once the LAST word's own animation
// genuinely finishes (via 'animationend', never a hardcoded setTimeout
// that could drift out of sync with the CSS duration/stagger above).
function _organicDissolve(el, onDone) {
  const words = el.querySelectorAll('.lira-organic-word')
  if (!words.length) { if (onDone) onDone(); return }
  let remaining = words.length
  words.forEach((span, i) => {
    span.style.animationDelay = `${i * 20}ms`
    span.classList.add('lira-dissolving')
    span.addEventListener('animationend', () => {
      remaining--
      if (remaining <= 0 && onDone) onDone()
    }, { once: true })
  })
}

// ── Sentence chunking + reading-pace, for long responses ─────────────────
// A long reply used to appear all at once and sit there for a single fixed
// hold — unreadable if it ran more than a sentence or two, especially with
// TTS muted (no "while she's speaking" runway at all in that case). Long
// text now paces itself through one sentence/chunk at a time, each held
// for its own calculated reading time, independent of TTS timing (status
// is a coarse speaking/not-speaking signal with no audio-position info to
// sync against, and a reader going at their own pace — or with TTS off —
// needs pacing that doesn't depend on it anyway). Very short (single-
// chunk) responses are untouched — see the `chunks.length <= 1` branches
// below, which are exactly the original behavior.
function _splitIntoChunks(text) {
  const raw = String(text || '').trim()
  if (!raw) return []
  const chunks = []
  let start = 0
  for (let i = 0; i < raw.length; i++) {
    const ch = raw[i]
    if (ch === '.' || ch === '!' || ch === '?' || ch === '…') {
      // Don't split mid-decimal ("3.5", "$3.99") — only a real sentence
      // boundary if the char right after isn't a digit.
      if (ch === '.' && /\d/.test(raw[i + 1] || '')) continue
      // Swallow any further terminators right after ("...", "?!") into the
      // same boundary instead of splitting between them.
      let j = i + 1
      while (j < raw.length && /[.!?…]/.test(raw[j])) j++
      chunks.push(raw.slice(start, j).trim())
      start = j
      i = j - 1
    }
  }
  if (start < raw.length) {
    const rest = raw.slice(start).trim()
    if (rest) chunks.push(rest)
  }
  return chunks.filter(Boolean)
}

// ~200ms/word, floored at 2s and ceilinged at 6s per chunk, per spec —
// "roughly 3-4 seconds per sentence" is the typical case in that range for
// an ordinary 15-20 word sentence.
const CHUNK_READ_MS_PER_WORD = 200
const CHUNK_READ_MS_MIN      = 2000
const CHUNK_READ_MS_MAX      = 6000
function _readingTimeMs(text) {
  const words = String(text || '').trim().split(/\s+/).filter(Boolean).length
  return Math.max(CHUNK_READ_MS_MIN, Math.min(CHUNK_READ_MS_MAX, words * CHUNK_READ_MS_PER_WORD))
}

// Generic multi-chunk cycling driver, shared by the diamond and Main Menu
// text below — reveals chunks[i], holds it for its own reading time, then
// organically dissolves and advances. onAllDone() fires once the LAST
// chunk's hold has elapsed and it has finished dissolving. Returns a
// cancel() the caller invokes if a NEW call supersedes this one mid-cycle
// (e.g. a fresh reply — or even just a live partial-transcript update —
// arrives before the previous cycle finished).
function _cycleChunks(el, chunks, onAllDone) {
  let cancelled = false
  let timer = null
  function playChunk(i) {
    if (cancelled) return
    if (i >= chunks.length) { if (onAllDone) onAllDone(); return }
    _organicReveal(el, chunks[i])
    timer = setTimeout(() => {
      if (cancelled) return
      _organicDissolve(el, () => { if (!cancelled) playChunk(i + 1) })
    }, _readingTimeMs(chunks[i]))
  }
  playChunk(0)
  return () => { cancelled = true; clearTimeout(timer) }
}

// ── Response text — fades in above the diamond. Per spec, after she stops
// speaking the diamond holds for 3s (text fading, still at bottom-center),
// then glides back to its corner — see _scheduleDiamondTextHide(). That
// original single-chunk timing is untouched; a multi-chunk response instead
// paces itself via _cycleChunks() above and glides back the moment its own
// last chunk finishes, without waiting on this fixed hold at all — each
// chunk already got its own full reading time on the way there. ──
const LIRA_DIAMOND_TEXT_HOLD_MS = 3000
let _diamondTextHideTimer  = null
let _diamondChunkCancel    = null   // non-null while a multi-chunk cycle owns liraDiamondText's lifecycle

function _showDiamondText(text) {
  clearTimeout(_diamondTextHideTimer)
  if (_diamondChunkCancel) { _diamondChunkCancel(); _diamondChunkCancel = null }
  liraDiamondText.classList.remove('visible')
  void liraDiamondText.offsetWidth   // force reflow so the fade-in replays even if already visible
  liraDiamondText.classList.add('visible')

  const chunks = _splitIntoChunks(text)
  if (chunks.length <= 1) {
    _organicReveal(liraDiamondText, text)
    return
  }
  _diamondChunkCancel = _cycleChunks(liraDiamondText, chunks, () => {
    _diamondChunkCancel = null
    liraDiamondText.classList.remove('visible')
    liraDiamondText.innerHTML = ''
    // Glide back to the corner now — unless a NEW turn already started
    // while this was still cycling (setStatus's own sync already moved
    // the diamond for it; don't yank it back mid-turn).
    if (currentStatus !== 'processing' && currentStatus !== 'speaking') _applyDiamondState('idle')
  })
}
function _scheduleDiamondTextHide() {
  clearTimeout(_diamondTextHideTimer)
  if (_diamondChunkCancel) return   // a multi-chunk cycle owns the lifecycle now — let it finish at its own pace
  _diamondTextHideTimer = setTimeout(() => {
    _organicDissolve(liraDiamondText, () => {
      liraDiamondText.classList.remove('visible')
      liraDiamondText.innerHTML = ''
    })
    // Glide back to the corner now — unless a NEW turn already started
    // during the hold (setStatus's own sync already moved the diamond to
    // 'processing'/'speaking' for it; don't yank it back mid-turn).
    if (currentStatus !== 'processing' && currentStatus !== 'speaking') _applyDiamondState('idle')
  }, LIRA_DIAMOND_TEXT_HOLD_MS)
}

// ── Click-to-expand bubble ──────────────────────────────────────────────
const LIRA_DIAMOND_BUBBLE_TIMEOUT_MS = 8000
let _diamondBubbleTimer = null

// Grows away from whichever screen edge the (autonomously positioned, so
// varies by state/section) diamond is currently closest to, so the bubble
// never opens off-screen.
function _positionDiamondBubble() {
  const rect     = liraDiamond.getBoundingClientRect()
  const growLeft = rect.left > window.innerWidth  / 2
  const growUp   = rect.top  > window.innerHeight / 2
  liraDiamondBubble.style.right  = growLeft ? '0'    : 'auto'
  liraDiamondBubble.style.left   = growLeft ? 'auto' : '0'
  liraDiamondBubble.style.bottom = growUp   ? 'calc(100% + 14px)' : 'auto'
  liraDiamondBubble.style.top    = growUp   ? 'auto' : 'calc(100% + 14px)'
}

function _resetDiamondBubbleTimer() {
  clearTimeout(_diamondBubbleTimer)
  _diamondBubbleTimer = setTimeout(_closeDiamondBubble, LIRA_DIAMOND_BUBBLE_TIMEOUT_MS)
}
function _openDiamondBubble() {
  // _organicReveal('') on an empty _lastJarvisReply still ends up with
  // el genuinely empty (innerHTML cleared, no spans appended), so
  // .lira-diamond-bubble-text:empty::before's placeholder still applies.
  _organicReveal(liraDiamondBubbleText, _lastJarvisReply)
  _positionDiamondBubble()
  liraDiamond.classList.add('open')
  _resetDiamondBubbleTimer()
}
function _closeDiamondBubble() {
  liraDiamond.classList.remove('open')
  clearTimeout(_diamondBubbleTimer)
}
function _toggleDiamondBubble() {
  if (liraDiamond.classList.contains('open')) _closeDiamondBubble()
  else _openDiamondBubble()
}

liraDiamondInput.addEventListener('input', _resetDiamondBubbleTimer)
liraDiamondInput.addEventListener('keydown', e => {
  _resetDiamondBubbleTimer()
  if (e.key === 'Enter') sendTextCommand(liraDiamondInput)
})

// Tap outside the bubble closes it (per spec) — capture phase so this
// still sees the click even if something inside a section stops
// propagation. Only closes the BUBBLE, not the diamond itself.
document.addEventListener('click', (e) => {
  if (!liraDiamond.classList.contains('open')) return
  if (liraDiamond.contains(e.target)) return
  _closeDiamondBubble()
}, { capture: true })

// ── Click to open/close the bubble — not draggable (see the top of this
// section: LIRA controls her own position autonomously). ─────────────────
liraDiamondOrb.addEventListener('click', () => _toggleDiamondBubble())


// ── Main menu floating text ──────────────────────────────────────────────
// Hold duration for the user's own brief echo, and the fallback hold for a
// single-chunk (short) reply from LIRA once she stops speaking (see
// setStatus's speaking→not-speaking transition below, which arms this
// timer at that point — so a short reply stays up for the full time she's
// actually speaking, not just a fixed few seconds from when the text first
// arrived). A multi-chunk (long) reply from LIRA instead paces itself via
// _cycleChunks() — see _showMMFloatingText below — and never reaches this
// timer at all, same split as the floating diamond's own text above.
const MM_FLOATING_TEXT_HOLD_MS = 4000
let _mmFloatingHideTimer = null
let _mmChunkCancel       = null   // non-null while a multi-chunk cycle owns mmFloatingText's lifecycle

function _showMMFloatingText(text, isUserEcho) {
  clearTimeout(_mmFloatingHideTimer)
  if (_mmChunkCancel) { _mmChunkCancel(); _mmChunkCancel = null }
  mmFloatingText.classList.toggle('user-echo', !!isUserEcho)
  mmFloatingText.classList.remove('visible')
  void mmFloatingText.offsetWidth
  mmFloatingText.classList.add('visible')
  // Organic word-by-word reveal (and chunk pacing) is specifically LIRA's
  // own response text (per spec) — the user's own brief echo alongside it
  // stays a plain, instant set + the existing container-level opacity
  // fade, unchanged.
  if (isUserEcho) { mmFloatingText.textContent = text; return }

  const chunks = _splitIntoChunks(text)
  if (chunks.length <= 1) {
    _organicReveal(mmFloatingText, text)
    return
  }
  _mmChunkCancel = _cycleChunks(mmFloatingText, chunks, () => {
    _mmChunkCancel = null
    mmFloatingText.classList.remove('visible')
  })
}
function _scheduleMMFloatingTextHide() {
  clearTimeout(_mmFloatingHideTimer)
  if (_mmChunkCancel) return   // a multi-chunk cycle owns the lifecycle now — let it finish at its own pace
  _mmFloatingHideTimer = setTimeout(() => {
    if (mmFloatingText.classList.contains('user-echo')) {
      mmFloatingText.classList.remove('visible')
    } else {
      _organicDissolve(mmFloatingText, () => mmFloatingText.classList.remove('visible'))
    }
  }, MM_FLOATING_TEXT_HOLD_MS)
}

mmFloatingInput.addEventListener('keydown', e => { if (e.key === 'Enter') sendTextCommand(mmFloatingInput) })

// ════════════════════════════════════════════════════════════════════════════
// APP LAUNCHER — floating button + slide-out row, always visible (fixed,
// outside #sectionContainer, same as #persistentBar it sits beside). Apps
// are a plain array so adding one is a single entry — see #appLauncher's
// own HTML comment for the {id, icon, label, action} shape.
// ════════════════════════════════════════════════════════════════════════════
const APP_LAUNCHER_APPS = [
  {
    id:     'core',
    icon:   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 L20 12 L12 21 L4 12 Z"/><path d="M12 3 V21 M4 12 H20"/></svg>',
    label:  'NÚCLEO LIRA',
    action: () => switchSection('core'),
  },
  {
    id:     'control',
    icon:   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="6" x2="20" y2="6"/><circle cx="9" cy="6" r="2"/><line x1="4" y1="12" x2="20" y2="12"/><circle cx="15" cy="12" r="2"/><line x1="4" y1="18" x2="20" y2="18"/><circle cx="7" cy="18" r="2"/></svg>',
    label:  'CONTROL',
    action: () => switchSection('control'),
  },
  {
    id:     'armor',
    icon:   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4.5 16 C4.5 8.5 7.8 4 12 4 C16.2 4 19.5 8.5 19.5 16 C19.5 17.5 18.3 18.5 17 18.5 H7 C5.7 18.5 4.5 17.5 4.5 16 Z"/><path d="M4.5 13.5 H19.5"/></svg>',
    label:  'Armaduras',
    action: () => switchSection('armor'),
  },
  // Add new apps here — one line each: { id, icon (SVG string), label, action }
]

const appLauncher    = document.getElementById('appLauncher')
const appLauncherBtn = document.getElementById('appLauncherBtn')
const appLauncherRow = document.getElementById('appLauncherRow')

function _renderAppLauncherRow() {
  appLauncherRow.innerHTML = APP_LAUNCHER_APPS.map(app => `
    <button class="app-icon-btn" data-app="${esc(app.id)}">
      ${app.icon}
      <span class="app-icon-tooltip">${esc(app.label)}</span>
    </button>
  `).join('')
  appLauncherRow.querySelectorAll('.app-icon-btn').forEach((btn, i) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation()
      _closeAppLauncher()
      APP_LAUNCHER_APPS[i].action()
    })
  })
}
_renderAppLauncherRow()

function _openAppLauncher() {
  // Stagger delay set fresh right before opening, one line per icon —
  // "each icon appears slightly after the previous".
  appLauncherRow.querySelectorAll('.app-icon-btn').forEach((btn, i) => {
    btn.style.transitionDelay = `${i * 70}ms`
  })
  appLauncher.classList.add('open')
}
function _closeAppLauncher() {
  appLauncher.classList.remove('open')
  appLauncherRow.querySelectorAll('.app-icon-btn').forEach(btn => { btn.style.transitionDelay = '0ms' })
}

appLauncherBtn.addEventListener('click', () => {
  if (appLauncher.classList.contains('open')) _closeAppLauncher()
  else _openAppLauncher()
})

// Tap outside collapses the row — capture phase, same pattern as the
// unified floating diamond's own bubble-close dismissal above.
document.addEventListener('click', (e) => {
  if (!appLauncher.classList.contains('open')) return
  if (appLauncher.contains(e.target)) return
  _closeAppLauncher()
}, { capture: true })

// ════════════════════════════════════════════════════════════════════════════
// LIRA CORE — Estado / Pensamiento / Memoria / Mapa. Reached only via the
// app launcher (switchSection('core')). Own independent sub-tab state
// (_currentCoreSub), separate from Armor Bay's _currentSub even though
// both reuse .armor-subtabs/.armor-subtab's generic CSS.
// ════════════════════════════════════════════════════════════════════════════
let _currentCoreSub      = 'estado'
let _coreEstadoPollTimer = null
const CORE_ESTADO_POLL_MS = 2500   // fallback tick; the socket hooks above make most changes feel instant

function _switchCoreSubTab(sub) {
  _currentCoreSub = sub
  document.querySelectorAll('#section-core .armor-subtab').forEach(b => b.classList.toggle('active', b.dataset.coreSub === sub))
  document.querySelectorAll('.core-panel').forEach(p => p.classList.remove('active'))
  const panel = document.getElementById(`core${sub.charAt(0).toUpperCase()}${sub.slice(1)}Panel`)
  if (panel) panel.classList.add('active')

  if (sub === 'estado')           _renderCoreEstado()
  else if (sub === 'pensamiento') { _loadThinkLog(); _loadSleepInsights() }
  else if (sub === 'memoria')     _renderCoreMemoria()
  else if (sub === 'mapa')        _renderCoreMapa()
}

document.querySelectorAll('#section-core .armor-subtab').forEach(btn => {
  btn.addEventListener('click', () => _switchCoreSubTab(btn.dataset.coreSub))
})

document.getElementById('coreClose').addEventListener('click', () => switchSection('home'))
document.getElementById('controlClose').addEventListener('click', () => switchSection('home'))

// ── Estado ────────────────────────────────────────────────────────────────
const CORE_MODE_LABELS = { wake_word: 'Wake Word', conversation: 'Conversación' }

async function _renderCoreEstado() {
  const body = document.getElementById('coreEstadoBody')
  if (!body) return

  let info = {}
  try {
    const res = await fetch(`${JARVIS_API}/api/info`)
    info = await res.json()
  } catch { /* leave info empty — still show whatever's already tracked locally below */ }

  const latency    = info.last_latency || {}
  const latencyStr = latency.total != null ? `${latency.total.toFixed(2)}s` : '—'
  const modelStr    = latency.model || (info.groq_model_chain && info.groq_model_chain[0]) || '—'

  // Reflective mode's token budget — see core.reflective / GET /api/info's
  // 'reflective' field and data/reflective_budget.json.
  const reflective = info.reflective || {}
  const reflectiveStr = (reflective.tokens_used_today != null && reflective.daily_budget != null)
    ? `Modo reflexivo: ${reflective.tokens_used_today}/${reflective.daily_budget} tokens usados hoy`
    : '—'
  let lastSessionStr = 'Aún no hay sesiones reflexivas'
  if (reflective.last_session_at) {
    const when = new Date(reflective.last_session_at).toLocaleString('es-ES', {
      day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
    })
    const insights = reflective.last_session_insights || 0
    lastSessionStr = `${when} — ${insights} insight${insights === 1 ? '' : 's'} generado${insights === 1 ? '' : 's'}`
  }

  // Próximo ciclo — rough estimate of when the 20-minute idle auto-trigger
  // could next fire, while nothing is currently sleeping. The detailed
  // "last sleep" stats (cycles/duration/deleted/merged/promoted/insights/
  // connections) live in their own dedicated ÚLTIMO SUEÑO section below
  // (see _renderSleepSummarySection()), sourced from the purpose-built
  // GET /api/sleep_summary rather than folded into these generic rows.
  let nextSleepStr = '—'
  try {
    const sleepRes    = await fetch(`${JARVIS_API}/api/sleep/status`)
    const sleepStatus = await sleepRes.json()
    if (!(sleepStatus.continuous && sleepStatus.continuous.running) && sleepStatus.next_trigger_seconds != null) {
      const mins = Math.round(sleepStatus.next_trigger_seconds / 60)
      nextSleepStr = mins <= 0 ? 'En cualquier momento' : `En ~${mins} min`
    } else if (sleepStatus.continuous && sleepStatus.continuous.running) {
      nextSleepStr = 'Durmiendo ahora — ver ÚLTIMO SUEÑO abajo'
    }
  } catch { /* leave default — a failed fetch here shouldn't blank out the rest of Estado */ }

  const rows = [
    ['Personalidad',    ((typeof PERSONALITY_LABEL !== 'undefined' && PERSONALITY_LABEL[currentPersonality]) || currentPersonality)],
    ['Modo',             CORE_MODE_LABELS[_listenMode] || _listenMode || '—'],
    ['Tiempo activo',    info.session_uptime || '—'],
    ['Última latencia',  latencyStr],
    ['Modelo Groq',      modelStr],
    ['Micrófono',        _isMuted ? 'Silenciado' : 'Activo'],
    ['Voz (TTS)',        _isTtsMuted ? 'Silenciada' : 'Activa'],
    ['Conexión',         (jarvisSocket && jarvisSocket.connected) ? 'Conectado' : 'Desconectado'],
    ['Presupuesto reflexivo', reflectiveStr],
    ['Última sesión reflexiva', lastSessionStr],
    ['Próximo ciclo de sueño', nextSleepStr],
  ]
  body.innerHTML = rows.map(([k, v]) => `
    <div class="info-row core-fact-row"><span class="info-key">${esc(k)}</span><span class="info-val">${esc(String(v))}</span></div>
  `).join('')

  body.innerHTML += await _renderSleepSummarySection()
}

// ── ÚLTIMO SUEÑO — see GET /api/sleep_summary / core.sleep.get_sleep_summary().
// Three states: currently sleeping (pulsing "DURMIENDO — Ciclo X · Fase Y"
// line, per spec), never slept ("Sin ciclos de sueño registrados aún"), or
// a finished run's stats (date/time, cycles, duration, deleted/merged/
// promoted facts, insights generated, mind-map connections updated).
async function _renderSleepSummarySection() {
  let summary = null
  try {
    const res = await fetch(`${JARVIS_API}/api/sleep_summary`)
    summary = await res.json()
  } catch { return '' }
  if (!summary || summary.error) return ''

  if (summary.current && summary.current.running) {
    const c = summary.current
    return `
      <div class="core-section-label">ÚLTIMO SUEÑO</div>
      <div class="core-sleep-status core-sleep-status-active">DURMIENDO — Ciclo ${c.current_cycle || 0} · Fase ${c.current_phase_num || 0}: ${esc(c.current_phase || '…')}</div>
    `
  }

  if (!summary.has_ever_slept) {
    return `
      <div class="core-section-label">ÚLTIMO SUEÑO</div>
      <div class="core-empty-note">Sin ciclos de sueño registrados aún</div>
    `
  }

  const whenSource = summary.stopped_at || summary.started_at
  const when = whenSource
    ? new Date(whenSource).toLocaleString('es-ES', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
    : '—'
  const durationStr = summary.duration_seconds == null
    ? '—'
    : summary.duration_seconds < 60
      ? `${Math.round(summary.duration_seconds)}s`
      : `${Math.round(summary.duration_seconds / 60)} min`

  const statRows = [
    ['Fecha/hora',              when],
    ['Ciclos completados',      String(summary.total_cycles_completed || 0)],
    ['Duración total',          durationStr],
    ['Hechos eliminados',       String(summary.total_deleted || 0)],
    ['Hechos fusionados',       String(summary.total_merged || 0)],
    ['Hechos promovidos',       String(summary.total_promoted || 0)],
    ['Insights generados',      String(summary.total_insights_generated || 0)],
    ['Conexiones actualizadas', String(summary.total_mind_map_updates || 0)],
  ]
  return `
    <div class="core-section-label">ÚLTIMO SUEÑO</div>
    ${statRows.map(([k, v]) => `<div class="info-row core-fact-row"><span class="info-key">${esc(k)}</span><span class="info-val">${esc(v)}</span></div>`).join('')}
  `
}

function _startCoreEstadoPoll() {
  _stopCoreEstadoPoll()
  _coreEstadoPollTimer = setInterval(() => {
    if (_currentSection === 'core' && _currentCoreSub === 'estado') _renderCoreEstado()
  }, CORE_ESTADO_POLL_MS)
}
function _stopCoreEstadoPoll() {
  clearInterval(_coreEstadoPollTimer)
  _coreEstadoPollTimer = null
}

// ── Pensamiento ──────────────────────────────────────────────────────────
let _coreThinkEntries = []   // newest first, capped at 10 — see _onLiraThinking()

// Reveals `text` progressively into `el` — a subtle typewriter effect, not
// genuine token-by-token streaming (the backend emits the whole finished
// block in one 'lira_thinking' event — see core.commands._groq_complete()).
// Reveals in small chunks rather than one character at a time so a long
// block doesn't take unreasonably long to finish appearing.
function _typewriterReveal(el, text) {
  el.textContent = ''
  let i = 0
  const step = Math.max(1, Math.ceil(text.length / 120))
  const timer = setInterval(() => {
    i += step
    el.textContent = text.slice(0, i)
    if (i >= text.length) clearInterval(timer)
  }, 12)
}

function _renderCoreThinkList() {
  const list = document.getElementById('coreThinkList')
  if (!list) return
  if (!_coreThinkEntries.length) {
    list.innerHTML = '<div class="core-think-empty">Modelo sin razonamiento visible</div>'
    return
  }
  list.innerHTML = _coreThinkEntries.map((e, i) => `
    <div class="core-think-card">
      <div class="core-think-query">${esc(e.query || '—')}</div>
      <div class="core-think-body" id="coreThinkBody${i}"></div>
      <div class="core-think-model">${esc(e.model || '')}</div>
    </div>
  `).join('')
  // Only the newest LIVE arrival plays the typewriter effect — backfilled
  // history from GET /api/think_log already happened, so it just appears.
  _coreThinkEntries.forEach((e, i) => {
    const bodyEl = document.getElementById(`coreThinkBody${i}`)
    if (!bodyEl) return
    if (i === 0 && e._fresh) _typewriterReveal(bodyEl, e.thinking || '')
    else bodyEl.textContent = e.thinking || ''
  })
}

async function _loadThinkLog() {
  if (_coreThinkEntries.length) { _renderCoreThinkList(); return }   // already have live/backfilled data
  try {
    const res  = await fetch(`${JARVIS_API}/api/think_log`)
    const data = await res.json()
    _coreThinkEntries = (data.entries || []).map(e => ({ ...e, _fresh: false }))
  } catch { /* leave whatever's already there */ }
  _renderCoreThinkList()
}

function _onLiraThinking(data) {
  _coreThinkEntries.unshift({ ...data, _fresh: true })
  if (_coreThinkEntries.length > 10) _coreThinkEntries.length = 10
  if (_currentSection === 'core' && _currentCoreSub === 'pensamiento') _renderCoreThinkList()
}

// ── Sleep insights (PREGUNTAS DURANTE EL SUEÑO / REFLEXIONES DEL SUEÑO) ──
// GET /api/sleep_insights — see core.sleep.get_sleep_insights_summary().
// Both lists reuse .core-think-card/-body/-model (same card shape as the
// thinking feed above, no new styling needed) rather than a "query" line.
function _renderSleepQuestionsList(questions) {
  const list = document.getElementById('coreSleepQuestionsList')
  if (!list) return
  if (!questions || !questions.length) {
    list.innerHTML = '<div class="core-think-empty">Sin preguntas pendientes</div>'
    return
  }
  list.innerHTML = questions.map(q => {
    const pct  = Math.round((q.confidence || 0) * 100)
    const meta = [q.cycle != null ? `Ciclo ${q.cycle}` : null, `Confianza ${pct}%`].filter(Boolean).join(' · ')
    return `
      <div class="core-think-card${q.resolved ? ' core-sleep-resolved' : ''}">
        <div class="core-think-body">${esc(q.text)}</div>
        <div class="core-think-model">${esc(meta)}${q.resolved ? ' · <span class="core-sleep-resolved-tag">✓ Resuelta</span>' : ''}</div>
      </div>
    `
  }).join('')
}

function _renderSleepReflectionsList(reflections) {
  const list = document.getElementById('coreSleepReflectionsList')
  if (!list) return
  if (!reflections || !reflections.length) {
    list.innerHTML = '<div class="core-think-empty">Sin reflexiones registradas aún</div>'
    return
  }
  list.innerHTML = reflections.map(r => {
    const meta = [r.phase, r.cycle != null ? `Ciclo ${r.cycle}` : null].filter(Boolean).join(' · ')
    return `
      <div class="core-think-card">
        <div class="core-think-body">${esc(r.text)}</div>
        <div class="core-think-model">${esc(meta)}</div>
      </div>
    `
  }).join('')
}

async function _loadSleepInsights() {
  try {
    const res  = await fetch(`${JARVIS_API}/api/sleep_insights`)
    const data = await res.json()
    _renderSleepQuestionsList(data.questions || [])
    _renderSleepReflectionsList(data.reflections || [])
  } catch { /* leave whatever's already there */ }
}

// ── Memoria ──────────────────────────────────────────────────────────────
const CORE_CATEGORY_LABELS = {
  personal: 'Personal', preference: 'Preferencias', project: 'Proyectos', skill: 'Habilidades',
  relationship: 'Relaciones', interaction: 'Interacción', context: 'Contexto', reference: 'Referencias',
}

async function _renderCoreMemoria() {
  const body = document.getElementById('coreMemoriaBody')
  if (!body) return
  body.innerHTML = '<div class="core-empty-note">Cargando…</div>'

  let data = {}
  try {
    const res = await fetch(`${JARVIS_API}/api/memory_active`)
    data = await res.json()
  } catch {
    body.innerHTML = '<div class="core-empty-note">No se pudo cargar la memoria.</div>'
    return
  }

  const facts    = data.facts || {}
  const episodes = data.episodes || []
  const hud      = data.hud_context || {}

  let html = '<div class="core-section-label">Hechos activos</div>'
  const categories = Object.keys(facts)
  if (!categories.length) {
    html += '<div class="core-empty-note">Sin hechos guardados aún.</div>'
  } else {
    categories.forEach(cat => {
      html += `<div class="core-section-label">${esc(CORE_CATEGORY_LABELS[cat] || cat)}</div>`
      facts[cat].forEach(f => {
        html += `<div class="info-row core-fact-row"><span class="info-val" style="text-align:left">${esc(f.fact)}</span></div>`
      })
    })
  }

  html += '<div class="core-section-label">Episodios recientes</div>'
  if (!episodes.length) {
    html += '<div class="core-empty-note">Sin episodios guardados aún.</div>'
  } else {
    episodes.forEach(e => {
      html += `
        <div class="core-episode-card">
          <div class="core-episode-head"><span>${esc(e.date || '')}</span><span>Importancia ${esc(String(e.importance ?? '—'))}</span></div>
          <div class="core-episode-summary">${esc(e.summary || '')}</div>
        </div>`
    })
  }

  html += '<div class="core-section-label">Contexto de pantalla actual</div>'
  if (hud.type) {
    html += `<div class="info-row core-fact-row"><span class="info-key">Tipo</span><span class="info-val">${esc(hud.type)}</span></div>`
  } else {
    html += '<div class="core-empty-note">Sin contexto de pantalla activo.</div>'
  }

  body.innerHTML = html
}

// ── Mapa Mental ──────────────────────────────────────────────────────────
// Interactive D3 force-directed graph — memory facts, episodes, armor
// models (ARMOR_DATA, defined later in this file — safe to reference here
// since this function is only ever CALLED from a user click, by which
// point the whole script has finished its top-to-bottom parse, same
// hoisting pattern used throughout this file) and concepts, pulled from
// GET /api/memory_active (facts/episodes/concepts, see
// core.commands.get_active_memory) plus the inline ARMOR_DATA constant,
// which mirrors data/armor_knowledge.json exactly (see its own comment) —
// the same source Armor Bay itself already renders from, so this doesn't
// introduce a second, divergent copy of that data.
//
// Edge logic mirrors core/commands.py's own _fact_similarity/_keywords
// design ("cheap, dependency-free... simple keyword matching") rather than
// inventing a different heuristic: two texts are considered related if
// they share at least one meaningful (length > 2, non-stopword) word.
const MAPA_STOPWORDS_ES = new Set([
  'el','la','los','las','un','una','unos','unas','de','del','al','y','o','u',
  'que','en','a','por','para','con','sin','es','son','se','su','sus','lo',
  'le','les','mi','mis','tu','tus','yo','tú','él','ella','nosotros',
  'vosotros','ellos','ellas','me','te','nos','os','más','pero','como',
  'cuando','donde','qué','quién','cómo','cuál','cuáles','cuánto','cuánta',
  'cuántos','muy','ya','este','esta','esto','estos','estas','ese','esa',
  'eso','esos','esas','también','hay','no','sí','si','soy','eres','era',
  'fue','ser','estar','está','están','he','has','ha','han',
])
function _mapaKeywords(text) {
  const words = String(text || '').toLowerCase().match(/[\p{L}\p{N}_]+/gu) || []
  return new Set(words.filter(w => w.length > 2 && !MAPA_STOPWORDS_ES.has(w)))
}
function _mapaShareKeyword(a, b) {
  for (const w of a) if (b.has(w)) return true
  return false
}

const CORE_MAP_NODE_RADIUS = { fact: 4, episode: 9, armor: 7, concept: 7 }

function _mapaDiamondPath(r) { return `M0,${-r} L${r},0 L0,${r} L${-r},0 Z` }
function _mapaHexPath(r) {
  const pts = []
  for (let i = 0; i < 6; i++) {
    const a = (Math.PI / 3) * i - Math.PI / 2
    pts.push([r * Math.cos(a), r * Math.sin(a)])
  }
  return 'M' + pts.map(p => p.join(',')).join('L') + 'Z'
}

function _mapaBuildGraph(facts, episodes, concepts, connections) {
  const nodes = []
  const links = []

  const categories = Object.keys(facts)
  categories.forEach(cat => {
    (facts[cat] || []).forEach((f, i) => {
      nodes.push({ id: `fact-${cat}-${i}`, type: 'fact', category: cat, label: f.fact, data: f })
    })
  })
  episodes.forEach((e, i) => {
    nodes.push({ id: `ep-${i}`, type: 'episode', label: e.topic || e.summary || 'Episodio', data: e })
  })
  concepts.forEach((c, i) => {
    nodes.push({ id: `concept-${i}`, type: 'concept', label: c.name || 'Concepto', data: c })
  })
  const armorModels = [...(ARMOR_DATA.primarios || []), ...(ARMOR_DATA.paralelos || [])]
  armorModels.forEach(m => {
    nodes.push({ id: `armor-${m.id}`, type: 'armor', label: m.name, data: m })
  })

  const factNodes    = nodes.filter(n => n.type === 'fact')
  const episodeNodes = nodes.filter(n => n.type === 'episode')
  const conceptNodes = nodes.filter(n => n.type === 'concept')
  const armorNodes   = nodes.filter(n => n.type === 'armor')

  // Facts connected to the episodes they came from — matched against each
  // episode's key_facts (short phrases summarizing what was learned) and
  // its topic.
  episodeNodes.forEach(epNode => {
    const keyFactKw = (epNode.data.key_facts || []).map(_mapaKeywords)
    const topicKw   = _mapaKeywords(epNode.data.topic || '')
    factNodes.forEach(factNode => {
      const factKw = _mapaKeywords(factNode.label)
      const related = keyFactKw.some(kw => _mapaShareKeyword(factKw, kw)) || _mapaShareKeyword(factKw, topicKw)
      if (related) links.push({ source: factNode.id, target: epNode.id, kind: 'fact-episode' })
    })
  })

  // Concepts connected to the armor models they relate to.
  conceptNodes.forEach(cNode => {
    const cKw = _mapaKeywords(`${cNode.data.name || ''} ${cNode.data.desc || ''}`)
    armorNodes.forEach(aNode => {
      const aKw = _mapaKeywords(`${aNode.data.name || ''} ${aNode.data.nickname || ''} ${aNode.data.descripcion || ''}`)
      if (_mapaShareKeyword(cKw, aKw)) links.push({ source: cNode.id, target: aNode.id, kind: 'concept-armor' })
    })
  })

  // Episodes connected to concepts they touched on.
  episodeNodes.forEach(epNode => {
    const epKw = _mapaKeywords(`${epNode.data.topic || ''} ${(epNode.data.key_facts || []).join(' ')}`)
    conceptNodes.forEach(cNode => {
      const cKw = _mapaKeywords(`${cNode.data.name || ''} ${cNode.data.desc || ''}`)
      if (_mapaShareKeyword(epKw, cKw)) links.push({ source: epNode.id, target: cNode.id, kind: 'episode-concept' })
    })
  })

  // Armor evolution chain — each model links to the next within its own
  // category list, grounded in the data's own ordering (and its
  // "evolucion" narrative field), not a guessed relation.
  ;['primarios', 'paralelos'].forEach(cat => {
    const arr = ARMOR_DATA[cat] || []
    for (let i = 0; i < arr.length - 1; i++) {
      links.push({ source: `armor-${arr[i].id}`, target: `armor-${arr[i + 1].id}`, kind: 'armor-chain' })
    }
  })

  // Reflective-mode connections (data/mind_map_connections.json, see
  // core.reflective) — identified by fact TEXT rather than the fact-${cat}-${i}
  // ids above, since those ids are just this function's own array-index
  // scheme and shift whenever a fact file reorders (dedup sorts by 'added').
  // Text is the only identifier both sides of the backend/frontend boundary
  // can agree on without coupling core.reflective to this function's
  // internals. Silently skipped if either endpoint's fact no longer exists
  // (e.g. it went outdated and dropped off) — a stale connection just never
  // renders, same "let it fade out" spirit as everything else in Mapa.
  ;(connections || []).forEach((c, i) => {
    const fromNode = factNodes.find(n => n.label === c.from)
    const toNode   = factNodes.find(n => n.label === c.to)
    if (!fromNode || !toNode) return
    links.push({
      source: fromNode.id, target: toNode.id, kind: 'reflective',
      strength: typeof c.strength === 'number' ? c.strength : 0.5,
      relationship: c.relationship || '', id: `reflective-${i}`,
    })
  })

  return { nodes, links }
}

let _coreMapaSimulation = null

function _mapaNodeDetailHTML(node) {
  const d = node.data
  if (node.type === 'fact') {
    return `
      <div class="core-map-detail-kind">Hecho de memoria — ${esc(CORE_CATEGORY_LABELS[node.category] || node.category)}</div>
      <div class="core-map-detail-title">${esc(d.fact)}</div>`
  }
  if (node.type === 'episode') {
    const facts = (d.key_facts || []).map(k => `<div class="core-map-detail-row">• ${esc(k)}</div>`).join('')
    return `
      <div class="core-map-detail-kind">Episodio — ${esc(d.date || '')}</div>
      <div class="core-map-detail-title">${esc(d.topic || d.summary || '')}</div>
      <div class="core-map-detail-row">${esc(d.summary || '')}</div>
      ${facts}
      <div class="core-map-detail-row"><strong>Importancia</strong>${esc(String(d.importance ?? '—'))}</div>`
  }
  if (node.type === 'concept') {
    return `
      <div class="core-map-detail-kind">Concepto — ${esc(d.type === 'general' ? 'General' : 'Armadura')}</div>
      <div class="core-map-detail-title">${esc(d.name || '')}</div>
      <div class="core-map-detail-row">${esc(d.desc || '')}</div>
      <div class="core-map-detail-row"><strong>Estado</strong>${esc(d.status || '—')}</div>`
  }
  // armor
  return `
    <div class="core-map-detail-kind">Armadura — ${esc(d.status || '')}</div>
    <div class="core-map-detail-title">${esc(d.name || '')}${d.nickname ? ` "${esc(d.nickname)}"` : ''}</div>
    <div class="core-map-detail-row">${esc(d.descripcion || '')}</div>
    <div class="core-map-detail-row"><strong>Innovaciones</strong>${esc(d.innovaciones || '—')}</div>
    <div class="core-map-detail-row"><strong>Limitaciones</strong>${esc(d.limitaciones || '—')}</div>
    <div class="core-map-detail-row"><strong>Horas</strong>${esc(d.hours || '—')}</div>`
}

function _closeMapaDetail() {
  const detail = document.getElementById('coreMapaDetail')
  detail.classList.remove('open')
  document.querySelectorAll('.core-map-node-group.core-map-node-selected')
    .forEach(g => g.classList.remove('core-map-node-selected'))
}

async function _renderCoreMapa() {
  const emptyEl = document.getElementById('coreMapaEmpty')
  const graphEl = document.getElementById('coreMapaGraph')
  const svgEl   = document.getElementById('coreMapaSvg')

  if (_coreMapaSimulation) { _coreMapaSimulation.stop(); _coreMapaSimulation = null }
  _closeMapaDetail()

  let data = {}
  let connections = []
  try {
    const res = await fetch(`${JARVIS_API}/api/memory_active`)
    data = await res.json()
  } catch { data = {} }
  try {
    const res = await fetch(`${JARVIS_API}/api/mind_map_connections`)
    connections = await res.json()
  } catch { connections = [] }

  const facts    = data.facts    || {}
  const episodes = data.episodes || []
  const concepts = data.concepts || []

  // "Sin datos suficientes aún" specifically means LIRA hasn't had enough
  // conversations yet — facts and episodes are conversation-derived, while
  // armor/concepts are authored data that exists regardless, so only the
  // former two decide the empty state.
  const hasConversationData = Object.values(facts).some(arr => arr.length > 0) || episodes.length > 0
  if (!hasConversationData) {
    emptyEl.classList.remove('core-map-hidden')
    graphEl.classList.remove('active')
    return
  }
  emptyEl.classList.add('core-map-hidden')
  graphEl.classList.add('active')

  const { nodes, links } = _mapaBuildGraph(facts, episodes, concepts, connections)

  const width  = svgEl.clientWidth  || 320
  const height = svgEl.clientHeight || 400

  const svg = d3.select(svgEl)
  svg.selectAll('*').remove()
  svg.attr('viewBox', `0 0 ${width} ${height}`)

  const zoomLayer = svg.append('g')
  svg.call(d3.zoom().scaleExtent([0.4, 2.5]).on('zoom', ev => zoomLayer.attr('transform', ev.transform)))

  // Category clustering — facts of the same category are pulled toward
  // their own anchor point (spread evenly around the canvas), so "grouped
  // by category" is a spatial/physics property rather than an extra node
  // type or a fully-connected edge tangle.
  const categories = Object.keys(facts).filter(c => (facts[c] || []).length > 0)
  const categoryAnchor = {}
  categories.forEach((cat, i) => {
    const angle = (2 * Math.PI * i) / Math.max(1, categories.length)
    categoryAnchor[cat] = {
      x: width  / 2 + Math.cos(angle) * (Math.min(width, height) * 0.28),
      y: height / 2 + Math.sin(angle) * (Math.min(width, height) * 0.28),
    }
  })

  const linkSel = zoomLayer.append('g').selectAll('line')
    .data(links).join('line')
    .attr('class', d => d.kind === 'reflective' ? 'core-map-link core-map-link-reflective' : 'core-map-link')
    // Stronger connections = thicker/brighter edges (per spec) — only
    // reflective links currently carry a 'strength' value, so this is a
    // no-op (falls back to the CSS default) for every other link kind.
    .attr('stroke-width', d => typeof d.strength === 'number' ? 1 + d.strength * 3 : null)

  const nodeSel = zoomLayer.append('g').selectAll('g')
    .data(nodes).join('g')
    .attr('class', 'core-map-node-group')
    .attr('data-id', d => d.id)

  nodeSel.each(function (d) {
    const g = d3.select(this)
    const r = CORE_MAP_NODE_RADIUS[d.type]
    if (d.type === 'armor')        g.append('path').attr('d', _mapaDiamondPath(r)).attr('class', 'core-map-node-armor')
    else if (d.type === 'concept') g.append('path').attr('d', _mapaHexPath(r)).attr('class', 'core-map-node-concept')
    else                           g.append('circle').attr('r', r).attr('class', `core-map-node-${d.type}`)
  })

  // Labels only for the coarser, fewer node types — hundreds of tiny fact
  // circles with labels would just be unreadable clutter; clicking any
  // node still opens the full detail panel regardless.
  nodeSel.filter(d => d.type !== 'fact').append('text')
    .attr('class', 'core-map-label')
    .attr('x', d => CORE_MAP_NODE_RADIUS[d.type] + 4)
    .attr('y', 3)
    .text(d => (d.label || '').length > 22 ? d.label.slice(0, 21) + '…' : (d.label || ''))

  nodeSel.on('click', (ev, d) => {
    ev.stopPropagation()
    document.querySelectorAll('.core-map-node-group.core-map-node-selected')
      .forEach(g => g.classList.remove('core-map-node-selected'))
    d3.select(ev.currentTarget).classed('core-map-node-selected', true)
    document.getElementById('coreMapaDetailBody').innerHTML = _mapaNodeDetailHTML(d)
    document.getElementById('coreMapaDetail').classList.add('open')
  })

  const simulation = d3.forceSimulation(nodes)
    .force('link', d3.forceLink(links).id(d => d.id).distance(l => l.kind === 'armor-chain' ? 34 : 46).strength(0.35))
    .force('charge', d3.forceManyBody().strength(-70))
    .force('center', d3.forceCenter(width / 2, height / 2))
    .force('collide', d3.forceCollide().radius(d => CORE_MAP_NODE_RADIUS[d.type] + 6))
    .force('catX', d3.forceX(d => d.type === 'fact' && categoryAnchor[d.category] ? categoryAnchor[d.category].x : width  / 2).strength(d => d.type === 'fact' ? 0.12 : 0.02))
    .force('catY', d3.forceY(d => d.type === 'fact' && categoryAnchor[d.category] ? categoryAnchor[d.category].y : height / 2).strength(d => d.type === 'fact' ? 0.12 : 0.02))
    .on('tick', () => {
      linkSel
        .attr('x1', d => d.source.x).attr('y1', d => d.source.y)
        .attr('x2', d => d.target.x).attr('y2', d => d.target.y)
      nodeSel.attr('transform', d => `translate(${d.x},${d.y})`)
    })

  nodeSel.call(d3.drag()
    .on('start', (ev, d) => {
      if (!ev.active) simulation.alphaTarget(0.25).restart()
      d.fx = d.x; d.fy = d.y
    })
    .on('drag', (ev, d) => { d.fx = ev.x; d.fy = ev.y })
    .on('end', (ev, d) => {
      if (!ev.active) simulation.alphaTarget(0)
      // Left pinned at the dropped position — the whole point of dragging
      // is to let the user arrange the layout, not spring back from it.
    }))

  _coreMapaSimulation = simulation
}

document.getElementById('coreMapaDetailClose').addEventListener('click', _closeMapaDetail)
document.getElementById('coreMapaSvg').addEventListener('click', _closeMapaDetail)

// ════════════════════════════════════════════════════════════════════════════
// PERSONALITY — nuclear direct DOM theming (no CSS classes)
// ════════════════════════════════════════════════════════════════════════════

// [CHANGE 15] _PERSONALITY_QUOTES moved here (before applyPersonality) to fix a
// Temporal Dead Zone bug: applyPersonality() references this const, but it was
// previously defined ~400 lines later.  The TDZ caused a ReferenceError on the
// first applyPersonality('lira') call, which silently prevented the clock
// setInterval from ever being registered — causing the "—:—:—" frozen display.
// Guards the quote-on-personality-switch logic in applyPersonality() below
// (only re-pick on a genuine switch) and tracks LIRA's own sequential
// position for both that switch-in pick and _rotateMMQuote()'s 45s cycle —
// declared before applyPersonality() for the same TDZ reason as
// _PERSONALITY_QUOTES itself (see [CHANGE 15] above).
let _mmLastQuotedPersonality = null
let _mmLiraQuoteIdx = 0

const _PERSONALITY_QUOTES = {
  jarvis: [
    'Siempre a su servicio, señor.',
    'Todos los sistemas operativos y listos.',
    'He ejecutado el análisis. Puede continuar.',
    'Iniciando protocolos de seguridad.',
    'Su café está listo, señor. Metafóricamente.',
  ],
  friday: [
    'Lista para la acción, jefe.',
    'Radar despejado. Podemos proceder.',
    'Sistemas en línea. Di la palabra.',
    'Siempre un paso adelante.',
    'Sin amenazas detectadas en el perímetro.',
  ],
  // LIRA's own pool — direct, slightly sardonic, warm underneath. Also the
  // sequential rotation pool for _rotateMMQuote() (45s cycle) below; kept in
  // this exact spec order rather than randomized so "cycling" reads as
  // intentional, not repetitive-random.
  lira: [
    'Sin novedades. Por ahora.',
    'Sistemas en orden. Tú decides qué hacemos.',
    'Aquí. Como siempre.',
    'Todo bajo control. Más o menos.',
    'Escuchando. No siempre es fácil.',
    'Lista cuando quieras.',
    'Nada que reportar. Aún.',
    'Operative. Aunque nadie lo pidió.',
  ],
}

const themes = {
  jarvis: { accent: '#00d4ff', glow: 'rgba(0,212,255,0.3)',  shape: 'circle'  },
  friday: { accent: '#b44fff', glow: 'rgba(180,79,255,0.3)', shape: 'circle'  },
  lira:   { accent: '#f0c040', glow: 'rgba(240,192,64,0.3)',  shape: 'diamond' },
}

function _hexToRgba(hex, alpha) {
  const r = parseInt(hex.slice(1, 3), 16)
  const g = parseInt(hex.slice(3, 5), 16)
  const b = parseInt(hex.slice(5, 7), 16)
  return `rgba(${r},${g},${b},${alpha})`
}

// Personality transition — see .orb-wrap.personality-shift's own CSS
// comment for the full liquify/ink-bleed rationale. applyPersonality()
// itself is called on EVERY status change (setStatus() calls it with the
// UNCHANGED currentPersonality on every turn, not just real switches — see
// its own call site), so this must only ever play on a GENUINE change,
// never every time applyPersonality() happens to run.
let _personalityLiquifyTimer = null
function _playPersonalityLiquify() {
  const orbWrap   = document.querySelector('.orb-wrap')
  const mmOrbWrap = document.getElementById('mmOrbWrap')
  ;[orbWrap, mmOrbWrap, liraDiamond].forEach(el => { if (el) el.classList.add('personality-shift') })
  clearTimeout(_personalityLiquifyTimer)
  _personalityLiquifyTimer = setTimeout(() => {
    ;[orbWrap, mmOrbWrap, liraDiamond].forEach(el => { if (el) el.classList.remove('personality-shift') })
  }, 800)
}

function applyPersonality(name, displayName) {
  console.log('[Jarvis] applyPersonality:', name, displayName)
  // LIRA is the default personality — an invalid/missing name must never
  // fall back to JARVIS. Correct `name` itself (not just the theme lookup)
  // so currentPersonality and every downstream label/lookup stay consistent.
  if (!themes[name]) name = 'lira'
  const theme = themes[name]
  const _personalityChanged = currentPersonality !== name
  currentPersonality = name
  if (_personalityChanged) _playPersonalityLiquify()

  // 1. Update only PERSONALITY CSS vars — shell vars (--accent and family) stay fixed.
  const a = theme.accent
  // personality-owned: orb, name label, messages use --p-color and derivatives
  document.documentElement.style.setProperty('--p-color',  a)
  document.documentElement.style.setProperty('--p-mid',    _hexToRgba(a, 0.35))
  document.documentElement.style.setProperty('--p-dim',    _hexToRgba(a, 0.12))
  document.documentElement.style.setProperty('--p-glow',   _hexToRgba(a, 0.55))
  document.documentElement.style.setProperty('--p-a02',    _hexToRgba(a, 0.20))
  document.documentElement.style.setProperty('--p-a04',    _hexToRgba(a, 0.40))
  document.documentElement.style.setProperty('--p-a015',   _hexToRgba(a, 0.15))
  document.documentElement.style.setProperty('--p-a035',   _hexToRgba(a, 0.35))
  document.documentElement.style.setProperty('--p-a018',   _hexToRgba(a, 0.18))
  // --accent (shell) and --scanline are intentionally NOT updated here
  document.documentElement.getBoundingClientRect() // force repaint

  // 2. Apply color to EVERY individual colored element
  const orbRing          = document.querySelector('.orb-ring')
  const orbGlow          = document.querySelector('.orb-glow')
  const orbCore          = document.querySelector('.orb-core')
  const orbShapeEl       = document.querySelector('.orb-shape')
  const orbSpinnerCircle = document.querySelector('.orb-spinner circle')
  const orbBrackets      = document.querySelectorAll('.orb-bracket')
  const orbGems          = document.querySelectorAll('.orb-gem')
  const bars             = document.querySelectorAll('.bar')
  const micDotEl         = document.getElementById('micDot')
  const partialEl        = document.getElementById('partialTranscript')

  if (orbRing) {
    orbRing.style.borderColor = a
    orbRing.style.boxShadow = `0 0 10px ${a}, 0 0 24px ${_hexToRgba(a,0.20)}, inset 0 0 18px ${_hexToRgba(a,0.12)}`
    orbRing.getBoundingClientRect()
  }
  if (orbGlow) {
    orbGlow.style.background = `radial-gradient(ellipse at center, ${_hexToRgba(a,0.55)} 0%, transparent 68%)`
    orbGlow.getBoundingClientRect()
  }
  if (orbCore) {
    orbCore.style.background = `radial-gradient(circle, ${_hexToRgba(a,0.12)} 0%, transparent 75%)`
    orbCore.getBoundingClientRect()
  }
  if (orbSpinnerCircle) {
    orbSpinnerCircle.style.stroke = a
    orbSpinnerCircle.getBoundingClientRect()
  }
  orbBrackets.forEach(el => { el.style.borderColor = _hexToRgba(a, 0.35); el.getBoundingClientRect() })
  orbGems.forEach(el => {
    el.style.background   = a
    el.style.borderColor  = a
    el.style.boxShadow    = `0 0 4px ${a}, 0 0 8px ${_hexToRgba(a, 0.55)}`
    el.getBoundingClientRect()
  })
  bars.forEach(bar => {
    bar.style.background = a
    bar.style.boxShadow = `0 0 6px ${a}`
    bar.getBoundingClientRect()
  })
  if (titleEl) {
    titleEl.style.color = a
    titleEl.style.textShadow = `0 0 16px ${_hexToRgba(a, 0.55)}`
    titleEl.getBoundingClientRect()
  }
  if (micDotEl) {
    // shell-owned: clear any previous inline override so CSS var(--accent) applies
    micDotEl.style.background = ''
    micDotEl.style.boxShadow  = ''
  }
  if (partialEl) {
    // shell-owned: partial transcript uses shell color; clear inline override
    partialEl.style.color = ''
  }

  // 3. Handle orb shape — direct clipPath + borderRadius, applied to .orb-shape
  //    (NOT .orb-wrap) so the decorations below it are never clipped away —
  //    .orb-bracket/.orb-gem are siblings of .orb-shape, not descendants.
  if (orbShapeEl) {
    if (theme.shape === 'diamond') {
      orbShapeEl.style.clipPath = 'polygon(50% 5%, 95% 50%, 50% 95%, 5% 50%)'
      if (orbRing) orbRing.style.borderRadius = '0'
      if (orbCore) orbCore.style.borderRadius = '0'
      // Swap decoration set: circle brackets out, diamond gems in.
      orbBrackets.forEach(el => { el.style.opacity = '0' })
      orbGems.forEach(el => { el.style.opacity = '1' })
    } else {
      orbShapeEl.style.clipPath = 'none'
      if (orbRing) orbRing.style.borderRadius = '50%'
      if (orbCore) orbCore.style.borderRadius = '50%'
      // Explicit 0.7, not '' — the CSS default is now 0 (LIRA is the default
      // personality), so clearing the inline override would hide these
      // instead of restoring the circle-mode look.
      orbBrackets.forEach(el => { el.style.opacity = '0.7' })
      orbGems.forEach(el => { el.style.opacity = '0' })
    }
    orbShapeEl.getBoundingClientRect()
  }

  // 4. Update title text and show flash (only when displayName provided)
  if (displayName) {
    titleEl.textContent = displayName
    showPersonalityFlash(displayName)
  }

  // 5. Sync personality switcher buttons — active button glows, others dim.
  //    This runs on every personality change (voice, button click, or socket event).
  personalityBtns.forEach(btn => {
    btn.classList.toggle('active', btn.dataset.p === name)
  })

  // 6. Update persistent bar personality indicator (dot color + name text).
  const _pIndDot  = document.querySelector('#personalityIndicator .p-indicator-dot')
  const _pIndName = document.querySelector('#personalityIndicator .p-indicator-name')
  const _nameLabels = { jarvis: 'J A R V I S', friday: 'F R I D A Y', lira: 'L I R A' }
  if (_pIndDot) {
    _pIndDot.style.background = a
    _pIndDot.style.boxShadow  = `0 0 6px ${_hexToRgba(a, 0.55)}`
  }
  if (_pIndName) _pIndName.textContent = _nameLabels[name] ?? name.toUpperCase()

  // 7. Close personality modal after a selection (if it was open).
  if (typeof _closePersonalityModal === 'function') _closePersonalityModal()

  // 8. Update Main Menu large orb — same color/shape logic as the chat orb.
  const mmRing  = document.getElementById('mmOrbRing')
  const mmGlow  = document.getElementById('mmOrbGlow')
  const mmCore  = document.getElementById('mmOrbCore')
  const mmShape = document.querySelector('.mm-orb-shape')
  const mmSpin  = document.getElementById('mmOrbSpinnerCircle')
  const mmName  = document.getElementById('mmName')
  const mmBars  = document.querySelectorAll('.mm-bar')
  const mmGems  = document.querySelectorAll('.mm-orb-gem')

  if (mmRing) {
    mmRing.style.borderColor = a
    mmRing.style.boxShadow   = `0 0 12px ${a}, 0 0 30px ${_hexToRgba(a,0.20)}, inset 0 0 22px ${_hexToRgba(a,0.12)}`
  }
  if (mmGlow) {
    mmGlow.style.background = `radial-gradient(ellipse at center, ${_hexToRgba(a,0.55)} 0%, transparent 68%)`
  }
  if (mmCore) {
    mmCore.style.background = `radial-gradient(circle, ${_hexToRgba(a,0.12)} 0%, transparent 75%)`
  }
  if (mmSpin) mmSpin.style.stroke = a
  mmBars.forEach(bar => {
    bar.style.background = a
    bar.style.boxShadow  = `0 0 8px ${a}`
  })
  if (mmName) {
    mmName.textContent   = _nameLabels[name] ?? name.toUpperCase()
    mmName.style.color       = a
    mmName.style.textShadow  = `0 0 22px ${_hexToRgba(a, 0.55)}`
  }
  // Main menu orb shape — mirrors chat orb (diamond for lira, circle for others)
  if (mmShape) {
    if (theme.shape === 'diamond') {
      mmShape.style.clipPath = 'polygon(50% 5%, 95% 50%, 50% 95%, 5% 50%)'
      if (mmRing) mmRing.style.borderRadius = '0'
      if (mmCore) mmCore.style.borderRadius = '0'
      mmGems.forEach(el => { el.style.opacity = '1' })
    } else {
      mmShape.style.clipPath = 'none'
      if (mmRing) mmRing.style.borderRadius = '50%'
      if (mmCore) mmCore.style.borderRadius = '50%'
      mmGems.forEach(el => { el.style.opacity = '0' })
    }
  }

  // 9. Update personality quote with fade animation — only on a genuine
  // personality SWITCH (_mmLastQuotedPersonality guard), not on every call.
  // applyPersonality() itself runs on every setStatus() (see 6584), which
  // used to re-roll a random quote on every listening/processing/speaking
  // flip — far more often than the spec's dedicated 45s LIRA rotation
  // (_rotateMMQuote(), near _wireMMActions) wants. JARVIS/FRIDAY still get
  // a random pick, same as before, just now gated to real switches only.
  const quoteEl = document.getElementById('mmQuote')
  if (quoteEl && _mmLastQuotedPersonality !== name) {
    _mmLastQuotedPersonality = name
    const quotes = _PERSONALITY_QUOTES[name] ?? _PERSONALITY_QUOTES.lira
    const nextQuote = name === 'lira' ? quotes[(_mmLiraQuoteIdx = 0)] : quotes[Math.floor(Math.random() * quotes.length)]
    if (quoteEl.textContent !== nextQuote) {
      quoteEl.classList.add('fading')
      setTimeout(() => {
        quoteEl.textContent = nextQuote
        quoteEl.classList.remove('fading')
      }, 800) // spec: 800ms fade out / 800ms fade in, see .mm-quote's transition
    }
  }

  // 10. Update quick stats personality name
  const mmStatPersonality = document.getElementById('mmStatPersonality')
  if (mmStatPersonality) mmStatPersonality.textContent = (_nameLabels[name] ?? name.toUpperCase())

  // 11. Refresh system status strip
  _updateMMSysStrip()
}

function showPersonalityFlash(name) {
  personalityFlash.textContent = name
  personalityFlash.classList.add('visible')
  clearTimeout(_flashTimer)
  _flashTimer = setTimeout(() => personalityFlash.classList.remove('visible'), 2400)
}

// Double-click title to manually test personality cycle (dev helper)
titleEl.addEventListener('dblclick', () => {
  const cycle   = { jarvis: 'friday', friday: 'lira', lira: 'jarvis' }
  const nameMap = { jarvis: 'J A R V I S', friday: 'F R I D A Y', lira: 'L I R A' }
  const next    = cycle[currentPersonality] ?? 'lira'
  console.log('[Dev] manual personality toggle →', next)
  applyPersonality(next, nameMap[next])
})

// ════════════════════════════════════════════════════════════════════════════
// PERSONALITY SWITCHER BUTTONS
// Clicking sends the same voice-equivalent command through /text_command so
// the backend executes the identical code path as a spoken "cambia a friday".
// The personality_change socket event comes back and applyPersonality() syncs
// the active button state — no optimistic update needed.
// ════════════════════════════════════════════════════════════════════════════
const _PERSONALITY_CMDS = {
  jarvis: 'cambia a jarvis',
  friday: 'cambia a friday',
  lira:   'cambia a lira',
}

personalityBtns.forEach(btn => {
  btn.addEventListener('click', async () => {
    const p = btn.dataset.p
    if (p === currentPersonality) return   // already active, no-op
    const cmd = _PERSONALITY_CMDS[p]
    if (!cmd) return
    try {
      await fetch(`${JARVIS_API}/text_command`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ text: cmd }),
      })
      // Backend will emit personality_change → applyPersonality() picks it up
    } catch {
      // Connectivity error from UI control — goes to system panel, not chat
      addMaintMessage('Error: cambio de personalidad — Jarvis no responde')
    }
  })
})

// ════════════════════════════════════════════════════════════════════════════
// LOG
// ════════════════════════════════════════════════════════════════════════════
function ts() {
  return new Date().toLocaleTimeString('es-ES', {
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  })
}

function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

// ════════════════════════════════════════════════════════════════════════════
// LOG — addMessage routes messages to the right destination:
//   user/jarvis → main chat log (clean conversation view)
//   system      → maintenance panel only (never in chat)
//   error       → main chat log as a single red line 'Error: [brief]'
//                 ONLY for critical errors that affect the conversation
//                 (mic denied, command failed). All other operational/
//                 connectivity errors go directly to addMaintMessage().
// ════════════════════════════════════════════════════════════════════════════
function addMessage(type, message) {
  // ── System → maintenance panel only ─────────────────────────────────────
  if (type === 'system') {
    addMaintMessage(message)
    return
  }

  // ── Error → single red line 'Error: [brief description]' ────────────────
  if (type === 'error') {
    // Strip any existing 'Error: ' prefix to avoid doubling, then cap at 60 chars
    const stripped = message.replace(/^error:\s*/i, '').trim()
    const brief    = stripped.length > 60 ? stripped.slice(0, 57) + '…' : stripped
    message        = 'Error: ' + brief
  }

  // Bug fix: this used to be measured AFTER the new row (+ divider) was
  // already appended below, which conflates "was the user already at the
  // bottom" with "is this new message's own height still under the
  // threshold" — any reasonably long reply pushed scrollHeight past the
  // check on its own, so auto-scroll silently stopped firing for exactly
  // the messages most likely to scroll out of view. Measuring here, before
  // any DOM mutation, captures the PRE-append scroll state instead.
  const wasAtBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 100

  // ── Unified floating diamond — track every LIRA reply unconditionally
  // (the bubble's "last response" line needs this even when the diamond
  // isn't currently eligible to show, e.g. the reply arrived while still
  // on Main/Chat), but only fade the floating text in when it actually is
  // eligible. Also proactively shows the diamond's idle state here, not
  // just from setStatus's processing/speaking transition — a defensive
  // belt-and-suspenders in case this 'log' event ever beats the
  // corresponding 'status' event across the wire.
  if (type === 'jarvis') {
    _lastJarvisReply = message
    if (_diamondEligible()) {
      liraDiamond.classList.add('visible')
      _showDiamondText(message)
      if (currentStatus !== 'speaking') _scheduleDiamondTextHide()
    }
  }

  // ── Main menu floating text — both the user's own brief echo and LIRA's
  // reply show here while on Main. The user's echo always holds briefly;
  // LIRA's reply's hold timer is armed by setStatus once she actually
  // stops speaking (see there) — but if she isn't speaking right now
  // (TTS muted, or this arrived after status already moved on), there's no
  // such transition to catch it, so arm the same fallback timer here too.
  if (_currentSection === 'home' && (type === 'user' || type === 'jarvis')) {
    _showMMFloatingText(message, type === 'user')
    if (type === 'user' || currentStatus !== 'speaking') _scheduleMMFloatingTextHide()
  }

  const TYPES = {
    user:   { cls: 'msg-user',   label: '🎤  you' },
    jarvis: { cls: `msg-${currentPersonality}`, label: PERSONALITY_LABEL[currentPersonality] ?? PERSONALITY_LABEL.lira },
    error:  { cls: 'msg-error',  label: '✕  err' },
  }
  const cfg = TYPES[type] ?? TYPES.error
  // Snapshot personality label at insertion time so it survives future switches
  const label = type === 'jarvis'
    ? (PERSONALITY_LABEL[currentPersonality] ?? PERSONALITY_LABEL.lira)
    : cfg.label

  // Insert a visual divider before every user message (new turn separator)
  if (type === 'user' && lastMsgType !== null) {
    const div = document.createElement('div')
    div.className = 'divider'
    logEl.appendChild(div)
  }
  lastMsgType = type

  const row = document.createElement('div')
  row.className = `msg ${cfg.cls}`
  row.innerHTML = `
    <div class="msg-row">
      <span class="msg-time">${ts()}</span>
      <span class="msg-label">${label}</span>
      <span class="msg-text">${esc(message)}</span>
    </div>`
  logEl.appendChild(row)

  // ── Response timer integration ───────────────────────────────────────────
  if (type === 'user') {
    _startResponseTimer()
  } else if (type === 'jarvis') {
    _stopResponseTimer()
  }

  // Auto-scroll to the new bottom. Your own outgoing message (typed or
  // voice-transcribed) always scrolls into view regardless of prior
  // position — you just sent it, standard chat UX never leaves that
  // hanging off-screen. LIRA's replies and error lines instead respect
  // wasAtBottom: if the user has manually scrolled up to read older
  // messages, an incoming reply doesn't yank the view down — scrolling
  // resumes on its own the moment they're back near the bottom (e.g. after
  // sending their own next message). `scroll-behavior: smooth` on
  // .log-section (see its CSS) makes this plain scrollTop assignment
  // animate — smooth but fast, not scrollTo()'s slower easing on a long
  // jump.
  if (type === 'user' || wasAtBottom) logEl.scrollTop = logEl.scrollHeight
}

// ── Maintenance panel message writer ────────────────────────────────────────
function addMaintMessage(message) {
  const div = document.createElement('div')
  // Sistema premium pass: classify by leading prefix so entries get the
  // spec's tiered color treatment — see .maint-type-* CSS for the exact
  // colors and the reasoning behind defaulting to "system" rather than
  // "normal". Purely additive/cosmetic: no call site changes, and an
  // unmatched message always falls through to maint-type-system safely.
  let typeClass = 'maint-type-system'
  if (/^error:/i.test(message)) typeClass = 'maint-type-error'
  else if (/^(warning|advertencia):/i.test(message)) typeClass = 'maint-type-warning'
  div.className = `maint-msg ${typeClass}`
  div.innerHTML = `<span class="maint-msg-time">${ts()}</span>${esc(message)}`
  maintLog.appendChild(div)

  // Update unread badge on the Sistema nav item only when the section is not active
  if (_currentSection !== 'maintenance') {
    _sysCount++
    maintCount.textContent = _sysCount
    if (navMaintBadge) navMaintBadge.textContent = _sysCount
  }

  // Auto-scroll maintenance log if near bottom
  const atBottom = maintLog.scrollHeight - maintLog.scrollTop - maintLog.clientHeight < 40
  if (atBottom) maintLog.scrollTo({ top: maintLog.scrollHeight })
}

// ════════════════════════════════════════════════════════════════════════════
// TEXT INPUT
// ════════════════════════════════════════════════════════════════════════════
function setInputEnabled(enabled) {
  textInput.disabled = !enabled
  sendBtn.disabled   = !enabled
  textInput.placeholder = enabled ? 'Type a command…' : 'Sin conexión…'
}

// sourceInput defaults to the Chat section's own #textInput, but accepts
// any other input element (the unified floating diamond's #liraDiamondInput,
// Main's #mmFloatingInput) so every "type a command" surface in the app
// shares this exact same send path — literally the same code, not a re-
// implementation, per the floating diamond / Main floating input requirements.
async function sendTextCommand(sourceInput) {
  const input = sourceInput || textInput
  const text  = input.value.trim()
  if (!text) return
  // [CHANGE 14] Guard: if socket is not connected, show a clear indicator instead
  // of silently failing.  Input stays enabled so the user can retry after reconnect.
  if (!jarvisSocket || !jarvisSocket.connected) {
    const original = input.placeholder
    input.placeholder = 'Sin conexión — reconectando…'
    setTimeout(() => { input.placeholder = original }, 2500)
    return
  }
  input.value = ''
  addMessage('user', text)

  try {
    const res = await fetch(`${JARVIS_API}/text_command`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ text }),
    })
    if (!res.ok) addMessage('error', 'Jarvis rechazó el comando')
  } catch {
    addMessage('error', 'Jarvis no responde')
  }
}

sendBtn.addEventListener('click', () => sendTextCommand())
textInput.addEventListener('keydown', e => { if (e.key === 'Enter') sendTextCommand() })

// ════════════════════════════════════════════════════════════════════════════
// AJUSTES (SETTINGS) — now a full nav section; updateSettingsInfo() is
// called from switchSection() below on navigating to it (same pattern as
// maintenance's unread-badge reset and armor's sub-tab render).
// ════════════════════════════════════════════════════════════════════════════
settingsClose.addEventListener('click', () => switchSection('home'))

async function updateSettingsInfo() {
  try {
    const res  = await fetch(`${JARVIS_API}/api/info`)
    const info = await res.json()
    settingsBody.innerHTML = `
      <div class="info-row"><span class="info-key">Personality</span><span class="info-val">${esc(info.display_name)}</span></div>
      <div class="info-row"><span class="info-key">TTS Engine</span><span class="info-val">${esc(info.tts)}</span></div>
      <div class="info-row"><span class="info-key">Kokoro Voice</span><span class="info-val">${esc(info.kokoro_voice)}</span></div>
      <div class="info-row"><span class="info-key">Fallback Voice</span><span class="info-val">${esc(info.fallback_voice)}</span></div>
      <div class="info-row"><span class="info-key">STT Model</span><span class="info-val">${esc(info.vosk_model)}</span></div>
      <div class="info-row"><span class="info-key">Jarvis Port</span><span class="info-val">8080</span></div>
      <div class="info-row"><span class="info-key">Launcher Port</span><span class="info-val">8079</span></div>
    `
  } catch {
    settingsBody.innerHTML = '<div class="info-row"><span class="info-key">Status</span><span class="info-val" style="color:var(--red)">Jarvis offline</span></div>'
  }
  // Build hash — from launcher.py's own GET /api/version (see that
  // endpoint's docstring), the SAME process that actually serves this
  // page, so this is always ground truth for "which commit's
  // ui/index.html is genuinely being displayed right now" — independent
  // of whether jarvis.py (a separate process) is up, so it's appended
  // regardless of the /api/info outcome above. '*' suffix means there are
  // uncommitted local changes on top of that commit (git-describe-style).
  try {
    const vres      = await fetch(`${LAUNCHER_API}/api/version`)
    const version    = await vres.json()
    const frontendHash = version.repo_commit ? version.repo_commit + (version.repo_dirty ? '*' : '') : '—'
    const shellHash     = version.installed_shell_commit || '—'
    const dateStr = version.repo_commit_date
      ? new Date(version.repo_commit_date).toLocaleString('es-ES', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
      : ''
    settingsBody.insertAdjacentHTML('beforeend', `
      <div class="info-row"><span class="info-key">Build (frontend)</span><span class="info-val" title="${esc(dateStr)}">${esc(frontendHash)}</span></div>
      <div class="info-row"><span class="info-key">Build (app instalada)</span><span class="info-val">${esc(shellHash)}</span></div>
    `)
  } catch { /* leave build rows out — non-critical diagnostic info */ }
  if (typeof _refreshSleepStatus === 'function') _refreshSleepStatus()
}

// Feature toggles — config-driven: each entry's `key` must match a key in
// core/commands.py's _DEFAULT_FEATURE_FLAGS / data/feature_flags.json.
// Loaded once at startup (see the jarvis_ready handler) and re-synced
// whenever a 'feature_flags_state' socket event arrives, so a toggle
// flipped from another connected HUD tab (or a future settings surface)
// reflects here immediately too.
const FEATURE_FLAG_LABELS = [
  { key: 'proactividad',      label: 'Proactividad' },
  { key: 'busqueda_web',      label: 'Búsqueda web' },
  { key: 'copiloto_hud',      label: 'Co-piloto HUD' },
  { key: 'paneles_dinamicos', label: 'Paneles dinámicos' },
  { key: 'deteccion_tono',    label: 'Detección de tono' },
  { key: 'memoria_episodica', label: 'Memoria episódica' },
]
let _featureFlags = {}

function _renderFeatureToggles() {
  const container = document.getElementById('settingsToggles')
  if (!container) return
  container.innerHTML = FEATURE_FLAG_LABELS.map(({ key, label }) => {
    const on = _featureFlags[key] !== false   // unknown/not-yet-loaded defaults ON
    return `
      <div class="toggle-row">
        <span class="toggle-label">${esc(label)}</span>
        <button class="toggle-switch${on ? ' on' : ''}" data-flag="${key}" role="switch" aria-checked="${on}" title="${esc(label)}"></button>
      </div>
    `
  }).join('')
  container.querySelectorAll('.toggle-switch').forEach(btn => {
    btn.addEventListener('click', () => _toggleFeatureFlag(btn.dataset.flag, btn))
  })
  _applyTestModeUI()
}

// MODO TEST — reflects _featureFlags.modo_test into every surface that
// cares: the Ajustes toggle itself (button + appearing hint), the
// always-visible persistent-bar dot (via body.test-mode-active, see its
// CSS), and "Iniciar Sueño"'s visually-blocked-but-still-clickable state
// (see _showSleepConfirm below for why it's not a real `disabled` — a
// disabled button never fires click, which would silently swallow the
// "Desactiva el modo test..." message instead of showing it). Called from
// _renderFeatureToggles() itself (the one place all three _featureFlags
// update paths already converge — load, manual toggle, socket sync), not
// wired separately at each call site.
function _applyTestModeUI() {
  const active = _featureFlags.modo_test === true
  document.body.classList.toggle('test-mode-active', active)

  const toggle = document.getElementById('testModeToggle')
  if (toggle) {
    toggle.classList.toggle('on', active)
    toggle.setAttribute('aria-checked', String(active))
  }
  const hint = document.getElementById('testModeHint')
  if (hint) hint.classList.toggle('visible', active)

  if (sleepStartBtn) sleepStartBtn.classList.toggle('test-blocked', active)
}

async function _toggleFeatureFlag(key, btn) {
  const newState = !(_featureFlags[key] !== false)
  btn.disabled = true
  try {
    const res  = await fetch(`${JARVIS_API}/api/feature_flags`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: key, enabled: newState }),
    })
    const data = await res.json()
    if (res.ok && data.ok) {
      _featureFlags = data.flags
    }
  } catch { /* leave previous state; next load/sync reconciles */ }
  _renderFeatureToggles()
}

async function _loadFeatureFlags() {
  try {
    const res = await fetch(`${JARVIS_API}/api/feature_flags`)
    _featureFlags = await res.json()
  } catch { _featureFlags = {} }
  _renderFeatureToggles()
}

// MODO TEST toggle — reuses _toggleFeatureFlag() (same POST endpoint, same
// flip-and-re-render flow every other flag uses), it's just not rendered
// as one of the generic .toggle-switch rows (see its own HTML comment).
const testModeToggle = document.getElementById('testModeToggle')
testModeToggle.addEventListener('click', () => _toggleFeatureFlag('modo_test', testModeToggle))

// "Actualizar Sistema" — triggers scripts/rebuild_app.sh via launcher.py
// (git pull -> rebuild -> reinstall /Applications/LIRA.app). A real update
// takes a while (npm install + electron-builder), so this fetch is simply
// awaited for as long as it takes; the button stays disabled the whole time.
// Gated behind a HUD-styled confirmation (#updateConfirmModal) — never a
// browser confirm() — since this restarts the whole app. During the
// rebuild, updateLiraStatus reflects REAL progress via 'update_progress'
// socket events from launcher.py's api_update() (see _applyUpdateProgress()
// below), not a fixed sequence of timed messages.
const updateLiraBtn            = document.getElementById('updateLiraBtn')
const updateLiraStatus         = document.getElementById('updateLiraStatus')
const updateConfirmModal       = document.getElementById('updateConfirmModal')
const updateConfirmBtn         = document.getElementById('updateConfirmBtn')
const updateCancelBtn          = document.getElementById('updateCancelBtn')
// Second-chance modal — only shown after a first attempt comes back skipped
// by rebuild_app.sh's Claude Code guard (see api_update()'s docstring). Lets
// the user explicitly force through it, e.g. right after a prompt when they
// want the new build visible immediately, instead of the button just
// refusing outright.
const updateForceClaudeModal      = document.getElementById('updateForceClaudeModal')
const updateForceClaudeConfirmBtn = document.getElementById('updateForceClaudeConfirmBtn')
const updateForceClaudeCancelBtn  = document.getElementById('updateForceClaudeCancelBtn')

function _showUpdateConfirm() { updateConfirmModal.classList.add('open') }
function _hideUpdateConfirm() { updateConfirmModal.classList.remove('open') }
function _showUpdateForceClaudeConfirm() { updateForceClaudeModal.classList.add('open') }
function _hideUpdateForceClaudeConfirm() { updateForceClaudeModal.classList.remove('open') }

const UPDATE_PROGRESS_LABELS = {
  downloading: 'Descargando cambios...',
  compiling:   'Compilando...',
  installing:  'Instalando...',
  restarting:  'Reiniciando...',
}
function _applyUpdateProgress(data) {
  if (!data || !data.stage) return
  updateLiraStatus.style.color = 'var(--accent)'
  updateLiraStatus.textContent = data.label || UPDATE_PROGRESS_LABELS[data.stage] || data.stage
}

updateLiraBtn.addEventListener('click', _showUpdateConfirm)
updateCancelBtn.addEventListener('click', _hideUpdateConfirm)
updateConfirmModal.addEventListener('click', e => { if (e.target === updateConfirmModal) _hideUpdateConfirm() })
updateForceClaudeCancelBtn.addEventListener('click', () => {
  _hideUpdateForceClaudeConfirm()
  updateLiraBtn.disabled = false   // allow retry
})
updateForceClaudeModal.addEventListener('click', e => {
  if (e.target === updateForceClaudeModal) updateForceClaudeCancelBtn.click()
})

// skipClaudeGuard=true only on the explicit re-confirmed retry below — see
// LIRA_SKIP_CLAUDE_GUARD in rebuild_app.sh for what this actually overrides.
async function _runUpdate(skipClaudeGuard) {
  updateLiraBtn.disabled = true
  updateLiraStatus.style.color = 'var(--accent)'
  updateLiraStatus.textContent = UPDATE_PROGRESS_LABELS.downloading
  try {
    const res  = await fetch(`${LAUNCHER_API}/api/update`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ skip_claude_guard: skipClaudeGuard }),
    })
    const data = await res.json()
    if (res.ok && data.ok) {
      updateLiraStatus.style.color = 'var(--green)'
      updateLiraStatus.textContent = UPDATE_PROGRESS_LABELS.restarting
      // Left disabled: electron/main.js's health poll picks up pending_relaunch
      // within a few seconds and relaunches the app on its own — no manual
      // restart needed, so this reads as "in progress" rather than an
      // instruction for the user to act on.
    } else {
      throw new Error(data.error || `HTTP ${res.status}`)
    }
  } catch (e) {
    console.error('[Update] failed:', e)
    // rebuild_app.sh's Claude Code guard (see its "--- Claude Code guard ---"
    // comment) no-ops the rebuild while any Claude Code session is active,
    // to avoid stashing/losing live uncommitted edits. On the first attempt,
    // offer an explicit override instead of just refusing outright; on the
    // already-confirmed retry (skipClaudeGuard=true), the guard was bypassed
    // server-side, so a repeat of this same error means something else is
    // wrong — show it as a normal failure instead of looping the modal.
    if (!skipClaudeGuard && /Claude Code session active/.test(e.message)) {
      updateLiraStatus.style.color = 'var(--accent)'
      updateLiraStatus.textContent = 'Omitido: sesión de Claude Code activa'
      _showUpdateForceClaudeConfirm()
      return
    }
    updateLiraStatus.style.color = 'var(--red)'
    updateLiraStatus.textContent = 'Error en actualización'
    updateLiraBtn.disabled = false   // allow retry
  }
}

updateConfirmBtn.addEventListener('click', () => { _hideUpdateConfirm(); _runUpdate(false) })
updateForceClaudeConfirmBtn.addEventListener('click', () => { _hideUpdateForceClaudeConfirm(); _runUpdate(true) })

// "Compilar para iPhone" — triggers scripts/build_ios.sh via launcher.py
// (cap sync -> xcodebuild archive -> xcodebuild -exportArchive). Same
// fetch-and-await-however-long-it-takes shape as Actualizar Sistema above,
// but no confirm modal — this only produces a file, it doesn't touch the
// running app. buildIosStatus reflects REAL progress via
// 'build_ios_progress' socket events (see _applyBuildIosProgress() below),
// not a fixed sequence of timed messages.
const buildIosBtn    = document.getElementById('buildIosBtn')
const buildIosStatus = document.getElementById('buildIosStatus')

const BUILD_IOS_PROGRESS_LABELS = {
  syncing:   'Sincronizando...',
  compiling: 'Compilando...',
  done:      'IPA lista',
}
function _applyBuildIosProgress(data) {
  if (!data || !data.stage) return
  buildIosStatus.style.color = 'var(--accent)'
  buildIosStatus.textContent = data.label || BUILD_IOS_PROGRESS_LABELS[data.stage] || data.stage
}

buildIosBtn.addEventListener('click', async () => {
  buildIosBtn.disabled = true
  buildIosStatus.style.color = 'var(--accent)'
  buildIosStatus.textContent = BUILD_IOS_PROGRESS_LABELS.syncing
  try {
    const res  = await fetch(`${LAUNCHER_API}/api/build_ios`, { method: 'POST' })
    const data = await res.json()
    if (res.ok && data.ok) {
      buildIosStatus.style.color = 'var(--green)'
      // Path + install instructions, not just "IPA lista" — the button's
      // whole point is telling the user where the file is and what to do
      // with it next.
      buildIosStatus.textContent = `IPA lista: ${data.ipa_path} — instala con SideStore`
    } else {
      throw new Error(data.error || `HTTP ${res.status}`)
    }
  } catch (e) {
    console.error('[BuildIos] failed:', e)
    buildIosStatus.style.color = 'var(--red)'
    buildIosStatus.textContent = 'Error al compilar'
  } finally {
    buildIosBtn.disabled = false   // never left disabled — always safe to retry
  }
})

// ── Sleep System — 'Iniciar Sueño' manual trigger + live status ─────────────
// State machine: idle (shows "Presupuesto manual: X/5000...") → confirm dialog →
// POST /api/sleep/start → poll GET /api/sleep/status every 2s while
// continuous.running is true, showing "DURMIENDO — Ciclo X, Fase Y:
// [nombre]" → once continuous.running flips back to false, show a summary
// of what the run accomplished. Also picks up a run that was auto-triggered
// by core/commands.py's own 20-minute idle loop (not just ones started
// from this button) — _refreshSleepStatus() is called every time Ajustes
// is opened (see updateSettingsInfo()) and checks continuous.running
// regardless of who started it. Sleep is now continuous (cycles forever
// until interrupted, see core/sleep.py's run_continuous_sleep) rather than
// a bounded session — sleepStopBtn ("Detener Sueño") is the only thing
// that reliably ends a run started from here, alongside the auto-wake on
// the next voice/text interaction.
const sleepStartBtn     = document.getElementById('sleepStartBtn')
const sleepStopBtn      = document.getElementById('sleepStopBtn')
const sleepStartStatus  = document.getElementById('sleepStartStatus')
const sleepConfirmModal = document.getElementById('sleepConfirmModal')
const sleepConfirmBtn   = document.getElementById('sleepConfirmBtn')
const sleepCancelBtn    = document.getElementById('sleepCancelBtn')

let _sleepPollTimer = null

function _showSleepConfirm() {
  if (_featureFlags.modo_test === true) {
    sleepStartStatus.style.color = 'var(--red)'
    sleepStartStatus.textContent = 'Desactiva el modo test antes de iniciar el sueño.'
    return
  }
  sleepConfirmModal.classList.add('open')
}
function _hideSleepConfirm() { sleepConfirmModal.classList.remove('open') }

function _stopSleepPoll() {
  clearInterval(_sleepPollTimer)
  _sleepPollTimer = null
}

// Renders the idle-state budget line — shows the Groq-fallback pool
// (data/sleep_budget.json's continuous.groq_fallback_*), which only ever
// gets spent if Ollama is unreachable (see core/sleep.py's _groq_call) —
// Ollama itself has no token cost to show a budget for. Never called while
// a run is actively sleeping (see _applySleepStatus) so it can't clobber
// the live "DURMIENDO..." status or the just-finished summary.
function _renderSleepBudget(status) {
  const cont = status.continuous || {}
  if (cont.groq_fallback_limit == null) return
  const remaining = Math.max(0, cont.groq_fallback_limit - (cont.groq_fallback_used || 0))
  sleepStartStatus.style.color = ''
  sleepStartStatus.textContent =
    `Motor: Ollama (local, sin coste) — reserva Groq: ${remaining}/${cont.groq_fallback_limit} tokens`
}

// Tracks whether body.sleeping was set on the LAST poll (Ajustes or
// ambient), across both — so whichever one notices the true→false
// transition first triggers exactly one wake animation, not one per poller.
let _sleepBodyWasActive = false

// Toggles the always-visible bottom-bar indicator + main-orb "cooler,
// slower breathing" state (see .sleep-bar-indicator / body.sleeping in the
// stylesheet), and plays a brief wake transition the moment sleep flips
// from active to inactive. Called from BOTH the detailed Ajustes poll
// below and the ambient poll (_ambientSleepPoll further down) — idempotent
// either way.
function _applySleepBodyState(running) {
  if (running) {
    document.body.classList.add('sleeping')
    _sleepBodyWasActive = true
    return
  }
  if (_sleepBodyWasActive) {
    const orbWrap = document.getElementById('mmOrbWrap')
    if (orbWrap) {
      orbWrap.classList.add('waking')
      setTimeout(() => orbWrap.classList.remove('waking'), 1400)
    }
  }
  document.body.classList.remove('sleeping')
  _sleepBodyWasActive = false
}

// Applies one GET /api/sleep/status response to the button/status line,
// and starts/stops the poll timer to match reality.
function _applySleepStatus(status) {
  const cont = status.continuous || {}
  _applySleepBodyState(!!cont.running)

  if (cont.running) {
    sleepStartBtn.disabled = true
    sleepStartBtn.style.display = 'none'
    if (sleepStopBtn) sleepStopBtn.style.display = 'block'
    const cycle = cont.current_cycle || 0
    const phaseNum = cont.current_phase_num || 0
    const phaseName = cont.current_phase || '…'
    sleepStartStatus.style.color = 'var(--accent)'
    sleepStartStatus.textContent = `DURMIENDO — Ciclo ${cycle}, Fase ${phaseNum}: ${phaseName}`
    if (!_sleepPollTimer) _sleepPollTimer = setInterval(_pollSleepStatus, 2000)
    return
  }
  // Not running (any more) — was THIS poll cycle the one that caught it
  // finishing? Only show the completion summary once, right as it
  // transitions from running to done; a plain idle refresh (e.g. opening
  // Ajustes fresh, nothing ever ran) goes straight to the budget line.
  const wasPolling = !!_sleepPollTimer
  _stopSleepPoll()
  sleepStartBtn.disabled = false
  sleepStartBtn.style.display = ''
  sleepStartBtn.textContent = 'Iniciar Sueño'
  if (sleepStopBtn) sleepStopBtn.style.display = 'none'
  if (wasPolling && cont.total_cycles_completed != null) {
    const reasonLabel = { interaction: 'LIRA despertó', manual_stop: 'detenido manualmente', error: 'error' }[cont.stop_reason] || 'detenido'
    sleepStartStatus.style.color = 'var(--green)'
    sleepStartStatus.textContent =
      `Sueño finalizado (${reasonLabel}) — ${cont.total_cycles_completed} ciclo${cont.total_cycles_completed === 1 ? '' : 's'} completado${cont.total_cycles_completed === 1 ? '' : 's'}`
  } else {
    _renderSleepBudget(status)
  }
}

async function _pollSleepStatus() {
  try {
    const res    = await fetch(`${JARVIS_API}/api/sleep/status`)
    const status = await res.json()
    _applySleepStatus(status)
  } catch { /* transient — next 2s tick retries; button stays in its last known state */ }
}

// Same fetch as _pollSleepStatus, exposed as its own name for
// updateSettingsInfo()'s call site (semantically "check status now", not
// "this IS the poll loop" — even though it happens to share the same
// underlying request).
const _refreshSleepStatus = _pollSleepStatus

// Ambient poll — always running (not just while Ajustes is open), purely
// to keep the bottom-bar indicator + orb sleep state honest app-wide.
// Deliberately slower (8s) than the Ajustes-specific 2s poll above, since
// this one's only job is two boolean-ish UI states, not a live phase
// readout. Started once at page load, see PAGE LOAD section below.
async function _ambientSleepPoll() {
  try {
    const res    = await fetch(`${JARVIS_API}/api/sleep/status`)
    const status = await res.json()
    _applySleepBodyState(!!(status.continuous && status.continuous.running))
  } catch { /* transient — next 8s tick retries */ }
}
setInterval(_ambientSleepPoll, 8000)
_ambientSleepPoll()

sleepStartBtn.addEventListener('click', _showSleepConfirm)
sleepCancelBtn.addEventListener('click', _hideSleepConfirm)
sleepConfirmModal.addEventListener('click', e => { if (e.target === sleepConfirmModal) _hideSleepConfirm() })

sleepConfirmBtn.addEventListener('click', async () => {
  _hideSleepConfirm()
  try {
    const res  = await fetch(`${JARVIS_API}/api/sleep/start`, { method: 'POST' })
    const data = await res.json()
    if (res.ok && data.ok) {
      sleepStartBtn.disabled = true
      sleepStartBtn.style.display = 'none'
      if (sleepStopBtn) sleepStopBtn.style.display = 'block'
      sleepStartStatus.style.color = 'var(--accent)'
      sleepStartStatus.textContent = 'DURMIENDO — iniciando…'
      if (!_sleepPollTimer) _sleepPollTimer = setInterval(_pollSleepStatus, 2000)
    } else {
      sleepStartStatus.style.color = 'var(--red)'
      sleepStartStatus.textContent = data.error || 'No se pudo iniciar el modo sueño'
    }
  } catch (e) {
    console.error('[Sleep] failed to start:', e)
    sleepStartStatus.style.color = 'var(--red)'
    sleepStartStatus.textContent = 'Error al iniciar el modo sueño'
  }
})

if (sleepStopBtn) {
  sleepStopBtn.addEventListener('click', async () => {
    sleepStopBtn.disabled = true
    try {
      const res  = await fetch(`${JARVIS_API}/api/sleep/stop`, { method: 'POST' })
      const data = await res.json()
      if (!(res.ok && data.ok)) {
        sleepStartStatus.style.color = 'var(--red)'
        sleepStartStatus.textContent = data.error || 'No se pudo detener el sueño'
      }
      // Either way, the next 2s poll tick reflects reality — no need to
      // guess the new state here.
    } catch (e) {
      console.error('[Sleep] failed to stop:', e)
    } finally {
      sleepStopBtn.disabled = false
    }
  })
}

// ════════════════════════════════════════════════════════════════════════════
// PAGE LOAD — clock first, then personality init
// ════════════════════════════════════════════════════════════════════════════

// [CHANGE 16] Start the clock BEFORE any other init that could throw, so the
// time display is always live on page load regardless of backend connection state.
// The function declaration is hoisted, so calling it here is safe even though
// the body is defined later in the file.
_updateMMClock()
setInterval(_updateMMClock, 1000)

applyPersonality('lira')  // set initial state immediately (default personality)

// Boot splash is purely decorative and must never risk taking down anything
// after it — wrapped so a failure here can't halt the rest of this script's
// top-level execution (a real, previously-hit failure mode in this file).
try { _playBootSplash() } catch (e) { console.error('[BootSplash] failed:', e) }

// ════════════════════════════════════════════════════════════════════════════
// MAIN MENU CLOCK — updates #mmTime and #mmDate every second.
// Startup calls (_updateMMClock() + setInterval) moved to PAGE LOAD above
// so the clock is guaranteed to start before any other init. [CHANGE 16]
// ════════════════════════════════════════════════════════════════════════════
function _updateMMClock() {
  const now     = new Date()
  const timeEl  = document.getElementById('mmTime')
  const dateEl  = document.getElementById('mmDate')
  const timeStr = now.toLocaleTimeString('es-ES', {
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  })
  if (timeEl) timeEl.textContent = timeStr
  if (dateEl) {
    dateEl.textContent = now.toLocaleDateString('es-ES', {
      weekday: 'long', year: 'numeric', month: 'long', day: 'numeric',
    })
  }
  // Mirror the same time string into the corner HUD readouts (UPTIME/TIME —
  // both literally the same live clock value, just surfaced twice per the
  // redesign's corner layout, not two independent timers).
  const hudTimeEl  = document.getElementById('mmHudTime')
  const hudClockEl = document.getElementById('mmHudClock')
  if (hudTimeEl)  hudTimeEl.textContent  = timeStr
  if (hudClockEl) hudClockEl.textContent = timeStr
}

// ════════════════════════════════════════════════════════════════════════════
// BOOT SPLASH — fixed ~3.5s cinematic intro, plays on every page load.
// Purely decorative and self-timed; never reads or sets anything on
// #bootOverlay (setBootState/setBootMsg keep driving that independently,
// for exactly as long as the real backend takes). Always LIRA's gold
// diamond regardless of the active personality — see the HTML comment
// above #bootSplash for the full design rationale.
// ════════════════════════════════════════════════════════════════════════════
function _playBootSplash() {
  const splash   = document.getElementById('bootSplash')
  const grid     = document.getElementById('bootSplashGrid')
  const terminal = document.getElementById('bootTerminal')
  const track    = document.getElementById('bootProgressTrack')
  const fill     = document.getElementById('bootProgressFill')
  const orb      = document.getElementById('bootSplashOrb')
  const nameEl   = document.getElementById('bootSplashName')
  const statusEl = document.getElementById('bootSplashStatus')
  const retryBtn = document.getElementById('bootSplashRetry')
  if (!splash || !grid || !terminal || !track || !fill || !orb || !nameEl || !statusEl || !retryBtn) return

  const LINES = [
    'SISTEMA LIRA v2.0',
    'Inicializando núcleo de IA...',
    'Cargando modelos de voz...',
    'Estableciendo conexión...',
    'Sistemas en línea.',
  ]
  const LINE_START = 300   // ms before the first line appears
  const LINE_GAP   = 250   // ms between each subsequent line

  // Beat 1: dark screen, then the grid fades in
  requestAnimationFrame(() => grid.classList.add('visible'))

  // Beat 2 + 3: terminal lines typewriter in; progress bar fills alongside
  LINES.forEach((text, i) => {
    setTimeout(() => {
      const el = document.createElement('div')
      el.className = 'boot-terminal-line'
      el.textContent = text
      terminal.appendChild(el)
      requestAnimationFrame(() => el.classList.add('visible'))
    }, LINE_START + i * LINE_GAP)
  })
  setTimeout(() => { fill.style.width = '100%' }, LINE_START)

  // Beat 4: orb reveal (scale-up + glow burst) — terminal/progress fade out
  const linesDoneAt = LINE_START + LINES.length * LINE_GAP   // ~1550ms
  const revealAt     = linesDoneAt + 400                     // ~1950ms
  setTimeout(() => {
    terminal.classList.add('fading')
    track.classList.add('fading')
    orb.classList.add('reveal')
  }, revealAt)

  // Beat 5: "L I R A" fades in below the now-pulsing orb, then the splash
  // holds — it only releases once the real backend state resolves (see
  // _enterBootSplashWait below), not on a fixed timer.
  const nameAt = revealAt + 400
  setTimeout(() => nameEl.classList.add('visible'), nameAt)
  setTimeout(() => _enterBootSplashWait(splash, nameEl, orb, statusEl, retryBtn), nameAt + 300)
}

// Must match @keyframes boot-trace-loop's duration above — how long the
// border-trace dash takes to complete one full lap of the diamond.
const BOOT_TRACE_LOOP_MS = 1600

// Real, backend-driven boot progress (see .boot-real-progress above) — fed
// by 'boot_progress' events from BOTH launcher.py's socket (stages 1-3,
// before jarvis.py's own server exists) and jarvis.py's socket (stages
// 4-5-7, once it does — registered on `launcher` and inside
// _attemptConnect() respectively, see below). Monotonic on purpose:
// launcher.py and jarvis.py are independent processes/sockets, so a later,
// lower-percent event arriving after an earlier, higher one is possible —
// the bar must never visibly regress.
let _bootProgressPercent = 0
function _resetBootProgress() {
  _bootProgressPercent = 0
  const fill  = document.getElementById('bootRealProgressFill')
  const label = document.getElementById('bootRealProgressLabel')
  if (fill)  fill.style.width = '0%'
  if (label) label.textContent = ''
}
function _applyBootProgress(data) {
  if (!data || typeof data.percent !== 'number') return
  if (data.percent < _bootProgressPercent) return
  _bootProgressPercent = data.percent
  const fill  = document.getElementById('bootRealProgressFill')
  const label = document.getElementById('bootRealProgressLabel')
  if (fill)  fill.style.width = `${_bootProgressPercent}%`
  if (label && data.label) label.textContent = data.label
}

// Drives the boot splash's hold phase: diamond + "L I R A" are showing, and
// we're genuinely waiting on #bootOverlay's real state machine (setBootMsg/
// setBootState, health polling, retries — all untouched, still driving
// independently underneath). While waiting: a dash traces the diamond's
// border continuously and "Cargando..." pulses below the name. On a real
// timeout/error (setBootMsg sets 'TIMEOUT — RETRY' or 'ERROR — RETRY') the
// trace stops, the status turns into a red "Error de conexión", and a
// "Reintentar" button appears — clicking it just re-fires the real
// #powerBtn start flow and goes back to the loading look. Only a genuine
// 'running' state (jarvis_ready succeeded) ends this: the trace gets one
// more full loop to finish, then the whole splash fades to reveal the HUD.
function _enterBootSplashWait(splash, nameEl, orb, statusEl, retryBtn) {
  const overlay = document.getElementById('bootOverlay')

  const showLoading = () => {
    statusEl.textContent = 'Cargando...'
    statusEl.classList.remove('error')
    statusEl.classList.add('visible', 'pulsing')
    retryBtn.classList.remove('show')
    orb.classList.add('tracing')
    // Fresh boot sequence (or a retry) — start the real progress bar over.
    _resetBootProgress()
    const progEl = document.getElementById('bootRealProgress')
    if (progEl) progEl.classList.add('visible')
  }

  const showError = () => {
    orb.classList.remove('tracing')
    statusEl.classList.remove('pulsing')
    statusEl.textContent = 'Error de conexión'
    statusEl.classList.add('error')
    retryBtn.classList.add('show')
  }

  const finishAndFade = () => {
    if (observer) observer.disconnect()
    // Let the border trace complete its current loop before fading, instead
    // of cutting it off mid-lap.
    setTimeout(() => {
      orb.classList.remove('tracing')
      statusEl.classList.remove('pulsing')
      statusEl.classList.add('fading')
      nameEl.classList.add('fading')
      splash.classList.add('fading')
      setTimeout(() => splash.classList.add('gone'), 600)   // matches #bootSplash's own transition
    }, BOOT_TRACE_LOOP_MS)
  }

  showLoading()

  if (!overlay) { finishAndFade(); return }

  let observer = null
  const check = () => {
    const state = overlay.dataset.state
    const msg   = (bootMsg && bootMsg.textContent) || ''
    if (state === 'running') {
      finishAndFade()
    } else if (/TIMEOUT|ERROR/i.test(msg)) {
      showError()
    }
    // Otherwise still genuinely booting ('idle' pre-autostart or 'starting')
    // — keep showing the loading trace and wait for the next mutation.
  }

  observer = new MutationObserver(check)
  observer.observe(overlay, { attributes: true, attributeFilter: ['data-state'] })
  if (bootMsg) observer.observe(bootMsg, { childList: true, characterData: true, subtree: true })
  check()   // covers the case where state/msg already resolved before we got here

  retryBtn.addEventListener('click', () => {
    showLoading()
    // Inside Electron: the "nuclear" recovery — kills launcher.py +
    // jarvis.py, force-frees ports 8079/8080 in case something's wedged
    // outside normal process tracking, then reboots from scratch (see
    // electron/main.js's restart-backend handler / preload.js's
    // restartBackend()). This is what actually fixes the "stuck forever"
    // case per spec — the plain HTTP retry below can't recover a wedged
    // launcher.py at all (there's nothing to ask, if the thing you'd ask
    // is itself the thing that's stuck), only jarvis.py restarting
    // cleanly under an otherwise-healthy launcher.py.
    //
    // Outside Electron (a plain browser tab — this HUD is a self-contained
    // web app that works without any Electron APIs, see preload.js's own
    // comment): fall back to the original behavior, reusing the real,
    // already-tested start/retry flow via the power button.
    if (window.electronAPI && typeof window.electronAPI.restartBackend === 'function') {
      window.electronAPI.restartBackend()
    } else {
      powerBtn.click()
    }
  })
}

// ════════════════════════════════════════════════════════════════════════════
// MAIN MENU — system status strip, session timer, actions
// (_PERSONALITY_QUOTES moved to before applyPersonality — see [CHANGE 15])
// ════════════════════════════════════════════════════════════════════════════

function _updateMMSysStrip() {
  const micEl  = document.getElementById('mmSysMic')
  const modeEl = document.getElementById('mmSysMode')
  const connEl = document.getElementById('mmSysConn')
  const muteBtnEl = document.getElementById('mmToggleMute')

  if (micEl) {
    micEl.querySelector('.mm-sys-icon').textContent = _isMuted ? '🔇' : '🎤'
    micEl.querySelector('.mm-sys-lbl').textContent  = _isMuted ? 'Muted' : 'Mic'
    micEl.className = 'mm-sys-item ' + (_isMuted ? 'sys-muted' : 'sys-active')
  }
  if (modeEl) {
    const isConv = _listenMode === 'conversation'
    modeEl.querySelector('.mm-sys-icon').textContent = isConv ? '◉' : '◎'
    modeEl.querySelector('.mm-sys-lbl').textContent  = isConv ? 'Conv Mode' : 'Wake Word'
    modeEl.className = 'mm-sys-item ' + (isConv ? 'sys-active' : '')
  }
  if (connEl) {
    connEl.querySelector('.mm-sys-icon').textContent = _jarvisOnline ? '●' : '◌'
    connEl.querySelector('.mm-sys-lbl').textContent  = _jarvisOnline ? 'Online' : 'Offline'
    connEl.className = 'mm-sys-item ' + (_jarvisOnline ? 'sys-online' : 'sys-offline')
  }
  // Mirror mic/mode into the orb's floating HUD readouts (same source data
  // as the strip above, just a second display of it next to the orb).
  const hudMicEl  = document.getElementById('mmHudMic')
  const hudModeEl = document.getElementById('mmHudMode')
  if (hudMicEl)  hudMicEl.textContent  = _isMuted ? 'OFF' : 'ON'
  if (hudModeEl) hudModeEl.textContent = (_listenMode === 'conversation') ? 'CONV' : 'WAKE'
  if (muteBtnEl) {
    muteBtnEl.querySelector('.mm-action-icon').textContent = _isMuted ? '🔇' : '🎤'
    muteBtnEl.classList.toggle('active-muted', _isMuted)
  }
  const ttsBtnEl = document.getElementById('mmToggleTts')
  if (ttsBtnEl) {
    ttsBtnEl.querySelector('.mm-action-icon').textContent = _isTtsMuted ? '🔇' : '🔊'
    ttsBtnEl.classList.toggle('active-muted', _isTtsMuted)
  }
  const modeBtnEl = document.getElementById('mmToggleMode')
  if (modeBtnEl) {
    modeBtnEl.classList.toggle('active-conv', _listenMode === 'conversation')
  }
}

// Session timer — counts up from when the main UI was shown
let _sessionStart = 0
let _sessionTimerInterval = null

function _startSessionTimer() {
  _sessionStart = Date.now()
  if (_sessionTimerInterval) clearInterval(_sessionTimerInterval)
  _sessionTimerInterval = setInterval(() => {
    const el = document.getElementById('mmStatDuration')
    if (!el) return
    const secs  = Math.floor((Date.now() - _sessionStart) / 1000)
    const m     = Math.floor(secs / 60).toString().padStart(2, '0')
    const s     = (secs % 60).toString().padStart(2, '0')
    _flipDigits(el, `${m}:${s}`)
  }, 1000)
}

// LIRA's own quote pool cycles on a dedicated 45s timer, independent of the
// applyPersonality()-driven switch-in pick above — sequential (not random),
// so it reads as a deliberate rotation. Only advances while LIRA is the
// active personality; a no-op tick while JARVIS/FRIDAY are active is
// cheaper than tearing the interval down and recreating it on every switch.
setInterval(() => {
  if (currentPersonality !== 'lira') return
  const quoteEl = document.getElementById('mmQuote')
  if (!quoteEl) return
  const quotes = _PERSONALITY_QUOTES.lira
  _mmLiraQuoteIdx = (_mmLiraQuoteIdx + 1) % quotes.length
  const nextQuote = quotes[_mmLiraQuoteIdx]
  quoteEl.classList.add('fading')
  setTimeout(() => {
    quoteEl.textContent = nextQuote
    quoteEl.classList.remove('fading')
  }, 800) // matches .mm-quote's 0.8s opacity transition — 800ms out, 800ms in
}, 45000)

// Quick action buttons
;(function _wireMMActions() {
  const goChat = document.getElementById('mmGoChat')
  if (goChat) goChat.addEventListener('click', () => switchSection('chat'))

  const goArmor = document.getElementById('mmGoArmor')
  if (goArmor) goArmor.addEventListener('click', () => switchSection('armor'))

  const toggleMute = document.getElementById('mmToggleMute')
  if (toggleMute) toggleMute.addEventListener('click', () => {
    const realMuteBtn = document.getElementById('muteBtn')
    if (realMuteBtn) realMuteBtn.click()
  })

  const toggleTts = document.getElementById('mmToggleTts')
  if (toggleTts) toggleTts.addEventListener('click', () => {
    const realTtsBtn = document.getElementById('ttsMuteBtn')
    if (realTtsBtn) realTtsBtn.click()
  })

  const toggleMode = document.getElementById('mmToggleMode')
  if (toggleMode) toggleMode.addEventListener('click', () => {
    const realModeBtn = document.getElementById('modeBtn')
    if (realModeBtn) realModeBtn.click()
  })
})()

// Sliding-tray handles (.mm-actions / .mm-sys-strip trays) — toggles
// .mm-tray-retracted, which the CSS max-height transition above animates.
;(function _wireMMTrays() {
  document.querySelectorAll('.mm-tray-handle').forEach(handle => {
    handle.addEventListener('click', () => {
      const tray = handle.closest('.mm-tray')
      if (tray) tray.classList.toggle('mm-tray-retracted')
    })
  })
})()

// Commercial-grade redesign (2nd pass) — single requestAnimationFrame loop
// driving BOTH the diamond's breathing (facet SVG scale + inner-glow
// opacity, combined out-of-phase sine waves at different frequencies so the
// loop never reads as perfectly periodic — spec: "multiple overlapping
// sinusoidal functions... never perfectly periodic") AND the gold particle
// field (each particle on its own organic curve — figure-8, spiral, or
// drift — never a circular orbit, duration randomized 8-20s so nothing
// synchronizes). One shared loop, not two competing ones, per spec's own
// "implementation: JS with Math.sin ... updated via requestAnimationFrame."
// Reads document.body's existing state classes (speaking/processing) each
// frame rather than adding a second state machine: speaking multiplies
// particle speed 1.8x and raises opacity toward 0.8; processing eases every
// particle toward the diamond's center.
;(function _mmVisualLoop() {
  const facetSvg = document.getElementById('mmOrbFacetSvg')
  const glowEl   = document.getElementById('mmFacetGlow')
  const host     = document.getElementById('mmOrbParticles')
  if (!facetSvg || !glowEl || !host) return

  // 25 desktop / 18 tablet / 12 mobile — spec's exact 3-tier particle counts,
  // decided once at spawn (viewport width rarely changes after load on this
  // kiosk-style app; a full respawn on resize isn't worth the complexity).
  const vw = window.innerWidth
  const COUNT = vw <= 500 ? 12 : (vw <= 900 ? 18 : 25)

  // Per-particle motion parameters, each randomized independently so no two
  // particles ever move identically. Base position (cx/cy, % of host box) is
  // set once via left/top (a layout property, cheap since it's set only
  // here, not per frame); the per-frame animation below only ever touches
  // `transform`/`opacity` (compositor-only, no layout thrash).
  const particles = []
  for (let i = 0; i < COUNT; i++) {
    const el = document.createElement('div')
    el.className = 'mm-particle'
    const cx = 50 + (Math.random() * 60 - 30)
    const cy = 50 + (Math.random() * 60 - 30)
    el.style.left = cx + '%'
    el.style.top  = cy + '%'
    host.appendChild(el)
    particles.push({
      el,
      cx, cy,                                    // base position (% of host), needed to pull toward center while processing
      kind: i % 3,                              // 0 figure-8, 1 spiral, 2 organic drift — spread evenly, not random-clustered
      rx: 18 + Math.random() * 34,
      ry: 18 + Math.random() * 34,
      phase: Math.random() * Math.PI * 2,
      duration: 8 + Math.random() * 12,          // 8-20s per spec
      spiralDir: (i % 2 === 0) ? 1 : -1,
      baseOpacity: 0.4 + Math.random() * 0.3,    // 0.4-0.7 per spec
    })
  }

  // Host box's pixel size, used to convert the percentage-scale motion
  // amplitudes (rx/ry) into real px offsets for `transform: translate()`.
  // Re-measured on resize; a rAF-driven loop re-reading getBoundingClientRect()
  // every frame would force layout, so this is cached instead.
  let hostW = host.offsetWidth, hostH = host.offsetHeight
  window.addEventListener('resize', () => { hostW = host.offsetWidth; hostH = host.offsetHeight })

  const t0 = performance.now()
  function frame(now) {
    const t = (now - t0) / 1000

    const speaking   = document.body.classList.contains('speaking')
    const processing = document.body.classList.contains('processing')

    // ── Diamond breathing: 2 sines at different frequencies/phases, range
    //    lands at ~0.972–1.028 (spec's exact bounds). ──
    const scale = 1 + 0.020 * Math.sin(t * 0.42) + 0.008 * Math.sin(t * 1.11 + 1.7)
    facetSvg.style.transform = `scale(${scale.toFixed(4)})`

    // Glow breathes independently, out of phase with the scale above (own
    // frequency + phase offset) — never locks into the same rhythm.
    const glowOpacity = 0.4 + 0.15 * Math.sin(t * 0.35 + 2.4) + 0.05 * Math.sin(t * 0.9 + 0.6)
    glowEl.style.opacity = Math.max(0, Math.min(1, glowOpacity)).toFixed(3)

    // ── Particles ──
    const speedMul = speaking ? 1.8 : 1
    particles.forEach(p => {
      const pt = (t * speedMul) / p.duration * Math.PI * 2 + p.phase
      let dx, dy // organic-motion deltas, in % of host box
      if (p.kind === 0) {
        // figure-8 (2:1 lissajous ratio)
        dx = Math.sin(pt) * p.rx
        dy = Math.sin(pt * 2) * p.ry * 0.5
      } else if (p.kind === 1) {
        // slow spiral — radius itself oscillates while circling
        const r = (0.5 + 0.5 * Math.sin(pt * 0.3)) * p.rx
        dx = Math.cos(pt * p.spiralDir) * r
        dy = Math.sin(pt * p.spiralDir) * r * (p.ry / p.rx)
      } else {
        // organic drift — two off-ratio sines per axis so it never repeats on a short, obvious cycle
        dx = Math.sin(pt * 0.7 + p.phase) * p.rx * 0.7 + Math.sin(pt * 1.3) * p.rx * 0.3
        dy = Math.cos(pt * 0.6 + p.phase) * p.ry * 0.7 + Math.cos(pt * 1.7) * p.ry * 0.3
      }

      // Processing: pull each particle's actual base position (p.cx/p.cy)
      // toward the host's center (50%, the diamond) rather than just
      // damping the motion amplitude — a particle spawned far from center
      // needs an explicit inward pull, shrinking the orbit alone would just
      // freeze it in place out at the edge.
      if (processing) {
        dx = dx * 0.3 + (50 - p.cx) * 0.5
        dy = dy * 0.3 + (50 - p.cy) * 0.5
      }

      const pxX = (dx / 100) * hostW
      const pxY = (dy / 100) * hostH
      const opacity = speaking ? Math.min(0.8, p.baseOpacity + 0.2) : p.baseOpacity

      p.el.style.transform = `translate(${pxX.toFixed(1)}px, ${pxY.toFixed(1)}px)`
      p.el.style.opacity   = opacity.toFixed(2)
    })

    requestAnimationFrame(frame)
  }
  requestAnimationFrame(frame)
})()

// ════════════════════════════════════════════════════════════════════════════
// SERVICE WORKER
// ════════════════════════════════════════════════════════════════════════════
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {})
}

// ════════════════════════════════════════════════════════════════════════════
// ARMOR BAY — data, rendering, navigation, detail page, concept form
// ════════════════════════════════════════════════════════════════════════════

// ── Inline armor data (mirrors data/armor_knowledge.json) ──────────────────
const ARMOR_DATA = {
  primarios: [
    { id:'model-0',  name:'Modelo 0',   hours:'3h',           status:'COMPLETADO',
      descripcion:'El origen. Peto básico sin características especiales. Papel blanco y pegamento. El primer concepto, la base de todo.',
      innovaciones:'Ninguna — es el punto de partida.',
      limitaciones:'Sin simetría, sin estructura, sin complejidad.',
      evolucion:'Da paso al Modelo I con la introducción del celo como mejora de unión.',
      specs:'Papel blanco, pegamento.' },
    { id:'model-1',  name:'Modelo I',   hours:'10h',          status:'COMPLETADO',
      descripcion:'Primer modelo de cuerpo completo. Muy rudimentario y completamente blanco, sin simetría pero funcional como primer traje integral.',
      innovaciones:'Cuerpo completo. Sustitución de pegamento por celo.',
      limitaciones:'Ausencia total de simetría. Sin color ni estructura formal.',
      evolucion:'Sienta las bases para la introducción de elementos estructurales en el Modelo II.',
      specs:'Papel blanco, celo.' },
    { id:'model-2',  name:'Modelo II',  hours:'6h',           status:'COMPLETADO',
      descripcion:'Primera aproximación a la simetría. Introduce los rollos de cartón de papel higiénico como elemento de volumen y refuerzo estructural exterior.',
      innovaciones:'Primera simetría parcial. Uso de cartón reciclado para volumen.',
      limitaciones:'Simetría incompleta. Sin color.',
      evolucion:'La experimentación con volumen lleva al uso decorativo/estructural de cartulina en el Modelo III.',
      specs:'Papel, celo, rollos de cartón.' },
    { id:'model-3',  name:'Modelo III', hours:'6h',           status:'COMPLETADO',
      descripcion:'Primer modelo en color. La cartulina aparece como elemento simultáneamente decorativo y estructural. No es de cuerpo completo.',
      innovaciones:'Primer uso de color. Cartulina como material dual (estructura + estética).',
      limitaciones:'Nada ergonómico. No cubre cuerpo completo.',
      evolucion:'La estética mejora radicalmente en el Modelo IV con la introducción del velcro y el diseño plegable.',
      specs:'Cartulina de color.' },
    { id:'model-4',  name:'Modelo IV',  hours:'10h',          status:'COMPLETADO',
      descripcion:'Evolución completa respecto al III. Simétrico, visualmente sólido, plegable y con movilidad casi sin restricciones.',
      innovaciones:'Introducción del velcro. Diseño plegable. Alta ergonomía. Primera simetría real.',
      limitaciones:'No es de cuerpo completo.',
      evolucion:'La búsqueda de acabado visual lleva al cromado del Modelo V.',
      specs:'Cartulina, velcro.' },
    { id:'model-5',  name:'Modelo V',   hours:'8h',           status:'COMPLETADO',
      descripcion:'Aspecto completamente cromado. El casco incorpora papel de celofán en las ranuras de los ojos para mejorar el acabado visual.',
      innovaciones:'Primer acabado cromado. Celofán en visor del casco.',
      limitaciones:'No es de cuerpo completo. Articulación brazo-antebrazo mal medida, restringe movilidad significativamente.',
      evolucion:'Los problemas de movilidad y la ambición de cuerpo completo llevan al rediseño total del Modelo VI.',
      specs:'Cartulina cromada, celofán.' },
    { id:'model-6',  name:'Modelo VI',  hours:'30-50h',       status:'COMPLETADO',
      descripcion:'Ruptura total con los modelos anteriores. Cuerpo completo, diseño semimodular, reactor triangular. Primera armadura visualmente impresionante. La máscara incorpora un sistema de raíles que permite apertura y cierre.',
      innovaciones:'Cuerpo completo. Diseño semimodular. Reactor triangular. Sistema de raíles en máscara. Suelas acolchadas. Articulación abdominal con raíles de alta movilidad.',
      limitaciones:'La articulación abdominal de raíles sacrifica resistencia estructural — punto de ahogo en la columna inferior como única unión entre cintura y espalda superior. Movilidad general limitada.',
      evolucion:'La complejidad estructural inspira el proyecto paralelo T-45 y lleva al Modelo VII.',
      specs:'Cartulina, sistema de raíles, semimodular.' },
    { id:'model-7',  name:'Modelo VII', hours:'40-60h',       status:'NO COMPLETADO',
      descripcion:'Nunca terminado — el constructor creció y el traje quedó pequeño antes de poder finalizarlo. Aun así introduce los avances técnicos más significativos hasta la fecha.',
      innovaciones:'Primer circuito eléctrico (LEDs con interruptores). Reactor circular desmontable. Mecanismo retractor que oculta huecos de articulaciones. Luces en los ojos del casco. Apertura frontal completa. Cuerpo completo.',
      limitaciones:'Papel crespón en articulaciones demostró no ser apto — demasiado frágil. Luces en los ojos reducían visibilidad severamente. Nunca finalizado.',
      evolucion:'Sus sistemas electrónicos y mecánicos sientan la base conceptual del Modelo VIII.',
      specs:'Cartulina, cartón, LEDs, interruptores, papel crespón.' },
    { id:'model-8',  name:'Modelo VIII', nickname:'Midas', hours:'150-200h', status:'COMPLETADO',
      descripcion:'Salto generacional. La cartulina pasa a ser elemento puramente estético — la estructura la dan palos de madera con rigidez extrema. Precisión de construcción con error inferior a 400 micras en puntos específicos. Primer uso de Arduino con servos funcionales.',
      innovaciones:'Palos de madera como estructura primaria. Imanes en lugar de velcro. Arduino con servos (flaps). Interruptor oculto bajo las axilas. Diseño modular completo. Lentes de gafas de sol en lugar de luces en ojos. Sistema de correas, cintas elásticas y hebillas. Articulación abdominal con gomas-resorte.',
      limitaciones:'No completado. Estimación de 150-200 horas totales.',
      evolucion:'Sienta la arquitectura base del Modelo IX con ESP32 y sensores avanzados.',
      specs:'Cartulina (estética), madera estructural, imanes, Arduino Nano, servos, correas, hebillas, gomas elásticas. Precisión: <400 micras.' },
    { id:'model-9',  name:'Modelo IX', nickname:"Black'n Red", hours:'0h',           status:'NO CONSTRUIDO',
      descripcion:'En planificación. Salto completo a arquitectura distribuida con nodos ESP32 inteligentes. Estética negra metalizada con líneas rojas de tono agresivo.',
      innovaciones:'4 nodos ESP32 WROOM. Conectores pogo pin estilo MagSafe. Sistema LiPo con MOSFET y regulación buck-boost. Sensores: DS18B20, MPU6050, FSR, HC-SR04, BME280. Sistema de audio dual. Actuación por inclinación de muñeca, FSR masetero y contacto axilar. Posible HUD integrado.',
      limitaciones:'No construido — arquitectura en fase de diseño.',
      evolucion:'Lleva al Modelo X con coordinación de servos avanzada y visión instrumental.',
      specs:'ESP32 WROOM x4, LiPo, pogo pins, sensores múltiples. Colores: negro metalizado con líneas rojas.' },
    { id:'model-10', name:'Modelo X', nickname:'Infinity', hours:'0h',           status:'NO CONSTRUIDO',
      descripcion:'Concepto avanzado. Coordinación de servos en efecto dominó, visión instrumental para HUD mejorado, reintroducción de luces en los ojos.',
      innovaciones:'Coordinación servo avanzada (efecto dominó). Comandos por voz. Posible visión instrumental. Reactor triangular. Reintroducción de luces en ojos.',
      limitaciones:'No construido — concepto en desarrollo.',
      evolucion:'Modelo final de la serie actual. Posible integración con LIRA.',
      specs:'Servos coordinados, sistema de voz, HUD avanzado. Colores: rojo, negro y dorado.' },
  ],
  paralelos: [
    { id:'t45', name:'T-45', hours:'50h', status:'COMPLETADO',
      descripcion:'Proyecto paralelo inspirado en la servoarmadura de Fallout. No lleva denominación de Modelo — es una línea de investigación independiente centrada en apertura completa y resistencia estructural.',
      innovaciones:'Apertura completa del traje (acceso total al interior). Casco desplegable. Alta resistencia estructural — casco aguanta 8-10 kg. Estructura de botas con alzas de ~10cm.',
      limitaciones:'Sin articulación abdominal — movilidad del usuario gravemente afectada. Las alzas de las botas fueron retiradas por fragilidad estructural. Estética menos refinada que la línea Modelo.',
      evolucion:'Línea de investigación paralela; sus hallazgos estructurales informan a la serie principal.',
      specs:'Cartulina reforzada, sistema de apertura completa, casco desplegable.' },
  ],
}

// ── Shared suit silhouette SVG (blueprint schematic style) ─────────────────
function _suitSVG() {
  return `<svg class="armor-silhouette" viewBox="0 0 60 96" fill="none"
    stroke="currentColor" stroke-width="1" xmlns="http://www.w3.org/2000/svg">
    <rect x="19" y="2"  width="22" height="14" rx="2"/>
    <rect x="22" y="6"  width="16" height="5"  rx="1" opacity=".45"/>
    <rect x="24" y="16" width="12" height="5"/>
    <path d="M12 21 L48 21 L50 31 L10 31 Z"/>
    <circle cx="30" cy="26" r="4"/>
    <rect x="16" y="31" width="28" height="8" rx="1"/>
    <rect x="13" y="39" width="34" height="5" rx="1"/>
    <rect x="11" y="44" width="38" height="3"/>
    <rect x="2"  y="21" width="8"  height="17" rx="2"/>
    <rect x="2"  y="39" width="8"  height="13" rx="2"/>
    <rect x="2"  y="53" width="8"  height="6"  rx="1"/>
    <rect x="50" y="21" width="8"  height="17" rx="2"/>
    <rect x="50" y="39" width="8"  height="13" rx="2"/>
    <rect x="50" y="53" width="8"  height="6"  rx="1"/>
    <rect x="15" y="47" width="12" height="19" rx="2"/>
    <rect x="15" y="67" width="12" height="16" rx="2"/>
    <rect x="13" y="83" width="16" height="7"  rx="1"/>
    <rect x="33" y="47" width="12" height="19" rx="2"/>
    <rect x="33" y="67" width="12" height="16" rx="2"/>
    <rect x="31" y="83" width="16" height="7"  rx="1"/>
  </svg>`
}

// ── Model VI custom blueprint diagram — the only model with a bespoke
// silhouette instead of the generic placeholder above. Hardcoded to
// #4db8ff (a brightened variant of the fixed shell/UI blue — the generic
// diagrams still render in var(--accent)'s #3fa9f5, which never changes
// with personality; this one's bumped slightly brighter, along with
// thicker stroke-width throughout, since the original was hard to read)
// rather than currentColor, per spec: this diagram's color is pinned
// regardless of any personality-driven theming context it might ever be
// placed in. Front-view, angular/faceted take on a Mark 6-style
// suit — straight panel edges rather than rounded curves. Every shape is
// stroke-only (fill="none"); the thinner interior lines mark panel
// boundaries (visor, reactor, pauldron/bicep, bicep/forearm, torso/waist,
// thigh/shin/boot, gauntlet) the same way real color-zone seams would sit
// on the actual suit, without using any actual color fill to show them. -->
function _model6SVG() {
  return `<svg class="armor-silhouette model6-blueprint" viewBox="0 0 400 800"
    fill="none" stroke="#4db8ff" xmlns="http://www.w3.org/2000/svg">
    <!-- Helmet -->
    <path stroke-width="2.5" d="M200,30 L235,45 L248,78 L238,112 L213,136 L187,136 L162,112 L152,78 L165,45 Z"/>
    <!-- Visor (angular, split down the middle) -->
    <path stroke-width="2.5" d="M164,80 L236,80 L230,102 L170,102 Z"/>
    <line stroke-width="2" x1="200" y1="80"  x2="200" y2="102"/>
    <line stroke-width="2" x1="155" y1="78"  x2="245" y2="78"/>
    <line stroke-width="2" x1="162" y1="110" x2="238" y2="110"/>
    <!-- Neck -->
    <path stroke-width="2.5" d="M182,136 L218,136 L214,158 L186,158 Z"/>
    <!-- Shoulder pauldrons -->
    <path stroke-width="2.5" d="M90,172 L182,158 L182,224 L100,230 L74,204 Z"/>
    <path stroke-width="2.5" d="M310,172 L218,158 L218,224 L300,230 L326,204 Z"/>
    <line stroke-width="2" x1="84"  y1="224" x2="160" y2="221"/>
    <line stroke-width="2" x1="240" y1="221" x2="316" y2="224"/>
    <!-- Chest / torso, with reactor and ab-plate lines -->
    <path stroke-width="2.5" d="M182,158 L218,158 L260,190 L257,328 L200,349 L143,328 L140,190 Z"/>
    <circle stroke-width="2" cx="200" cy="234" r="27"/>
    <circle stroke-width="2" cx="200" cy="234" r="14"/>
    <line stroke-width="2" x1="168" y1="282" x2="232" y2="282"/>
    <line stroke-width="2" x1="170" y1="306" x2="230" y2="306"/>
    <line stroke-width="2" x1="145" y1="326" x2="255" y2="326"/>
    <!-- Waist -->
    <path stroke-width="2.5" d="M143,328 L200,349 L257,328 L249,398 L200,419 L151,398 Z"/>
    <!-- Biceps -->
    <path stroke-width="2.5" d="M74,224 L100,230 L109,236 L106,314 L69,317 L61,239 Z"/>
    <path stroke-width="2.5" d="M326,224 L300,230 L291,236 L294,314 L331,317 L339,239 Z"/>
    <line stroke-width="2" x1="64"  y1="315" x2="108" y2="312"/>
    <line stroke-width="2" x1="292" y1="312" x2="336" y2="315"/>
    <!-- Forearms -->
    <path stroke-width="2.5" d="M69,317 L106,314 L101,414 L73,417 Z"/>
    <path stroke-width="2.5" d="M331,317 L294,314 L299,414 L327,417 Z"/>
    <line stroke-width="2" x1="72"  y1="415" x2="103" y2="412"/>
    <line stroke-width="2" x1="297" y1="412" x2="328" y2="415"/>
    <!-- Gauntlets -->
    <path stroke-width="2.5" d="M73,417 L101,414 L99,456 L86,469 L67,461 L65,426 Z"/>
    <path stroke-width="2.5" d="M327,417 L299,414 L301,456 L314,469 L333,461 L335,426 Z"/>
    <line stroke-width="2" x1="68"  y1="436" x2="98"  y2="434"/>
    <line stroke-width="2" x1="302" y1="434" x2="332" y2="436"/>
    <!-- Thighs -->
    <path stroke-width="2.5" d="M151,398 L199,419 L196,556 L153,559 L149,421 Z"/>
    <path stroke-width="2.5" d="M249,398 L201,419 L204,556 L247,559 L251,421 Z"/>
    <line stroke-width="2" x1="151" y1="557" x2="197" y2="554"/>
    <line stroke-width="2" x1="203" y1="554" x2="249" y2="557"/>
    <!-- Shins -->
    <path stroke-width="2.5" d="M153,559 L196,556 L193,676 L159,679 Z"/>
    <path stroke-width="2.5" d="M247,559 L204,556 L207,676 L241,679 Z"/>
    <line stroke-width="2" x1="157" y1="677" x2="195" y2="675"/>
    <line stroke-width="2" x1="205" y1="675" x2="243" y2="677"/>
    <!-- Boots -->
    <path stroke-width="2.5" d="M159,679 L193,676 L197,731 L186,756 L141,756 L136,716 Z"/>
    <path stroke-width="2.5" d="M241,679 L207,676 L203,731 L214,756 L259,756 L264,716 Z"/>
    <line stroke-width="2" x1="136" y1="749" x2="197" y2="749"/>
    <line stroke-width="2" x1="203" y1="749" x2="264" y2="749"/>
  </svg>`
}

// ── Model VIII custom blueprint diagram — same rationale as Model VI's
// above (bespoke silhouette instead of the generic placeholder, only for
// this one model; hardcoded #4db8ff (brightened from the base #3fa9f5, same as Model VI), never currentColor/personality-owned).
// More refined than Model VI throughout: more panel lines per segment,
// boxier forearms, and a chest built around a central downward-pointing
// triangular reactor with facet lines radiating out to kite-shaped panels
// (the real suit's actual centerpiece — see armor_knowledge.json's
// description of Model VIII), plus a separately-segmented abdominal
// section below it (3 lines vs Model VI's 2), matching "the most advanced
// completed suit" in the collection. -->
function _model8SVG() {
  return `<svg class="armor-silhouette model8-blueprint" viewBox="0 0 400 800"
    fill="none" stroke="#4db8ff" xmlns="http://www.w3.org/2000/svg">
    <!-- Helmet — more angular/refined faceting than Model VI -->
    <path stroke-width="2.5" d="M200,28 L232,42 L246,72 L240,98 L226,124 L200,140 L174,124 L160,98 L154,72 L168,42 Z"/>
    <!-- Visor slit (thinner/more precise than Model VI's band) -->
    <path stroke-width="2.5" d="M172,82 L228,82 L228,90 L172,90 Z"/>
    <line stroke-width="2" x1="200" y1="82"  x2="200" y2="90"/>
    <line stroke-width="2" x1="158" y1="72"  x2="242" y2="72"/>
    <line stroke-width="2" x1="168" y1="100" x2="232" y2="100"/>
    <line stroke-width="2" x1="178" y1="120" x2="222" y2="120"/>
    <!-- Neck -->
    <path stroke-width="2.5" d="M183,140 L217,140 L213,160 L187,160 Z"/>
    <!-- Shoulder pauldrons — large, structured, with an added facet line -->
    <path stroke-width="2.5" d="M85,170 L184,158 L184,226 L98,234 L68,202 Z"/>
    <path stroke-width="2.5" d="M315,170 L216,158 L216,226 L302,234 L332,202 Z"/>
    <line stroke-width="2" x1="100" y1="180" x2="178" y2="195"/>
    <line stroke-width="2" x1="300" y1="180" x2="222" y2="195"/>
    <line stroke-width="2" x1="80"  y1="228" x2="165" y2="224"/>
    <line stroke-width="2" x1="235" y1="224" x2="320" y2="228"/>
    <!-- Chest / torso outline -->
    <path stroke-width="2.5" d="M184,158 L216,158 L262,192 L259,330 L200,352 L141,330 L138,192 Z"/>
    <!-- Collar seam -->
    <line stroke-width="2" x1="158" y1="168" x2="242" y2="168"/>
    <!-- Central reactor: triangle pointing down, with an inner outline for depth -->
    <path stroke-width="2.5" d="M183,200 L217,200 L200,252 Z"/>
    <path stroke-width="2"   d="M191,212 L209,212 L200,242 Z"/>
    <!-- Chest facets converging on the reactor — the centerpiece geometric pattern -->
    <line stroke-width="2" x1="183" y1="200" x2="150" y2="182"/>
    <line stroke-width="2" x1="217" y1="200" x2="250" y2="182"/>
    <line stroke-width="2" x1="183" y1="200" x2="145" y2="225"/>
    <line stroke-width="2" x1="217" y1="200" x2="255" y2="225"/>
    <line stroke-width="2" x1="150" y1="182" x2="145" y2="225"/>
    <line stroke-width="2" x1="250" y1="182" x2="255" y2="225"/>
    <!-- Segmented abdominal section below the reactor -->
    <line stroke-width="2" x1="160" y1="270" x2="240" y2="270"/>
    <line stroke-width="2" x1="163" y1="292" x2="237" y2="292"/>
    <line stroke-width="2" x1="166" y1="314" x2="234" y2="314"/>
    <line stroke-width="2" x1="141" y1="328" x2="259" y2="328"/>
    <!-- Waist -->
    <path stroke-width="2.5" d="M141,330 L200,352 L259,330 L251,400 L200,421 L149,400 Z"/>
    <!-- Biceps -->
    <path stroke-width="2.5" d="M68,225 L98,234 L107,240 L104,315 L66,318 L58,242 Z"/>
    <path stroke-width="2.5" d="M332,225 L302,234 L293,240 L296,315 L334,318 L342,242 Z"/>
    <line stroke-width="2" x1="61"  y1="316" x2="106" y2="313"/>
    <line stroke-width="2" x1="294" y1="313" x2="339" y2="316"/>
    <!-- Forearms — slightly boxier (straighter, less taper) than Model VI -->
    <path stroke-width="2.5" d="M66,318 L104,315 L102,410 L68,412 Z"/>
    <path stroke-width="2.5" d="M334,318 L296,315 L298,410 L332,412 Z"/>
    <line stroke-width="2" x1="70"  y1="360" x2="100" y2="358"/>
    <line stroke-width="2" x1="300" y1="358" x2="330" y2="360"/>
    <line stroke-width="2" x1="69"  y1="408" x2="101" y2="405"/>
    <line stroke-width="2" x1="299" y1="405" x2="331" y2="408"/>
    <!-- Gauntlets -->
    <path stroke-width="2.5" d="M69,408 L101,405 L99,448 L86,461 L67,453 L65,418 Z"/>
    <path stroke-width="2.5" d="M331,408 L299,405 L301,448 L314,461 L333,453 L335,418 Z"/>
    <line stroke-width="2" x1="68"  y1="428" x2="98"  y2="426"/>
    <line stroke-width="2" x1="70"  y1="440" x2="96"  y2="438"/>
    <line stroke-width="2" x1="302" y1="426" x2="332" y2="428"/>
    <line stroke-width="2" x1="304" y1="438" x2="330" y2="440"/>
    <!-- Thighs -->
    <path stroke-width="2.5" d="M149,400 L199,421 L196,556 L153,559 L147,423 Z"/>
    <path stroke-width="2.5" d="M251,400 L201,421 L204,556 L247,559 L253,423 Z"/>
    <line stroke-width="2" x1="152" y1="480" x2="197" y2="478"/>
    <line stroke-width="2" x1="203" y1="478" x2="248" y2="480"/>
    <line stroke-width="2" x1="151" y1="557" x2="197" y2="554"/>
    <line stroke-width="2" x1="203" y1="554" x2="249" y2="557"/>
    <!-- Shins -->
    <path stroke-width="2.5" d="M153,559 L196,556 L193,676 L159,679 Z"/>
    <path stroke-width="2.5" d="M247,559 L204,556 L207,676 L241,679 Z"/>
    <line stroke-width="2" x1="156" y1="615" x2="194" y2="613"/>
    <line stroke-width="2" x1="206" y1="613" x2="244" y2="615"/>
    <line stroke-width="2" x1="157" y1="677" x2="195" y2="675"/>
    <line stroke-width="2" x1="205" y1="675" x2="243" y2="677"/>
    <!-- Boots, with a defined sole line -->
    <path stroke-width="2.5" d="M159,679 L193,676 L197,731 L186,756 L141,756 L136,716 Z"/>
    <path stroke-width="2.5" d="M241,679 L207,676 L203,731 L214,756 L259,756 L264,716 Z"/>
    <line stroke-width="2" x1="140" y1="700" x2="193" y2="698"/>
    <line stroke-width="2" x1="207" y1="698" x2="260" y2="700"/>
    <line stroke-width="2" x1="136" y1="749" x2="197" y2="749"/>
    <line stroke-width="2" x1="203" y1="749" x2="264" y2="749"/>
  </svg>`
}

// ── Model X (Infinity) custom blueprint diagram — same rationale as Model
// VI/VIII above (bespoke silhouette instead of the generic placeholder,
// only for this one model; hardcoded #4db8ff (brightened from the base #3fa9f5, same as Model VI), never currentColor). The
// most advanced/aggressive of the three: wider shoulder stance, a small
// vertical-oval reactor inside a diamond frame (vs VI's plain circle and
// VIII's downward triangle) with sharp diagonal lines converging on it
// instead of VIII's more symmetric kite facets, boxier closed-fist
// gauntlets, small rect "tech detail" marks on the forearms, and more
// panel lines per segment than either earlier model throughout. -->
function _model10SVG() {
  return `<svg class="armor-silhouette model10-blueprint" viewBox="0 0 400 800"
    fill="none" stroke="#4db8ff" xmlns="http://www.w3.org/2000/svg">
    <!-- Helmet — sharper peak, more aggressive faceting than Model VI/VIII -->
    <path stroke-width="2.5" d="M200,22 L228,38 L244,68 L236,96 L220,122 L200,138 L180,122 L164,96 L156,68 L172,38 Z"/>
    <!-- Distinctive separate eye slits (not one visor band) -->
    <path stroke-width="2.5" d="M172,76 L196,76 L194,86 L174,86 Z"/>
    <path stroke-width="2.5" d="M204,76 L228,76 L226,86 L206,86 Z"/>
    <line stroke-width="2" x1="160" y1="66"  x2="240" y2="66"/>
    <line stroke-width="2" x1="168" y1="100" x2="232" y2="100"/>
    <line stroke-width="2" x1="178" y1="118" x2="222" y2="118"/>
    <!-- Neck -->
    <path stroke-width="2.5" d="M183,138 L217,138 L213,158 L187,158 Z"/>
    <!-- Shoulder pauldrons — wider stance, sharper than Model VIII -->
    <path stroke-width="2.5" d="M60,168 L182,156 L182,226 L92,236 L48,198 Z"/>
    <path stroke-width="2.5" d="M340,168 L218,156 L218,226 L308,236 L352,198 Z"/>
    <line stroke-width="2" x1="78"  y1="178" x2="172" y2="192"/>
    <line stroke-width="2" x1="322" y1="178" x2="228" y2="192"/>
    <line stroke-width="2" x1="74"  y1="230" x2="160" y2="224"/>
    <line stroke-width="2" x1="240" y1="224" x2="326" y2="230"/>
    <!-- Chest / torso outline -->
    <path stroke-width="2.5" d="M182,156 L218,156 L264,190 L261,330 L200,354 L139,330 L136,190 Z"/>
    <line stroke-width="2" x1="156" y1="166" x2="244" y2="166"/>
    <!-- Reactor: small vertical oval inside a diamond frame -->
    <path stroke-width="2.5" d="M200,193 L223,219 L200,246 L177,219 Z"/>
    <ellipse stroke-width="2" cx="200" cy="219" rx="10" ry="18"/>
    <!-- Sharp diagonal lines converging on the reactor -->
    <line stroke-width="2" x1="145" y1="178" x2="200" y2="193"/>
    <line stroke-width="2" x1="255" y1="178" x2="200" y2="193"/>
    <line stroke-width="2" x1="134" y1="248" x2="177" y2="219"/>
    <line stroke-width="2" x1="266" y1="248" x2="223" y2="219"/>
    <line stroke-width="2" x1="200" y1="246" x2="200" y2="288"/>
    <!-- Diagonal accents suggesting layered plating -->
    <line stroke-width="2" x1="152" y1="198" x2="188" y2="226"/>
    <line stroke-width="2" x1="248" y1="198" x2="212" y2="226"/>
    <!-- Segmented abdominal section — most segments of the three models -->
    <line stroke-width="2" x1="158" y1="266" x2="242" y2="266"/>
    <line stroke-width="2" x1="162" y1="284" x2="238" y2="284"/>
    <line stroke-width="2" x1="165" y1="302" x2="235" y2="302"/>
    <line stroke-width="2" x1="168" y1="320" x2="232" y2="320"/>
    <line stroke-width="2" x1="170" y1="275" x2="195" y2="293"/>
    <line stroke-width="2" x1="230" y1="275" x2="205" y2="293"/>
    <line stroke-width="2" x1="139" y1="330" x2="261" y2="330"/>
    <!-- Waist -->
    <path stroke-width="2.5" d="M139,330 L200,354 L261,330 L253,402 L200,423 L147,402 Z"/>
    <!-- Biceps -->
    <path stroke-width="2.5" d="M48,224 L92,236 L102,242 L99,317 L58,320 L38,244 Z"/>
    <path stroke-width="2.5" d="M352,224 L308,236 L298,242 L301,317 L342,320 L362,244 Z"/>
    <line stroke-width="2" x1="45"  y1="260" x2="90"  y2="255"/>
    <line stroke-width="2" x1="310" y1="255" x2="355" y2="260"/>
    <line stroke-width="2" x1="52"  y1="318" x2="101" y2="315"/>
    <line stroke-width="2" x1="299" y1="315" x2="348" y2="318"/>
    <!-- Forearms — extra rect "tech detail" marks suggesting circuitry underneath -->
    <path stroke-width="2.5" d="M58,320 L99,317 L96,412 L62,415 Z"/>
    <path stroke-width="2.5" d="M342,320 L301,317 L304,412 L338,415 Z"/>
    <line stroke-width="2" x1="64" y1="362" x2="94"  y2="360"/>
    <line stroke-width="2" x1="306" y1="360" x2="336" y2="362"/>
    <rect stroke-width="2" x="68" y="375" width="20" height="10"/>
    <rect stroke-width="2" x="312" y="375" width="20" height="10"/>
    <line stroke-width="2" x1="61"  y1="410" x2="93"  y2="407"/>
    <line stroke-width="2" x1="307" y1="407" x2="339" y2="410"/>
    <!-- Gauntlets — slightly closed fists, more compact than an open gauntlet -->
    <path stroke-width="2.5" d="M61,410 L93,407 L91,443 L80,458 L64,452 L58,420 Z"/>
    <path stroke-width="2.5" d="M339,410 L307,407 L309,443 L320,458 L336,452 L342,420 Z"/>
    <line stroke-width="2" x1="62"  y1="428" x2="90"  y2="426"/>
    <line stroke-width="2" x1="310" y1="426" x2="338" y2="428"/>
    <!-- Thighs -->
    <path stroke-width="2.5" d="M147,402 L198,423 L195,558 L151,561 L145,425 Z"/>
    <path stroke-width="2.5" d="M253,402 L202,423 L205,558 L249,561 L255,425 Z"/>
    <line stroke-width="2" x1="150" y1="460" x2="193" y2="455"/>
    <line stroke-width="2" x1="207" y1="455" x2="250" y2="460"/>
    <line stroke-width="2" x1="149" y1="559" x2="196" y2="556"/>
    <line stroke-width="2" x1="204" y1="556" x2="251" y2="559"/>
    <!-- Shins -->
    <path stroke-width="2.5" d="M151,561 L195,558 L191,678 L157,681 Z"/>
    <path stroke-width="2.5" d="M249,561 L205,558 L209,678 L243,681 Z"/>
    <line stroke-width="2" x1="154" y1="610" x2="192" y2="607"/>
    <line stroke-width="2" x1="208" y1="607" x2="246" y2="610"/>
    <line stroke-width="2" x1="155" y1="679" x2="193" y2="677"/>
    <line stroke-width="2" x1="207" y1="677" x2="245" y2="679"/>
    <!-- Boots, with a defined sole line -->
    <path stroke-width="2.5" d="M157,681 L191,677 L196,733 L184,758 L138,758 L133,718 Z"/>
    <path stroke-width="2.5" d="M243,681 L209,677 L204,733 L216,758 L262,758 L267,718 Z"/>
    <line stroke-width="2" x1="137" y1="702" x2="191" y2="700"/>
    <line stroke-width="2" x1="209" y1="700" x2="263" y2="702"/>
    <line stroke-width="2" x1="133" y1="751" x2="196" y2="751"/>
    <line stroke-width="2" x1="204" y1="751" x2="267" y2="751"/>
  </svg>`
}

// ── Model VII custom blueprint diagram — same rationale as Model VI/VIII/X
// above (bespoke silhouette instead of the generic placeholder, only for
// this one model; hardcoded #4db8ff (brightened from the base #3fa9f5, same as Model VI), never currentColor). The most
// Mark-7-faithful diagram in the series — a rounded dome helmet (the only
// one of the four using actual curves rather than pure angular facets),
// a circular double-ring reactor with V-shaped pec lines converging on it,
// rounded pauldrons, and knee-guard/toe-cap details the other three don't
// have. Deliberately NOT a trace: reactor sits a touch lower than the real
// Mark 7's, the pauldron outer edge is a shallower, less circular curve,
// and the thigh/shin seam is a diagonal cut instead of a straight one —
// meant to read as "clearly inspired by," not "copied from." -->
function _model7SVG() {
  return `<svg class="armor-silhouette model7-blueprint" viewBox="0 0 400 800"
    fill="none" stroke="#4db8ff" xmlns="http://www.w3.org/2000/svg">
    <!-- Helmet — rounded dome top, angular cheekpieces, defined chin guard -->
    <path stroke-width="2.5" d="M160,70 A42,46 0 0 1 240,70 L235,96 L221,122 L200,134 L179,122 L165,96 Z"/>
    <!-- Narrow horizontal eye slits -->
    <path stroke-width="2.5" d="M168,88 L194,88 L194,94 L168,94 Z"/>
    <path stroke-width="2.5" d="M206,88 L232,88 L232,94 L206,94 Z"/>
    <!-- Neck piece, with a defined mid-neck seam -->
    <path stroke-width="2.5" d="M182,134 L218,134 L214,156 L186,156 Z"/>
    <line stroke-width="2" x1="184" y1="145" x2="216" y2="145"/>
    <!-- Shoulders: rounded Mark-7-style pauldrons (curved outer edge, unlike
         Model VI/VIII/X's sharp angular point), with a raised edge line -->
    <path stroke-width="2.5" d="M90,168 L184,154 L184,222 L102,226 Q78,222 78,198 Q78,180 90,168 Z"/>
    <path stroke-width="2.5" d="M310,168 L216,154 L216,222 L298,226 Q322,222 322,198 Q322,180 310,168 Z"/>
    <line stroke-width="2" x1="95"  y1="178" x2="175" y2="168"/>
    <line stroke-width="2" x1="305" y1="178" x2="225" y2="168"/>
    <line stroke-width="2" x1="85"  y1="224" x2="165" y2="220"/>
    <line stroke-width="2" x1="235" y1="220" x2="315" y2="224"/>
    <!-- Chest / torso outline -->
    <path stroke-width="2.5" d="M182,154 L218,154 L258,186 L255,328 L200,350 L145,328 L142,186 Z"/>
    <line stroke-width="2" x1="168" y1="162" x2="232" y2="162"/>
    <!-- Iconic V-shaped pec lines converging on the reactor -->
    <line stroke-width="2" x1="160" y1="170" x2="200" y2="215"/>
    <line stroke-width="2" x1="240" y1="170" x2="200" y2="215"/>
    <!-- Reactor — double circle outline, sitting a touch lower than the real
         Mark 7's for a deliberate, recognizable difference -->
    <circle stroke-width="2.5" cx="200" cy="235" r="24"/>
    <circle stroke-width="2"   cx="200" cy="235" r="15"/>
    <!-- Abdominal bands, a slight taper toward the waist -->
    <line stroke-width="2" x1="165" y1="275" x2="235" y2="273"/>
    <line stroke-width="2" x1="168" y1="297" x2="232" y2="295"/>
    <line stroke-width="2" x1="171" y1="317" x2="229" y2="315"/>
    <line stroke-width="2" x1="145" y1="328" x2="255" y2="328"/>
    <!-- Waist, tapered -->
    <path stroke-width="2.5" d="M145,330 L200,350 L255,330 L247,398 L200,419 L153,398 Z"/>
    <!-- Biceps -->
    <path stroke-width="2.5" d="M85,222 L100,228 L108,234 L105,312 L70,315 L63,236 Z"/>
    <path stroke-width="2.5" d="M315,222 L300,228 L292,234 L295,312 L330,315 L337,236 Z"/>
    <line stroke-width="2" x1="66"  y1="313" x2="107" y2="310"/>
    <line stroke-width="2" x1="293" y1="310" x2="334" y2="313"/>
    <!-- Forearms — slight flare toward the wrist -->
    <path stroke-width="2.5" d="M70,315 L105,312 L110,410 L72,414 Z"/>
    <path stroke-width="2.5" d="M330,315 L295,312 L290,410 L328,414 Z"/>
    <line stroke-width="2" x1="73"  y1="360" x2="107" y2="357"/>
    <line stroke-width="2" x1="293" y1="357" x2="327" y2="360"/>
    <!-- Gauntlet, with knuckle lines -->
    <path stroke-width="2.5" d="M72,414 L109,410 L107,452 L94,466 L74,458 L68,420 Z"/>
    <path stroke-width="2.5" d="M328,414 L291,410 L293,452 L306,466 L326,458 L332,420 Z"/>
    <line stroke-width="2" x1="76" y1="432" x2="105" y2="429"/>
    <line stroke-width="2" x1="78" y1="444" x2="103" y2="441"/>
    <line stroke-width="2" x1="295" y1="429" x2="324" y2="432"/>
    <line stroke-width="2" x1="297" y1="441" x2="322" y2="444"/>
    <!-- Thighs -->
    <path stroke-width="2.5" d="M153,398 L199,419 L195,552 L154,558 L149,421 Z"/>
    <path stroke-width="2.5" d="M247,398 L201,419 L205,552 L246,558 L251,421 Z"/>
    <!-- Knee guards -->
    <path stroke-width="2.5" d="M158,542 L172,536 L184,545 L172,556 Z"/>
    <path stroke-width="2.5" d="M242,542 L228,536 L216,545 L228,556 Z"/>
    <!-- Thigh/shin seam — a diagonal cut, unlike the near-horizontal
         division lines on the other three models -->
    <line stroke-width="2" x1="152" y1="556" x2="197" y2="548"/>
    <line stroke-width="2" x1="248" y1="556" x2="203" y2="548"/>
    <!-- Shins -->
    <path stroke-width="2.5" d="M154,558 L195,552 L191,674 L159,678 Z"/>
    <path stroke-width="2.5" d="M246,558 L205,552 L209,674 L241,678 Z"/>
    <line stroke-width="2" x1="157" y1="612" x2="193" y2="608"/>
    <line stroke-width="2" x1="207" y1="608" x2="243" y2="612"/>
    <line stroke-width="2" x1="158" y1="676" x2="192" y2="673"/>
    <line stroke-width="2" x1="208" y1="673" x2="242" y2="676"/>
    <!-- Boots, with a defined toe cap and sole line -->
    <path stroke-width="2.5" d="M159,678 L191,673 L195,728 L184,753 L138,753 L134,714 Z"/>
    <path stroke-width="2.5" d="M241,678 L209,673 L205,728 L216,753 L262,753 L266,714 Z"/>
    <line stroke-width="2" x1="138" y1="735" x2="184" y2="733"/>
    <line stroke-width="2" x1="216" y1="733" x2="262" y2="735"/>
    <line stroke-width="2" x1="134" y1="747" x2="195" y2="747"/>
    <line stroke-width="2" x1="205" y1="747" x2="266" y2="747"/>
  </svg>`
}

// ── Model-specific blueprint diagram, falling back to the generic
// silhouette — shared by the grid cards (_renderArmorGrid) and the detail
// view (_openDetail) so the two never drift out of sync on which models
// have a bespoke diagram (currently VI, VII, VIII, X). ───────────────────
function _armorDiagramSVG(id) {
  switch (id) {
    case 'model-6':  return _model6SVG()
    case 'model-7':  return _model7SVG()
    case 'model-8':  return _model8SVG()
    case 'model-10': return _model10SVG()
    default:         return _suitSVG()
  }
}

// ── Badge CSS class from status string ─────────────────────────────────────
function _badgeClass(status) {
  switch (status) {
    case 'COMPLETADO':     return 'badge-completado'
    case 'NO COMPLETADO':  return 'badge-no-completado'
    case 'EN CONSTRUCCIÓN':return 'badge-construccion'
    default:               return 'badge-no-construido'
  }
}

// ── Render a list of models into #armorGrid ─────────────────────────────────
function _renderArmorGrid(models) {
  const grid = document.getElementById('armorGrid')
  grid.innerHTML = ''
  models.forEach(m => {
    const card = document.createElement('div')
    card.className = 'armor-card'
    card.dataset.id = m.id
    card.innerHTML = `
      <div class="armor-sil-wrap">
        ${_armorDiagramSVG(m.id)}
        <div class="armor-scan-line"></div>
      </div>
      <div class="armor-card-name">${esc(m.name)}</div>
      <div class="armor-card-hours">${esc(m.hours)}</div>
      <div class="armor-badge ${_badgeClass(m.status)}">${esc(m.status)}</div>
      <div class="armor-card-hint">${esc((m.innovaciones || '').slice(0, 90))}</div>`
    card.addEventListener('click', () => _openDetail(m))
    grid.appendChild(card)
  })
}

// ── Detail panel open / close ───────────────────────────────────────────────
const armorDetailView = document.getElementById('armorDetailView')
const detailBackBtn   = document.getElementById('detailBackBtn')
const detailName2     = document.getElementById('detailName2')
const detailBadge2    = document.getElementById('detailBadge2')
const detailBody2     = document.getElementById('detailBody2')
const detailSilWrap   = document.getElementById('detailSilWrap')

// ── CONTROLAR / VER HUD — placeholder actions, per-model message only,
// no real functionality yet. Explicit per-id lookup (not a range/switch)
// so each entry maps 1:1 to the spec and is easy to audit at a glance.
const _CONTROLAR_MESSAGES = {
  'model-0':  'Este modelo no dispone de sistemas electrónicos compatibles',
  'model-1':  'Este modelo no dispone de sistemas electrónicos compatibles',
  'model-2':  'Este modelo no dispone de sistemas electrónicos compatibles',
  'model-3':  'Este modelo no dispone de sistemas electrónicos compatibles',
  'model-4':  'Este modelo no dispone de sistemas electrónicos compatibles',
  'model-5':  'Este modelo no dispone de sistemas electrónicos compatibles',
  'model-6':  'Este modelo no tiene esta función',
  'model-7':  'Este modelo no tiene esta función',
  'model-8':  'Función en desarrollo — sistemas Arduino no compatibles aún',
  'model-9':  'Este modelo aún no está construido',
  'model-10': 'Este modelo aún no está construido',
  't45':      'Este modelo no tiene esta función',
}
const _HUD_MESSAGES = {
  'model-0':  'Este modelo no tiene esta función',
  'model-1':  'Este modelo no tiene esta función',
  'model-2':  'Este modelo no tiene esta función',
  'model-3':  'Este modelo no tiene esta función',
  'model-4':  'Este modelo no tiene esta función',
  'model-5':  'Este modelo no tiene esta función',
  'model-6':  'Este modelo no tiene esta función',
  'model-7':  'Este modelo no tiene esta función',
  'model-8':  'Función en desarrollo',
  'model-9':  'No disponible — modelo no construido',
  'model-10': 'No disponible — modelo no construido',
  't45':      'Este modelo no tiene esta función',
}

let _currentDetailModelId    = null
let _currentDetailModelRoman = null   // e.g. 'VIII' — derived from m.name, used by hud_context events
let _detailToastTimer        = null
let _detailSectionObserver   = null   // IntersectionObserver — see _setupDetailSectionObserver()
let _lastDetailSection       = null   // dedupe: only emit armor_section on an actual change

// Inline toast below the buttons — fades/expands in, auto-hides after a
// few seconds. Deliberately not a modal, per the spec.
function _showDetailToast(message) {
  const el = document.getElementById('detailActionToast')
  if (!el) return
  clearTimeout(_detailToastTimer)
  el.textContent = message
  el.classList.add('visible')
  _detailToastTimer = setTimeout(() => el.classList.remove('visible'), 4000)
}

// Section key → title, in render order. Keys are what hud_context's
// 'armor_section' events and PANTALLA ACTUAL both refer to a scrolled-to
// section by (see _setupDetailSectionObserver below).
const _DETAIL_SECTION_DEFS = [
  { key: 'resumen',      title: 'Resumen',                     field: 'descripcion',  type: 'text' },
  { key: 'horas',        title: 'Horas de construcción',       field: 'hours',        type: 'text' },
  { key: 'innovaciones', title: 'Innovaciones clave',          field: 'innovaciones', type: 'list' },
  { key: 'limitaciones', title: 'Limitaciones conocidas',      field: 'limitaciones', type: 'text' },
  { key: 'evolucion',    title: 'Evolución',                   field: 'evolucion',    type: 'text' },
  { key: 'specs',        title: 'Materiales y specs técnicas', field: 'specs',        type: 'text' },
]

function _openDetail(m) {
  _currentDetailModelId    = m.id
  _currentDetailModelRoman = (m.name || '').replace(/^Modelo\s+/i, '')
  _lastDetailSection       = null
  document.getElementById('detailActionToast').classList.remove('visible')
  detailName2.textContent = m.nickname ? `${m.name} — ${m.nickname}` : m.name
  detailBadge2.innerHTML  = `<div class="armor-badge ${_badgeClass(m.status)}">${esc(m.status)}</div>`
  detailSilWrap.innerHTML = _armorDiagramSVG(m.id)

  // Build spec sections in the order requested — only show ones with
  // content. 'list' sections split on ". " into bullet points; everything
  // else renders as a plain paragraph (reuses .detail-sec/.detail-sec-title
  // styling either way). Each section carries data-section so
  // _setupDetailSectionObserver can report which one is on screen.
  detailBody2.innerHTML = _DETAIL_SECTION_DEFS
    .map(s => ({ ...s, body: m[s.field] }))
    .filter(s => s.body)
    .map(s => {
      if (s.type === 'list') {
        const items = s.body.split(/\.\s+/).map(x => x.trim().replace(/\.$/, '')).filter(Boolean)
        return `
          <div class="detail-sec" data-section="${s.key}">
            <div class="detail-sec-title">${esc(s.title)}</div>
            <ul class="detail-sec-list">${items.map(i => `<li>${esc(i)}</li>`).join('')}</ul>
          </div>`
      }
      return `
        <div class="detail-sec" data-section="${s.key}">
          <div class="detail-sec-title">${esc(s.title)}</div>
          <div class="detail-sec-body">${esc(s.body)}</div>
        </div>`
    })
    .join('')

  armorDetailView.classList.add('active')
  _markUiInteraction()
  _emitUserActivity('armor_detail', 'opening', { model: m.id, name: m.name })
  _emitHudContext({
    type: 'armor_detail',
    model: _currentDetailModelRoman,
    name: m.nickname || m.name,
    section: 'detail',
    data: {
      id: m.id, name: m.name, nickname: m.nickname || null,
      hours: m.hours, status: m.status, descripcion: m.descripcion,
      innovaciones: m.innovaciones, limitaciones: m.limitaciones,
      evolucion: m.evolucion, specs: m.specs,
    },
  })
  _setupDetailSectionObserver()
}

// Reports which section of the open armor's detail view is actually on
// screen, as Joan scrolls — a lightweight hud_context refinement of the
// armor_detail context above (core/server.py merges it in rather than
// replacing the full armor data — see its 'hud_context' handler). Picks
// the most-visible section on every intersection change; re-created each
// time _openDetail runs so a stale observer never lingers across models.
function _setupDetailSectionObserver() {
  if (_detailSectionObserver) { _detailSectionObserver.disconnect(); _detailSectionObserver = null }
  const root = document.querySelector('.armor-detail-page')
  if (!root) return
  _detailSectionObserver = new IntersectionObserver((entries) => {
    const visible = entries.filter(e => e.isIntersecting)
    if (!visible.length) return
    visible.sort((a, b) => b.intersectionRatio - a.intersectionRatio)
    const section = visible[0].target.dataset.section
    if (!section || section === _lastDetailSection) return
    _lastDetailSection = section
    _emitHudContext({ type: 'armor_section', model: _currentDetailModelRoman, section })
  }, { root, threshold: [0.4, 0.6] })
  detailBody2.querySelectorAll('.detail-sec[data-section]').forEach(el => _detailSectionObserver.observe(el))
}

function _closeDetailView() {
  armorDetailView.classList.remove('active')
  if (_detailSectionObserver) { _detailSectionObserver.disconnect(); _detailSectionObserver = null }
  _emitHudContext({ type: 'idle', section: _ACTIVITY_SECTION_MAP[_currentSection] || _currentSection })
}

detailBackBtn.addEventListener('click', _closeDetailView)

document.getElementById('detailControlarBtn').addEventListener('click', () => {
  _showDetailToast(_CONTROLAR_MESSAGES[_currentDetailModelId] || 'Función no disponible')
})
document.getElementById('detailHudBtn').addEventListener('click', () => {
  _showDetailToast(_HUD_MESSAGES[_currentDetailModelId] || 'Función no disponible')
})

// ── Sub-tab switching (Primarios | Paralelos | Conceptuales | Diseño) ───────
// Bug fix: NÚCLEO LIRA's own sub-tabs (Estado/Pensamiento/Memoria/Mapa,
// #section-core) share this exact same .armor-subtab CSS class — an
// unscoped `.armor-subtab` selector here matched BOTH sets of tabs. That
// meant every CORE tab silently got a SECOND click handler wired below
// (armorSubtabs.forEach(btn => ...)) calling _switchSubTab(btn.dataset.sub)
// — undefined for a CORE tab, since those use data-core-sub, not data-sub
// — which then did `armorSubtabs.forEach(b => b.classList.toggle('active',
// b.dataset.sub === sub))`: b.dataset.sub === undefined is TRUE for every
// CORE tab (none of them have data-sub at all), so this second handler
// re-activated ALL FOUR of them right after _switchCoreSubTab() (CORE's
// own, correctly-scoped handler — see that function) had just set only
// the clicked one — the exact "multiple tabs active at once" bug. Scoping
// this query to #section-armor excludes CORE's tabs entirely, so they
// only ever get their own single, correct click handler.
const armorSubtabs  = document.querySelectorAll('#section-armor .armor-subtab')
const armorGridWrap = document.getElementById('armorGridWrap')
const conceptPanel  = document.getElementById('conceptPanel')
const designPanel   = document.getElementById('designPanel')

let _currentSub = 'primarios'

function _switchSubTab(sub) {
  _currentSub = sub
  armorSubtabs.forEach(b => b.classList.toggle('active', b.dataset.sub === sub))

  const isGrid = sub === 'primarios' || sub === 'paralelos'
  armorGridWrap.style.display = isGrid ? '' : 'none'
  conceptPanel.classList.toggle('active', sub === 'conceptuales')
  designPanel.classList.toggle('active', sub === 'diseno')   // placeholder only — no data to load

  _markUiInteraction()
  _emitUserActivity(sub === 'conceptuales' ? 'concepts' : 'armor', 'navigate', { subtab: sub })

  if (sub === 'conceptuales') {
    _renderConcepts()   // show the current cache immediately, no empty-state flash
    _fetchConcepts()    // then refresh from the backend in case it changed elsewhere
  } else if (isGrid) {
    _renderArmorGrid(sub === 'primarios' ? ARMOR_DATA.primarios : ARMOR_DATA.paralelos)
  }
}

armorSubtabs.forEach(btn => {
  btn.addEventListener('click', () => _switchSubTab(btn.dataset.sub))
})

// ── Armor section reference (always .active inside #section-armor) ──────────
const armorSection = document.getElementById('armorSection')

// _switchView kept for any internal callers; now delegates to switchSection.
function _switchView(view) {
  switchSection(view === 'armor' ? 'armor' : 'chat')
  if (view !== 'armor') _closeDetailView()
}

// ── Conceptuales — backend-persisted list ───────────────────────────────────
// Source of truth is now data/concepts.json via GET/POST /api/concepts
// (core/server.py) instead of this browser's localStorage, so concepts
// survive reinstalls/other devices and are never tied to a single tab.
// localStorage is kept ONLY as a temporary offline fallback (see
// _loadConceptsFallback / _saveConcepts below) for when the backend is
// unreachable — it migrates the old 'jarvis_concepts_v1' key for that case.
const CONCEPT_KEY = 'jarvis_concepts'
;(function _migrateConceptKey() {
  const legacy = localStorage.getItem('jarvis_concepts_v1')
  if (legacy && !localStorage.getItem(CONCEPT_KEY)) {
    localStorage.setItem(CONCEPT_KEY, legacy)
    localStorage.removeItem('jarvis_concepts_v1')
  }
})()

// In-memory cache of the concepts list. Every UI read (_renderConcepts,
// _beginEdit, the save/delete handlers) reads this synchronously; it is kept
// current by _fetchConcepts() (backend → cache) and _saveConcepts() (cache
// updated immediately, then persisted to the backend).
let _conceptsCache = []

// Offline-only fallback — used solely when the backend can't be reached.
function _loadConceptsFallback() {
  try { return JSON.parse(localStorage.getItem(CONCEPT_KEY) || '[]') }
  catch { return [] }
}

// Synchronous accessor for the rest of the Conceptuales UI code.
function _loadConcepts() {
  return _conceptsCache
}

// Ensures every concept has a 'type' — missing/anything-but-'general'
// defaults to 'armor', since existing concepts (saved before this field
// existed) must migrate to 'armor' per spec. Applied once wherever
// concepts enter the cache (below), not on every read, so _conceptsCache
// itself is always the normalized source of truth for the rest of the UI.
function _normalizeConceptTypes(arr) {
  return arr.map(c => (c.type === 'general' ? c : { ...c, type: 'armor' }))
}

// Pull the full list from the backend and refresh the cache + rendered list.
// Called on page load and on every socket (re)connect so this tab reflects
// concepts saved elsewhere. Falls back to localStorage if the backend is
// unreachable, per the "never lose concepts" requirement.
async function _fetchConcepts() {
  try {
    const res = await fetch(`${JARVIS_API}/api/concepts`)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data = await res.json()
    _conceptsCache = _normalizeConceptTypes(data.concepts || [])
  } catch (e) {
    console.warn('[Concepts] Backend unreachable, falling back to localStorage:', e)
    _conceptsCache = _normalizeConceptTypes(_loadConceptsFallback())
  }
  _renderConcepts()
}

// Persist the full list. Updates the cache immediately (so the UI never waits
// on the network) and mirrors to localStorage as an offline fallback copy,
// then POSTs to the backend so data/concepts.json — and LIRA's live memory,
// via core/commands.reload_concepts() — stay in sync. Covers create, edit and
// delete, since all three funnel through this function.
async function _saveConcepts(arr) {
  _conceptsCache = arr
  localStorage.setItem(CONCEPT_KEY, JSON.stringify(arr))   // offline fallback only
  try {
    const res = await fetch(`${JARVIS_API}/api/concepts`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ concepts: arr }),
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
  } catch (e) {
    console.warn('[Concepts] Failed to reach backend — change kept in localStorage fallback only:', e)
  }
}

// Kick off an initial load as soon as the script runs, so the cache is
// populated even before the SocketIO connection (which re-fetches on
// 'connect') finishes resolving the right backend URL.
_fetchConcepts()

// -1 = create mode; N = index of concept currently being edited
let _editIdx = -1
// Index of concept pending deletion (set when ✕ clicked; cleared on confirm/cancel)
let _pendingDeleteIdx = -1

// ── Concept create/edit modal — open/close + title swap ─────────────────────
// Everything below the field-reset logic is new chrome around the same
// #cptName/#cptDesc/#cptStatus/#cptSave/#cptCancel elements the old inline
// form used; the save/edit/delete business logic elsewhere is untouched.
const conceptModalOverlay = document.getElementById('conceptModalOverlay')
const cptModalTitle       = document.getElementById('cptModalTitle')

function _openConceptModal() {
  conceptModalOverlay.classList.add('open')
  document.getElementById('cptName').focus()
  // Baseline for the unsaved-changes check below — taken AFTER the caller
  // (either the "+ Nuevo Concepto" handler, which resets fields to empty
  // via _cancelEdit() first, or _beginEdit(), which fills them from the
  // existing concept) has already set the fields, so this always captures
  // the correct "nothing changed yet" starting point for either mode.
  _captureConceptSnapshot()
}
function _closeConceptModal() {
  conceptModalOverlay.classList.remove('open')
  _cptSavedSnapshot = null
  _emitHudContext({ type: 'idle', section: 'concepts' })
}

// ── Unsaved concept-edit warning ─────────────────────────────────────────
// "Tienes cambios sin guardar en este concepto. ¿Quieres guardar antes de
// salir?" — shown by switchSection() (see its own comment, much earlier in
// this file) when the user tries to navigate to another section while this
// modal has unsaved changes. HUD-styled dialog (#unsavedConceptModal),
// never a browser confirm()/alert().
let _cptSavedSnapshot  = null   // {name, desc, status} as of open (or last save)
let _pendingSectionNav = null   // section switchSection() was trying to reach when intercepted

function _captureConceptSnapshot() {
  _cptSavedSnapshot = {
    name:   document.getElementById('cptName').value,
    desc:   document.getElementById('cptDesc').value,
    status: document.getElementById('cptStatus').value,
  }
}

function _conceptFormHasUnsavedChanges() {
  if (!conceptModalOverlay.classList.contains('open') || !_cptSavedSnapshot) return false
  return document.getElementById('cptName').value   !== _cptSavedSnapshot.name
      || document.getElementById('cptDesc').value   !== _cptSavedSnapshot.desc
      || document.getElementById('cptStatus').value !== _cptSavedSnapshot.status
}

function _showUnsavedConceptDialog(targetSection) {
  _pendingSectionNav = targetSection
  document.getElementById('unsavedConceptModal').classList.add('open')
}
function _hideUnsavedConceptDialog() {
  _pendingSectionNav = null
  document.getElementById('unsavedConceptModal').classList.remove('open')
}

document.getElementById('unsavedConceptSaveBtn').addEventListener('click', () => {
  const target = _pendingSectionNav
  const saved  = _saveConceptForm()   // false (e.g. empty name) ⇒ stays put, dialog + modal both remain open to fix it
  _hideUnsavedConceptDialog()
  if (saved && target) _performSwitchSection(target)
})
document.getElementById('unsavedConceptDiscardBtn').addEventListener('click', () => {
  const target = _pendingSectionNav
  _cancelEdit()
  _closeConceptModal()
  _hideUnsavedConceptDialog()
  if (target) _performSwitchSection(target)
})
document.getElementById('unsavedConceptCancelBtn').addEventListener('click', () => {
  _hideUnsavedConceptDialog()   // stays on the current section — concept modal remains open, changes intact
})
// Backdrop click behaves like Cancelar — stay put, same convention as
// every other confirm dialog in this file (delete-confirm, update-confirm).
document.getElementById('unsavedConceptModal').addEventListener('click', e => {
  if (e.target === document.getElementById('unsavedConceptModal')) _hideUnsavedConceptDialog()
})

// Debounced 'typing' activity — see USER ACTIVITY section above. Fires
// ~700ms after the user pauses, not on every keystroke, and folds
// name+description into first-50-chars-per-field snapshots the same way
// the task's own event shape describes.
let _cptTypingDebounce = null
function _reportConceptTyping(field) {
  clearTimeout(_cptTypingDebounce)
  _cptTypingDebounce = setTimeout(() => {
    const el = document.getElementById(field === 'nombre' ? 'cptName' : 'cptDesc')
    if (!el) return
    _markUiInteraction()
    _emitUserActivity('concepts', 'typing', { field, partial_text: el.value.slice(0, 50) })
  }, 700)
}
document.getElementById('cptName').addEventListener('input', () => _reportConceptTyping('nombre'))
document.getElementById('cptDesc').addEventListener('input', () => _reportConceptTyping('descripcion'))

function _cancelEdit() {
  _editIdx = -1
  document.getElementById('cptName').value = ''
  document.getElementById('cptDesc').value = ''
  document.getElementById('cptStatus').value = 'idea'
}

function _beginEdit(idx) {
  const c = _loadConcepts()[idx]
  if (!c) return
  _editIdx = idx
  document.getElementById('cptName').value = c.name
  document.getElementById('cptDesc').value = c.desc || ''
  document.getElementById('cptStatus').value = c.status || 'idea'
  cptModalTitle.textContent = 'Editar Concepto'
  _emitHudContext({ type: 'concept_detail', concept: { name: c.name, desc: c.desc || '', status: c.status || 'idea', type: c.type } })
  _openConceptModal()
}

// "+ Nuevo Concepto" trigger — always starts from a clean create-mode form
document.getElementById('cptNewBtn').addEventListener('click', () => {
  _cancelEdit()
  cptModalTitle.textContent = 'Nuevo Concepto'
  _openConceptModal()
})
// ✕ in the modal header — same as Cancelar
document.getElementById('cptModalClose').addEventListener('click', () => {
  _cancelEdit()
  _closeConceptModal()
})
// Backdrop click closes without saving (same convention as the delete-confirm modal)
conceptModalOverlay.addEventListener('click', e => {
  if (e.target === conceptModalOverlay) { _cancelEdit(); _closeConceptModal() }
})
// Escape closes without saving, only while the modal is actually open
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && conceptModalOverlay.classList.contains('open')) {
    _cancelEdit()
    _closeConceptModal()
  }
})

function _showDeleteConfirm(idx) {
  _pendingDeleteIdx = idx
  document.getElementById('conceptDeleteConfirm').classList.add('open')
}

function _hideDeleteConfirm() {
  _pendingDeleteIdx = -1
  document.getElementById('conceptDeleteConfirm').classList.remove('open')
}

// Which Conceptuales subsection is showing — 'armor' (default) or
// 'general'. Toggled by #conceptTypeToggle's buttons below; new concepts
// created while a subsection is active are tagged with it (see cptSave's
// click handler).
let _currentConceptType = 'armor'

document.querySelectorAll('.concept-type-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    _currentConceptType = btn.dataset.type
    document.querySelectorAll('.concept-type-btn').forEach(b => b.classList.toggle('active', b === btn))
    _renderConcepts()
  })
})

function _renderConcepts() {
  const list     = document.getElementById('conceptList')
  const concepts = _loadConcepts()
  list.innerHTML = ''

  // Keep each entry's ORIGINAL index into _conceptsCache (not its position
  // in this filtered subset) — _beginEdit/_showDeleteConfirm both index
  // into the full unfiltered cache, so losing that mapping here would edit
  // or delete the wrong concept.
  const visible = concepts
    .map((c, idx) => ({ c, idx }))
    .filter(({ c }) => c.type === _currentConceptType)

  if (!visible.length) {
    list.innerHTML = `<div class="concept-empty">No hay conceptos guardados aún${_currentConceptType === 'general' ? ' en Conceptos Generales' : ''}.</div>`
    return
  }

  visible.forEach(({ c, idx }) => {
    const card = document.createElement('div')
    card.className = 'concept-card'
    const badgeMap = { 'idea': 'badge-no-construido', 'en desarrollo': 'badge-construccion', 'descartado': 'badge-no-completado' }
    const bCls = badgeMap[c.status] || 'badge-no-construido'
    card.innerHTML = `
      <div class="concept-card-header">
        <span class="concept-card-name">${esc(c.name)}</span>
        <span class="armor-badge ${bCls}">${esc(c.status)}</span>
        <!-- Design section placeholder — disabled until that feature exists -->
        <button class="concept-card-design" title="Próximamente" disabled>🎨</button>
        <button class="concept-card-edit" data-idx="${idx}" title="Editar">✎</button>
        <button class="concept-card-del" data-idx="${idx}" title="Eliminar">✕</button>
      </div>
      <div class="concept-card-body">${esc(c.desc)}</div>`
    // Expand/collapse on click (not when clicking edit, delete, or the
    // disabled design placeholder). Expanding counts as "has a concept card
    // open" for hud_context — see PANTALLA ACTUAL in core/commands.py.
    card.addEventListener('click', e => {
      if (e.target.classList.contains('concept-card-del') ||
          e.target.classList.contains('concept-card-edit') ||
          e.target.classList.contains('concept-card-design')) return
      card.classList.toggle('expanded')
      if (card.classList.contains('expanded')) {
        _emitHudContext({ type: 'concept_detail', concept: { name: c.name, desc: c.desc || '', status: c.status || 'idea', type: c.type } })
      } else {
        _emitHudContext({ type: 'idle', section: 'concepts' })
      }
    })
    // Edit button — pre-fill form and switch to edit mode
    card.querySelector('.concept-card-edit').addEventListener('click', e => {
      e.stopPropagation()
      _beginEdit(idx)
    })
    // Delete button — always show confirmation first; never delete on first click
    card.querySelector('.concept-card-del').addEventListener('click', e => {
      e.stopPropagation()
      _showDeleteConfirm(idx)
    })
    list.appendChild(card)
  })
}

// Delete confirmation dialog buttons
document.getElementById('cptDelConfirm').addEventListener('click', () => {
  if (_pendingDeleteIdx < 0) { _hideDeleteConfirm(); return }
  const all = _loadConcepts()
  all.splice(_pendingDeleteIdx, 1)
  _saveConcepts(all)
  // Keep _editIdx consistent after removal
  if (_editIdx === _pendingDeleteIdx) {
    _cancelEdit()
  } else if (_pendingDeleteIdx < _editIdx) {
    _editIdx--
  }
  _hideDeleteConfirm()
  _renderConcepts()
})
document.getElementById('cptDelCancel').addEventListener('click', _hideDeleteConfirm)
// Clicking the backdrop also dismisses the dialog
document.getElementById('conceptDeleteConfirm').addEventListener('click', e => {
  if (e.target === document.getElementById('conceptDeleteConfirm')) _hideDeleteConfirm()
})

// Save / update — handles both create and edit mode, then closes the
// modal. Edit mode overwrites all[_editIdx] in place — it can only ever
// update the concept _beginEdit() opened the modal for, never create a
// duplicate. Extracted as its own function (not just an inline click
// handler) so the unsaved-changes dialog's own "Guardar" button can reuse
// this exact logic — see _showUnsavedConceptDialog above. Returns true on
// an actual save, false if validation blocked it (empty name), so callers
// that navigate away afterward know whether it's actually safe to.
function _saveConceptForm() {
  const name   = document.getElementById('cptName').value.trim()
  const desc   = document.getElementById('cptDesc').value.trim()
  const status = document.getElementById('cptStatus').value
  if (!name) { document.getElementById('cptName').focus(); return false }

  const all = _loadConcepts()
  if (_editIdx >= 0 && _editIdx < all.length) {
    // Edit mode: overwrite in place, preserve original timestamp AND type
    // (the ...spread keeps it — editing a concept never moves it between
    // subsections; that's a create-only decision, tagged below).
    all[_editIdx] = { ...all[_editIdx], name, desc, status }
  } else {
    // Create mode: prepend new concept, tagged with whichever subsection
    // (Armaduras/Conceptos Generales) is active right now.
    all.unshift({ name, desc, status, type: _currentConceptType, ts: Date.now() })
  }
  _saveConcepts(all)
  _cancelEdit()
  _closeConceptModal()
  _renderConcepts()
  return true
}
document.getElementById('cptSave').addEventListener('click', () => { _saveConceptForm() })

// Cancel — resets form to create-new mode without saving, and closes the modal
document.getElementById('cptCancel').addEventListener('click', () => {
  _cancelEdit()
  _closeConceptModal()
})

// ════════════════════════════════════════════════════════════════════════════
// AUTH GATE  — this IIFE is the only entry point into the app.
// Nothing else initialises until the device fingerprint is verified.
// ════════════════════════════════════════════════════════════════════════════
// AUTH GATE TEMPORARILY DISABLED — all devices allowed without fingerprint check.
/*
;(async () => {
  // Step 1: generate a stable device fingerprint from browser characteristics.
  try {
    _deviceFingerprint = await _generateFingerprint()
  } catch (e) {
    // crypto.subtle unavailable (non-secure context?) — use a fallback string
    // so bootstrap mode still works; the server will see an empty fingerprint
    // and allow access only if no devices are registered.
    console.warn('[Auth] Fingerprint generation failed:', e)
    _deviceFingerprint = ''
  }
  // [AUTH] Always log the fingerprint to the console so the user can copy it
  // to /api/register_device when setting up a new device.
  console.log('[Auth] Device fingerprint:', _deviceFingerprint)

  // Step 2: ask the launcher if this device is allowed.
  let allowed   = false
  let bootstrap = false
  try {
    const res  = await fetch(`${LAUNCHER_API}/api/auth?fingerprint=${encodeURIComponent(_deviceFingerprint)}`)
    if (res.ok) {
      const data = await res.json()
      allowed   = data.allowed   === true
      bootstrap = data.bootstrap === true
    } else {
      // Auth endpoint returned an error status — fail-open so a misconfigured
      // server does not permanently lock out the owner.
      console.warn('[Auth] /api/auth returned', res.status, '— failing open')
      allowed = true
    }
  } catch (e) {
    // Launcher unreachable — fail-open (local tool, network errors are benign).
    console.warn('[Auth] /api/auth unreachable — failing open:', e)
    allowed = true
  }

  // Step 3: block unregistered devices before any socket is created.
  if (!allowed) {
    _showRejectionPage()
    return   // all further code in this script is abandoned
  }

  // Step 4: first-time setup notice — no devices registered yet.
  if (bootstrap) {
    console.warn(
      '[Auth] BOOTSTRAP MODE — no devices registered.\n' +
      'Register this device by visiting:\n' +
      `  ${location.origin}/api/register_device?fingerprint=${_deviceFingerprint}&token=YOUR_REGISTER_TOKEN`
    )
  }

  // Step 5: auth passed — create the launcher socket and start the app.
  _initLauncherSocket()
})()
*/
// AUTH DISABLED: skip fingerprint check and start the app directly.
_initLauncherSocket()
