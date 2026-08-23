"""Flask routes for Proactive Intelligence Phase 4 — GET /api/initiative/queue
(pending suggestions), GET /api/initiative/log (recent initiative decisions),
POST /api/initiative/scan (Joan-triggered manual scan), GET
/api/action-engine/log (recent action results). See core/initiative.py and
core/action_engine.py's own module docstrings."""
import logging

from flask import jsonify

from core.server import app
from core import initiative
from core import action_engine as action_engine_mod

logger = logging.getLogger(__name__)


@app.route("/api/initiative/queue")
def api_initiative_queue():
    try:
        return jsonify({"queue": initiative.get_queue()})
    except Exception as exc:
        logger.error("Failed to load initiative queue: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/initiative/log")
def api_initiative_log():
    try:
        return jsonify({"decisions": initiative.get_recent_initiative_log(limit=50)})
    except Exception as exc:
        logger.error("Failed to load initiative log: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/initiative/scan", methods=["POST"])
def api_initiative_scan():
    """Joan-requested manual scan — runs synchronously (not on a
    background thread like the 30-min tick / conversation-start trigger)
    since this is an explicit request Joan is waiting on a response for,
    same reasoning as core/routes_sleep.py's manual-trigger endpoint."""
    try:
        result = initiative.run_proactive_cycle()
        return jsonify({"ok": True, **result})
    except Exception as exc:
        logger.error("Manual initiative scan failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/action-engine/log")
def api_action_engine_log():
    try:
        return jsonify({"results": action_engine_mod.get_recent_results(limit=50)})
    except Exception as exc:
        logger.error("Failed to load action engine log: %s", exc)
        return jsonify({"error": str(exc)}), 500
