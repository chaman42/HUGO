// diamond-motion.js — Floating diamond best-position selection, glide/bounce animation, ambient movement, and wake trigger.
function _bestPositionInRegions(regionKeys) {
  const { cells, cellW, cellH } = _diamondGridCells()
  const rects = _diamondProtectedRects()
  let pool = []
  for (const key of regionKeys) pool = pool.concat(_regionCells(key, cells))
  if (!pool.length) pool = cells   // degenerate/tiny viewport — fall back to the whole grid rather than finding nothing

  const { top: curTop, left: curLeft } = _currentDiamondTopLeft()
  const maxDist = Math.hypot(window.innerWidth, window.innerHeight) || 1
  let best = null, bestScore = -Infinity
  for (const cell of pool) {
    const density = _scoreCell(cell, rects, cellW, cellH)
    const dist = Math.hypot(cell.cy - curTop, cell.cx - curLeft) / maxDist
    const score = density + dist * 0.35   // density dominates; distance only breaks ties/nudges away from "already there"
    if (score > bestScore) { bestScore = score; best = cell }
  }
  const { w, h } = _diamondSize()
  return { top: best.cy - h / 2, left: best.cx - w / 2 }
}

function _bestIdlePosition()      { return _bestPositionInRegions(DIAMOND_CORNER_REGIONS) }   // "lowest-density corner" per spec
function _bestAttentionPosition() { return _bestPositionInRegions(['bottom-center']) }         // wake/speaking
function _bestRegionPosition(regionKey) {
  // 'away' (from a bare "muévete"/"quítate de ahí" with no direction) —
  // just get out of the way, same algorithm as an idle re-home.
  if (!regionKey || regionKey === 'away' || !DIAMOND_REGIONS[regionKey]) return _bestIdlePosition()
  return _bestPositionInRegions([regionKey])
}

// Where should she be for section `name`? Called by section-nav.js on
// every switchSection. Main/Chat resolve to their own layout slot's real
// rect (#mmOrbSlot/#chatOrbSlot — empty placeholders, see their own CSS
// comment in main-menu-orb.css); anything else resolves to her ordinary
// ambient idle spot, at her normal resting scale. This one function is
// what replaced the old separate dock-in/undock/hop machinery — there's
// nothing left to special-case once "go to Main" and "go anywhere else"
// both just mean "glide to a {top, left, scale}".
const DIAMOND_SECTION_SLOTS = { home: 'mmOrbSlot', chat: 'chatOrbSlot' }

function _resolveDiamondTarget(name) {
  const slotId = DIAMOND_SECTION_SLOTS[name]
  if (slotId) {
    const slot = document.getElementById(slotId)
    const rect = slot ? slot.getBoundingClientRect() : null
    if (rect && rect.width && rect.height) {
      const { w, h } = _diamondSize()
      return {
        top:   rect.top  + rect.height / 2 - h / 2,
        left:  rect.left + rect.width  / 2 - w / 2,
        scale: Math.max(rect.width, rect.height) / Math.max(w, h),
      }
    }
    // Slot not actually laid out (e.g. mounts lazily) — fall back to the
    // ambient idle spot rather than morphing toward a zero-size point.
  }
  const { top, left } = _bestIdlePosition()
  return { top, left, scale: 1 }
}

// ── Move queue ────────────────────────────────────────────────────────────
// "If the diamond is already animating, queue the next position and
// animate to it after the current animation completes" (per spec) — rather
// than letting rapid section-switching retarget the CSS transition
// mid-flight on every call, an in-flight move always finishes first; only
// the LATEST position requested during that time is kept (an older,
// superseded request is simply dropped — no reason to visit a spot the
// caller has already moved past before the diamond ever got there).
//
// Bug fix (the actual "never a teleport" fix): every position here is
// expressed as explicit top+left PIXEL values, never bottom/right, and
// never the literal string 'auto'. CSS transitions cannot interpolate
// between 'auto' and a length (a hard CSS limitation — the same reason
// "animate height to auto" famously doesn't work), so an EARLIER
// bottom/right/auto-toggling version of this code silently SNAPPED
// instead of gliding on whichever axis flipped which side was "active".
// Anchoring everything to top+left in pixels makes every move a pure
// length→length interpolation, which .lira-diamond's transition (500ms,
// cubic-bezier(0.23, 1, 0.32, 1) — see its own CSS comment) can always
// animate.
let _diamondMoving        = false   // true while a move's CSS transition is in flight
let _diamondMoveSettled   = true    // guards against double-handling offset-distance's/transform's own separate transitionend events for the same move
let _diamondQueuedMove    = null    // {top, left, scale} of the most recent request made mid-animation
let _diamondMoveFallbackTimer = null
let _diamondPositionSetAt = Date.now()   // last time a REAL position change was applied — anti-annoyance cooldown clock (see _diamondAmbientMoveAllowed)

