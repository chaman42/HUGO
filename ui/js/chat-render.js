// chat-render.js — Timestamp helper, chat message rendering, maintenance log, input enable state, and feature toggles.
// esc() moved to bootstrap-auth.js — see its own comment for why.

// Renders message text as HTML paragraphs instead of one run-on line —
// long HUGO replies (and multi-line user input) collapse to a single
// visual block under plain esc() since browsers ignore bare '\n' in HTML.
// Blank-line-separated chunks become their own <p>; a lone '\n' inside a
// chunk (no blank line) becomes a <br> rather than a new paragraph, so a
// short aside doesn't get the same wide spacing as a real paragraph break.
function _renderParagraphs(message) {
  return String(message)
    .split(/\n{2,}/)
    .map(para => esc(para).replace(/\n/g, '<br>'))
    .filter(Boolean)
    .map(para => `<p>${para}</p>`)
    .join('') || esc(message)
}

// The most recently added assistant message's .msg-timing element — see
// addMessage()'s own comment and _applyResponseTiming() below.
let _lastJarvisTimingEl = null

// Same "most recently added assistant bubble" tracking as _lastJarvisTimingEl
// above, for the 'repeat that' replay button — see _applyTtsAudioReady() below.
let _lastJarvisReplayBtn = null

// ════════════════════════════════════════════════════════════════════════════
// LOG — addMessage routes messages to the right destination:
//   user/jarvis → main chat log (clean conversation view)
//   system      → maintenance panel only (never in chat)
//   error       → main chat log as a single red line 'Error: [brief]'
//                 ONLY for critical errors that affect the conversation
//                 (mic denied, command failed). All other operational/
//                 connectivity errors go directly to addMaintMessage().
// ════════════════════════════════════════════════════════════════════════════
function addMessage(type, message) {
  // ── System → maintenance panel only ─────────────────────────────────────
  if (type === 'system') {
    addMaintMessage(message)
    return
  }

  // ── Error → single red line 'Error: [brief description]' ────────────────
  if (type === 'error') {
    // Strip any existing 'Error: ' prefix to avoid doubling, then cap at 60 chars
    const stripped = message.replace(/^error:\s*/i, '').trim()
    const brief    = stripped.length > 60 ? stripped.slice(0, 57) + '…' : stripped
    message        = 'Error: ' + brief
  }

  // Bug fix: this used to be measured AFTER the new row (+ divider) was
  // already appended below, which conflates "was the user already at the
  // bottom" with "is this new message's own height still under the
  // threshold" — any reasonably long reply pushed scrollHeight past the
  // check on its own, so auto-scroll silently stopped firing for exactly
  // the messages most likely to scroll out of view. Measuring here, before
  // any DOM mutation, captures the PRE-append scroll state instead.
  const wasAtBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 100

  // ── Unified floating diamond — track every HUGO reply unconditionally
  // (the bubble's "last response" line needs this even when the diamond
  // isn't currently eligible to show, e.g. the reply arrived while still
  // on Main/Chat), but only fade the floating text in when it actually is
  // eligible. Also proactively shows the diamond's idle state here, not
  // just from setStatus's processing/speaking transition — a defensive
  // belt-and-suspenders in case this 'log' event ever beats the
  // corresponding 'status' event across the wire.
  if (type === 'jarvis') {
    _lastJarvisReply = message
    if (_diamondEligible()) {
      hugoDiamond.classList.add('visible')
      _showDiamondText(message)
      if (currentStatus !== 'speaking') _scheduleDiamondTextHide()
    }
  }

  // ── Main menu floating text — both the user's own brief echo and HUGO's
  // reply show here while on Main. The user's echo always holds briefly;
  // HUGO's reply's hold timer is armed by setStatus once she actually
  // stops speaking (see there) — but if she isn't speaking right now
  // (TTS muted, or this arrived after status already moved on), there's no
  // such transition to catch it, so arm the same fallback timer here too.
  if (_currentSection === 'home' && (type === 'user' || type === 'jarvis')) {
    _showMMFloatingText(message, type === 'user')
    if (type === 'user' || currentStatus !== 'speaking') _scheduleMMFloatingTextHide()
  }

  // 'jarvis' here is the generic message-type key for every HUGO reply
  // (legacy name, predates personality removal — not worth a wider rename
  // since it's an internal identifier, not anything user-visible).
  const TYPES = {
    user:   { cls: 'msg-user',   label: '🎤  you' },
    jarvis: { cls: 'msg-hugo',   label: PERSONALITY_LABEL },
    error:  { cls: 'msg-error',  label: '✕  err' },
  }
  const cfg = TYPES[type] ?? TYPES.error
  const label = cfg.label

  // Insert a visual divider before every user message (new turn separator)
  if (type === 'user' && lastMsgType !== null) {
    const div = document.createElement('div')
    div.className = 'divider'
    logEl.appendChild(div)
  }
  lastMsgType = type

  const row = document.createElement('div')
  row.className = `msg ${cfg.cls}`
  row.innerHTML = `
    <div class="msg-row">
      <span class="msg-time">${ts()}</span>
      <span class="msg-label">${label}</span>
      ${type === 'jarvis' ? '<button class="msg-replay-btn" title="Repetir audio" style="display:none">\u{1F50A}</button>' : ''}
      <span class="msg-text">${_renderParagraphs(message)}</span>
    </div>
    ${type === 'jarvis' ? '<div class="msg-timing"></div>' : ''}`
  logEl.appendChild(row)

  // Response-latency display (core/commands.py + core/voice.py emit
  // 'response_timing' independently — llm_latency right after the reply
  // text is ready, tts_latency much later once audio genuinely starts,
  // see core.server.emit_response_timing's own docstring for why they're
  // never bundled). No per-message id exists to correlate against, so
  // track the single most-recently-added assistant bubble's .msg-timing
  // element here and merge each partial update onto it in
  // _applyResponseTiming() below.
  if (type === 'jarvis') {
    _lastJarvisTimingEl = row.querySelector('.msg-timing')
    _lastJarvisReplayBtn = row.querySelector('.msg-replay-btn')
  }

  // ── Response timer integration ───────────────────────────────────────────
  if (type === 'user') {
    _startResponseTimer()
  } else if (type === 'jarvis') {
    _stopResponseTimer()
  }

  // Auto-scroll to the new bottom. Your own outgoing message (typed or
  // voice-transcribed) always scrolls into view regardless of prior
  // position — you just sent it, standard chat UX never leaves that
  // hanging off-screen. HUGO's replies and error lines instead respect
  // wasAtBottom: if the user has manually scrolled up to read older
  // messages, an incoming reply doesn't yank the view down — scrolling
  // resumes on its own the moment they're back near the bottom (e.g. after
  // sending their own next message). `scroll-behavior: smooth` on
  // .log-section (see its CSS) makes this plain scrollTop assignment
  // animate — smooth but fast, not scrollTo()'s slower easing on a long
  // jump.
  if (type === 'user' || wasAtBottom) logEl.scrollTop = logEl.scrollHeight
}

