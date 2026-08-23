// clock-boot-splash-wiring.js — Main-menu clock, boot splash playback/progress, system strip, session timer, and main-menu action wiring.
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
// Terminal-line REVEAL timing is fixed/self-timed (so pacing/drama never
// depends on network or backend speed), but each line's CONTENT is real —
// BOOT_STAGES mirrors the exact 'boot_progress' stages core/process_manager.py
// and jarvis.py emit, in order, and _applyBootProgress() (below) flips a
// line from pending to done live the instant its real event actually
// arrives, whenever that happens to be relative to the reveal. Never reads
// or sets anything on #bootOverlay (setBootState/setBootMsg keep driving
// that independently, for exactly as long as the real backend takes).
// Always LIRA's gold diamond regardless of the active personality — see
// the HTML comment above #bootSplash for the full design rationale.
// ════════════════════════════════════════════════════════════════════════════

// Mirrors core/process_manager.py's emit_boot_progress() call sites plus
// jarvis.py's own two — keep in sync if a stage is ever added/renamed there.
const BOOT_STAGES = [
  { stage: 'connecting_launcher', percent: 10,  label: 'Conectando con el launcher' },
  { stage: 'launcher_responded',  percent: 25,  label: 'Launcher activo' },
  { stage: 'jarvis_starting',     percent: 40,  label: 'Iniciando núcleo de IA' },
  { stage: 'vosk_loading',        percent: 55,  label: 'Cargando modelos de voz' },
  { stage: 'kokoro_prewarm',      percent: 70,  label: 'Precalentando síntesis de voz' },
  { stage: 'socket_connected',    percent: 85,  label: 'Sincronizando' },
  { stage: 'jarvis_ready',        percent: 100, label: 'Sistemas en línea' },
]

function _playBootSplash() {
  const splash   = document.getElementById('bootSplash')
  const grid     = document.getElementById('bootSplashGrid')
  const terminal = document.getElementById('bootTerminal')
  const orb      = document.getElementById('bootSplashOrb')
  const nameEl   = document.getElementById('bootSplashName')
  const statusEl = document.getElementById('bootSplashStatus')
  const retryBtn = document.getElementById('bootSplashRetry')
  if (!splash || !grid || !terminal || !orb || !nameEl || !statusEl || !retryBtn) return

  const LINE_START = 250   // ms before the first line appears
  const LINE_GAP   = 170   // ms between each subsequent line — tighter than the old 5-line cadence to fit 7 in about the same window

  // Beat 1: dark screen, then the grid fades in
  requestAnimationFrame(() => grid.classList.add('visible'))

  // Beat 2/3: the real stage checklist types in. Whatever's genuinely
  // already complete by the moment a given line is revealed (e.g. a fast
  // launcher.py handshake finishing before the animation even gets there)
  // renders as done immediately, no fake catch-up flash — the flash is
  // reserved for a stage completing live, in _applyBootProgress().
  BOOT_STAGES.forEach((s, i) => {
    setTimeout(() => {
      const el = document.createElement('div')
      el.className = 'boot-terminal-line'
      el.dataset.percent = String(s.percent)
      const already = _bootProgressPercent >= s.percent
      if (already) el.classList.add('done')
      el.innerHTML = `<span class="boot-terminal-glyph">${already ? '✓' : '○'}</span><span class="boot-terminal-text">${s.label}</span>`
      terminal.appendChild(el)
      requestAnimationFrame(() => el.classList.add('visible'))
      _refreshActiveLine()
    }, LINE_START + i * LINE_GAP)
  })

  // Beat 4: orb reveal (scale-up + outline draw + glow burst + light sweep)
  // — the terminal does NOT fade out here anymore (bug fix: it used to,
  // which meant the checklist went invisible ~2s in and every live
  // checkmark for the rest of the ~15-20s real boot happened on a
  // display: none/opacity: 0 element nobody could see — the checklist
  // existed but was functionally pointless). It settles into a smaller,
  // dimmer supporting role instead and keeps checking off stages live for
  // the entire wait — see .boot-terminal.settled in boot-splash.css. Only
  // truly hides at the very end, when #bootSplash itself fades as a whole
  // (see finishAndFade() below).
  const linesDoneAt = LINE_START + BOOT_STAGES.length * LINE_GAP
  const revealAt     = linesDoneAt + 400
  setTimeout(() => {
    terminal.classList.add('settled')
    orb.classList.add('reveal')
  }, revealAt)

  // Beat 5: "L I R A" fades in below the now-pulsing orb, then the splash
  // holds — it only releases once the real backend state resolves (see
  // _enterBootSplashWait below), not on a fixed timer.
  //
  // Bug fix: this used to fire at revealAt+400, well BEFORE the diamond's
  // own reveal choreography (draw-in -> ring solidify -> light sweep,
  // ~1350ms total — see the timing comment on .boot-splash-draw in
  // boot-splash.css) had actually finished, so the name/status/wait-phase
  // kicked in while the diamond was still visibly mid-animation — the
  // "mixed, not properly timed" look. 1400ms gives the choreography a
  // clean ~50ms to fully settle first.
  const nameAt = revealAt + 1400
  setTimeout(() => nameEl.classList.add('visible'), nameAt)
  setTimeout(() => _enterBootSplashWait(splash, nameEl, orb, statusEl, retryBtn), nameAt + 300)
}

// Whichever line is the first not-yet-done one gets the blinking cursor
// (.active) — the "current line" concept now tracks real progress instead
// of reveal order. Cheap: only ever a handful of DOM nodes.
function _refreshActiveLine() {
  const terminal = document.getElementById('bootTerminal')
  if (!terminal) return
  const lines = terminal.querySelectorAll('.boot-terminal-line')
  let activeSet = false
  lines.forEach(line => {
    const isActive = !activeSet && !line.classList.contains('done')
    line.classList.toggle('active', isActive)
    if (isActive) activeSet = true
  })
}

