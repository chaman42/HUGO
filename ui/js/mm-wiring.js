// mm-wiring.js — Main-menu tray wiring, ambient visual loop, and the start of the armor suit SVG definitions.
;(function _wireMMTrays() {
  document.querySelectorAll('.mm-tray-handle').forEach(handle => {
    handle.addEventListener('click', () => {
      const tray = handle.closest('.mm-tray')
      if (tray) tray.classList.toggle('mm-tray-retracted')
    })
  })
})()

// Commercial-grade redesign (2nd pass) — single requestAnimationFrame loop
// driving BOTH the diamond's breathing (facet SVG scale + inner-glow
// opacity, combined out-of-phase sine waves at different frequencies so the
// loop never reads as perfectly periodic — spec: "multiple overlapping
// sinusoidal functions... never perfectly periodic") AND the gold particle
// field (each particle on its own organic curve — figure-8, spiral, or
// drift — never a circular orbit, duration randomized 8-20s so nothing
// synchronizes). One shared loop, not two competing ones, per spec's own
// "implementation: JS with Math.sin ... updated via requestAnimationFrame."
// Reads document.body's existing state classes (speaking/processing) each
// frame rather than adding a second state machine: speaking multiplies
// particle speed 1.8x and raises opacity toward 0.8; processing eases every
// particle toward the diamond's center.
;(function _mmVisualLoop() {
  // Targets #liraDiamond's own facet-svg/glow/particles now (formerly
  // #mmOrbWrap's own separate copies — see index.html's markup comment).
  // The loop itself runs unconditionally regardless of section (as it
  // always did); the CSS side (.lira-diamond.context-main ...) is what
  // actually hides these layers except while docked onto Main, so driving
  // them off-context is harmless, not just off-screen.
  const facetSvg = document.getElementById('liraDiamondFacetSvg')
  const glowEl   = document.getElementById('liraDiamondFacetGlow')
  const host     = document.getElementById('liraDiamondParticles')
  if (!facetSvg || !glowEl || !host) return

  // 25 desktop / 18 tablet / 12 mobile — spec's exact 3-tier particle counts,
  // decided once at spawn (viewport width rarely changes after load on this
  // kiosk-style app; a full respawn on resize isn't worth the complexity).
  const vw = window.innerWidth
  const COUNT = vw <= 500 ? 12 : (vw <= 900 ? 18 : 25)

  // Per-particle motion parameters, each randomized independently so no two
  // particles ever move identically. Base position (cx/cy, % of host box) is
  // set once via left/top (a layout property, cheap since it's set only
  // here, not per frame); the per-frame animation below only ever touches
  // `transform`/`opacity` (compositor-only, no layout thrash).
  const particles = []
  for (let i = 0; i < COUNT; i++) {
    const el = document.createElement('div')
    el.className = 'mm-particle'
    const cx = 50 + (Math.random() * 60 - 30)
    const cy = 50 + (Math.random() * 60 - 30)
    el.style.left = cx + '%'
    el.style.top  = cy + '%'
    host.appendChild(el)
    particles.push({
      el,
      cx, cy,                                    // base position (% of host), needed to pull toward center while processing
      kind: i % 3,                              // 0 figure-8, 1 spiral, 2 organic drift — spread evenly, not random-clustered
      rx: 18 + Math.random() * 34,
      ry: 18 + Math.random() * 34,
      phase: Math.random() * Math.PI * 2,
      duration: 8 + Math.random() * 12,          // 8-20s per spec
      spiralDir: (i % 2 === 0) ? 1 : -1,
      baseOpacity: 0.4 + Math.random() * 0.3,    // 0.4-0.7 per spec
    })
  }

  // Host box's pixel size, used to convert the percentage-scale motion
  // amplitudes (rx/ry) into real px offsets for `transform: translate()`.
  // Re-measured on resize; a rAF-driven loop re-reading getBoundingClientRect()
  // every frame would force layout, so this is cached instead.
  let hostW = host.offsetWidth, hostH = host.offsetHeight
  window.addEventListener('resize', () => { hostW = host.offsetWidth; hostH = host.offsetHeight })

  const t0 = performance.now()
  function frame(now) {
    const t = (now - t0) / 1000

    const speaking   = document.body.classList.contains('speaking')
    const processing = document.body.classList.contains('processing')

    // ── Diamond breathing: 2 sines at different frequencies/phases, range
    //    lands at ~0.972–1.028 (spec's exact bounds). ──
    const scale = 1 + 0.020 * Math.sin(t * 0.42) + 0.008 * Math.sin(t * 1.11 + 1.7)
    facetSvg.style.transform = `scale(${scale.toFixed(4)})`

    // Glow breathes independently, out of phase with the scale above (own
    // frequency + phase offset) — never locks into the same rhythm.
    const glowOpacity = 0.4 + 0.15 * Math.sin(t * 0.35 + 2.4) + 0.05 * Math.sin(t * 0.9 + 0.6)
    glowEl.style.opacity = Math.max(0, Math.min(1, glowOpacity)).toFixed(3)

    // ── Particles ──
    const speedMul = speaking ? 1.8 : 1
    particles.forEach(p => {
      const pt = (t * speedMul) / p.duration * Math.PI * 2 + p.phase
      let dx, dy // organic-motion deltas, in % of host box
      if (p.kind === 0) {
        // figure-8 (2:1 lissajous ratio)
        dx = Math.sin(pt) * p.rx
        dy = Math.sin(pt * 2) * p.ry * 0.5
      } else if (p.kind === 1) {
        // slow spiral — radius itself oscillates while circling
        const r = (0.5 + 0.5 * Math.sin(pt * 0.3)) * p.rx
        dx = Math.cos(pt * p.spiralDir) * r
        dy = Math.sin(pt * p.spiralDir) * r * (p.ry / p.rx)
      } else {
        // organic drift — two off-ratio sines per axis so it never repeats on a short, obvious cycle
        dx = Math.sin(pt * 0.7 + p.phase) * p.rx * 0.7 + Math.sin(pt * 1.3) * p.rx * 0.3
        dy = Math.cos(pt * 0.6 + p.phase) * p.ry * 0.7 + Math.cos(pt * 1.7) * p.ry * 0.3
      }

      // Processing: pull each particle's actual base position (p.cx/p.cy)
      // toward the host's center (50%, the diamond) rather than just
      // damping the motion amplitude — a particle spawned far from center
      // needs an explicit inward pull, shrinking the orbit alone would just
      // freeze it in place out at the edge.
      if (processing) {
        dx = dx * 0.3 + (50 - p.cx) * 0.5
        dy = dy * 0.3 + (50 - p.cy) * 0.5
      }

      const pxX = (dx / 100) * hostW
      const pxY = (dy / 100) * hostH
      const opacity = speaking ? Math.min(0.8, p.baseOpacity + 0.2) : p.baseOpacity

      p.el.style.transform = `translate(${pxX.toFixed(1)}px, ${pxY.toFixed(1)}px)`
      p.el.style.opacity   = opacity.toFixed(2)
    })

    requestAnimationFrame(frame)
  }
  requestAnimationFrame(frame)
})()

