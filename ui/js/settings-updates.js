// settings-updates.js — Test-mode toggle wiring, update/build-ios progress, and sleep confirm/status rendering.
const testModeToggle = document.getElementById('testModeToggle')
testModeToggle.addEventListener('click', () => _toggleFeatureFlag('modo_test', testModeToggle))

// "Actualizar Sistema" — triggers scripts/rebuild_app.sh via launcher.py
// (git pull -> rebuild -> reinstall /Applications/HUGO.app). A real update
// takes a while (npm install + electron-builder), so this fetch is simply
// awaited for as long as it takes; the button stays disabled the whole time.
// Gated behind a HUD-styled confirmation (#updateConfirmModal) — never a
// browser confirm() — since this restarts the whole app. During the
// rebuild, updateHugoStatus reflects REAL progress via 'update_progress'
// socket events from launcher.py's api_update() (see _applyUpdateProgress()
// below), not a fixed sequence of timed messages.
const updateHugoBtn      = document.getElementById('updateHugoBtn')
const updateHugoStatus   = document.getElementById('updateHugoStatus')
const updateConfirmModal = document.getElementById('updateConfirmModal')
const updateConfirmBtn   = document.getElementById('updateConfirmBtn')
const updateCancelBtn    = document.getElementById('updateCancelBtn')

function _showUpdateConfirm() { updateConfirmModal.classList.add('open') }
function _hideUpdateConfirm() { updateConfirmModal.classList.remove('open') }

const UPDATE_PROGRESS_LABELS = {
  downloading: 'Descargando cambios...',
  compiling:   'Compilando...',
  installing:  'Instalando...',
  restarting:  'Reiniciando...',
}
function _applyUpdateProgress(data) {
  if (!data || !data.stage) return
  updateHugoStatus.style.color = 'var(--accent)'
  updateHugoStatus.textContent = data.label || UPDATE_PROGRESS_LABELS[data.stage] || data.stage
}

updateHugoBtn.addEventListener('click', _showUpdateConfirm)
updateCancelBtn.addEventListener('click', _hideUpdateConfirm)
updateConfirmModal.addEventListener('click', e => { if (e.target === updateConfirmModal) _hideUpdateConfirm() })

updateConfirmBtn.addEventListener('click', async () => {
  _hideUpdateConfirm()
  updateHugoBtn.disabled = true
  updateHugoStatus.style.color = 'var(--accent)'
  updateHugoStatus.textContent = UPDATE_PROGRESS_LABELS.downloading
  try {
    const res  = await fetch(`${LAUNCHER_API}/api/update`, { method: 'POST' })
    const data = await res.json()
    if (res.ok && data.ok) {
      updateHugoStatus.style.color = 'var(--green)'
      updateHugoStatus.textContent = UPDATE_PROGRESS_LABELS.restarting
      // Left disabled: electron/main.js's health poll picks up pending_relaunch
      // within a few seconds and relaunches the app on its own — no manual
      // restart needed, so this reads as "in progress" rather than an
      // instruction for the user to act on.
    } else {
      throw new Error(data.error || `HTTP ${res.status}`)
    }
  } catch (e) {
    console.error('[Update] failed:', e)
    updateHugoStatus.style.color = 'var(--red)'
    updateHugoStatus.textContent = 'Error en actualización'
    updateHugoBtn.disabled = false   // allow retry
  }
})

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
    // Formerly #mmOrbWrap — #hugoDiamond is the only diamond element now;
    // the CSS (.hugo-diamond.context-main.waking, diamond-core.css) only
    // actually shows the flash while she's docked onto Main anyway, so
    // firing this unconditionally regardless of current section is
    // harmless, not just off-screen.
    hugoDiamond.classList.add('waking')
    setTimeout(() => hugoDiamond.classList.remove('waking'), 1400)
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
    const reasonLabel = { interaction: 'HUGO despertó', manual_stop: 'detenido manualmente', error: 'error' }[cont.stop_reason] || 'detenido'
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
// CLAVES API — see ui/index.html's #apiKeysPanel comment and
// core/routes_api_keys.py. Per-person scoped server-side (2026-08-24
// rework, core.api_key_store.keys_for): GET only ever returns the CURRENT
// identified person's own keys — Joan's 6 or Dani's 2, never both, never
// the other's — so this renders one flat labeled list rather than the two
// group headers an earlier version of this panel used, since one viewer
// can now never see both groups at once anyway. Labels below cover every
// key either group could return; whichever ones the GET response actually
// contains are the only rows rendered.
const API_KEY_LABELS = {
  GROQ_API_KEY:          'Groq',
  GROQ_API_KEY_2:        'Groq (cuenta 2)',
  SERPER_API_KEY:        'Serper (búsqueda web)',
  DEEPSEEK_API_KEY:      'DeepSeek (code engine)',
  CLOUDFLARE_ACCOUNT_ID: 'Cloudflare (account id)',
  CLOUDFLARE_API_TOKEN:  'Cloudflare (token)',
  GROQ_API_KEY_DANI:     'Groq',
  SERPER_API_KEY_DANI:   'Serper (búsqueda web)',
}
// Fixed display order (GET's own key order isn't guaranteed) — filtered
// down to whichever keys the response actually included.
const API_KEY_ORDER = Object.keys(API_KEY_LABELS)

