// mind-map.js — Mind-map keyword matching, graph building (D3), and node detail panel.
const MAPA_STOPWORDS_ES = new Set([
  'el','la','los','las','un','una','unos','unas','de','del','al','y','o','u',
  'que','en','a','por','para','con','sin','es','son','se','su','sus','lo',
  'le','les','mi','mis','tu','tus','yo','tú','él','ella','nosotros',
  'vosotros','ellos','ellas','me','te','nos','os','más','pero','como',
  'cuando','donde','qué','quién','cómo','cuál','cuáles','cuánto','cuánta',
  'cuántos','muy','ya','este','esta','esto','estos','estas','ese','esa',
  'eso','esos','esas','también','hay','no','sí','si','soy','eres','era',
  'fue','ser','estar','está','están','he','has','ha','han',
])
function _mapaKeywords(text) {
  const words = String(text || '').toLowerCase().match(/[\p{L}\p{N}_]+/gu) || []
  return new Set(words.filter(w => w.length > 2 && !MAPA_STOPWORDS_ES.has(w)))
}
function _mapaShareKeyword(a, b) {
  for (const w of a) if (b.has(w)) return true
  return false
}

const CORE_MAP_NODE_RADIUS = { fact: 4, episode: 9, armor: 7, concept: 7 }

function _mapaDiamondPath(r) { return `M0,${-r} L${r},0 L0,${r} L${-r},0 Z` }
function _mapaHexPath(r) {
  const pts = []
  for (let i = 0; i < 6; i++) {
    const a = (Math.PI / 3) * i - Math.PI / 2
    pts.push([r * Math.cos(a), r * Math.sin(a)])
  }
  return 'M' + pts.map(p => p.join(',')).join('L') + 'Z'
}

function _mapaBuildGraph(facts, episodes, concepts, connections, armorData) {
  const nodes = []
  const links = []

  const categories = Object.keys(facts)
  categories.forEach(cat => {
    (facts[cat] || []).forEach((f, i) => {
      nodes.push({ id: `fact-${cat}-${i}`, type: 'fact', category: cat, label: f.fact, data: f })
    })
  })
  episodes.forEach((e, i) => {
    nodes.push({ id: `ep-${i}`, type: 'episode', label: e.topic || e.summary || 'Episodio', data: e })
  })
  concepts.forEach((c, i) => {
    nodes.push({ id: `concept-${i}`, type: 'concept', label: c.name || 'Concepto', data: c })
  })
  const armorModels = [...(armorData.primarios || []), ...(armorData.paralelos || [])]
  armorModels.forEach(m => {
    nodes.push({ id: `armor-${m.id}`, type: 'armor', label: m.name, data: m })
  })

  const factNodes    = nodes.filter(n => n.type === 'fact')
  const episodeNodes = nodes.filter(n => n.type === 'episode')
  const conceptNodes = nodes.filter(n => n.type === 'concept')
  const armorNodes   = nodes.filter(n => n.type === 'armor')

  // Facts connected to the episodes they came from — matched against each
  // episode's key_facts (short phrases summarizing what was learned) and
  // its topic.
  episodeNodes.forEach(epNode => {
    const keyFactKw = (epNode.data.key_facts || []).map(_mapaKeywords)
    const topicKw   = _mapaKeywords(epNode.data.topic || '')
    factNodes.forEach(factNode => {
      const factKw = _mapaKeywords(factNode.label)
      const related = keyFactKw.some(kw => _mapaShareKeyword(factKw, kw)) || _mapaShareKeyword(factKw, topicKw)
      if (related) links.push({ source: factNode.id, target: epNode.id, kind: 'fact-episode' })
    })
  })

  // Concepts connected to the armor models they relate to.
  conceptNodes.forEach(cNode => {
    const cKw = _mapaKeywords(`${cNode.data.name || ''} ${cNode.data.desc || ''}`)
    armorNodes.forEach(aNode => {
      const aKw = _mapaKeywords(`${aNode.data.name || ''} ${aNode.data.nickname || ''} ${aNode.data.descripcion || ''}`)
      if (_mapaShareKeyword(cKw, aKw)) links.push({ source: cNode.id, target: aNode.id, kind: 'concept-armor' })
    })
  })

  // Episodes connected to concepts they touched on.
  episodeNodes.forEach(epNode => {
    const epKw = _mapaKeywords(`${epNode.data.topic || ''} ${(epNode.data.key_facts || []).join(' ')}`)
    conceptNodes.forEach(cNode => {
      const cKw = _mapaKeywords(`${cNode.data.name || ''} ${cNode.data.desc || ''}`)
      if (_mapaShareKeyword(epKw, cKw)) links.push({ source: epNode.id, target: cNode.id, kind: 'episode-concept' })
    })
  })

  // Armor evolution chain — each model links to the next within its own
  // category list, grounded in the data's own ordering (and its
  // "evolucion" narrative field), not a guessed relation.
  ;['primarios', 'paralelos'].forEach(cat => {
    const arr = armorData[cat] || []
    for (let i = 0; i < arr.length - 1; i++) {
      links.push({ source: `armor-${arr[i].id}`, target: `armor-${arr[i + 1].id}`, kind: 'armor-chain' })
    }
  })

  // Reflective-mode connections (data/mind_map_connections.json, see
  // core.reflective) — identified by fact TEXT rather than the fact-${cat}-${i}
  // ids above, since those ids are just this function's own array-index
  // scheme and shift whenever a fact file reorders (dedup sorts by 'added').
  // Text is the only identifier both sides of the backend/frontend boundary
  // can agree on without coupling core.reflective to this function's
  // internals. Silently skipped if either endpoint's fact no longer exists
  // (e.g. it went outdated and dropped off) — a stale connection just never
  // renders, same "let it fade out" spirit as everything else in Mapa.
  ;(connections || []).forEach((c, i) => {
    const fromNode = factNodes.find(n => n.label === c.from)
    const toNode   = factNodes.find(n => n.label === c.to)
    if (!fromNode || !toNode) return
    links.push({
      source: fromNode.id, target: toNode.id, kind: 'reflective',
      strength: typeof c.strength === 'number' ? c.strength : 0.5,
      relationship: c.relationship || '', id: `reflective-${i}`,
    })
  })

  return { nodes, links }
}

