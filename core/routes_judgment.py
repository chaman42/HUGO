"""Flask routes for Proactive Intelligence Phase 3 — GET /api/judgment/log
(last 50 decisions), GET /api/judgment/permissions (current config), POST
/api/judgment/permissions (Joan updates joan_state_thresholds only — see
core.judgment.update_thresholds's own docstring for why the three
action-category lists aren't editable here). See core/judgment.py's own
module docstring."""
import logging

from flask import jsonify, request

from core.server import app
from core import judgment

logger = logging.getLogger(__name__)


@app.route("/api/judgment/log")
def api_judgment_log():
    try:
        return jsonify({"decisions": judgment.get_recent_decisions(limit=50)})
    except Exception as exc:
        logger.error("Failed to load judgment log: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/judgment/permissions", methods=["GET"])
def api_judgment_permissions_get():
    try:
        return jsonify(judgment.get_permissions_snapshot())
    except Exception as exc:
        logger.error("Failed to load judgment permissions: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/judgment/permissions", methods=["POST"])
def api_judgment_permissions_post():
    try:
        body = request.get_json(silent=True) or {}
        thresholds = body.get("joan_state_thresholds")
        if not isinstance(thresholds, dict):
            return jsonify({"ok": False, "error": "expected {\"joan_state_thresholds\": {...}}"}), 400
        updated = judgment.update_thresholds(thresholds)
        return jsonify({"ok": True, "permissions": updated})
    except Exception as exc:
        logger.error("Failed to update judgment permissions: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500
