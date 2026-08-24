"""Flask routes backing Ajustes' "Claves API" panel — lets Joan paste an
API key for each provider (core.api_key_store.MANAGED_KEYS), including
Dani's own isolated Groq/Serper keys (see core/active_person.py), without
hand-editing .env. Joan-only: this is credential management, no different
in spirit from core.routes_social's Personas tab — Dani never sees it, and
his own keys are set BY Joan here, not by Dani himself.

Imported after core.routes_social in core/server.py specifically so
_joan_only can be reused directly instead of a third copy of it (see
core.routes_sleep's own docstring for why THAT module duplicates it
instead — a different, earlier import-order constraint that doesn't apply
here)."""
import logging

from flask import jsonify, request

from core.server import app
from core import api_key_store
from core.routes_social import _joan_only

logger = logging.getLogger(__name__)


@app.route("/api/api_keys", methods=["GET"])
@_joan_only
def api_get_api_keys():
    """{key: bool} for every managed key — whether it's currently set, from
    .env or a saved override. Never the value itself; see
    api_key_store.get_status()'s own docstring."""
    return jsonify(api_key_store.get_status())


@app.route("/api/api_keys", methods=["POST"])
@_joan_only
def api_set_api_key():
    """Body: {"key": "GROQ_API_KEY_DANI", "value": "gsk_..."}. An empty
    `value` clears the override (restoring .env's own value, if any — see
    api_key_store.set_key's own docstring)."""
    data = request.get_json(silent=True) or {}
    key   = data.get("key")
    value = data.get("value", "")
    if key not in api_key_store.MANAGED_KEYS:
        return jsonify({"ok": False, "error": "unknown key"}), 400
    try:
        api_key_store.set_key(key, value)
    except Exception as exc:
        logger.error("Failed to save API key %s: %s", key, exc, exc_info=True)
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "status": api_key_store.get_status()})