// ── Response-latency display — 'response_timing' socket event (see
// core/server.py's emit_response_timing() for why llm_latency/tts_latency
// arrive as two separate, independently-timed partial updates rather than
// one bundled event). Merges onto whichever field(s) are newly known,
// stored on the element's own dataset so a later partial update doesn't
// clobber an earlier one — reads as "LLM: 1.23s" the moment the reply
// text is ready, then "LLM: 1.23s · VOZ: 14.80s" once audio actually
// starts (which can take dramatically longer, especially XTTS v2 on CPU).
// ────────────────────────────────────────────────────────────────────────
function _applyResponseTiming(data) {
  const el = _lastJarvisTimingEl
  if (!el) return
  if (typeof data.llm_latency === 'number') el.dataset.llm = data.llm_latency.toFixed(2)
  if (typeof data.tts_latency === 'number') el.dataset.tts = data.tts_latency.toFixed(2)
  const parts = []
  if (el.dataset.llm) parts.push(`LLM: ${el.dataset.llm}s`)
  if (el.dataset.tts) parts.push(`VOZ: ${el.dataset.tts}s`)
  el.textContent = parts.join(' · ')
}

// ── 'Repeat that' replay button — 'tts_audio_ready' socket event (see
// core/server.py's emit_tts_audio_ready() and core/voice.py's replay-cache
// module section). Same "no per-message id, attach to the most recently
// added assistant bubble" convention as _applyResponseTiming above — fired
// once per reply, once edge-tts has actually finished synthesizing (say
// fallback replies never fire this at all, so the button just never shows
// for those — see core.voice._speak_edge_tts_blocking's own docstring).
// ────────────────────────────────────────────────────────────────────────
function _applyTtsAudioReady(data) {
  const btn = _lastJarvisReplayBtn
  if (!btn || !data.id) return
  btn.dataset.audioId = data.id
  btn.style.display = ''
}