// Logical top-left corner of her (unscaled, base-size) box — top/left in
// the CSS are now permanently 0 (see .lira-diamond's own comment); this is
// the JS-side bookkeeping of "where does she logically live right now",
// updated the instant a move STARTS (not when it visually finishes,
// matching the old top/left-based code's exact semantics — a CSS
// transition's target value is already "current" the moment it's set,
// same as before). Always accurate because a new move only ever actually
// starts once the previous one has fully settled (see the move-queue
// below) — she's never asked to plot a fresh path while mid-flight from
// an unknown in-between position.
let _diamondCurTop  = 80
let _diamondCurLeft = 80

function _onDiamondMoveSettled() {
  if (_diamondMoveSettled) return   // another property's transitionend (or the fallback timer) already handled this move
  _diamondMoveSettled = true
  _diamondMoving = false
  clearTimeout(_diamondMoveFallbackTimer)
  if (_diamondQueuedMove) {
    const next = _diamondQueuedMove
    _diamondQueuedMove = null
    _setDiamondPosition(next.top, next.left, next.scale)
    return   // still mid-glide — the bounce plays once only, on the FINAL settle below
  }
  _playDiamondArrivalBounce()
}
liraDiamond.addEventListener('transitionend', ev => {
  // 'transform' alongside offset-distance since scale (--diamond-scale)
  // rides the exact same transition — a scale-only move (rare; scale
  // changes normally accompany a position change) still needs to settle
  // off its own real transition end, not just the 700ms fallback.
  if (ev.target === liraDiamond && (ev.propertyName === 'offset-distance' || ev.propertyName === 'transform')) _onDiamondMoveSettled()
})

// Builds a single genuine SVG quadratic-bezier curve (not the old two-
// straight-segment "arc") from her current CENTER point to the target
// CENTER point, gently bowed off the direct line so it never reads as a
// rigid straight shot — same "organic, never mechanical" intent as the
// old bow, just an actual continuous curve now instead of a polyline with
// a hard corner in it. Bow amount is capped as an ABSOLUTE pixel range
// (not a flat fraction of distance) so a short hop still curves visibly
// without a long cross-screen move bowing out into something absurd.
function _diamondCurvePath(curCx, curCy, targetCx, targetCy) {
  const dx = targetCx - curCx, dy = targetCy - curCy
  const dist = Math.hypot(dx, dy)
  if (dist < 1) return `path('M ${curCx} ${curCy} L ${targetCx} ${targetCy}')`
  const midX = curCx + dx / 2, midY = curCy + dy / 2
  const nx = -dy / dist, ny = dx / dist   // unit vector perpendicular to the straight line
  const bow = Math.min(60, Math.max(14, dist * 0.12)) * (Math.random() < 0.5 ? -1 : 1)
  const cx = midX + nx * bow, cy = midY + ny * bow
  return `path('M ${curCx} ${curCy} Q ${cx} ${cy} ${targetCx} ${targetCy}')`
}

