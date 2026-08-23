// bughunter.js — BUG HUNTER app: Scope/Programas/Status/Scan/Hallazgos/
// Supervisión tabs.
//
// Visual language: reuses the "instrument console" chrome ESTUDIO's
// Investigación detail view established (.estudio-console/-hero/-body-grid/
// -rail/-item-card/-section-label, see ui/css/estudio.css) rather than the
// plain .estudio-card-list ESTUDIO's own top-level tabs use — Joan asked
// for the detail view's aesthetic specifically. Each panel below is just a
// mount point; render*Console() builds the full markup and injects it,
// same approach as estudio.js's _openEstudioInvestigacionDetail().
//
// Phase 1 (see "Bug Hunter Backend Plan" memory) is now wired to a real
// backend — core/bughunter_routes.py, GET/POST /api/bughunter* — for
// Scope (add/delete), Findings status, and the Auto Mode toggle. Loaded
// once via _loadBughunterData() below, kept in sync live via the
// 'bughunter_updated' socket event (bound the same reconnect-safe way
// estudio.js binds 'estudio_updated' — see the setInterval near the
// bottom of this file). The Scan tab's "Iniciar escaneo" still hits a
// real endpoint (POST /api/bughunter/scan) but the scan engine itself
// (core/bughunter_scan.py) isn't built yet — Phase 2 — so it only logs the
// attempt server-side and echoes that back.
//
// HARD RULE for whenever the actual scanning logic gets built: read-only
// proof-of-concept only, never exploit, never touch real user data, never
// go outside the Scope list, never auto-submit a finding anywhere — LIRA
// only ever prepares a report for Joan to copy-paste himself. See the
// "Bug Hunter Constraints" memory.

let _bhScope = []
let _bhFindings = []
let _bhState = { auto_mode: false, activity_log: [] }
let _bhLoaded = false

// Sugerencias — candidate bounty programs Auto Mode's discovery side
// activity found (core.bughunter_scan.discover_program_suggestions). Only
// ever pending ones — the backend already filters out dismissed entries.
let _bhSuggestions = []

// Supervisión — every 'bughunter_scan_log' event received this session,
// manual or auto-mode alike. In-memory only, deliberately not persisted
// (it's a live trace, not a record — data/bughunter_state.json's
// activity_log already covers the durable high-level history).
let _bhSupervisionLog = []
const _BH_SUPERVISION_MAX = 300

let _bhFindingsSearch = ''
let _bhFindingsSort = 'severity'
let _bhFindingsFilterSeverity = 'all'
let _bhFindingsFilterStatus = 'all'
const BH_SEVERITY_RANK = { critica: 4, alta: 3, media: 2, baja: 1 }

const BH_SEVERITY_LABELS = { critica: 'Crítica', alta: 'Alta', media: 'Media', baja: 'Baja' }
const BH_SEVERITY_COLOR  = { critica: '#ff5050', alta: '#ff8c40', media: '#f0c040', baja: 'rgba(255,255,255,0.35)' }
const BH_STATUS_LABELS   = { nuevo: 'Nuevo', borrador: 'Borrador', enviado: 'Enviado', duplicado: 'Duplicado', resuelto: 'Resuelto (auto)', descartado: 'Descartado' }

// Programas — static, curated reference list of major bug bounty platforms.
// Deliberately NOT specific company programs (those open/close/change scope
// constantly — a stale entry here could point at something no longer in
// scope, which is exactly the kind of thing the Scope allowlist elsewhere
// is supposed to prevent). This is "where to go look", not an allowlist —
// adding something here does NOT put it in Scope. access: 'público' means
// anyone can browse/apply; 'invitación' means the platform vets and invites
// researchers before they can see programs.
const BH_KNOWN_PROGRAMS = [
  { name: 'HackerOne', access: 'público', url: 'https://hackerone.com/directory/programs',
    note: 'La plataforma de bug bounty más grande — miles de programas activos de empresas de todos los tamaños.' },
  { name: 'Bugcrowd', access: 'público', url: 'https://bugcrowd.com/programs',
    note: 'Directorio público de programas, desde startups hasta grandes empresas.' },
  { name: 'Intigriti', access: 'público', url: 'https://www.intigriti.com/programs',
    note: 'Plataforma europea con un directorio público de programas.' },
  { name: 'YesWeHack', access: 'público', url: 'https://yeswehack.com/programs',
    note: 'Plataforma francesa, directorio público de programas.' },
  { name: 'Synack', access: 'invitación', url: 'https://www.synack.com',
    note: 'Red privada de investigadores vetados (Synack Red Team) — requiere solicitud y aceptación.' },
  { name: 'Google Bug Hunters', access: 'público', url: 'https://bughunters.google.com',
    note: 'Programa de recompensas de Google para sus propios productos y servicios.' },
  { name: 'Microsoft MSRC', access: 'público', url: 'https://www.microsoft.com/en-us/msrc/bounty',
    note: 'Programa de recompensas de Microsoft, varios sub-programas según el producto.' },
  { name: 'Apple Security Bounty', access: 'público', url: 'https://security.apple.com/bounty/',
    note: 'Programa de recompensas de Apple para iOS/macOS/hardware y otros productos.' },
]

