// design-studio.js — Armor Design Studio (Diseño → Armaduras).
// Phase 1: session picker + workspace (chat + live SVG diagram + save).
// Phase 2: interactive per-zone design — click a zone to open its detail
// panel (material/mecanismo/estética/notas/estado), ask LIRA for 3 options,
// hand a zone to LIRA via chat, cross-zone consistency checks, and a
// DISEÑO COMPLETO state once all 7 zones are 'diseñado'.
// Phase 2.5: a parts drawer (cajón de piezas) on the left of the workspace
// — historical parts from armor_knowledge.json + LIRA-documented/session-
// reused parts, drag-or-click onto a zone or onto a suggestion option —
// and the zone-suggestions flow now always surfaces exactly 3 A/B/C option
// cards (from both 'Pedir sugerencia' and 'Diseñar con LIRA'), each with a
// mini SVG preview and SELECCIONAR/MODIFICAR/DESCARTAR, with a feedback-
// driven regenerate when all 3 are discarded.
//
// Lives entirely inside [data-design-content="armaduras"] (see
// ui/index.html), which armor-detail-concepts-load.js's own
// _switchDesignSub() already shows/hides via .design-sub-content.active —
// this file never needs to hook into that switcher, it just renders inside
// whatever's already visible or not. Self-contained: own fetch calls to
// core/design_routes.py, own in-memory state, no coupling to estudio.js or
// concepts-edit.js beyond sharing the same visual language. The zone detail
// panel and progress/completion UI are built dynamically here (not in
// index.html) — same reasoning as the picker/workspace markup in Phase 1
// stayed in index.html: this batch of Phase 2 additions was scoped to
// design-studio.js/.css only, so new DOM is injected at init time instead.

const ZONES = [
  { key: 'helmet',    label: 'Casco' },
  { key: 'shoulders', label: 'Hombreras' },
  { key: 'chest',     label: 'Pecho' },
  { key: 'arms',      label: 'Brazos' },
  { key: 'waist',     label: 'Cintura' },
  { key: 'legs',      label: 'Piernas' },
  { key: 'boots',     label: 'Botas' },
]
const ZONE_LABEL = Object.fromEntries(ZONES.map(z => [z.key, z.label]))

const COMMON_MATERIALS = [
  'Cartulina', 'Cartón', 'Madera', 'EVA Foam', 'Fibra de vidrio',
  'Plástico ABS', 'Vinilo', 'Tela', 'Metal', 'Espuma XPS', 'Impresión 3D (PLA)',
]

const ZONE_STATUSES = [
  { key: 'diseñado',   label: 'Diseñado' },
  { key: 'pendiente',  label: 'Pendiente' },
  { key: 'descartado', label: 'Descartado' },
]

// Drawer category headers — plural, per spec ("CASCOS, HOMBROS, PECHOS...").
const DRAWER_CATEGORY_LABEL = {
  helmet: 'CASCOS', shoulders: 'HOMBROS', chest: 'PECHOS', arms: 'BRAZOS',
  waist: 'CINTURAS', legs: 'PIERNAS', boots: 'BOTAS',
}

// Same path data as the live diagram in index.html — reused here to draw
// small dimmed full-silhouette previews for the 3 option cards (see
// _dsMiniSilhouetteSvg). Kept as a separate copy rather than reading it
// back out of the live SVG: these render off-DOM (inside cards that don't
// exist until a suggestion batch comes back), and duplicating ~7 short
// path strings is simpler than plumbing a shared source across two files.
const ZONE_SVG_PATHS = {
  helmet:    ['M120,8 L142,18 L146,40 L138,56 L102,56 L94,40 L98,18 Z'],
  shoulders: ['M50,60 L96,58 L94,90 L70,98 L46,88 Z', 'M190,60 L144,58 L146,90 L170,98 L194,88 Z'],
  chest:     ['M92,60 L148,60 L156,140 L150,168 L90,168 L84,140 Z'],
  arms:      ['M46,88 L72,96 L78,180 L74,255 L54,268 L34,255 L30,180 Z', 'M194,88 L168,96 L162,180 L166,255 L186,268 L206,255 L210,180 Z'],
  waist:     ['M90,168 L150,168 L146,205 L94,205 Z'],
  legs:      ['M94,205 L118,205 L120,330 L104,338 L86,332 L84,220 Z', 'M146,205 L122,205 L120,330 L136,338 L154,332 L156,220 Z'],
  boots:     ['M84,332 L120,330 L124,362 L108,372 L78,370 L74,345 Z', 'M156,332 L120,330 L116,362 L132,372 L162,370 L166,345 Z'],
}

const OPTION_LABELS = ['OPCIÓN A', 'OPCIÓN B', 'OPCIÓN C']
const OPTION_COLORS = ['#f0c040', '#3fa9f5', '#a97cf0']

// Autopilot's "style reference" dropdown (Phase 3) — same model roster as
// data/armor_knowledge.json's own model names. Kept as a plain string list
// here (no fetch) since it's just fed back as a phrase into the
// constraints text (core.commands.run_autopilot_zone), not looked up
// structurally client-side.
const AUTOPILOT_MODEL_REFS = [
  'Modelo 0', 'Modelo I', 'Modelo II', 'Modelo III', 'Modelo IV', 'Modelo V',
  'Modelo VI', 'Modelo VII', 'Modelo VIII', 'Modelo IX', 'Modelo X', 'T-45',
]

let _dsDesigns = []
let _dsCurrent = null      // in-memory active design record
let _dsActiveZone = null
let _dsAutosaveTimer = null
let _dsSuggestionsCache = {} // zone -> last-fetched 3 options (mutable, carries .discarded), cleared per zone switch
let _dsPartsLibrary = []     // flat array, all zones — fetched once per workspace open
let _dsDrawerCollapsed = true
let _dsAutopilotRunning = false
// Explicit completion gate for review mode — only flipped true once the
// 'autopilot_complete' event fires (see _dsRunAutopilot), never by review
// mode's own call chain. This is a belt-and-suspenders guard: _dsStartReview
// already only had one caller (the end of the autopilot loop), but nothing
// previously stopped _dsRenderReviewStep from being invoked before real
// content existed if that call chain were ever extended — this flag makes
// "review only after genuine completion" an explicit, checkable invariant
// instead of an implicit consequence of call order.
let _dsAutopilotComplete = false
let _dsReviewQueue = []      // zone keys still awaiting review, in order — Phase 3
// Conceptuales integration — the "ts" of the linked data/concepts.json
// entry (see core/design_routes.py's _new_design_skeleton comment on
// design.concept_ts). null for a design never saved to Conceptuales yet.
let _dsLinkedConceptTs = null

// ── DOM refs (queried once the section markup is in the page) ──────────────
let _dsEl = {}

function _dsQueryEls() {
  _dsEl = {
    root:        document.getElementById('designStudioRoot'),
    picker:      document.getElementById('dsPicker'),
    newBtn:      document.getElementById('dsNewBtn'),
    sessionList: document.getElementById('dsSessionList'),
    sessionLabel:document.getElementById('dsSessionListLabel'),
    empty:       document.getElementById('dsEmptyState'),
    workspace:   document.getElementById('dsWorkspace'),
    backBtn:     document.getElementById('dsBackBtn'),
    chatLog:     document.getElementById('dsChatLog'),
    zoneReadout: document.getElementById('dsActiveZoneReadout'),
    chatInput:   document.getElementById('dsChatInput'),
    sendBtn:     document.getElementById('dsSendBtn'),
    nameInput:   document.getElementById('dsNameInput'),
    saveBtn:     document.getElementById('dsSaveBtn'),
    saveStatus:  document.getElementById('dsSaveStatus'),
    suggestOverlay: document.getElementById('dsNameSuggestOverlay'),
    suggestOptions: document.getElementById('dsNameSuggestOptions'),
    diagramPanel: document.querySelector('.ds-diagram-panel'),
    workspaceMain: document.querySelector('.ds-workspace-main'),
  }
}

function _dsSkeletonZones() {
  const zones = {}
  ZONES.forEach(z => { zones[z.key] = { material: '', mechanism: '', aesthetic_notes: '', notes: '', status: 'pendiente', lira_contribution: false, reasoning: '', locked: false } })
  return zones
}

function _dsEsc(s) {
  const d = document.createElement('div')
  d.textContent = s
  return d.innerHTML
}

// ── Session picker ──────────────────────────────────────────────────────────
async function _dsFetchDesigns() {
  try {
    const res = await fetch('/api/designs')
    const data = await res.json()
    _dsDesigns = Array.isArray(data.designs) ? data.designs : []
  } catch {
    _dsDesigns = []
  }
}

function _dsDesignedCount(design) {
  const zones = design.zones || {}
  return Object.values(zones).filter(z => z && z.status === 'diseñado').length
}

function _dsZoneProgress(design) {
  return `${_dsDesignedCount(design)}/${ZONES.length}`
}

