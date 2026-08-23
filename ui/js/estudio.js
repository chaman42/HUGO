// estudio.js — ESTUDIO app: data loading, card rendering, sub-tab
// navigation, and the expanded detail view for Investigación and
// Exploraciones cards (#estudioDetail is shared between both — see
// _openEstudioInvestigacionDetail / _openEstudioExploracionDetail).
// Reached only via the app launcher (switchSection('estudio') — see
// APP_LAUNCHER_APPS in diamond-text-launcher.js), same as NÚCLEO LIRA/
// CONTROL. Backed by a single GET /api/estudio call (core/estudio_routes.py)
// covering all 6 subsections in one round trip.

let _currentEstudioSub = 'investigacion'
let _estudioData = { investigations: [], summaries: [], explorations: [], schemas: [], ideas: [], documents: [] }
let _estudioLoaded = false

// LIRA's own type tags for autonomous sleep-time discoveries (see
// core/sleep_curiosity_search.py) — kept out of RESÚMENES (Joan-requested
// summaries only) and shown in EXPLORACIONES instead.
const ESTUDIO_EXPLORATION_TYPES = ['curiosidad', 'exploración profunda']

const ESTUDIO_STATUS_LABELS = {
  activa: 'Activa', incubando: 'Incubando',
  lista_para_revision: 'Lista para revisión', completada: 'Completada',
}

// ── Data loading ─────────────────────────────────────────────────────────
async function _loadEstudioData() {
  try {
    const res  = await fetch(`${JARVIS_API}/api/estudio`)
    const data = await res.json()
    _estudioData = {
      investigations: data.investigations || [],
      summaries:      data.summaries || [],
      explorations:   data.explorations || [],
      schemas:        data.schemas || [],
      ideas:          data.ideas || [],
      documents:      data.documents || [],
    }
    _estudioLoaded = true
  } catch { /* leave whatever's already there (or the initial empty state) */ }
  _renderEstudioActiveSub()
}

function _renderEstudioActiveSub() {
  if (_currentEstudioSub === 'investigacion')     _renderEstudioInvestigaciones()
  else if (_currentEstudioSub === 'resumenes')    _renderEstudioResumenes()
  else if (_currentEstudioSub === 'esquemas')     _renderEstudioEsquemas()
  else if (_currentEstudioSub === 'exploraciones') _renderEstudioExploraciones()
  else if (_currentEstudioSub === 'ideas')        _renderEstudioIdeas()
  else if (_currentEstudioSub === 'documentos')   _renderEstudioDocumentos()
}

// ── Sub-tab switching — same pattern as _switchCoreSubTab ───────────────
function _switchEstudioSubTab(sub) {
  _currentEstudioSub = sub
  document.querySelectorAll('#section-estudio .armor-subtabs .armor-subtab')
    .forEach(b => b.classList.toggle('active', b.dataset.estudioSub === sub))
  document.querySelectorAll('#section-estudio .estudio-panel').forEach(p => p.classList.remove('active'))
  const panel = document.getElementById(`estudio${sub.charAt(0).toUpperCase()}${sub.slice(1)}Panel`)
  if (panel) panel.classList.add('active')
  _renderEstudioActiveSub()
}

document.querySelectorAll('#section-estudio .armor-subtabs .armor-subtab').forEach(btn => {
  btn.addEventListener('click', () => _switchEstudioSubTab(btn.dataset.estudioSub))
})
document.getElementById('estudioClose').addEventListener('click', () => switchSection('home'))

// ── Live refresh — LIRA just saved a new summary/schema (core/commands.py's
// generate_summary()/generate_schema(), see core/routes_control.py-style
// socket emits) via the 'estudio_updated' event; reload all 5 subsections
// (_loadEstudioData already re-renders whichever tab is currently open) so
// the new card appears immediately, no page reload needed.
//
// jarvisSocket (ui/js/connection.js) is created lazily inside
// _attemptConnect() — it doesn't exist yet when this script first runs —
// and gets torn down/recreated on every reconnect. Polling for it (rather
// than binding once at load time) means this keeps working across
// reconnects too, without needing a change in connection.js itself.
let _estudioSocketBound = null
setInterval(() => {
  if (typeof jarvisSocket !== 'undefined' && jarvisSocket && jarvisSocket !== _estudioSocketBound) {
    jarvisSocket.on('estudio_updated', () => { _loadEstudioData() })
    _estudioSocketBound = jarvisSocket
  }
}, 1000)

