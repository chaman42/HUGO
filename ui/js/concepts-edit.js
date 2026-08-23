// concepts-edit.js — Concept modal editing: unsaved-changes guard, field typing, delete confirm, rendering, and save.
function _closeConceptModal() {
  conceptModalOverlay.classList.remove('open')
  _cptSavedSnapshot = null
  _emitHudContext({ type: 'idle', section: 'concepts' })
}

// ── Unsaved concept-edit warning ─────────────────────────────────────────
// "Tienes cambios sin guardar en este concepto. ¿Quieres guardar antes de
// salir?" — shown by switchSection() (see its own comment, much earlier in
// this file) when the user tries to navigate to another section while this
// modal has unsaved changes. HUD-styled dialog (#unsavedConceptModal),
// never a browser confirm()/alert().
let _cptSavedSnapshot  = null   // {name, desc, status} as of open (or last save)
let _pendingSectionNav = null   // section switchSection() was trying to reach when intercepted

function _captureConceptSnapshot() {
  _cptSavedSnapshot = {
    name:   document.getElementById('cptName').value,
    desc:   document.getElementById('cptDesc').value,
    status: document.getElementById('cptStatus').value,
  }
}

function _conceptFormHasUnsavedChanges() {
  if (!conceptModalOverlay.classList.contains('open') || !_cptSavedSnapshot) return false
  return document.getElementById('cptName').value   !== _cptSavedSnapshot.name
      || document.getElementById('cptDesc').value   !== _cptSavedSnapshot.desc
      || document.getElementById('cptStatus').value !== _cptSavedSnapshot.status
}

function _showUnsavedConceptDialog(targetSection) {
  _pendingSectionNav = targetSection
  document.getElementById('unsavedConceptModal').classList.add('open')
}
function _hideUnsavedConceptDialog() {
  _pendingSectionNav = null
  document.getElementById('unsavedConceptModal').classList.remove('open')
}

document.getElementById('unsavedConceptSaveBtn').addEventListener('click', () => {
  const target = _pendingSectionNav
  const saved  = _saveConceptForm()   // false (e.g. empty name) ⇒ stays put, dialog + modal both remain open to fix it
  _hideUnsavedConceptDialog()
  if (saved && target) _performSwitchSection(target)
})
document.getElementById('unsavedConceptDiscardBtn').addEventListener('click', () => {
  const target = _pendingSectionNav
  _cancelEdit()
  _closeConceptModal()
  _hideUnsavedConceptDialog()
  if (target) _performSwitchSection(target)
})
document.getElementById('unsavedConceptCancelBtn').addEventListener('click', () => {
  _hideUnsavedConceptDialog()   // stays on the current section — concept modal remains open, changes intact
})
// Backdrop click behaves like Cancelar — stay put, same convention as
// every other confirm dialog in this file (delete-confirm, update-confirm).
document.getElementById('unsavedConceptModal').addEventListener('click', e => {
  if (e.target === document.getElementById('unsavedConceptModal')) _hideUnsavedConceptDialog()
})

// Debounced 'typing' activity — see USER ACTIVITY section above. Fires
// ~700ms after the user pauses, not on every keystroke, and folds
// name+description into first-50-chars-per-field snapshots the same way
// the task's own event shape describes.
let _cptTypingDebounce = null
function _reportConceptTyping(field) {
  clearTimeout(_cptTypingDebounce)
  _cptTypingDebounce = setTimeout(() => {
    const el = document.getElementById(field === 'nombre' ? 'cptName' : 'cptDesc')
    if (!el) return
    _markUiInteraction()
    _emitUserActivity('concepts', 'typing', { field, partial_text: el.value.slice(0, 50) })
  }, 700)
}
document.getElementById('cptName').addEventListener('input', () => _reportConceptTyping('nombre'))
document.getElementById('cptDesc').addEventListener('input', () => _reportConceptTyping('descripcion'))

function _cancelEdit() {
  _editIdx = -1
  document.getElementById('cptName').value = ''
  document.getElementById('cptDesc').value = ''
  document.getElementById('cptStatus').value = 'idea'
}

