// core-tabs-sleep-panel.js — NUCLEO LIRA sub-tab switching, estado polling, think list, and sleep questions/reflections rendering.
const CORE_MODE_LABELS = { wake_word: 'Wake Word', conversation: 'Conversación' }

async function _renderCoreEstado() {
  const body = document.getElementById('coreEstadoBody')
  if (!body) return

  let info = {}
  try {
    const res = await fetch(`${JARVIS_API}/api/info`)
    info = await res.json()
  } catch { /* leave info empty — still show whatever's already tracked locally below */ }

  const latency    = info.last_latency || {}
  const latencyStr = latency.total != null ? `${latency.total.toFixed(2)}s` : '—'
  const modelStr    = latency.model || (info.groq_model_chain && info.groq_model_chain[0]) || '—'

  // Reflective mode's token budget — see core.reflective / GET /api/info's
  // 'reflective' field and data/reflective_budget.json.
  const reflective = info.reflective || {}
  const reflectiveStr = (reflective.tokens_used_today != null && reflective.daily_budget != null)
    ? `Modo reflexivo: ${reflective.tokens_used_today}/${reflective.daily_budget} tokens usados hoy`
    : '—'
  let lastSessionStr = 'Aún no hay sesiones reflexivas'
  if (reflective.last_session_at) {
    const when = new Date(reflective.last_session_at).toLocaleString('es-ES', {
      day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
    })
    const insights = reflective.last_session_insights || 0
    lastSessionStr = `${when} — ${insights} insight${insights === 1 ? '' : 's'} generado${insights === 1 ? '' : 's'}`
  }

  // Próximo ciclo — rough estimate of when the 20-minute idle auto-trigger
  // could next fire, while nothing is currently sleeping. The detailed
  // "last sleep" stats (cycles/duration/deleted/merged/promoted/insights/
  // connections) live in their own dedicated ÚLTIMO SUEÑO section below
  // (see _renderSleepSummarySection()), sourced from the purpose-built
  // GET /api/sleep_summary rather than folded into these generic rows.
  let nextSleepStr = '—'
  try {
    const sleepRes    = await fetch(`${JARVIS_API}/api/sleep/status`)
    const sleepStatus = await sleepRes.json()
    if (!(sleepStatus.continuous && sleepStatus.continuous.running) && sleepStatus.next_trigger_seconds != null) {
      const mins = Math.round(sleepStatus.next_trigger_seconds / 60)
      nextSleepStr = mins <= 0 ? 'En cualquier momento' : `En ~${mins} min`
    } else if (sleepStatus.continuous && sleepStatus.continuous.running) {
      nextSleepStr = 'Durmiendo ahora — ver ÚLTIMO SUEÑO abajo'
    }
  } catch { /* leave default — a failed fetch here shouldn't blank out the rest of Estado */ }

  const rows = [
    ['Personalidad',    (typeof PERSONALITY_LABEL !== 'undefined' ? PERSONALITY_LABEL : currentPersonality)],
    ['Modo',             CORE_MODE_LABELS[_listenMode] || _listenMode || '—'],
    ['Tiempo activo',    info.session_uptime || '—'],
    ['Última latencia',  latencyStr],
    ['Modelo Groq',      modelStr],
    ['Micrófono',        _isMuted ? 'Silenciado' : 'Activo'],
    ['Voz (TTS)',        _isTtsMuted ? 'Silenciada' : 'Activa'],
    ['Conexión',         (jarvisSocket && jarvisSocket.connected) ? 'Conectado' : 'Desconectado'],
    ['Presupuesto reflexivo', reflectiveStr],
    ['Última sesión reflexiva', lastSessionStr],
    ['Próximo ciclo de sueño', nextSleepStr],
  ]
  body.innerHTML = rows.map(([k, v]) => `
    <div class="info-row core-fact-row"><span class="info-key">${esc(k)}</span><span class="info-val">${esc(String(v))}</span></div>
  `).join('')

  body.innerHTML += await _renderSleepSummarySection()
}

