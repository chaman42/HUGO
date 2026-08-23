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
  // Targets #hugoDiamond's own facet-svg/glow/particles now (formerly
  // #mmOrbWrap's own separate copies — see index.html's markup comment).
  // The loop itself runs unconditionally regardless of section (as it
  // always did); the CSS side (.hugo-diamond.context-main ...) is what
  // actually hides these layers except while docked onto Main, so driving
  // them off-context is harmless, not just off-screen.
  const facetSvg = document.getElementById('hugoDiamondFacetSvg')
  const glowEl   = document.getElementById('hugoDiamondFacetGlow')
  const host     = document.getElementById('hugoDiamondParticles')
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