// ════════════════════════════════════════════════════════════════════════════
// PAGE LOAD — clock first, then personality init
//
// Moved here from settings-updates.js (pure refactor bug fix, no intended
// behavior change from the original single-file app.js): that script loads
// BEFORE this one, and _updateMMClock/_playBootSplash are only defined in
// this file — a bare call to either from settings-updates.js throws
// ReferenceError (uncaught, since only the _playBootSplash call was
// wrapped), which silently aborted the rest of that script's top-level
// execution, including the _playBootSplash() call itself. In the original
// single-file app.js this was safe because function declarations are
// hoisted within one script regardless of source order — splitting into
// separate <script src> files removed that guarantee, so these calls must
// now physically load after the functions they call.
// ════════════════════════════════════════════════════════════════════════════

// [CHANGE 16] Start the clock BEFORE any other init that could throw, so the
// time display is always live on page load regardless of backend connection state.
_updateMMClock()
setInterval(_updateMMClock, 1000)

applyPersonality('lira')  // set initial state immediately (default personality)

// Boot splash is purely decorative and must never risk taking down anything
// after it — wrapped so a failure here can't halt the rest of this script's
// top-level execution (a real, previously-hit failure mode in this file).
try { _playBootSplash() } catch (e) { console.error('[BootSplash] failed:', e) }

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
  const fill    = document.getElementById('bootRealProgressFill')
  const label   = document.getElementById('bootRealProgressLabel')
  const percent = document.getElementById('bootRealProgressPercent')
  if (fill)    fill.style.width = '0%'
  if (label)   label.textContent = ''
  if (percent) percent.textContent = ''

  // Reset the terminal checklist too — matters on the plain-browser-tab
  // retry path (no page reload there, see #bootSplashRetry's own handler),
  // where this same DOM otherwise keeps its old checkmarks from the failed
  // attempt. Electron's own retry reloads the whole page, which already
  // gives a fresh checklist for free, but running this unconditionally is
  // harmless either way.
  const terminal = document.getElementById('bootTerminal')
  if (terminal && terminal.children.length) {
    terminal.querySelectorAll('.boot-terminal-line').forEach(line => {
      line.classList.remove('done', 'just-completed', 'active')
      const glyph = line.querySelector('.boot-terminal-glyph')
      if (glyph) glyph.textContent = '○'
    })
    _refreshActiveLine()
  }
}
function _applyBootProgress(data) {
  if (!data || typeof data.percent !== 'number') return
  if (data.percent < _bootProgressPercent) return
  _bootProgressPercent = data.percent
  const fill    = document.getElementById('bootRealProgressFill')
  const label   = document.getElementById('bootRealProgressLabel')
  const percent = document.getElementById('bootRealProgressPercent')
  if (fill)    fill.style.width = `${_bootProgressPercent}%`
  if (label && data.label) label.textContent = data.label
  if (percent) percent.textContent = `${_bootProgressPercent}%`

  // Mirror the same real progress onto the terminal checklist (see
  // BOOT_STAGES/_refreshActiveLine above) — any line whose stage threshold
  // this now clears gets checked off, with a one-time flash on whichever
  // line(s) actually completed on THIS event (possibly more than one, if a
  // fast boot fires two stages between animation frames).
  const terminal = document.getElementById('bootTerminal')
  if (terminal) {
    terminal.querySelectorAll('.boot-terminal-line:not(.done)').forEach(line => {
      const threshold = Number(line.dataset.percent)
      if (Number.isFinite(threshold) && threshold <= _bootProgressPercent) {
        line.classList.add('done', 'just-completed')
        const glyph = line.querySelector('.boot-terminal-glyph')
        if (glyph) glyph.textContent = '✓'
        setTimeout(() => line.classList.remove('just-completed'), 500)   // matches boot-check-flash's duration
      }
    })
    _refreshActiveLine()
  }
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
  const overlay  = document.getElementById('bootOverlay')
  const terminal = document.getElementById('bootTerminal')

  // The checklist's whole job is to make the FIRST few seconds of the wait
  // feel real (watch a couple of real stages actually check themselves
  // off) — it's not meant to sit on screen for the entire ~15-20s boot,
  // that's what made it feel cluttered (bug fix: it used to either vanish
  // too early with checkmarks nobody could see, or never leave at all and
  // just look busy — see this function's own history). So it gets a
  // bounded window here, then fades out for good and the diamond +
  // .boot-real-progress bar/percent (already minimal) carry the rest of
  // the wait alone. Stored so a retry can cancel and reschedule instead of
  // firing against a checklist that's already been reset.
  const TERMINAL_VISIBLE_MS = 1200
  let terminalFadeTimer = null

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

    if (terminal) {
      clearTimeout(terminalFadeTimer)
      terminal.classList.remove('fading')
      terminalFadeTimer = setTimeout(() => terminal.classList.add('fading'), TERMINAL_VISIBLE_MS)
    }
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
    clearTimeout(terminalFadeTimer)
    // Let the border trace complete its current loop before fading, instead
    // of cutting it off mid-lap.
    setTimeout(() => {
      orb.classList.remove('tracing')
      statusEl.classList.remove('pulsing')
      statusEl.classList.add('fading')
      nameEl.classList.add('fading')
      if (terminal) terminal.classList.add('fading')
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
// so it reads as a deliberate rotation.
setInterval(() => {
  const quoteEl = document.getElementById('mmQuote')
  if (!quoteEl) return
  const quotes = _LIRA_QUOTES
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