let _bhScopeAdding = false
// Persists across re-renders of the add-form — critically, across a
// 'bughunter_updated'-triggered _loadBughunterData() reload that happens
// to land mid-edit (e.g. _bhPromoteSuggestion's own dismiss-suggestion
// call emits that exact event a moment after prefilling this form, which
// used to wipe the just-prefilled values back to blank — see
// _renderBughunterScopeAddForm). Read from on every render, written to on
// every keystroke and by _bhPromoteSuggestion.
let _bhScopeAddDraft = { name: '', platform: '', domain: '', notes: '', automationAllowed: false }

// ── Subtab switching — same pattern as _switchEstudioSubTab. ──────────────
function _switchBughunterSubTab(sub) {
  document.querySelectorAll('#section-bughunter .armor-subtabs .armor-subtab')
    .forEach(b => b.classList.toggle('active', b.dataset.bughunterSub === sub))
  document.querySelectorAll('#section-bughunter .estudio-panel').forEach(p => p.classList.remove('active'))
  const panel = document.getElementById(`bughunter${sub.charAt(0).toUpperCase()}${sub.slice(1)}Panel`)
  if (panel) panel.classList.add('active')
}
document.querySelectorAll('#section-bughunter .armor-subtabs .armor-subtab').forEach(btn => {
  btn.addEventListener('click', () => _switchBughunterSubTab(btn.dataset.bughunterSub))
})

// ── Scope ───────────────────────────────────────────────────────────────
function _bhScopeItemCard(t) {
  return `
    <div class="estudio-console-item-card" style="--hc:#3fa9f5;">
      <div class="estudio-console-item-head">
        <span class="estudio-console-item-type">${esc(t.platform)}</span>
        <span class="estudio-console-item-title">${esc(t.name)}</span>
        <button class="bughunter-scope-remove" data-scope-id="${esc(t.id)}" title="Quitar de Scope">✕</button>
      </div>
      <div class="estudio-console-item-text">${esc(t.domain)}</div>
      <div class="estudio-console-item-text">${esc(t.notes)}</div>
    </div>`
}
function _renderBughunterScope() {
  const mount = document.getElementById('bughunterScopeConsole')
  mount.innerHTML = `
    <div class="estudio-console">
      <div class="estudio-console-hero">
        <div class="estudio-console-hero-main">
          <div class="estudio-console-hero-eyebrow">
            <span class="estudio-console-chip status-activa">SCOPE</span>
            <span class="estudio-console-meta-readout">${_bhScope.length} OBJETIVO${_bhScope.length === 1 ? '' : 'S'}</span>
          </div>
          <div class="estudio-console-hero-title">Objetivos autorizados</div>
          <div class="estudio-console-hero-subtitle">Solo lo que aparece aquí — nunca más.</div>
        </div>
      </div>
      <div class="estudio-console-body-grid">
        <div class="estudio-console-col-main">
          <div class="estudio-console-block">
            <div class="estudio-console-section-label neutral"><span class="dot"></span>Programas</div>
            ${_bhScope.length
              ? _bhScope.map(_bhScopeItemCard).join('')
              : '<div class="estudio-console-rail-body">Sin objetivos aún. Añade un programa de bug bounty para que LIRA pueda trabajar sobre él.</div>'}
          </div>
          <div class="estudio-console-block" id="bughunterScopeAdd"></div>
        </div>
        <div class="estudio-console-rail">
          <div class="estudio-console-rail-section">
            <div class="estudio-console-section-label uncertain"><span class="dot"></span>Reglas</div>
            <div class="estudio-console-rail-body">Solo pruebas de concepto no destructivas. Nunca fuera de esta lista. Nunca se envía nada sin revisión de Joan.</div>
          </div>
        </div>
      </div>
    </div>
  `
  mount.querySelectorAll('.bughunter-scope-remove').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation()
      _bhDeleteScopeTarget(btn.dataset.scopeId)
    })
  })
  _renderBughunterScopeAddForm()
}
async function _bhDeleteScopeTarget(id) {
  try {
    const res = await fetch(`${JARVIS_API}/api/bughunter/scope/delete`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ id }),
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    _bhScope = _bhScope.filter(t => t.id !== id)
    _renderBughunterScope()
    _renderBughunterScan()
  } catch { /* leave the list as-is; the socket-driven reload will catch up if this was transient */ }
}
function _renderBughunterScopeAddForm() {
  const addWrap = document.getElementById('bughunterScopeAdd')
  if (!addWrap) return
  const d = _bhScopeAddDraft
  addWrap.innerHTML = _bhScopeAdding ? `
    <div class="bughunter-add-form">
      <div class="bughunter-add-row">
        <input type="text" class="core-module-change-input" id="bhAddName" placeholder="Nombre del programa" value="${esc(d.name)}">
        <input type="text" class="core-module-change-input" id="bhAddPlatform" placeholder="Plataforma (HackerOne, Bugcrowd...)" value="${esc(d.platform)}">
      </div>
      <input type="text" class="core-module-change-input" id="bhAddDomain" placeholder="Dominio / alcance (ej. ejemplo.com, *.ejemplo.com)" value="${esc(d.domain)}">
      <input type="text" class="core-module-change-input" id="bhAddNotes" placeholder="Notas de alcance (qué está permitido, qué está excluido)" value="${esc(d.notes)}">
      <label class="bughunter-add-checkbox">
        <input type="checkbox" id="bhAddAutomationAllowed"${d.automationAllowed ? ' checked' : ''}>
        He revisado las reglas reales del programa (no las de esta lista) y permiten herramientas de escaneo automatizado/pasivo — muchos programas lo prohíben explícitamente.
      </label>
      <p class="bughunter-add-error" id="bhAddError" style="display:none;"></p>
      <div class="core-module-build-actions">
        <button class="core-module-build-btn" id="bhAddSave">Guardar</button>
        <button class="core-module-build-btn" id="bhAddCancel">Cancelar</button>
      </div>
    </div>
  ` : '<button class="core-module-build-btn" id="bhAddBtn">+ Añadir objetivo</button>'

  if (_bhScopeAdding) {
    // Keep the draft in sync on every keystroke — this is what makes a
    // reload landing mid-edit (see _bhScopeAddDraft's comment) harmless:
    // whatever gets typed survives because the next render reads it back.
    const fieldMap = { bhAddName: 'name', bhAddPlatform: 'platform', bhAddDomain: 'domain', bhAddNotes: 'notes' }
    Object.keys(fieldMap).forEach(id => {
      document.getElementById(id).addEventListener('input', (e) => {
        _bhScopeAddDraft[fieldMap[id]] = e.target.value
      })
    })
    document.getElementById('bhAddAutomationAllowed').addEventListener('change', (e) => {
      _bhScopeAddDraft.automationAllowed = e.target.checked
    })
    document.getElementById('bhAddSave').addEventListener('click', async (e) => {
      const errEl = document.getElementById('bhAddError')
      const showError = (msg) => { errEl.textContent = msg; errEl.style.display = '' }
      const name = document.getElementById('bhAddName').value.trim()
      const domain = document.getElementById('bhAddDomain').value.trim()
      const automationAllowed = document.getElementById('bhAddAutomationAllowed').checked
      if (!name || !domain) {
        showError('Falta nombre del programa y/o dominio — ambos son obligatorios.')
        return
      }
      if (!automationAllowed) {
        showError('Confirma que revisaste las reglas reales del programa y permiten escaneo automatizado/pasivo antes de guardar — muchos programas lo prohíben y LIRA no debe escanear ninguno que lo prohíba.')
        return
      }
      errEl.style.display = 'none'
      const btn = e.target
      btn.disabled = true
      btn.textContent = 'Guardando…'
      try {
        const res = await fetch(`${JARVIS_API}/api/bughunter/scope`, {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({
            name,
            domain,
            platform: document.getElementById('bhAddPlatform').value.trim(),
            notes:    document.getElementById('bhAddNotes').value.trim(),
            automation_allowed: automationAllowed,
          }),
        })
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const data = await res.json()
        _bhScope.push(data.target)
        _bhScopeAdding = false
        _bhScopeAddDraft = { name: '', platform: '', domain: '', notes: '', automationAllowed: false }
        _renderBughunterScope()
        _renderBughunterScan()
      } catch {
        btn.disabled = false
        btn.textContent = 'Guardar'
        showError('No se pudo guardar — revisa la conexión e inténtalo de nuevo.')
      }
    })
    document.getElementById('bhAddCancel').addEventListener('click', () => {
      _bhScopeAdding = false
      _bhScopeAddDraft = { name: '', platform: '', domain: '', notes: '', automationAllowed: false }
      _renderBughunterScopeAddForm()
    })
  } else {
    document.getElementById('bhAddBtn').addEventListener('click', () => {
      _bhScopeAdding = true
      _renderBughunterScopeAddForm()
    })
  }
}

