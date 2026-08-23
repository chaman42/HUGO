// backend_process.js — locating and spawning the local launcher.py process,
// health-probing it (locally or over Tailscale), auto-restarting it on an
// unexpected crash, and the graceful/forceful shutdown paths used on quit
// and on the HUD's "Reintentar" (full backend restart) button.
'use strict'

const { app } = require('electron')
const path = require('path')
const os   = require('os')
const fs   = require('fs')
const http = require('http')
const { spawn, execSync, execFileSync } = require('child_process')
const { showStartupError } = require('./window')
const state = require('./state')

const PROBE_TIMEOUT = 2_000    // ms per individual URL probe
const BOOT_TIMEOUT  = 120_000  // ms to wait for local backend on first launch — Vosk +
                                // Kokoro pre-warm can take 30-60s on a cold first launch
const POLL_INTERVAL = 1_500    // ms between readiness poll ticks

// Passed to app.relaunch({ args }) after a successful in-app update, and
// checked in startLauncher() below to set HUGO_AUTOSTART=1 for that one
// launcher.py spawn.
const AUTOSTART_ARG = '--hugo-autostart'

// ---------------------------------------------------------------------------
// Locate the JarvisLite project root (the directory that contains launcher.py)
// ---------------------------------------------------------------------------
function findProjectRoot () {
  const candidates = [
    path.resolve(__dirname, '..'),                          // dev: electron/ is inside project
    path.join(os.homedir(), 'Desktop', 'HUGO'),
    path.join(os.homedir(), 'HUGO'),
    '/opt/HUGO',
  ]
  for (const dir of candidates) {
    if (fs.existsSync(path.join(dir, 'launcher.py'))) return dir
  }
  return null
}

// ---------------------------------------------------------------------------
// HTTP health probe
// ---------------------------------------------------------------------------
function probe (baseUrl) {
  return new Promise(resolve => {
    const req = http.get(`${baseUrl}/api/health`, { timeout: PROBE_TIMEOUT }, res => {
      res.resume()
      resolve(res.statusCode < 500)
    })
    req.on('error',   () => resolve(false))
    req.on('timeout', () => { req.destroy(); resolve(false) })
  })
}

// Poll the health endpoint until it responds, then resolve.
function waitForBackend (url) {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + BOOT_TIMEOUT
    const tick = () => {
      probe(url).then(ok => {
        if (ok) return resolve()
        if (Date.now() > deadline) return reject(new Error('Backend startup timed out'))
        setTimeout(tick, POLL_INTERVAL)
      })
    }
    tick()
  })
}

// ---------------------------------------------------------------------------
// Auto-start jarvis.py — no manual button in the loop. The boot splash in
// ui/index.html ("Cargando...") only fades once #bootOverlay's state
// machine reaches 'running' (see _enterBootSplashWait()), and that state
// machine already reacts correctly to jarvis starting via socket/poll
// events (launcher.on('jarvis_status'/'jarvis_ready')) regardless of who
// triggered the start — it never needed the click itself, only the
// resulting POST /api/start. The diamond #powerBtn used to make that POST,
// but it sits inside #bootOverlay, which is covered by the splash until the
// splash itself fades — which never happened, since nothing else called
// /api/start. Fire it here instead, the moment launcher.py's health check
// succeeds, so the boot splash's own listeners take it from there. Safe to
// call unconditionally: /api/start is idempotent (a no-op if jarvis.py is
// already running, e.g. via HUGO_AUTOSTART=1 on a post-update relaunch).
// ---------------------------------------------------------------------------
function autoStartJarvis (baseUrl) {
  try {
    const u = new URL(baseUrl)
    const req = http.request(
      { hostname: u.hostname, port: Number(u.port || 80), path: '/api/start', method: 'POST', timeout: 5000 },
      res => res.resume()
    )
    req.on('error', err => console.warn('[HUGO] Auto-start POST /api/start failed:', err.message))
    req.end()
  } catch (err) {
    console.warn('[HUGO] Auto-start POST /api/start failed:', err.message)
  }
}

