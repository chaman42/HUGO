// personality-switch.js — LIRA is now the only personality (JARVIS/FRIDAY
// removed, along with every switcher UI). This file keeps its name/existence
// since applyPersonality() is still a real function called from 3 places
// (initial boot in clock-boot-splash-wiring.js, a personality_change socket
// sync in connection.js, and status-diamond-grid.js's backend view) and
// still owns quote rotation + CSS var application — it's not JUST switching
// machinery, so it didn't collapse to nothing. What DID collapse (theme
// lookup, orb shape toggling, button/modal sync) is gone: the orb is always
// diamond-shaped/gold now — CSS already defaults to that shape (see
// .orb-shape/.mm-orb-shape's own base rules), so this file no longer needs
// to set clip-path/border-radius via JS at all.

// LIRA's own quote pool — direct, slightly sardonic, warm underneath. Also
// the sequential rotation pool for _rotateMMQuote() (45s cycle) below; kept
// in this exact spec order rather than randomized so "cycling" reads as
// intentional, not repetitive-random.
const _LIRA_QUOTES = [
  'Sin novedades. Por ahora.',
  'Sistemas en orden. Tú decides qué hacemos.',
  'Aquí. Como siempre.',
  'Todo bajo control. Más o menos.',
  'Escuchando. No siempre es fácil.',
  'Lista cuando quieras.',
  'Nada que reportar. Aún.',
  'Operative. Aunque nadie lo pidió.',
]

const LIRA_ACCENT = '#f0c040'

function _hexToRgba(hex, alpha) {
  const r = parseInt(hex.slice(1, 3), 16)
  const g = parseInt(hex.slice(3, 5), 16)
  const b = parseInt(hex.slice(5, 7), 16)
  return `rgba(${r},${g},${b},${alpha})`
}

function applyPersonality(name, displayName) {
  console.log('[Jarvis] applyPersonality:', name, displayName)
  // `name` is accepted purely for call-site compatibility with the 3
  // callers above — currentPersonality is now a fixed constant (see
  // bootstrap-auth.js), there is only ever one personality, so `name`
  // isn't looked up against anything.

  // 1. Update PERSONALITY CSS vars — shell vars (--accent and family) stay fixed.
  const a = LIRA_ACCENT
  document.documentElement.style.setProperty('--p-color',  a)
  document.documentElement.style.setProperty('--p-mid',    _hexToRgba(a, 0.35))
  document.documentElement.style.setProperty('--p-dim',    _hexToRgba(a, 0.12))
  document.documentElement.style.setProperty('--p-glow',   _hexToRgba(a, 0.55))
  document.documentElement.style.setProperty('--p-a02',    _hexToRgba(a, 0.20))
  document.documentElement.style.setProperty('--p-a04',    _hexToRgba(a, 0.40))
  document.documentElement.style.setProperty('--p-a015',   _hexToRgba(a, 0.15))
  document.documentElement.style.setProperty('--p-a035',   _hexToRgba(a, 0.35))
  document.documentElement.style.setProperty('--p-a018',   _hexToRgba(a, 0.18))
  document.documentElement.getBoundingClientRect() // force repaint

  // 2. Apply color to every individual colored element that ISN'T already
  // driven by var(--p-color) in its own stylesheet. The old diamond-orb
  // decorations (ring/glow/core/spinner/gems/bars) used to need manual
  // repainting here because #chatOrbWrap/#mmOrbWrap's rules referenced
  // var(--p-color) too, but two DIFFERENT elements... actually no — they
  // already used var(--p-color) directly in CSS, so this was always
  // redundant even before the merge; removed along with those elements
  // now that #liraDiamond (diamond-toggles.css/diamond-core.css) is the
  // only diamond and was never repainted this way to begin with (she's
  // long been "always gold" by design, independent of --p-color).
  const micDotEl         = document.getElementById('micDot')
  const partialEl        = document.getElementById('partialTranscript')

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

  // 3. Update title text and show flash (only when displayName provided)
  if (displayName) {
    titleEl.textContent = displayName
    showPersonalityFlash(displayName)
  }

  // 4. Update persistent bar personality indicator (dot color + name text).
  const _pIndDot  = document.querySelector('#personalityIndicator .p-indicator-dot')
  const _pIndName = document.querySelector('#personalityIndicator .p-indicator-name')
  if (_pIndDot) {
    _pIndDot.style.background = a
    _pIndDot.style.boxShadow  = `0 0 6px ${_hexToRgba(a, 0.55)}`
  }
  if (_pIndName) _pIndName.textContent = 'L I R A'

  // 5. Update Main Menu name label (the orb itself is #liraDiamond now —
  // see step 2's own comment — nothing left to repaint here for it).
  const mmName  = document.getElementById('mmName')

  if (mmName) {
    mmName.textContent   = 'L I R A'
    mmName.style.color       = a
    mmName.style.textShadow  = `0 0 22px ${_hexToRgba(a, 0.55)}`
  }

  // 6. Update personality quote with fade animation — once per session
  // (_mmLastQuotedPersonality guard keeps this from re-rolling on every
  // setStatus()-driven applyPersonality() call, same reasoning as before,
  // just no longer gated on an actual personality CHANGE since there isn't
  // one anymore).
  const quoteEl = document.getElementById('mmQuote')
  if (quoteEl && !_mmLastQuotedPersonality) {
    _mmLastQuotedPersonality = 'lira'
    const nextQuote = _LIRA_QUOTES[(_mmLiraQuoteIdx = 0)]
    if (quoteEl.textContent !== nextQuote) {
      quoteEl.classList.add('fading')
      setTimeout(() => {
        quoteEl.textContent = nextQuote
        quoteEl.classList.remove('fading')
      }, 800) // spec: 800ms fade out / 800ms fade in, see .mm-quote's transition
    }
  }

  // 7. Update quick stats personality name
  const mmStatPersonality = document.getElementById('mmStatPersonality')
  if (mmStatPersonality) mmStatPersonality.textContent = 'L I R A'

  // 8. Refresh system status strip
  _updateMMSysStrip()
}

function showPersonalityFlash(name) {
  personalityFlash.textContent = name
  personalityFlash.classList.add('visible')
  clearTimeout(_flashTimer)
  _flashTimer = setTimeout(() => personalityFlash.classList.remove('visible'), 2400)
}

// ════════════════════════════════════════════════════════════════════════════
// LOG
// ════════════════════════════════════════════════════════════════════════════
function ts() {
  return new Date().toLocaleTimeString('es-ES', {
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  })
}