// ── Programas ───────────────────────────────────────────────────────────
// Main column: Sugerencias — dynamic, from Auto Mode's discovery side
// activity (core.bughunter_scan.discover_program_suggestions, once an
// hour while Auto Mode is on). NEVER added to Scope automatically —
// "Añadir a Scope" below just pre-fills the Scope add-form and switches
// tabs, domain left blank for Joan to fill in after actually reading the
// program's real scope page. Rail: the static platform reference
// (BH_KNOWN_PROGRAMS), unchanged from before, just demoted to secondary
// position now that Sugerencias is the more actionable content.
function _bhProgramItemCard(p) {
  return `
    <div class="estudio-console-item-card" style="--hc:${p.access === 'invitación' ? '#f0c040' : '#3fa9f5'};">
      <div class="estudio-console-item-head">
        <span class="estudio-console-item-type">${esc(p.access.toUpperCase())}</span>
        <span class="estudio-console-item-title">${esc(p.name)}</span>
      </div>
      <div class="estudio-console-item-text">${esc(p.note)}</div>
      <a class="estudio-console-source-link" href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.url.replace(/^https?:\/\//, ''))}</a>
    </div>`
}
function _bhSuggestionItemCard(s) {
  return `
    <div class="estudio-console-item-card" style="--hc:#3fa9f5;">
      <div class="estudio-console-item-head">
        <span class="estudio-console-item-type">${esc(s.platform)}</span>
        <span class="estudio-console-item-title">${esc(s.name)}</span>
      </div>
      ${s.note ? `<div class="estudio-console-item-text">${esc(s.note)}</div>` : ''}
      <a class="estudio-console-source-link" href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.url.replace(/^https?:\/\//, ''))}</a>
      <div class="bughunter-finding-actions">
        <button class="core-module-build-btn" data-promote="${esc(s.id)}">Añadir a Scope</button>
        <button class="core-module-build-btn" data-dismiss="${esc(s.id)}">Descartar</button>
      </div>
    </div>`
}
function _renderBughunterProgramas() {
  const mount = document.getElementById('bughunterProgramasConsole')
  if (!mount) return
  mount.innerHTML = `
    <div class="estudio-console">
      <div class="estudio-console-hero">
        <div class="estudio-console-hero-main">
          <div class="estudio-console-hero-eyebrow">
            <span class="estudio-console-chip status-activa">PROGRAMAS</span>
            <span class="estudio-console-meta-readout">${_bhSuggestions.length} SUGERENCIA${_bhSuggestions.length === 1 ? '' : 'S'}</span>
          </div>
          <div class="estudio-console-hero-title">Dónde encontrar programas</div>
          <div class="estudio-console-hero-subtitle">Nada de esto está en Scope. Las sugerencias las encuentra LIRA sola cuando Modo Auto está activo — revísalas y añade tú mismo lo que te interese.</div>
        </div>
      </div>
      <div class="estudio-console-body-grid">
        <div class="estudio-console-col-main">
          <div class="estudio-console-block">
            <div class="estudio-console-section-label neutral"><span class="dot"></span>Sugerencias</div>
            ${_bhSuggestions.length
              ? _bhSuggestions.map(_bhSuggestionItemCard).join('')
              : '<div class="estudio-console-rail-body">Sin sugerencias todavía. Activa Modo Auto — LIRA busca candidatos en las plataformas conocidas una vez por hora.</div>'}
          </div>
        </div>
        <div class="estudio-console-rail">
          <div class="estudio-console-rail-section">
            <div class="estudio-console-section-label uncertain"><span class="dot"></span>Plataformas conocidas</div>
            ${BH_KNOWN_PROGRAMS.map(p => `<a class="estudio-console-source-link" href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.name)}</a>`).join('')}
          </div>
        </div>
      </div>
    </div>
  `
  mount.querySelectorAll('[data-dismiss]').forEach(btn => {
    btn.addEventListener('click', () => _bhDismissSuggestion(btn.dataset.dismiss))
  })
  mount.querySelectorAll('[data-promote]').forEach(btn => {
    btn.addEventListener('click', () => {
      const s = _bhSuggestions.find(x => x.id === btn.dataset.promote)
      if (s) _bhPromoteSuggestion(s)
    })
  })
}
async function _bhDismissSuggestion(id) {
  try {
    const res = await fetch(`${JARVIS_API}/api/bughunter/suggestions/dismiss`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ id }),
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    _bhSuggestions = _bhSuggestions.filter(s => s.id !== id)
    _renderBughunterProgramas()
  } catch { /* leave it — next 'bughunter_updated' reload will catch up if this was transient */ }
}
function _bhPromoteSuggestion(s) {
  // Domain is deliberately NEVER auto-guessed here (removed 2026-08-18) —
  // a suggestion's URL is a listing page on the bounty PLATFORM's own
  // site, not the target company's, so any hostname-based guess either
  // produces the platform's domain (confirmed live: "Doctolib bug bounty
  // program", a yeswehack.com listing, got saved to Scope with
  // domain=yeswehack.com — an unauthorized third party Auto Mode would
  // have scanned next) or requires a platform-specific allowlist that's
  // one missed case away from the same mistake. Scope is the entire
  // safety boundary this app operates under — a convenience shortcut
  // isn't worth that risk. Joan always types the real domain himself.
  // Written into the draft (not the DOM directly) BEFORE rendering, so it
  // survives the 'bughunter_updated' reload that _bhDismissSuggestion
  // below triggers a moment later — see _bhScopeAddDraft's comment for
  // why that reload used to wipe these values back to blank.
  _bhScopeAddDraft = {
    name:     s.name,
    platform: s.platform,
    domain:   '',
    notes:    `Revisa el scope real en ${s.url} antes de guardar — completa el dominio autorizado manualmente.`,
    automationAllowed: false,
  }
  _bhDismissSuggestion(s.id)
  _switchBughunterSubTab('scope')
  _bhScopeAdding = true
  _renderBughunterScopeAddForm()
}