// Event delegation on the whole log — one listener instead of rebinding a
// per-button handler on every addMessage() call. new Audio().play() rather
// than a shared <audio> element so overlapping clicks (repeat this one,
// then that one before it finishes) just play concurrently instead of one
// cutting the other off.
logEl.addEventListener('click', (e) => {
  const btn = e.target.closest('.msg-replay-btn')
  if (!btn || !btn.dataset.audioId) return
  new Audio(`${JARVIS_API}/api/tts_audio/${btn.dataset.audioId}`).play().catch(() => {})
})

// ── Maintenance panel message writer ────────────────────────────────────────
function addMaintMessage(message) {
  const div = document.createElement('div')
  // Sistema premium pass: classify by leading prefix so entries get the
  // spec's tiered color treatment — see .maint-type-* CSS for the exact
  // colors and the reasoning behind defaulting to "system" rather than
  // "normal". Purely additive/cosmetic: no call site changes, and an
  // unmatched message always falls through to maint-type-system safely.
  let typeClass = 'maint-type-system'
  if (/^error:/i.test(message)) typeClass = 'maint-type-error'
  else if (/^(warning|advertencia):/i.test(message)) typeClass = 'maint-type-warning'
  div.className = `maint-msg ${typeClass}`
  div.innerHTML = `<span class="maint-msg-time">${ts()}</span>${esc(message)}`
  maintLog.appendChild(div)

  // Update unread badge on the Sistema nav item only when the section is not active
  if (_currentSection !== 'maintenance') {
    _sysCount++
    maintCount.textContent = _sysCount
    if (navMaintBadge) navMaintBadge.textContent = _sysCount
  }

  // Auto-scroll maintenance log if near bottom
  const atBottom = maintLog.scrollHeight - maintLog.scrollTop - maintLog.clientHeight < 40
  if (atBottom) maintLog.scrollTo({ top: maintLog.scrollHeight })
}

// ════════════════════════════════════════════════════════════════════════════
// TEXT INPUT
// ════════════════════════════════════════════════════════════════════════════
function setInputEnabled(enabled) {
  textInput.disabled = !enabled
  sendBtn.disabled   = !enabled
  textInput.placeholder = enabled ? 'Type a command…' : 'Sin conexión…'
}

// ════════════════════════════════════════════════════════════════════════════
// ATTACHMENTS — images are real now: core/vision.py (OpenRouter primary, Ollama
// moondream fallback — see that module's own header) actually describes
// them server-side, folded into the same turn's reply by core.commands
// before the personality LLM call. PDF/Documentos are still UI-only staging
// (HUGO has no document-reading endpoint yet) — picking one just folds its
// filename into the outgoing message as a plain-text note, same as every
// attachment type used to work before this change.
//
// The paperclip opens a small type menu (#attachMenu, ui/index.html) before
// the actual file picker — only "Fotos" is live (accept="image/*"); PDF/
// Documentos are disabled placeholders, setting up the menu shape for when
// HUGO can actually read those too.
// ════════════════════════════════════════════════════════════════════════════

// FileReader wrapped in a Promise — readAsDataURL's result is
// "data:image/jpeg;base64,<data>"; only the part after the comma is what
// core.vision/OpenRouter/Ollama actually want, the mime type comes from the
// File object itself rather than being re-parsed out of the prefix.
function _fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload  = () => resolve(String(reader.result).split(',', 2)[1] || '')
    reader.onerror = () => reject(reader.error)
    reader.readAsDataURL(file)
  })
}
let _stagedAttachments = []   // File[]