// ── ÚLTIMO SUEÑO — see GET /api/sleep_summary / core.sleep.get_sleep_summary().
// Three states: currently sleeping (pulsing "DURMIENDO — Ciclo X · Fase Y"
// line, per spec), never slept ("Sin ciclos de sueño registrados aún"), or
// a finished run's stats (date/time, cycles, duration, deleted/merged/
// promoted facts, insights generated, mind-map connections updated).
async function _renderSleepSummarySection() {
  let summary = null
  try {
    const res = await fetch(`${JARVIS_API}/api/sleep_summary`)
    summary = await res.json()
  } catch { return '' }
  if (!summary || summary.error) return ''

  if (summary.current && summary.current.running) {
    const c = summary.current
    return `
      <div class="core-section-label">ÚLTIMO SUEÑO</div>
      <div class="core-sleep-status core-sleep-status-active">DURMIENDO — Ciclo ${c.current_cycle || 0} · Fase ${c.current_phase_num || 0}: ${esc(c.current_phase || '…')}</div>
    `
  }

  if (!summary.has_ever_slept) {
    return `
      <div class="core-section-label">ÚLTIMO SUEÑO</div>
      <div class="core-empty-note">Sin ciclos de sueño registrados aún</div>
    `
  }

  const whenSource = summary.stopped_at || summary.started_at
  const when = whenSource
    ? new Date(whenSource).toLocaleString('es-ES', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
    : '—'
  const durationStr = summary.duration_seconds == null
    ? '—'
    : summary.duration_seconds < 60
      ? `${Math.round(summary.duration_seconds)}s`
      : `${Math.round(summary.duration_seconds / 60)} min`

  const statRows = [
    ['Fecha/hora',              when],
    ['Ciclos completados',      String(summary.total_cycles_completed || 0)],
    ['Duración total',          durationStr],
    ['Hechos eliminados',       String(summary.total_deleted || 0)],
    ['Hechos fusionados',       String(summary.total_merged || 0)],
    ['Hechos promovidos',       String(summary.total_promoted || 0)],
    ['Insights generados',      String(summary.total_insights_generated || 0)],
    ['Conexiones actualizadas', String(summary.total_mind_map_updates || 0)],
  ]
  return `
    <div class="core-section-label">ÚLTIMO SUEÑO</div>
    ${statRows.map(([k, v]) => `<div class="info-row core-fact-row"><span class="info-key">${esc(k)}</span><span class="info-val">${esc(v)}</span></div>`).join('')}
  `
}

function _startCoreEstadoPoll() {
  _stopCoreEstadoPoll()
  _coreEstadoPollTimer = setInterval(() => {
    if (_currentSection === 'core' && _currentCoreSub === 'estado') _renderCoreEstado()
  }, CORE_ESTADO_POLL_MS)
}
function _stopCoreEstadoPoll() {
  clearInterval(_coreEstadoPollTimer)
  _coreEstadoPollTimer = null
}

// ── Pensamiento ──────────────────────────────────────────────────────────
let _coreThinkEntries = []   // newest first, capped at 10 — see _onLiraThinking()

// Reveals `text` progressively into `el` — a subtle typewriter effect, not
// genuine token-by-token streaming (the backend emits the whole finished
// block in one 'lira_thinking' event — see core.commands._groq_complete()).
// Reveals in small chunks rather than one character at a time so a long
// block doesn't take unreasonably long to finish appearing.
function _typewriterReveal(el, text) {
  el.textContent = ''
  let i = 0
  const step = Math.max(1, Math.ceil(text.length / 120))
  const timer = setInterval(() => {
    i += step
    el.textContent = text.slice(0, i)
    if (i >= text.length) clearInterval(timer)
  }, 12)
}

function _renderCoreThinkList() {
  const list = document.getElementById('coreThinkList')
  if (!list) return
  if (!_coreThinkEntries.length) {
    list.innerHTML = '<div class="core-think-empty">Modelo sin razonamiento visible</div>'
    return
  }
  list.innerHTML = _coreThinkEntries.map((e, i) => `
    <div class="estudio-console-item-card">
      <div class="estudio-console-item-head">
        <span class="estudio-console-item-type">↳ ${esc(e.query || '—')}</span>
      </div>
      <div class="estudio-console-item-text" id="coreThinkBody${i}" style="margin-bottom:6px;"></div>
      <div class="estudio-console-item-links">${esc(e.model || '')}</div>
    </div>
  `).join('')
  // Only the newest LIVE arrival plays the typewriter effect — backfilled
  // history from GET /api/think_log already happened, so it just appears.
  _coreThinkEntries.forEach((e, i) => {
    const bodyEl = document.getElementById(`coreThinkBody${i}`)
    if (!bodyEl) return
    if (i === 0 && e._fresh) _typewriterReveal(bodyEl, e.thinking || '')
    else bodyEl.textContent = e.thinking || ''
  })
}

async function _loadThinkLog() {
  if (_coreThinkEntries.length) { _renderCoreThinkList(); return }   // already have live/backfilled data
  try {
    const res  = await fetch(`${JARVIS_API}/api/think_log`)
    const data = await res.json()
    _coreThinkEntries = (data.entries || []).map(e => ({ ...e, _fresh: false }))
  } catch { /* leave whatever's already there */ }
  _renderCoreThinkList()
}

function _onLiraThinking(data) {
  _coreThinkEntries.unshift({ ...data, _fresh: true })
  if (_coreThinkEntries.length > 10) _coreThinkEntries.length = 10
  if (_currentSection === 'core' && _currentCoreSub === 'pensamiento') _renderCoreThinkList()
}

// ── Sleep insights (PREGUNTAS DURANTE EL SUEÑO / REFLEXIONES DEL SUEÑO) ──
// GET /api/sleep_insights — see core.sleep.get_sleep_insights_summary().
// Both lists reuse .estudio-console-item-card (same shell as the thinking
// feed above) — resolved questions get the green "confident" tick,
// unresolved stay neutral cyan, reflections get violet (same tone this
// aesthetic already gives ESTUDIO's "open questions"/forward-looking
// content elsewhere).
function _renderSleepQuestionsList(questions) {
  const list = document.getElementById('coreSleepQuestionsList')
  if (!list) return
  if (!questions || !questions.length) {
    list.innerHTML = '<div class="core-think-empty">Sin preguntas pendientes</div>'
    return
  }
  list.innerHTML = questions.map(q => {
    const pct  = Math.round((q.confidence || 0) * 100)
    const meta = [q.cycle != null ? `Ciclo ${q.cycle}` : null, `Confianza ${pct}%`].filter(Boolean).join(' · ')
    const hc   = q.resolved ? 'var(--green)' : 'var(--accent)'
    return `
      <div class="estudio-console-item-card${q.resolved ? ' core-sleep-resolved' : ''}" style="--hc:${hc};">
        <div class="estudio-console-item-text" style="margin-bottom:6px;">${esc(q.text)}</div>
        <div class="estudio-console-item-links">${esc(meta)}${q.resolved ? ' · <span class="core-sleep-resolved-tag">✓ Resuelta</span>' : ''}</div>
      </div>
    `
  }).join('')
}

function _renderSleepReflectionsList(reflections) {
  const list = document.getElementById('coreSleepReflectionsList')
  if (!list) return
  if (!reflections || !reflections.length) {
    list.innerHTML = '<div class="core-think-empty">Sin reflexiones registradas aún</div>'
    return
  }
  list.innerHTML = reflections.map(r => {
    const meta = [r.phase, r.cycle != null ? `Ciclo ${r.cycle}` : null].filter(Boolean).join(' · ')
    return `
      <div class="estudio-console-item-card" style="--hc:#9a78f5;">
        <div class="estudio-console-item-text" style="margin-bottom:6px;">${esc(r.text)}</div>
        <div class="estudio-console-item-links">${esc(meta)}</div>
      </div>
    `
  }).join('')
}

async function _loadSleepInsights() {
  try {
    const res  = await fetch(`${JARVIS_API}/api/sleep_insights`)
    const data = await res.json()
    _renderSleepQuestionsList(data.questions || [])
    _renderSleepReflectionsList(data.reflections || [])
  } catch { /* leave whatever's already there */ }
}

// ── Memoria ──────────────────────────────────────────────────────────────
const CORE_CATEGORY_LABELS = {
  personal: 'Personal', preference: 'Preferencias', project: 'Proyectos', skill: 'Habilidades',
  relationship: 'Relaciones', interaction: 'Interacción', context: 'Contexto', reference: 'Referencias',
}

async function _renderCoreMemoria() {
  const body = document.getElementById('coreMemoriaBody')
  if (!body) return
  body.innerHTML = '<div class="core-empty-note">Cargando…</div>'

  let data = {}
  try {
    const res = await fetch(`${JARVIS_API}/api/memory_active`)
    data = await res.json()
  } catch {
    body.innerHTML = '<div class="core-empty-note">No se pudo cargar la memoria.</div>'
    return
  }

  const facts    = data.facts || {}
  const episodes = data.episodes || []
  const hud      = data.hud_context || {}

  let html = '<div class="core-section-label">Hechos activos</div>'
  const categories = Object.keys(facts)
  if (!categories.length) {
    html += '<div class="core-empty-note">Sin hechos guardados aún.</div>'
  } else {
    categories.forEach(cat => {
      html += `<div class="core-section-label">${esc(CORE_CATEGORY_LABELS[cat] || cat)}</div>`
      html += `<ul class="estudio-console-list">${facts[cat].map(f => `<li>${esc(f.fact)}</li>`).join('')}</ul>`
    })
  }

  html += '<div class="core-section-label">Episodios recientes</div>'
  if (!episodes.length) {
    html += '<div class="core-empty-note">Sin episodios guardados aún.</div>'
  } else {
    episodes.forEach(e => {
      html += `
        <div class="estudio-console-item-card">
          <div class="estudio-console-item-head">
            <span class="estudio-console-item-type">${esc(e.date || '')}</span>
            <span class="estudio-console-item-title">Importancia ${esc(String(e.importance ?? '—'))}</span>
          </div>
          <div class="estudio-console-item-text" style="margin-bottom:0;">${esc(e.summary || '')}</div>
        </div>`
    })
  }

  html += '<div class="core-section-label">Contexto de pantalla actual</div>'
  if (hud.type) {
    html += `<div class="info-row core-fact-row"><span class="info-key">Tipo</span><span class="info-val">${esc(hud.type)}</span></div>`
  } else {
    html += '<div class="core-empty-note">Sin contexto de pantalla activo.</div>'
  }

  body.innerHTML = html
}

// ── Mapa Mental ──────────────────────────────────────────────────────────
// Interactive D3 force-directed graph — memory facts, episodes, armor
// models, and concepts, pulled from GET /api/memory_active (facts/
// episodes/concepts, see core.commands.get_active_memory) plus armor
// models via _fetchArmorModels() (ui/js/mm-wiring.js — GET /api/armor,
// core/armor_manager.py), the same source Armor Bay itself renders from,
// so this never sees a second, divergent copy of that data.
//
// Edge logic mirrors core/commands.py's own _fact_similarity/_keywords
// design ("cheap, dependency-free... simple keyword matching") rather than
// inventing a different heuristic: two texts are considered related if
// they share at least one meaningful (length > 2, non-stopword) word.

// ── Módulos — capability catalog (core.module_manager, data/
// modules_catalog.json) — EVERY capability LIRA has, is building towards,
// or has merely planned, not just the ones currently loaded/running (that
// narrower runtime view is GET /api/modules — this uses the separate
// GET /api/modules/catalog instead). Grouped by category, collapsible;
// clicking a row expands its detail (description/dependencies/
// permissions/priority) inline. Read-only by design — no install/enable
// controls yet, per spec.
const CORE_MODULE_STATUS_EMOJI = {
  planned:      '⚪',
  researching:  '🔵',
  designing:    '🟡',
  developing:   '🟠',
  testing:      '🟣',
  ready:        '🟢',
  installed:    '🟢',
  updating:     '🔄',
  error:        '🔴',
}
// Same tiers as .core-module-status-* (armor-mindmap-detail.css) — reused
// here as inline chip colors since the expanded detail's status chip
// isn't a fixed small set of class names the way ESTUDIO's investigation
// statuses are.
const CORE_MODULE_STATUS_COLOR = {
  installed: 'var(--green)', ready: 'var(--green)',
  error: 'var(--red)', updating: 'var(--yellow)',
  developing: 'rgba(255,255,255,0.5)', testing: 'rgba(255,255,255,0.5)',
  designing: 'rgba(255,255,255,0.5)', researching: 'rgba(255,255,255,0.5)',
  planned: 'rgba(255,255,255,0.28)',
}

// Collapse state persists across re-renders within the same page session
// (categories start expanded — module rows start collapsed).
const _coreModuleCollapsedCategories = new Set()
const _coreModuleExpandedRows        = new Set()

async function _renderCoreModulos() {
  const body = document.getElementById('coreModulosBody')
  if (!body) return

  let catalog = []
  try {
    const res = await fetch(`${JARVIS_API}/api/modules/catalog`)
    catalog = await res.json()
  } catch {
    body.innerHTML = '<div class="core-empty-note">No se pudo cargar el catálogo de módulos.</div>'
    return
  }
  if (!Array.isArray(catalog) || !catalog.length) {
    body.innerHTML = '<div class="core-empty-note">El catálogo de módulos está vacío.</div>'
    return
  }

  const byCategory = {}
  catalog.forEach(m => {
    const cat = m.category || '—'
    ;(byCategory[cat] = byCategory[cat] || []).push(m)
  })
  const categories = Object.keys(byCategory).sort((a, b) => a.localeCompare(b, 'es'))

  body.innerHTML = categories.map(cat => {
    const items    = byCategory[cat].sort((a, b) => (a.name || '').localeCompare(b.name || '', 'es'))
    const collapsed = _coreModuleCollapsedCategories.has(cat)
    return `
      <div class="core-module-category">
        <button class="core-module-category-head" data-toggle-category="${esc(cat)}">
          <span class="core-module-category-chevron">${collapsed ? '▸' : '▾'}</span>
          <span class="core-module-category-name">${esc(cat)}</span>
          <span class="core-module-category-count">${items.length}</span>
        </button>
        <div class="core-module-category-body" style="${collapsed ? 'display:none' : ''}">
          ${items.map(m => _renderCatalogRow(m)).join('')}
        </div>
      </div>
    `
  }).join('')

  body.querySelectorAll('[data-toggle-category]').forEach(btn => {
    btn.addEventListener('click', () => {
      const cat = btn.dataset.toggleCategory
      if (_coreModuleCollapsedCategories.has(cat)) _coreModuleCollapsedCategories.delete(cat)
      else _coreModuleCollapsedCategories.add(cat)
      _renderCoreModulos()
    })
  })
  body.querySelectorAll('[data-module-row]').forEach(row => {
    row.addEventListener('click', () => {
      const id = row.dataset.moduleRow
      if (_coreModuleExpandedRows.has(id)) _coreModuleExpandedRows.delete(id)
      else _coreModuleExpandedRows.add(id)
      _renderCoreModulos()
    })
  })
  body.querySelectorAll('[data-block-toggle]').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation()
      _toggleCatalogBlock(btn.dataset.blockToggle, btn.dataset.blocked === 'true')
    })
  })
  body.querySelectorAll('[data-priority-step]').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation()
      _stepCatalogPriority(btn.dataset.moduleId, parseInt(btn.dataset.priorityStep, 10))
    })
  })
  body.querySelectorAll('[data-build-action]').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation()
      const id = btn.dataset.moduleId
      if (btn.dataset.buildAction === 'update') {
        const input = body.querySelector(`[data-change-input="${CSS.escape(id)}"]`)
        const change = (input && input.value || '').trim()
        if (!change) { input && input.focus(); return }
        _triggerModuleBuild(id, 'update', change)
      } else {
        _triggerModuleBuild(id, 'create')
      }
    })
  })
}

