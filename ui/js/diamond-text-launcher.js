// diamond-text-launcher.js — Diamond text reveal/chunking/bubble display and the app launcher panel.
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
const HUGO_DIAMOND_TEXT_HOLD_MS = 3000
let _diamondTextHideTimer  = null
let _diamondChunkCancel    = null   // non-null while a multi-chunk cycle owns hugoDiamondText's lifecycle

function _showDiamondText(text) {
  clearTimeout(_diamondTextHideTimer)
  if (_diamondChunkCancel) { _diamondChunkCancel(); _diamondChunkCancel = null }
  hugoDiamondText.classList.remove('visible')
  void hugoDiamondText.offsetWidth   // force reflow so the fade-in replays even if already visible
  hugoDiamondText.classList.add('visible')

  const chunks = _splitIntoChunks(text)
  if (chunks.length <= 1) {
    _organicReveal(hugoDiamondText, text)
    return
  }
  _diamondChunkCancel = _cycleChunks(hugoDiamondText, chunks, () => {
    _diamondChunkCancel = null
    hugoDiamondText.classList.remove('visible')
    hugoDiamondText.innerHTML = ''
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
    _organicDissolve(hugoDiamondText, () => {
      hugoDiamondText.classList.remove('visible')
      hugoDiamondText.innerHTML = ''
    })
    // Glide back to the corner now — unless a NEW turn already started
    // during the hold (setStatus's own sync already moved the diamond to
    // 'processing'/'speaking' for it; don't yank it back mid-turn).
    if (currentStatus !== 'processing' && currentStatus !== 'speaking') _applyDiamondState('idle')
  }, HUGO_DIAMOND_TEXT_HOLD_MS)
}

// ── Click-to-expand bubble ──────────────────────────────────────────────
const HUGO_DIAMOND_BUBBLE_TIMEOUT_MS = 8000
let _diamondBubbleTimer = null

// Grows away from whichever screen edge the (autonomously positioned, so
// varies by state/section) diamond is currently closest to, so the bubble
// never opens off-screen.
function _positionDiamondBubble() {
  const rect     = hugoDiamond.getBoundingClientRect()
  const growLeft = rect.left > window.innerWidth  / 2
  const growUp   = rect.top  > window.innerHeight / 2
  hugoDiamondBubble.style.right  = growLeft ? '0'    : 'auto'
  hugoDiamondBubble.style.left   = growLeft ? 'auto' : '0'
  hugoDiamondBubble.style.bottom = growUp   ? 'calc(100% + 14px)' : 'auto'
  hugoDiamondBubble.style.top    = growUp   ? 'auto' : 'calc(100% + 14px)'
}

function _resetDiamondBubbleTimer() {
  clearTimeout(_diamondBubbleTimer)
  _diamondBubbleTimer = setTimeout(_closeDiamondBubble, HUGO_DIAMOND_BUBBLE_TIMEOUT_MS)
}
function _openDiamondBubble() {
  // _organicReveal('') on an empty _lastJarvisReply still ends up with
  // el genuinely empty (innerHTML cleared, no spans appended), so
  // .hugo-diamond-bubble-text:empty::before's placeholder still applies.
  _organicReveal(hugoDiamondBubbleText, _lastJarvisReply)
  _positionDiamondBubble()
  hugoDiamond.classList.add('open')
  _resetDiamondBubbleTimer()
}
function _closeDiamondBubble() {
  hugoDiamond.classList.remove('open')
  clearTimeout(_diamondBubbleTimer)
}
function _toggleDiamondBubble() {
  if (hugoDiamond.classList.contains('open')) _closeDiamondBubble()
  else _openDiamondBubble()
}

hugoDiamondInput.addEventListener('input', _resetDiamondBubbleTimer)
hugoDiamondInput.addEventListener('keydown', e => {
  _resetDiamondBubbleTimer()
  if (e.key === 'Enter') sendTextCommand(hugoDiamondInput)
})

// Tap outside the bubble closes it (per spec) — capture phase so this
// still sees the click even if something inside a section stops
// propagation. Only closes the BUBBLE, not the diamond itself.
document.addEventListener('click', (e) => {
  if (!hugoDiamond.classList.contains('open')) return
  if (hugoDiamond.contains(e.target)) return
  _closeDiamondBubble()
}, { capture: true })

// ── Click to open/close the bubble — not draggable (see the top of this
// section: HUGO controls her own position autonomously). ─────────────────
hugoDiamondOrb.addEventListener('click', () => _toggleDiamondBubble())


// ── Main menu floating text ──────────────────────────────────────────────
// Hold duration for the user's own brief echo, and the fallback hold for a
// single-chunk (short) reply from HUGO once she stops speaking (see
// setStatus's speaking→not-speaking transition below, which arms this
// timer at that point — so a short reply stays up for the full time she's
// actually speaking, not just a fixed few seconds from when the text first
// arrived). A multi-chunk (long) reply from HUGO instead paces itself via
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
  // Organic word-by-word reveal (and chunk pacing) is specifically HUGO's
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
    label:  'NÚCLEO HUGO',
    action: () => switchSection('core'),
  },
  {
    id:     'estudio',
    icon:   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 6.5 C10.2 5 7.3 4.5 4 5 V18 C7.3 17.5 10.2 18 12 19.5 C13.8 18 16.7 17.5 20 18 V5 C16.7 4.5 13.8 5 12 6.5 Z"/><path d="M12 6.5 V19.5"/></svg>',
    label:  'ESTUDIO',
    action: () => switchSection('estudio'),
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
// HUGO CORE — Estado / Pensamiento / Memoria / Mapa. Reached only via the
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
  else if (sub === 'modulos')     _renderCoreModulos()
  else if (sub === 'personas')    _renderCorePersonas()
}

document.querySelectorAll('#section-core .armor-subtab').forEach(btn => {
  btn.addEventListener('click', () => _switchCoreSubTab(btn.dataset.coreSub))
})

document.getElementById('coreClose').addEventListener('click', () => switchSection('home'))

// ── Estado ────────────────────────────────────────────────────────────────
