// armor-detail-concepts-load.js — Armor detail panel DOM refs/close, view/sub-tab switching, and concept loading.
const detailBackBtn   = document.getElementById('detailBackBtn')
const detailName2     = document.getElementById('detailName2')
const detailBadge2    = document.getElementById('detailBadge2')
const detailBody2     = document.getElementById('detailBody2')
const detailSilWrap   = document.getElementById('detailSilWrap')

// ── CONTROLAR / VER HUD — placeholder actions, per-model message only,
// no real functionality yet. Explicit per-id lookup (not a range/switch)
// so each entry maps 1:1 to the spec and is easy to audit at a glance.
const _CONTROLAR_MESSAGES = {
  'model-0':  'Este modelo no dispone de sistemas electrónicos compatibles',
  'model-1':  'Este modelo no dispone de sistemas electrónicos compatibles',
  'model-2':  'Este modelo no dispone de sistemas electrónicos compatibles',
  'model-3':  'Este modelo no dispone de sistemas electrónicos compatibles',
  'model-4':  'Este modelo no dispone de sistemas electrónicos compatibles',
  'model-5':  'Este modelo no dispone de sistemas electrónicos compatibles',
  'model-6':  'Este modelo no tiene esta función',
  'model-7':  'Este modelo no tiene esta función',
  'model-8':  'Función en desarrollo — sistemas Arduino no compatibles aún',
  'model-9':  'Este modelo aún no está construido',
  'model-10': 'Este modelo aún no está construido',
  't45':      'Este modelo no tiene esta función',
}
const _HUD_MESSAGES = {
  'model-0':  'Este modelo no tiene esta función',
  'model-1':  'Este modelo no tiene esta función',
  'model-2':  'Este modelo no tiene esta función',
  'model-3':  'Este modelo no tiene esta función',
  'model-4':  'Este modelo no tiene esta función',
  'model-5':  'Este modelo no tiene esta función',
  'model-6':  'Este modelo no tiene esta función',
  'model-7':  'Este modelo no tiene esta función',
  'model-8':  'Función en desarrollo',
  'model-9':  'No disponible — modelo no construido',
  'model-10': 'No disponible — modelo no construido',
  't45':      'Este modelo no tiene esta función',
}

let _currentDetailModelId    = null
let _currentDetailModelRoman = null   // e.g. 'VIII' — derived from m.name, used by hud_context events
let _detailToastTimer        = null
let _detailSectionObserver   = null   // IntersectionObserver — see _setupDetailSectionObserver()
let _lastDetailSection       = null   // dedupe: only emit armor_section on an actual change

// Inline toast below the buttons — fades/expands in, auto-hides after a
// few seconds. Deliberately not a modal, per the spec.
function _showDetailToast(message) {
  const el = document.getElementById('detailActionToast')
  if (!el) return
  clearTimeout(_detailToastTimer)
  el.textContent = message
  el.classList.add('visible')
  _detailToastTimer = setTimeout(() => el.classList.remove('visible'), 4000)
}

// Section key → title, in render order. Keys are what hud_context's
// 'armor_section' events and PANTALLA ACTUAL both refer to a scrolled-to
// section by (see _setupDetailSectionObserver below). 'horas' moved out of
// this list into the hero's meta-readout (see _openDetail) — it's a single
// stat, not a scrollable section. `tone` picks the same green/amber/violet/
// cyan language ESTUDIO's console view already uses (confident/uncertain/
// open/neutral) — innovaciones reads as the "good news" (green), limitaciones
// as the caveat (amber), evolución as the forward-looking one (violet).
const _DETAIL_SECTION_DEFS = [
  { key: 'resumen',      title: 'Resumen',                     field: 'descripcion',  type: 'prose', tone: 'neutral' },
  { key: 'innovaciones', title: 'Innovaciones clave',          field: 'innovaciones', type: 'list',  tone: 'confident' },
  { key: 'limitaciones', title: 'Limitaciones conocidas',      field: 'limitaciones', type: 'prose', tone: 'uncertain' },
  { key: 'evolucion',    title: 'Evolución',                   field: 'evolucion',    type: 'prose', tone: 'open' },
  { key: 'specs',        title: 'Materiales y specs técnicas', field: 'specs',        type: 'prose', tone: 'neutral' },
]