async function _toggleCatalogBlock(catalogId, currentlyBlocked) {
  try {
    await fetch(`${JARVIS_API}/api/modules/catalog/${encodeURIComponent(catalogId)}/block`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ blocked: !currentlyBlocked }),
    })
  } catch { /* re-render below reflects whatever the backend actually ended up with */ }
  _renderCoreModulos()
}

async function _stepCatalogPriority(catalogId, delta) {
  const catalog = await (async () => {
    try {
      const res = await fetch(`${JARVIS_API}/api/modules/catalog`)
      return await res.json()
    } catch { return [] }
  })()
  const entry = Array.isArray(catalog) ? catalog.find(m => m.id === catalogId) : null
  const current = entry && entry.priority != null ? entry.priority : 3
  const next = Math.max(1, current + delta)
  try {
    await fetch(`${JARVIS_API}/api/modules/catalog/${encodeURIComponent(catalogId)}/priority`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ priority: next }),
    })
  } catch { /* re-render below reflects whatever the backend actually ended up with */ }
  _renderCoreModulos()
}

// ── Build/update trigger — POST /api/code-engine/create|update, both
// fire-and-forget on the backend (spawned in a daemon thread, returns
// immediately) since a real generation involves LLM calls + sandbox +
// up to 3 retries and can take minutes. _pollModuleBuild() re-fetches the
// catalog every 5s (reusing _renderCoreModulos() itself, which keeps
// expand/collapse state) until this entry reaches a terminal status
// (installed/error) or ~3 minutes pass, so the row's status/emoji update
// live without needing a manual refresh.
const _coreModuleBuildMessages = {}   // catalogId -> ephemeral status line shown under its build button
const _coreModuleBuildPolls    = {}   // catalogId -> setInterval id, so a second click can't double-poll

