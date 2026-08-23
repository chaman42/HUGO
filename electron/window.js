// window.js — the main HUD BrowserWindow, startup-error reporting, and
// window position/size persistence between sessions. Owns `mainWindow`
// directly (only this module ever reassigns it); other modules read the
// current window via getMainWindow().
'use strict'

const { app, BrowserWindow, shell, screen, dialog } = require('electron')
const path = require('path')
const fs   = require('fs')
const state = require('./state')

// ---------------------------------------------------------------------------
// Notification injector — patched into the renderer on every page load.
// Wraps the HUD's global addMessage() so jarvis responses are forwarded to
// the main process via IPC for native macOS notifications.
// The guard prevents double-patching if did-finish-load fires twice.
// ---------------------------------------------------------------------------
const NOTIFY_INJECTOR = `
(function () {
  if (window.__liraNotifyPatched) return;
  window.__liraNotifyPatched = true;
  var _orig = window.addMessage;
  if (typeof _orig !== 'function') return;
  window.addMessage = function (type, message) {
    if (type === 'jarvis' && message &&
        window.electronAPI && window.electronAPI.notifyResponse) {
      var p = (typeof currentPersonality !== 'undefined') ? currentPersonality : 'lira';
      window.electronAPI.notifyResponse(p, message);
    }
    return _orig.apply(this, arguments);
  };
})();
`

// ---------------------------------------------------------------------------
// Window state persistence — ~/Library/Application Support/LIRA/window-state.json
// Saves and restores window position and size between sessions.
// ---------------------------------------------------------------------------
const WIN_STATE_FILE = 'window-state.json'
const DEFAULT_WIDTH  = 1200
const DEFAULT_HEIGHT = 800

function loadWindowState () {
  try {
    const raw   = fs.readFileSync(path.join(app.getPath('userData'), WIN_STATE_FILE), 'utf8')
    const saved = JSON.parse(raw)
    // Validate that we have usable dimensions before trusting the saved state
    if (typeof saved.width === 'number' && typeof saved.height === 'number') return saved
  } catch (_) {}
  return null
}

function saveWindowState (win) {
  if (!win || win.isDestroyed()) return
  try {
    const [x, y]          = win.getPosition()
    const [width, height]  = win.getSize()
    fs.writeFileSync(
      path.join(app.getPath('userData'), WIN_STATE_FILE),
      JSON.stringify({ x, y, width, height })
    )
  } catch (err) {
    console.error('[LIRA] Could not save window state:', err.message)
  }
}

// ---------------------------------------------------------------------------
// Startup errors — no separate loading window exists anymore (removed so
// clicking the app goes straight to the real HUD's own boot-splash instead
// of a generic wheel spinner first); the 30-60s Vosk/Kokoro cold-start wait
// is covered entirely by that in-HUD splash once createWindow() below loads
// it. A startup failure (project not found, backend never came up, launcher
// crash-looped) can therefore happen with NO window open at all, so this
// reports it via a native dialog instead of an HTML window. `onRetry`, if
// given, is called when the user picks the retry button; omit it for
// errors where retrying from here doesn't make sense (e.g. a crash-loop
// that already exhausted its own auto-retries).
// ---------------------------------------------------------------------------
function showStartupError (message, onRetry) {
  const buttons = onRetry ? ['Reintentar', 'Salir'] : ['Entendido']
  dialog.showMessageBox({
    type:     'error',
    title:    'LIRA',
    message:  'No se pudo iniciar LIRA',
    detail:   message,
    buttons,
    defaultId: 0,
  }).then(({ response }) => {
    if (onRetry && response === 0) onRetry()
    else if (onRetry && response === 1) app.quit()
  })
}

// ---------------------------------------------------------------------------
// Main HUD BrowserWindow
// ---------------------------------------------------------------------------
let mainWindow = null

function getMainWindow () { return mainWindow }