function _renderAttachPreview() {
  attachPreview.innerHTML = ''
  _stagedAttachments.forEach((file, i) => {
    const chip = document.createElement('div')
    chip.className = 'attach-chip'
    if (file.type.startsWith('image/')) {
      const img = document.createElement('img')
      img.className = 'attach-chip-thumb'
      img.src = URL.createObjectURL(file)
      chip.appendChild(img)
    }
    const name = document.createElement('span')
    name.className = 'attach-chip-name'
    name.textContent = file.name
    chip.appendChild(name)
    const remove = document.createElement('button')
    remove.className = 'attach-chip-remove'
    remove.type = 'button'
    remove.textContent = '×'
    remove.addEventListener('click', () => {
      _stagedAttachments.splice(i, 1)
      _renderAttachPreview()
    })
    chip.appendChild(remove)
    attachPreview.appendChild(chip)
  })
}

// Attach menu — clicking the paperclip no longer opens the file picker
// directly; it reveals a small popup (Fotos / PDF / Documentos) first.
// Only Fotos is wired to an actual accept-scoped trigger right now — PDF/
// Documentos are `disabled` in the markup (ui/index.html), so they never
// reach this handler at all; this just needs to open/close the menu and
// forward whichever ENABLED item was clicked to the hidden file input.
attachBtn?.addEventListener('click', (e) => {
  e.stopPropagation()   // don't let this same click immediately re-trigger the outside-click closer below
  attachMenu?.classList.toggle('open')
})
attachMenu?.querySelectorAll('.attach-menu-item[data-attach-accept]').forEach(btn => {
  btn.addEventListener('click', () => {
    attachMenu.classList.remove('open')
    attachFileInput.accept = btn.dataset.attachAccept
    attachFileInput.click()
  })
})
// Outside click (or Escape) closes the menu without picking anything —
// same "click elsewhere dismisses" convention the app already uses for
// the diamond's own bubble (_closeDiamondBubble's outside-click handler).
document.addEventListener('click', (e) => {
  if (attachMenu?.classList.contains('open') && !attachMenu.contains(e.target) && e.target !== attachBtn) {
    attachMenu.classList.remove('open')
  }
})
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') attachMenu?.classList.remove('open')
})

attachFileInput?.addEventListener('change', () => {
  _stagedAttachments.push(...attachFileInput.files)
  attachFileInput.value = ''   // allows picking the same file again later
  attachFileInput.accept = 'image/*,video/*,.pdf,.doc,.docx,.txt,.md,.csv'   // restore the default, broader accept for next time
  _renderAttachPreview()
})

