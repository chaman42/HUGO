// onboarding-intro.js — Dani's one-time first-launch onboarding sequence.
// Gated behind GET /api/onboarding/status (core/routes_onboarding.py):
// only ever runs when the CURRENT identified person is 'dani' and he
// hasn't seen it before. Reuses #bootSplash/#bootSplashOrb/#bootSplashName
// (ui/index.html, already black background + the diamond) as its own
// stage rather than a second overlay — see onboarding-line/-clock's own
// HTML comment. Loaded BEFORE clock-boot-splash-wiring.js (script order in
// index.html) so _maybeRunOnboarding is defined by the time that file's
// own top-level init calls it; loaded AFTER diamond-motion.js so
// _organicReveal is available.

// Real incident (2026-08-24, found live-testing): JARVIS_API starts out
// pointing at BACKEND_URLS[0] (the Tailscale candidate) and only gets
// corrected to a working address AFTER connection.js's socket 'connect_error'
// handler advances it — but that correction happens on ITS OWN timing
// (~5s socket timeout, then a fresh attempt every 3s), and there's no way
// to know in advance whether it's already run by the time onboarding needs
// an answer. A bare `fetch()` against a bad candidate, or even a single
// retry loop timed to guess when the correction lands, either hangs
// forever or is racy — and since _maybeRunOnboarding() is awaited BEFORE
// _playBootSplash() even starts (see clock-boot-splash-wiring.js's own
// gate), a hang here used to block the ENTIRE app from ever booting for
// ANYONE, not just skip onboarding for Dani.
//
// Fix: don't depend on JARVIS_API/connection.js's retry state AT ALL for
// the initial status check — race every BACKEND_URLS candidate (see
// bootstrap-auth.js) in parallel ourselves and take whichever answers
// first. _resolveBackendBase() below does this once; every subsequent
// onboarding call in this file reuses that SAME resolved base rather than
// re-reading the (possibly still-uncorrected) JARVIS_API.
function _fetchWithTimeout(url, options, timeoutMs = 4000) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  return fetch(url, { ...options, signal: controller.signal }).finally(() => clearTimeout(timer))
}

async function _resolveBackendBase() {
  const attempts = BACKEND_URLS.map(base =>
    _fetchWithTimeout(`${base}/api/onboarding/status`, {}, 4000).then(res => {
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.json().then(data => ({ base, data }))
    })
  )
  try {
    return await Promise.any(attempts)
  } catch (e) {
    return null   // every candidate failed — genuinely unreachable right now
  }
}

// Short generated tone for the diamond's reveal beat — no existing sound-
// asset convention anywhere in this codebase (every other effect here is
// CSS/JS-only), so a tiny Web Audio oscillator sweep avoids adding a new
// binary asset entirely. Best-effort: a blocked/unsupported AudioContext
// (autoplay policy, etc.) just means silence, never breaks the sequence.
function _playOnboardingTone() {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext
    const ctx = new Ctx()
    const osc  = ctx.createOscillator()
    const gain = ctx.createGain()
    osc.type = 'sine'
    osc.frequency.setValueAtTime(220, ctx.currentTime)
    osc.frequency.exponentialRampToValueAtTime(660, ctx.currentTime + 0.25)
    gain.gain.setValueAtTime(0.0001, ctx.currentTime)
    gain.gain.exponentialRampToValueAtTime(0.15, ctx.currentTime + 0.05)
    gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.3)
    osc.connect(gain).connect(ctx.destination)
    osc.start()
    osc.stop(ctx.currentTime + 0.32)
  } catch (e) { /* silence is an acceptable degrade */ }
}

// Exact wording spoken/typed during the sequence — MUST match
// core/routes_onboarding.py's _ONBOARDING_LINES exactly (kept as a small
// duplicated constant rather than an extra round trip just to fetch text,
// since these 3 lines are fixed and rarely change).
const _ONBOARDING_LINE_TEXT = {
  intro: (
    'Hola, soy tu herramienta universal de gestión de olvidos, ' +
    'o puedes llamarme HUGO.'
  ),
  purpose: (
    'Estoy diseñado para asistirte en una amplia gama de campos y trabajos, ' +
    'ya sea haciendo resúmenes, esquemas, investigaciones, ' +
    'o recordándote que no seas un vago.'
  ),
  keys: (
    'Sin embargo, antes de activar mis funciones totalmente, necesito que ' +
    'pongas las llaves de API en Ajustes. Tendrás que entrar a internet y ' +
    'crearte una cuenta gratuita para obtener esas claves API. Una vez las ' +
    'tengas, podrás acceder a mis plenas capacidades.'
  ),
  unlocked: (
    'Perfecto, ya tienes tus claves configuradas. A partir de ahora tienes ' +
    'acceso a todas mis funciones — resúmenes, esquemas, investigaciones, ' +
    'recordatorios, y todo lo demás. Adelante.'
  ),
}