// ════════════════════════════════════════════════════════════════════════════
// SERVICE WORKER
// ════════════════════════════════════════════════════════════════════════════
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {})
}

// ════════════════════════════════════════════════════════════════════════════
// ARMOR BAY — data, rendering, navigation, detail page, concept form
// ════════════════════════════════════════════════════════════════════════════

// ── Armor data — fetched fresh from the backend on every call, never
// cached (same "no staleness to reason about" convention
// _renderCoreModulos() already uses for its own catalog fetch, see
// ui/js/core-tabs-sleep-panel.js). Used to be a hand-maintained JS literal
// here that duplicated data/armor_knowledge.json — see core/armor_manager.py's
// own module docstring for why that stopped being true (editing a model's
// status needs a real round trip to the file that's actually rendered).
async function _fetchArmorModels() {
  try {
    const res = await fetch(`${JARVIS_API}/api/armor`)
    const data = await res.json()
    const models = Array.isArray(data.models) ? data.models : []
    return {
      primarios: models.filter(m => m.category === 'primario'),
      paralelos: models.filter(m => m.category === 'paralelo'),
    }
  } catch {
    return { primarios: [], paralelos: [] }
  }
}

// ── Shared suit silhouette SVG (blueprint schematic style) ─────────────────
function _suitSVG() {
  return `<svg class="armor-silhouette" viewBox="0 0 60 96" fill="none"
    stroke="currentColor" stroke-width="1" xmlns="http://www.w3.org/2000/svg">
    <rect x="19" y="2"  width="22" height="14" rx="2"/>
    <rect x="22" y="6"  width="16" height="5"  rx="1" opacity=".45"/>
    <rect x="24" y="16" width="12" height="5"/>
    <path d="M12 21 L48 21 L50 31 L10 31 Z"/>
    <circle cx="30" cy="26" r="4"/>
    <rect x="16" y="31" width="28" height="8" rx="1"/>
    <rect x="13" y="39" width="34" height="5" rx="1"/>
    <rect x="11" y="44" width="38" height="3"/>
    <rect x="2"  y="21" width="8"  height="17" rx="2"/>
    <rect x="2"  y="39" width="8"  height="13" rx="2"/>
    <rect x="2"  y="53" width="8"  height="6"  rx="1"/>
    <rect x="50" y="21" width="8"  height="17" rx="2"/>
    <rect x="50" y="39" width="8"  height="13" rx="2"/>
    <rect x="50" y="53" width="8"  height="6"  rx="1"/>
    <rect x="15" y="47" width="12" height="19" rx="2"/>
    <rect x="15" y="67" width="12" height="16" rx="2"/>
    <rect x="13" y="83" width="16" height="7"  rx="1"/>
    <rect x="33" y="47" width="12" height="19" rx="2"/>
    <rect x="33" y="67" width="12" height="16" rx="2"/>
    <rect x="31" y="83" width="16" height="7"  rx="1"/>
  </svg>`
}