// Editable status — click the badge in the detail view to reveal this
// 5-button picker (matches ui/js/core-tabs-sleep-panel.js's Personas
// trust-tier picker: same .core-persona-tier-btn/-buttons CSS, same
// segmented-row interaction, reused rather than inventing a third editing
// widget style in this app). Deliberately NOT offering the pre-existing
// 'NO COMPLETADO' value (model-7, as of this writing) — see
// core/armor_manager.py's own comment on why that's left to fade out
// rather than being an option here.
const ARMOR_STATUS_OPTIONS = ['COMPLETADO', 'EN CONSTRUCCIÓN', 'EN REPARACIÓN', 'DESTRUIDO', 'NO CONSTRUIDO']

function _renderArmorStatusBadge(m) {
  return `
    <div class="armor-badge ${_badgeClass(m.status)}" id="armorStatusBadge" title="Editar estado">${esc(m.status)}</div>
    <div class="core-persona-tier-buttons armor-status-picker" id="armorStatusPicker">
      ${ARMOR_STATUS_OPTIONS.map(s => `
        <button class="core-persona-tier-btn${s === m.status ? ' active' : ''}" data-armor-status="${esc(s)}">${esc(s)}</button>
      `).join('')}
    </div>
  `
}

// POST the new status, then re-fetch fresh (same "fetch fresh, re-render
// everything, simplicity over diffing" approach already used for Módulos/
// Personas) and re-render BOTH the still-open detail view and whichever
// grid tab is current, so going back afterward already shows the updated
// badge instead of stale data from before the edit.
async function _changeArmorStatus(modelId, status) {
  try {
    await fetch(`${JARVIS_API}/api/armor/${encodeURIComponent(modelId)}/status`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status }),
    })
  } catch { /* re-fetch below reflects whatever the backend actually ended up with */ }

  const models  = await _fetchArmorModels()
  const updated = [...models.primarios, ...models.paralelos].find(m => m.id === modelId)
  if (updated) _openDetail(updated)
  if (_currentSub === 'primarios' || _currentSub === 'paralelos') {
    _renderArmorGrid(_currentSub === 'primarios' ? models.primarios : models.paralelos)
  }
}

function _openDetail(m) {
  _currentDetailModelId    = m.id
  _currentDetailModelRoman = (m.name || '').replace(/^Modelo\s+/i, '')
  _lastDetailSection       = null
  document.getElementById('detailActionToast').classList.remove('visible')
  detailName2.textContent = m.nickname ? `${m.name} — ${m.nickname}` : m.name
  detailBadge2.innerHTML  = _renderArmorStatusBadge(m)
  document.getElementById('armorStatusBadge').addEventListener('click', () => {
    document.getElementById('armorStatusPicker').classList.toggle('open')
  })
  detailBadge2.querySelectorAll('[data-armor-status]').forEach(btn => {
    btn.addEventListener('click', () => _changeArmorStatus(m.id, btn.dataset.armorStatus))
  })
  detailSilWrap.innerHTML = _armorDiagramSVG(m.id)
  document.getElementById('detailHoursReadout').textContent = m.hours ? m.hours.toUpperCase() : ''

  // Build spec sections in the order requested — only show ones with
  // content. 'list' sections (innovaciones) split on ". " into individual
  // estudio-console-item-card chips, same treatment as ESTUDIO's hypothesis/
  // key-point cards; everything else renders as flowing prose
  // (estudio-console-prose), same "a summary reads as text" treatment
  // RESÚMENES got. `.detail-sec` is kept as a marker class purely so
  // _setupDetailSectionObserver's existing selector below still finds these
  // — it carries no styling of its own anymore.
  detailBody2.innerHTML = _DETAIL_SECTION_DEFS
    .map(s => ({ ...s, body: m[s.field] }))
    .filter(s => s.body)
    .map(s => {
      if (s.type === 'list') {
        const items = s.body.split(/\.\s+/).map(x => x.trim().replace(/\.$/, '')).filter(Boolean)
        return `
          <div class="estudio-console-block detail-sec ${s.tone}" data-section="${s.key}">
            <div class="estudio-console-section-label ${s.tone}"><span class="dot"></span>${esc(s.title)}</div>
            ${items.map(i => `<div class="estudio-console-item-card"><div class="estudio-console-item-text" style="margin-bottom:0;">${esc(i)}</div></div>`).join('')}
          </div>`
      }
      return `
        <div class="estudio-console-block detail-sec ${s.tone}" data-section="${s.key}">
          <div class="estudio-console-section-label ${s.tone}"><span class="dot"></span>${esc(s.title)}</div>
          <div class="estudio-console-prose">${esc(s.body)}</div>
        </div>`
    })
    .join('')

  armorDetailView.classList.add('active')
  _markUiInteraction()
  _emitUserActivity('armor_detail', 'opening', { model: m.id, name: m.name })
  _emitHudContext({
    type: 'armor_detail',
    model: _currentDetailModelRoman,
    name: m.nickname || m.name,
    section: 'detail',
    data: {
      id: m.id, name: m.name, nickname: m.nickname || null,
      hours: m.hours, status: m.status, descripcion: m.descripcion,
      innovaciones: m.innovaciones, limitaciones: m.limitaciones,
      evolucion: m.evolucion, specs: m.specs,
    },
  })
  _setupDetailSectionObserver()
}