function _dsRenderPicker() {
  _dsEl.sessionList.innerHTML = ''
  const hasSessions = _dsDesigns.length > 0
  _dsEl.sessionLabel.style.display = hasSessions ? '' : 'none'
  _dsEl.empty.style.display = hasSessions ? 'none' : ''

  _dsDesigns.forEach(design => {
    const card = document.createElement('div')
    card.className = 'ds-session-card'
    const dateStr = design.updated_at ? new Date(design.updated_at).toLocaleDateString('es-ES') : ''
    card.innerHTML = `
      <span class="ds-session-name${design.name ? '' : ' untitled'}">${design.name ? _dsEsc(design.name) : 'Diseño sin nombre'}</span>
      <span class="ds-session-meta">
        <span class="ds-session-progress">${_dsZoneProgress(design)}</span>
        <span>${dateStr}</span>
      </span>
    `
    card.addEventListener('click', () => _dsOpenDesign(design))
    _dsEl.sessionList.appendChild(card)
  })
}

async function _dsStartNewDesign() {
  const skeleton = { name: '', status: 'en_progreso', zones: _dsSkeletonZones(), notes: '', lira_suggestions: [], conversation: [], concept_ts: null }
  try {
    const res = await fetch('/api/designs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(skeleton),
    })
    const data = await res.json()
    _dsCurrent = data.design
  } catch {
    _dsCurrent = { id: null, name: '', status: 'en_progreso', zones: _dsSkeletonZones(), notes: '', lira_suggestions: [], conversation: [], concept_ts: null }
  }
  _dsLinkedConceptTs = null
  _dsOpenWorkspace()
}

// 'CREAR DISEÑO' on a Conceptuales card (see concepts-edit.js) — same as
// _dsStartNewDesign but pre-named with the concept's own name and already
// linked to it, so the very first 'GUARDAR EN ESTUDIO' updates that same
// concept instead of creating a second one.
async function _dsStartNewDesignForConcept(name, conceptTs) {
  const skeleton = { name: name || '', status: 'en_progreso', zones: _dsSkeletonZones(), notes: '', lira_suggestions: [], conversation: [], concept_ts: conceptTs }
  try {
    const res = await fetch('/api/designs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(skeleton),
    })
    const data = await res.json()
    _dsCurrent = data.design
  } catch {
    _dsCurrent = { id: null, name: name || '', status: 'en_progreso', zones: _dsSkeletonZones(), notes: '', lira_suggestions: [], conversation: [], concept_ts: conceptTs }
  }
  _dsLinkedConceptTs = conceptTs
  _dsOpenWorkspace()
}

async function _dsFetchDesignById(id) {
  try {
    const res = await fetch('/api/designs')
    const data = await res.json()
    return (data.designs || []).find(d => d.id === id) || null
  } catch {
    return null
  }
}

// 'EDITAR DISEÑO' on a Conceptuales card — loads that concept's existing
// design, all zones/decisions, fully editable. `conceptTs` is a fallback
// link only used if the design itself somehow predates concept_ts.
async function _dsOpenDesignById(designId, conceptTs) {
  const design = await _dsFetchDesignById(designId)
  if (!design) return false
  _dsOpenDesign(design)
  if (!_dsLinkedConceptTs && conceptTs) _dsLinkedConceptTs = conceptTs
  return true
}

function _dsOpenDesign(design) {
  _dsCurrent = JSON.parse(JSON.stringify(design))
  // Backfill Phase 2 zone fields for designs saved before this field existed.
  ZONES.forEach(z => {
    _dsCurrent.zones = _dsCurrent.zones || {}
    if (!_dsCurrent.zones[z.key]) _dsCurrent.zones[z.key] = { material: '', mechanism: '', aesthetic_notes: '', notes: '', status: 'pendiente', lira_contribution: false, reasoning: '', locked: false }
    if (_dsCurrent.zones[z.key].notes === undefined) _dsCurrent.zones[z.key].notes = ''
    if (_dsCurrent.zones[z.key].reasoning === undefined) _dsCurrent.zones[z.key].reasoning = ''
    if (_dsCurrent.zones[z.key].locked === undefined) _dsCurrent.zones[z.key].locked = false
  })
  _dsLinkedConceptTs = _dsCurrent.concept_ts || null
  _dsOpenWorkspace()
}

function _dsShowPicker() {
  _dsStopAutosave()
  _dsEl.picker.style.display = ''
  _dsEl.workspace.classList.remove('active')
  _dsFetchDesigns().then(_dsRenderPicker)
}

function _dsOpenWorkspace() {
  _dsActiveZone = null
  _dsSuggestionsCache = {}
  _dsReviewQueue = []
  _dsAutopilotRunning = false
  _dsAutopilotComplete = false
  _dsEl.picker.style.display = 'none'
  _dsEl.workspace.classList.add('active')
  _dsEl.nameInput.value = _dsCurrent.name || ''
  _dsRenderChatLog()
  _dsRenderDiagram()
  _dsUpdateZoneReadout()
  _dsCloseZonePanel()
  _dsRenderProgress()
  _dsEnsurePartsDrawer()
  _dsFetchPartsLibrary()
  document.getElementById('dsReviewOverlay')?.classList.remove('open')
  document.getElementById('dsAutopilotProgress')?.classList.remove('open')
  document.getElementById('dsAutopilotDialog')?.classList.remove('open')
  // Dismiss the persistent-bar indicator when Joan opens the workspace
  // AFTER a run finished (the completion state, not mid-run — the in-
  // workspace progress overlay above already covers the mid-run case).
  if (!_dsAutopilotRunning) _dsHideApBarIndicator()
  document.getElementById('dsApToast')?.classList.remove('open')
  _dsStartAutosave()
}

// ── SVG armor diagram ────────────────────────────────────────────────────
function _dsRenderDiagram() {
  ZONES.forEach(z => {
    const el = document.getElementById(`dsZone_${z.key}`)
    const label = document.getElementById(`dsZoneLabel_${z.key}`)
    if (!el) return
    const zoneData = (_dsCurrent.zones || {})[z.key] || {}
    const status = zoneData.status || 'pendiente'
    const isActive = _dsActiveZone === z.key
    // Autopilot has proposed content but Joan hasn't reviewed it yet — see
    // .ds-zone.ap-ready's own comment in design-studio.css.
    const isApReady = status === 'pendiente' && zoneData.lira_contribution && !!zoneData.material
    el.classList.toggle('designed', status === 'diseñado' && !isActive)
    el.classList.toggle('ap-ready', isApReady && !isActive)
    el.classList.toggle('discarded', status === 'descartado' && !isActive)
    el.classList.toggle('active', isActive)
    if (label) {
      label.classList.toggle('designed', status === 'diseñado' && !isActive)
      label.classList.toggle('ap-ready', isApReady && !isActive)
      label.classList.toggle('discarded', status === 'descartado')
      label.classList.toggle('active', isActive)
    }
  })
}

function _dsSelectZone(zoneKey) {
  _dsActiveZone = zoneKey
  _dsRenderDiagram()
  _dsUpdateZoneReadout()
  _dsRenderZonePanel(zoneKey)
}

function _dsUpdateZoneReadout() {
  _dsEl.zoneReadout.textContent = _dsActiveZone
    ? `Zona activa: ${ZONE_LABEL[_dsActiveZone]}`
    : 'Selecciona una zona en el diagrama para empezar'
}

// ── Parts drawer (cajón de piezas) — Phase 2.5 ──────────────────────────
function _dsEnsurePartsDrawer() {
  let drawer = document.getElementById('dsPartsDrawer')
  if (drawer) return drawer

  drawer = document.createElement('div')
  drawer.id = 'dsPartsDrawer'
  drawer.className = 'ds-parts-drawer collapsed'
  drawer.innerHTML = `
    <button class="ds-drawer-toggle" id="dsDrawerToggle" title="Mostrar/ocultar cajón de piezas">⛭</button>
    <div class="ds-drawer-body">
      <div class="ds-drawer-header">CAJÓN DE PIEZAS</div>
      <div class="ds-drawer-hint" id="dsDrawerHint">Arrastra una pieza a una zona, o selecciona una zona y haz clic en una pieza.</div>
      <div class="ds-drawer-categories" id="dsDrawerCategories"></div>
      <button class="ds-add-part-btn" id="dsAddPartBtn">+ AÑADIR PIEZA</button>
      <div class="ds-add-part-form" id="dsAddPartForm">
        <label class="ds-field-label">Zona</label>
        <select class="ds-field-input" id="dsAddPartZone">
          ${ZONES.map(z => `<option value="${z.key}">${_dsEsc(DRAWER_CATEGORY_LABEL[z.key])}</option>`).join('')}
        </select>
        <label class="ds-field-label">Describe la pieza</label>
        <textarea class="ds-field-textarea" id="dsAddPartDescription" rows="3" placeholder="Ej. un guante con garras retráctiles accionadas por un botón oculto..."></textarea>
        <div class="ds-add-part-actions">
          <button class="ds-zone-action-btn chat" id="dsAddPartCancel">Cancelar</button>
          <button class="ds-zone-action-btn suggest" id="dsAddPartSubmit">LIRA la documenta</button>
        </div>
      </div>
    </div>
  `
  _dsEl.workspaceMain.insertBefore(drawer, _dsEl.workspaceMain.firstChild)

  drawer.querySelector('#dsDrawerToggle').addEventListener('click', _dsToggleDrawer)
  drawer.querySelector('#dsAddPartBtn').addEventListener('click', () => {
    if (_dsActiveZone) drawer.querySelector('#dsAddPartZone').value = _dsActiveZone
    drawer.querySelector('#dsAddPartForm').classList.add('open')
  })
  drawer.querySelector('#dsAddPartCancel').addEventListener('click', () => {
    drawer.querySelector('#dsAddPartForm').classList.remove('open')
  })
  drawer.querySelector('#dsAddPartSubmit').addEventListener('click', _dsSubmitAddPart)

  return drawer
}