// ── 1. INVESTIGACIÓN ─────────────────────────────────────────────────────
function _renderEstudioInvestigaciones() {
  const list = document.getElementById('estudioInvestigacionList')
  if (!list) return
  const items = _estudioData.investigations
  if (!items.length) {
    list.innerHTML = '<div class="estudio-empty">Sin investigaciones aún. LIRA comenzará a investigar cuando le asignes un objetivo.</div>'
    return
  }
  list.innerHTML = items.map((inv, i) => {
    const hypotheses      = Array.isArray(inv.hypotheses) ? inv.hypotheses : []
    const latestHypothesis = hypotheses.length ? hypotheses[hypotheses.length - 1] : null
    const cycles          = Number.isFinite(inv.cycles_processed) ? inv.cycles_processed : null
    const confidencePct   = Math.round((inv.confidence || 0) * 100)
    return `
    <div class="estudio-card" data-expandable="true" data-idx="${i}">
      <div class="estudio-card-header">
        <span class="estudio-card-title">${esc(inv.title || 'Investigación sin título')}</span>
        <span class="estudio-card-date">${esc(_estudioFormatDate(inv.date))}</span>
      </div>
      <div class="estudio-card-meta">
        <span class="estudio-tag status-${esc(inv.status || '')}">${esc(ESTUDIO_STATUS_LABELS[inv.status] || inv.status || '—')}</span>
        ${cycles !== null ? `<span class="estudio-card-relevance">Ciclo ${cycles}</span>` : ''}
        <span class="estudio-card-relevance">Confianza ${confidencePct}%</span>
      </div>
      <div class="estudio-card-summary">${esc((latestHypothesis && latestHypothesis.text) || inv.summary || '')}</div>
    </div>
  `
  }).join('')
  list.querySelectorAll('.estudio-card').forEach(card => {
    card.addEventListener('click', () => _openEstudioInvestigacionDetail(items[Number(card.dataset.idx)]))
  })
}

// Confidence threshold splitting a hypothesis into "lo que tiene más
// claro" vs "aún no está segura" — same 0.6 cut LIRA's own incubation
// phase (core/sleep_phases_incubation.py) already treats as a meaningful
// hypothesis, just surfaced visually here rather than left as a bare %.
const ESTUDIO_CONFIDENT_THRESHOLD = 0.6

// Same green/amber tiers as the confidence gauge (≥70 green, ≥40 amber,
// below that dim) — kept as one lookup so the gauge and every hypothesis
// bar always agree on what a given % means.
function _estudioConfidenceColor(pct) {
  return pct >= 70 ? '#00ff88' : pct >= 40 ? '#f0c040' : 'rgba(255,255,255,0.35)'
}

// core.sleep_phases_incubation sometimes stashes structured search
// metadata (a Python dict repr, e.g. "{'query': '...', 'relevancia': 0.9}")
// into `sources` instead of a real URL — rendering that as a clickable
// link would silently try to navigate to garbage. Only real http(s) URLs
// get the link treatment; anything else renders as plain text.
function _estudioSourceItemHtml(s) {
  if (/^https?:\/\//i.test(s)) {
    return `<a class="estudio-console-source-link" href="${esc(s)}" target="_blank" rel="noopener">${esc(s)}</a>`
  }
  return `<div class="estudio-console-source-plain">${esc(s)}</div>`
}

function _estudioHypothesisCardHtml(h) {
  const pct = Math.round((h.confidence || 0) * 100)
  const color = _estudioConfidenceColor(pct)
  return `
    <div class="estudio-console-item-card" style="--hc:${color};">
      <div class="estudio-console-item-text">${esc(h.text || '')}</div>
      <div class="estudio-inv-hyp-bar-row">
        <div class="estudio-inv-hyp-bar-track"><div class="estudio-inv-hyp-bar-fill" style="--w:${pct}%;"></div></div>
        <div class="estudio-inv-hyp-pct">${pct}%</div>
      </div>
    </div>`
}

