"""Flask routes backing Ajustes' "Claves API" panel — lets whoever's
identified paste an API key for each provider they own
(core.api_key_store.KEY_OWNERS), including Dani's own isolated Groq/Serper
keys (see core/active_person.py), without hand-editing .env.

Per-person scoped (2026-08-24 rework), NOT Joan-only: Dani gets his own
fully separate HUGO install (his own Mac, his own .env/data/), and the
identity system already defaults an unrecognized device to "dani" — so on
HIS OWN machine, Dani is who gets identified, day one. A blanket Joan-only
gate here would lock him out of his own Ajustes panel entirely. Instead,
core.api_key_store.keys_for(person_id) scopes both GET and POST to exactly
the keys that person owns — Joan's keys don't exist to Dani, and
symmetrically Dani's don't exist to Joan either (his explicit request:
"api keys that only me use, simply do not exist to him").

A key is validated with a real provider call (core.api_key_validation)
before it's ever persisted or applied — see api_set_api_key() below."""
import logging

from flask import jsonify, request

from core.server import app
from core import api_key_store
from core import api_key_validation
from core import social as social_mod

logger = logging.getLogger(__name__)


def _current_person_id() -> str | None:
    """Same who_is_present()-based identity resolution used throughout the
    app (core.commands, core.routes_social, core.routes_sleep, ...)."""
    try:
        present = social_mod.social_engine.who_is_present()
        return present[0].id if present else None
    except Exception:
        logger.debug("api_keys identity lookup failed (non-critical)", exc_info=True)
        return None


@app.route("/api/api_keys/lock_status")
def api_key_lock_status():
    """Whether the CURRENT identified person is locked out of HUGO's real
    functionality (core.api_key_store.is_person_locked — only ever true
    for Dani, and only until both his keys are set+validated). Polled by
    ui/js/onboarding-intro.js at boot and after every Ajustes key save to
    drive the Main+Ajustes-only nav restriction and detect the moment he
    unlocks (triggering the second "rest of the system" sequence)."""
    person_id = _current_person_id()
    return jsonify({"locked": api_key_store.is_person_locked(person_id), "person_id": person_id})


@app.route("/api/api_keys", methods=["GET"])
def api_get_api_keys():
    """{key: bool} for only the keys the CURRENT identified person owns —
    whether each is currently set, from .env or a saved override. Never
    the value itself; see api_key_store.get_status()'s own docstring."""
    allowed = api_key_store.keys_for(_current_person_id())
    status  = api_key_store.get_status()
    return jsonify({k: status[k] for k in allowed})


@app.route("/api/api_keys", methods=["POST"])
def api_set_api_key():
    """Body: {"key": "GROQ_API_KEY_DANI", "value": "gsk_..."}. Rejects a
    key the current identified person doesn't own (403 — out of scope, not
    malformed). A non-empty value is validated with a real provider call
    (core.api_key_validation.VALIDATORS) before being persisted; an empty
    value clears the override (restoring .env's own value, if any) and
    skips validation entirely, same as before this existed."""
    data  = request.get_json(silent=True) or {}
    key   = data.get("key")
    value = data.get("value", "")

    allowed = api_key_store.keys_for(_current_person_id())
    if key not in allowed:
        return jsonify({"ok": False, "error": "unknown key"}), 403

    if value.strip():
        validator = api_key_validation.VALIDATORS.get(key)
        if validator:
            ok, err = validator(value.strip())
            if not ok:
                return jsonify({"ok": False, "error": err}), 400

    try:
        api_key_store.set_key(key, value)
    except Exception as exc:
        logger.error("Failed to save API key %s: %s", key, exc, exc_info=True)
        return jsonify({"ok": False, "error": str(exc)}), 500

    status = api_key_store.get_status()
    return jsonify({"ok": True, "status": {k: status[k] for k in allowed}})