// Where to actually get each key — Joan's request, so Dani (or Joan
// himself, setting up a fresh key) isn't left guessing which site a
// "Groq" or "Serper" label even refers to. Same provider → same URL
// regardless of which variable it fills (Dani's isolated *_DANI slots
// point at the exact same signup page as the shared ones).
const API_KEY_SIGNUP_URLS = {
  GROQ_API_KEY:          'https://console.groq.com/keys',
  GROQ_API_KEY_2:        'https://console.groq.com/keys',
  GROQ_API_KEY_DANI:     'https://console.groq.com/keys',
  SERPER_API_KEY:        'https://serper.dev',
  SERPER_API_KEY_DANI:   'https://serper.dev',
  DEEPSEEK_API_KEY:      'https://platform.deepseek.com',
  CLOUDFLARE_ACCOUNT_ID: 'https://dash.cloudflare.com',
  CLOUDFLARE_API_TOKEN:  'https://dash.cloudflare.com',
}

async function _renderApiKeys() {
  const panel = document.getElementById('apiKeysPanel')
  if (!panel) return
  let status
  try {
    const res = await fetch(`${JARVIS_API}/api/api_keys`)
    status = await res.json()
  } catch {
    panel.innerHTML = '<div class="api-key-row-note">Jarvis offline</div>'
    return
  }
  const keys = API_KEY_ORDER.filter(k => k in status)
  if (!keys.length) {
    panel.innerHTML = '<div class="api-key-row-note">No hay claves para configurar aquí</div>'
    return
  }
  panel.innerHTML = keys.map(key => {
    const label = API_KEY_LABELS[key]
    const isSet = !!status[key]
    const signupUrl = API_KEY_SIGNUP_URLS[key]
    return `
      <div class="api-key-row" data-key="${key}">
        <div class="api-key-row-head">
          <span class="api-key-label">${esc(label)}</span>
          ${signupUrl ? `<a class="api-key-signup-link" href="${signupUrl}" target="_blank" rel="noopener noreferrer">Obtener clave ↗</a>` : ''}
          <span class="api-key-state ${isSet ? 'set' : 'unset'}">${isSet ? '● Configurada' : '○ Vacía'}</span>
        </div>
        <div class="api-key-row-controls">
          <input type="password" class="api-key-input" placeholder="${isSet ? '•••••••••••• (pegar para reemplazar)' : 'Pegar clave…'}" autocomplete="off" spellcheck="false">
          <button class="api-key-save-btn">Guardar</button>
          <button class="api-key-clear-btn" ${isSet ? '' : 'disabled'} title="Borrar">✕</button>
        </div>
        <div class="api-key-row-error" data-role="error"></div>
      </div>
    `
  }).join('')

  panel.querySelectorAll('.api-key-row').forEach(row => {
    const key      = row.dataset.key
    const input    = row.querySelector('.api-key-input')
    const saveBtn  = row.querySelector('.api-key-save-btn')
    const clearBtn = row.querySelector('.api-key-clear-btn')
    const errorEl  = row.querySelector('[data-role="error"]')
    // A non-empty value gets a REAL test call against that provider
    // server-side (core.api_key_validation) before it's ever persisted —
    // a bad/rejected key surfaces its specific reason here instead of
    // silently no-oping, so Dani isn't left guessing why chat still won't
    // work after "saving" a typo'd key.
    const save = async (value) => {
      saveBtn.disabled = true
      clearBtn.disabled = true
      errorEl.textContent = ''
      try {
        const res  = await fetch(`${JARVIS_API}/api/api_keys`, {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ key, value }),
        })
        const data = await res.json()
        if (res.ok && data.ok) {
          _renderApiKeys()   // re-render from the new status — never leaves the pasted value sitting in the input
          // Re-check nav lockout (ui/js/onboarding-intro.js) — this save
          // may have just been the LAST key Dani needed, which should
          // trigger the one-time "unlocked" sequence and open up full nav.
          if (typeof _refreshLockState === 'function') _refreshLockState()
        } else {
          errorEl.textContent = data.error || 'No se pudo guardar la clave.'
        }
      } catch {
        errorEl.textContent = 'Jarvis no responde — inténtalo de nuevo.'
      } finally {
        saveBtn.disabled = false
        clearBtn.disabled = false
      }
    }
    saveBtn.addEventListener('click', () => { if (input.value.trim()) save(input.value) })
    input.addEventListener('keydown', e => { if (e.key === 'Enter' && input.value.trim()) save(input.value) })
    clearBtn.addEventListener('click', () => save(''))
  })
}

// PAGE LOAD init (clock/personality/boot-splash) moved to
// clock-boot-splash-wiring.js — see its own comment for why: it was a bare
// hoisted-function-declaration call in the original single-file app.js,
// which only worked because same-script hoisting made the call order-
// independent of the definition. Splitting the file broke that (this
// script loads before the one defining _updateMMClock/_playBootSplash), so
// the init calls had to move to load after their definitions instead.

