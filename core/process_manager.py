"""jarvis.py process lifecycle: spawn, health-poll, auto-restart on crash,
and the /api/start, /api/stop, /api/restart routes that mutate this state.

Those three routes live here (rather than in api_routes.py) deliberately:
they read and rebind the module-level process-state globals below, and
Python's `from module import name` only copies the value at import time —
mutating a rebound name from another module would silently desync from the
real state. Routes that only *read* state (status/health/mic_status) are
safe in api_routes.py via qualified `process_manager.<name>` access; routes
that *rebind* it are kept here so `global` does what it looks like it does.
"""
import json
import os
import subprocess
import threading
import time
import urllib.request

from flask import jsonify

from core.launcher_app import _BASE_DIR, _PYTHON, _LOGS_DIR, app, socketio, logger
from core.mic_permissions import _get_mic_status, _open_mic_preferences, _request_mic_permission
from core.port_cleanup import _free_port, _kill_stale_jarvis

# ---------------------------------------------------------------------------
# Process state
# ---------------------------------------------------------------------------
_jarvis_proc: subprocess.Popen | None = None
_proc_lock       = threading.Lock()
_start_time: float | None = None
_jarvis_ready    = False          # True once /api/ready on port 8080 confirms
_retry_count     = 0
_MAX_RETRIES     = 5
_RESTART_DELAY   = 3.0            # seconds before first restart attempt
_COOLDOWN_DELAY  = 30.0           # seconds after max retries
_JARVIS_PORT     = 8080

# Guards _start_jarvis() against concurrent invocation — see that function's
# own docstring for the real incident this fixes (logs/launcher.log,
# 2026-07-18: two jarvis.py PIDs spawned one second apart, both later timing
# out on the ready poll). _jarvis_proc alone isn't enough to detect "a start
# is already under way": it stays None for the ENTIRE mic-permission wait
# (_request_mic_permission() blocks up to 30s) — every check made during
# that window (api_start()'s own, and any other caller's) sees "nothing
# running yet" and proceeds, regardless of another call already mid-start.
_start_in_progress = False

# Set to True by /api/stop before killing jarvis.py so that _monitor_loop
# skips the auto-restart.  Cleared by /api/start (and by the restart path in
# api_restart) so crashes after a fresh start still trigger auto-recovery.
# Always read/written while holding _proc_lock.
_intentional_stop: bool = False

# Set to True by /api/update once scripts/rebuild_app.sh has actually
# completed successfully. electron/main.js has no direct channel from this
# process (SocketIO events don't reach the Electron main process, only the
# renderer) — it polls /api/health, sees this flag, and relaunches the whole
# app. Never reset back to False: this launcher.py process gets killed as
# part of that relaunch anyway, and the fresh instance starts with a fresh
# (False) value by construction.
_pending_relaunch: bool = False

# ---------------------------------------------------------------------------
# Boot progress — see emit_boot_progress() below. _health_progress_emitted
# guards /api/health (polled every ~2s throughout boot AND indefinitely
# during normal operation) so its 'launcher.py responded' stage only ever
# fires once per boot sequence instead of on every poll. Reset at the top of
# _start_jarvis() so a retry/restart correctly re-arms it.
# ---------------------------------------------------------------------------
_health_progress_emitted: bool = False


def emit_boot_progress(stage: str, percent: int, label: str) -> None:
    """Broadcast a real boot-progress milestone to ui/index.html's boot
    splash (see _applyBootProgress() there). One of two emit sources for
    this event — the other is jarvis.py's own socket (core/server.py's
    `socketio`, via `server_mod.socketio.emit(...)`), used once jarvis.py's
    server exists for the stages that happen inside that process (Vosk,
    Kokoro, final ready). The frontend treats percent as monotonic (never
    regresses), since these are two independent processes/sockets and
    perfectly ordered arrival isn't guaranteed."""
    try:
        socketio.emit("boot_progress", {"stage": stage, "percent": percent, "label": label})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------

def _is_running() -> bool:
    with _proc_lock:
        return _jarvis_proc is not None and _jarvis_proc.poll() is None