async function _triggerModuleBuild(catalogId, action, change) {
  const isUpdate = action === 'update'
  const url  = isUpdate
    ? `${JARVIS_API}/api/code-engine/update/${encodeURIComponent(catalogId)}`
    : `${JARVIS_API}/api/code-engine/create/${encodeURIComponent(catalogId)}`
  const opts = isUpdate
    ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ change }) }
    : { method: 'POST' }

  _coreModuleBuildMessages[catalogId] = 'Iniciando…'
  _renderCoreModulos()
  try {
    const res  = await fetch(url, opts)
    const data = await res.json()
    _coreModuleBuildMessages[catalogId] = data.ok
      ? (isUpdate ? 'Actualización iniciada — puede tardar varios minutos.' : 'Generación iniciada — puede tardar varios minutos.')
      : (data.error || 'No se pudo iniciar.')
    if (data.ok) _pollModuleBuild(catalogId)
  } catch {
    _coreModuleBuildMessages[catalogId] = 'Error de red al iniciar.'
  }
  _renderCoreModulos()
}

function _pollModuleBuild(catalogId) {
  if (_coreModuleBuildPolls[catalogId]) return
  let ticks = 0
  const maxTicks = 36   // ~36 * 5s = 3 min
  _coreModuleBuildPolls[catalogId] = setInterval(async () => {
    ticks++
    let entry = null
    try {
      const res = await fetch(`${JARVIS_API}/api/modules/catalog`)
      const catalog = await res.json()
      entry = Array.isArray(catalog) ? catalog.find(m => m.id === catalogId) : null
    } catch { /* transient — keep polling rather than giving up on one failed fetch */ }

    const terminal = entry && (entry.status === 'installed' || entry.status === 'error')
    if (terminal || ticks >= maxTicks) {
      clearInterval(_coreModuleBuildPolls[catalogId])
      delete _coreModuleBuildPolls[catalogId]
      delete _coreModuleBuildMessages[catalogId]
    }
    _renderCoreModulos()
  }, 5000)
}

