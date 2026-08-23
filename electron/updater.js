// updater.js — GitHub Releases auto-updater (chaman42/JarvisLite). Packaged
// builds only (see main.js's `if (app.isPackaged) setupUpdater()`).
'use strict'

const { app } = require('electron')
const { autoUpdater } = require('electron-updater')

function setupUpdater () {
  autoUpdater.logger              = console
  autoUpdater.autoDownload        = true    // download silently in background
  autoUpdater.autoInstallOnAppQuit = true   // install the moment the user quits

  autoUpdater.on('checking-for-update', () => {
    console.log(`[upd] Checking for updates… (current: v${app.getVersion()})`)
  })
  autoUpdater.on('update-available', info => {
    // Update detected — log version, release date, and download size for visibility
    const size = info.files?.[0]?.size
    const mb   = size ? ` (${(size / 1024 / 1024).toFixed(1)} MB)` : ''
    console.log(`[upd] UPDATE DETECTED: v${info.version} released ${info.releaseDate}${mb} — downloading silently…`)
  })
  autoUpdater.on('update-not-available', info => {
    console.log(`[upd] App is up to date (v${info.version}).`)
  })
  autoUpdater.on('download-progress', p => {
    // Log progress every ~25% to avoid flooding the console
    if (Math.floor(p.percent) % 25 === 0) {
      console.log(`[upd] Download progress: ${p.percent.toFixed(0)}% (${(p.transferred / 1024 / 1024).toFixed(1)} MB / ${(p.total / 1024 / 1024).toFixed(1)} MB)`)
    }
  })
  autoUpdater.on('update-downloaded', info => {
    console.log(`[upd] v${info.version} downloaded — will install on next quit`)
  })
  autoUpdater.on('error', err => {
    console.error('[upd] Error:', err.message)
  })

  // Small delay so the window is shown before the network request fires.
  // checkForUpdatesAndNotify() returns a promise on top of emitting 'error' —
  // leaving it unhandled throws an UnhandledPromiseRejectionWarning on every
  // single launch (confirmed live: this repo is private, so the GitHub
  // Releases check always 404s and always rejects). The 'error' listener
  // above already logs it; this just stops it from being an unhandled
  // rejection too.
  setTimeout(() => {
    autoUpdater.checkForUpdatesAndNotify().catch(() => {})
  }, 5000)
}

module.exports = { setupUpdater }