def _start_jarvis() -> bool:
    """
    Clean-start jarvis.py as a direct subprocess (no Terminal.app, ever).

    Historically this opened a real Terminal.app window when launcher.py had
    no TTY (launchd-spawned), on the theory that CoreAudio/TCC needed a full
    interactive GUI session for microphone access to work. That's no longer
    necessary: _request_mic_permission() actively triggers the AVFoundation
    permission dialog itself, and both of launcher.py's real invocation paths
    (Electron's child process, or a user running `python launcher.py` by
    hand) already run inside a proper Aqua/WindowServer session.

    jarvis.py's stdout/stderr are piped straight into logs/activity.log so
    nothing is lost now that there's no Terminal window to display it in —
    jarvis.py also does its own structured logging to the same file via its
    internal RotatingFileHandler, so this is a defensive belt-and-suspenders
    capture of anything that bypasses Python logging (native library output,
    print()s, tracebacks before logging is configured, etc).

    Bug fix (real incident, logs/launcher.log 2026-07-18 11:45-11:47): this
    function used to have no guard against running twice concurrently.
    _request_mic_permission() below blocks for up to 30s whenever mic access
    is 'not_determined', and _jarvis_proc stays None for that entire window
    — so a second call arriving during it (api_start() only checks
    _jarvis_proc, which looks exactly like "nothing started yet" the whole
    time) sailed straight through the exact same checks and spawned a
    SECOND jarvis.py. The log shows precisely that: two PIDs started one
    second apart, both later timing out on the ready poll (fighting over
    port 8080, or one silently orphaned) — this is almost certainly what a
    "stuck boot animation" looks like in practice. _start_in_progress below
    closes that window: set atomically under _proc_lock before anything
    else runs, checked by every call site (api_start(), the auto-restart in
    _monitor_loop(), a future retry-triggered call — all of them, since the
    guard lives HERE rather than being re-implemented per-caller), and
    always cleared in `finally` so a crash mid-start can never wedge it
    permanently.

    Returns True if the process launched.
    """
    global _jarvis_proc, _start_time, _jarvis_ready, _health_progress_emitted, _start_in_progress

    with _proc_lock:
        if _start_in_progress:
            logger.info("_start_jarvis: a start is already in progress — ignoring duplicate call")
            return False
        if _jarvis_proc is not None and _jarvis_proc.poll() is None:
            logger.info("_start_jarvis: jarvis.py already running — ignoring duplicate call")
            return False
        _start_in_progress = True

    try:
        logger.info("Preparing to start jarvis.py…")
        # Re-arm the boot-progress stages for this fresh attempt — a retry/
        # restart runs this exact function again, and each one is its own boot
        # sequence from the frontend's point of view.
        _health_progress_emitted = False
        _kill_stale_jarvis()
        _free_port(_JARVIS_PORT)

        # Resolve mic permission before starting — request it if never asked
        # before, otherwise just report the (already-decided) status.
        mic = _get_mic_status()
        logger.info("Microphone permission: %s", mic)
        if mic == "not_determined":
            socketio.emit("mic_status", {"status": "not_determined"})
            granted = _request_mic_permission()
            mic = "authorized" if granted else _get_mic_status()
            logger.info("Microphone permission after request: %s", mic)
        if mic == "denied":
            logger.warning("Mic access denied — opening System Preferences automatically.")
            _open_mic_preferences()
        socketio.emit("mic_status", {"status": mic})

        log_path = os.path.join(_LOGS_DIR, "activity.log")
        try:
            log_fh = open(log_path, "a", buffering=1)  # line-buffered append
        except OSError as exc:
            logger.error("Could not open %s for jarvis.py output: %s", log_path, exc)
            log_fh = subprocess.DEVNULL

        try:
            proc = subprocess.Popen(
                [_PYTHON, os.path.join(_BASE_DIR, "jarvis.py")],
                cwd=_BASE_DIR,
                stdout=log_fh,
                stderr=log_fh,
            )
            # Popen dup()s the fd internally — our own handle can close immediately.
            if log_fh is not subprocess.DEVNULL:
                log_fh.close()

            with _proc_lock:
                _jarvis_proc = proc
                _start_time  = time.time()
                _jarvis_ready = False

            logger.info("jarvis.py started — PID %d", proc.pid)
            socketio.emit("jarvis_status", {"running": True, "pid": proc.pid})
            # Boot progress, stage 3/7 — jarvis.py's process now exists (its own
            # socket doesn't yet — that comes later, once server_mod.start() has
            # run inside it; see stages 5-7 in jarvis.py's _signal_ready()).
            emit_boot_progress("jarvis_starting", 40, "Iniciando núcleo...")

            threading.Thread(
                target=_monitor_loop, args=(proc,), daemon=True, name="proc-monitor"
            ).start()
            threading.Thread(
                target=_poll_ready, args=(proc,), daemon=True, name="ready-poll"
            ).start()
            return True

        except Exception as exc:
            logger.error("Failed to launch jarvis.py: %s", exc)
            socketio.emit("jarvis_status", {"running": False, "error": str(exc)})
            return False
    finally:
        with _proc_lock:
            _start_in_progress = False