function _renderModuleBuildActions(m, status, blocked) {
  const message = _coreModuleBuildMessages[m.id] || ''
  if (blocked) {
    return `<div class="core-module-build-actions"><span class="core-module-build-hint">Bloqueado — desbloquea primero para crear o actualizar.</span></div>`
  }
  if (status === 'updating' || _coreModuleBuildPolls[m.id]) {
    return `
      <div class="core-module-build-actions">
        <span class="core-module-build-status">${esc(message || 'Generando…')}</span>
      </div>
    `
  }
  if (status === 'installed' || status === 'ready') {
    return `
      <div class="core-module-build-actions">
        <input type="text" class="core-module-change-input" data-change-input="${esc(m.id)}"
               placeholder="Describe el cambio a aplicar…">
        <button class="core-module-build-btn" data-build-action="update" data-module-id="${esc(m.id)}">Actualizar</button>
        ${message ? `<span class="core-module-build-status">${esc(message)}</span>` : ''}
      </div>
    `
  }
  return `
    <div class="core-module-build-actions">
      <button class="core-module-build-btn" data-build-action="create" data-module-id="${esc(m.id)}">Crear módulo</button>
      ${message ? `<span class="core-module-build-status">${esc(message)}</span>` : ''}
    </div>
  `
}

function _renderCatalogRow(m) {
  const status   = m.status || 'planned'
  const emoji    = CORE_MODULE_STATUS_EMOJI[status] || '⚪'
  const version  = m.version ? `v${esc(m.version)}` : ''
  const expanded = _coreModuleExpandedRows.has(m.id)
  const blocked  = !!m.blocked
  const priority = m.priority ?? null

  let detail = ''
  if (expanded) {
    const deps  = Array.isArray(m.dependencies) && m.dependencies.length ? m.dependencies.map(esc).join(', ') : 'Ninguna'
    const perms = Array.isArray(m.permissions) && m.permissions.length ? m.permissions.map(esc).join(', ') : 'Ninguno'
    const chipColor = CORE_MODULE_STATUS_COLOR[status] || 'var(--accent)'
    // Same estudio-console-* hero/body-grid/rail shell as ESTUDIO/Armor Bay
    // — every interactive control below (priority stepper, block toggle,
    // build/update trigger) keeps its exact data-* attributes, so the
    // delegated listeners in _renderCoreModulos() need no changes at all.
    detail = `
      <div class="estudio-console">
        <div class="estudio-console-hero">
          <div class="estudio-console-hero-main">
            <div class="estudio-console-hero-eyebrow">
              <span class="estudio-console-chip" style="color:${chipColor};border-color:${chipColor};">${emoji} ${esc(status.toUpperCase())}</span>
              ${version ? `<span class="estudio-console-meta-readout">${esc(version)}</span>` : ''}
            </div>
            <div class="estudio-console-hero-title" style="font-size:1rem;">${esc(m.name || m.id)}</div>
          </div>
        </div>
        <div class="estudio-console-body-grid">
          <div class="estudio-console-col-main">
            <div class="estudio-console-block">
              <div class="estudio-console-section-label neutral"><span class="dot"></span>Descripción</div>
              <div class="estudio-console-prose">${esc(m.description || '—')}</div>
            </div>
            <div class="estudio-console-block">
              <div class="estudio-console-section-label neutral"><span class="dot"></span>Dependencias y permisos</div>
              <div class="estudio-console-item-card">
                <div class="estudio-console-item-links">Dependencias: ${deps}</div>
                <div class="estudio-console-item-links">Permisos: ${perms}</div>
              </div>
            </div>
          </div>
          <div class="estudio-console-rail">
            <div class="estudio-console-rail-section">
              <div class="estudio-console-section-label neutral"><span class="dot"></span>Prioridad</div>
              <div class="core-module-priority-editor">
                <button class="core-module-priority-btn" data-priority-step="1" data-module-id="${esc(m.id)}" title="Menos prioritario">−</button>
                <span class="core-module-priority-value">${esc(priority === null ? '—' : String(priority))}</span>
                <button class="core-module-priority-btn" data-priority-step="-1" data-module-id="${esc(m.id)}" title="Más prioritario">+</button>
              </div>
              <button class="core-module-block-btn${blocked ? ' is-blocked' : ''}" data-block-toggle="${esc(m.id)}" data-blocked="${blocked}">
                ${blocked ? 'Desbloquear' : 'Bloquear (no crear/actualizar)'}
              </button>
            </div>
            <div class="estudio-console-rail-section">
              <div class="estudio-console-section-label neutral"><span class="dot"></span>Build</div>
              ${_renderModuleBuildActions(m, status, blocked)}
            </div>
          </div>
        </div>
      </div>
    `
  }

  return `
    <div class="core-module-row${expanded ? ' expanded' : ''}${blocked ? ' blocked' : ''}" data-module-row="${esc(m.id)}">
      <span class="core-module-emoji">${emoji}</span>
      <span class="core-module-name">${esc(m.name || m.id)}</span>
      ${blocked ? '<span class="core-module-blocked-tag" title="Bloqueado — no se creará ni actualizará">🚫</span>' : ''}
      <span class="core-module-version">${version}</span>
      <span class="core-module-status-label core-module-status-${esc(status)}">${esc(status.toUpperCase())}</span>
    </div>
    ${detail}
  `
}