function _openEstudioInvestigacionDetail(inv) {
  const detail = document.getElementById('estudioDetail')
  const body   = document.getElementById('estudioDetailBody')
  if (!detail || !body || !inv) return
  const pct = Math.round((inv.confidence || 0) * 100)
  const gaugeColor = _estudioConfidenceColor(pct)
  const sources      = Array.isArray(inv.sources) ? inv.sources : []
  const hypotheses   = Array.isArray(inv.hypotheses) ? inv.hypotheses : []
  const subQuestions = Array.isArray(inv.sub_questions) ? inv.sub_questions : []

  const confident = hypotheses
    .filter(h => (h.confidence || 0) >= ESTUDIO_CONFIDENT_THRESHOLD)
    .sort((a, b) => (b.confidence || 0) - (a.confidence || 0))
  const shaky = hypotheses
    .filter(h => (h.confidence || 0) < ESTUDIO_CONFIDENT_THRESHOLD)
    .sort((a, b) => (b.confidence || 0) - (a.confidence || 0))

  const nothingYet = !inv.conclusions && !hypotheses.length && !subQuestions.length

  body.innerHTML = `
    <div class="estudio-console">
      <div class="estudio-console-hero">
        <div class="estudio-console-hero-main">
          <div class="estudio-console-hero-eyebrow">
            <span class="estudio-console-chip status-${esc(inv.status || '')}">${esc(ESTUDIO_STATUS_LABELS[inv.status] || inv.status || '—')}</span>
            ${Number.isFinite(inv.cycles_processed) ? `<span class="estudio-console-meta-readout">CICLO ${String(inv.cycles_processed).padStart(2, '0')}</span>` : ''}
          </div>
          <div class="estudio-console-hero-title">${esc(inv.title || 'Investigación sin título')}</div>
          ${inv.question ? `<div class="estudio-console-hero-subtitle">"${esc(inv.question)}"</div>` : ''}
        </div>
        <div class="estudio-inv-gauge-wrap">
          <div class="estudio-inv-gauge" style="--pct:${pct};--gc:${gaugeColor};">
            <span class="estudio-inv-gauge-value">${pct}%</span>
          </div>
          <div class="estudio-inv-gauge-caption">Confianza</div>
        </div>
      </div>

      <div class="estudio-console-body-grid">
        <div class="estudio-console-col-main">
          ${inv.conclusions ? `
          <div class="estudio-console-verdict">
            <div class="estudio-console-verdict-badge">
              <svg viewBox="0 0 24 24" fill="none"><path d="M4 12.5L9.5 18L20 6" stroke="#04140c" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg>
            </div>
            <div class="estudio-console-verdict-copy">
              <div class="estudio-console-verdict-label">Conclusión</div>
              <div class="estudio-console-verdict-text">${esc(inv.conclusions)}</div>
            </div>
          </div>` : ''}

          ${confident.length ? `
          <div class="estudio-console-block">
            <div class="estudio-console-section-label confident"><span class="dot"></span>Lo que tiene más claro</div>
            ${confident.map(_estudioHypothesisCardHtml).join('')}
          </div>` : ''}

          ${shaky.length ? `
          <div class="estudio-console-block">
            <div class="estudio-console-section-label uncertain"><span class="dot"></span>Aún no está segura</div>
            ${shaky.map(_estudioHypothesisCardHtml).join('')}
          </div>` : ''}

          ${subQuestions.length ? `
          <div class="estudio-console-block">
            <div class="estudio-console-section-label open"><span class="dot"></span>Preguntas abiertas</div>
            <ul class="estudio-inv-open-list">${subQuestions.map(q => `<li>${esc(q)}</li>`).join('')}</ul>
          </div>` : ''}

          ${nothingYet ? '<div class="estudio-detail-body">Aún sin resultados — LIRA todavía no ha empezado a razonar sobre esto.</div>' : ''}
        </div>

        <div class="estudio-console-rail">
          <div class="estudio-console-rail-section">
            <div class="estudio-console-section-label neutral"><span class="dot"></span>Metodología</div>
            <div class="estudio-console-rail-body">${esc(inv.methodology || '—')}</div>
          </div>
          ${sources.length ? `
          <div class="estudio-console-rail-section">
            <div class="estudio-console-section-label neutral"><span class="dot"></span>Fuentes</div>
            ${sources.map(_estudioSourceItemHtml).join('')}
          </div>` : ''}
        </div>
      </div>
    </div>
  `
  detail.classList.add('open')
}

function _closeEstudioDetail() {
  const detail = document.getElementById('estudioDetail')
  if (detail) detail.classList.remove('open')
}
document.getElementById('estudioDetailClose').addEventListener('click', _closeEstudioDetail)

