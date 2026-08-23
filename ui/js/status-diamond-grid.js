// status-diamond-grid.js — Context panel, main-menu status text effects, setStatus/partial transcript, and the floating diamond's grid/position math.
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

  // Unified diamond — state class always reflects live status, in EVERY
  // context now (no longer gated on _diamondEligible(): she's the same
  // element whether floating or docked onto Main/Chat, so
  // listening/processing/speaking's own look should keep animating
  // regardless — _applyDiamondState itself is what still skips
  // repositioning while docked, see its own comment in diamond-motion.js),
  // EXCEPT the one transition owned by the post-speech hold instead:
  // speaking → idle. Per spec ("after speaking: waits 3 seconds... then
  // glides back to corner"), that specific return-to-corner is deferred to
  // _scheduleDiamondTextHide()'s timeout below, not applied immediately
  // here — unless a NEW turn (processing/speaking) starts before the hold
  // elapses, which takes over immediately same as any other transition.
  const _diamondTargetState =
    status === 'speaking'   ? 'speaking'   :
    status === 'processing' ? 'processing' :
    status === 'listening'  ? 'listening'  : 'idle'
  if (!(prevStatus === 'speaking' && _diamondTargetState === 'idle')) {
    _applyDiamondState(_diamondTargetState)
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

// Now means "not currently docked onto Main/Chat" — she's shown (and the
// state-class machinery below applies) in every context; this just gates
// POSITION/scale changes and the click-to-expand bubble/text-bubble UI
// (Main/Chat have their own dedicated text surfaces instead), used by
// _applyDiamondState/_resolveDiamondTarget (diamond-motion.js) and
// section-nav.js's own switchSection.
const LIRA_DIAMOND_EXCLUDED_SECTIONS = new Set(['home', 'chat'])
function _diamondEligible() { return !LIRA_DIAMOND_EXCLUDED_SECTIONS.has(_currentSection) }

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

// #liraDiamond's own top/left are now permanently 0 (position is driven by
// offset-path instead — see .lira-diamond's own CSS comment and
// _setDiamondPosition in diamond-motion.js), so this reads the JS-side
// bookkeeping of her logical position instead of the (no longer
// meaningful) inline style.
function _currentDiamondTopLeft() {
  return { top: _diamondCurTop, left: _diamondCurLeft }
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
//
// On top of that area score, a hard penalty for cells where her OWN actual
// rendered footprint (not just the coarser grid cell) would directly touch
// a protected rect — e.g. a cramped panel like Ajustes where every grid
// cell in the target region partially overlaps the toggle list, so the
// area score alone ties and the distance tiebreak (_bestPositionInRegions)
// can still land her dead-center on a toggle switch. A 12px buffer keeps
// her glow from visually kissing the edge of whatever she's dodging.
function _scoreCell(cell, rects, cellW, cellH) {
  const cellRect = {
    left: cell.cx - cellW / 2, right: cell.cx + cellW / 2,
    top:  cell.cy - cellH / 2, bottom: cell.cy + cellH / 2,
  }
  let overlap = 0
  for (const r of rects) overlap += _rectOverlapArea(cellRect, r)
  const areaScore = 1 - Math.min(1, overlap / (cellW * cellH))

  const { w, h } = _diamondSize()
  const buffer = 12
  const selfRect = {
    left: cell.cx - w / 2 - buffer, right: cell.cx + w / 2 + buffer,
    top:  cell.cy - h / 2 - buffer, bottom: cell.cy + h / 2 + buffer,
  }
  const directTouch = rects.some(r => _rectOverlapArea(selfRect, r) > 0)
  return directTouch ? areaScore - 10 : areaScore
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