// ── Status ──────────────────────────────────────────────────────────────
function _renderBughunterStatus() {
  const mount = document.getElementById('bughunterStatusConsole')
  const auto = !!_bhState.auto_mode
  const running = !!_bhState.current_activity
  const lastRun = _bhState.last_run
  const lastRunReadout = lastRun
    ? `ÚLTIMA: ${esc((lastRun.target || '').toUpperCase())} · ${esc(_estudioFormatDate(lastRun.when))}`
    : 'SIN EJECUCIONES AÚN'
  const log = Array.isArray(_bhState.activity_log) ? _bhState.activity_log : []

  let chipLabel = 'INACTIVA', chipClass = 'status-completada', subtitle = 'LIRA no está trabajando en ningún objetivo ahora mismo.'
  if (running) {
    chipLabel = 'ESCANEANDO'
    chipClass = 'status-activa'
    subtitle = `${_bhState.current_activity}…`
  } else if (auto) {
    chipLabel = 'MODO AUTO'
    chipClass = 'status-activa'
    subtitle = 'Modo automático activo — inactiva en este momento entre objetivos.'
  }

  mount.innerHTML = `
    <div class="estudio-console">
      <div class="estudio-console-hero">
        <div class="estudio-console-hero-main">
          <div class="estudio-console-hero-eyebrow">
            <span class="estudio-console-chip ${chipClass}">${chipLabel}</span>
            <span class="estudio-console-meta-readout">${lastRunReadout}</span>
          </div>
          <div class="estudio-console-hero-title">Estado actual</div>
          <div class="estudio-console-hero-subtitle">${esc(subtitle)}</div>
        </div>
      </div>
      <div class="estudio-console-body-grid single-col">
        <div class="estudio-console-col-main">
          <div class="estudio-console-block">
            <div class="estudio-console-section-label neutral"><span class="dot"></span>Actividad reciente</div>
            <ul class="estudio-console-list">
              ${log.length
                ? log.map(e => `<li>${esc(_estudioFormatDate(e.time))} — ${esc(e.message)}</li>`).join('')
                : '<li>Sin actividad todavía.</li>'}
            </ul>
          </div>
        </div>
      </div>
    </div>
  `
}

