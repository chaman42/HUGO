'use strict'

// ---------------------------------------------------------------------------
// LIRA — Electron main process entry point
//
// Responsibilities:
//   1. Probe Tailscale → fall back to localhost and spawn launcher.py locally
//   2. Poll the backend health endpoint until ready
//   3. Open a chromeless BrowserWindow loading the JarvisLite HUD directly —
//      no separate loading window; the HUD's own boot-splash covers the wait
//   4. Check GitHub Releases for app updates on every launch
//   5. Kill launcher.py + jarvis.py cleanly on quit
//
// Port notes:
//   8079 — launcher.py: serves the full HUD (index.html) + launcher SocketIO
//   8080 — jarvis.py  : AI backend SocketIO (the HUD connects to it internally)
//   The Electron window loads from port 8079. Port 8080 is handled by the HUD.
//   The user-specified Tailscale primary URL uses 8079 on the remote machine.
//
// The window/tray/backend-process/updater responsibilities themselves live in
// window.js, tray.js, backend_process.js, and updater.js respectively — this
// file wires them together plus owns the IPC handlers and app lifecycle
// events that don't cleanly belong to any one of those.
// ---------------------------------------------------------------------------

const { app, ipcMain, globalShortcut, Notification } = require('electron')
const http = require('http')

const state   = require('./state')
const winMod  = require('./window')
const trayMod = require('./tray')
const backend = require('./backend_process')
const { setupUpdater } = require('./updater')

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

// Primary: Tailscale IP where the remote JarvisLite stack is running.
// The HUD is served by launcher.py on port 8079; port 8080 is the AI backend
// that the HUD page connects to by itself once loaded.
// NOTE: The requirements specified port 8080 as the Electron window URL, but
// the HUD page lives at 8079 (launcher). Adjust TAILSCALE_URL if your remote
// machine's launcher uses a non-default port.
const TAILSCALE_URL  = 'http://100.124.252.100:8079'
const LOCALHOST_URL  = 'http://localhost:8079'

const UPDATE_POLL_INTERVAL = 3_000  // ms between /api/health checks for a pending relaunch

// ---------------------------------------------------------------------------
// Single-instance lock — if a second LIRA process is launched (e.g. double-
// clicking the app icon), focus the existing window and exit the new process.
// ---------------------------------------------------------------------------
const _gotSingleInstanceLock = app.requestSingleInstanceLock()
if (!_gotSingleInstanceLock) {
  // This IS the second instance — signal the first and exit immediately
  app.quit()
}
app.on('second-instance', () => {
  // First instance receives this event; bring its window to front
  const mainWindow = winMod.getMainWindow()
  if (mainWindow && !mainWindow.isDestroyed()) {
    if (!mainWindow.isVisible()) {
      mainWindow.show()
      if (app.dock) app.dock.show()
    }
    mainWindow.focus()
    trayMod.updateTray()
  }
})

// ---------------------------------------------------------------------------
// Global keyboard shortcut — Cmd+Shift+Space activates LIRA from any app.
// TODO: make the accelerator configurable via settings panel
//       (persist to ~/Library/Application Support/LIRA/settings.json).
// ---------------------------------------------------------------------------
function setupGlobalShortcut () {
  const accelerator = 'CommandOrControl+Shift+Space'
  const ok = globalShortcut.register(accelerator, () => {
    const mainWindow = winMod.getMainWindow()
    if (!mainWindow || mainWindow.isDestroyed()) return
    if (mainWindow.isVisible() && mainWindow.isFocused()) {
      // Already in focus — hide it (toggle)
      mainWindow.hide()
    } else {
      mainWindow.show()
      mainWindow.focus()
      if (app.dock) app.dock.show()
    }
    trayMod.updateTray()
  })
  if (!ok) {
    console.warn('[LIRA] Could not register global shortcut', accelerator,
      '— another app may be using it.')
  } else {
    console.log('[LIRA] Global shortcut registered:', accelerator)
  }
}

// ---------------------------------------------------------------------------
// Auto-relaunch after an in-app update ("Actualizar LIRA" in System Info).
// Same reasoning as the mute-state poll in tray.js — SocketIO events from
// launcher.py reach the renderer, not this main process — so this polls
// launcher.py's /api/health for the pending_relaunch flag that api_update()
// sets once scripts/rebuild_app.sh has actually finished. On detection, it
// funnels through the exact same graceful-shutdown path as Cmd+Q/Tray Quit
// (_relaunchAfterQuit + app.quit(), handled in the before-quit handler
// below) rather than a second parallel shutdown routine, then relaunches
// with AUTOSTART_ARG so the fresh instance starts jarvis.py immediately —
// see backend_process.js's startLauncher() and launcher.py's LIRA_AUTOSTART check.
// ---------------------------------------------------------------------------
let _relaunchTriggered = false