// sourceInput defaults to the Chat section's own #textInput, but accepts
// any other input element (the unified floating diamond's #hugoDiamondInput,
// Main's #mmFloatingInput) so every "type a command" surface in the app
// shares this exact same send path — literally the same code, not a re-
// implementation, per the floating diamond / Main floating input requirements.
async function sendTextCommand(sourceInput) {
  const input = sourceInput || textInput
  let text  = input.value.trim()
  const hasAttachments = _stagedAttachments.length > 0
  if (!text && !hasAttachments) return
  // [CHANGE 14] Guard: if socket is not connected, show a clear indicator instead
  // of silently failing.  Input stays enabled so the user can retry after reconnect.
  if (!jarvisSocket || !jarvisSocket.connected) {
    const original = input.placeholder
    input.placeholder = 'Sin conexión — reconectando…'
    setTimeout(() => { input.placeholder = original }, 2500)
    return
  }
  input.value = ''
  let images = []
  if (hasAttachments) {
    const staged = _stagedAttachments
    _stagedAttachments = []
    _renderAttachPreview()

    const imageFiles = staged.filter(f => f.type.startsWith('image/'))
    const otherFiles  = staged.filter(f => !f.type.startsWith('image/'))
    // Non-image attachments (PDF/Documentos) have no read path yet — same
    // filename-note fallback this whole flow used to use for everything.
    if (otherFiles.length) {
      const names = otherFiles.map(f => f.name).join(', ')
      text = text ? `${text}\n📎 ${names}` : `📎 ${names}`
    }
    try {
      images = await Promise.all(imageFiles.map(async f => ({ data: await _fileToBase64(f), mime: f.type })))
    } catch {
      addMessage('error', 'No se pudo leer la imagen adjunta')
      images = []
    }
    if (imageFiles.length) {
      const names = imageFiles.map(f => f.name).join(', ')
      text = text ? `${text}\n📎 ${names}` : `📎 ${names}`
    }
  }
  addMessage('user', text)

  try {
    // device_id: the persistent per-device UUID from bootstrap-auth.js
    // (_deviceFingerprint) — lets HUGO tell Joan's own devices apart from
    // Dani's (or anyone else's) without relying on IP/network, which
    // wouldn't survive Joan using someone else's computer. See
    // core.social.SocialEngine._match_device.
    const payload = { text, device_id: typeof _deviceFingerprint !== 'undefined' ? _deviceFingerprint : undefined }
    if (images.length) payload.images = images
    const res = await fetch(`${JARVIS_API}/text_command`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(payload),
    })
    if (!res.ok) addMessage('error', 'Jarvis rechazó el comando')
  } catch {
    addMessage('error', 'Jarvis no responde')
  }
}

sendBtn.addEventListener('click', () => sendTextCommand())
textInput.addEventListener('keydown', e => { if (e.key === 'Enter') sendTextCommand() })

// ════════════════════════════════════════════════════════════════════════════
// AJUSTES (SETTINGS) — now a full nav section; updateSettingsInfo() is
// called from switchSection() below on navigating to it (same pattern as
// maintenance's unread-badge reset and armor's sub-tab render).
// ════════════════════════════════════════════════════════════════════════════
settingsClose.addEventListener('click', () => switchSection('home'))