def _poll_ready(proc: subprocess.Popen) -> None:
    """Poll jarvis's /api/ready every 2 s until it confirms ready or process dies.

    /api/ready on the jarvis side only returns true once ALL subsystems are
    confirmed live (Vosk models, mic stream, Kokoro pre-warm) — so a true
    response here means the system is genuinely operational, not just starting up.
    """
    global _jarvis_ready
    url      = f"http://localhost:{_JARVIS_PORT}/api/ready"
    deadline = time.time() + 120  # give up after 2 minutes
    # Boot progress, stage 6/7 — fires once, on the first response of any
    # kind (ready or not) from jarvis's own HTTP server. Its "ready" flag
    # itself lags behind (Vosk/mic/Kokoro all still loading in-process at
    # this point — see jarvis.py's _signal_ready()), but a response at all
    # means jarvis.py's server is up and reachable. jarvis.py emits stages
    # 4-5-7 directly on its own socket once it's this far along, so this
    # 85% event may arrive before or after those — the frontend treats
    # percent as monotonic, so ordering here doesn't matter.
    server_reachable_emitted = False

    while time.time() < deadline:
        if proc.poll() is not None:
            logger.warning("jarvis.py exited before reporting ready.")
            return

        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                data = json.loads(resp.read())
                if not server_reachable_emitted:
                    server_reachable_emitted = True
                    emit_boot_progress("socket_connected", 85, "Sincronizando...")
                if data.get("ready"):
                    with _proc_lock:
                        _jarvis_ready = True
                    elapsed = time.time() - (_start_time or time.time())
                    logger.info("Jarvis ready — %.1f s after launch", elapsed)
                    socketio.emit("jarvis_ready", {"ready": True, "pid": proc.pid})
                    return
        except Exception:
            pass  # port not up yet or not ready yet

        time.sleep(2)

    # Startup timed out — notify the frontend so the user knows to check logs / restart.
    logger.warning(
        "Ready poll timed out after 120 s — jarvis.py may be stuck during initialization. "
        "Check logs/activity.log for which subsystem failed to start."
    )
    socketio.emit("jarvis_startup_timeout", {
        "message": "Jarvis startup timed out. Check logs/activity.log and restart."
    })


def _monitor_loop(proc: subprocess.Popen) -> None:
    """Wait for process exit, then auto-restart with retry logic."""
    global _retry_count, _jarvis_proc, _jarvis_ready

    proc.wait()                          # block until dead
    exit_code = proc.returncode

    with _proc_lock:
        if _jarvis_proc is not proc:
            # Process was replaced intentionally (stop/restart) — don't auto-restart
            return
        if _intentional_stop:
            # /api/stop set this flag before the kill — do NOT auto-restart.
            # This prevents the race where proc.terminate() fires and _monitor_loop
            # wakes up before api_stop() has had a chance to set _jarvis_proc=None.
            _jarvis_proc  = None
            _jarvis_ready = False
            return
        _jarvis_proc  = None
        _jarvis_ready = False

    logger.warning("jarvis.py exited (code %s)", exit_code)
    socketio.emit("jarvis_status", {"running": False, "exit_code": exit_code})

    if _retry_count < _MAX_RETRIES:
        _retry_count += 1
        logger.info(
            "Auto-restart in %.0f s (attempt %d/%d)…",
            _RESTART_DELAY, _retry_count, _MAX_RETRIES,
        )
        socketio.emit("jarvis_restart", {
            "attempt": _retry_count,
            "max":     _MAX_RETRIES,
            "delay":   _RESTART_DELAY,
        })
        time.sleep(_RESTART_DELAY)
        _start_jarvis()

    else:
        logger.error(
            "Max retries (%d) reached. Cooling down for %.0f s…",
            _MAX_RETRIES, _COOLDOWN_DELAY,
        )
        socketio.emit("jarvis_restart", {
            "failed":  True,
            "wait":    _COOLDOWN_DELAY,
            "message": "Max retries reached. Please fix the error and restart manually.",
        })
        time.sleep(_COOLDOWN_DELAY)
        _retry_count = 0
        logger.info("Retry counter reset. Ready for manual start.")