// ── 2. RESÚMENES — Joan-requested summaries only. Backend (data/summaries.
// json, written exclusively by core/commands.py's generate_summary()) no
// longer contains LIRA's own autonomous 'curiosidad'/'exploración profunda'
// finds — those live in data/explorations.json (see EXPLORACIONES below) —
// this filter is just a defensive belt-and-suspenders in case anything
// ever lands here with one of those types. ─────────────────────────────────
function _renderEstudioResumenes() {
  const list = document.getElementById('estudioResumenesList')
  if (!list) return
  const items = _estudioData.summaries.filter(s => !ESTUDIO_EXPLORATION_TYPES.includes(s.type))
  if (!items.length) {
    list.innerHTML = '<div class="estudio-empty">LIRA generará resúmenes automáticamente con el tiempo.</div>'
    return
  }
  list.innerHTML = items.map((s, i) => `
    <div class="estudio-card" data-expandable="true" data-idx="${i}">
      <div class="estudio-card-header">
        <span class="estudio-card-title">${esc(s.title || 'Resumen sin título')}</span>
        <span class="estudio-card-date">${esc(_estudioFormatDate(s.date))}</span>
      </div>
      <div class="estudio-card-meta">
        <span class="estudio-tag">${esc(s.type || '—')}</span>
      </div>
      <div class="estudio-card-summary">${esc(s.excerpt || '')}</div>
    </div>
  `).join('')
  list.querySelectorAll('.estudio-card').forEach(card => {
    card.addEventListener('click', () => _openEstudioResumenDetail(items[Number(card.dataset.idx)]))
  })
}

// core.commands.generate_summary() bakes its "- punto\n- punto\n\nConclusión:
// ..." shape into one `content` string rather than storing points/conclusion
// as separate fields (see that function's own docstring) — pulled back
// apart here so the console layout can give the conclusion its own verdict
// card and each point its own item card, same treatment as investigaciones'
// hypotheses/conclusions instead of one flat pre-wrapped text block.
function _estudioParseSummaryContent(content) {
  const lines = String(content || '').split('\n')
  const points = []
  let conclusion = ''
  for (const line of lines) {
    const t = line.trim()
    if (t.startsWith('- ')) points.push(t.slice(2).trim())
    else if (/^conclusi[oó]n:/i.test(t)) conclusion = t.replace(/^conclusi[oó]n:\s*/i, '').trim()
  }
  return { points, conclusion }
}

function _openEstudioResumenDetail(s) {
  const detail = document.getElementById('estudioDetail')
  const body   = document.getElementById('estudioDetailBody')
  if (!detail || !body || !s) return
  const { points, conclusion } = _estudioParseSummaryContent(s.content)
  // New records (post core.commands.generate_summary()'s RESUMEN: prompt
  // line) carry a proper prose paragraph in `s.narrative`. Older saved
  // records never had one — a resumen is meant to read as text, not just
  // a bullet list (that's what ESQUEMAS is for), so this is the section
  // that actually delivers on that; it's just absent for pre-existing
  // summaries rather than faked from the bullet points.
  const narrative = (s.narrative || '').trim()
  const nothingParsed = !narrative && !points.length && !conclusion

  body.innerHTML = `
    <div class="estudio-console">
      <div class="estudio-console-hero">
        <div class="estudio-console-hero-main">
          <div class="estudio-console-hero-eyebrow">
            <span class="estudio-console-chip">${esc(s.type || '—')}</span>
            <span class="estudio-console-meta-readout">${esc(_estudioFormatDate(s.date))}</span>
          </div>
          <div class="estudio-console-hero-title">${esc(s.title || 'Resumen sin título')}</div>
          ${s.source_topic ? `<div class="estudio-console-hero-subtitle">${esc(s.source_topic)}</div>` : ''}
        </div>
      </div>

      <div class="estudio-console-body-grid">
        <div class="estudio-console-col-main">
          ${narrative ? `
          <div class="estudio-console-block">
            <div class="estudio-console-section-label neutral"><span class="dot"></span>Resumen</div>
            <div class="estudio-console-prose">${esc(narrative)}</div>
          </div>` : ''}

          ${conclusion ? `
          <div class="estudio-console-verdict">
            <div class="estudio-console-verdict-badge">
              <svg viewBox="0 0 24 24" fill="none"><path d="M4 12.5L9.5 18L20 6" stroke="#04140c" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg>
            </div>
            <div class="estudio-console-verdict-copy">
              <div class="estudio-console-verdict-label">Conclusión</div>
              <div class="estudio-console-verdict-text">${esc(conclusion)}</div>
            </div>
          </div>` : ''}

          ${points.length ? `
          <div class="estudio-console-block">
            <div class="estudio-console-section-label neutral"><span class="dot"></span>Puntos clave</div>
            ${points.map(p => `<div class="estudio-console-item-card"><div class="estudio-console-item-text" style="margin-bottom:0;">${esc(p)}</div></div>`).join('')}
          </div>` : ''}

          ${nothingParsed ? `<div class="estudio-detail-body">${esc(s.content || '—')}</div>` : ''}
        </div>

        <div class="estudio-console-rail">
          <div class="estudio-console-rail-section">
            <div class="estudio-console-section-label neutral"><span class="dot"></span>Tema</div>
            <div class="estudio-console-rail-body">${esc(s.source_topic || '—')}</div>
          </div>
        </div>
      </div>
    </div>
  `
  detail.classList.add('open')
}