function _dsToggleDrawer() {
  _dsDrawerCollapsed = !_dsDrawerCollapsed
  document.getElementById('dsPartsDrawer').classList.toggle('collapsed', _dsDrawerCollapsed)
}

async function _dsFetchPartsLibrary() {
  try {
    const res = await fetch('/api/parts-library')
    const data = await res.json()
    _dsPartsLibrary = Array.isArray(data.parts) ? data.parts : []
  } catch {
    _dsPartsLibrary = []
  }
  _dsRenderDrawer()
}

function _dsRenderDrawer() {
  const box = document.getElementById('dsDrawerCategories')
  if (!box) return
  box.innerHTML = ''

  ZONES.forEach(z => {
    const parts = _dsPartsLibrary.filter(p => p.zone === z.key)
    const section = document.createElement('div')
    section.className = 'ds-drawer-category'
    section.innerHTML = `<div class="ds-drawer-category-title">${_dsEsc(DRAWER_CATEGORY_LABEL[z.key])} <span class="ds-drawer-category-count">${parts.length}</span></div>`
    const list = document.createElement('div')
    list.className = 'ds-drawer-part-list'
    if (!parts.length) {
      list.innerHTML = '<div class="ds-drawer-empty">Sin piezas todavía</div>'
    } else {
      parts.forEach(part => list.appendChild(_dsPartCardEl(part)))
    }
    section.appendChild(list)
    box.appendChild(section)
  })
}

const SOURCE_BADGE = { historical: 'Histórico', lira: 'LIRA', session: 'Sesión' }

function _dsPartCardEl(part) {
  const card = document.createElement('div')
  card.className = 'ds-part-card'
  card.draggable = true
  card.dataset.partId = part.id
  card.innerHTML = `
    <div class="ds-part-card-name">${_dsEsc(part.name || 'Sin nombre')}</div>
    <div class="ds-part-card-desc">${_dsEsc(part.description || '')}</div>
    ${part.material ? `<div class="ds-part-card-material">${_dsEsc(part.material)}</div>` : ''}
    <div class="ds-part-card-footer">
      <span class="ds-part-card-badge ${part.source}">${_dsEsc(SOURCE_BADGE[part.source] || part.source)}</span>
      ${part.source_model ? `<span class="ds-part-card-model">${_dsEsc(part.source_model)}</span>` : ''}
    </div>
  `
  card.addEventListener('dragstart', e => {
    e.dataTransfer.setData('text/plain', part.id)
    e.dataTransfer.effectAllowed = 'copy'
  })
  card.addEventListener('click', () => {
    if (_dsActiveZone) {
      _dsApplyPartToZone(part, _dsActiveZone)
    } else {
      const hint = document.getElementById('dsDrawerHint')
      hint.textContent = 'Selecciona primero una zona en el diagrama.'
      hint.classList.add('flash')
      setTimeout(() => hint.classList.remove('flash'), 1200)
    }
  })
  return card
}

function _dsPartById(id) {
  return _dsPartsLibrary.find(p => p.id === id) || null
}

function _dsApplyPartToZone(part, zoneKey) {
  if (_dsActiveZone !== zoneKey) _dsSelectZone(zoneKey)
  const zone = _dsCurrentZoneData()
  if (!zone) return
  zone.material = part.material || zone.material
  zone.mechanism = part.mechanism || zone.mechanism
  zone.aesthetic_notes = zone.aesthetic_notes ? `${zone.aesthetic_notes} / ${part.description}` : (part.description || zone.aesthetic_notes)
  if (part.source !== 'session') zone.lira_contribution = true
  _dsRenderZonePanel(zoneKey)
  _dsSaveDesign(false)
}

async function _dsSubmitAddPart() {
  const drawer = document.getElementById('dsPartsDrawer')
  const zone = drawer.querySelector('#dsAddPartZone').value
  const description = drawer.querySelector('#dsAddPartDescription').value.trim()
  if (!description) return
  const submitBtn = drawer.querySelector('#dsAddPartSubmit')
  submitBtn.disabled = true
  submitBtn.textContent = 'Documentando…'
  try {
    const res = await fetch('/api/parts-library', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: 'describe', zone, description }),
    })
    const data = await res.json()
    if (data.part) {
      _dsPartsLibrary.unshift(data.part)
      _dsRenderDrawer()
      drawer.querySelector('#dsAddPartDescription').value = ''
      drawer.querySelector('#dsAddPartForm').classList.remove('open')
    }
  } catch {
    // Silent — the form just stays open so Joan can retry.
  } finally {
    submitBtn.disabled = false
    submitBtn.textContent = 'LIRA la documenta'
  }
}

// Silent auto-save-as-part — fired whenever a zone flips to 'diseñado'
// (Estado toggle, a suggestion SELECCIONAR, or a chat suggestion accept),
// per spec: "every design decision that works gets saved as a reusable
// part". Skipped for empty zones (nothing to reuse) — backend also dedupes
// by zone+name+material, so repeated saves of the same zone are cheap.
async function _dsAutoSavePart(zoneKey, zoneData) {
  if (!zoneData.material && !zoneData.mechanism && !zoneData.aesthetic_notes) return
  const name = `${ZONE_LABEL[zoneKey]} — ${zoneData.material || 'pieza'}`.slice(0, 80)
  try {
    const res = await fetch('/api/parts-library', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        mode: 'manual',
        zone: zoneKey,
        part: {
          name,
          description: zoneData.aesthetic_notes || zoneData.notes || '',
          material: zoneData.material || '',
          mechanism: zoneData.mechanism || '',
          source: 'session',
          source_model: _dsCurrent.name || 'Diseño sin nombre',
        },
      }),
    })
    const data = await res.json()
    if (data.part && !data.duplicate) {
      _dsPartsLibrary.unshift(data.part)
      _dsRenderDrawer()
    }
  } catch {
    // Non-critical — the zone itself is already saved either way.
  }
}

// ── Progress indicator + DISEÑO COMPLETO banner ─────────────────────────
function _dsEnsureProgressEl() {
  let el = document.getElementById('dsProgressBar')
  if (el) return el
  el = document.createElement('div')
  el.id = 'dsProgressBar'
  el.className = 'ds-progress-bar'
  _dsEl.diagramPanel.insertBefore(el, _dsEl.diagramPanel.firstChild)
  return el
}

function _dsEnsureCompletionEl() {
  let el = document.getElementById('dsCompletionBanner')
  if (el) return el
  el = document.createElement('div')
  el.id = 'dsCompletionBanner'
  el.className = 'ds-completion-banner'
  el.innerHTML = `
    <div class="ds-completion-title">DISEÑO COMPLETO</div>
    <div class="ds-completion-actions">
      <button class="ds-completion-btn primary" id="dsGenSummaryBtn">Generar resumen y guardar en Estudio</button>
      <button class="ds-completion-btn" id="dsRenderBtn">Proceder a render</button>
    </div>
  `
  _dsEl.diagramPanel.appendChild(el)
  el.querySelector('#dsGenSummaryBtn').addEventListener('click', _dsGenerateSummary)
  el.querySelector('#dsRenderBtn').addEventListener('click', () => {
    _dsAppendChatBubble({ role: 'lira', text: 'El render de la armadura llega en la Fase 4 — por ahora el diseño ya está completo y guardado.' })
  })
  return el
}

function _dsRenderProgress() {
  const bar = _dsEnsureProgressEl()
  const done = _dsDesignedCount(_dsCurrent)
  const total = ZONES.length
  bar.innerHTML = `
    <div class="ds-progress-bar-row1">
      <span class="ds-progress-text">${done}/${total} zonas diseñadas</span>
      <span class="ds-progress-track"><span class="ds-progress-fill" style="width:${(done / total) * 100}%"></span></span>
    </div>
    <div class="ds-progress-bar-row2">
      <button class="ds-autopilot-btn" id="dsAutopilotBtn" title="Piloto automático">⟁ PILOTO AUTOMÁTICO</button>
      <button class="ds-consistency-btn" id="dsConsistencyBtn" title="Revisar consistencia entre zonas">Revisar consistencia</button>
    </div>
  `
  bar.querySelector('#dsConsistencyBtn').addEventListener('click', () => _dsRunConsistencyCheck(true))
  bar.querySelector('#dsAutopilotBtn').addEventListener('click', _dsOpenAutopilotDialog)

  const banner = _dsEnsureCompletionEl()
  banner.classList.toggle('open', done === total)
}

async function _dsGenerateSummary() {
  const btn = document.getElementById('dsGenSummaryBtn')
  btn.disabled = true
  btn.textContent = 'Generando…'
  try {
    const res = await fetch('/api/designs/summary', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ design: _dsCurrent }),
    })
    const data = await res.json()
    if (data.record) {
      _dsAppendChatBubble({ role: 'lira', text: `Resumen "${data.record.title}" guardado en Estudio → Resúmenes.` })
    }
  } catch {
    _dsAppendChatBubble({ role: 'lira', text: 'No he podido generar el resumen ahora mismo. Inténtalo de nuevo en un momento.' })
  } finally {
    btn.disabled = false
    btn.textContent = 'Generar resumen y guardar en Estudio'
  }
}