// ── Scan ────────────────────────────────────────────────────────────────
function _renderBughunterScan() {
  const mount = document.getElementById('bughunterScanConsole')
  const options = _bhScope.length
    ? _bhScope.map(t => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join('')
    : '<option value="">Sin objetivos en Scope</option>'
  const running = !!_bhState.current_activity
  mount.innerHTML = `
    <div class="estudio-console">
      <div class="estudio-console-hero">
        <div class="estudio-console-hero-main">
          <div class="estudio-console-hero-eyebrow">
            <span class="estudio-console-chip ${running ? 'status-activa' : 'status-completada'}">SCAN</span>
          </div>
          <div class="estudio-console-hero-title">Escaneo manual</div>
          <div class="estudio-console-hero-subtitle">${running ? esc(_bhState.current_activity) + '…' : 'Elige un objetivo del Scope y lánzalo.'}</div>
        </div>
      </div>
      <div class="estudio-console-body-grid single-col">
        <div class="estudio-console-col-main">
          <div class="bughunter-scan-controls">
            <select class="bughunter-select" id="bughunterScanTarget" ${running ? 'disabled' : ''}>${options}</select>
            <button class="core-module-build-btn" id="bughunterScanStart" ${running ? 'disabled' : ''}>${running ? 'Escaneando…' : 'Iniciar escaneo'}</button>
          </div>
          <div class="estudio-console-block">
            <div class="estudio-console-section-label neutral"><span class="dot"></span>Registro en vivo</div>
            <ul class="estudio-console-list" id="bughunterScanLogList"></ul>
          </div>
        </div>
      </div>
    </div>
  `
  document.getElementById('bughunterScanStart').addEventListener('click', async (e) => {
    const targetId = document.getElementById('bughunterScanTarget').value
    const list = document.getElementById('bughunterScanLogList')
    if (!targetId) return
    const btn = e.target
    btn.disabled = true
    try {
      const res = await fetch(`${JARVIS_API}/api/bughunter/scan`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ target_id: targetId }),
      })
      const data = await res.json()
      const li = document.createElement('li')
      li.textContent = data.message || (res.ok ? 'Solicitud enviada.' : 'No se pudo iniciar el escaneo.')
      list.appendChild(li)
    } catch {
      const li = document.createElement('li')
      li.textContent = 'No se pudo contactar con el backend.'
      list.appendChild(li)
    } finally {
      btn.disabled = false
    }
  })
}

// ── Hallazgos ───────────────────────────────────────────────────────────
function _bhBuildReportText(f) {
  const steps = (f.repro_steps || []).map((s, i) => `${i + 1}. ${s}`).join('\n')
  return `Título: ${f.title}\nObjetivo: ${f.target}\nSeveridad: ${BH_SEVERITY_LABELS[f.severity] || f.severity}\n\nDescripción:\n${f.description || f.summary}\n\nPasos para reproducir:\n${steps || '(pendiente)'}\n\nImpacto:\n${f.impact || '(pendiente)'}\n\nSugerencia de corrección:\n${f.fix_suggestion || '(pendiente)'}`
}
function _bhWireCopyButtons(root) {
  root.querySelectorAll('.bughunter-copy-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation()
      const f = _bhFindings.find(x => x.id === btn.dataset.finding)
      if (!f) return
      navigator.clipboard.writeText(_bhBuildReportText(f)).then(() => {
        const original = btn.textContent
        btn.textContent = 'Copiado ✓'
        btn.classList.add('copied')
        setTimeout(() => { btn.textContent = original; btn.classList.remove('copied') }, 1600)
      })
    })
  })
}
async function _bhUpdateFindingStatus(id, status) {
  try {
    const res = await fetch(`${JARVIS_API}/api/bughunter/findings/status`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ id, status }),
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const f = _bhFindings.find(x => x.id === id)
    if (f) f.status = status
    _renderBughunterFindingsList()
  } catch { /* the select reverts on next _loadBughunterData() (socket-driven) if this failed */ }
}
function _bhFindingItemCard(f) {
  return `
    <div class="estudio-console-item-card" data-expandable="true" data-finding-id="${esc(f.id)}" style="--hc:${BH_SEVERITY_COLOR[f.severity] || '#3fa9f5'};">
      <div class="estudio-console-item-head">
        <span class="estudio-console-item-type">${esc(BH_SEVERITY_LABELS[f.severity] || f.severity)} · ${esc(BH_STATUS_LABELS[f.status] || f.status)}</span>
        <span class="estudio-console-item-title">${esc(f.title)}</span>
      </div>
      <div class="estudio-console-item-text">${esc(f.target)} — ${esc(f.summary)}</div>
      <div class="bughunter-finding-actions">
        <button class="core-module-build-btn bughunter-copy-btn" data-finding="${esc(f.id)}">Copiar reporte</button>
      </div>
    </div>`
}