// ── 3. ESQUEMAS ───────────────────────────────────────────────────────────
// Two record shapes can be present in data/schemas.json side by side —
// old entries were never migrated when core/commands.py's generate_schema()
// was redesigned (a flat "TEMA PRINCIPAL: / ESTRUCTURA:" outline, one
// `content` string, `title`/`date`/`type` at the top level), new ones are a
// node graph (`title`/`topic`/`created`/`nodes: [{type,title,body,links_to,depth}]`/
// `open_questions`/`connections_to_known`, LLM-generated top-level title,
// no top-level type). Both
// render here — detected via Array.isArray(sc.nodes) — rather than
// migrating historical records into a shape they were never generated for.
const ESTUDIO_SCHEMA_NODE_TYPE_LABELS = {
  concept:    'Concepto',
  question:   'Pregunta',
  connection: 'Conexión',
  example:    'Ejemplo',
  insight:    'Idea',
}

// Same colored-left-border item-card language as everywhere else in the
// console idiom — one color per node type, reusing the same tones already
// established (amber=question, violet=connection, green=example) rather
// than inventing a fourth palette.
const ESTUDIO_SCHEMA_NODE_COLORS = {
  concept:    'var(--accent)',
  question:   'var(--p-color)',
  connection: '#9a78f5',
  example:    'var(--green)',
  insight:    '#f5825a',
}

function _isNewSchemaShape(sc) {
  return Array.isArray(sc.nodes)
}

function _renderEstudioEsquemas() {
  const list = document.getElementById('estudioEsquemasList')
  if (!list) return
  const items = _estudioData.schemas
  if (!items.length) {
    list.innerHTML = '<div class="estudio-empty">Los esquemas aparecerán cuando LIRA organice información compleja.</div>'
    return
  }
  list.innerHTML = items.map((sc, i) => {
    if (_isNewSchemaShape(sc)) {
      const nodeCount = sc.nodes.length
      const firstConcept = sc.nodes.find(n => n.type === 'concept') || sc.nodes[0]
      return `
      <div class="estudio-card" data-expandable="true" data-idx="${i}">
        <div class="estudio-card-header">
          <span class="estudio-card-title">${esc(sc.topic || 'Esquema sin título')}</span>
          <span class="estudio-card-date">${esc(_estudioFormatDate(sc.created))}</span>
        </div>
        <div class="estudio-card-meta">
          <span class="estudio-tag">${nodeCount} nodo${nodeCount === 1 ? '' : 's'}</span>
          ${sc.open_questions?.length ? `<span class="estudio-tag">${sc.open_questions.length} pregunta${sc.open_questions.length === 1 ? '' : 's'} abierta${sc.open_questions.length === 1 ? '' : 's'}</span>` : ''}
        </div>
        <div class="estudio-card-summary">${esc(firstConcept?.title || '')}</div>
      </div>
    `
    }
    // Legacy flat-outline shape.
    const firstLine = (sc.content || '').split('\n').find(l => l.trim()) || sc.topic || ''
    return `
    <div class="estudio-card" data-expandable="true" data-idx="${i}">
      <div class="estudio-card-header">
        <span class="estudio-card-title">${esc(sc.title || 'Esquema sin título')}</span>
        <span class="estudio-card-date">${esc(_estudioFormatDate(sc.date))}</span>
      </div>
      <div class="estudio-card-meta">
        <span class="estudio-tag">${esc(sc.type || '—')}</span>
      </div>
      <div class="estudio-card-summary">${esc(firstLine)}</div>
    </div>
  `
  }).join('')
  list.querySelectorAll('.estudio-card').forEach(card => {
    card.addEventListener('click', () => _openEstudioEsquemaDetail(items[Number(card.dataset.idx)]))
  })
}