// Reports which section of the open armor's detail view is actually on
// screen, as Joan scrolls — a lightweight hud_context refinement of the
// armor_detail context above (core/server.py merges it in rather than
// replacing the full armor data — see its 'hud_context' handler). Picks
// the most-visible section on every intersection change; re-created each
// time _openDetail runs so a stale observer never lingers across models.
function _setupDetailSectionObserver() {
  if (_detailSectionObserver) { _detailSectionObserver.disconnect(); _detailSectionObserver = null }
  const root = document.querySelector('.armor-detail-page')
  if (!root) return
  _detailSectionObserver = new IntersectionObserver((entries) => {
    const visible = entries.filter(e => e.isIntersecting)
    if (!visible.length) return
    visible.sort((a, b) => b.intersectionRatio - a.intersectionRatio)
    const section = visible[0].target.dataset.section
    if (!section || section === _lastDetailSection) return
    _lastDetailSection = section
    _emitHudContext({ type: 'armor_section', model: _currentDetailModelRoman, section })
  }, { root, threshold: [0.4, 0.6] })
  detailBody2.querySelectorAll('.detail-sec[data-section]').forEach(el => _detailSectionObserver.observe(el))
}

function _closeDetailView() {
  armorDetailView.classList.remove('active')
  if (_detailSectionObserver) { _detailSectionObserver.disconnect(); _detailSectionObserver = null }
  _emitHudContext({ type: 'idle', section: _ACTIVITY_SECTION_MAP[_currentSection] || _currentSection })
}

detailBackBtn.addEventListener('click', _closeDetailView)

document.getElementById('detailControlarBtn').addEventListener('click', () => {
  if (_currentDetailModelId === 'model-8') {
    document.getElementById('detailLightControls').classList.toggle('open')
    return
  }
  _showDetailToast(_CONTROLAR_MESSAGES[_currentDetailModelId] || 'Función no disponible')
})
document.getElementById('detailHudBtn').addEventListener('click', () => {
  _showDetailToast(_HUD_MESSAGES[_currentDetailModelId] || 'Función no disponible')
})

// Modelo 8's chest LED — POSTs to the backend, which writes 'h'/'l'/'b' over
// BLE to the ESP32 (armor_light.py / armoros/core/src/main.cpp). 'baliza'
// starts the firmware's beacon blink loop (on/off/on, then a pause); it
// keeps running until 'on' or 'off' is sent, same as the firmware side.
const _LIGHT_STATE_LABELS = { on: 'encendida', off: 'apagada', baliza: 'en modo baliza' }
async function _setModel8Light(state) {
  try {
    const res  = await fetch(`${JARVIS_API}/api/armor/model-8/light`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ state }),
    })
    const data = await res.json()
    _showDetailToast(data.ok ? `Luz del reactor ${_LIGHT_STATE_LABELS[state]}` : (data.error || 'Error al controlar la luz'))
  } catch {
    _showDetailToast('Error al controlar la luz')
  }
}
document.getElementById('detailLightOnBtn').addEventListener('click', () => _setModel8Light('on'))
document.getElementById('detailLightOffBtn').addEventListener('click', () => _setModel8Light('off'))
document.getElementById('detailLightBalizaBtn').addEventListener('click', () => _setModel8Light('baliza'))