// Console shell + controls are built once; only the list re-renders on
// search/sort/filter changes, so the search input never loses focus.
function _renderBughunterFindings() {
  const mount = document.getElementById('bughunterFindingsConsole')
  mount.innerHTML = `
    <div class="estudio-console">
      <div class="estudio-console-hero">
        <div class="estudio-console-hero-main">
          <div class="estudio-console-hero-eyebrow">
            <span class="estudio-console-chip status-activa">HALLAZGOS</span>
            <span class="estudio-console-meta-readout">${_bhFindings.length} EN TOTAL</span>
          </div>
          <div class="estudio-console-hero-title">Hallazgos</div>
          <div class="estudio-console-hero-subtitle">Cada uno con un reporte listo para copiar y pegar — LIRA nunca lo envía por su cuenta.</div>
        </div>
      </div>
      <div class="estudio-console-body-grid single-col">
        <div class="estudio-console-col-main">
          <div class="bughunter-findings-controls">
            <input type="text" class="core-module-change-input" id="bhFindingsSearch" placeholder="Buscar por título, objetivo o descripción...">
            <select class="bughunter-select" id="bhFindingsSort">
              <option value="severity">Ordenar: Severidad</option>
              <option value="status">Ordenar: Estado</option>
              <option value="target">Ordenar: Objetivo</option>
              <option value="title">Ordenar: Título</option>
            </select>
            <select class="bughunter-select" id="bhFindingsFilterSeverity">
              <option value="all">Severidad: Todas</option>
              <option value="critica">Severidad: Crítica</option>
              <option value="alta">Severidad: Alta</option>
              <option value="media">Severidad: Media</option>
              <option value="baja">Severidad: Baja</option>
            </select>
            <select class="bughunter-select" id="bhFindingsFilterStatus">
              <option value="all">Estado: Todos</option>
              <option value="nuevo">Estado: Nuevo</option>
              <option value="borrador">Estado: Borrador</option>
              <option value="enviado">Estado: Enviado</option>
              <option value="duplicado">Estado: Duplicado</option>
              <option value="resuelto">Estado: Resuelto (auto)</option>
              <option value="descartado">Estado: Descartado</option>
            </select>
          </div>
          <div class="estudio-console-block" id="bughunterFindingsListWrap"></div>
        </div>
      </div>
    </div>
  `

  document.getElementById('bhFindingsSearch').addEventListener('input', (e) => {
    _bhFindingsSearch = e.target.value
    _renderBughunterFindingsList()
  })
  document.getElementById('bhFindingsSort').addEventListener('change', (e) => {
    _bhFindingsSort = e.target.value
    _renderBughunterFindingsList()
  })
  document.getElementById('bhFindingsFilterSeverity').addEventListener('change', (e) => {
    _bhFindingsFilterSeverity = e.target.value
    _renderBughunterFindingsList()
  })
  document.getElementById('bhFindingsFilterStatus').addEventListener('change', (e) => {
    _bhFindingsFilterStatus = e.target.value
    _renderBughunterFindingsList()
  })

  _renderBughunterFindingsList()
}

function _renderBughunterFindingsList() {
  const wrap = document.getElementById('bughunterFindingsListWrap')
  if (!wrap) return

  const q = _bhFindingsSearch.trim().toLowerCase()
  let items = _bhFindings.filter(f => {
    if (_bhFindingsFilterSeverity !== 'all' && f.severity !== _bhFindingsFilterSeverity) return false
    if (_bhFindingsFilterStatus !== 'all' && f.status !== _bhFindingsFilterStatus) return false
    if (q && !(`${f.title} ${f.target} ${f.summary}`.toLowerCase().includes(q))) return false
    return true
  })
  items = items.slice().sort((a, b) => {
    if (_bhFindingsSort === 'severity') return (BH_SEVERITY_RANK[b.severity] || 0) - (BH_SEVERITY_RANK[a.severity] || 0)
    if (_bhFindingsSort === 'status') return (a.status || '').localeCompare(b.status || '')
    if (_bhFindingsSort === 'target') return (a.target || '').localeCompare(b.target || '')
    return (a.title || '').localeCompare(b.title || '')
  })

  if (!_bhFindings.length) {
    wrap.innerHTML = '<div class="estudio-console-rail-body">Sin hallazgos aún. Aparecerán aquí cuando LIRA complete un escaneo.</div>'
    return
  }
  if (!items.length) {
    wrap.innerHTML = '<div class="estudio-console-rail-body">Ningún hallazgo coincide con la búsqueda/filtros actuales.</div>'
    return
  }
  wrap.innerHTML = items.map(_bhFindingItemCard).join('')
  wrap.querySelectorAll('[data-expandable="true"]').forEach(card => {
    card.addEventListener('click', () => _openBughunterFindingDetail(card.dataset.findingId))
  })
  _bhWireCopyButtons(wrap)
}