async function _dsRunConsistencyCheck(explicit) {
  try {
    const res = await fetch('/api/designs/consistency-check', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ design: _dsCurrent }),
    })
    const data = await res.json()
    if (data.flag) {
      _dsCurrent.conversation = _dsCurrent.conversation || []
      const turn = { role: 'lira', text: data.flag, zone: null }
      _dsCurrent.conversation.push(turn)
      _dsAppendChatBubble(turn)
      _dsSaveDesign(false)
    } else if (explicit) {
      _dsAppendChatBubble({ role: 'lira', text: 'No veo ninguna incoherencia clara entre las zonas diseñadas por ahora.' })
    }
  } catch {
    if (explicit) _dsAppendChatBubble({ role: 'lira', text: 'No he podido revisar la consistencia ahora mismo.' })
  }
}

// ── PILOTO AUTOMÁTICO (Phase 3) — constraints dialog ────────────────────
// Joan sets the direction, LIRA designs the selected zones autonomously
// (core.commands.run_autopilot_zone, one Ollama call per zone, sequential —
// Groq stays reserved for conversation), then Joan reviews each one
// (_dsStartReview below) before anything is marked final. Dialog + progress
// overlay both live as absolute children
// of #dsWorkspace (position:relative since Phase 1) so they cover the
// whole workspace, not just the diagram panel — the zone checkboxes need
// real room.
function _dsEnsureAutopilotDialog() {
  let el = document.getElementById('dsAutopilotDialog')
  if (el) return el

  el = document.createElement('div')
  el.id = 'dsAutopilotDialog'
  el.className = 'ds-autopilot-dialog'
  el.innerHTML = `
    <div class="ds-autopilot-dialog-box">
      <div class="ds-autopilot-dialog-title">PILOTO AUTOMÁTICO</div>

      <label class="ds-field-label">Describe qué quieres</label>
      <textarea class="ds-field-textarea" id="dsApDescription" rows="3" placeholder="Ej. más ligero que el VIII, más agresivo visualmente, reactor triangular..."></textarea>

      <label class="ds-field-label">Solo estas zonas (opcional — si no marcas ninguna, LIRA diseña las 7)</label>
      <div class="ds-ap-zone-checks" id="dsApZoneChecks">
        ${ZONES.map(z => `
          <label class="ds-ap-zone-check">
            <input type="checkbox" value="${z.key}"> ${_dsEsc(z.label)}
          </label>`).join('')}
      </div>

      <label class="ds-field-label">Referencia estética/estructural</label>
      <select class="ds-field-input" id="dsApStyleRef">
        <option value="">Ninguna</option>
        ${AUTOPILOT_MODEL_REFS.map(m => `<option value="${_dsEsc(m)}">${_dsEsc(m)}</option>`).join('')}
      </select>

      <label class="ds-field-label">Límite de materiales (opcional)</label>
      <input class="ds-field-input" id="dsApMaterialLimit" type="text" placeholder="Ej. solo cartulina y madera">

      <label class="ds-field-label">Preferencia de mecanismo (opcional)</label>
      <input class="ds-field-input" id="dsApMechanismPref" type="text" placeholder="Ej. evita electrónica, prioriza mecanismos simples">

      <label class="ds-field-label">Cosas a evitar (opcional)</label>
      <input class="ds-field-input" id="dsApAvoid" type="text" placeholder="Ej. el sistema de raíles del Model VI">

      <div class="ds-autopilot-dialog-actions">
        <button class="ds-zone-action-btn chat" id="dsApCancel">Cancelar</button>
        <button class="ds-zone-action-btn suggest" id="dsApStart">INICIAR AUTOPILOTO</button>
      </div>
    </div>
  `
  _dsEl.workspace.appendChild(el)

  el.querySelector('#dsApCancel').addEventListener('click', _dsCloseAutopilotDialog)
  el.querySelector('#dsApStart').addEventListener('click', _dsStartAutopilot)

  return el
}

function _dsOpenAutopilotDialog() {
  if (_dsAutopilotRunning) return
  const el = _dsEnsureAutopilotDialog()
  el.querySelectorAll('.ds-ap-zone-check input').forEach(cb => { cb.checked = false })
  el.querySelector('#dsApDescription').value = ''
  el.querySelector('#dsApStyleRef').value = ''
  el.querySelector('#dsApMaterialLimit').value = ''
  el.querySelector('#dsApMechanismPref').value = ''
  el.querySelector('#dsApAvoid').value = ''
  el.classList.add('open')
}

function _dsCloseAutopilotDialog() {
  const el = document.getElementById('dsAutopilotDialog')
  if (el) el.classList.remove('open')
}

async function _dsStartAutopilot() {
  console.log('[AUTOPILOT] INICIAR AUTOPILOTO clicked — _dsStartAutopilot()')
  const el = document.getElementById('dsAutopilotDialog')
  const checked = Array.from(el.querySelectorAll('.ds-ap-zone-check input:checked')).map(cb => cb.value)
  const zones = checked.length ? ZONES.filter(z => checked.includes(z.key)) : ZONES

  const constraints = {
    description: el.querySelector('#dsApDescription').value.trim(),
    style_reference: el.querySelector('#dsApStyleRef').value,
    material_limit: el.querySelector('#dsApMaterialLimit').value.trim(),
    mechanism_pref: el.querySelector('#dsApMechanismPref').value.trim(),
    avoid: el.querySelector('#dsApAvoid').value.trim(),
  }
  console.log('[AUTOPILOT] zones=%o constraints=%o', zones.map(z => z.key), constraints)

  _dsCloseAutopilotDialog()
  await _dsRunAutopilot(zones.map(z => z.key), constraints)
}

// ── Autopilot execution loop ─────────────────────────────────────────────
function _dsEnsureAutopilotProgress() {
  let el = document.getElementById('dsAutopilotProgress')
  if (el) return el
  el = document.createElement('div')
  el.id = 'dsAutopilotProgress'
  el.className = 'ds-autopilot-progress'
  el.innerHTML = `
    <div class="ds-ap-progress-title">PILOTO AUTOMÁTICO EN CURSO</div>
    <div class="ds-ap-progress-status" id="dsApProgressStatus">Preparando…</div>
    <div class="ds-ap-progress-hint" id="dsApProgressHint">GENERANDO — puede tardar varios minutos</div>
    <div class="ds-progress-track"><span class="ds-progress-fill" id="dsApProgressFill" style="width:0%"></span></div>
  `
  _dsEl.workspace.appendChild(el)
  return el
}

