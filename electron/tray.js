// tray.js — menu-bar tray icon/menu, mute-state polling, and
// window-visibility toggling (Cmd+Shift+Space and the tray icon's own
// click both funnel through toggleWindow() here).
'use strict'

const { app, Tray, Menu, nativeImage } = require('electron')
const path = require('path')
const http = require('http')
const state  = require('./state')
const winMod = require('./window')

let tray         = null
let trayIconNorm = null   // nativeImage for normal (unmuted) state
let trayIconMute = null   // nativeImage for muted state
let isMuted      = false  // local mirror of backend mute state (polled every 5 s)

// Derive the jarvis.py API base URL from the launcher URL (always port 8180).
// Works for both local and Tailscale targets.
const jarvisUrl = () => state.getBackendUrl().replace(/:(\d+)$/, ':8180')

// Fire-and-forget POST — used for mute/unmute/mic_stop/mic_start calls.
function _httpPost (fullUrl) {
  try {
    const u   = new URL(fullUrl)
    const req = http.request(
      { hostname: u.hostname, port: Number(u.port || 80), path: u.pathname,
        method: 'POST', timeout: 2000 },
      res => res.resume()
    )
    req.on('error', () => {})
    req.end()
  } catch (_) {}
}

function buildTrayMenu () {
  const mainWindow = winMod.getMainWindow()
  const winVisible = mainWindow && !mainWindow.isDestroyed() && mainWindow.isVisible()
  return Menu.buildFromTemplate([
    { label: winVisible ? 'Hide HUGO' : 'Show HUGO', click: toggleWindow },
    { type: 'separator' },
    { label: isMuted ? 'Unmute'    : 'Mute',         click: toggleMuteFromTray },
    { type: 'separator' },
    { label: 'Quit HUGO', click: () => app.quit() },
  ])
}

function updateTray () {
  if (!tray) return
  // Switch icon and tooltip to reflect mute state
  tray.setImage(isMuted ? trayIconMute : trayIconNorm)
  tray.setToolTip(isMuted ? 'HUGO — Muted' : 'HUGO')
  tray.setContextMenu(buildTrayMenu())
}

function toggleWindow () {
  const mainWindow = winMod.getMainWindow()
  if (!mainWindow || mainWindow.isDestroyed()) return
  if (mainWindow.isVisible()) {
    mainWindow.hide()
  } else {
    mainWindow.show()
    mainWindow.focus()
    // Restore dock icon in case it was hidden while the window was invisible
    if (app.dock) app.dock.show()
  }
  // Re-build the menu so the "Show/Hide" label flips
  updateTray()
}

function toggleMuteFromTray () {
  isMuted = !isMuted
  const jUrl = jarvisUrl()
  if (isMuted) {
    _httpPost(`${jUrl}/api/mute`)
    // Stop the PortAudio stream so macOS removes the orange mic indicator dot
    _httpPost(`${jUrl}/api/mic_stop`)
  } else {
    _httpPost(`${jUrl}/api/unmute`)
    // Resume the PortAudio stream → orange dot reappears naturally
    _httpPost(`${jUrl}/api/mic_start`)
  }
  updateTray()
}

function setupTray () {
  // Load the two tray icon PNGs (16×16 monochrome waveform shapes).
  // setTemplateImage(true) lets macOS auto-invert for dark/light menu bar.
  const assetsDir = path.join(__dirname, 'assets')
  trayIconNorm = nativeImage.createFromPath(path.join(assetsDir, 'tray-icon.png'))
  trayIconNorm.setTemplateImage(true)
  trayIconMute = nativeImage.createFromPath(path.join(assetsDir, 'tray-icon-muted.png'))
  trayIconMute.setTemplateImage(true)

  tray = new Tray(trayIconNorm)
  tray.setToolTip('HUGO')

  // Single-click the menu bar icon → toggle window visibility
  tray.on('click', toggleWindow)

  updateTray()

  // Poll mute state every 5 s so the tray icon stays in sync when the user
  // toggles mute via the HUD button (SocketIO events don't reach main.js directly)
  setInterval(() => {
    if (!state.getBackendUrl()) return
    try {
      const u = new URL(`${jarvisUrl()}/api/mute_state`)
      const req = http.get(
        { hostname: u.hostname, port: Number(u.port || 80), path: u.pathname, timeout: 1500 },
        res => {
          let body = ''
          res.on('data', c => { body += c })
          res.on('end', () => {
            try {
              const { muted } = JSON.parse(body)
              if (typeof muted === 'boolean' && muted !== isMuted) {
                isMuted = muted
                updateTray()
              }
            } catch (_) {}
          })
        }
      )
      req.on('error', () => {})
    } catch (_) {}
  }, 5000)
}

function destroyTray () {
  if (tray) { tray.destroy(); tray = null }
}

module.exports = {
  setupTray,
  updateTray,
  toggleWindow,
  destroyTray,
}
