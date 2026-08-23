// connection.js — Health polling and the jarvis backend connect/attempt/disconnect flow.
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
    _loadTtsEngine()
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
    _checkNotifications()
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
  // Chat latency display — see core/server.py's emit_response_timing()
  // and chat-render.js's _applyResponseTiming()/_lastJarvisTimingEl.
  jarvisSocket.on('response_timing', (data) => { if (typeof _applyResponseTiming === 'function') _applyResponseTiming(data) })
  // MOTOR DE VOZ toggle (Ajustes) — broadcast whenever POST /api/set_tts_engine
  // changes it, from any connected tab, so every open HUD stays in sync.
  jarvisSocket.on('tts_engine_state', ({ engine }) => { if (typeof _applyTtsEngineState === 'function') _applyTtsEngineState(engine) })
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