# ---------------------------------------------------------------------------
# Routes that rebind process state directly — kept alongside the globals
# they mutate (see module docstring).
# ---------------------------------------------------------------------------

@app.route("/api/start", methods=["POST"])
def api_start():
    """Start jarvis.py. Called automatically by electron/main.js's
    autoStartJarvis() the moment this launcher reports healthy (see
    bootBackend() there) — no user interaction required on a normal launch.
    Also reachable manually via the in-HUD power button (#statusPowerBtn) or
    the boot splash's "Reintentar" retry flow. Idempotent either way.
    """
    global _retry_count, _intentional_stop
    logger.info("POST /api/start received")
    with _proc_lock:
        # Clear the intentional-stop flag so crashes after this start auto-recover.
        _intentional_stop = False
        if _jarvis_proc is not None and _jarvis_proc.poll() is None:
            logger.info("POST /api/start: jarvis.py already running — no-op")
            return jsonify({"ok": True, "message": "already running"})
    _retry_count = 0
    ok = _start_jarvis()
    return jsonify({"ok": ok})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    """Stop jarvis.py cleanly (direct-Popen only — no Terminal mode anymore).

    Resets the retry counter so the next manual start gets a clean slate.
    """
    global _jarvis_proc, _jarvis_ready, _retry_count, _intentional_stop
    with _proc_lock:
        # Set the flag BEFORE killing so _monitor_loop skips the auto-restart.
        _intentional_stop = True
        proc = _jarvis_proc
    if proc and proc.poll() is None:
        logger.info("Stopping jarvis.py (PID %d)…", proc.pid)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    else:
        # No tracked proc (e.g. launcher restarted without jarvis dying first) —
        # kill any lingering jarvis.py by name so the port is released.
        logger.info("No tracked proc — killing any stale jarvis processes.")
        _kill_stale_jarvis()
        _free_port(_JARVIS_PORT)

    with _proc_lock:
        _jarvis_proc  = None
        _jarvis_ready = False
    _retry_count = 0   # reset so the next start gets the full retry budget
    socketio.emit("jarvis_status", {"running": False})
    logger.info("jarvis.py stopped.")
    return jsonify({"ok": True})


@app.route("/api/restart", methods=["POST"])
def api_restart():
    global _retry_count

    def _do():
        global _jarvis_proc, _jarvis_ready, _intentional_stop
        with _proc_lock:
            # Flag the stop so _monitor_loop doesn't race us to auto-restart.
            _intentional_stop = True
            proc = _jarvis_proc
        if proc and proc.poll() is None:
            logger.info("Restarting: stopping PID %d…", proc.pid)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        else:
            logger.info("Restarting: killing any stale jarvis processes.")
            _kill_stale_jarvis()
            _free_port(_JARVIS_PORT)

        with _proc_lock:
            _jarvis_proc  = None
            _jarvis_ready = False
            # Clear the flag before the fresh start so crashes in the new process
            # still trigger auto-recovery.
            _intentional_stop = False
        socketio.emit("jarvis_status", {"running": False})

        time.sleep(1.5)
        _retry_count = 0
        _start_jarvis()

    threading.Thread(target=_do, daemon=True, name="proc-restart").start()
    return jsonify({"ok": True})