// ── Sub-tab switching (Primarios | Paralelos | Conceptuales | Diseño) ───────
// Bug fix: NÚCLEO LIRA's own sub-tabs (Estado/Pensamiento/Memoria/Mapa,
// #section-core) share this exact same .armor-subtab CSS class — an
// unscoped `.armor-subtab` selector here matched BOTH sets of tabs. That
// meant every CORE tab silently got a SECOND click handler wired below
// (armorSubtabs.forEach(btn => ...)) calling _switchSubTab(btn.dataset.sub)
// — undefined for a CORE tab, since those use data-core-sub, not data-sub
// — which then did `armorSubtabs.forEach(b => b.classList.toggle('active',
// b.dataset.sub === sub))`: b.dataset.sub === undefined is TRUE for every
// CORE tab (none of them have data-sub at all), so this second handler
// re-activated ALL FOUR of them right after _switchCoreSubTab() (CORE's
// own, correctly-scoped handler — see that function) had just set only
// the clicked one — the exact "multiple tabs active at once" bug. Scoping
// this query to #section-armor excludes CORE's tabs entirely, so they
// only ever get their own single, correct click handler.
const armorSubtabs  = document.querySelectorAll('#section-armor .armor-subtab')
const armorGridWrap = document.getElementById('armorGridWrap')
const conceptPanel  = document.getElementById('conceptPanel')
const designPanel   = document.getElementById('designPanel')

let _currentSub = 'primarios'

async function _switchSubTab(sub) {
  _currentSub = sub
  armorSubtabs.forEach(b => b.classList.toggle('active', b.dataset.sub === sub))

  const isGrid = sub === 'primarios' || sub === 'paralelos'
  armorGridWrap.style.display = isGrid ? '' : 'none'
  conceptPanel.classList.toggle('active', sub === 'conceptuales')
  designPanel.classList.toggle('active', sub === 'diseno')   // placeholder only — no data to load

  _markUiInteraction()
  _emitUserActivity(sub === 'conceptuales' ? 'concepts' : 'armor', 'navigate', { subtab: sub })

  if (sub === 'conceptuales') {
    _renderConcepts()   // show the current cache immediately, no empty-state flash
    _fetchConcepts()    // then refresh from the backend in case it changed elsewhere
  } else if (isGrid) {
    const models = await _fetchArmorModels()
    _renderArmorGrid(sub === 'primarios' ? models.primarios : models.paralelos)
  }
}

armorSubtabs.forEach(btn => {
  btn.addEventListener('click', () => _switchSubTab(btn.dataset.sub))
})

// ── Diseño's own sub-toggle (Armaduras | Mecanismos | HUDs) — three
// placeholder empty states, no data to load yet. Scoped to #designPanel so
// this never collides with any other .design-subtab-shaped control. ─────────
const designSubtabs = document.querySelectorAll('#designPanel .design-subtab')

function _switchDesignSub(sub) {
  designSubtabs.forEach(b => b.classList.toggle('active', b.dataset.designSub === sub))
  document.querySelectorAll('#designPanel .design-sub-content').forEach(panel => {
    panel.classList.toggle('active', panel.dataset.designContent === sub)
  })
}

designSubtabs.forEach(btn => {
  btn.addEventListener('click', () => _switchDesignSub(btn.dataset.designSub))
})

// ── Armor section reference (always .active inside #section-armor) ──────────
const armorSection = document.getElementById('armorSection')

// _switchView kept for any internal callers; now delegates to switchSection.
function _switchView(view) {
  switchSection(view === 'armor' ? 'armor' : 'chat')
  if (view !== 'armor') _closeDetailView()
}

// ── Conceptuales — backend-persisted list ───────────────────────────────────
// Source of truth is now data/concepts.json via GET/POST /api/concepts
// (core/server.py) instead of this browser's localStorage, so concepts
// survive reinstalls/other devices and are never tied to a single tab.
// localStorage is kept ONLY as a temporary offline fallback (see
// _loadConceptsFallback / _saveConcepts below) for when the backend is
// unreachable — it migrates the old 'jarvis_concepts_v1' key for that case.
const CONCEPT_KEY = 'jarvis_concepts'
;(function _migrateConceptKey() {
  const legacy = localStorage.getItem('jarvis_concepts_v1')
  if (legacy && !localStorage.getItem(CONCEPT_KEY)) {
    localStorage.setItem(CONCEPT_KEY, legacy)
    localStorage.removeItem('jarvis_concepts_v1')
  }
})()