function _beginEdit(idx) {
  const c = _loadConcepts()[idx]
  if (!c) return
  _editIdx = idx
  document.getElementById('cptName').value = c.name
  document.getElementById('cptDesc').value = c.desc || ''
  document.getElementById('cptStatus').value = c.status || 'idea'
  cptModalTitle.textContent = 'Editar Concepto'
  _emitHudContext({ type: 'concept_detail', concept: { name: c.name, desc: c.desc || '', status: c.status || 'idea', type: c.type } })
  _openConceptModal()
}

// "+ Nuevo Concepto" trigger — always starts from a clean create-mode form
document.getElementById('cptNewBtn').addEventListener('click', () => {
  _cancelEdit()
  cptModalTitle.textContent = 'Nuevo Concepto'
  _openConceptModal()
})
// ✕ in the modal header — same as Cancelar
document.getElementById('cptModalClose').addEventListener('click', () => {
  _cancelEdit()
  _closeConceptModal()
})
// Backdrop click closes without saving (same convention as the delete-confirm modal)
conceptModalOverlay.addEventListener('click', e => {
  if (e.target === conceptModalOverlay) { _cancelEdit(); _closeConceptModal() }
})
// Escape closes without saving, only while the modal is actually open
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && conceptModalOverlay.classList.contains('open')) {
    _cancelEdit()
    _closeConceptModal()
  }
})

function _showDeleteConfirm(idx) {
  _pendingDeleteIdx = idx
  document.getElementById('conceptDeleteConfirm').classList.add('open')
}

function _hideDeleteConfirm() {
  _pendingDeleteIdx = -1
  document.getElementById('conceptDeleteConfirm').classList.remove('open')
}

// Which Conceptuales subsection is showing — 'armor' (default) or
// 'general'. Toggled by #conceptTypeToggle's buttons below; new concepts
// created while a subsection is active are tagged with it (see cptSave's
// click handler).
let _currentConceptType = 'armor'

document.querySelectorAll('.concept-type-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    _currentConceptType = btn.dataset.type
    document.querySelectorAll('.concept-type-btn').forEach(b => b.classList.toggle('active', b === btn))
    _renderConcepts()
  })
})