// scale: 1 = her normal resting size everywhere. Anything else means she's
// currently overlaying Main's/Chat's orb slot (or, transiently, the wake
// state's own slight grow) — see _resolveDiamondTarget() in section-nav.js
// and _applyDiamondState() below, the only two callers that ever pass
// something other than the default.
function _setDiamondPosition(top, left, scale = 1) {
  top = Math.round(top)
  left = Math.round(left)
  if (_diamondMoving) {
    _diamondQueuedMove = { top, left, scale }
    return
  }
  const currentScale = parseFloat(liraDiamond.style.getPropertyValue('--diamond-scale')) || 1
  if (_diamondCurTop === top && _diamondCurLeft === left && currentScale === scale) return

  const { w, h } = _diamondSize()
  const path = _diamondCurvePath(
    _diamondCurLeft + w / 2, _diamondCurTop + h / 2,
    left + w / 2,            top + h / 2
  )

  _diamondMoving      = true
  _diamondMoveSettled = false
  _diamondPositionSetAt = Date.now()
  _diamondCurTop  = top
  _diamondCurLeft = left

  // Lay the new curve down and rewind to its start INSTANTLY (no
  // transition), then restore the transition and animate offset-distance
  // 0%->100% along it — same warp-then-transition technique already used
  // elsewhere in this app for "start a fresh animation from an exact
  // point without any of it visibly happening first".
  liraDiamond.style.transition = 'none'
  liraDiamond.style.offsetPath = path
  liraDiamond.style.offsetDistance = '0%'
  void liraDiamond.offsetWidth   // force the rewind to actually apply before...
  liraDiamond.style.transition = ''   // ...restoring it so the traversal below animates normally
  liraDiamond.style.offsetDistance = '100%'
  liraDiamond.style.setProperty('--diamond-scale', scale)

  // Fallback safety net — if transitionend never fires for some reason
  // (e.g. the element becomes momentarily unrenderable), this guarantees
  // _diamondMoving can never get stuck true forever, which would silently
  // freeze all future diamond movement.
  clearTimeout(_diamondMoveFallbackTimer)
  _diamondMoveFallbackTimer = setTimeout(_onDiamondMoveSettled, 700)
}

// Arrival overshoot — "slight overshoot on arrival, spring feel" (per
// spec). The overshoot comes from the TIMING CURVE, not the keyframe
// values (0% -> 100% is a perfectly ordinary scale(1) -> scale(1.045)
// change): cubic-bezier(0.34, 1.56, 0.64, 1)'s Y control points exceed 1,
// so the interpolated value transiently spikes past 1.045 mid-animation
// before settling exactly there at 100% — that spike IS the "spring".
// Deliberately a temporary class + keyframe animation rather than a second
// permanent transform owner: every element here already has its own
// state-driven transform (.lira-diamond.wake, .lira-diamond-orb/-glow's
// per-state scale rules) — once this animation ends (see the
// 'animationend' listener below) and the class is removed, .lira-diamond's
// own EXISTING transform transition (same spring curve, already used for
// the wake-state grow) smoothly glides back down to whatever the current
// state's resting scale actually is, so this never fights or overrides it.
function _playDiamondArrivalBounce() {
  liraDiamond.classList.remove('arrived')
  void liraDiamond.offsetWidth   // force reflow so re-adding immediately restarts the animation
  liraDiamond.classList.add('arrived')
}
liraDiamond.addEventListener('animationend', ev => {
  if (ev.animationName === 'lira-diamond-arrival-bounce') liraDiamond.classList.remove('arrived')
})

// _glideDiamondTo used to bow long moves through a hard-cornered TWO-LEG
// polyline here (a straight line to a midpoint, then a second straight
// line to the target) — replaced by _setDiamondPosition's own single
// continuous quadratic-curve path (_diamondCurvePath) now, so this is just
// a thin, still-named-for-call-site-clarity wrapper: one call, one curve,
// one transition, position and scale always finishing together because
// they're driven by the exact same transition timeline. Kept as its own
// function (rather than inlining every call site) since "glide to" reads
// better at call sites than "set position", and in case a future caller
// ever needs to intercept/log every glide request in one place again.
function _glideDiamondTo(top, left, scale = 1) {
  _setDiamondPosition(top, left, scale)
}

// ── Anti-annoyance — gates AMBIENT repositioning only (resize/mutation-
// triggered idle re-homes) ─────────────────────────────────────────────
// State transitions (wake/processing/speaking/idle-after-speaking),
// section changes, and user-commanded moves are NEVER gated by this —
// per spec, the cooldown only applies to LIRA's own passive drifting, and
// explicitly does not apply "when triggered by state change". 8s alone
// satisfies both stated rules ("never more than once every 8s" and "if
// she's been somewhere less than 5s, don't move her again", since 8 > 5).
const DIAMOND_AMBIENT_COOLDOWN_MS = 8000