async function _dsRunAutopilot(zoneKeys, constraints) {
  console.log('[AUTOPILOT] _dsRunAutopilot() start — zoneKeys=%o', zoneKeys)
  if (!zoneKeys.length) { console.log('[AUTOPILOT] no zones selected — aborting'); return }
  _dsAutopilotRunning = true
  // Gate closed for the whole duration of the run — only the
  // 'autopilot_complete' event handler below is allowed to open it again,
  // once every zone in zoneKeys has actually been through its Ollama call.
  _dsAutopilotComplete = false
  document.getElementById('dsReviewOverlay')?.classList.remove('open')
  const overlay = _dsEnsureAutopilotProgress()
  overlay.classList.add('open')
  const statusEl = overlay.querySelector('#dsApProgressStatus')
  const fillEl = overlay.querySelector('#dsApProgressFill')
  // Persistent-bar indicator — stays visible in the bottom toolbar no
  // matter which section Joan navigates to while this async loop keeps
  // running (the SPA never tears down JS on section switch, so the fetch
  // loop below already continues in the background — this indicator just
  // makes that fact visible outside the Design Studio workspace itself).
  _dsShowApBarIndicator(`DISEÑANDO 0/${zoneKeys.length}...`)

  statusEl.textContent = 'Iniciando motor de diseño local…'
  console.log('[AUTOPILOT] POST /api/designs/autopilot-start')
  try {
    const startRes = await fetch('/api/designs/autopilot-start', { method: 'POST' })
    console.log('[AUTOPILOT] autopilot-start response status=%d', startRes.status)
  } catch (e) {
    console.error('[AUTOPILOT] autopilot-start fetch threw — network/CORS error:', e)
    // Best-effort — a zone call against an unreachable daemon just falls
    // back to the honest empty placeholder server-side.
  }

  for (let i = 0; i < zoneKeys.length; i++) {
    const zoneKey = zoneKeys[i]
    statusEl.textContent = `Diseñando zona ${i + 1}/${zoneKeys.length}: ${ZONE_LABEL[zoneKey].toUpperCase()}…`
    _dsShowApBarIndicator(`DISEÑANDO ${i}/${zoneKeys.length}...`)
    console.log('[AUTOPILOT] zone %d/%d (%s) — POST /api/designs/autopilot-zone', i + 1, zoneKeys.length, zoneKey)
    try {
      const t0 = performance.now()
      const res = await fetch('/api/designs/autopilot-zone', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ zone: zoneKey, design: _dsCurrent, constraints }),
      })
      const data = await res.json()
      console.log(
        '[AUTOPILOT] zone=%s status=%d elapsed=%.0fms response=%o',
        zoneKey, res.status, performance.now() - t0, data,
      )
      if (!res.ok) {
        console.error('[AUTOPILOT] zone=%s backend returned an error status — zone left unfilled:', zoneKey, data)
      } else if (!data.material && !data.mechanism && !data.aesthetic_notes) {
        console.warn('[AUTOPILOT] zone=%s came back EMPTY (fallback path) — reasoning=%s', zoneKey, data.reasoning)
      }
      _dsCurrent.zones = _dsCurrent.zones || {}
      const zone = _dsCurrentZoneDataFor(zoneKey)
      zone.material = data.material || zone.material
      zone.mechanism = data.mechanism || zone.mechanism
      zone.aesthetic_notes = data.aesthetic_notes || zone.aesthetic_notes
      zone.reasoning = data.reasoning || ''
      zone.lira_contribution = true
      zone.locked = false
      // Left as 'pendiente' — review mode below is what actually confirms
      // each zone (APROBAR), same "propose, then Joan decides" spirit as
      // every other LIRA suggestion path in this file. Diagram still
      // updates immediately though (.ap-ready, see _dsRenderDiagram) so
      // Joan sees each zone fill in live rather than only at the end.
      _dsRenderDiagram()
      _dsFlashZone(zoneKey)
    } catch (e) {
      console.error('[AUTOPILOT] zone=%s fetch threw — zone left unfilled:', zoneKey, e)
      // Non-critical for the run as a whole — that zone just stays as it
      // was, Joan can always fill it manually or re-run autopilot on it.
    }
    fillEl.style.width = `${((i + 1) / zoneKeys.length) * 100}%`
    _dsShowApBarIndicator(`DISEÑANDO ${i + 1}/${zoneKeys.length}...`)
  }

  console.log('[AUTOPILOT] all zones processed — saving design, entering review')
  statusEl.textContent = 'Diseño autopilotado — pasando a revisión…'
  await _dsSaveDesign(false)
  try {
    await fetch('/api/designs/autopilot-stop', { method: 'POST' })
  } catch {
    // Best-effort — worst case llama-server stays resident until the next
    // sleep session or autopilot run cleans it up.
  }
  await new Promise(r => setTimeout(r, 500))
  overlay.classList.remove('open')
  _dsAutopilotRunning = false

  // Completion signal — dispatched only here, after every zone in
  // zoneKeys has actually been through _autopilot_ollama_generate and the
  // resulting design has been saved. allFilled is diagnostic only (a zone
  // that hit the empty fallback still needs reviewing/filling by hand, so
  // it doesn't block the event) — logged so an all-fallback run is visible
  // in the console instead of silently looking like a normal completion.
  const allFilled = zoneKeys.every(zk => !!_dsCurrentZoneDataFor(zk).material)
  if (!allFilled) {
    console.warn('[AUTOPILOT] autopilot_complete firing with at least one empty-fallback zone — check logs/activity.log', zoneKeys)
  }
  console.log('[AUTOPILOT] dispatching autopilot_complete — allFilled=%s', allFilled)
  document.dispatchEvent(new CustomEvent('autopilot_complete', { detail: { zoneKeys, allFilled } }))
}

// Review mode is only ever entered in response to this event — never by a
// direct call from the run loop — so "review appears before generation is
// done" can't happen regardless of how _dsRunAutopilot's internals change
// in the future.
document.addEventListener('autopilot_complete', e => {
  console.log('[AUTOPILOT] autopilot_complete received — entering review', e.detail)
  _dsAutopilotComplete = true
  _dsShowApCompleteToast()
  _dsStartReview(e.detail.zoneKeys)
})

function _dsCurrentZoneDataFor(zoneKey) {
  _dsCurrent.zones = _dsCurrent.zones || {}
  if (!_dsCurrent.zones[zoneKey]) {
    _dsCurrent.zones[zoneKey] = { material: '', mechanism: '', aesthetic_notes: '', notes: '', status: 'pendiente', lira_contribution: false, reasoning: '', locked: false }
  }
  return _dsCurrent.zones[zoneKey]
}

function _dsFlashZone(zoneKey) {
  const el = document.getElementById(`dsZone_${zoneKey}`)
  if (!el) return
  el.classList.add('autopilot-flash')
  setTimeout(() => el.classList.remove('autopilot-flash'), 700)
}

// ── Review mode — one zone at a time: APROBAR / MODIFICAR / REDISEÑAR ───
function _dsEnsureReviewOverlay() {
  let el = document.getElementById('dsReviewOverlay')
  if (el) return el
  el = document.createElement('div')
  el.id = 'dsReviewOverlay'
  el.className = 'ds-review-overlay'
  _dsEl.workspace.appendChild(el)
  return el
}

function _dsStartReview(zoneKeys) {
  _dsReviewQueue = [...zoneKeys]
  _dsRenderDiagram()
  _dsRenderProgress()
  _dsRenderReviewStep()
}

function _dsRenderReviewStep() {
  const overlay = _dsEnsureReviewOverlay()
  // Hard gate: even though _dsStartReview is only reached via the
  // autopilot_complete listener above, this is the actual DOM-visibility
  // choke point — nothing shows the review card unless the completion
  // flag is set, no matter what call path reaches here.
  if (!_dsAutopilotComplete || !_dsReviewQueue.length) {
    overlay.classList.remove('open')
    return
  }
  const zoneKey = _dsReviewQueue[0]
  const zone = _dsCurrentZoneDataFor(zoneKey)
  const remaining = _dsReviewQueue.length

  overlay.innerHTML = `
    <div class="ds-review-card">
      <div class="ds-review-header">DISEÑO COMPLETADO — REVISIÓN <span class="ds-review-counter">(${remaining} pendiente${remaining === 1 ? '' : 's'})</span></div>
      <div class="ds-review-zone-title">${_dsEsc(ZONE_LABEL[zoneKey].toUpperCase())}</div>
      <div class="ds-review-field"><b>Material:</b> ${_dsEsc(zone.material || '—')}</div>
      <div class="ds-review-field"><b>Mecanismo:</b> ${_dsEsc(zone.mechanism || '—')}</div>
      <div class="ds-review-field"><b>Estética:</b> ${_dsEsc(zone.aesthetic_notes || '—')}</div>
      ${zone.reasoning ? `<div class="ds-review-reasoning">“${_dsEsc(zone.reasoning)}”</div>` : ''}
      <div class="ds-review-actions">
        <button class="ds-suggestion-card-btn select" id="dsReviewApprove">APROBAR</button>
        <button class="ds-suggestion-card-btn modify" id="dsReviewModify">MODIFICAR</button>
        <button class="ds-suggestion-card-btn discard" id="dsReviewRedesign">REDISEÑAR</button>
      </div>
    </div>
  `
  overlay.classList.add('open')

  overlay.querySelector('#dsReviewApprove').addEventListener('click', () => _dsReviewApprove(zoneKey))
  overlay.querySelector('#dsReviewModify').addEventListener('click', () => _dsReviewHandoff(zoneKey, false))
  overlay.querySelector('#dsReviewRedesign').addEventListener('click', () => _dsReviewHandoff(zoneKey, true))
}

function _dsReviewApprove(zoneKey) {
  const zone = _dsCurrentZoneDataFor(zoneKey)
  zone.status = 'diseñado'
  zone.locked = true
  _dsReviewQueue.shift()
  _dsRenderDiagram()
  _dsRenderProgress()
  _dsSaveDesign(false)
  _dsRunConsistencyCheck(false)
  _dsAutoSavePart(zoneKey, zone)
  _dsRenderReviewStep()
}

// MODIFICAR/REDISEÑAR both hand off to the normal zone panel — Joan
// finishes that zone there (editing fields directly, or via a fresh
// 3-option batch for REDISEÑAR), same UI he already knows from Phase 2/2.5
// rather than a second, parallel editing surface inside the review card.
// This ends the guided walkthrough for the remaining queued zones — they
// stay exactly as autopilot left them (filled in, unlocked, reasoning
// visible in their own panel) and can be reviewed individually anytime.
function _dsReviewHandoff(zoneKey, thenRedesign) {
  document.getElementById('dsReviewOverlay').classList.remove('open')
  _dsReviewQueue = []
  _dsSelectZone(zoneKey)
  if (thenRedesign) _dsAskZoneSuggestion()
}