function _openEstudioEsquemaDetail(sc) {
  const detail = document.getElementById('estudioDetail')
  const body   = document.getElementById('estudioDetailBody')
  if (!detail || !body || !sc) return

  if (_isNewSchemaShape(sc)) {
    const nodesByDepth = [...sc.nodes].sort((a, b) => (a.depth || 1) - (b.depth || 1))
    const nodesHtml = nodesByDepth.map(n => {
      const color = ESTUDIO_SCHEMA_NODE_COLORS[n.type] || ESTUDIO_SCHEMA_NODE_COLORS.concept
      return `
        <div class="estudio-console-item-card" style="--hc:${color};margin-left:${((n.depth || 1) - 1) * 16}px;">
          <div class="estudio-console-item-head">
            <span class="estudio-console-item-type">${esc(ESTUDIO_SCHEMA_NODE_TYPE_LABELS[n.type] || 'Concepto')}</span>
            <span class="estudio-console-item-title">${esc(n.title || '')}</span>
          </div>
          ${n.body ? `<div class="estudio-console-item-text" style="margin-bottom:0;">${esc(n.body)}</div>` : ''}
          ${n.links_to?.length ? `<div class="estudio-console-item-links">↳ ${n.links_to.map(esc).join(', ')}</div>` : ''}
        </div>`
    }).join('')

    body.innerHTML = `
      <div class="estudio-console">
        <div class="estudio-console-hero">
          <div class="estudio-console-hero-main">
            <div class="estudio-console-hero-eyebrow">
              <span class="estudio-console-chip">${sc.nodes.length} nodo${sc.nodes.length === 1 ? '' : 's'}</span>
              ${sc.open_questions?.length ? `<span class="estudio-console-meta-readout">${sc.open_questions.length} pregunta${sc.open_questions.length === 1 ? '' : 's'} abierta${sc.open_questions.length === 1 ? '' : 's'}</span>` : ''}
            </div>
            <div class="estudio-console-hero-title">${esc(sc.title || sc.topic || 'Esquema sin título')}</div>
            ${sc.title ? `<div class="estudio-console-hero-subtitle">${esc(sc.topic)}</div>` : ''}
          </div>
        </div>

        <div class="estudio-console-body-grid single-col">
          <div class="estudio-console-col-main">
            <div class="estudio-console-block">
              <div class="estudio-console-section-label neutral"><span class="dot"></span>Mapa de comprensión</div>
              ${nodesHtml || '<div class="estudio-detail-body">—</div>'}
            </div>

            ${sc.connections_to_known?.length ? `
            <div class="estudio-console-block">
              <div class="estudio-console-section-label confident"><span class="dot"></span>Conexiones con lo ya conocido</div>
              ${sc.connections_to_known.map(c => `<div class="estudio-console-item-card"><div class="estudio-console-item-text" style="margin-bottom:0;">${esc(c)}</div></div>`).join('')}
            </div>` : ''}

            ${sc.open_questions?.length ? `
            <div class="estudio-console-block">
              <div class="estudio-console-section-label open"><span class="dot"></span>Preguntas abiertas</div>
              <ul class="estudio-inv-open-list">${sc.open_questions.map(q => `<li>${esc(q)}</li>`).join('')}</ul>
            </div>` : ''}
          </div>
        </div>
      </div>
    `
    detail.classList.add('open')
    return
  }

  // Legacy flat-outline shape — lighter console treatment, prose body only
  // (no per-node data to break into item-cards).
  body.innerHTML = `
    <div class="estudio-console">
      <div class="estudio-console-hero">
        <div class="estudio-console-hero-main">
          <div class="estudio-console-hero-eyebrow">
            <span class="estudio-console-chip">${esc(sc.type || '—')}</span>
          </div>
          <div class="estudio-console-hero-title">${esc(sc.title || 'Esquema sin título')}</div>
          ${sc.topic ? `<div class="estudio-console-hero-subtitle">${esc(sc.topic)}</div>` : ''}
        </div>
      </div>
      <div class="estudio-console-body-grid single-col">
        <div class="estudio-console-col-main">
          <div class="estudio-console-block">
            <div class="estudio-console-section-label neutral"><span class="dot"></span>Esquema</div>
            <div class="estudio-console-prose">${esc(sc.content || '—')}</div>
          </div>
        </div>
      </div>
    </div>
  `
  detail.classList.add('open')
}

