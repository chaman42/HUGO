"""Launcher Flask routes that don't mutate jarvis-process state directly:
static index, version/health/status reporting, force-reload, device-fingerprint
auth, and the launcher SocketIO connect handler. (The /api/start, /api/stop,
/api/restart routes live in process_manager.py instead — see its docstring
for why.) See core/api_routes_update.py for the long-running update/build_ios
routes.
"""
import json
import os
import subprocess
import time

from flask import jsonify, request

from core.launcher_app import _BASE_DIR, app, socketio, logger
from core.mic_permissions import _get_mic_status
from core import process_manager as pm

# ---------------------------------------------------------------------------
# Device authentication — fingerprint whitelist
# ---------------------------------------------------------------------------
# Load REGISTER_TOKEN from .env if python-dotenv is available.
try:
    from dotenv import load_dotenv as _dotenv_load
    _dotenv_load(os.path.join(_BASE_DIR, ".env"))
except ImportError:
    pass

# Path to the JSON file that stores allowed device fingerprints.
_ALLOWED_DEVICES_FILE = os.path.join(_BASE_DIR, "data", "allowed_devices.json")

# One-time token required to register new devices via /api/register_device.
# Set REGISTER_TOKEN in .env (or the process environment) before use.
_REGISTER_TOKEN: str = os.environ.get("REGISTER_TOKEN", "")


def _load_allowed_fingerprints() -> set[str]:
    """Load the set of allowed device fingerprints from disk.

    Supports two entry formats in the JSON array:
      - plain string:  "abc123…"
      - labelled dict: {"hash": "abc123…", "label": "MacBook Pro Joan"}

    Returns an empty set if the file is missing, empty, or malformed.
    """
    try:
        with open(_ALLOWED_DEVICES_FILE) as fh:
            data = json.load(fh)
        result: set[str] = set()
        for item in data.get("fingerprints", []):
            if isinstance(item, str):
                result.add(item)
            elif isinstance(item, dict) and "hash" in item:
                result.add(item["hash"])
        return result
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return set()


def _is_fingerprint_allowed(fp: str) -> bool:
    """Return True if fp is in the whitelist.

    Bootstrap rule: if NO devices are registered yet the whitelist is treated
    as open so the first user can register without a chicken-and-egg problem.
    Once at least one fingerprint is saved, only registered devices are allowed.
    """
    if not fp:
        return False
    registered = _load_allowed_fingerprints()
    if not registered:
        return True   # bootstrap: no devices registered yet — allow everyone
    return fp in registered


def _save_fingerprint(fp: str) -> bool:
    """Append fp to allowed_devices.json. Returns True on success."""
    try:
        registered = _load_allowed_fingerprints()
        registered.add(fp)
        os.makedirs(os.path.dirname(_ALLOWED_DEVICES_FILE), exist_ok=True)
        with open(_ALLOWED_DEVICES_FILE, "w") as fh:
            json.dump({"fingerprints": sorted(registered)}, fh, indent=2)
        return True
    except Exception as exc:
        logger.error("Failed to save device fingerprint: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/api/status")
def api_status():
    return jsonify({"running": pm._is_running()})


@app.route("/api/version")
def api_version():
    """Diagnostic endpoint — answers "which version of ui/index.html is
    ACTUALLY being served right now" directly from THIS process, the one
    responsible for serving it (see index() above), rather than inferring
    it from a separate service. Backs the build-hash display in Ajustes
    (see ui/index.html's own script). Two hashes, since they can
    legitimately differ:
      - repo_commit: this git checkout's current HEAD — what index() is
        ACTUALLY serving ui/index.html from, right now.
      - installed_shell_commit: the commit electron/.app_version recorded
        the last time rebuild_app.sh successfully rebuilt+installed
        /Applications/LIRA.app's Electron SHELL (main.js/preload.js only —
        confirmed via `asar extract`: ui/index.html and ui/sw.js are never
        bundled into the app at all, they're served live from this repo
        checkout, same as jarvis.py/launcher.py themselves). A stale
        mismatch here means the Electron wrapper itself is out of date,
        not the frontend content — a genuinely separate failure mode from
        the one this endpoint mainly exists to make visible.
    """
    def _git(*args):
        try:
            result = subprocess.run(
                ["git", *args], cwd=_BASE_DIR, capture_output=True, text=True, timeout=5,
            )
            return result.stdout.strip() or None
        except Exception:
            return None

    repo_commit      = _git("rev-parse", "--short", "HEAD")
    repo_commit_full = _git("rev-parse", "HEAD")
    repo_commit_date = _git("log", "-1", "--format=%ci")
    repo_dirty       = bool(_git("status", "--porcelain"))

    installed_shell_commit = None
    try:
        version_file = os.path.join(_BASE_DIR, "electron", ".app_version")
        with open(version_file, "r", encoding="utf-8") as f:
            installed_shell_commit = (f.read().strip() or None)
            if installed_shell_commit:
                installed_shell_commit = installed_shell_commit[:7]
    except (FileNotFoundError, OSError):
        pass

    return jsonify({
        "repo_commit":            repo_commit,
        "repo_commit_full":       repo_commit_full,
        "repo_commit_date":       repo_commit_date,
        "repo_dirty":             repo_dirty,
        "installed_shell_commit": installed_shell_commit,
    })