// Reveals `lineKey`'s text immediately (word-by-word, via the same
// _organicReveal() every HUGO spoken reply already uses —
// ui/js/diamond-motion.js) and, in parallel, asks the SERVER to actually
// speak it (POST /api/onboarding/speak/<key> — core/routes_onboarding.py,
// blocks until afplay finishes; see that module's own docstring for why
// this ISN'T a browser <audio> element: Chrome/Electron's autoplay policy
// blocks audio.play() with zero prior user interaction, exactly the
// situation on a fresh boot — a real bug found live-testing this). Falls
// back to a fixed hold if the speak call itself fails, so a TTS hiccup
// degrades to "text appears, then a short pause" rather than skipping the
// line entirely.
async function _playOnboardingLine(base, lineKey, textEl) {
  _organicReveal(textEl, _ONBOARDING_LINE_TEXT[lineKey] || '')
  try {
    await _fetchWithTimeout(`${base}/api/onboarding/speak/${lineKey}`, { method: 'POST' }, 30000)
  } catch (e) {
    console.error('[Onboarding] speak failed:', e)
    await new Promise(resolve => setTimeout(resolve, 1800))
  }
}

async function _runOnboardingSequence(base) {
  const grid      = document.getElementById('bootSplashGrid')
  const orb       = document.getElementById('bootSplashOrb')
  const nameEl    = document.getElementById('bootSplashName')
  const lineEl    = document.getElementById('onboardingLine')
  const clockEl   = document.getElementById('onboardingClock')
  const bottomNav = document.getElementById('bottomNav')
  if (!orb || !nameEl || !lineEl || !clockEl) return

  // Beat 1: black screen (already the default), grid fades in — same
  // ambient depth the normal boot splash opens with.
  requestAnimationFrame(() => { if (grid) grid.classList.add('visible') })
  await new Promise(resolve => setTimeout(resolve, 500))

  // Beat 2: the diamond appears — same draw-in/ring/glow-burst/light-sweep
  // choreography _playBootSplash() itself uses (.reveal, boot-splash.css),
  // plus the generated tone. 1400ms matches that choreography's own
  // settle time (see _playBootSplash()'s nameAt comment) before anything
  // else starts.
  orb.classList.add('reveal')
  _playOnboardingTone()
  await new Promise(resolve => setTimeout(resolve, 1400))

  // Beat 3: "Hola, soy... HUGO" — spoken + typed together.
  lineEl.classList.add('visible')
  await _playOnboardingLine(base, 'intro', lineEl)

  // Beat 4: the HUGO name reveals right as/after she says it.
  nameEl.classList.add('visible')
  await new Promise(resolve => setTimeout(resolve, 500))

  // Beat 5: the clock appears.
  clockEl.textContent = new Date().toLocaleTimeString('es-ES', {
    hour: '2-digit', minute: '2-digit', hour12: false,
  })
  clockEl.classList.add('visible')
  await new Promise(resolve => setTimeout(resolve, 600))

  // Beat 6: second line — what HUGO's for.
  await _playOnboardingLine(base, 'purpose', lineEl)

  // Beat 7: the bottom nav bar appears (see #bottomNav.onboarding-nav-peek
  // in boot-splash.css for why this needs a temporary z-index lift above
  // the still-opaque #bootSplash overlay).
  if (bottomNav) {
    bottomNav.classList.add('onboarding-nav-peek')
    requestAnimationFrame(() => bottomNav.classList.add('visible'))
  }
  await new Promise(resolve => setTimeout(resolve, 600))

  // Beat 8: third line — the API-keys directive to Ajustes.
  await _playOnboardingLine(base, 'keys', lineEl)

  // Clean up the peek override and this sequence's own text/clock —
  // #bootSplash is still fully opaque at this point (its own fade-out
  // hasn't started), so this happens invisibly; #bottomNav's normal,
  // non-peek flow position underneath is unaffected either way.
  if (bottomNav) bottomNav.classList.remove('onboarding-nav-peek', 'visible')
  lineEl.classList.remove('visible')
  clockEl.classList.remove('visible')
}

