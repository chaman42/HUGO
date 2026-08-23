'use strict'

// ---------------------------------------------------------------------------
// HUGO — Electron preload script
//
// Runs in the renderer process (the HUD page) with access to Node APIs before
// the web content loads.  Only the explicitly exposed surface crosses the
// context isolation boundary — the HUD cannot access Node or Electron directly.
//
// The HUD is a self-contained web app that works fine without any Electron APIs.
// The surface below is an optional integration layer (version badge, native quit).
// ---------------------------------------------------------------------------

const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('electronAPI', {
  // App version string (from package.json) — useful for a version badge in the HUD
  getVersion: () => ipcRenderer.invoke('get-version'),

  // Trigger a clean quit from the renderer (calls app.quit() in main)
  quit: () => ipcRenderer.send('quit'),

  // Read-only OS identifier so the HUD can apply platform-specific styles
  platform: process.platform,

  // Forward an assistant response to main.js for native macOS notifications.
  // Called by the NOTIFY_INJECTOR script that wraps addMessage() in the HUD.
  notifyResponse: (personality, text) =>
    ipcRenderer.send('jarvis-response', { personality, text }),

  // "Nuclear" boot recovery — called by the HUD's boot-splash "Reintentar"
  // button once it's been stuck waiting on jarvis_ready past its own
  // timeout (see _enterBootSplashWait() in ui/index.html). Distinct from
  // the plain HTTP retry that button already falls back to outside
  // Electron: main.js's restart-backend handler kills launcher.py +
  // jarvis.py, force-frees ports 8079/8080 in case something's wedged
  // outside normal process tracking, then reboots from scratch — a
  // stronger recovery than just asking the (possibly unresponsive)
  // launcher.py to restart jarvis.py again via its own HTTP API.
  restartBackend: () => ipcRenderer.send('restart-backend'),
})