function _renderConcepts() {
  const list     = document.getElementById('conceptList')
  const concepts = _loadConcepts()
  list.innerHTML = ''

  // Keep each entry's ORIGINAL index into _conceptsCache (not its position
  // in this filtered subset) — _beginEdit/_showDeleteConfirm both index
  // into the full unfiltered cache, so losing that mapping here would edit
  // or delete the wrong concept.
  const visible = concepts
    .map((c, idx) => ({ c, idx }))
    .filter(({ c }) => c.type === _currentConceptType)

  if (!visible.length) {
    list.innerHTML = `<div class="concept-empty">No hay conceptos guardados aún${_currentConceptType === 'general' ? ' en Conceptos Generales' : ''}.</div>`
    return
  }

  visible.forEach(({ c, idx }) => {
    const card = document.createElement('div')
    card.className = 'concept-card'
    const badgeMap = { 'idea': 'badge-no-construido', 'en desarrollo': 'badge-construccion', 'descartado': 'badge-no-completado' }
    const bCls = badgeMap[c.status] || 'badge-no-construido'
    const hasDesign = c.type === 'armor' && !!c.design_id
    // Design integration (Design Studio ↔ Conceptuales) only applies to
    // 'armor' concepts — Conceptos Generales never gets a design button/
    // badge/INSPECCIONAR at all, same as the old disabled placeholder was
    // scoped implicitly by always sitting under the Armaduras subtab.
    const designBadge = c.type === 'armor'
      ? (hasDesign
          ? `<span class="concept-design-badge with-design">◆ CON DISEÑO</span>`
          : `<span class="concept-design-badge no-design">SIN DISEÑO</span>`)
      : ''
    const designBtn = c.type === 'armor'
      ? `<button class="concept-card-design" data-idx="${idx}" title="${hasDesign ? 'Editar diseño' : 'Crear diseño'}">${hasDesign ? 'EDITAR DISEÑO' : 'CREAR DISEÑO'}</button>`
      : ''
    const inspectBtn = c.type === 'armor'
      ? `<button class="concept-card-inspect" data-idx="${idx}" title="Inspeccionar">🔍</button>`
      : ''
    card.innerHTML = `
      <div class="concept-card-header">
        <span class="concept-card-name">${esc(c.name)}</span>
        <span class="armor-badge ${bCls}">${esc(c.status)}</span>
        ${designBadge}
        ${inspectBtn}
        <button class="concept-card-edit" data-idx="${idx}" title="Editar">✎</button>
        <button class="concept-card-del" data-idx="${idx}" title="Eliminar">✕</button>
      </div>
      <div class="concept-card-body">${esc(c.desc)}</div>
      ${designBtn ? `<div class="concept-card-footer">${designBtn}</div>` : ''}`
    // Expand/collapse on click (not when clicking any of the action
    // buttons). Expanding counts as "has a concept card open" for
    // hud_context — see PANTALLA ACTUAL in core/commands.py.
    card.addEventListener('click', e => {
      if (e.target.closest('.concept-card-del, .concept-card-edit, .concept-card-design, .concept-card-inspect')) return
      card.classList.toggle('expanded')
      if (card.classList.contains('expanded')) {
        _emitHudContext({ type: 'concept_detail', concept: { name: c.name, desc: c.desc || '', status: c.status || 'idea', type: c.type } })
      } else {
        _emitHudContext({ type: 'idle', section: 'concepts' })
      }
    })
    // Edit button — pre-fill form and switch to edit mode
    card.querySelector('.concept-card-edit').addEventListener('click', e => {
      e.stopPropagation()
      _beginEdit(idx)
    })
    // Delete button — always show confirmation first; never delete on first click
    card.querySelector('.concept-card-del').addEventListener('click', e => {
      e.stopPropagation()
      _showDeleteConfirm(idx)
    })
    // CREAR DISEÑO / EDITAR DISEÑO — jumps to Diseño → Armaduras and opens
    // the Design Studio workspace linked to this concept.
    const designButtonEl = card.querySelector('.concept-card-design')
    if (designButtonEl) {
      designButtonEl.addEventListener('click', e => {
        e.stopPropagation()
        _openDesignForConcept(c)
      })
    }
    // INSPECCIONAR — full two-column detail view (concept data + diagram)
    const inspectButtonEl = card.querySelector('.concept-card-inspect')
    if (inspectButtonEl) {
      inspectButtonEl.addEventListener('click', e => {
        e.stopPropagation()
        _openConceptDesignDetail(c)
      })
    }
    list.appendChild(card)
  })
}

// ── Design Studio ↔ Conceptuales bridge ─────────────────────────────────
// Both branches jump into Armor Bay → Diseño → Armaduras (the three
// globals below are section-nav.js/armor-detail-concepts-load.js's own,
// both loaded before this script) and then hand off to design-studio.js
// (loaded after this script, but these are only ever called from a click
// handler — by then it's fully loaded).
function _openDesignForConcept(c) {
  _closeConceptDesignDetail()
  switchSection('armor')
  _switchSubTab('diseno')
  _switchDesignSub('armaduras')
  if (c.design_id) {
    _dsOpenDesignById(c.design_id, c.ts)
  } else {
    _dsStartNewDesignForConcept(c.name, c.ts)
  }
}