function _diamondUserIsTyping() {
  const active = document.activeElement
  if (!active) return false
  return (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA') && active.offsetParent !== null
}

function _diamondAmbientMoveAllowed() {
  if (!_diamondEligible()) return false
  if (_liraDiamondState !== 'idle') return false          // wake/processing/speaking own their own position already
  if (Date.now() - _diamondPositionSetAt < DIAMOND_AMBIENT_COOLDOWN_MS) return false
  if (_diamondUserIsTyping()) return false                // "never move during active conversation"
  return true
}

function _diamondAmbientRecompute() {
  if (!_diamondAmbientMoveAllowed()) return
  const { top, left } = _bestIdlePosition()
  _glideDiamondTo(top, left)
}

// Recalculation triggers (per spec): window resize and "any DOM mutation
// that adds/removes significant content" — both debounced (content tends
// to change in bursts, e.g. a whole card grid rendering at once) and
// scoped to #sectionContainer only (the diamond's own style/text mutations
// live outside it, so this can't self-trigger a feedback loop).
let _diamondResizeTimer = null
window.addEventListener('resize', () => {
  clearTimeout(_diamondResizeTimer)
  _diamondResizeTimer = setTimeout(_diamondAmbientRecompute, 350)
})

let _diamondMutationTimer = null
const _diamondMutationObserver = new MutationObserver(() => {
  clearTimeout(_diamondMutationTimer)
  _diamondMutationTimer = setTimeout(_diamondAmbientRecompute, 500)
})
{
  const _sectionContainerEl = document.getElementById('sectionContainer')
  if (_sectionContainerEl) _diamondMutationObserver.observe(_sectionContainerEl, { childList: true, subtree: true })
}

// (the 'diamond_move' socket handler itself now lives inside
// _attemptConnect(), registered against the real jarvisSocket connection —
// see that function's own comment on the boot-crashing bug this fixes)

// ── State machine — idle / wake / listening / processing / speaking ──────
// Position only changes on entering 'idle' (best low-density corner) or
// 'wake'/'speaking' (best spot in the bottom-center region); 'listening'/
// 'processing' deliberately never reposition — "stays in current position"
// per spec, spinning/pulsing wherever wake/idle already left it. State
// transitions always move immediately, never gated by the ambient cooldown
// above (per spec: "unless triggered by state change").
//
// The state CLASS always applies, in every context (setStatus() no longer
// gates this on _diamondEligible() — see status-diamond-grid.js), so
// listening/processing/speaking's own look keeps working while she's
// docked onto Main/Chat too. Position/scale changes are the one thing
// still gated to !_diamondEligible()-false (i.e. skipped while docked) —
// while docked, section-nav.js's own _resolveDiamondTarget/_glideDiamondTo
// call owns her position/scale entirely, and a state change alone must
// never yank her out of the dock.
let _liraDiamondState = 'idle'

function _applyDiamondState(state) {
  if (state === _liraDiamondState) {
    // Already in this state — 'idle' still re-homes to the CURRENT best
    // spot, since _currentSection (and the DOM around her) can change
    // while status stays 'listening' the whole time (see switchSection's
    // own hook, which calls this same path).
    if (state === 'idle' && _diamondEligible()) { const { top, left } = _bestIdlePosition(); _glideDiamondTo(top, left, 1) }
    return
  }
  _liraDiamondState = state
  liraDiamond.classList.remove('idle', 'wake', 'listening', 'processing', 'speaking')
  liraDiamond.classList.add(state)

  if (!_diamondEligible()) return

  if (state === 'idle') {
    const { top, left } = _bestIdlePosition()
    _glideDiamondTo(top, left, 1)
  } else if (state === 'wake') {
    const { top, left } = _bestAttentionPosition()
    _glideDiamondTo(top, left, 1.15)   // whole-widget grow, formerly a CSS rule on .lira-diamond.wake — now just another scale target
  } else if (state === 'speaking') {
    const { top, left } = _bestAttentionPosition()
    _glideDiamondTo(top, left, 1)
  }
}

// Wake word detected (see the 'log' handler above) — a transient attention
// state: grows + pulses + glides to the bottom-center region immediately,
// then either settles into 'processing' (a real command followed — same
// position, _applyDiamondState('processing') is a no-op move) or, if
// nothing followed within the hold window (false alarm / cooldown-ignored
// trigger), quietly returns to 'idle' at its best corner.
const LIRA_DIAMOND_WAKE_HOLD_MS = 3000
let _diamondWakeTimer = null

function _triggerDiamondWake() {
  if (!_diamondEligible()) return
  clearTimeout(_diamondWakeTimer)
  _applyDiamondState('wake')
  _diamondWakeTimer = setTimeout(() => {
    if (currentStatus !== 'processing' && currentStatus !== 'speaking') _applyDiamondState('idle')
  }, LIRA_DIAMOND_WAKE_HOLD_MS)
}

// Initial position/context/visibility, before any status/section event has
// fired — instant (no glide/arc/bounce needed before she's even visible for
// the first time). Bug fix: switchSection()/_performSwitchSection() is only
// ever called on an actual NAVIGATION — the app boots with Main already
// `.active` in the static HTML, so nothing previously initialized
// .visible/context-main/her docked position for that very first render,
// leaving her invisible (opacity:0, never toggled) until the user
// navigated away from and back to Main at least once.
{
  liraDiamond.classList.toggle('context-main', _currentSection === 'home')
  liraDiamond.classList.toggle('context-chat', _currentSection === 'chat')
  liraDiamond.classList.add('visible')
  const { top, left, scale } = _resolveDiamondTarget(_currentSection)
  _setDiamondPosition(top, left, scale)
}

// ── Organic reveal/dissolve — see .lira-organic-word's own CSS comment for
// the full rationale and why this is separate from _typewriterReveal().
// Shared by every LIRA-own response text surface (_showDiamondText below,
// _showMMFloatingText, _openDiamondBubble). ──
function _organicReveal(el, text) {
  el.innerHTML = ''
  // Bug fix: whitespace segments used to get wrapped in their OWN
  // .lira-organic-word span (display:inline-block) same as real words —
  // but a display:inline-block element whose entire content is a single
  // whitespace character has that whitespace collapsed to zero width by
  // the browser's normal text-layout rules (leading/trailing whitespace
  // trimming applies within the span's own isolated inline-block content,
  // and since the space IS that content, it collapses to nothing). That
  // silently ate every space between words — "Estás en la pantalla"
  // rendered as "Estásenlapantalla". Whitespace segments are now appended
  // as plain text nodes instead, which flow normally in the surrounding
  // text and are never subject to that isolated-collapse — only real
  // words get their own animated span.
  const parts = String(text || '').split(/(\s+)/).filter(p => p.length)
  let wordIndex = 0
  parts.forEach(part => {
    if (/^\s+$/.test(part)) {
      el.appendChild(document.createTextNode(part))
      return
    }
    const span = document.createElement('span')
    span.className = 'lira-organic-word'
    span.textContent = part
    span.style.animationDelay = `${wordIndex * 45}ms`
    el.appendChild(span)
    wordIndex++
  })
}

// Plays the "dissolves outward like dissipating energy" exit on every word
// already in `el`, then calls onDone once the LAST word's own animation
// genuinely finishes (via 'animationend', never a hardcoded setTimeout
// that could drift out of sync with the CSS duration/stagger above).
function _organicDissolve(el, onDone) {
  const words = el.querySelectorAll('.lira-organic-word')
  if (!words.length) { if (onDone) onDone(); return }
  let remaining = words.length
  words.forEach((span, i) => {
    span.style.animationDelay = `${i * 20}ms`
    span.classList.add('lira-dissolving')
    span.addEventListener('animationend', () => {
      remaining--
      if (remaining <= 0 && onDone) onDone()
    }, { once: true })
  })
}

// ── Sentence chunking + reading-pace, for long responses ─────────────────
// A long reply used to appear all at once and sit there for a single fixed
// hold — unreadable if it ran more than a sentence or two, especially with
// TTS muted (no "while she's speaking" runway at all in that case). Long
// text now paces itself through one sentence/chunk at a time, each held
// for its own calculated reading time, independent of TTS timing (status
// is a coarse speaking/not-speaking signal with no audio-position info to
// sync against, and a reader going at their own pace — or with TTS off —
// needs pacing that doesn't depend on it anyway). Very short (single-
// chunk) responses are untouched — see the `chunks.length <= 1` branches
// below, which are exactly the original behavior.
