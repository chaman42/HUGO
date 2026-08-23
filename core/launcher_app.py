"""Shared Flask/SocketIO app instance + logging setup for the launcher
process. Imported by process_manager.py, api_routes.py, api_routes_update.py,
and launcher.py itself — kept in its own module (rather than living in
launcher.py) so those siblings can import `app`/`socketio` without a circular
import back to launcher.py (which is always executed as `__main__`, never
importable as a module named `launcher`).
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from flask import Flask
from flask_socketio import SocketIO

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_BASE_DIR = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
_PYTHON   = sys.executable
_LOGS_DIR = os.path.join(_BASE_DIR, "logs")
os.makedirs(_LOGS_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Logging — file + console, both timestamped
# ---------------------------------------------------------------------------
logging.getLogger("werkzeug").setLevel(logging.ERROR)
logging.getLogger("engineio").setLevel(logging.ERROR)
logging.getLogger("socketio").setLevel(logging.ERROR)

_LOG_FMT = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger("launcher")
logger.setLevel(logging.DEBUG)

_fh = RotatingFileHandler(
    os.path.join(_LOGS_DIR, "launcher.log"),
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
)
_fh.setFormatter(_LOG_FMT)
logger.addHandler(_fh)

_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(_LOG_FMT)
logger.addHandler(_ch)

# ---------------------------------------------------------------------------
# Flask / SocketIO
# ---------------------------------------------------------------------------
app = Flask(
    __name__,
    static_folder=os.path.join(_BASE_DIR, "ui"),
    static_url_path="",
)
app.config["SECRET_KEY"] = "jarvis-launcher-secret"

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
    # Prevent WebSocket upgrade attempts — Werkzeug's dev server doesn't support
    # the WebSocket protocol and raises "write() before start_response" on every
    # upgrade handshake. Long-polling is identical in practice for a local app.
    allow_upgrades=False,
    logger=False,
    engineio_logger=False,
)