// ════════════════════════════════════════════════════════════════════════════
// PERSONAS — every person LIRA has a saved record of (core/social.py,
// data/social_profiles.json via core/routes_social.py), with a Joan-
// editable trust tier and knows_lira flag. Modeled directly on the
// Módulos list above (same .core-module-row/-detail shell, same
// expand-on-click/re-render-whole-body approach) — a flat list, not
// grouped by category, since this is a handful of people, not dozens of
// catalog entries.
//
// Four named tiers map onto the existing 0.0-1.0 float trust_level (see
// core/routes_social.py's own comment on the extended trust route) —
// "Desconocido" isn't a 5th trust_level range, it's trust_confirmed
// still being false (Joan has never reviewed this person), shown
// regardless of whatever numeric default the record happens to carry.
// ════════════════════════════════════════════════════════════════════════════
const PERSONA_TIERS = [
  { key: 'owner',    label: 'Owner',    value: 1.0  },
  { key: 'private',  label: 'Private',  value: 0.75 },
  { key: 'personal', label: 'Personal', value: 0.45 },
  { key: 'public',   label: 'Public',   value: 0.15 },
]

function _personaTierFor(person) {
  if (!person.trust_confirmed) return null   // Desconocido
  const t = person.trust_level
  if (t >= 0.85) return 'owner'
  if (t >= 0.6)  return 'private'
  if (t >= 0.3)  return 'personal'
  return 'public'
}

const _corePersonaExpandedRows = new Set()
let _corePersonaAdding = false

async function _renderCorePersonas() {
  const body = document.getElementById('corePersonasBody')
  if (!body) return

  let people = []
  try {
    const res = await fetch(`${JARVIS_API}/api/social/people`)
    const data = await res.json()
    people = Array.isArray(data.people) ? data.people : []
  } catch {
    body.innerHTML = '<div class="core-empty-note">No se pudo cargar la lista de personas.</div>'
    return
  }

  // Joan first (she's not just another row — her trust is fixed), then
  // alphabetical by name.
  people.sort((a, b) => {
    if (a.id === 'joan') return -1
    if (b.id === 'joan') return 1
    return (a.name || '').localeCompare(b.name || '', 'es')
  })

  body.innerHTML = `
    <div class="core-persona-add">
      ${_corePersonaAdding ? _renderPersonaAddForm() : '<button class="core-module-build-btn" id="corePersonaAddBtn">+ Añadir persona</button>'}
    </div>
    ${people.length ? people.map(p => _renderPersonaRow(p)).join('') : '<div class="core-empty-note">LIRA aún no tiene ningún perfil guardado además de Joan.</div>'}
  `

  const addBtn = document.getElementById('corePersonaAddBtn')
  if (addBtn) addBtn.addEventListener('click', () => { _corePersonaAdding = true; _renderCorePersonas() })
  _wirePersonaAddForm(body)

  body.querySelectorAll('[data-persona-row]').forEach(row => {
    row.addEventListener('click', () => {
      const id = row.dataset.personaRow
      if (_corePersonaExpandedRows.has(id)) _corePersonaExpandedRows.delete(id)
      else _corePersonaExpandedRows.add(id)
      _renderCorePersonas()
    })
  })
  body.querySelectorAll('[data-persona-tier]').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation()
      _setPersonaTrust(btn.dataset.personaId, parseFloat(btn.dataset.personaTier))
    })
  })
  body.querySelectorAll('[data-persona-knows-toggle]').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation()
      _editPersona(btn.dataset.personaId, { knows_lira: btn.dataset.knowsLira !== 'true' })
    })
  })
  body.querySelectorAll('[data-persona-name-save]').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation()
      const id = btn.dataset.personaNameSave
      const input = body.querySelector(`[data-persona-name-input="${CSS.escape(id)}"]`)
      const name = (input && input.value || '').trim()
      if (!name) { input && input.focus(); return }
      _editPersona(id, { name })
    })
  })
  body.querySelectorAll('[data-persona-delete]').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation()
      if (!confirm(`¿Olvidar a ${btn.dataset.personaName}? Esto borra su perfil e historial de interacciones.`)) return
      _deletePersona(btn.dataset.personaDelete)
    })
  })
}