let _coreMapaSimulation = null

// Reuses ui/css/estudio.css's estudio-console-* building blocks directly
// (chip/meta-readout/hero-title/prose/item-card — all globally available,
// same instrument-console language as ESTUDIO/Armor Bay/NÚCLEO's other
// tabs) rather than this panel's own now-retired .core-map-detail-kind/
// -title/-row classes. hero-title's font-size is knocked down inline —
// this slide-in panel is only ~260px wide, too narrow for its default
// 1.25rem hero sizing.
function _mapaNodeDetailHTML(node) {
  const d = node.data
  if (node.type === 'fact') {
    return `
      <div class="estudio-console-hero-eyebrow">
        <span class="estudio-console-chip">Hecho de memoria</span>
        <span class="estudio-console-meta-readout">${esc(CORE_CATEGORY_LABELS[node.category] || node.category)}</span>
      </div>
      <div class="estudio-console-hero-title" style="font-size:0.95rem;">${esc(d.fact)}</div>`
  }
  if (node.type === 'episode') {
    const facts = (d.key_facts || []).map(k => `<li>${esc(k)}</li>`).join('')
    return `
      <div class="estudio-console-hero-eyebrow">
        <span class="estudio-console-chip">Episodio</span>
        <span class="estudio-console-meta-readout">${esc(d.date || '')}</span>
      </div>
      <div class="estudio-console-hero-title" style="font-size:0.95rem;">${esc(d.topic || d.summary || '')}</div>
      <div class="estudio-console-prose">${esc(d.summary || '')}</div>
      ${facts ? `<ul class="estudio-console-list">${facts}</ul>` : ''}
      <div class="estudio-console-item-card">
        <div class="estudio-console-item-head">
          <span class="estudio-console-item-type">Importancia</span>
          <span class="estudio-console-item-title">${esc(String(d.importance ?? '—'))}</span>
        </div>
      </div>`
  }
  if (node.type === 'concept') {
    return `
      <div class="estudio-console-hero-eyebrow">
        <span class="estudio-console-chip">Concepto</span>
        <span class="estudio-console-meta-readout">${esc(d.type === 'general' ? 'General' : 'Armadura')}</span>
      </div>
      <div class="estudio-console-hero-title" style="font-size:0.95rem;">${esc(d.name || '')}</div>
      <div class="estudio-console-prose">${esc(d.desc || '')}</div>
      <div class="estudio-console-item-card">
        <div class="estudio-console-item-head">
          <span class="estudio-console-item-type">Estado</span>
          <span class="estudio-console-item-title">${esc(d.status || '—')}</span>
        </div>
      </div>`
  }
  // armor
  return `
    <div class="estudio-console-hero-eyebrow">
      <span class="estudio-console-chip">Armadura</span>
      <span class="estudio-console-meta-readout">${esc(d.status || '')}</span>
    </div>
    <div class="estudio-console-hero-title" style="font-size:0.95rem;">${esc(d.name || '')}${d.nickname ? ` "${esc(d.nickname)}"` : ''}</div>
    <div class="estudio-console-prose">${esc(d.descripcion || '')}</div>
    <div class="estudio-console-item-card" style="--hc:var(--green);">
      <div class="estudio-console-item-head"><span class="estudio-console-item-type">Innovaciones</span></div>
      <div class="estudio-console-item-text" style="margin-bottom:0;">${esc(d.innovaciones || '—')}</div>
    </div>
    <div class="estudio-console-item-card" style="--hc:var(--p-color);">
      <div class="estudio-console-item-head"><span class="estudio-console-item-type">Limitaciones</span></div>
      <div class="estudio-console-item-text" style="margin-bottom:0;">${esc(d.limitaciones || '—')}</div>
    </div>
    <div class="estudio-console-item-card">
      <div class="estudio-console-item-head">
        <span class="estudio-console-item-type">Horas</span>
        <span class="estudio-console-item-title">${esc(d.hours || '—')}</span>
      </div>
    </div>`
}