// ── EXPLORACIONES — data/explorations.json. LIRA's own autonomous
// sleep-time discoveries (Curiosidad's active web search + "curiosidad
// profunda" deep-dive — see core/sleep_curiosity_search.py), never
// user-requested. Most recent first — the raw array itself is append-only,
// so display order is reversed here; each card keeps its true array index
// (`_idx`, tagged BEFORE the reverse) since that's what
// POST /api/estudio/explorations/read needs to identify the entry. ────────
function _renderEstudioExploraciones() {
  const list = document.getElementById('estudioExploracionesList')
  if (!list) return
  const items = _estudioData.explorations
    .map((ex, i) => ({ ...ex, _idx: i }))
    .reverse()
  if (!items.length) {
    list.innerHTML = '<div class="estudio-empty">LIRA guardará aquí lo que encuentre explorando por su cuenta durante el sueño.</div>'
    return
  }
  list.innerHTML = items.map(ex => `
    <div class="estudio-card${ex.read ? ' read' : ''}" data-expandable="true" data-idx="${ex._idx}">
      <div class="estudio-card-header">
        ${!ex.read ? '<span class="estudio-unread-dot" title="Sin leer"></span>' : ''}
        <span class="estudio-card-title">${esc(ex.title || 'Exploración sin título')}</span>
        <span class="estudio-card-date">${esc(_estudioFormatDate(ex.date))}</span>
      </div>
      <div class="estudio-card-meta">
        <span class="estudio-tag">${esc(ex.type || '—')}</span>
        ${ex.topic ? `<span class="estudio-card-relevance">${esc(ex.topic)}</span>` : ''}
        ${Number.isFinite(ex.relevance) ? `<span class="estudio-card-relevance">Relevancia ${Math.round(ex.relevance * 100)}%</span>` : ''}
      </div>
      <div class="estudio-card-summary">${esc(ex.excerpt || '')}</div>
    </div>
  `).join('')
  list.querySelectorAll('.estudio-card').forEach(card => {
    const idx = Number(card.dataset.idx)
    card.addEventListener('click', () => _openEstudioExploracionDetail(_estudioData.explorations[idx], idx))
  })
}

function _openEstudioExploracionDetail(ex, idx) {
  const detail = document.getElementById('estudioDetail')
  const body   = document.getElementById('estudioDetailBody')
  if (!detail || !body || !ex) return

  const fullText  = ex.summary || ex.excerpt || ''
  const cycle     = Number.isFinite(ex.found_during_sleep_cycle) ? ex.found_during_sleep_cycle : null
  const relPct    = Number.isFinite(ex.relevance) ? Math.round(ex.relevance * 100) : null
  const gaugeColor = relPct !== null ? _estudioConfidenceColor(relPct) : 'rgba(255,255,255,0.35)'
  // Only http(s) links, and quote-escaped on top of esc()'s own </>/& escaping
  // — ex.url comes from an external search API (Serper/DuckDuckGo), so it's
  // treated as untrusted: a stray '"' in it must not be able to break out of
  // the href="..." attribute.
  const safeUrl   = typeof ex.url === 'string' && /^https?:\/\//i.test(ex.url)
    ? esc(ex.url).replace(/"/g, '&quot;')
    : null

  body.innerHTML = `
    <div class="estudio-console">
      <div class="estudio-console-hero">
        <div class="estudio-console-hero-main">
          <div class="estudio-console-hero-eyebrow">
            <span class="estudio-console-chip">${esc(ex.type || '—')}</span>
            <span class="estudio-console-meta-readout">${esc(_estudioFormatDate(ex.date))}${cycle !== null ? ` · CICLO ${String(cycle).padStart(2, '0')}` : ''}</span>
          </div>
          <div class="estudio-console-hero-title">${esc(ex.title || 'Exploración sin título')}</div>
          ${ex.topic ? `<div class="estudio-console-hero-subtitle">${esc(ex.topic)}</div>` : ''}
        </div>
        ${relPct !== null ? `
        <div class="estudio-inv-gauge-wrap">
          <div class="estudio-inv-gauge" style="--pct:${relPct};--gc:${gaugeColor};">
            <span class="estudio-inv-gauge-value">${relPct}%</span>
          </div>
          <div class="estudio-inv-gauge-caption">Relevancia</div>
        </div>` : ''}
      </div>

      <div class="estudio-console-body-grid">
        <div class="estudio-console-col-main">
          <div class="estudio-console-block">
            <div class="estudio-console-section-label neutral"><span class="dot"></span>Contenido</div>
            <div class="estudio-console-prose">${esc(fullText || '—')}</div>
          </div>
        </div>
        <div class="estudio-console-rail">
          ${safeUrl ? `
          <div class="estudio-console-rail-section">
            <div class="estudio-console-section-label neutral"><span class="dot"></span>Fuente</div>
            <a class="estudio-console-source-link" href="${safeUrl}" target="_blank" rel="noopener noreferrer">${safeUrl}</a>
          </div>` : ''}
          <div class="estudio-console-rail-section">
            <button class="estudio-mark-read-btn" id="estudioMarkReadBtn" ${ex.read ? 'disabled' : ''}>
              ${ex.read ? 'Leído' : 'Marcar como leído'}
            </button>
          </div>
        </div>
      </div>
    </div>
  `

  const btn = document.getElementById('estudioMarkReadBtn')
  if (btn && !ex.read) {
    btn.addEventListener('click', () => _markExplorationRead(idx, btn))
  }

  detail.classList.add('open')
}

// Marks an exploration entry read on the backend (data/explorations.json),
// then updates the local cache + button + list in place — no full
// _loadEstudioData() round trip needed (the 'estudio_updated' socket emit
// from core/estudio_routes.py still keeps any OTHER open tab/window in sync).
async function _markExplorationRead(idx, btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Guardando…' }
  try {
    const res = await fetch(`${JARVIS_API}/api/estudio/explorations/read`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ index: idx }),
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    if (_estudioData.explorations[idx]) _estudioData.explorations[idx].read = true
    if (btn) btn.textContent = 'Leído'
    if (_currentEstudioSub === 'exploraciones') _renderEstudioExploraciones()
  } catch {
    if (btn) { btn.disabled = false; btn.textContent = 'Marcar como leído' }
  }
}

