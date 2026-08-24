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

// Fetches one fixed onboarding line's audio id (synthesized/cached
// server-side, GET /api/onboarding/audio/<key> — core/routes_onboarding.py),
// reveals its text in sync via the same word-by-word _organicReveal()
// every HUGO spoken reply already uses (ui/js/diamond-motion.js), and
// resolves once playback actually ends. Falls back to a fixed hold if the
// audio itself couldn't be fetched/played, so a TTS hiccup degrades to
// "text appears, then a short pause" rather than skipping the line.
async function _playOnboardingLine(lineKey, textEl) {
  let audioId = null, text = null
  try {
    const data = await fetch(`${JARVIS_API}/api/onboarding/audio/${lineKey}`).then(r => r.json())
    audioId = data.audio_id
    text    = data.text
  } catch (e) {
    console.error('[Onboarding] audio fetch failed:', e)
  }
  if (text) _organicReveal(textEl, text)
  if (!audioId) {
    await new Promise(resolve => setTimeout(resolve, 1800))
    return
  }
  const audio = new Audio(`${JARVIS_API}/api/tts_audio/${audioId}`)
  await new Promise(resolve => {
    audio.onended = resolve
    audio.onerror = resolve
    audio.play().catch(resolve)
  })
}

async function _runOnboardingSequence() {
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
  await _playOnboardingLine('intro', lineEl)

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
  await _playOnboardingLine('purpose', lineEl)

  // Beat 7: the bottom nav bar appears (see #bottomNav.onboarding-nav-peek
  // in boot-splash.css for why this needs a temporary z-index lift above
  // the still-opaque #bootSplash overlay).
  if (bottomNav) {
    bottomNav.classList.add('onboarding-nav-peek')
    requestAnimationFrame(() => bottomNav.classList.add('visible'))
  }
  await new Promise(resolve => setTimeout(resolve, 600))

  // Beat 8: third line — the API-keys directive to Ajustes.
  await _playOnboardingLine('keys', lineEl)

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
    const status = await fetch(`${JARVIS_API}/api/onboarding/status`).then(r => r.json())
    if (status.seen || status.person_id !== 'dani') return false
    await _runOnboardingSequence()
    fetch(`${JARVIS_API}/api/onboarding/seen`, { method: 'POST' }).catch(() => {})
    return true
  } catch (e) {
    console.error('[Onboarding] sequence failed:', e)
    return false
  }
}