// Same estudio-console-* hero/body-grid shell as ESTUDIO/Armor Bay/NÚCLEO's
// other tabs — grouped into Voz/Conexión/Build blocks instead of one flat
// list of .info-row pairs (still .info-row underneath, just organized).
// #settingsBuildRows is a stable anchor so the build-hash fetch below can
// insertAdjacentHTML into the right block instead of settingsBody itself,
// which now holds the whole console shell rather than a flat row list.
async function updateSettingsInfo() {
  try {
    const res  = await fetch(`${JARVIS_API}/api/info`)
    const info = await res.json()
    settingsBody.innerHTML = `
      <div class="estudio-console">
        <div class="estudio-console-hero">
          <div class="estudio-console-hero-main">
            <div class="estudio-console-hero-eyebrow">
              <span class="estudio-console-chip">${esc(info.display_name)}</span>
            </div>
            <div class="estudio-console-hero-title" style="font-size:1rem;">Información del sistema</div>
          </div>
        </div>
        <div class="estudio-console-body-grid single-col">
          <div class="estudio-console-col-main">
            <div class="estudio-console-block">
              <div class="estudio-console-section-label neutral"><span class="dot"></span>Voz</div>
              <div class="info-row"><span class="info-key">TTS Engine</span><span class="info-val">${esc(info.tts)}</span></div>
              <div class="info-row"><span class="info-key">STT Model</span><span class="info-val">${esc(info.vosk_model)}</span></div>
            </div>
            <div class="estudio-console-block">
              <div class="estudio-console-section-label neutral"><span class="dot"></span>Conexión</div>
              <div class="info-row"><span class="info-key">Jarvis Port</span><span class="info-val">8180</span></div>
              <div class="info-row"><span class="info-key">Launcher Port</span><span class="info-val">8179</span></div>
            </div>
            <div class="estudio-console-block">
              <div class="estudio-console-section-label neutral"><span class="dot"></span>Build</div>
              <div id="settingsBuildRows"></div>
            </div>
          </div>
        </div>
      </div>
    `
  } catch {
    settingsBody.innerHTML = '<div class="info-row"><span class="info-key">Status</span><span class="info-val" style="color:var(--red)">Jarvis offline</span></div>'
  }
  // Build hash — from launcher.py's own GET /api/version (see that
  // endpoint's docstring), the SAME process that actually serves this
  // page, so this is always ground truth for "which commit's
  // ui/index.html is genuinely being displayed right now" — independent
  // of whether jarvis.py (a separate process) is up, so it's appended
  // regardless of the /api/info outcome above. '*' suffix means there are
  // uncommitted local changes on top of that commit (git-describe-style).
  try {
    const vres      = await fetch(`${LAUNCHER_API}/api/version`)
    const version    = await vres.json()
    const frontendHash = version.repo_commit ? version.repo_commit + (version.repo_dirty ? '*' : '') : '—'
    const shellHash     = version.installed_shell_commit || '—'
    const dateStr = version.repo_commit_date
      ? new Date(version.repo_commit_date).toLocaleString('es-ES', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
      : ''
    const buildRows = document.getElementById('settingsBuildRows')
    if (buildRows) buildRows.insertAdjacentHTML('beforeend', `
      <div class="info-row"><span class="info-key">Build (frontend)</span><span class="info-val" title="${esc(dateStr)}">${esc(frontendHash)}</span></div>
      <div class="info-row"><span class="info-key">Build (app instalada)</span><span class="info-val">${esc(shellHash)}</span></div>
    `)
  } catch { /* leave build rows out — non-critical diagnostic info */ }
  if (typeof _refreshSleepStatus === 'function') _refreshSleepStatus()
  if (typeof _renderApiKeys === 'function') _renderApiKeys()
}

// Feature toggles — config-driven: each entry's `key` must match a key in
// core/commands.py's _DEFAULT_FEATURE_FLAGS / data/feature_flags.json.
// Loaded once at startup (see the jarvis_ready handler) and re-synced
// whenever a 'feature_flags_state' socket event arrives, so a toggle
// flipped from another connected HUD tab (or a future settings surface)
// reflects here immediately too.
const FEATURE_FLAG_LABELS = [
  { key: 'proactividad',      label: 'Proactividad' },
  { key: 'busqueda_web',      label: 'Búsqueda web' },
  { key: 'copiloto_hud',      label: 'Co-piloto HUD' },
  { key: 'paneles_dinamicos', label: 'Paneles dinámicos' },
  { key: 'deteccion_tono',    label: 'Detección de tono' },
  { key: 'memoria_episodica', label: 'Memoria episódica' },
]
let _featureFlags = {}

function _renderFeatureToggles() {
  const container = document.getElementById('settingsToggles')
  if (!container) return
  container.innerHTML = FEATURE_FLAG_LABELS.map(({ key, label }) => {
    const on = _featureFlags[key] !== false   // unknown/not-yet-loaded defaults ON
    return `
      <div class="toggle-row">
        <span class="toggle-label">${esc(label)}</span>
        <button class="toggle-switch${on ? ' on' : ''}" data-flag="${key}" role="switch" aria-checked="${on}" title="${esc(label)}"></button>
      </div>
    `
  }).join('')
  container.querySelectorAll('.toggle-switch').forEach(btn => {
    btn.addEventListener('click', () => _toggleFeatureFlag(btn.dataset.flag, btn))
  })
  _applyTestModeUI()
  _renderTestToolsToggles()
}

// ════════════════════════════════════════════════════════════════════════════
// TEST TOOLS PANEL — Modo Test's expandable sub-menu. Same config-driven
// .toggle-row rendering as FEATURE_FLAG_LABELS/_renderFeatureToggles above
// (deliberately a separate list/container, not just more entries appended
// to FEATURE_FLAG_LABELS — these are grouped under their own collapsed-by-
// default panel rather than mixed into the always-visible FUNCIONES list),
// reusing the exact same _toggleFeatureFlag()/#settingsToggles.toggle-switch
// machinery — no new backend wiring needed beyond the flags themselves
// (core/memory_flags.py) already existing.
// ════════════════════════════════════════════════════════════════════════════
const TEST_TOOLS_TOGGLE_LABELS = [
  { key: 'voice_recognition_enabled', label: 'Reconocimiento de voz' },
  { key: 'voice_learning_enabled',    label: 'Aprendizaje de voz' },
  { key: 'voice_trust_all',           label: 'Marcar toda voz como conocida' },
]

function _renderTestToolsToggles() {
  const container = document.getElementById('testToolsToggles')
  if (!container) return
  container.innerHTML = TEST_TOOLS_TOGGLE_LABELS.map(({ key, label }) => {
    const on = _featureFlags[key] !== false
    return `
      <div class="toggle-row">
        <span class="toggle-label">${esc(label)}</span>
        <button class="toggle-switch${on ? ' on' : ''}" data-flag="${key}" role="switch" aria-checked="${on}" title="${esc(label)}"></button>
      </div>
    `
  }).join('')
  container.querySelectorAll('.toggle-switch').forEach(btn => {
    btn.addEventListener('click', () => _toggleFeatureFlag(btn.dataset.flag, btn))
  })
}

// Expand/collapse — purely a display grouping (see the panel's own HTML
// comment), so this only ever toggles the .open class; every toggle inside
// stays live and backend-synced regardless of whether the panel is shown.
const testToolsExpandBtn = document.getElementById('testToolsExpandBtn')
const testToolsPanel     = document.getElementById('testToolsPanel')
testToolsExpandBtn?.addEventListener('click', () => {
  const open = testToolsPanel.classList.toggle('open')
  testToolsExpandBtn.classList.toggle('open', open)
  testToolsExpandBtn.setAttribute('aria-expanded', String(open))
})

// MODO TEST — reflects _featureFlags.modo_test into every surface that
// cares: the Ajustes toggle itself (button + appearing hint), the
// always-visible persistent-bar dot (via body.test-mode-active, see its
// CSS), and "Iniciar Sueño"'s visually-blocked-but-still-clickable state
// (see _showSleepConfirm below for why it's not a real `disabled` — a
// disabled button never fires click, which would silently swallow the
// "Desactiva el modo test..." message instead of showing it). Called from
// _renderFeatureToggles() itself (the one place all three _featureFlags
// update paths already converge — load, manual toggle, socket sync), not
// wired separately at each call site.
function _applyTestModeUI() {
  const active = _featureFlags.modo_test === true
  document.body.classList.toggle('test-mode-active', active)

  const toggle = document.getElementById('testModeToggle')
  if (toggle) {
    toggle.classList.toggle('on', active)
    toggle.setAttribute('aria-checked', String(active))
  }
  const hint = document.getElementById('testModeHint')
  if (hint) hint.classList.toggle('visible', active)

  if (sleepStartBtn) sleepStartBtn.classList.toggle('test-blocked', active)
}

async function _toggleFeatureFlag(key, btn) {
  const newState = !(_featureFlags[key] !== false)
  btn.disabled = true
  try {
    const res  = await fetch(`${JARVIS_API}/api/feature_flags`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: key, enabled: newState }),
    })
    const data = await res.json()
    if (res.ok && data.ok) {
      _featureFlags = data.flags
    }
  } catch { /* leave previous state; next load/sync reconciles */ }
  // Bug fix: the FUNCIONES/test-tools rows below get brand-new (so
  // naturally un-disabled) button elements every render — innerHTML
  // rebuilds them from scratch — so this re-enable was never needed for
  // those. #testModeToggle, though, is a static element wired ONCE in
  // settings-updates.js and never recreated: without this line, the FIRST
  // click on it (btn.disabled = true above) left it permanently disabled,
  // since nothing else ever cleared that flag on the same persistent DOM
  // node — one click on, and it could never be clicked again to turn off.
  btn.disabled = false
  _renderFeatureToggles()
}

async function _loadFeatureFlags() {
  try {
    const res = await fetch(`${JARVIS_API}/api/feature_flags`)
    _featureFlags = await res.json()
  } catch { _featureFlags = {} }
  _renderFeatureToggles()
}

// MODO TEST toggle — reuses _toggleFeatureFlag() (same POST endpoint, same
// flip-and-re-render flow every other flag uses), it's just not rendered
// as one of the generic .toggle-switch rows (see its own HTML comment).