// In-memory cache of the concepts list. Every UI read (_renderConcepts,
// _beginEdit, the save/delete handlers) reads this synchronously; it is kept
// current by _fetchConcepts() (backend → cache) and _saveConcepts() (cache
// updated immediately, then persisted to the backend).
let _conceptsCache = []

// Offline-only fallback — used solely when the backend can't be reached.
function _loadConceptsFallback() {
  try { return JSON.parse(localStorage.getItem(CONCEPT_KEY) || '[]') }
  catch { return [] }
}

// Synchronous accessor for the rest of the Conceptuales UI code.
function _loadConcepts() {
  return _conceptsCache
}

// Ensures every concept has a 'type' — missing/anything-but-'general'
// defaults to 'armor', since existing concepts (saved before this field
// existed) must migrate to 'armor' per spec. Applied once wherever
// concepts enter the cache (below), not on every read, so _conceptsCache
// itself is always the normalized source of truth for the rest of the UI.
function _normalizeConceptTypes(arr) {
  return arr.map(c => (c.type === 'general' ? c : { ...c, type: 'armor' }))
}

// Pull the full list from the backend and refresh the cache + rendered list.
// Called on page load and on every socket (re)connect so this tab reflects
// concepts saved elsewhere. Falls back to localStorage if the backend is
// unreachable, per the "never lose concepts" requirement.
async function _fetchConcepts() {
  try {
    const res = await fetch(`${JARVIS_API}/api/concepts`)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data = await res.json()
    _conceptsCache = _normalizeConceptTypes(data.concepts || [])
  } catch (e) {
    console.warn('[Concepts] Backend unreachable, falling back to localStorage:', e)
    _conceptsCache = _normalizeConceptTypes(_loadConceptsFallback())
  }
  _renderConcepts()
}

// Persist the full list. Updates the cache immediately (so the UI never waits
// on the network) and mirrors to localStorage as an offline fallback copy,
// then POSTs to the backend so data/concepts.json — and LIRA's live memory,
// via core/commands.reload_concepts() — stay in sync. Covers create, edit and
// delete, since all three funnel through this function.
async function _saveConcepts(arr) {
  _conceptsCache = arr
  localStorage.setItem(CONCEPT_KEY, JSON.stringify(arr))   // offline fallback only
  try {
    const res = await fetch(`${JARVIS_API}/api/concepts`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ concepts: arr }),
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
  } catch (e) {
    console.warn('[Concepts] Failed to reach backend — change kept in localStorage fallback only:', e)
  }
}

// Kick off an initial load as soon as the script runs, so the cache is
// populated even before the SocketIO connection (which re-fetches on
// 'connect') finishes resolving the right backend URL.
_fetchConcepts()

// -1 = create mode; N = index of concept currently being edited
let _editIdx = -1
// Index of concept pending deletion (set when ✕ clicked; cleared on confirm/cancel)
let _pendingDeleteIdx = -1

// ── Concept create/edit modal — open/close + title swap ─────────────────────
// Everything below the field-reset logic is new chrome around the same
// #cptName/#cptDesc/#cptStatus/#cptSave/#cptCancel elements the old inline
// form used; the save/edit/delete business logic elsewhere is untouched.
const conceptModalOverlay = document.getElementById('conceptModalOverlay')
const cptModalTitle       = document.getElementById('cptModalTitle')

function _openConceptModal() {
  conceptModalOverlay.classList.add('open')
  document.getElementById('cptName').focus()
  // Baseline for the unsaved-changes check below — taken AFTER the caller
  // (either the "+ Nuevo Concepto" handler, which resets fields to empty
  // via _cancelEdit() first, or _beginEdit(), which fills them from the
  // existing concept) has already set the fields, so this always captures
  // the correct "nothing changed yet" starting point for either mode.
  _captureConceptSnapshot()
}
