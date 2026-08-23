// state.js — small shared mutable state read/written from more than one of
// main.js/window.js/tray.js (backendUrl, the quitting flag). CommonJS
// `require()` caches the module, so every requirer gets the same object;
// destructuring a plain exported variable elsewhere would only capture its
// value at require-time, so these are exposed as get/set functions instead.
'use strict'

let _backendUrl = ''
let _quitting   = false

module.exports = {
  getBackendUrl: () => _backendUrl,
  setBackendUrl: url => { _backendUrl = url },
  isQuitting:    () => _quitting,
  setQuitting:   val => { _quitting = val },
}
