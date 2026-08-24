// section-nav.js — Response timer, activity/HUD emit helpers, section switching, and mic indicator state.
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
// navigation, going idle) over the existing
// jarvisSocket connection, so HUGO can act as a co-pilot
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
// HUD CONTEXT — precise, full-detail state (exactly which concept is on
// screen right now), separate from USER ACTIVITY above. USER ACTIVITY is a
// lightweight "what's happening" signal for co-pilot commentary; HUD
// CONTEXT carries the full object (concept description) so HUGO can answer
// specific questions about whatever's on screen without asking which one —
// see core/server.py's 'hud_context' socket handler and
// core/commands.py's PANTALLA ACTUAL system-prompt block. Fires on every
// meaningful state change (opening a concept, navigating away), not just
// navigation.
// ════════════════════════════════════════════════════════════════════════════
function _emitHudContext(payload) {
  if (!jarvisSocket || !jarvisSocket.connected) return
  jarvisSocket.emit('hud_context', payload)
}

// Internal nav-section id → the vocabulary HUGO's prompt actually uses.
const _ACTIVITY_SECTION_MAP = { home: 'main', chat: 'chat', maintenance: 'system', settings: 'settings' }

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
// THIS, not _performSwitchSection() directly.
// Nav lockout (ui/js/onboarding-intro.js's _daniLocked/_refreshLockState)
// — while Dani hasn't finished entering his own keys, every section
// except Main and Ajustes redirects here instead of actually switching.
// Checked at this single public entry point so every caller (bottom nav
// clicks, sysPanelClose, any future switchSection() call) is covered
// automatically, no per-call-site guards needed.
function switchSection(name) {
  if (typeof _daniLocked !== 'undefined' && _daniLocked && name !== 'home' && name !== 'settings') {
    name = 'settings'
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

  // ── Diamond identity across sections ──────────────────────────────────
  // #hugoDiamond is the ONLY diamond DOM element that exists — Main's and
  // Chat's own "orb" are now just empty layout placeholders (#mmOrbSlot,
  // #chatOrbSlot; see their own comments in main-menu-orb.css). There is
  // no second element to hide/reveal/swap-time anymore: entering Main or
  // Chat, leaving them, and hopping directly between them are all just
  // "glide to a new {top, left, scale} target" — the exact same call
  // ambient repositioning already uses (_resolveDiamondTarget/
  // _glideDiamondTo below) — because she was already sitting at the
  // PREVIOUS section's real position/scale the whole time (never hidden),
  // so the browser interpolates smoothly from wherever she actually is to
  // wherever she's going next, with no warp-to-source-rect step needed
  // (that used to exist ONLY to fake "where would she have been" for an
  // element that was invisible while docked — moot now that she never is).
  hugoDiamond.classList.toggle('context-main', name === 'home')
  hugoDiamond.classList.toggle('context-chat', name === 'chat')
  hugoDiamond.classList.add('visible')   // permanently visible from her first appearance on — never toggled off again, unlike the old per-section fade
  if (!_diamondEligible()) _closeDiamondBubble()   // Main/Chat have their own dedicated text UI; never leave the bubble open behind on them

  // Reposition whenever entering/leaving Main or Chat (always, regardless
  // of state — matches the old dock/hop/undock behavior, which docked
  // even mid wake/processing/speaking), OR for an ordinary move between
  // two other sections while idle (state-owned position otherwise — see
  // _applyDiamondState's own comment; a section switch mid-turn must
  // never yank her out of wake/processing/speaking's own spot).
  const _enteringDockedSection = (name === 'home' || name === 'chat')
  const _leavingDockedSection  = (_prevSection === 'home' || _prevSection === 'chat') && !_enteringDockedSection
  if (_enteringDockedSection || _leavingDockedSection || _hugoDiamondState === 'idle') {
    const { top, left, scale } = _resolveDiamondTarget(name)
    _glideDiamondTo(top, left, scale)
  }

  // Navigating to maintenance: clear unread badge and scroll log to bottom
  if (name === 'maintenance') {
    _sysCount = 0
    maintCount.textContent = ''
    if (navMaintBadge) navMaintBadge.textContent = ''
    maintLog.scrollTo({ top: maintLog.scrollHeight })
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

  // Main menu floating text — hide immediately when leaving Main, so it
  // doesn't awkwardly reappear stale when coming back.
  if (_prevSection === 'home' && name !== 'home') {
    clearTimeout(_mmFloatingHideTimer)
    mmFloatingText.classList.remove('visible')
  }

  // HUGO CORE — render whichever sub-tab is already active on entry (so
  // switching back to CORE later shows fresh data, not a stale render from
  // last time), and only run Estado's polling fallback while CORE itself
  // is the visible section.
  if (name === 'core') {
    _switchCoreSubTab(_currentCoreSub)
    _startCoreEstadoPoll()
  } else if (_prevSection === 'core') {
    _stopCoreEstadoPoll()
  }

  // ESTUDIO — fetch fresh data every time the section is entered (it's not
  // pushed live via socket events, unlike CORE's Estado tab), and close the
  // expanded detail view on the way out so coming back later always shows
  // the active tab's card list fresh, not a stale detail page.
  if (name === 'estudio') {
    _loadEstudioData()
  } else if (_prevSection === 'estudio') {
    _closeEstudioDetail()
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
// while HUGO speaks about a specific topic (weather, time, ...). Backend
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

