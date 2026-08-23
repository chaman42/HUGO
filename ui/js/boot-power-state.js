// boot-power-state.js — Boot message/state, showMainUI, and applying power/mute/tts-mute/mode state to the UI.
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
// The mic itself is never touched: HUGO keeps listening and replying in
// chat, she just stops speaking.
function applyTtsMuteState(muted) {
  _isTtsMuted = muted
  if (muted) {
    ttsMuteBtn.textContent = '🔈'
    ttsMuteBtn.title       = "Unmute HUGO's voice"
    ttsMuteBtn.classList.add('muted')
  } else {
    ttsMuteBtn.textContent = '🔊'
    ttsMuteBtn.title       = "Mute HUGO's voice"
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