// ── Zone detail panel (Phase 2) ─────────────────────────────────────────
function _dsEnsureZonePanel() {
  let panel = document.getElementById('dsZonePanel')
  if (panel) return panel

  panel = document.createElement('div')
  panel.id = 'dsZonePanel'
  panel.className = 'ds-zone-panel'
  panel.innerHTML = `
    <button class="ds-zone-panel-close" id="dsZonePanelClose" title="Cerrar">✕</button>
    <div class="ds-zone-panel-title" id="dsZonePanelTitle"></div>

    <label class="ds-field-label">Material</label>
    <input class="ds-field-input" id="dsZoneMaterial" list="dsMaterialOptions" placeholder="Ej. Cartulina, EVA Foam...">
    <datalist id="dsMaterialOptions">
      ${COMMON_MATERIALS.map(m => `<option value="${_dsEsc(m)}">`).join('')}
    </datalist>

    <label class="ds-field-label">Mecanismo</label>
    <textarea class="ds-field-textarea" id="dsZoneMechanism" rows="2" placeholder="Partes móviles, apertura, articulaciones..."></textarea>

    <label class="ds-field-label">Estética</label>
    <textarea class="ds-field-textarea" id="dsZoneAesthetic" rows="2" placeholder="Color, acabado, rasgos distintivos..."></textarea>

    <label class="ds-field-label">Notas</label>
    <textarea class="ds-field-textarea" id="dsZoneNotes" rows="2" placeholder="Notas libres..."></textarea>

    <label class="ds-field-label">Estado</label>
    <div class="ds-status-toggle" id="dsZoneStatusToggle">
      ${ZONE_STATUSES.map(s => `<button type="button" class="ds-status-btn" data-status="${s.key}">${_dsEsc(s.label)}</button>`).join('')}
    </div>

    <div class="ds-zone-reasoning" id="dsZoneReasoning"></div>

    <div class="ds-zone-panel-actions">
      <button class="ds-zone-action-btn suggest" id="dsAskSuggestionBtn">Pedir sugerencia a LIRA</button>
      <button class="ds-zone-action-btn chat" id="dsDesignWithLiraBtn">Diseñar con LIRA</button>
    </div>

    <div class="ds-suggestions" id="dsSuggestions"></div>
  `
  _dsEl.diagramPanel.appendChild(panel)

  panel.querySelector('#dsZonePanelClose').addEventListener('click', _dsCloseZonePanel)
  panel.querySelector('#dsZoneMaterial').addEventListener('change', _dsOnZoneFieldChange)
  panel.querySelector('#dsZoneMechanism').addEventListener('blur', _dsOnZoneFieldChange)
  panel.querySelector('#dsZoneAesthetic').addEventListener('blur', _dsOnZoneFieldChange)
  panel.querySelector('#dsZoneNotes').addEventListener('blur', _dsOnZoneFieldChange)
  panel.querySelectorAll('.ds-status-btn').forEach(btn => {
    btn.addEventListener('click', () => _dsSetZoneStatus(btn.dataset.status))
  })
  panel.querySelector('#dsAskSuggestionBtn').addEventListener('click', _dsAskZoneSuggestion)
  panel.querySelector('#dsDesignWithLiraBtn').addEventListener('click', _dsDesignWithLira)

  return panel
}

function _dsCurrentZoneData() {
  if (!_dsActiveZone || !_dsCurrent) return null
  _dsCurrent.zones = _dsCurrent.zones || {}
  if (!_dsCurrent.zones[_dsActiveZone]) {
    _dsCurrent.zones[_dsActiveZone] = { material: '', mechanism: '', aesthetic_notes: '', notes: '', status: 'pendiente', lira_contribution: false, reasoning: '', locked: false }
  }
  return _dsCurrent.zones[_dsActiveZone]
}

function _dsRenderZonePanel(zoneKey) {
  const panel = _dsEnsureZonePanel()
  const zone = _dsCurrentZoneData()
  if (!zone) return

  panel.querySelector('#dsZonePanelTitle').textContent = ZONE_LABEL[zoneKey].toUpperCase() + (zone.locked ? ' 🔒' : '')
  panel.querySelector('#dsZoneMaterial').value = zone.material || ''
  panel.querySelector('#dsZoneMechanism').value = zone.mechanism || ''
  panel.querySelector('#dsZoneAesthetic').value = zone.aesthetic_notes || ''
  panel.querySelector('#dsZoneNotes').value = zone.notes || ''
  panel.querySelectorAll('.ds-status-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.status === (zone.status || 'pendiente'))
  })

  // Autopilot's stated reasoning (Phase 3) — read-only, only shown when
  // this zone was actually autopiloted at some point.
  const reasoningEl = panel.querySelector('#dsZoneReasoning')
  if (zone.reasoning) {
    reasoningEl.style.display = ''
    reasoningEl.innerHTML = `<span class="ds-zone-reasoning-label">Razonamiento de LIRA</span><span class="ds-zone-reasoning-text"></span>`
    reasoningEl.querySelector('.ds-zone-reasoning-text').textContent = zone.reasoning
  } else {
    reasoningEl.style.display = 'none'
    reasoningEl.innerHTML = ''
  }

  const suggestBox = panel.querySelector('#dsSuggestions')
  suggestBox.innerHTML = ''
  const cached = _dsSuggestionsCache[zoneKey]
  if (cached) _dsRenderSuggestionCards(cached)

  panel.classList.add('open')
}

function _dsCloseZonePanel() {
  const panel = document.getElementById('dsZonePanel')
  if (panel) panel.classList.remove('open')
}

function _dsOnZoneFieldChange() {
  const zone = _dsCurrentZoneData()
  if (!zone) return
  const panel = document.getElementById('dsZonePanel')
  zone.material = panel.querySelector('#dsZoneMaterial').value.trim()
  zone.mechanism = panel.querySelector('#dsZoneMechanism').value.trim()
  zone.aesthetic_notes = panel.querySelector('#dsZoneAesthetic').value.trim()
  zone.notes = panel.querySelector('#dsZoneNotes').value.trim()
  zone.locked = false // editing a locked (autopilot-approved) zone implicitly revises it
  _dsSaveDesign(false)
}

function _dsSetZoneStatus(status) {
  const zone = _dsCurrentZoneData()
  if (!zone) return
  const wasDesigned = zone.status === 'diseñado'
  zone.status = status
  const panel = document.getElementById('dsZonePanel')
  panel.querySelectorAll('.ds-status-btn').forEach(btn => btn.classList.toggle('active', btn.dataset.status === status))
  _dsRenderDiagram()
  _dsRenderProgress()
  _dsSaveDesign(false)
  if (status === 'diseñado' && !wasDesigned) {
    _dsRunConsistencyCheck(false)
    _dsAutoSavePart(_dsActiveZone, zone)
  }
}

// ── 'Pedir sugerencia a LIRA' / 'Diseñar con LIRA' — always exactly 3
// A/B/C option cards (Phase 2.5) ─────────────────────────────────────────
function _dsMiniSilhouetteSvg(highlightZone, colorHex) {
  const shapes = Object.entries(ZONE_SVG_PATHS).map(([zoneKey, paths]) => {
    const isTarget = zoneKey === highlightZone
    const fill = isTarget ? `${colorHex}33` : 'rgba(255,255,255,0.05)'
    const stroke = isTarget ? colorHex : 'rgba(255,255,255,0.15)'
    const width = isTarget ? 2.2 : 1
    return paths.map(d => `<path d="${d}" fill="${fill}" stroke="${stroke}" stroke-width="${width}"/>`).join('')
  }).join('')
  return `<svg class="ds-option-preview-svg" viewBox="0 0 240 440" xmlns="http://www.w3.org/2000/svg">${shapes}</svg>`
}

// Read-only full silhouette, colored by each zone's actual status — used
// by Conceptuales' INSPECCIONAR view (concepts-edit.js) to show a design's
// current state without opening the full interactive workspace. Same
// visual language as the live workspace diagram (gold=diseñado,
// red=descartado, dim=pendiente) but no click/drag handlers at all.
function _dsRenderStaticDiagram(design) {
  const zones = (design && design.zones) || {}
  const shapes = Object.entries(ZONE_SVG_PATHS).map(([zoneKey, paths]) => {
    const status = (zones[zoneKey] || {}).status || 'pendiente'
    let fill = 'rgba(255,255,255,0.05)', stroke = 'rgba(255,255,255,0.2)'
    if (status === 'diseñado')  { fill = 'rgba(240,192,64,0.12)'; stroke = '#f0c040' }
    if (status === 'descartado') { fill = 'rgba(255,68,68,0.08)'; stroke = 'rgba(255,68,68,0.5)' }
    return paths.map(d => `<path d="${d}" fill="${fill}" stroke="${stroke}" stroke-width="1.4"/>`).join('')
  }).join('')
  return `<svg class="ds-static-diagram-svg" viewBox="0 0 240 440" xmlns="http://www.w3.org/2000/svg">${shapes}</svg>`
}

async function _dsAskZoneSuggestion(feedback) {
  const zoneKey = _dsActiveZone
  if (!zoneKey) return
  const box = document.querySelector('#dsZonePanel #dsSuggestions')
  box.innerHTML = '<div class="ds-suggestions-loading">LIRA está pensando 3 opciones…</div>'
  try {
    const res = await fetch('/api/designs/zone-suggestions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ zone: zoneKey, design: _dsCurrent, feedback: feedback || undefined }),
    })
    const data = await res.json()
    const options = (data.options || []).map(o => ({ ...o, discarded: false }))
    _dsSuggestionsCache[zoneKey] = options
    _dsRenderSuggestionCards(options)
  } catch {
    box.innerHTML = '<div class="ds-suggestions-loading">No he podido generar opciones ahora mismo.</div>'
  }
}

