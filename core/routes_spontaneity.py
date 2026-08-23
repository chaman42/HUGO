"""Flask routes for Proactive Intelligence Phase 5 — GET /api/spontaneity/log
(history + outcome weights), GET /api/spontaneity/status (cooldown,
today_count, next_available), POST /api/spontaneity/outcome (Joan/UI
records a reaction), POST /api/spontaneity/disable (Joan pauses
spontaneity temporarily). See core/spontaneity.py's own module docstring."""
import logging

from flask import jsonify, request

from core.server import app
from core import spontaneity

logger = logging.getLogger(__name__)


@app.route("/api/spontaneity/log")
def api_spontaneity_log():
    try:
        log = spontaneity.get_log_snapshot()
        return jsonify({
            "history":        spontaneity.get_recent_history(limit=50),
            "outcome_weights": log.get("outcome_weights", {}),
        })
    except Exception as exc:
        logger.error("Failed to load spontaneity log: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/spontaneity/status")
def api_spontaneity_status():
    try:
        return jsonify(spontaneity.get_status())
    except Exception as exc:
        logger.error("Failed to load spontaneity status: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/spontaneity/outcome", methods=["POST"])
def api_spontaneity_outcome():
    try:
        body = request.get_json(silent=True) or {}
        candidate_id = body.get("candidate_id")
        reaction     = body.get("reaction")
        if not candidate_id or reaction not in ("appreciated", "neutral", "unwanted"):
            return jsonify({"ok": False, "error": "expected {candidate_id, reaction: appreciated|neutral|unwanted}"}), 400
        spontaneity.spontaneity_engine.record_outcome(candidate_id, reaction)
        return jsonify({"ok": True})
    except Exception as exc:
        logger.error("Failed to record spontaneity outcome: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/spontaneity/disable", methods=["POST"])
def api_spontaneity_disable():
    try:
        body = request.get_json(silent=True) or {}
        hours = float(body.get("hours", 24.0))
        updated = spontaneity.disable_temporarily(hours=hours)
        return jsonify({"ok": True, "manually_disabled_until": updated.get("manually_disabled_until")})
    except Exception as exc:
        logger.error("Failed to disable spontaneity: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500