// ---------------------------------------------------------------------------
// Python / launcher.py process management
// ---------------------------------------------------------------------------
let launcherProc     = null   // Popen handle for the locally spawned launcher
let startedLocally   = false  // true only when this process spawned the launcher
let localProjectRoot = null   // set by startLauncher() — used to scope the quit-time pkill sweep

// Auto-restart bookkeeping — see the `exit` handler in startLauncher() below.
let launcherRestartAttempts = 0
const MAX_LAUNCHER_RESTARTS  = 5     // give up after this many consecutive crashes
const LAUNCHER_RESTART_DELAY = 2_000 // ms between respawn attempts

function findPython (projectRoot) {
  // Prefer the project's own venv, but only if it can actually run launcher.py
  // — checking `import flask` (launcher.py's first real dependency) instead
  // of just `--version` catches the case where a venv directory exists but
  // was never fully provisioned with requirements.txt. Without this check a
  // half-empty venv would be picked, launcher.py would spawn and immediately
  // crash with ModuleNotFoundError, and the app would silently fail to start.
  const candidates = [
    path.join(projectRoot, 'venv', 'bin', 'python3'),
    // Legacy sibling-project venv — this repo was originally developed
    // alongside ~/Desktop/JarvisProject and its venv is still the one with
    // the full dependency set (torch, spacy, kokoro, groq, etc.) actually
    // installed. Kept as a fallback until JarvisLite/venv is fully
    // provisioned from requirements.txt in its own right.
    path.join(path.dirname(projectRoot), 'JarvisProject', 'venv', 'bin', 'python3'),
    'python3',
    '/usr/bin/python3',
    '/usr/local/bin/python3',
  ]
  for (const py of candidates) {
    try { execSync(`"${py}" -c "import flask"`, { stdio: 'ignore' }); return py } catch (_) {}
  }
  // Nothing has Flask — fall back to whatever merely responds to --version,
  // so the resulting failure is at least a clear ModuleNotFoundError from
  // launcher.py rather than "spawn ENOENT" from a nonexistent interpreter.
  for (const py of candidates) {
    try { execSync(`"${py}" --version`, { stdio: 'ignore' }); return py } catch (_) {}
  }
  return 'python3'  // last-resort: rely on $PATH
}

