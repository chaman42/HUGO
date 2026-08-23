"""Flask routes for Armor Bay's card grid + editable status —
GET /api/armor (all models, replaces the frontend's old hardcoded
ARMOR_DATA duplicate) and POST /api/armor/<model_id>/status (Joan sets a
model's build status from the detail view's status picker). See
core/armor_manager.py's own module docstring for the fuller history."""
import logging

from flask import jsonify, request

from core.server import app
from core import armor_manager
from core import armor_light

logger = logging.getLogger(__name__)


@app.route("/api/armor")
def api_armor_get():
    try:
        return jsonify({"models": armor_manager.get_all_models()})
    except Exception as exc:
        logger.error("Failed to load armor models: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/armor/<model_id>/status", methods=["POST"])
def api_armor_set_status(model_id):
    try:
        body = request.get_json(silent=True) or {}
        status = body.get("status")
        if not isinstance(status, str) or status not in armor_manager.VALID_STATUSES:
            return jsonify({
                "ok": False,
                "error": f"status must be one of {sorted(armor_manager.VALID_STATUSES)}",
            }), 400

        ok = armor_manager.set_model_status(model_id, status)
        if not ok:
            return jsonify({"ok": False, "error": "not found"}), 404

        model = next((m for m in armor_manager.get_all_models() if m.get("id") == model_id), None)
        return jsonify({"ok": True, "model": model})
    except Exception as exc:
        logger.error("Failed to update armor status: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/armor/model-8/light", methods=["POST"])
def api_armor_model8_light():
    try:
        body = request.get_json(silent=True) or {}
        state = body.get("state")
        if state not in ("on", "off", "baliza"):
            return jsonify({"ok": False, "error": "state must be 'on', 'off', or 'baliza'"}), 400

        if state == "baliza":
            armor_light.set_baliza()
        else:
            armor_light.set_light(state == "on")
        return jsonify({"ok": True, "state": state})
    except (RuntimeError, TimeoutError) as exc:
        logger.error("Modelo 8 light: board not reachable over BLE: %s", exc)
        return jsonify({"ok": False, "error": "board not connected"}), 503
    except Exception as exc:
        logger.error("Failed to set Modelo 8 light: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500
