// Bug fix ("Actualizar Sistema completes with no errors but changes aren't
// visible"): root-caused to the fetch handler below — see its own comment.
// Bumped to v62 alongside that fix so this exact change purges itself in
// immediately, same as every future rebuild_app.sh cache-bust does.
//
// v82: ui/index.html was split into index.html + styles.css + app.js (pure
// refactor for Claude Code token efficiency, no behavior change). Bumped so
// clients drop any stale cached HTML that still embeds the old inline
// <style>/<script> blocks, and so styles.css/app.js get precached below.
//
// v84: styles.css/app.js were split further into ui/css/*.css (13 files)
// and ui/js/*.js (17 files) — same pure-refactor reasoning as v82. Bumped
// so clients purge any cache still holding the now-deleted /styles.css and
// /app.js and precache the real split files instead; a stale-cached
// index.html referencing those dead paths, combined with cache.addAll()
// failing atomically on their 404s, is exactly what left the app stuck on
// the boot splash after that split shipped.
//
// v85: the JS split itself had a real bug — some functions ended up in a
// later-loaded chunk than a bare top-level call to them in an earlier
// chunk (only safe in the original single-file app.js because function-
// declaration hoisting made call order irrelevant there). Fixed in
// clock-boot-splash-wiring.js/settings-updates.js (moved the boot-splash
// init calls) and bootstrap-auth.js/chat-render.js (moved esc()). Bumped
// so clients actually pick up the fixed JS instead of their already-cached
// (and still-broken) v84 copies.
//
// v86: Ajustes premium design pass — controls-bar.css/diamond-toggles.css/
// boot-splash.css content changed (new .settings-scroll/.settings-divider
// rules, redesigned .toggle-switch, removed the now-superseded
// #section-settings .section-inner scroll override). Bumped so clients
// fetch the updated CSS instead of serving their already-cached v85 copies
// — CSS content changes don't invalidate a cache-first entry on their own,
// only a byte change to this file does.
//
// v88: added the ESTUDIO app — new ui/css/estudio.css and ui/js/estudio.js,
// both added to PRECACHE below. Same reasoning as every prior bump: a new
// file only reaches clients once this file's own bytes change.
//
// v101: added the Armor Design Studio (Diseño → Armaduras Phase 1) — new
// ui/css/design-studio.css and ui/js/design-studio.js, both added to
// PRECACHE below. Same reasoning as v88.
//
// v102: Armor Design Studio Phase 2 — interactive zone-by-zone design.
// design-studio.css/design-studio.js content changed (already in PRECACHE
// since v101, just needs a bump so clients fetch the new bytes instead of
// their cached v101 copies — same reasoning as v86.
//
// v103: Armor Design Studio Phase 2.5 — parts drawer + always-3 options.
// design-studio.css/design-studio.js content changed again — same bump
// reasoning as v102.
//
// v104: Armor Design Studio Phase 3 — autopilot mode. design-studio.css/
// design-studio.js content changed again — same bump reasoning as v103.
//
// v105: design studio workspace layout fix (chat panel dead space, diagram
// sizing/centering, progress bar button cutoff) — design-studio.css/
// design-studio.js content changed again — same bump reasoning as v104.
//
// v106: Design Studio ↔ Conceptuales integration — GUARDAR EN ESTUDIO now
// creates/updates a linked Conceptuales entry, and concept cards get
// CREAR/EDITAR DISEÑO + CON/SIN DISEÑO badges + an INSPECCIONAR detail
// view. design-studio.css/design-studio.js/concepts.css/concepts-edit.js
// all changed — same bump reasoning as v105 (all four already in
// PRECACHE, concepts.css/concepts-edit.js since the original ESTUDIO-era
// list, design-studio.css/design-studio.js since v101).
// v107: autopilot now calls Ollama (llama3.2:3b) instead of Groq for zone
// design generation, plus the autopilot-start/-stop Ollama lifecycle calls
// — design-studio.js changed, already in PRECACHE since v101.
// v108: autopilot debug console.log instrumentation (every step of the
// INICIAR AUTOPILOTO flow, per-zone request/response/timing/empty-result
// warnings) — design-studio.js changed again, same PRECACHE entry as v107.
//
// v109: autopilot speed + background UX pass — switched zone generation to
// llama3.2:1b (from 3b) with 200 max_tokens and a reinforced first-attempt
// prompt (core/commands.py, not client-cached but bundled in this same
// change); progressive per-zone diagram updates + "GENERANDO — puede
// tardar varios minutos" hint (already precached design-studio.js/css);
// and a new persistent bottom-toolbar autopilot indicator + completion
// toast that stay visible across section navigation — new markup in
// index.html itself (not precached, but index.html is served network-
// first per the fetch handler below, so this bump exists purely to push
// the changed design-studio.js/css bytes past clients' cached v108 copies).
// v110: review mode is now explicitly gated behind a real 'autopilot_complete'
// event (dispatched only after every zone in a run has been through its
// Ollama call) instead of relying on _dsStartReview's call order — a new
// _dsAutopilotComplete flag hard-blocks the review overlay from opening
// otherwise. design-studio.js changed, same PRECACHE entry as v109.
// v111: ESTUDIO → RESÚMENES/ESQUEMAS cards never rendered the actual
// generated content (sc.content/s.content) — only the topic/excerpt line,
// so a real resumen/esquema looked identical to an empty one. Both are now
// expandable (same click-to-detail pattern as INVESTIGACIÓN) to show the
// full text. estudio.js changed, already in PRECACHE since v88.
// v112: NÚCLEO HUGO → Módulos tab — first a runtime skills/ registry view
// (enable/disable toggles), then replaced with the full capability catalog
// (grouped by category, collapsible, click-to-expand detail) backed by
// GET /api/modules/catalog. core-tabs-sleep-panel.js and
// armor-mindmap-detail.css changed across both passes — both already in
// PRECACHE (core-tabs-sleep-panel.js since the original NÚCLEO HUGO build,
// armor-mindmap-detail.css likewise) — this bump is what actually gets
// clients off their stale cached copies, which is why the tab rendered
// empty/stale until now.
// v113: Módulos catalog view gained per-entry block/priority controls
// (POST /api/modules/catalog/<id>/block|priority) — core-tabs-sleep-panel.js
// and armor-mindmap-detail.css changed again, both already in PRECACHE.
// v114: Módulos catalog view gained a build/update trigger — 'Crear módulo'
// for a not-yet-installed entry, 'Actualizar' (+ change description input)
// for an installed one, both POSTing to /api/code-engine/create|update and
// polling for status until terminal. Same two files changed again.
// v115: MOTOR DE VOZ toggle (Ajustes) gained a third engine option — macOS
// native `say` — index.html got a new <button data-engine="say"> and
// components.css's header comment was updated; bumped so clients drop
// their stale cached index.html/components.css and see the new button.
// v116: Ajustes gained a 'Código HUGO' feature toggle (Code Engine
// Phase 4 kill switch) — chat-render.js's FEATURE_FLAG_LABELS changed.
// v117: Ajustes gained an 'Auto-actualización' toggle, separate from
// 'Código HUGO' — chat-render.js's FEATURE_FLAG_LABELS changed again.
// v118: Chat section's orb was missing the faceted gold-diamond SVG layer
// the main-menu orb and persistent-bar orb already had (same geometry,
// see .mm-orb-facet-svg/.hugo-diamond-facet-svg) — added as .orb-facet-svg
// in index.html + chat.css. Bumped so clients drop their stale cached
// index.html/chat.css and see the updated logo instead of the old plain one.
// v119: JARVIS/FRIDAY personalities removed entirely — HUGO is now the only
// one. Orb is always diamond-shaped/gold (no more shape-toggle branching or
// .orb-bracket circle decoration), the settings-panel personality switcher
// row, #personalityModal, and the Electron tray's Personality submenu are
// all gone. index.html, chat.css, personality-nav.css, controls-bar.css,
// nav-personality-modal.css, main-menu-orb.css, and every ui/js/*.js file
// that referenced jarvis/friday all changed — bumped so clients drop every
// stale cached copy instead of a half-updated mix of old/new assets.
// v120: Two additions — (1) an attach button (📎) next to the chat text
// input, staging picked files as removable chips and folding their names
// into the outgoing message (no real upload/read backend yet, see
// chat-render.js's own comment); index.html, controls-bar.css, app.js,
// and chat-render.js all changed. (2) The small floating diamond now
// docks/morphs into Main's or Chat's big diamond (glide + scale + fade)
// instead of a plain fade-in-place when navigating there — index.html
// (added #chatOrbWrap), diamond-motion.js, section-nav.js, and
// diamond-toggles.css all changed. Bumped so clients drop every stale
// cached copy of all of the above.
// v121: Reworked the docking morph (v120) from a cross-fade into a true
// same-frame swap — she now stays fully opaque through the whole glide/
// grow and is hidden with transition:none at the exact instant the big
// diamond is revealed, instead of a lingering opacity fade that showed
// both at once. Main's #mmOrbWrap entrance (previously its own
// independent 0.5s pop, running concurrently with the glide) is now
// deferred and fired in that same instant too, via _dockDiamondInto's new
// onArrive param — the two were racing each other, which broke the
// "one diamond becomes the other" illusion into "two things happening at
// once". diamond-motion.js, section-nav.js, and diamond-toggles.css all
// changed again.
// v122: Found the actual remaining cause of "big diamond still animates
// separately" — a generic .app-section.active fade/scale (concepts.css)
// runs on EVERY section switch independent of anything diamond-specific,
// so Chat's own big diamond (which, unlike Main's #mmOrbWrap, had no
// per-orb hide mechanism at all) was visible the entire time via that
// generic fade, not hidden until the swap the way Main's already was.
// Added #chatOrbWrap.docking-hidden (main-menu-orb.css) — hidden the
// instant a dock into Chat begins, revealed instantly (transition:none)
// at the swap, mirroring .mm-orb-leaving's existing role for Main.
// main-menu-orb.css and section-nav.js changed.
// v123: Three fixes per direct feedback the docking animation still felt
// off. (1) Faster: docking's transform (size change) trimmed 0.55s->0.32s
// and top/left 0.5s->0.4s, and the transform curve swapped to the app's
// own established "arrival spring" bezier (same as .arrived/.wake)
// instead of a bespoke one. (2) More natural movement: docking now routes
// through _glideDiamondTo — the SAME arc-or-straight logic every ambient
// reposition already uses — instead of a rigid straight line, and the
// swap now fires off the real move-settle event instead of a fixed
// setTimeout guess (correct for both 1-leg and 2-leg/arc moves). (3)
// Direct Main<->Chat navigation ("Hop") now animates too — previously
// neither endpoint ever showed the small diamond so nothing animated at
// all; now the SOURCE big diamond itself flies across and grows/shrinks
// into the DESTINATION, via the same dock/swap machinery with a new
// fromRect param. #chatOrbWrap starts docking-hidden by default in the
// static HTML now (matching #mmOrbWrap's default-visible state, since
// the app boots on Main). index.html, diamond-motion.js, section-nav.js,
// and diamond-toggles.css all changed.
// v124: Fixed the remaining case the v120-123 docking work never covered —
// LEAVING Main/Chat into any other section had no undock morph at all, so
// the small diamond just faded in at its idle spot while the big orb
// independently played its own shrink, breaking the "one diamond"
// illusion in that direction. Added a new _undockDiamondFrom (mirror of
// _dockDiamondInto) wired into a new _isUndock case in switchSection.
// Also fixed a geometry mismatch found in the process: the floating
// diamond's border-ring clip-path was full-bleed (0/50/100) while both
// big orbs' rings are 5%-inset (5/95), causing a visible size "pop" at
// every dock/undock swap instant even when position/scale matched
// perfectly. diamond-motion.js, section-nav.js, and diamond-toggles.css
// all changed.
// v125: Found the actual cause of the "diamond flies off to the side then
// teleports into place" glitch reported live on both Main<->Chat hop
// directions. switchSection()'s "section-aware recompute" (re-home to the
// best idle spot for the new section) ran unconditionally on every nav,
// including dock-in/hop/undock — for a dock-in/hop this queued a SECOND,
// unrelated glide toward an ambient idle spot right behind the dock's own
// glide into the big orb. Because a queued move always consumes the next
// settle event first, the dock's arrival swap (reveal big orb/hide small
// diamond) got deferred until that unrelated detour finished — so she'd
// visibly dock (at full docked size, which is also what looked like a
// resizing glitch), peel off toward some idle corner, then hard-swap into
// place only once the wandering stopped. Recompute now skips entirely
// when _isDockIn/_isHop/_isUndock, since each already owns and completes
// its own final positioning. section-nav.js changed.
// v126: Architectural rewrite of the diamond animation system per direct
// feedback that v120-125's incremental dock/undock/hop patches kept
// surfacing new sync bugs — because #hugoDiamond, #mmOrbWrap, and
// #chatOrbWrap were three separate DOM elements, and every transition
// between them relied on JS warping one to match another's rect then
// hard-swapping visibility at exactly the right instant. #hugoDiamond is
// now the ONLY diamond element that ever exists, on every section
// including Main/Chat — she's never hidden/swapped, just continuously
// repositioned/rescaled (top/left/--diamond-scale) via one unified
// _glideDiamondTo(), the same call ambient repositioning already used.
// #mmOrbWrap/#chatOrbWrap are gone, replaced by empty layout placeholders
// (#mmOrbSlot/#chatOrbSlot) that only exist so sibling elements (name/
// status labels, title, mic dot) keep their layout; #hugoDiamond overlays
// their rect while docked. Main's/Chat's own decorative layers (energy
// fields, rings, gems, particles) moved into #hugoDiamond itself as
// context-gated sibling layers (.context-main/.context-chat), with the
// circular processing spinner and speaking wave-bars unified to the
// diamond's own existing perimeter-arc/shape-ripple look everywhere
// (previously duplicated three ways). This deletes the entire dock-in/
// undock/hop special-casing (_dockDiamondInto, _undockDiamondFrom,
// DIAMOND_DOCK_TARGETS, --dock-scale/.docking, mm-orb-leaving/docking-
// hidden, onArrive callbacks) — there's nothing left to swap, so there's
// nothing left to get out of sync. index.html, diamond-motion.js,
// section-nav.js, status-diamond-grid.js, mm-wiring.js, settings-
// updates.js, personality-switch.js, and every CSS file touching the
// orb/diamond all changed.
// v127: v126's unified diamond never actually appeared on Main at boot —
// switchSection()/_performSwitchSection() (the only place that added
// .visible/context-main and set her docked position) is only ever called
// on an actual navigation; the app boots with Main already `.active` in
// the static HTML, so nothing initialized any of that for the very first
// render. Fixed at the same "initial position" boot hook that already set
// her starting top/left, in diamond-motion.js.
// v128: The arrival-bounce keyframe hardcoded scale(1)->scale(1.045),
// ignoring --diamond-scale — harmless before since a docked arrival always
// bypassed the bounce via the old dock-arrive callback, but now every
// settle plays it, so a docked diamond would visibly snap down to
// floating-size for the animation's 0.35s on every arrival. Keyframe
// values now scale relative to --diamond-scale. diamond-toggles.css changed.
// v129: Fixed live feedback that docking into Main/Chat still "resizes,
// then moves" — .hugo-diamond's transform (scale) transitioned over 0.34s
// while top/left transitioned over 0.42s, so the size visibly finished
// changing well before the position glide caught up. All three now share
// the same 0.42s duration so position and scale land together.
// diamond-toggles.css changed.
// v130: Found the ACTUAL cause of "resizes then moves" (v129's duration
// equalization wasn't it) — a docking move covering >260px (most
// section->Main/Chat moves) bows through two legs, and _glideDiamondTo was
// passing the FINAL target scale on BOTH legs. That completes the scale
// transition during leg 1 alone (0.42s), while position keeps traveling
// through leg 2 (another 0.42s) — she'd finish resizing, then keep
// gliding the rest of the way at full size. Scale now only changes on the
// final leg (held at her current scale during the midpoint bow), so
// position and size land together. diamond-motion.js changed.
// v131: "resizes then moves" persisted even after v130's arc-leg fix and
// matching top/left/transform DURATIONS, because the CURVES still
// differed — transform used a spring/overshoot bezier that visually
// settles well before its transition's nominal end, while top/left's
// plain ease-out only reaches target smoothly at the very end. Same
// duration, same start, still visibly finish at different moments.
// transform now shares top/left's exact curve too, so position and
// scale track in lockstep the whole time, not just start/end together.
// The spring "arrival" feel still happens, entirely via .arrived's
// separate post-settle bounce. diamond-toggles.css changed.
// v132: Replaced the whole top/left + fake two-straight-segment "arc"
// positioning system with a real single CSS offset-path/offset-distance
// quadratic-bezier curve per move. Rounds 1-2 (matching transition
// duration, then matching the curve too) still weren't enough because
// the underlying motion was never one continuous thing — a "long" move
// bowed through a hard-cornered TWO-LEG polyline (straight line to a
// midpoint, then a second straight line to target), which read as
// mechanical no matter how well-matched the timing was, and gave scale a
// real seam to desync at (the leg1->leg2 boundary, a brand new transition
// restarting mid-flight). Position now animates along one genuine curved
// path in a single transition, sharing the exact same timeline as the
// scale transform — mechanically incapable of finishing at different
// moments now. diamond-toggles.css, diamond-motion.js, and
// status-diamond-grid.js changed.
// v133: New "Personas" tab in Núcleo HUGO — lists every person HUGO has a
// saved record of (core/social.py), with a Joan-editable trust tier
// (Owner/Private/Personal/Public, or "Desconocido" until Joan explicitly
// reviews someone — new Person.trust_confirmed field) and knows_hugo
// flag, plus add/edit/delete. New backend routes: POST /api/social/people
// (create), PATCH /api/social/people/<id> (edit name/relationship/
// knows_hugo); POST .../trust extended to also set trust_confirmed.
// index.html, diamond-text-launcher.js, core-tabs-sleep-panel.js, and
// armor-mindmap-detail.css changed.
// v134: Armor Bay's build status is now editable (Modelos Primarios /
// Proyectos Paralelos), 5 options via a click-to-reveal picker in the
// detail view (reuses the Personas trust-tier picker's CSS/interaction).
// Also removed a real duplication bug found along the way: the frontend
// used to render from a hand-maintained ARMOR_DATA JS literal
// (ui/js/mm-wiring.js) that duplicated data/armor_knowledge.json instead
// of actually reading it — editing status server-side would have done
// nothing visible until this was fixed. New backend: core/armor_manager.py
// + core/routes_armor.py (GET /api/armor, POST /api/armor/<id>/status).
// New badge colors for EN REPARACIÓN (amber) and DESTRUIDO (grey).
// mm-wiring.js, armor-detail-concepts-load.js, armor-svg-grid.js,
// mind-map.js, core-tabs-sleep-panel.js, and armor-grid.css all changed.
// v135: Found live that the armor status picker showed unconditionally
// instead of only after clicking the badge — armor-mindmap-detail.css
// loads after armor-grid.css, and its .core-persona-tier-buttons rule
// (display:flex, unconditional) beat this file's .armor-status-picker
// (display:none) at equal specificity, since the later stylesheet wins
// ties. Retargeted the hide/show rule at #armorStatusPicker (an id
// selector always outranks a class one) so it wins regardless of load
// order. armor-grid.css changed.
// v136: Chat's paperclip button now opens a small type menu (Fotos / PDF /
// Documentos) instead of the file picker directly. Only Fotos is wired to
// an actual accept="image/*" picker trigger — PDF/Documentos are disabled
// placeholders, staging the UI shape for when HUGO can actually read those
// too, without staging files nothing reads yet. No new backend, no change
// to HUGO's ability to see attachment content (still cosmetic staging
// only, per the existing comment in chat-render.js). index.html,
// chat-render.js, and controls-bar.css changed.
// v137: fixed a real (pre-existing, unrelated to v136) bug where the
// fixed bottom-left App Launcher's (#appLauncher, diamond-core.css) own
// layout box was as wide as its full icon row even while visually closed
// (opacity:0 but still pointer-events:auto), silently swallowing real
// mouse clicks aimed at anything underneath it in that screen region —
// including chat's paperclip button, which is why clicking it did
// nothing no matter how many times the app was restarted/cache-cleared.
// Fixed by giving .app-launcher pointer-events:none and re-enabling it
// only on .app-launcher-btn (the row already re-enables itself via
// .open). diamond-core.css changed.
//
// v138: backend-only bump (no ui/ files changed) — cache-busts after
// fixing HUGO's inline investigation asides (core/personalities/base.py)
// and the self-feeding module_error/task loop between core/situation.py
// and core/action_engine.py, both restarted and verified live this
// session.
// v141: ESTUDIO → INVESTIGACIÓN detail redesign (ui/js/estudio.js,
// ui/css/estudio.css) — certainty-graded hypothesis split + confidence
// gauge. Cache-busts because the previous open-window test still showed
// the old flat layout after a plain pkill/open cycle.
// v142: INVESTIGACIÓN detail restyle — "instrument console" treatment
// (hero band + gauge, hypothesis cards with confidence bars, checkmark-
// badge verdict, violet open-questions) replacing the plain first pass.
// v143: extended the console aesthetic to RESÚMENES' detail view (key
// points as item cards, conclusion as verdict badge) — generalized the
// previously investigación-only CSS classes to estudio-console-* so both
// share the same chrome (ui/css/estudio.css, ui/js/estudio.js).
// v144: RESÚMENES now generates and shows a real narrative paragraph
// (core.commands.generate_summary's new RESUMEN: prompt line, parsed by
// _parse_summary_output and rendered via .estudio-console-prose) instead
// of reading as bullet points only — distinguishes a resumen from
// ESQUEMAS' node-graph/bullet treatment. Existing saved summaries (no
// `narrative` field) just skip that section, same as before.
// v145: Armaduras' individual armor detail sheet (Diseño → Armaduras →
// click a model) restyled to the same estudio-console-* instrument-console
// aesthetic — hero band with status chip + hours readout + silhouette,
// innovaciones as green item-cards, limitaciones/evolución/specs as toned
// prose blocks (amber/violet/cyan), Controlar/Ver HUD moved into a rail
// panel (ui/index.html, ui/js/armor-detail-concepts-load.js,
// ui/css/armor-mindmap-detail.css). Old .detail-sec-title/-body/-list flat
// layout removed — confirmed unused elsewhere (Conceptuales' own detail
// view uses .armor-detail-left/-right/.detail-name-lg directly, untouched).
// v146: fixed can't-scroll bug in the new Armaduras detail console — flex
// stretch was forcing .armor-console to the viewport's height and clipping
// everything past it instead of letting .armor-detail-page scroll
// (ui/css/armor-mindmap-detail.css: align-self:flex-start).
// v147: extended the instrument-console aesthetic to ESQUEMAS and
// EXPLORACIONES detail views — schema nodes render as colored item-cards
// (type-coded: concept/question/connection/example/insight), open
// questions/connections_to_known get their own toned blocks; exploraciones
// gets a relevance gauge, prose content, and a rail with source link +
// mark-read button (replacing the old two-col/isLong heuristic). Removed
// now-dead .estudio-schema-node*/.estudio-detail-columns.two-col CSS
// (ui/js/estudio.js, ui/css/estudio.css).
// v148: extended the instrument-console aesthetic to NÚCLEO HUGO's
// Pensamiento (thinking/sleep-questions/reflections feeds → estudio-
// console-item-card) and Memoria (facts → estudio-console-list bullets,
// episodes → item-cards with a date/importance head) tabs, plus a shared
// subtle console-family left-accent touch on the base .info-row used
// across AJUSTES/Memoria/Módulos/Personas (ui/css/controls-bar.css). Diamond/
// main-menu visual untouched. Módulos/Personas/Mapa's own detail redesign
// deferred to a follow-up pass (real interactive widgets — priority
// stepper, block/build controls, tier picker — need a more careful hybrid
// treatment, not a drop-in swap).
// v149: finished the NÚCLEO HUGO console pass — Módulos/Personas' shared
// row/detail shell (.core-module-row/-detail) gets the same left-accent-
// tick + bordered-card treatment as everywhere else (zero interactivity
// changes: priority stepper/block/build/tier-picker markup untouched), and
// Mapa's node-click detail panel now renders via estudio-console-* building
// blocks (chip/prose/item-card) instead of its own bespoke kind/title/row
// classes, switched from personality-tinted --p-color to the fixed
// --accent cyan for consistency with the rest of the console family
// (ui/js/core-tabs-sleep-panel.js, ui/js/mind-map.js, ui/css/armor-
// mindmap-detail.css, ui/css/concepts.css). Diamond/main-menu untouched.
// v150: full instrument-console treatment for Módulos/Personas' expanded
// detail (hero band with status/tier chip + meta-readout, body-grid with
// main=description/tier-picker prose+item-cards, rail=priority/block/
// build controls or last-seen/delete — every interactive data-* attribute
// unchanged) and AJUSTES' info panel (grouped into Voz/Conexión/Build
// blocks inside the same console shell instead of one flat .info-row
// list). ui/js/core-tabs-sleep-panel.js, ui/js/chat-render.js, ui/css/
// armor-mindmap-detail.css. Diamond/main-menu untouched.
// v151: Conceptuales' INSPECCIONAR view (concepts-edit.js's
// _ensureConceptDesignDetail/_openConceptDesignDetail) moved off the old
// .armor-detail-left/-right/.detail-name-lg shell onto the same estudio-
// console-* hero/body-grid treatment as Armor Bay's own detail sheet —
// static design diagram now sits in the hero, description as prose.
// Removed the now-fully-dead .armor-detail-left/-right/.detail-name-lg/
// .detail-body CSS (ui/css/armor-mindmap-detail.css) and .concept-design-
// detail-desc (ui/css/concepts.css) — nothing referenced either anymore.
// v152: AJUSTES' section dividers (.settings-divider, "Funciones"/
// "Sistema") get the same glowing-dot marker as every estudio-console-
// section-label elsewhere — last remaining console-family touch in
// AJUSTES; toggles/forms/action buttons/status lines deliberately left
// untouched per scope (ui/css/controls-bar.css).
// v153: final console-family pass on chat + nav (the two most actively-
// used surfaces, deliberately left for last). Chat bubbles/input were
// already close to this language (angular clip-path, accent/gold coloring)
// so this is a light touch: .attach-chip switched from the one remaining
// rounded corner in the app to clip-path, .msg-error got a faint tint
// behind its border instead of a bare line, .msg-timing's letter-spacing
// matches the rest of the mono-caption family. Nav's active indicator
// (.nav-item.active) got a soft inset glow on its existing thin top
// border — no dot/pill added, respecting that bar's own documented "sharp,
// not a bubble" intent. #hugoDiamond and every diamond-*/boot-splash file
// were NOT touched, per the standing constraint (ui/css/chat.css,
// ui/css/controls-bar.css, ui/css/nav-personality-modal.css).
// v154: added a fancy ambient background to the main menu (#section-home)
// — 4 drifting glow orbs spread across the whole section, a slow diagonal
// HUD light-sweep pass, and a faint drifting star-dot field
// (.mm-atmosphere, ui/css/personality-nav.css, markup in ui/index.html).
// Distinct from and doesn't touch #hugoDiamond's own particle ring — spans
// the whole section rather than being centered on the diamond, and the
// diamond itself (and every diamond-*/boot-splash file) remains untouched.
// v155: fixed the main-menu light sweep (.mm-atmosphere-sweep) — v1
// animated transform:translateX(%) on a %-sized element, which resolves
// against the element's own width rather than the section's, so the
// traversal never lined up predictably and read as a diffuse smear.
// Redone as a background-position-driven gradient (thin bright core, soft
// falloff) and tuned down after feedback that the first pass (cycling
// every ~10s) felt distracting — now crosses once every ~42s and sits at
// roughly half the previous brightness (ui/css/personality-nav.css).
// v156: removed the main-menu diagonal light sweep entirely — stayed
// distracting even after two tuning passes (fixed traversal math, then
// halved brightness + slowed to a ~42s cycle). Glow orbs + star field
// stay as the section's ambient background (ui/index.html, ui/css/
// personality-nav.css).
// v157: Sistema panel console pass — hero-style header (status chip +
// display title + mono readout), RLD/CLR restyled as angular mono buttons
// (with appearance:none to kill the native button bezel bleeding through
// as a white texture), log entries now left-accent item rows instead of
// flat text lines (ui/index.html, ui/css/personality-nav.css).
// v158: added BUG HUNTER placeholder app — new launcher tile (ui/js/
// diamond-text-launcher.js) and #section-bughunter panel (ui/index.html),
// same "En desarrollo" treatment as CONTROL, no functionality yet.
// v159: built out BUG HUNTER's real front-end shell — Scope/Status/Scan/
// Hallazgos subtabs (ui/js/bughunter.js, ui/css/bughunter.css, new files
// added to PRECACHE below) plus a header auto-mode toggle. Mock/in-memory
// data only, no backend yet.
// v160: restyled BUG HUNTER's 4 panels to reuse ESTUDIO's Investigación
// detail "instrument console" chrome (.estudio-console/-hero/-body-grid/
// -rail/-item-card/-section-label) instead of plain .estudio-card-list —
// matches the aesthetic Joan confirmed as the redesign reference.
// v161: Hallazgos gained click-to-expand detail (description/repro steps/
// impact/fix, same slide-in recipe as #estudioDetail) plus a search/sort/
// filter row above the list (ui/index.html, ui/js/bughunter.js,
// ui/css/bughunter.css).
// v162: BUG HUNTER Phase 1 — wired to a real backend (core/bughunter_routes.py,
// GET/POST /api/bughunter*). Scope add/delete, Findings status change, and
// Auto Mode toggle all persist now; live-synced via 'bughunter_updated'
// socket event. Scan tab hits a real endpoint but the scan engine itself
// is still Phase 2. ui/js/bughunter.js, ui/css/bughunter.css changed.
// v163: BUG HUNTER Phase 2 — real scan engine (core/bughunter_scan.py,
// passive/read-only checks: headers, TLS, security.txt, sensitive paths,
// crt.sh subdomains), Ollama-drafted write-ups, background-thread
// execution with live progress via the new 'bughunter_scan_log' socket
// event. ui/js/bughunter.js changed (Scan tab now streams real progress
// and reflects an in-progress scan on both Scan and Status).
// v164: BUG HUNTER gained a 5th subtab, Supervisión — a live granular
// trace of every scan step (manual or auto-mode), fed by the same
// 'bughunter_scan_log' socket event the Scan tab already used. Frontend
// only, no backend change (ui/index.html, ui/js/bughunter.js).
// v165: BUG HUNTER gained a 6th subtab, Programas — a static, curated
// reference list of major bug bounty platforms (HackerOne, Bugcrowd,
// Intigriti, YesWeHack, Synack, Google/Microsoft/Apple's own programs).
// Reference only, does NOT add anything to Scope. Also raised Auto Mode's
// default pace: tick 300s→60s, default interval 4h→0.02h (~72s) — Joan
// wants it brisk once enabled, not paced like the ambient background
// loops (core/background_loops.py, core/bughunter_routes.py changed too,
// but those don't need a cache bump — noted here for the full picture).
// v166: Programas gained Sugerencias — Auto Mode now also runs a slow-paced
// (once/hour) discovery side activity (core/bughunter_scan.py's
// discover_program_suggestions(), reuses core.tools_search.search_web())
// that surfaces candidate bounty programs found on known platforms.
// NEVER auto-added to Scope — "Añadir a Scope" pre-fills the add form
// (domain left blank for Joan to fill in himself) rather than creating a
// real Scope entry on its own. New data/bughunter_suggestions.json, new
// POST /api/bughunter/suggestions/dismiss. ui/index.html, ui/js/bughunter.js
// changed (Programas tab restructured to body-grid: Sugerencias main,
// known platforms demoted to rail).
// v167: bughunter.js changed — "Añadir a Scope" now prefills domain from the
// suggestion's URL hostname instead of leaving it blank for manual entry.
// v168: bughunter.js/index.html/bughunter.css changed — retry the initial
// data load on socket connect (fixes a boot-race blank panel), loading
// placeholders instead of empty divs, and visible errors on scope-add
// validation/save failures instead of silent no-ops.
// v169: bughunter.js — the v168 'connect' listener could miss the socket's
// own fallback (Tailscale IP unreachable on this machine, JARVIS_API only
// corrected once connect_error's fallback chain resolves, possibly before
// this file's binder even runs). Replaced with a direct 2s poll-retry
// until the first load actually succeeds.
// v170: bughunter.js — added "resuelto" (auto) to BH_STATUS_LABELS and the
// Findings status filter dropdown, for findings the scan engine's own
// auto-resolve step now marks when a repeat scan no longer detects them.
// v171: bughunter.js — added "descartado" (Joan's manual false-positive/
// accepted-risk triage call) to BH_STATUS_LABELS and the Findings status
// filter dropdown.
// v172: bughunter.js — real fix for "Añadir a Scope" prefill getting wiped
// blank: _bhPromoteSuggestion's own dismiss-suggestion call triggers a
// 'bughunter_updated' socket event, which fires a full _loadBughunterData()
// reload moments later that used to rebuild the add-form from scratch
// (blank) since nothing preserved the just-set values. Added
// _bhScopeAddDraft — the form now always renders from (and writes back to
// on every keystroke) that module-level draft, so any reload landing
// mid-edit is harmless.
// v173: bughunter.js — SAFETY FIX. _bhPromoteSuggestion's domain-guessing
// (new URL(s.url).hostname) returned the bounty PLATFORM's own domain
// (hackerone.com/bugcrowd.com/intigriti.com/yeswehack.com) whenever a
// suggestion's URL was a listing page on that platform, not the target
// company's own site — confirmed live when "Doctolib bug bounty program"
// (a yeswehack.com listing) got saved to Scope with domain=yeswehack.com,
// an unauthorized third party Auto Mode would have scanned next. Added
// BH_PLATFORM_HOSTS guard — the domain field is now left blank (forcing
// manual entry) whenever the suggestion's URL hostname is one of the 4
// known platforms, instead of confidently guessing wrong.
// v174: bughunter.js — removed domain auto-guessing from
// _bhPromoteSuggestion entirely (superseding v173's platform-host
// allowlist patch). Any hostname-based guess off a suggestion's listing-
// page URL is either wrong (platform's own domain) or depends on an
// allowlist that's one missed platform away from the same mistake — not
// worth the risk against the one thing (Scope) that gates everything this
// app is allowed to touch. Domain is now always left blank for Joan to
// fill in himself, same as this feature's original 2026-08-18 design
// before the "prefill it" convenience request introduced the bug.
// v175: bughunter.js/bughunter.css — added a required "automation_allowed"
// checkbox to the Scope add-form. Found live 2026-08-18 that real bug
// bounty programs (Intigriti's "Exact" VDP, YesWeHack's BIND 9) explicitly
// prohibit automated/passive scanning tools in their rules of engagement
// — this forces a conscious "I checked the real rules" confirmation
// before any new Scope entry can be saved, enforced server-side too (see
// core.bughunter_routes.api_bughunter_add_scope) so it can't be bypassed
// by calling the route directly.
// v176: removed the Armor Design Studio / ArmorOS subsystem (armor grid,
// armor detail view, Diseño tab) — never applied to HUGO. PRECACHE dropped
// the deleted armor-svg-grid.js/armor-detail-concepts-load.js/
// design-studio.js/design-studio.css entries (were 404ing) and added the
// new concepts-load.js (armor-detail-concepts-load.js's concepts-only
// successor).
// v180: bootstrap-auth.js fix — actual root cause of the infinite
// "Cargando..." boot screen. The ui/js/*.js split from ui/app.js dropped
// the top-level call to _initLauncherSocket() that used to live at the
// bottom of app.js's auth-gate IIFE — the function was still defined
// correctly but nothing ever invoked it, so the launcher socket was never
// created and the boot state machine never moved off its initial state.
// Now called for real. (v179's connection.js guard fix is still correct
// and kept, but was never reachable without this.) Bump so clients drop
// their stale cached copy that never actually booted.
const CACHE = 'jarvis-v180';

