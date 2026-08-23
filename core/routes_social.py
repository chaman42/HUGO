"""Flask routes for Proactive Intelligence Phase 6 — GET /api/social/present
(who's currently detected), GET /api/social/people (all known people +
trust levels), POST /api/social/people (Joan manually registers someone),
POST /api/social/people/<id>/trust (Joan updates trust — the only place
trust_level ever changes, never auto-elevated by LIRA; also marks the
person trust_confirmed), GET /api/social/people/<id> (person detail +
interaction history), PATCH /api/social/people/<id> (edit name/
relationship_to_joan/knows_lira), DELETE /api/social/people/<id> (forget
person). Backs the Núcleo LIRA "Personas" tab. See core/social.py's own
module docstring."""
import logging
from dataclasses import asdict

from flask import jsonify, request

from core.server import app
from core import social

logger = logging.getLogger(__name__)


@app.route("/api/social/present")
def api_social_present():
    try:
        present = social.social_engine.who_is_present()
        return jsonify({"present": [asdict(p) for p in present]})
    except Exception as exc:
        logger.error("Failed to load social presence: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/social/people")
def api_social_people():
    try:
        people = social.get_all_people()
        return jsonify({"people": [asdict(p) for p in people]})
    except Exception as exc:
        logger.error("Failed to load social people: %s", exc)
        return jsonify({"error": str(exc)}), 500


_VALID_RELATIONSHIPS = {"friend", "family", "colleague", "stranger"}   # 'self' is reserved for Joan's own record


@app.route("/api/social/people", methods=["POST"])
def api_social_person_create():
    """Joan manually registering someone LIRA hasn't (yet) identified on
    her own — the Personas tab's 'Añadir persona'. Created-by-Joan means
    reviewed-by-Joan by definition, so trust_confirmed starts True (see
    Person.trust_confirmed's own docstring in core/social.py) — unlike a
    person LIRA creates herself (currently only via the Discord branch of
    _match_context), which starts False until Joan looks at it here."""
    try:
        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()
        if not name:
            return jsonify({"ok": False, "error": "expected non-empty {name}"}), 400

        relationship_to_joan = body.get("relationship_to_joan") or "stranger"
        if relationship_to_joan not in _VALID_RELATIONSHIPS:
            return jsonify({"ok": False, "error": f"relationship_to_joan must be one of {sorted(_VALID_RELATIONSHIPS)}"}), 400

        trust = body.get("trust_level", 0.15)
        if not isinstance(trust, (int, float)) or isinstance(trust, bool):
            return jsonify({"ok": False, "error": "trust_level must be a number"}), 400
        trust = max(0.0, min(1.0, float(trust)))

        knows_lira = bool(body.get("knows_lira", False))
        now = social._now_iso()

        with social._lock:
            data = social._load()
            person_id = social._next_person_id_locked(data)
            data["people"][person_id] = {
                "id": person_id, "name": name, "relationship_to_joan": relationship_to_joan,
                "trust_level": trust, "knows_lira": knows_lira, "trust_confirmed": True,
                "voice_profile_id": None, "linguistic_profile": {},
                "first_seen": now, "last_seen": now, "interaction_count": 0,
                "discord_id": None,
                "relationship": {"type": relationship_to_joan, "closeness": 0.0, "joan_sentiment": "unknown", "shared_topics": [], "notes": []},
                "interactions": [],
            }
            social._save_locked(data)
        return jsonify({"ok": True, "person": data["people"][person_id]})
    except Exception as exc:
        logger.error("Failed to create person: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/social/people/<person_id>")
def api_social_person_detail(person_id):
    try:
        data = social._load()
        record = data["people"].get(person_id)
        if record is None:
            return jsonify({"error": "not found"}), 404
        relationship = social.social_engine.get_relationship(person_id)
        return jsonify({
            "person":        record,
            "relationship":  {
                "type": relationship.type, "closeness": relationship.closeness,
                "joan_sentiment": relationship.joan_sentiment,
                "shared_topics": relationship.shared_topics, "notes": relationship.notes,
            },
            "interactions":  record.get("interactions", []),
        })
    except Exception as exc:
        logger.error("Failed to load person detail: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/social/people/<person_id>/trust", methods=["POST"])