// Called once from clock-boot-splash-wiring.js's own top-level init,
// BEFORE _playBootSplash() — resolves to whether the sequence actually
// ran, so that call site can either hand off straight to the real
// backend-connection wait (skipping a redundant second orb-reveal/
// terminal-checklist run) or fall through to the normal boot splash.
async function _maybeRunOnboarding() {
  try {
    const resolved = await _resolveBackendBase()
    if (!resolved) return false   // every BACKEND_URLS candidate unreachable right now — let the normal boot splash run
    _onboardingBackendBase = resolved.base   // reused by _refreshLockState/_runUnlockSequence below — no need to re-race BACKEND_URLS once we have a working one
    const status = resolved.data
    if (status.seen || status.person_id !== 'dani') return false
    await _runOnboardingSequence(resolved.base)
    _fetchWithTimeout(`${resolved.base}/api/onboarding/seen`, { method: 'POST' }).catch(() => {})
    return true
  } catch (e) {
    console.error('[Onboarding] sequence failed:', e)
    return false
  }
}

// ════════════════════════════════════════════════════════════════════════════
// NAV LOCKOUT (2026-08-24) — Dani gets Main+Ajustes only, view-only on
// Main (no chat/voice input works anywhere — see ui/js/chat-render.js's
// sendTextCommand() guard and ui/js/section-nav.js's switchSection()
// redirect), until BOTH his keys are set and validated
// (core.api_key_store.is_person_locked, polled via
// GET /api/api_keys/lock_status). This is INDEPENDENT of the one-time
// onboarding_seen flag above — it re-applies on every single load until
// he's actually done, not just during the first session.
// ════════════════════════════════════════════════════════════════════════════
let _onboardingBackendBase = null   // set by _maybeRunOnboarding() once resolved; _refreshLockState falls back to JARVIS_API if that never ran (e.g. Joan's own session)
let _daniLocked = false

// Fetches current lock state and applies it (body class + nav visuals via
// CSS, see boot-splash.css's/controls-bar.css's own .dani-locked rules).
// If this is a TRUE -> false transition (Dani just finished entering his
// keys), plays the one-time "unlocked" sequence before settling into the
// now-fully-open nav. Called once after boot resolves, and again by
// ui/js/settings-updates.js after every successful Ajustes key save.
async function _refreshLockState() {
  const base = _onboardingBackendBase || (typeof JARVIS_API !== 'undefined' ? JARVIS_API : null)
  if (!base) return
  let data
  try {
    data = await _fetchWithTimeout(`${base}/api/api_keys/lock_status`, {}, 4000).then(r => r.json())
  } catch (e) {
    return   // leave the previous known state as-is rather than guessing on a network hiccup
  }
  const wasLocked = _daniLocked
  _daniLocked = !!data.locked
  document.body.classList.toggle('dani-locked', _daniLocked)
  if (wasLocked && !_daniLocked) {
    await _runUnlockSequence(base)
  }
}

// Second sequence — plays exactly once, the moment lock status flips from
// true to false. Reuses the SAME #bootSplash stage as the first-launch
// sequence (see _runOnboardingSequence's own comments for why: it's
// already the black-background + diamond stage, no need for a second
// overlay), but shorter — one line, no name/clock beats (those already
// happened once; repeating them here would feel like a second first
// impression rather than "you've now unlocked the rest of me").
async function _runUnlockSequence(base) {
  const splash = document.getElementById('bootSplash')
  const orb    = document.getElementById('bootSplashOrb')
  const lineEl = document.getElementById('onboardingLine')
  if (!splash || !orb || !lineEl) return

  splash.classList.remove('fading', 'gone')
  splash.style.removeProperty('display')
  splash.style.opacity = '1'
  orb.classList.add('reveal')
  _playOnboardingTone()
  lineEl.classList.add('visible')
  await new Promise(resolve => setTimeout(resolve, 600))

  await _playOnboardingLine(base, 'unlocked', lineEl)

  await new Promise(resolve => setTimeout(resolve, 800))
  splash.classList.add('fading')
  lineEl.classList.remove('visible')
  await new Promise(resolve => setTimeout(resolve, 1000))
  splash.style.display = 'none'
}