@app.route("/api/health")
def api_health():
    # Boot progress, stage 2/7 — the frontend's first successful /api/health
    # round-trip after connecting, i.e. "launcher.py responded". Guarded to
    # fire once per boot: this route is polled every ~2s throughout startup
    # AND indefinitely during normal operation (see startHealthPolling() in
    # ui/index.html), not just once.
    if not pm._health_progress_emitted:
        pm._health_progress_emitted = True
        pm.emit_boot_progress("launcher_responded", 25, "Launcher activo...")

    with pm._proc_lock:
        proc  = pm._jarvis_proc
        ready = pm._jarvis_ready
        start = pm._start_time

    pid     = proc.pid if (proc and proc.poll() is None) else None
    running = (pid is not None) or ready
    uptime  = round(time.time() - start, 1) if (start and running) else 0

    return jsonify({
        "launcher":         "ok",
        "jarvis_running":   running,
        "jarvis_ready":     ready,
        "jarvis_pid":       pid,
        "uptime":           uptime,
        "mic_status":       _get_mic_status(),
        "retry_count":      pm._retry_count,
        "max_retries":      pm._MAX_RETRIES,
        "pending_relaunch": pm._pending_relaunch,
    })


@app.route("/api/mic_status")
def api_mic_status():
    status = _get_mic_status()
    return jsonify({"mic_status": status})


@app.route("/api/reload", methods=["POST"])
def api_reload():
    """Force every connected HUD client to perform a hard page reload.

    Because the launcher socket (port 8079) is always running — even when
    jarvis.py is down — this is the most reliable way to push a reload to all
    open browser tabs after a frontend deployment (e.g. after bumping the
    Service Worker cache key in ui/sw.js).
    """
    socketio.emit("force_reload", {})
    logger.info("force_reload emitted to all connected HUD clients.")
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Device auth endpoints
# ---------------------------------------------------------------------------

@socketio.on("connect")
def _on_launcher_socket_connect():
    """Reject launcher SocketIO connections from unregistered device fingerprints.

    The fingerprint is passed as a query-string param ?fp=... by the frontend.
    Returning False from a Flask-SocketIO connect handler rejects the connection.
    """
    # AUTH TEMPORARILY DISABLED — allow all devices without fingerprint check.
    pass
    # fp = request.args.get("fp", "")
    # if not _is_fingerprint_allowed(fp):
    #     logger.warning(
    #         "Launcher SocketIO rejected: unregistered fingerprint %r from %s",
    #         (fp[:16] + "...") if len(fp) > 16 else fp,
    #         request.remote_addr,
    #     )
    #     return False   # reject connection

    # Boot progress, stage 1/7 — the frontend's `launcher` socket just
    # completed its handshake with this process. See emit_boot_progress().
    pm.emit_boot_progress("connecting_launcher", 10, "Conectando...")


@app.route("/api/auth")
def api_auth():
    """Check whether the requesting device fingerprint is registered.

    GET /api/auth?fingerprint=<hex-sha256>

    Response JSON:
      allowed    — True if access should be granted
      bootstrap  — True if no devices are registered yet (first-time setup)
      fingerprint — echoed back for convenience
    """
    fp         = request.args.get("fingerprint", "").strip()
    registered = _load_allowed_fingerprints()
    bootstrap  = len(registered) == 0          # no devices → first-time setup
    allowed    = bootstrap or (fp in registered)
    logger.info(
        "Auth check: allowed=%s bootstrap=%s fp=%r from %s",
        allowed, bootstrap,
        (fp[:16] + "...") if len(fp) > 16 else fp,
        request.remote_addr,
    )
    return jsonify({"allowed": allowed, "bootstrap": bootstrap, "fingerprint": fp})


@app.route("/api/register_device")
def api_register_device():
    """Add a device fingerprint to the whitelist.

    GET /api/register_device?fingerprint=<hex-sha256>&token=<REGISTER_TOKEN>

    The token must match REGISTER_TOKEN in .env.
    """
    fp    = request.args.get("fingerprint", "").strip()
    token = request.args.get("token", "").strip()

    if not fp:
        return jsonify({"ok": False, "error": "fingerprint required"}), 400
    if not _REGISTER_TOKEN:
        return jsonify({"ok": False, "error": "REGISTER_TOKEN not set in .env"}), 500
    if token != _REGISTER_TOKEN:
        logger.warning(
            "register_device: invalid token from %s", request.remote_addr
        )
        return jsonify({"ok": False, "error": "invalid token"}), 403

    if _save_fingerprint(fp):
        logger.info(
            "Device registered: fp=%r from %s",
            (fp[:16] + "...") if len(fp) > 16 else fp,
            request.remote_addr,
        )
        return jsonify({"ok": True, "fingerprint": fp, "message": "Device registered"})
    return jsonify({"ok": False, "error": "failed to save fingerprint"}), 500


@app.route("/api/my_fingerprint")
def api_my_fingerprint():
    """Debug endpoint — echo back the fingerprint passed as a query param.

    GET /api/my_fingerprint?fingerprint=<hex-sha256>

    Useful to confirm what fingerprint the browser computed and whether it is
    already registered, without modifying the whitelist.
    """
    fp         = request.args.get("fingerprint", "").strip()
    registered = fp in _load_allowed_fingerprints()
    return jsonify({"fingerprint": fp, "registered": registered})