function startLauncher (projectRoot) {
  if (launcherProc) return  // already running
  localProjectRoot = projectRoot

  const python      = findPython(projectRoot)
  const launcherPath = path.join(projectRoot, 'launcher.py')

  // Set only when this exact process was relaunched with AUTOSTART_ARG (see
  // the before-quit handler in main.js and setupUpdateRelaunchPoll() there)
  // — tells launcher.py to start jarvis.py itself, slightly earlier than
  // waiting on the autoStartJarvis() POST below (which now fires on every
  // launch regardless of this flag — see bootBackend()). Redundant with
  // that POST in practice (both are idempotent), kept because it shaves the
  // extra health-check round trip off the specific post-update relaunch
  // path. process.argv keeps the arg for the lifetime of this Electron
  // process, so a launcher.py crash-restart (see the exit handler below)
  // also carries it.
  const autostart = process.argv.includes(AUTOSTART_ARG)
  console.log(`[HUGO] Spawning: ${python} ${launcherPath}${autostart ? '  (HUGO_AUTOSTART=1)' : ''}`)
  launcherProc = spawn(python, [launcherPath], {
    cwd:      projectRoot,
    stdio:    ['ignore', 'pipe', 'pipe'],
    detached: false,                        // child dies when Electron dies
    env:      {
      ...process.env,
      PYTHONUNBUFFERED: '1',
      ...(autostart ? { HUGO_AUTOSTART: '1' } : {}),
    },
  })

  launcherProc.stdout.on('data', d => {
    process.stdout.write(`[lnc] ${d}`)
    // Any output means the process is alive and doing real work — reset the
    // crash-loop counter so one old failure doesn't eat into a later, unrelated
    // restart budget.
    launcherRestartAttempts = 0
  })
  launcherProc.stderr.on('data', d => process.stderr.write(`[lnc] ${d}`))
  launcherProc.on('exit', (code, signal) => {
    console.log(`[HUGO] Launcher exited: code=${code} signal=${signal}`)
    launcherProc = null

    // Unexpected death while HUGO is still open (crash, OOM, killed from
    // outside, reaped after a sleep/wake cycle, etc.) — respawn automatically
    // and re-fire autoStartJarvis() once the respawned launcher is healthy
    // again, same as the initial boot (see bootBackend()) — no user
    // interaction needed here either. Skipped during our own intentional
    // shutdown (_quitting) and when we never spawned a local launcher to
    // begin with (remote Tailscale backend).
    if (state.isQuitting() || !startedLocally) return

    if (launcherRestartAttempts >= MAX_LAUNCHER_RESTARTS) {
      console.error(`[HUGO] Launcher crashed ${MAX_LAUNCHER_RESTARTS} times in a row — giving up auto-restart.`)
      if (app.dock) app.dock.bounce('critical')
      showStartupError(
        'HUGO se ha detenido inesperadamente varias veces.\n\n' +
        'Revisa logs/launcher.log y vuelve a intentarlo.',
        () => {
          launcherRestartAttempts = 0
          startLauncher(projectRoot)
        },
      )
      return
    }
    launcherRestartAttempts++
    console.warn(`[HUGO] Launcher exited unexpectedly — restarting in ${LAUNCHER_RESTART_DELAY}ms ` +
                 `(attempt ${launcherRestartAttempts}/${MAX_LAUNCHER_RESTARTS})…`)
    setTimeout(() => {
      startLauncher(projectRoot)
      const LOCALHOST_URL = 'http://localhost:8079'
      waitForBackend(LOCALHOST_URL)
        .then(() => autoStartJarvis(LOCALHOST_URL))
        .catch(() => {})  // waitForBackend already reports/handles its own timeout via bootBackend's caller
    }, LAUNCHER_RESTART_DELAY)
  })

  startedLocally = true
  console.log(`[HUGO] Launcher PID ${launcherProc.pid}`)
}

// Reset the crash-loop bookkeeping for a full user-triggered restart (see
// main.js's 'restart-backend' ipcMain handler) — startedLocally must be
// false so the next startLauncher() call actually spawns a fresh process.
function resetForRestart () {
  startedLocally = false
  launcherRestartAttempts = 0
}

