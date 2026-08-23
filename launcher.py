"""
launcher.py — JarvisLite process controller entry point.

Normally spawned as a child process of the HUGO Electron app (electron/main.js
startLauncher()), which already runs in a proper Aqua/WindowServer session —
no Terminal.app window is ever opened. Can also be run standalone for
development:
    python launcher.py

Serves on http://localhost:8079
- Hosts the full frontend (ui/)
- Manages the jarvis.py process lifecycle (see core/process_manager.py) —
  spawned directly via subprocess.Popen, stdout/stderr piped straight into
  logs/activity.log
- Health check: GET /api/health
- Mic status:   GET /api/mic_status  (permission is actively requested via
  AVFoundation/PyObjC when not yet determined — see core/mic_permissions.py)
- Auto-restarts jarvis.py on crash (max 5 retries, then 30s cooldown)
- Kills any other launcher.py/jarvis.py instances on its own startup, so a
  fresh launch never fails with "port already in use"
- Logs everything to logs/launcher.log

The Flask app, SocketIO instance, and logger are defined in
core/launcher_app.py (to avoid a circular import back to this always-`__main__`
script); the actual routes are registered as side effects of importing
core/process_manager.py, core/api_routes.py, and core/api_routes_update.py
below.
"""
import os
import threading

from core.launcher_app import app, socketio, logger
from core.mic_permissions import _get_mic_status
from core.port_cleanup import _free_port, _kill_stale_jarvis, _kill_stale_launcher
from core import process_manager as pm
from core import api_routes            # noqa: F401 — registers routes on import
from core import api_routes_update      # noqa: F401 — registers routes on import

if __name__ == "__main__":
    # No-orphan-processes guarantee: kill any other launcher.py or jarvis.py
    # still running before we touch anything else. This is what prevents the
    # classic "port already in use" error on a fresh launch — whether the
    # previous instance is a genuine orphan (crashed session) or this is a
    # deliberate fresh start while an old one is still alive.
    _kill_stale_launcher()
    _kill_stale_jarvis()
    _free_port(8079)
    _free_port(pm._JARVIS_PORT)

    logger.info("=" * 60)
    logger.info("JarvisLite Launcher starting — http://localhost:8079")
    logger.info("  PID=%d", os.getpid())
    logger.info("  mic_status=%s", _get_mic_status())
    logger.info("=" * 60)

    # jarvis.py normally starts via electron/main.js's autoStartJarvis(),
    # which POSTs /api/start the moment this launcher reports healthy — no
    # user interaction on a normal launch. HUGO_AUTOSTART=1 is a separate,
    # slightly earlier path: set by electron/main.js only on the relaunch it
    # performs right after a successful "Actualizar HUGO" (see api_update()
    # and its _pending_relaunch flag above), for that single launch only —
    # it shaves the extra health-check round trip off that specific case.
    # Redundant with the POST in practice (api_start() is idempotent) but
    # harmless to keep. Started in a background thread, before socketio.run()
    # below starts blocking, so the health endpoint is up immediately
    # regardless of how long jarvis.py takes to boot — main.js's own
    # boot-timeout logic doesn't need to know anything changed.
    if os.environ.get("HUGO_AUTOSTART") == "1":
        logger.info("HUGO_AUTOSTART=1 — starting jarvis.py automatically (post-update relaunch).")
        threading.Thread(target=pm._start_jarvis, daemon=True, name="autostart").start()

    socketio.run(
        app,
        host="0.0.0.0",
        port=8079,
        use_reloader=False,
        allow_unsafe_werkzeug=True,
        log_output=False,
    )