// Same colors as .core-persona-badge-* (armor-mindmap-detail.css), reused
// inline for the expanded detail's hero chip — that CSS family stays as
// the class-based version for the collapsed row's small badge.
const CORE_PERSONA_TIER_COLOR = {
  owner: 'var(--yellow)', private: 'var(--accent)', personal: 'var(--green)',
  public: 'rgba(255,255,255,0.45)', unknown: 'rgba(255,255,255,0.28)',
}

function _renderPersonaRow(p) {
  const isJoan   = p.id === 'joan'
  const tierKey  = _personaTierFor(p)
  const tierInfo = PERSONA_TIERS.find(t => t.key === tierKey)
  const badgeLabel = tierInfo ? tierInfo.label : 'Desconocido'
  const badgeClass = `core-persona-badge-${tierInfo ? tierInfo.key : 'unknown'}`
  const expanded = _corePersonaExpandedRows.has(p.id)

  let detail = ''
  if (expanded) {
    const tierButtons = isJoan
      ? `<span class="core-persona-tier-fixed">Owner — fija</span>`
      : PERSONA_TIERS.map(t => `
          <button class="core-persona-tier-btn${tierKey === t.key ? ' active' : ''}"
                  data-persona-tier="${t.value}" data-persona-id="${esc(p.id)}">${t.label}</button>
        `).join('')
    const chipColor = CORE_PERSONA_TIER_COLOR[tierInfo ? tierInfo.key : 'unknown']

    // Same estudio-console-* hero/body-grid/rail shell as Módulos above —
    // every interactive control (tier picker, knows-LIRA toggle, rename
    // form, delete) keeps its exact data-* attributes, so the delegated
    // listeners in _renderCorePersonas() need no changes.
    detail = `
      <div class="estudio-console">
        <div class="estudio-console-hero">
          <div class="estudio-console-hero-main">
            <div class="estudio-console-hero-eyebrow">
              <span class="estudio-console-chip" style="color:${chipColor};border-color:${chipColor};">${esc(badgeLabel)}</span>
              <span class="estudio-console-meta-readout">${p.interaction_count ?? 0} interacciones</span>
            </div>
            <div class="estudio-console-hero-title" style="font-size:1rem;">${isJoan ? '👑' : '👤'} ${esc(p.name || p.id)}</div>
            ${p.relationship_to_joan ? `<div class="estudio-console-hero-subtitle">${esc(p.relationship_to_joan)}</div>` : ''}
          </div>
        </div>
        <div class="estudio-console-body-grid">
          <div class="estudio-console-col-main">
            <div class="estudio-console-block">
              <div class="estudio-console-section-label neutral"><span class="dot"></span>Confianza</div>
              <div class="core-persona-tier-picker" style="margin-top:0;padding-top:0;border-top:none;">
                <div class="core-persona-tier-buttons">${tierButtons}</div>
              </div>
            </div>
            ${!isJoan ? `
            <div class="estudio-console-block">
              <div class="estudio-console-section-label neutral"><span class="dot"></span>Editar</div>
              <div class="toggle-row core-persona-knows-row">
                <span class="toggle-label">Conoce a LIRA</span>
                <button class="toggle-switch${p.knows_lira ? ' on' : ''}" data-persona-knows-toggle="1"
                        data-persona-id="${esc(p.id)}" data-knows-lira="${p.knows_lira}"></button>
              </div>
              <div class="core-persona-name-edit">
                <input type="text" class="core-module-change-input" data-persona-name-input="${esc(p.id)}"
                       value="${esc(p.name || '')}" placeholder="Nombre">
                <button class="core-module-build-btn" data-persona-name-save="${esc(p.id)}">Guardar nombre</button>
              </div>
            </div>` : ''}
          </div>
          <div class="estudio-console-rail">
            <div class="estudio-console-rail-section">
              <div class="estudio-console-section-label neutral"><span class="dot"></span>Última vez</div>
              <div class="estudio-console-rail-body">${esc(p.last_seen || '—')}</div>
            </div>
            ${!isJoan ? `
            <div class="estudio-console-rail-section">
              <button class="core-module-block-btn is-blocked" data-persona-delete="${esc(p.id)}" data-persona-name="${esc(p.name || p.id)}">
                Olvidar persona
              </button>
            </div>` : ''}
          </div>
        </div>
      </div>
    `
  }

  return `
    <div class="core-module-row${expanded ? ' expanded' : ''}" data-persona-row="${esc(p.id)}">
      <span class="core-module-emoji">${isJoan ? '👑' : '👤'}</span>
      <span class="core-module-name">${esc(p.name || p.id)}</span>
      <span class="core-persona-badge ${badgeClass}">${badgeLabel}</span>
      ${p.knows_lira ? '<span class="core-persona-knows-tag" title="Conoce a LIRA">💬</span>' : ''}
    </div>
    ${detail}
  `
}