// ── 4. IDEAS — already functional, pulled from data/sleep_insights.json ──
function _renderEstudioIdeas() {
  const list = document.getElementById('estudioIdeasList')
  if (!list) return
  const items = _estudioData.ideas   // already sorted by relevance descending — see core/estudio_routes.py
  if (!items.length) {
    list.innerHTML = '<div class="estudio-empty">LIRA generará ideas durante los ciclos de sueño.</div>'
    return
  }
  list.innerHTML = items.map(idea => `
    <div class="estudio-card">
      <div class="estudio-card-header">
        <span class="estudio-card-title">${esc(idea.description || '')}</span>
        <span class="estudio-card-date">${esc(_estudioFormatDate(idea.date))}</span>
      </div>
      <div class="estudio-card-meta">
        <span class="estudio-tag">${esc(idea.source || '—')}</span>
        <span class="estudio-card-relevance">Relevancia ${Math.round((idea.relevance || 0) * 100)}%</span>
      </div>
    </div>
  `).join('')
}

// ── 5. DOCUMENTOS ─────────────────────────────────────────────────────────
function _renderEstudioDocumentos() {
  const list = document.getElementById('estudioDocumentosList')
  if (!list) return
  const items = _estudioData.documents
  if (!items.length) {
    list.innerHTML = '<div class="estudio-empty">Los documentos que LIRA genere aparecerán aquí.</div>'
    return
  }
  list.innerHTML = items.map(doc => `
    <div class="estudio-card">
      <div class="estudio-card-header">
        <span class="estudio-card-title">${esc(doc.title || 'Documento sin título')}</span>
        <span class="estudio-card-date">${esc(_estudioFormatDate(doc.date))}</span>
      </div>
      <div class="estudio-card-meta">
        <span class="estudio-tag">${esc(doc.type || '—')}</span>
        <span class="estudio-card-wordcount">${esc(String(doc.word_count ?? '—'))} palabras</span>
      </div>
    </div>
  `).join('')
}

// ── Shared date formatting — 'YYYY-MM-DD' or a full ISO timestamp, both
// rendered as a short local date; falls back to the raw string if it
// doesn't parse (never throws on malformed/missing data). ──────────────
function _estudioFormatDate(dateStr) {
  if (!dateStr) return ''
  const d = new Date(dateStr)
  if (isNaN(d.getTime())) return dateStr
  return d.toLocaleDateString('es-ES', { day: '2-digit', month: 'short', year: 'numeric' })
}