function setupUpdateRelaunchPoll () {
  setInterval(() => {
    if (_relaunchTriggered || state.isQuitting() || !state.getBackendUrl()) return
    try {
      const u = new URL(state.getBackendUrl())
      const req = http.get(
        { hostname: u.hostname, port: Number(u.port || 80), path: '/api/health', timeout: 1500 },
        res => {
          let body = ''
          res.on('data', c => { body += c })
          res.on('end', () => {
            try {
              const { pending_relaunch } = JSON.parse(body)
              if (pending_relaunch && !_relaunchTriggered) {
                _relaunchTriggered  = true
                _relaunchAfterQuit  = true
                console.log('[LIRA] Update completed — relaunching to pick it up…')
                app.quit()
              }
            } catch (_) {}
          })
        }
      )
      req.on('error', () => {})
    } catch (_) {}
  }, UPDATE_POLL_INTERVAL)
}

// ---------------------------------------------------------------------------
// IPC handlers (called from the renderer via preload.js)
// ---------------------------------------------------------------------------
ipcMain.handle('get-version', () => app.getVersion())
ipcMain.on('quit',            () => app.quit())

// Renderer forwards jarvis responses here (see window.js's NOTIFY_INJECTOR).
// Only show a native notification when the window is not visible or not focused
// — never interrupt the user while they are actively watching the HUD.
ipcMain.on('jarvis-response', (event, { personality, text } = {}) => {
  const mainWindow = winMod.getMainWindow()
  if (!mainWindow || mainWindow.isDestroyed()) return
  if (mainWindow.isVisible() && mainWindow.isFocused()) return

  // Extract the first sentence; cap at 100 characters
  const first = String(text || '').split(/[.!?…]/)[0].trim()
  const body  = first.length > 100 ? first.slice(0, 97) + '…' : first
  if (!body) return

  if (!Notification.isSupported()) return

  const title = String(personality || 'lira').toUpperCase()
  const n = new Notification({ title, body, silent: true })
  n.on('click', () => {
    // Bring window to front — unhide if necessary (e.g. user clicked red X)
    const win = winMod.getMainWindow()
    if (win && !win.isDestroyed()) {
      win.show()
      win.focus()
      if (app.dock) app.dock.show()
      trayMod.updateTray()
    }
  })
  n.show()
})

// ---------------------------------------------------------------------------
// App lifecycle
// ---------------------------------------------------------------------------

// Runs the probe → spawn → poll sequence. Called once on launch and again
// each time the user clicks "Reintentar" on the loading window, so on
// failure it reports into the loading window instead of quitting the app —
// the user should never have to reopen LIRA just because the backend was
// slow once (Vosk + Kokoro pre-warm can take 30-60s on a cold first launch).
let bootInProgress = false
async function bootBackend () {
  if (bootInProgress) return
  bootInProgress = true
  // True when re-running this after the HUD window already exists and is
  // showing (a retry triggered from WITHIN the stuck HUD itself, via
  // ipcMain.on('restart-backend') below — distinct from the startup-error
  // dialog's own "Reintentar" button, which only ever fires before any
  // window exists). In that case step 3 below reloads the existing window instead of creating
  // a duplicate, and steps 4-7 (tray icon, global shortcut, updater, update-
  // relaunch poll) are skipped entirely — they're one-time app-level setup
  // that already ran on the original successful boot; calling setupTray()
  // again would create a SECOND menu-bar icon and leak another polling
  // interval, not fix anything.
  const mainWindowBefore = winMod.getMainWindow()
  const isRetry = !!(mainWindowBefore && !mainWindowBefore.isDestroyed())
  try {
    // 1. Try the Tailscale backend first; fall back to a local launch
    console.log('[LIRA] Probing Tailscale backend at', TAILSCALE_URL, '…')
    const tailscaleOk = await backend.probe(TAILSCALE_URL)

    if (tailscaleOk) {
      state.setBackendUrl(TAILSCALE_URL)
      console.log('[LIRA] Tailscale backend reachable — using', state.getBackendUrl())
    } else {
      state.setBackendUrl(LOCALHOST_URL)
      console.log('[LIRA] Tailscale unreachable — launching backend locally')

      const root = backend.findProjectRoot()
      if (!root) {
        // Bounce dock icon on error — the only time we ever call app.dock.bounce()
        if (app.dock) app.dock.bounce('critical')
        winMod.showStartupError(
          'No se encontró el proyecto JarvisLite.\n\n' +
          'Instálalo en ~/Desktop/JarvisLite o ~/JarvisLite y vuelve a intentarlo.',
          bootBackend,
        )
        return
      }

      backend.startLauncher(root)
    }

    // 2. Block until the backend health endpoint responds
    try {
      await backend.waitForBackend(state.getBackendUrl())
    } catch (err) {
      // Bounce dock icon on error — the only time we ever call app.dock.bounce()
      if (app.dock) app.dock.bounce('critical')
      console.error('[LIRA] Backend did not become ready in time:', err.message)
      winMod.showStartupError(
        'LIRA está tardando más de lo esperado en iniciar.\n\n' +
        'Puedes esperar un poco más o reintentarlo.',
        bootBackend,
      )
      return
    }

    // 2b. launcher.py is confirmed healthy — start jarvis.py automatically,
    //     no user interaction required. See backend_process.js's autoStartJarvis().
    backend.autoStartJarvis(state.getBackendUrl())

    // 3. Open the HUD window (its did-finish-load handler closes the
    //    loading window) — or, on a retry from within an already-open HUD,
    //    just reload the existing window into the fresh backend instead.
    if (isRetry) {
      console.log('[LIRA] Retry: reloading existing HUD window instead of creating a duplicate.')
      mainWindowBefore.loadURL(state.getBackendUrl())
    } else {
      winMod.createWindow(state.getBackendUrl())
    }

    if (!isRetry) {
      // 4. Menu bar tray icon (always-on — survives window hide/close on macOS)
      trayMod.setupTray()

      // 5. Global hotkey: Cmd+Shift+Space activates LIRA from any app
      setupGlobalShortcut()

      // 6. Set up auto-updater (packaged builds only — avoids spurious errors in dev)
      if (app.isPackaged) setupUpdater()

      // 7. Poll for a completed in-app update ("Actualizar LIRA") so this
      //    process can relaunch itself automatically
      setupUpdateRelaunchPoll()
    }
  } finally {
    bootInProgress = false
  }
}