// Assets to pre-cache on install so the icons/manifest load instantly
// offline. '/' (ui/index.html) is deliberately NOT in this list — see the
// fetch handler's network-first handling of navigation requests below.
// The CSS/JS split files ARE precached: they're separate non-navigate GET
// requests the page issues right after index.html, and the cache-first
// path further down would otherwise only pick them up after a first fetch.
const PRECACHE = [
  '/manifest.json', '/icon.svg', '/icon-192.png', '/icon-512.png',
  '/css/base.css', '/css/boot-splash.css', '/css/chat.css',
  '/css/controls-bar.css', '/css/diamond-toggles.css', '/css/diamond-core.css',
  '/css/personality-nav.css', '/css/main-menu-orb.css', '/css/main-menu-panels.css',
  '/css/armor-grid.css', '/css/armor-mindmap-detail.css', '/css/concepts.css',
  '/css/nav-personality-modal.css', '/css/estudio.css',
  '/js/bootstrap-auth.js', '/js/connection.js', '/js/boot-power-state.js',
  '/js/section-nav.js', '/js/status-diamond-grid.js', '/js/diamond-motion.js',
  '/js/diamond-text-launcher.js', '/js/core-tabs-sleep-panel.js', '/js/mind-map.js',
  '/js/personality-switch.js', '/js/chat-render.js', '/js/settings-updates.js',
  '/js/clock-boot-splash-wiring.js', '/js/mm-wiring.js',
  '/js/estudio.js',
];