function _dsRenderSuggestionCards(options) {
  const box = document.querySelector('#dsZonePanel #dsSuggestions')
  box.innerHTML = ''
  if (!options.length) return

  const title = document.createElement('div')
  title.className = 'ds-suggestions-title'
  title.textContent = 'Opciones de LIRA'
  box.appendChild(title)

  options.forEach((opt, i) => {
    const card = document.createElement('div')
    card.className = `ds-suggestion-card${opt.discarded ? ' discarded' : ''}`
    card.innerHTML = `
      <div class="ds-suggestion-card-header">
        <span class="ds-suggestion-card-label" style="color:${OPTION_COLORS[i]}">${OPTION_LABELS[i]}</span>
        <span class="ds-suggestion-card-title">${_dsEsc(opt.title || '')}</span>
      </div>
      ${_dsMiniSilhouetteSvg(_dsActiveZone, OPTION_COLORS[i])}
      <div class="ds-suggestion-card-line"><b>Material:</b> ${_dsEsc(opt.material || '—')}</div>
      <div class="ds-suggestion-card-line"><b>Mecanismo:</b> ${_dsEsc(opt.mechanism || '—')}</div>
      <div class="ds-suggestion-card-line"><b>Estética:</b> ${_dsEsc(opt.aesthetic_notes || '—')}</div>
      ${opt.rationale ? `<div class="ds-suggestion-card-rationale">${_dsEsc(opt.rationale)}</div>` : ''}
      ${opt.discarded ? '<div class="ds-suggestion-card-discarded-tag">Descartada</div>' : `
      <div class="ds-suggestion-card-actions">
        <button class="ds-suggestion-card-btn select">SELECCIONAR</button>
        <button class="ds-suggestion-card-btn modify">MODIFICAR</button>
        <button class="ds-suggestion-card-btn discard">DESCARTAR</button>
      </div>`}
    `
    if (!opt.discarded) {
      card.querySelector('.select').addEventListener('click', () => _dsSelectOption(opt))
      card.querySelector('.modify').addEventListener('click', () => _dsModifyWithOption(opt))
      card.querySelector('.discard').addEventListener('click', () => _dsDiscardOption(options, i))
      // Drop target — a drawer part dropped onto an option merges into it
      // before Joan decides (spec 2.5 §3: "parts from the drawer can be
      // dragged onto any option to modify it before selecting").
      card.addEventListener('dragover', e => { e.preventDefault(); card.classList.add('drag-over') })
      card.addEventListener('dragleave', () => card.classList.remove('drag-over'))
      card.addEventListener('drop', e => {
        e.preventDefault()
        card.classList.remove('drag-over')
        const part = _dsPartById(e.dataTransfer.getData('text/plain'))
        if (!part) return
        if (part.material) opt.material = part.material
        if (part.mechanism) opt.mechanism = part.mechanism
        opt.aesthetic_notes = opt.aesthetic_notes ? `${opt.aesthetic_notes} / ${part.description}` : (part.description || opt.aesthetic_notes)
        _dsRenderSuggestionCards(options)
      })
    }
    box.appendChild(card)
  })

  if (options.every(o => o.discarded)) {
    box.appendChild(_dsBuildRegenerateBox())
  }
}

function _dsBuildRegenerateBox() {
  const wrap = document.createElement('div')
  wrap.className = 'ds-regenerate-box'
  wrap.innerHTML = `
    <div class="ds-regenerate-label">Ninguna convence. Describe qué cambiarías:</div>
    <textarea class="ds-field-textarea" id="dsRegenerateFeedback" rows="2" placeholder="Ej. más ligero, sin mecanismo visible, colores más oscuros..."></textarea>
    <button class="ds-suggestion-card-btn select" id="dsRegenerateBtn">Regenerar opciones</button>
  `
  wrap.querySelector('#dsRegenerateBtn').addEventListener('click', () => {
    const feedback = wrap.querySelector('#dsRegenerateFeedback').value.trim()
    _dsAskZoneSuggestion(feedback)
  })
  return wrap
}

function _dsDiscardOption(options, index) {
  options[index].discarded = true
  _dsRenderSuggestionCards(options)
}

function _dsApplyOptionFields(zone, opt) {
  if (opt.material) zone.material = opt.material
  if (opt.mechanism) zone.mechanism = opt.mechanism
  if (opt.aesthetic_notes) zone.aesthetic_notes = opt.aesthetic_notes
  zone.lira_contribution = true
}

// MODIFICAR — populate the zone's fields for Joan to tweak; status is left
// as-is so nothing is final until he confirms via the Estado toggle (same
// "propose, then Joan decides" spirit as the chat-flow suggestion accept).
function _dsModifyWithOption(opt) {
  const zone = _dsCurrentZoneData()
  if (!zone) return
  _dsApplyOptionFields(zone, opt)
  _dsRenderZonePanel(_dsActiveZone)
  _dsSaveDesign(false)
}

// SELECCIONAR — a direct, final pick: applies the option AND marks the
// zone 'diseñado' immediately, no extra confirmation step.
function _dsSelectOption(opt) {
  const zoneKey = _dsActiveZone
  const zone = _dsCurrentZoneData()
  if (!zone) return
  _dsApplyOptionFields(zone, opt)
  const wasDesigned = zone.status === 'diseñado'
  zone.status = 'diseñado'
  delete _dsSuggestionsCache[zoneKey]
  _dsRenderZonePanel(zoneKey)
  _dsRenderDiagram()
  _dsRenderProgress()
  _dsSaveDesign(false)
  if (!wasDesigned) {
    _dsRunConsistencyCheck(false)
    _dsAutoSavePart(zoneKey, zone)
  }
}

// ── 'Diseñar con LIRA' — same 3-option generation as 'Pedir sugerencia',
// plus focuses the chat so Joan can keep talking through it too. ────────
function _dsDesignWithLira() {
  _dsAskZoneSuggestion()
  _dsEl.chatInput.focus()
}

// ── Chat ─────────────────────────────────────────────────────────────────
function _dsRenderChatLog() {
  _dsEl.chatLog.innerHTML = ''
  ;(_dsCurrent.conversation || []).forEach(turn => _dsAppendChatBubble(turn))
  _dsEl.chatLog.scrollTop = _dsEl.chatLog.scrollHeight
}

function _dsAppendChatBubble(turn, opts) {
  opts = opts || {}
  const bubble = document.createElement('div')
  bubble.className = `ds-msg ${turn.role === 'lira' ? 'lira' : 'user'}${opts.suggestion ? ' suggestion' : ''}`
  const zoneTag = turn.zone ? `<span class="ds-msg-zone-tag">${_dsEsc(ZONE_LABEL[turn.zone] || turn.zone)}</span>` : ''
  bubble.innerHTML = `${zoneTag}<span class="ds-msg-text"></span>`
  bubble.querySelector('.ds-msg-text').textContent = turn.text
  if (opts.suggestion) {
    const btn = document.createElement('button')
    btn.className = 'ds-suggestion-accept-btn'
    btn.textContent = 'Aceptar propuesta'
    btn.addEventListener('click', () => _dsAcceptSuggestion(btn, opts.zone, opts.suggestion))
    bubble.appendChild(btn)
  }
  _dsEl.chatLog.appendChild(bubble)
  _dsEl.chatLog.scrollTop = _dsEl.chatLog.scrollHeight
  return bubble
}

function _dsAcceptSuggestion(btn, zone, suggestion) {
  const z = (_dsCurrent.zones || {})[zone]
  if (!z) return
  if (suggestion.material) z.material = suggestion.material
  if (suggestion.mechanism) z.mechanism = suggestion.mechanism
  if (suggestion.aesthetic_notes) z.aesthetic_notes = suggestion.aesthetic_notes
  const wasDesigned = z.status === 'diseñado'
  z.status = 'diseñado'
  z.lira_contribution = true
  btn.textContent = 'Aplicada'
  btn.disabled = true
  _dsRenderDiagram()
  _dsRenderProgress()
  if (_dsActiveZone === zone) _dsRenderZonePanel(zone)
  _dsSaveDesign(false)
  if (!wasDesigned) {
    _dsRunConsistencyCheck(false)
    _dsAutoSavePart(zone, z)
  }
}

async function _dsSendMessage() {
  const text = _dsEl.chatInput.value.trim()
  if (!text) return
  if (!_dsActiveZone) {
    _dsAppendChatBubble({ role: 'lira', text: 'Antes de nada, selecciona una zona del diagrama — así sé de qué estamos hablando.' })
    return
  }
  _dsEl.chatInput.value = ''
  _dsEl.sendBtn.disabled = true

  const userTurn = { role: 'user', text, zone: _dsActiveZone }
  _dsCurrent.conversation = _dsCurrent.conversation || []
  _dsCurrent.conversation.push(userTurn)
  _dsAppendChatBubble(userTurn)

  const typing = document.createElement('div')
  typing.className = 'ds-msg-typing'
  typing.textContent = 'LIRA está pensando…'
  _dsEl.chatLog.appendChild(typing)
  _dsEl.chatLog.scrollTop = _dsEl.chatLog.scrollHeight

  try {
    const res = await fetch('/api/designs/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ zone: _dsActiveZone, message: text, design: _dsCurrent }),
    })
    const data = await res.json()
    typing.remove()
    const liraTurn = { role: 'lira', text: data.reply || 'No he podido procesar eso, prueba de nuevo.', zone: _dsActiveZone }
    _dsCurrent.conversation.push(liraTurn)
    _dsAppendChatBubble(liraTurn, data.suggestion ? { suggestion: data.suggestion, zone: _dsActiveZone } : {})
  } catch {
    typing.remove()
    _dsAppendChatBubble({ role: 'lira', text: 'Se ha cortado la conexión. Inténtalo de nuevo en un momento.' })
  } finally {
    _dsEl.sendBtn.disabled = false
    _dsSaveDesign(false)
  }
}