def api_social_person_trust(person_id):
    """Joan-only, explicit action — the ONLY place a trust_level ever
    changes (spec: 'Trust level is set by Joan — never auto-elevated by
    LIRA'). No caller-identity check here beyond this being a
    Joan-facing HUD/launcher API route (same trust boundary as every other
    /api/* route in this app, none of which are exposed to Discord or
    other untrusted callers). Joan's own trust_level is fixed at 1.0 —
    'self' isn't a relationship whose trust could meaningfully vary, same
    reasoning as the DELETE route below refusing to remove her profile."""
    if person_id == "joan":
        return jsonify({"ok": False, "error": "Joan's own trust level is fixed at 1.0"}), 400
    try:
        body = request.get_json(silent=True) or {}
        trust = body.get("trust_level")
        if trust is None or not isinstance(trust, (int, float)) or isinstance(trust, bool):
            return jsonify({"ok": False, "error": "expected {trust_level: 0.0-1.0}"}), 400
        trust = max(0.0, min(1.0, float(trust)))

        data = social._load()
        record = data["people"].get(person_id)
        if record is None:
            return jsonify({"ok": False, "error": "not found"}), 404
        with social._lock:
            data = social._load()
            data["people"][person_id]["trust_level"] = trust
            # This route being called at all IS Joan explicitly reviewing
            # this person — see Person.trust_confirmed's own docstring in
            # core/social.py for why this can't just be inferred from
            # trust_level being nonzero (system defaults, e.g. Discord's
            # authorized-'user' role, already set a nonzero value Joan
            # never actually looked at).
            data["people"][person_id]["trust_confirmed"] = True
            social._save_locked(data)
        return jsonify({"ok": True, "person_id": person_id, "trust_level": trust, "trust_confirmed": True})
    except Exception as exc:
        logger.error("Failed to update trust level: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/social/people/<person_id>", methods=["PATCH"])
def api_social_person_edit(person_id):
    """Joan-only — edits the fields the trust route (above) deliberately
    doesn't touch: name, relationship_to_joan, knows_lira. Refuses 'joan'
    entirely, same as the trust/delete routes — her record stays exactly
    as seeded (relationship_to_joan='self' isn't a value this route would
    ever accept anyway, see _VALID_RELATIONSHIPS above). Every field is
    optional in the body; only the ones present are changed."""
    if person_id == "joan":
        return jsonify({"ok": False, "error": "cannot edit Joan's own profile"}), 400
    try:
        body = request.get_json(silent=True) or {}
        updates = {}
        if "name" in body:
            name = (body.get("name") or "").strip()
            if not name:
                return jsonify({"ok": False, "error": "name cannot be empty"}), 400
            updates["name"] = name
        if "relationship_to_joan" in body:
            rel = body.get("relationship_to_joan")
            if rel not in _VALID_RELATIONSHIPS:
                return jsonify({"ok": False, "error": f"relationship_to_joan must be one of {sorted(_VALID_RELATIONSHIPS)}"}), 400
            updates["relationship_to_joan"] = rel
        if "knows_lira" in body:
            updates["knows_lira"] = bool(body.get("knows_lira"))
        if not updates:
            return jsonify({"ok": False, "error": "no editable fields provided"}), 400

        with social._lock:
            data = social._load()
            record = data["people"].get(person_id)
            if record is None:
                return jsonify({"ok": False, "error": "not found"}), 404
            record.update(updates)
            social._save_locked(data)
        return jsonify({"ok": True, "person": data["people"][person_id]})
    except Exception as exc:
        logger.error("Failed to edit person: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/social/people/<person_id>", methods=["DELETE"])
def api_social_person_delete(person_id):
    """Joan-only — 'forget person'. 'joan' herself can never be deleted
    through this route."""
    if person_id == "joan":
        return jsonify({"ok": False, "error": "cannot delete Joan's own profile"}), 400
    try:
        with social._lock:
            data = social._load()
            if person_id not in data["people"]:
                return jsonify({"ok": False, "error": "not found"}), 404
            del data["people"][person_id]
            social._save_locked(data)
        return jsonify({"ok": True})
    except Exception as exc:
        logger.error("Failed to delete person: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500