function _closeMapaDetail() {
  const detail = document.getElementById('coreMapaDetail')
  detail.classList.remove('open')
  document.querySelectorAll('.core-map-node-group.core-map-node-selected')
    .forEach(g => g.classList.remove('core-map-node-selected'))
}

async function _renderCoreMapa() {
  const emptyEl = document.getElementById('coreMapaEmpty')
  const graphEl = document.getElementById('coreMapaGraph')
  const svgEl   = document.getElementById('coreMapaSvg')

  if (_coreMapaSimulation) { _coreMapaSimulation.stop(); _coreMapaSimulation = null }
  _closeMapaDetail()

  let data = {}
  let connections = []
  try {
    const res = await fetch(`${JARVIS_API}/api/memory_active`)
    data = await res.json()
  } catch { data = {} }
  try {
    const res = await fetch(`${JARVIS_API}/api/mind_map_connections`)
    connections = await res.json()
  } catch { connections = [] }
  const armorData = await _fetchArmorModels()

  const facts    = data.facts    || {}
  const episodes = data.episodes || []
  const concepts = data.concepts || []

  // "Sin datos suficientes aún" specifically means LIRA hasn't had enough
  // conversations yet — facts and episodes are conversation-derived, while
  // armor/concepts are authored data that exists regardless, so only the
  // former two decide the empty state.
  const hasConversationData = Object.values(facts).some(arr => arr.length > 0) || episodes.length > 0
  if (!hasConversationData) {
    emptyEl.classList.remove('core-map-hidden')
    graphEl.classList.remove('active')
    return
  }
  emptyEl.classList.add('core-map-hidden')
  graphEl.classList.add('active')

  const { nodes, links } = _mapaBuildGraph(facts, episodes, concepts, connections, armorData)

  const width  = svgEl.clientWidth  || 320
  const height = svgEl.clientHeight || 400

  const svg = d3.select(svgEl)
  svg.selectAll('*').remove()
  svg.attr('viewBox', `0 0 ${width} ${height}`)

  const zoomLayer = svg.append('g')
  svg.call(d3.zoom().scaleExtent([0.4, 2.5]).on('zoom', ev => zoomLayer.attr('transform', ev.transform)))

  // Category clustering — facts of the same category are pulled toward
  // their own anchor point (spread evenly around the canvas), so "grouped
  // by category" is a spatial/physics property rather than an extra node
  // type or a fully-connected edge tangle.
  const categories = Object.keys(facts).filter(c => (facts[c] || []).length > 0)
  const categoryAnchor = {}
  categories.forEach((cat, i) => {
    const angle = (2 * Math.PI * i) / Math.max(1, categories.length)
    categoryAnchor[cat] = {
      x: width  / 2 + Math.cos(angle) * (Math.min(width, height) * 0.28),
      y: height / 2 + Math.sin(angle) * (Math.min(width, height) * 0.28),
    }
  })

  const linkSel = zoomLayer.append('g').selectAll('line')
    .data(links).join('line')
    .attr('class', d => d.kind === 'reflective' ? 'core-map-link core-map-link-reflective' : 'core-map-link')
    // Stronger connections = thicker/brighter edges (per spec) — only
    // reflective links currently carry a 'strength' value, so this is a
    // no-op (falls back to the CSS default) for every other link kind.
    .attr('stroke-width', d => typeof d.strength === 'number' ? 1 + d.strength * 3 : null)

  const nodeSel = zoomLayer.append('g').selectAll('g')
    .data(nodes).join('g')
    .attr('class', 'core-map-node-group')
    .attr('data-id', d => d.id)

  nodeSel.each(function (d) {
    const g = d3.select(this)
    const r = CORE_MAP_NODE_RADIUS[d.type]
    if (d.type === 'armor')        g.append('path').attr('d', _mapaDiamondPath(r)).attr('class', 'core-map-node-armor')
    else if (d.type === 'concept') g.append('path').attr('d', _mapaHexPath(r)).attr('class', 'core-map-node-concept')
    else                           g.append('circle').attr('r', r).attr('class', `core-map-node-${d.type}`)
  })

  // Labels only for the coarser, fewer node types — hundreds of tiny fact
  // circles with labels would just be unreadable clutter; clicking any
  // node still opens the full detail panel regardless.
  nodeSel.filter(d => d.type !== 'fact').append('text')
    .attr('class', 'core-map-label')
    .attr('x', d => CORE_MAP_NODE_RADIUS[d.type] + 4)
    .attr('y', 3)
    .text(d => (d.label || '').length > 22 ? d.label.slice(0, 21) + '…' : (d.label || ''))

  nodeSel.on('click', (ev, d) => {
    ev.stopPropagation()
    document.querySelectorAll('.core-map-node-group.core-map-node-selected')
      .forEach(g => g.classList.remove('core-map-node-selected'))
    d3.select(ev.currentTarget).classed('core-map-node-selected', true)
    document.getElementById('coreMapaDetailBody').innerHTML = _mapaNodeDetailHTML(d)
    document.getElementById('coreMapaDetail').classList.add('open')
  })

  const simulation = d3.forceSimulation(nodes)
    .force('link', d3.forceLink(links).id(d => d.id).distance(l => l.kind === 'armor-chain' ? 34 : 46).strength(0.35))
    .force('charge', d3.forceManyBody().strength(-70))
    .force('center', d3.forceCenter(width / 2, height / 2))
    .force('collide', d3.forceCollide().radius(d => CORE_MAP_NODE_RADIUS[d.type] + 6))
    .force('catX', d3.forceX(d => d.type === 'fact' && categoryAnchor[d.category] ? categoryAnchor[d.category].x : width  / 2).strength(d => d.type === 'fact' ? 0.12 : 0.02))
    .force('catY', d3.forceY(d => d.type === 'fact' && categoryAnchor[d.category] ? categoryAnchor[d.category].y : height / 2).strength(d => d.type === 'fact' ? 0.12 : 0.02))
    .on('tick', () => {
      linkSel
        .attr('x1', d => d.source.x).attr('y1', d => d.source.y)
        .attr('x2', d => d.target.x).attr('y2', d => d.target.y)
      nodeSel.attr('transform', d => `translate(${d.x},${d.y})`)
    })

  nodeSel.call(d3.drag()
    .on('start', (ev, d) => {
      if (!ev.active) simulation.alphaTarget(0.25).restart()
      d.fx = d.x; d.fy = d.y
    })
    .on('drag', (ev, d) => { d.fx = ev.x; d.fy = ev.y })
    .on('end', (ev, d) => {
      if (!ev.active) simulation.alphaTarget(0)
      // Left pinned at the dropped position — the whole point of dragging
      // is to let the user arrange the layout, not spring back from it.
    }))

  _coreMapaSimulation = simulation
}

document.getElementById('coreMapaDetailClose').addEventListener('click', _closeMapaDetail)
document.getElementById('coreMapaSvg').addEventListener('click', _closeMapaDetail)

// ════════════════════════════════════════════════════════════════════════════
// PERSONALITY — nuclear direct DOM theming (no CSS classes)
// ════════════════════════════════════════════════════════════════════════════

// [CHANGE 15] _PERSONALITY_QUOTES moved here (before applyPersonality) to fix a
// Temporal Dead Zone bug: applyPersonality() references this const, but it was
// previously defined ~400 lines later.  The TDZ caused a ReferenceError on the
// first applyPersonality('lira') call, which silently prevented the clock
// setInterval from ever being registered — causing the "—:—:—" frozen display.
// Guards the quote-on-personality-switch logic in applyPersonality() below
// (only re-pick on a genuine switch) and tracks LIRA's own sequential
// position for both that switch-in pick and _rotateMMQuote()'s 45s cycle —
// declared before applyPersonality() for the same TDZ reason as
// _PERSONALITY_QUOTES itself (see [CHANGE 15] above).
let _mmLastQuotedPersonality = null
let _mmLiraQuoteIdx = 0