// ── Save / autosave ─────────────────────────────────────────────────────
async function _dsSaveDesign(showStatus) {
  if (!_dsCurrent) return
  _dsCurrent.name = _dsEl.nameInput.value.trim()
  try {
    const res = await fetch('/api/designs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(_dsCurrent),
    })
    const data = await res.json()
    if (data.design) _dsCurrent = data.design
    if (showStatus) {
      _dsEl.saveStatus.textContent = 'Guardado'
      _dsEl.saveStatus.classList.add('saved')
      setTimeout(() => _dsEl.saveStatus.classList.remove('saved'), 1500)
    }
  } catch {
    if (showStatus) _dsEl.saveStatus.textContent = 'Error al guardar'
  }
}

function _dsStartAutosave() {
  _dsStopAutosave()
  _dsAutosaveTimer = setInterval(() => _dsSaveDesign(false), 30000)
}
function _dsStopAutosave() {
  if (_dsAutosaveTimer) clearInterval(_dsAutosaveTimer)
  _dsAutosaveTimer = null
}

async function _dsSaveToEstudio() {
  const name = _dsEl.nameInput.value.trim()
  if (!name) {
    _dsEl.suggestOptions.innerHTML = '<div class="ds-name-suggest-title">Generando sugerencias…</div>'
    _dsEl.suggestOverlay.classList.add('open')
    try {
      const res = await fetch('/api/designs/name-suggestions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(_dsCurrent),
      })
      const data = await res.json()
      _dsRenderNameSuggestions(data.names || [])
    } catch {
      _dsRenderNameSuggestions(['Diseño sin nombre I', 'Diseño sin nombre II', 'Diseño sin nombre III'])
    }
    return
  }
  await _dsFinishSaveToEstudio(name)
}

function _dsRenderNameSuggestions(names) {
  _dsEl.suggestOptions.innerHTML = ''
  const title = document.createElement('div')
  title.className = 'ds-name-suggest-title'
  title.textContent = 'Elige un nombre para el diseño'
  _dsEl.suggestOptions.appendChild(title)
  names.forEach(name => {
    const opt = document.createElement('div')
    opt.className = 'ds-name-suggest-option'
    opt.textContent = name
    opt.addEventListener('click', () => {
      _dsEl.suggestOverlay.classList.remove('open')
      _dsEl.nameInput.value = name
      _dsFinishSaveToEstudio(name)
    })
    _dsEl.suggestOptions.appendChild(opt)
  })
}

async function _dsFinishSaveToEstudio(name) {
  _dsCurrent.name = name
  _dsCurrent.status = 'guardado'
  await _dsSaveDesignToConceptuales()
}

// 'GUARDAR EN ESTUDIO' doesn't just save data/designs.json — it creates or
// updates a matching Conceptuales entry (data/concepts.json, type
// 'armor'), so the design always shows up as a concept Joan can browse
// alongside every other one. Reuses the SAME _loadConcepts()/_saveConcepts()
// the Conceptuales UI itself uses (armor-detail-concepts-load.js, loaded
// before this script) rather than talking to /api/concepts directly, so
// this can never drift from however that list actually gets persisted.
async function _dsSaveDesignToConceptuales() {
  _dsEl.saveStatus.textContent = 'Guardando…'

  // 1. Save the design itself first — a brand-new design doesn't have a
  // real "id" from the backend until this round-trips.
  await _dsSaveDesign(false)

  // 2. LIRA's one-line auto-description ('Armadura <name> — <características>').
  let description = `Armadura ${_dsCurrent.name || 'sin nombre'}.`
  try {
    const res = await fetch('/api/designs/concept-description', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ design: _dsCurrent }),
    })
    const data = await res.json()
    if (data.description) description = data.description
  } catch {
    // Falls back to the generic line above — still a valid concept entry.
  }

  // 3. Create (first save) or update (every save after) the linked concept.
  const concepts = _loadConcepts()
  let linked = _dsLinkedConceptTs ? concepts.find(c => c.ts === _dsLinkedConceptTs) : null
  if (linked) {
    linked.desc = description
    linked.design_id = _dsCurrent.id
  } else {
    linked = { name: _dsCurrent.name || 'Diseño sin nombre', desc: description, status: 'en desarrollo', type: 'armor', ts: Date.now(), design_id: _dsCurrent.id }
    concepts.unshift(linked)
  }
  await _saveConcepts(concepts)
  _dsLinkedConceptTs = linked.ts

  // 4. Stamp the design with the link so re-opening it from anywhere
  // (not just via this concept's own card) still finds its way back here.
  _dsCurrent.concept_ts = linked.ts
  await _dsSaveDesign(false)

  _dsEl.saveStatus.textContent = 'Guardado en Conceptuales.'
  _dsEl.saveStatus.classList.add('saved')
  setTimeout(() => _dsEl.saveStatus.classList.remove('saved'), 2500)

  const bubble = _dsAppendChatBubble({ role: 'lira', text: `Guardado en Conceptuales como "${linked.name}".`, zone: null })
  const goBtn = document.createElement('button')
  goBtn.className = 'ds-suggestion-accept-btn'
  goBtn.textContent = 'Ir a Conceptuales'
  goBtn.addEventListener('click', _dsGoToConceptuales)
  bubble.appendChild(goBtn)
}

// Optional navigation to Conceptuales → Armaduras, showing the just-saved
// concept — calls straight into section-nav.js/armor-detail-concepts-
// load.js's own globals (both load before this script).
function _dsGoToConceptuales() {
  switchSection('armor')
  _switchSubTab('conceptuales')
  _currentConceptType = 'armor'
  document.querySelectorAll('.concept-type-btn').forEach(b => b.classList.toggle('active', b.dataset.type === 'armor'))
  _renderConcepts()
}

// ── Wiring ───────────────────────────────────────────────────────────────
function _dsInit() {
  _dsQueryEls()
  if (!_dsEl.root) return

  _dsEl.newBtn.addEventListener('click', _dsStartNewDesign)
  _dsEl.backBtn.addEventListener('click', () => { _dsSaveDesign(false); _dsShowPicker() })
  _dsEl.sendBtn.addEventListener('click', _dsSendMessage)
  _dsEl.chatInput.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); _dsSendMessage() }
  })
  _dsEl.saveBtn.addEventListener('click', _dsSaveToEstudio)

  ZONES.forEach(z => {
    const el = document.getElementById(`dsZone_${z.key}`)
    if (!el) return
    el.addEventListener('click', () => _dsSelectZone(z.key))
    // Drop target — dragging a parts-drawer card straight onto the
    // diagram applies it without needing the zone selected first.
    el.addEventListener('dragover', e => { e.preventDefault(); el.classList.add('drag-over') })
    el.addEventListener('dragleave', () => el.classList.remove('drag-over'))
    el.addEventListener('drop', e => {
      e.preventDefault()
      el.classList.remove('drag-over')
      const partId = e.dataTransfer.getData('text/plain')
      const part = _dsPartById(partId)
      if (part) _dsApplyPartToZone(part, z.key)
    })
  })

  _dsEnsureZonePanel()
  _dsShowPicker()

  // Persistent-bar autopilot indicator — clicking it at any time jumps back
  // to the armor section and reopens the design workspace on whatever
  // design autopilot is currently running against (_dsCurrent), regardless
  // of which section Joan navigated away to.
  const apBarIndicator = document.getElementById('dsApBarIndicator')
  if (apBarIndicator) {
    apBarIndicator.addEventListener('click', () => {
      if (typeof switchSection === 'function') switchSection('armor')
      _dsOpenWorkspace()
    })
  }
}

// ── Persistent-bar autopilot indicator + completion toast ──────────────────
function _dsSetApBarText(text) {
  const el = document.getElementById('dsApBarText')
  if (el) el.textContent = text
}

function _dsShowApBarIndicator(text) {
  document.body.classList.add('ds-autopilot-active')
  document.getElementById('dsApBarIndicator')?.classList.remove('ds-ap-complete')
  _dsSetApBarText(text)
}

function _dsHideApBarIndicator() {
  document.body.classList.remove('ds-autopilot-active')
  document.getElementById('dsApBarIndicator')?.classList.remove('ds-ap-complete')
}

function _dsShowApCompleteToast() {
  const indicator = document.getElementById('dsApBarIndicator')
  if (indicator) {
    indicator.classList.add('ds-ap-complete')
    _dsSetApBarText('DISEÑO COMPLETO')
  }

  let toast = document.getElementById('dsApToast')
  if (!toast) {
    toast = document.createElement('div')
    toast.id = 'dsApToast'
    toast.className = 'ds-ap-toast'
    document.body.appendChild(toast)
  }
  toast.textContent = 'El piloto automático ha terminado. Revisa el diseño en Armaduras → Diseño.'
  toast.addEventListener('click', () => {
    if (typeof switchSection === 'function') switchSection('armor')
    _dsOpenWorkspace()
    toast.classList.remove('open')
  }, { once: true })
  toast.classList.add('open')

  // Bar indicator itself stays up (with the completed text) until Joan
  // dismisses it by opening the workspace — see _dsOpenWorkspace() below.
  // The toast is the one-off event notice, so it self-dismisses.
  setTimeout(() => toast.classList.remove('open'), 8000)
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _dsInit)
} else {
  _dsInit()
}