function createWindow (url) {
  // Restore saved bounds; default to 1200×800 centered on the primary display
  const saved = loadWindowState()
  const { width: sw, height: sh } = screen.getPrimaryDisplay().workAreaSize
  const winW  = saved?.width  ?? DEFAULT_WIDTH
  const winH  = saved?.height ?? DEFAULT_HEIGHT
  // When no saved position exists, center on the primary display
  const winX  = saved?.x ?? Math.floor((sw - winW) / 2)
  const winY  = saved?.y ?? Math.floor((sh - winH) / 2)

  mainWindow = new BrowserWindow({
    width:           winW,
    height:          winH,
    x:               winX,
    y:               winY,
    minWidth:        800,
    minHeight:       600,
    // Screen size at launch time — Electron enforces this on the initial
    // width/height too (not just live resizes), so a stale saved size from
    // a bigger display never opens larger than the CURRENT one.
    maxWidth:        sw,
    maxHeight:       sh,
    resizable:       true,
    // No native browser chrome — the HUD provides its own UI controls
    frame:           false,
    titleBarStyle:   'hiddenInset',   // macOS: traffic-light buttons, no title bar text
    backgroundColor: '#0a0a0a',       // dark background — prevents white flash before HUD paints
    fullscreenable:  true,
    webPreferences: {
      preload:          path.join(__dirname, 'preload.js'),
      nodeIntegration:  false,
      contextIsolation: true,
    },
    show: false,  // stay hidden until did-finish-load so the HUD is ready when we appear
  })

  // Show the window only after the HUD page has fully loaded — no splash, no white flash.
  // did-finish-load fires after navigation completes; backgroundColor covers the gap.
  // The HUD's own boot-splash (ui/css/boot-splash.css) takes it from here, painting
  // immediately and pulsing until the real jarvis_ready event — no separate window
  // handoff needed.
  mainWindow.webContents.once('did-finish-load', () => {
    mainWindow.show()
    mainWindow.focus()
  })

  // Re-inject the notification forwarder on every page load (first load AND force_reload).
  // Uses `on` not `once` so it survives force_reload navigations from the backend.
  mainWindow.webContents.on('did-finish-load', () => {
    mainWindow.webContents.executeJavaScript(NOTIFY_INJECTOR).catch(() => {})
  })

  mainWindow.loadURL(url)

  // Persist position and size on every move and resize so the next launch restores them
  mainWindow.on('moved',   () => saveWindowState(mainWindow))
  mainWindow.on('resized', () => saveWindowState(mainWindow))

  // ── Red traffic light: hide, don't quit ──────────────────────────────────
  // Standard macOS app behavior — the red button closes the WINDOW, not the
  // app; the backend (launcher.py + jarvis.py) and the menu-bar tray keep
  // running exactly as they already do while the window is hidden via the
  // tray's own "Hide LIRA" (see toggleWindow()) or Cmd+Shift+Space. Same
  // hide + tray-menu-refresh as those paths, so all three are indistinguishable
  // once hidden. Actually quitting is still Cmd+Q or Tray › Quit — both go
  // through the SAME graceful before-quit handler as before, unaffected by
  // this change; only the red button's own default (which used to also
  // route into that same quit path) now takes the hide branch instead.
  mainWindow.on('close', event => {
    if (state.isQuitting()) return  // already tearing down via before-quit — let it close naturally
    event.preventDefault()
    mainWindow.hide()
    // Deferred require — see tray.js's own top-level require of this module;
    // requiring it lazily here (rather than at module load time) avoids a
    // circular require between window.js and tray.js.
    require('./tray').updateTray()
  })

  // Open any target="_blank" links in the system browser, not inside the app
  mainWindow.webContents.setWindowOpenHandler(({ url: extUrl }) => {
    shell.openExternal(extUrl)
    return { action: 'deny' }
  })

  mainWindow.on('closed', () => { mainWindow = null })
}

module.exports = {
  createWindow,
  showStartupError,
  getMainWindow,
}
