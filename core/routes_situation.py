"""Flask routes for Proactive Intelligence Phase 2 — GET /api/situation
(current snapshot), /api/situation/patterns, /api/situation/routines,
/api/situation/anomalies. See core/situation.py's own module docstring."""
import logging

from flask import jsonify

from core.server import app
from core.situation import situation_engine

logger = logging.getLogger(__name__)


@app.route("/api/situation")
def api_situation():
    try:
        return jsonify(situation_engine.get_current_situation())
    except Exception as exc:
        logger.error("Failed to load situation snapshot: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/situation/patterns")
def api_situation_patterns():
    try:
        data = situation_engine._load()
        return jsonify({"patterns": data.get("patterns", [])})
    except Exception as exc:
        logger.error("Failed to load situation patterns: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/situation/routines")
def api_situation_routines():
    try:
        data = situation_engine._load()
        return jsonify({"routines": data.get("routines", [])})
    except Exception as exc:
        logger.error("Failed to load situation routines: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/situation/anomalies")
def api_situation_anomalies():
    try:
        data = situation_engine._load()
        unresolved = [a for a in data.get("anomalies", []) if not a.get("resolved")]
        return jsonify({"anomalies": unresolved})
    except Exception as exc:
        logger.error("Failed to load situation anomalies: %s", exc)
        return jsonify({"error": str(exc)}), 500