// ── Supervisión ─────────────────────────────────────────────────────────
// Console shell is built once (same split as Hallazgos above) — only the
// list re-renders as events arrive, so nothing steals scroll/focus mid-scan.
function _renderBughunterSupervision() {
  const mount = document.getElementById('bughunterSupervisionConsole')
  mount.innerHTML = `
    <div class="estudio-console">
      <div class="estudio-console-hero">
        <div class="estudio-console-hero-main">
          <div class="estudio-console-hero-eyebrow">
            <span class="estudio-console-chip status-activa">SUPERVISIÓN</span>
            <span class="estudio-console-meta-readout" id="bughunterSupervisionCount">0 EVENTOS</span>
          </div>
          <div class="estudio-console-hero-title">Actividad en vivo</div>
          <div class="estudio-console-hero-subtitle">Cada paso que LIRA da durante un escaneo, en el momento en que ocurre — manual o en Modo Auto.</div>
        </div>
      </div>
      <div class="estudio-console-body-grid single-col">
        <div class="estudio-console-col-main">
          <div class="core-module-build-actions">
            <button class="core-module-build-btn" id="bughunterSupervisionClear">Limpiar registro</button>
          </div>
          <ul class="estudio-console-list" id="bughunterSupervisionList"></ul>
        </div>
      </div>
    </div>
  `
  document.getElementById('bughunterSupervisionClear').addEventListener('click', () => {
    _bhSupervisionLog = []
    _renderBughunterSupervisionList()
  })
  _renderBughunterSupervisionList()
}
function _renderBughunterSupervisionList() {
  const list  = document.getElementById('bughunterSupervisionList')
  const count = document.getElementById('bughunterSupervisionCount')
  if (!list) return
  count.textContent = `${_bhSupervisionLog.length} EVENTO${_bhSupervisionLog.length === 1 ? '' : 'S'}`
  list.innerHTML = _bhSupervisionLog.length
    ? _bhSupervisionLog.map(e => `<li>${esc(e.time)} — ${esc(e.message)}</li>`).join('')
    : '<li>Sin actividad todavía — se llenará en cuanto LIRA inicie un escaneo (manual o en Modo Auto).</li>'
  list.scrollTop = list.scrollHeight
}
function _bhRecordSupervisionEvent(message) {
  _bhSupervisionLog.push({ time: ts(), message })
  if (_bhSupervisionLog.length > _BH_SUPERVISION_MAX) _bhSupervisionLog.shift()
  _renderBughunterSupervisionList()
}

// ── Finding detail — slide-in overlay, same recipe as
//    _openEstudioInvestigacionDetail in ui/js/estudio.js. ──────────────────
function _openBughunterFindingDetail(id) {
  const f = _bhFindings.find(x => x.id === id)
  const detail = document.getElementById('bughunterFindingDetail')
  const body = document.getElementById('bughunterFindingDetailBody')
  if (!f || !detail || !body) return

  body.innerHTML = `
    <div class="estudio-console">
      <div class="estudio-console-hero">
        <div class="estudio-console-hero-main">
          <div class="estudio-console-hero-eyebrow">
            <span class="estudio-console-chip severity-${esc(f.severity)}">${esc(BH_SEVERITY_LABELS[f.severity] || f.severity)}</span>
            <span class="estudio-console-meta-readout">${esc((BH_STATUS_LABELS[f.status] || f.status || '').toUpperCase())}</span>
          </div>
          <div class="estudio-console-hero-title">${esc(f.title)}</div>
          <div class="estudio-console-hero-subtitle">${esc(f.target)}</div>
        </div>
      </div>
      <div class="estudio-console-body-grid">
        <div class="estudio-console-col-main">
          <div class="estudio-console-block">
            <div class="estudio-console-section-label neutral"><span class="dot"></span>Descripción</div>
            <div class="estudio-console-prose">${esc(f.description || f.summary)}</div>
          </div>
          ${(f.repro_steps || []).length ? `
          <div class="estudio-console-block">
            <div class="estudio-console-section-label neutral"><span class="dot"></span>Pasos para reproducir</div>
            <ol class="bughunter-repro-list">${f.repro_steps.map(s => `<li>${esc(s)}</li>`).join('')}</ol>
          </div>` : ''}
          ${f.impact ? `
          <div class="estudio-console-block">
            <div class="estudio-console-section-label critical"><span class="dot"></span>Impacto</div>
            <div class="estudio-console-prose">${esc(f.impact)}</div>
          </div>` : ''}
          ${f.fix_suggestion ? `
          <div class="estudio-console-block">
            <div class="estudio-console-section-label confident"><span class="dot"></span>Sugerencia de corrección</div>
            <div class="estudio-console-prose">${esc(f.fix_suggestion)}</div>
          </div>` : ''}
        </div>
        <div class="estudio-console-rail">
          <div class="estudio-console-rail-section">
            <div class="estudio-console-section-label neutral"><span class="dot"></span>Detalles</div>
            <div class="estudio-console-rail-body">Objetivo: ${esc(f.target)}<br>Severidad: ${esc(BH_SEVERITY_LABELS[f.severity] || f.severity)}</div>
          </div>
          <div class="estudio-console-rail-section">
            <div class="estudio-console-section-label neutral"><span class="dot"></span>Estado</div>
            <select class="bughunter-select" id="bhFindingStatusSelect" data-finding="${esc(f.id)}">
              ${Object.entries(BH_STATUS_LABELS).map(([v, label]) =>
                `<option value="${v}"${f.status === v ? ' selected' : ''}>${esc(label)}</option>`).join('')}
            </select>
          </div>
          <div class="estudio-console-rail-section">
            <button class="core-module-build-btn bughunter-copy-btn" data-finding="${esc(f.id)}">Copiar reporte</button>
          </div>
        </div>
      </div>
    </div>
  `
  document.getElementById('bhFindingStatusSelect').addEventListener('change', (e) => {
    _bhUpdateFindingStatus(f.id, e.target.value)
  })
  _bhWireCopyButtons(body)
  detail.classList.add('open')
}
function _closeBughunterFindingDetail() {
  const detail = document.getElementById('bughunterFindingDetail')
  if (detail) detail.classList.remove('open')
}
document.getElementById('bughunterFindingDetailClose').addEventListener('click', _closeBughunterFindingDetail)