// ── Install: pre-cache static assets ─────────────────────────────────────────
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE)
      .then(cache => cache.addAll(PRECACHE))
      .then(() => self.skipWaiting())   // activate immediately, don't wait for old SW to die
  );
});

// ── Activate: purge stale caches from previous versions ──────────────────────
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(k => k !== CACHE).map(k => caches.delete(k))
      ))
      .then(() => self.clients.claim())  // take control of existing open tabs
  );
});

// ── Fetch: network-first for the app page itself, cache-first for static
//    assets, pass-through for everything else ────────────────────────────────
//
// Root-cause bug fix: EVERY GET request — including '/', which serves
// ui/index.html and IS the entire app (there are no separate JS/CSS files
// to fall back on) — used to go through cache-first: `caches.match(...)`
// was checked BEFORE ever touching the network, and only fell through to
// `fetch()` on a cache miss. rebuild_app.sh's whole update flow (git pull
// -> bump this file's own CACHE constant -> rebuild/reinstall the Electron
// shell) genuinely worked correctly end-to-end — verified live: launcher.py
// serves the current on-disk ui/index.html byte-for-byte on every request,
// with a correct `Cache-Control: no-cache` (always-revalidate) header. The
// break was entirely client-side: a new Service Worker version installs
// and activates ASYNCHRONOUSLY, in the background, relative to the
// navigation that triggered the update check — it does not retroactively
// affect the page load already in flight. So relaunching the app right
// after a "successful" update could still be served by the PREVIOUS
// session's already-active SW, from ITS cache, before the new SW ever got
// a chance to take over — self.skipWaiting()/self.clients.claim() shrink
// that window but don't eliminate it, since they only run once the new SW
// itself has already been detected and installed.
//
// Navigation requests (Request.mode === 'navigate' — top-level page loads
// and reloads, exactly what Electron's mainWindow.loadURL() triggers) now
// always try the network FIRST, caching the result as a fallback for
// genuinely offline use — never serving a stale cached copy while online.
// This is the standard PWA pattern (network-first for the content that
// actually changes, cache-first only for the static app-shell assets in
// PRECACHE above) and removes the race entirely: the very next launch
// after an update always fetches the real, current file.
self.addEventListener('fetch', event => {
  const req = event.request;
  const url = req.url;

  // Never intercept SocketIO traffic, non-GET requests, or dynamic API calls
  if (req.method !== 'GET')        return;
  if (url.includes('/socket.io/')) return;
  if (url.includes('/api/'))       return;  // always fetch API responses fresh

  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req, { cache: 'no-store' })   // bypass the browser's own HTTP cache too, not just this SW's
        .then(response => {
          if (response.ok) {
            const clone = response.clone();
            caches.open(CACHE).then(cache => cache.put(req, clone));
          }
          return response;
        })
        // Only reached if the network request genuinely failed (offline) —
        // fall back to whatever was last cached for this exact URL, or '/'.
        .catch(() => caches.match(req).then(cached => cached || caches.match('/')))
    );
    return;
  }

  event.respondWith(
    caches.match(event.request).then(cached => {
      if (cached) return cached;

      // Not in cache — fetch from network and cache the response
      return fetch(event.request).then(response => {
        if (response.ok) {
          const clone = response.clone();
          caches.open(CACHE).then(cache => cache.put(event.request, clone));
        }
        return response;
      });
    })
  );
});