function _renderPersonaAddForm() {
  return `
    <div class="core-persona-add-form">
      <input type="text" class="core-module-change-input" id="corePersonaAddName" placeholder="Nombre">
      <select class="core-persona-select" id="corePersonaAddRelationship">
        <option value="friend">Amigo</option>
        <option value="family">Familia</option>
        <option value="colleague">Colega</option>
        <option value="stranger">Desconocido</option>
      </select>
      <div class="core-persona-tier-buttons">
        ${PERSONA_TIERS.map((t, i) => `
          <button type="button" class="core-persona-tier-btn${i === 3 ? ' active' : ''}" data-add-tier="${t.value}">${t.label}</button>
        `).join('')}
      </div>
      <div class="toggle-row core-persona-knows-row">
        <span class="toggle-label">Conoce a LIRA</span>
        <button type="button" class="toggle-switch" id="corePersonaAddKnows"></button>
      </div>
      <div class="core-persona-add-actions">
        <button class="core-module-build-btn" id="corePersonaAddSave">Guardar</button>
        <button class="core-module-build-btn" id="corePersonaAddCancel">Cancelar</button>
      </div>
    </div>
  `
}

// Selected-but-not-yet-saved add-form state, module-level so it survives
// the whole-body re-render a tier-button click triggers (re-rendering
// only the add form, not the full list, would be nicer, but this form is
// shown alone/rarely enough that a full _renderCorePersonas() call per
// click — same "re-render everything, simplicity over diffing" approach
// Módulos already uses — is cheap enough not to bother avoiding).
let _corePersonaAddTier  = 0.15
let _corePersonaAddKnows = false

function _wirePersonaAddForm(body) {
  if (!_corePersonaAdding) return

  body.querySelectorAll('[data-add-tier]').forEach(btn => {
    btn.addEventListener('click', () => {
      _corePersonaAddTier = parseFloat(btn.dataset.addTier)
      body.querySelectorAll('[data-add-tier]').forEach(b => b.classList.toggle('active', b === btn))
    })
  })
  const knowsBtn = document.getElementById('corePersonaAddKnows')
  if (knowsBtn) {
    knowsBtn.classList.toggle('on', _corePersonaAddKnows)
    knowsBtn.addEventListener('click', () => {
      _corePersonaAddKnows = !_corePersonaAddKnows
      knowsBtn.classList.toggle('on', _corePersonaAddKnows)
    })
  }
  const cancelBtn = document.getElementById('corePersonaAddCancel')
  if (cancelBtn) {
    cancelBtn.addEventListener('click', () => {
      _corePersonaAdding = false
      _corePersonaAddTier = 0.15
      _corePersonaAddKnows = false
      _renderCorePersonas()
    })
  }
  const saveBtn = document.getElementById('corePersonaAddSave')
  if (saveBtn) {
    saveBtn.addEventListener('click', async () => {
      const nameInput = document.getElementById('corePersonaAddName')
      const name = (nameInput && nameInput.value || '').trim()
      if (!name) { nameInput && nameInput.focus(); return }
      const relSelect = document.getElementById('corePersonaAddRelationship')
      try {
        await fetch(`${JARVIS_API}/api/social/people`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name,
            relationship_to_joan: relSelect ? relSelect.value : 'stranger',
            trust_level: _corePersonaAddTier,
            knows_lira: _corePersonaAddKnows,
          }),
        })
      } catch { /* re-render below reflects whatever the backend actually ended up with */ }
      _corePersonaAdding = false
      _corePersonaAddTier = 0.15
      _corePersonaAddKnows = false
      _renderCorePersonas()
    })
  }
}

async function _setPersonaTrust(personId, trustLevel) {
  try {
    await fetch(`${JARVIS_API}/api/social/people/${encodeURIComponent(personId)}/trust`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ trust_level: trustLevel }),
    })
  } catch { /* re-render below reflects whatever the backend actually ended up with */ }
  _renderCorePersonas()
}

async function _editPersona(personId, updates) {
  try {
    await fetch(`${JARVIS_API}/api/social/people/${encodeURIComponent(personId)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(updates),
    })
  } catch { /* re-render below reflects whatever the backend actually ended up with */ }
  _renderCorePersonas()
}

async function _deletePersona(personId) {
  try {
    await fetch(`${JARVIS_API}/api/social/people/${encodeURIComponent(personId)}`, { method: 'DELETE' })
  } catch { /* re-render below reflects whatever the backend actually ended up with */ }
  _corePersonaExpandedRows.delete(personId)
  _renderCorePersonas()
}