// Ask launcher.py to stop jarvis.py cleanly via POST /api/stop, then actively
// poll GET /api/health until it confirms jarvis is no longer running (rather
// than guessing with a fixed delay) — this is what "wait for clean shutdown"
// means before Electron itself quits. SIGTERM's the launcher process once
// confirmed, then runs a pkill sweep as a last-resort safety net in case
// anything survived. A bounded SHUTDOWN_LIMIT guarantees quit never hangs
// even if the backend is unresponsive. Calls `done()` exactly once, so the
// caller (before-quit) can wait for genuine confirmation instead of a blind
// timeout. Safe to call multiple times (guarded by startedLocally).
function killBackend (done) {
  const finish = () => { if (done) done() }
  if (!startedLocally) { finish(); return }   // remote Tailscale backend — leave it alone

  const HEALTH_POLL_INTERVAL = 300   // ms between /api/health checks
  const SHUTDOWN_LIMIT       = 6000  // ms — safety net if polling never confirms
  const deadline = Date.now() + SHUTDOWN_LIMIT
  let settled = false

  function finalizeAndExit () {
    if (settled) return
    settled = true
    if (launcherProc) {
      try { launcherProc.kill('SIGTERM') } catch (_) {}
    }
    // Last-resort safety net: catch anything that survived the graceful path
    // above (e.g. launcher.py wedged, or a jarvis.py child it lost track of).
    // Scoped to this project's actual absolute script paths (via execFileSync,
    // no shell involved) rather than a bare 'python.*launcher\.py' pattern —
    // the broad version matches ANY process whose command line merely
    // mentions the filename (an editor with it open, a grep, a shell wrapper
    // running a command that names the file), which would kill something
    // that has nothing to do with HUGO.
    if (localProjectRoot) {
      const launcherPath = path.join(localProjectRoot, 'launcher.py')
      const jarvisPath    = path.join(localProjectRoot, 'jarvis.py')
      try { execFileSync('pkill', ['-f', launcherPath], { stdio: 'ignore' }) } catch (_) {}
      try { execFileSync('pkill', ['-f', jarvisPath],   { stdio: 'ignore' }) } catch (_) {}
    } else {
      try { execSync("pkill -f 'python.*launcher\\.py'", { stdio: 'ignore' }) } catch (_) {}
      try { execSync("pkill -f 'python.*jarvis\\.py'",   { stdio: 'ignore' }) } catch (_) {}
    }
    finish()
  }

  function pollUntilStopped () {
    if (Date.now() > deadline) { finalizeAndExit(); return }
    const req = http.get(
      { hostname: 'localhost', port: 8079, path: '/api/health', timeout: 1500 },
      res => {
        let body = ''
        res.on('data', c => { body += c })
        res.on('end', () => {
          try {
            const { jarvis_running } = JSON.parse(body)
            if (!jarvis_running) { finalizeAndExit(); return }
          } catch (_) {}
          setTimeout(pollUntilStopped, HEALTH_POLL_INTERVAL)
        })
      }
    )
    req.on('error',   () => finalizeAndExit())   // launcher itself is down — good enough
    req.on('timeout', () => { req.destroy(); setTimeout(pollUntilStopped, HEALTH_POLL_INTERVAL) })
  }

  try {
    const req = http.request(
      { hostname: 'localhost', port: 8079, path: '/api/stop', method: 'POST', timeout: 2000 },
      () => pollUntilStopped()
    )
    req.on('error', () => finalizeAndExit())  // launcher unreachable — nothing to wait for
    req.end()
  } catch (_) {
    finalizeAndExit()
  }
}

// Force-frees a set of TCP ports by killing whatever's bound to them — a
// broader net than killBackend()'s own pkill-by-script-path sweep above,
// which only matches processes whose command line names launcher.py/
// jarvis.py. This catches what that sweep can miss entirely: a stray
// process bound to 8079/8080 that isn't ours by name (an orphan from a
// previous crashed session that got re-parented, a manually-started
// process, etc.) — exactly the kind of wedged state a user-triggered
// "nuclear" restart needs to clear, per the retry button's own contract
// (see main.js's ipcMain.on('restart-backend') handler).
//
// SIGTERM first, then SIGKILL any survivor after a brief grace window, so a
// process mid-write to a data file at least gets a chance to close it
// cleanly before being forced — via setTimeout, NOT a blocking wait: this
// runs on Electron's main-process event loop, and a synchronous busy-wait
// here would freeze the whole app (window repaints, other IPC, the tray)
// for the entire grace window. `done` fires once ports are (hopefully)
// clear either way — never throws, never hangs past the grace window.
function _pidsOnPorts (ports) {
  const pids = []
  for (const port of ports) {
    try {
      execSync(`lsof -ti tcp:${port}`, { stdio: ['ignore', 'pipe', 'ignore'] })
        .toString().trim().split('\n').filter(Boolean).forEach(p => pids.push(Number(p)))
    } catch (_) { /* lsof exits non-zero when nothing is bound to this port — expected, not an error */ }
  }
  return pids
}

function killProcessesOnPorts (ports, done) {
  const finish = () => { if (done) done() }
  const initialPids = _pidsOnPorts(ports)
  if (!initialPids.length) { finish(); return }

  for (const pid of initialPids) {
    try { process.kill(pid, 'SIGTERM') } catch (_) {}
  }
  setTimeout(() => {
    for (const pid of _pidsOnPorts(ports)) {
      try { process.kill(pid, 'SIGKILL') } catch (_) {}
    }
    finish()
  }, 1200)
}

module.exports = {
  AUTOSTART_ARG,
  findProjectRoot,
  probe,
  waitForBackend,
  autoStartJarvis,
  startLauncher,
  resetForRestart,
  killBackend,
  killProcessesOnPorts,
}