// ── Model VI custom blueprint diagram — the only model with a bespoke
// silhouette instead of the generic placeholder above. Hardcoded to
// #4db8ff (a brightened variant of the fixed shell/UI blue — the generic
// diagrams still render in var(--accent)'s #3fa9f5, which never changes
// with personality; this one's bumped slightly brighter, along with
// thicker stroke-width throughout, since the original was hard to read)
// rather than currentColor, per spec: this diagram's color is pinned
// regardless of any personality-driven theming context it might ever be
// placed in. Front-view, angular/faceted take on a Mark 6-style
// suit — straight panel edges rather than rounded curves. Every shape is
// stroke-only (fill="none"); the thinner interior lines mark panel
// boundaries (visor, reactor, pauldron/bicep, bicep/forearm, torso/waist,
// thigh/shin/boot, gauntlet) the same way real color-zone seams would sit
// on the actual suit, without using any actual color fill to show them. -->
function _model6SVG() {
  return `<svg class="armor-silhouette model6-blueprint" viewBox="0 0 400 800"
    fill="none" stroke="#4db8ff" xmlns="http://www.w3.org/2000/svg">
    <!-- Helmet -->
    <path stroke-width="2.5" d="M200,30 L235,45 L248,78 L238,112 L213,136 L187,136 L162,112 L152,78 L165,45 Z"/>
    <!-- Visor (angular, split down the middle) -->
    <path stroke-width="2.5" d="M164,80 L236,80 L230,102 L170,102 Z"/>
    <line stroke-width="2" x1="200" y1="80"  x2="200" y2="102"/>
    <line stroke-width="2" x1="155" y1="78"  x2="245" y2="78"/>
    <line stroke-width="2" x1="162" y1="110" x2="238" y2="110"/>
    <!-- Neck -->
    <path stroke-width="2.5" d="M182,136 L218,136 L214,158 L186,158 Z"/>
    <!-- Shoulder pauldrons -->
    <path stroke-width="2.5" d="M90,172 L182,158 L182,224 L100,230 L74,204 Z"/>
    <path stroke-width="2.5" d="M310,172 L218,158 L218,224 L300,230 L326,204 Z"/>
    <line stroke-width="2" x1="84"  y1="224" x2="160" y2="221"/>
    <line stroke-width="2" x1="240" y1="221" x2="316" y2="224"/>
    <!-- Chest / torso, with reactor and ab-plate lines -->
    <path stroke-width="2.5" d="M182,158 L218,158 L260,190 L257,328 L200,349 L143,328 L140,190 Z"/>
    <circle stroke-width="2" cx="200" cy="234" r="27"/>
    <circle stroke-width="2" cx="200" cy="234" r="14"/>
    <line stroke-width="2" x1="168" y1="282" x2="232" y2="282"/>
    <line stroke-width="2" x1="170" y1="306" x2="230" y2="306"/>
    <line stroke-width="2" x1="145" y1="326" x2="255" y2="326"/>
    <!-- Waist -->
    <path stroke-width="2.5" d="M143,328 L200,349 L257,328 L249,398 L200,419 L151,398 Z"/>
    <!-- Biceps -->
    <path stroke-width="2.5" d="M74,224 L100,230 L109,236 L106,314 L69,317 L61,239 Z"/>
    <path stroke-width="2.5" d="M326,224 L300,230 L291,236 L294,314 L331,317 L339,239 Z"/>
    <line stroke-width="2" x1="64"  y1="315" x2="108" y2="312"/>
    <line stroke-width="2" x1="292" y1="312" x2="336" y2="315"/>
    <!-- Forearms -->
    <path stroke-width="2.5" d="M69,317 L106,314 L101,414 L73,417 Z"/>
    <path stroke-width="2.5" d="M331,317 L294,314 L299,414 L327,417 Z"/>
    <line stroke-width="2" x1="72"  y1="415" x2="103" y2="412"/>
    <line stroke-width="2" x1="297" y1="412" x2="328" y2="415"/>
    <!-- Gauntlets -->
    <path stroke-width="2.5" d="M73,417 L101,414 L99,456 L86,469 L67,461 L65,426 Z"/>
    <path stroke-width="2.5" d="M327,417 L299,414 L301,456 L314,469 L333,461 L335,426 Z"/>
    <line stroke-width="2" x1="68"  y1="436" x2="98"  y2="434"/>
    <line stroke-width="2" x1="302" y1="434" x2="332" y2="436"/>
    <!-- Thighs -->
    <path stroke-width="2.5" d="M151,398 L199,419 L196,556 L153,559 L149,421 Z"/>
    <path stroke-width="2.5" d="M249,398 L201,419 L204,556 L247,559 L251,421 Z"/>
    <line stroke-width="2" x1="151" y1="557" x2="197" y2="554"/>
    <line stroke-width="2" x1="203" y1="554" x2="249" y2="557"/>
    <!-- Shins -->
    <path stroke-width="2.5" d="M153,559 L196,556 L193,676 L159,679 Z"/>
    <path stroke-width="2.5" d="M247,559 L204,556 L207,676 L241,679 Z"/>
    <line stroke-width="2" x1="157" y1="677" x2="195" y2="675"/>
    <line stroke-width="2" x1="205" y1="675" x2="243" y2="677"/>
    <!-- Boots -->
    <path stroke-width="2.5" d="M159,679 L193,676 L197,731 L186,756 L141,756 L136,716 Z"/>
    <path stroke-width="2.5" d="M241,679 L207,676 L203,731 L214,756 L259,756 L264,716 Z"/>
    <line stroke-width="2" x1="136" y1="749" x2="197" y2="749"/>
    <line stroke-width="2" x1="203" y1="749" x2="264" y2="749"/>
  </svg>`
}

// ── Model VIII custom blueprint diagram — same rationale as Model VI's
// above (bespoke silhouette instead of the generic placeholder, only for
// this one model; hardcoded #4db8ff (brightened from the base #3fa9f5, same as Model VI), never currentColor/personality-owned).
// More refined than Model VI throughout: more panel lines per segment,
// boxier forearms, and a chest built around a central downward-pointing
// triangular reactor with facet lines radiating out to kite-shaped panels
// (the real suit's actual centerpiece — see armor_knowledge.json's
// description of Model VIII), plus a separately-segmented abdominal
// section below it (3 lines vs Model VI's 2), matching "the most advanced
// completed suit" in the collection. -->