// ── INSPECCIONAR — full two-column concept detail view ──────────────────
// Left: concept data (name/status/description). Right: the linked design's
// diagram (read-only, _dsRenderStaticDiagram from design-studio.js) or an
// empty state. Same full-page-overlay-within-#armorSection pattern as
// #armorDetailView (armor-detail-concepts-load.js) — an opaque absolutely-
// positioned layer that fades in/out over whatever's underneath, built
// once here since Conceptuales had no such view before this feature.
function _ensureConceptDesignDetail() {
  let el = document.getElementById('conceptDesignDetail')
  if (el) return el

  // Same estudio-console-* hero/body-grid shell as Armor Bay's own detail
  // sheet (ui/js/armor-detail-concepts-load.js) — the static diagram sits
  // in the hero next to the name/badge (same slot Armor Bay's silhouette
  // occupies), description as prose below, CREAR/EDITAR DISEÑO as its own
  // block. Element IDs unchanged so _openConceptDesignDetail's DOM refs
  // keep working as-is.
  el = document.createElement('div')
  el.id = 'conceptDesignDetail'
  el.className = 'concept-design-detail'
  el.innerHTML = `
    <button class="detail-back-btn" id="conceptDesignDetailBack">← Volver</button>
    <div class="armor-detail-page">
      <div class="estudio-console concept-design-console">
        <div class="estudio-console-hero">
          <div class="estudio-console-hero-main">
            <div class="estudio-console-hero-eyebrow" id="cddBadge"></div>
            <div class="estudio-console-hero-title" id="cddName" style="font-size:1.1rem;"></div>
          </div>
          <div id="cddDiagramBox"></div>
        </div>
        <div class="estudio-console-body-grid single-col">
          <div class="estudio-console-col-main">
            <div class="estudio-console-block">
              <div class="estudio-console-section-label neutral"><span class="dot"></span>Descripción</div>
              <div class="estudio-console-prose" id="cddDesc"></div>
            </div>
            <button class="ds-new-btn concept-design-detail-btn" id="cddDesignBtn"></button>
          </div>
        </div>
      </div>
    </div>
  `
  document.getElementById('armorSection').appendChild(el)
  el.querySelector('#conceptDesignDetailBack').addEventListener('click', _closeConceptDesignDetail)
  return el
}

async function _openConceptDesignDetail(c) {
  const el = _ensureConceptDesignDetail()
  const badgeMap = { 'idea': 'badge-no-construido', 'en desarrollo': 'badge-construccion', 'descartado': 'badge-no-completado' }
  el.querySelector('#cddName').textContent = c.name
  el.querySelector('#cddBadge').innerHTML = `<span class="armor-badge ${badgeMap[c.status] || 'badge-no-construido'}">${esc(c.status)}</span>`
  el.querySelector('#cddDesc').textContent = c.desc || ''

  const diagramBox = el.querySelector('#cddDiagramBox')
  const designBtn = el.querySelector('#cddDesignBtn')
  diagramBox.innerHTML = '<div class="concept-design-detail-empty">Cargando diseño…</div>'

  const design = c.design_id ? await _dsFetchDesignById(c.design_id) : null
  if (design) {
    diagramBox.innerHTML = _dsRenderStaticDiagram(design)
    designBtn.textContent = 'EDITAR DISEÑO'
  } else {
    diagramBox.innerHTML = '<div class="concept-design-detail-empty">Sin diseño — pulsa CREAR DISEÑO para empezar</div>'
    designBtn.textContent = 'CREAR DISEÑO'
  }
  designBtn.onclick = () => _openDesignForConcept(c)

  el.classList.add('open')
  _emitHudContext({ type: 'concept_detail', concept: { name: c.name, desc: c.desc || '', status: c.status || 'idea', type: c.type } })
}

function _closeConceptDesignDetail() {
  const el = document.getElementById('conceptDesignDetail')
  if (el) el.classList.remove('open')
}

// Delete confirmation dialog buttons
document.getElementById('cptDelConfirm').addEventListener('click', () => {
  if (_pendingDeleteIdx < 0) { _hideDeleteConfirm(); return }
  const all = _loadConcepts()
  all.splice(_pendingDeleteIdx, 1)
  _saveConcepts(all)
  // Keep _editIdx consistent after removal
  if (_editIdx === _pendingDeleteIdx) {
    _cancelEdit()
  } else if (_pendingDeleteIdx < _editIdx) {
    _editIdx--
  }
  _hideDeleteConfirm()
  _renderConcepts()
})
document.getElementById('cptDelCancel').addEventListener('click', _hideDeleteConfirm)
// Clicking the backdrop also dismisses the dialog
document.getElementById('conceptDeleteConfirm').addEventListener('click', e => {
  if (e.target === document.getElementById('conceptDeleteConfirm')) _hideDeleteConfirm()
})