// ── Auto mode toggle ────────────────────────────────────────────────────
document.getElementById('bughunterAutoModeToggle').addEventListener('click', async () => {
  const next = !_bhState.auto_mode
  const btn = document.getElementById('bughunterAutoModeToggle')
  btn.disabled = true
  try {
    const res = await fetch(`${JARVIS_API}/api/bughunter/automode`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ on: next }),
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    _bhState.auto_mode = next
    btn.classList.toggle('on', next)
    btn.setAttribute('aria-checked', String(next))
    _renderBughunterStatus()
  } catch { /* leave the toggle as it was; nothing changed server-side */ } finally {
    btn.disabled = false
  }
})

// ── Data loading ────────────────────────────────────────────────────────
async function _loadBughunterData() {
  try {
    const res  = await fetch(`${JARVIS_API}/api/bughunter`)
    const data = await res.json()
    _bhScope       = data.scope || []
    _bhFindings    = data.findings || []
    _bhState       = data.state || { auto_mode: false, activity_log: [] }
    _bhSuggestions = data.suggestions || []
    _bhLoaded = true
  } catch { /* leave whatever's already there (or the initial empty state) */ }

  const autoBtn = document.getElementById('bughunterAutoModeToggle')
  autoBtn.classList.toggle('on', !!_bhState.auto_mode)
  autoBtn.setAttribute('aria-checked', String(!!_bhState.auto_mode))

  _renderBughunterScope()
  _renderBughunterProgramas()
  _renderBughunterStatus()
  _renderBughunterScan()
  _renderBughunterFindings()
  _renderBughunterSupervision()
}

// Live refresh — same reconnect-safe lazy-bind pattern as estudio.js's own
// 'estudio_updated' listener (jarvisSocket doesn't exist yet when this
// script first runs, and gets torn down/recreated on reconnect).
// 'bughunter_scan_log' is the scan engine's own step-by-step progress
// (core/bughunter_scan.py's on_progress callback) — appended to whichever
// log list is on screen (Scan tab) AND recorded into Supervisión's running
// trace, no full reload needed for each line either way.
// The very first _loadBughunterData() call (bottom of this file) fires as
// soon as the script parses, against whatever JARVIS_API happens to be at
// that instant (BACKEND_URLS[0], the Tailscale IP — see
// ui/js/bootstrap-auth.js) — that can be unreachable, and/or jarvis.py's
// backend may not have opened its port yet (heavy imports run first). The
// fetch fails once, is caught silently, and nothing used to retry it, so
// the section stayed blank until an unrelated full-page force_reload
// happened to bail it out. connection.js only corrects JARVIS_API to a
// working URL once the socket's own connect_error fallback chain resolves
// — which can finish before this file's socket-binding poll even runs, so
// a one-shot 'connect' listener can miss the event entirely. Polling
// directly here instead guarantees a retry once JARVIS_API is actually
// correct, regardless of event timing.
let _bhLoadRetry = setInterval(() => {
  if (_bhLoaded) { clearInterval(_bhLoadRetry); return }
  _loadBughunterData()
}, 2000)

let _bhSocketBound = null
setInterval(() => {
  if (typeof jarvisSocket !== 'undefined' && jarvisSocket && jarvisSocket !== _bhSocketBound) {
    jarvisSocket.on('bughunter_updated', () => { _loadBughunterData() })
    jarvisSocket.on('bughunter_scan_log', (data) => {
      if (!data || !data.message) return
      const list = document.getElementById('bughunterScanLogList')
      if (list) {
        const li = document.createElement('li')
        li.textContent = data.message
        list.appendChild(li)
      }
      _bhRecordSupervisionEvent(data.message)
    })
    _bhSocketBound = jarvisSocket
  }
}, 1000)

_loadBughunterData()