// Fired by the HUD's own boot-splash "Reintentar" button once it's been
// stuck waiting on jarvis_ready past its own timeout (see
// _enterBootSplashWait()/preload.js's restartBackend() in ui/index.html) —
// a strictly stronger recovery than plain bootBackend(): first kills
// launcher.py + jarvis.py (both the graceful killBackend() sweep AND a
// broader force-kill of anything actually bound to ports 8079/8080, since
// the whole point of this button is recovering from a state where the
// normal, name-based process tracking might itself be wedged or wrong —
// see backend_process.js's killProcessesOnPorts()'s own comment), then
// re-runs the full boot sequence from scratch.
//
// Guarded by its OWN flag rather than bootInProgress — that one isn't set
// until bootBackend() itself starts, which only happens AFTER the kill
// chain below already completes (killBackend + killProcessesOnPorts can
// take several seconds), so relying on it alone would leave a window where
// a rapid double-click starts a second, overlapping kill+reboot cycle.
let restartInProgress = false
ipcMain.on('restart-backend', () => {
  if (restartInProgress || bootInProgress) return
  restartInProgress = true
  console.log('[LIRA] restart-backend requested — killing backend and rebooting.')
  backend.killBackend(() => {
    backend.killProcessesOnPorts([8079, 8080], () => {
      backend.resetForRestart()
      restartInProgress = false
      bootBackend()
    })
  })
})

app.whenReady().then(() => {
  bootBackend()
})

// macOS convention: keep the app process alive even with no windows open.
// With hide-on-close this event never fires (window is hidden, not closed).
app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})

// macOS: Dock icon click (or Spotlight re-launch) — show the window if it
// was hidden via red X, or create it fresh if somehow it was destroyed.
app.on('activate', () => {
  const mainWindow = winMod.getMainWindow()
  if (mainWindow && !mainWindow.isDestroyed()) {
    if (!mainWindow.isVisible()) {
      mainWindow.show()
      if (app.dock) app.dock.show()
    }
    mainWindow.focus()
    trayMod.updateTray()
  } else if (!mainWindow && state.getBackendUrl()) {
    winMod.createWindow(state.getBackendUrl())
  }
})

// ---------------------------------------------------------------------------
// Graceful shutdown — covers red-X (via window.js's close handler calling
// app.quit()), Cmd+Q, and Tray › Quit identically, since all three funnel
// through this single before-quit handler.
// ---------------------------------------------------------------------------
// event.preventDefault() blocks the quit until killBackend confirms
// launcher.py + jarvis.py are actually down; app.exit(0) then bypasses
// further lifecycle events so the process terminates unconditionally.

// Set by setupUpdateRelaunchPoll() above before calling app.quit() — tells
// this handler to relaunch (with AUTOSTART_ARG) instead of just exiting,
// once the graceful shutdown below confirms launcher.py/jarvis.py are down.
let _relaunchAfterQuit = false

app.on('before-quit', event => {
  if (state.isQuitting()) return
  state.setQuitting(true)
  event.preventDefault()

  // Restore Dock presence so the app appears during the quit animation on macOS
  if (app.dock) app.dock.show()
  // Unregister all global shortcuts and destroy the tray before the process exits
  globalShortcut.unregisterAll()
  trayMod.destroyTray()

  // Wait for genuine confirmation that jarvis.py/launcher.py have stopped
  // (killBackend itself is bounded by SHUTDOWN_LIMIT, so this can't hang).
  backend.killBackend(() => {
    if (_relaunchAfterQuit) {
      app.relaunch({ args: process.argv.slice(1).concat([backend.AUTOSTART_ARG]) })
    }
    app.exit(0)
  })
})