// Save / update — handles both create and edit mode, then closes the
// modal. Edit mode overwrites all[_editIdx] in place — it can only ever
// update the concept _beginEdit() opened the modal for, never create a
// duplicate. Extracted as its own function (not just an inline click
// handler) so the unsaved-changes dialog's own "Guardar" button can reuse
// this exact logic — see _showUnsavedConceptDialog above. Returns true on
// an actual save, false if validation blocked it (empty name), so callers
// that navigate away afterward know whether it's actually safe to.
function _saveConceptForm() {
  const name   = document.getElementById('cptName').value.trim()
  const desc   = document.getElementById('cptDesc').value.trim()
  const status = document.getElementById('cptStatus').value
  if (!name) { document.getElementById('cptName').focus(); return false }

  const all = _loadConcepts()
  if (_editIdx >= 0 && _editIdx < all.length) {
    // Edit mode: overwrite in place, preserve original timestamp AND type
    // (the ...spread keeps it — editing a concept never moves it between
    // subsections; that's a create-only decision, tagged below).
    all[_editIdx] = { ...all[_editIdx], name, desc, status }
  } else {
    // Create mode: prepend new concept, tagged with whichever subsection
    // (Armaduras/Conceptos Generales) is active right now.
    all.unshift({ name, desc, status, type: _currentConceptType, ts: Date.now() })
  }
  _saveConcepts(all)
  _cancelEdit()
  _closeConceptModal()
  _renderConcepts()
  return true
}
document.getElementById('cptSave').addEventListener('click', () => { _saveConceptForm() })

// Cancel — resets form to create-new mode without saving, and closes the modal
document.getElementById('cptCancel').addEventListener('click', () => {
  _cancelEdit()
  _closeConceptModal()
})

// ════════════════════════════════════════════════════════════════════════════
// AUTH GATE  — this IIFE is the only entry point into the app.
// Nothing else initialises until the device fingerprint is verified.
// ════════════════════════════════════════════════════════════════════════════
// AUTH GATE TEMPORARILY DISABLED — all devices allowed without fingerprint check.
/*
;(async () => {
  // Step 1: generate a stable device fingerprint from browser characteristics.
  try {
    _deviceFingerprint = await _generateFingerprint()
  } catch (e) {
    // crypto.subtle unavailable (non-secure context?) — use a fallback string
    // so bootstrap mode still works; the server will see an empty fingerprint
    // and allow access only if no devices are registered.
    console.warn('[Auth] Fingerprint generation failed:', e)
    _deviceFingerprint = ''
  }
  // [AUTH] Always log the fingerprint to the console so the user can copy it
  // to /api/register_device when setting up a new device.
  console.log('[Auth] Device fingerprint:', _deviceFingerprint)

  // Step 2: ask the launcher if this device is allowed.
  let allowed   = false
  let bootstrap = false
  try {
    const res  = await fetch(`${LAUNCHER_API}/api/auth?fingerprint=${encodeURIComponent(_deviceFingerprint)}`)
    if (res.ok) {
      const data = await res.json()
      allowed   = data.allowed   === true
      bootstrap = data.bootstrap === true
    } else {
      // Auth endpoint returned an error status — fail-open so a misconfigured
      // server does not permanently lock out the owner.
      console.warn('[Auth] /api/auth returned', res.status, '— failing open')
      allowed = true
    }
  } catch (e) {
    // Launcher unreachable — fail-open (local tool, network errors are benign).
    console.warn('[Auth] /api/auth unreachable — failing open:', e)
    allowed = true
  }

  // Step 3: block unregistered devices before any socket is created.
  if (!allowed) {
    _showRejectionPage()
    return   // all further code in this script is abandoned
  }

  // Step 4: first-time setup notice — no devices registered yet.
  if (bootstrap) {
    console.warn(
      '[Auth] BOOTSTRAP MODE — no devices registered.\n' +
      'Register this device by visiting:\n' +
      `  ${location.origin}/api/register_device?fingerprint=${_deviceFingerprint}&token=YOUR_REGISTER_TOKEN`
    )
  }

  // Step 5: auth passed — create the launcher socket and start the app.
  _initLauncherSocket()
})()
*/
// AUTH DISABLED: skip fingerprint check and start the app directly.
_initLauncherSocket()
