"""Flask routes for the Armor Design Studio (Diseño → Armaduras, Phase 1) —
GET/POST /api/designs (list/save session records) and POST /api/designs/chat
(one turn of the workspace's left-panel conversation with LIRA). Same
"own routes module, imported once for its side effect in core/server.py"
pattern as core/estudio_routes.py — see that module's own header comment.

data/designs.json is a plain JSON array, each entry shaped:
    {
        "id": str, "name": str, "created_at": str (ISO), "updated_at": str (ISO),
        "status": "en_progreso" | "guardado",
        "zones": {
            "helmet" | "shoulders" | "chest" | "arms" | "waist" | "legs" | "boots": {
                "material": str, "mechanism": str, "aesthetic_notes": str,
                "notes": str,
                "status": "diseñado" | "pendiente" | "descartado",
                "lira_contribution": bool,
                "reasoning": str, "locked": bool,
            }, ...
        },
        "notes": str,
        "lira_suggestions": [{"zone": str, "text": str, "at": str (ISO)}],
        "conversation": [{"role": "user" | "lira", "text": str, "zone": str | None}],
    }
POST /api/designs upserts by "id" (a new id creates a new record — see
_new_design_skeleton()). The frontend (ui/js/design-studio.js) autosaves
every 30s and on every explicit "Guardar en Estudio" click.
"""
import datetime
import json
import logging
import os
import uuid

from flask import jsonify, request

from core.server import app
from core import commands
from core import ollama_control

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DESIGNS_PATH = os.path.join(_REPO_ROOT, "data", "designs.json")

_ZONE_KEYS = ["helmet", "shoulders", "chest", "arms", "waist", "legs", "boots"]


def _load_designs() -> list[dict]:
    try:
        with open(_DESIGNS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _save_designs(designs: list[dict]) -> bool:
    try:
        os.makedirs(os.path.dirname(_DESIGNS_PATH), exist_ok=True)
        with open(_DESIGNS_PATH, "w", encoding="utf-8") as f:
            json.dump(designs, f, ensure_ascii=False, indent=2)
        return True
    except OSError:
        logger.warning("Failed to write designs.json", exc_info=True)
        return False


def _empty_zone() -> dict:
    return {
        "material": "", "mechanism": "", "aesthetic_notes": "", "notes": "",
        "status": "pendiente", "lira_contribution": False,
        # Phase 3 (autopilot) — "reasoning" is LIRA's stated explanation for
        # an autopiloted decision (empty for manually-designed zones);
        # "locked" is set once Joan hits APROBAR in autopilot's review mode.
        "reasoning": "", "locked": False,
    }


def _new_design_skeleton() -> dict:
    now = datetime.datetime.now().isoformat()
    return {
        "id": uuid.uuid4().hex[:12],
        "name": "",
        "created_at": now,
        "updated_at": now,
        "status": "en_progreso",
        "zones": {z: _empty_zone() for z in _ZONE_KEYS},
        "notes": "",
        "lira_suggestions": [],
        "conversation": [],
        # Conceptuales integration — the "ts" of the data/concepts.json
        # entry this design is linked to (None until the first "GUARDAR EN
        # ESTUDIO" — see ui/js/design-studio.js's
        # _dsSaveDesignToConceptuales()). One-directional back-reference:
        # the concept itself holds the authoritative "design_id" forward
        # link; this field just lets re-opening a design (from anywhere,
        # not only via its concept card) resolve which concept to keep in
        # sync on the next save, without the frontend needing to remember
        # that across navigation.
        "concept_ts": None,
    }


def _emit_estudio_updated(section: str) -> None:
    try:
        import core.server as server_mod
        server_mod.socketio.emit("estudio_updated", {"section": section})
    except Exception:
        logger.debug("estudio_updated emit failed (non-critical)", exc_info=True)


@app.route("/api/designs", methods=["GET"])
def api_designs_list():
    """Lists all design sessions — the 'NUEVO DISEÑO / continue an existing
    one' picker in ui/js/design-studio.js reads this on entering Diseño →
    Armaduras. Sorted most-recently-updated first."""
    try:
        designs = _load_designs()
        designs.sort(key=lambda d: d.get("updated_at", ""), reverse=True)
        return jsonify({"designs": designs})
    except Exception as exc:
        logger.error("Failed to load designs: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/designs", methods=["POST", "OPTIONS"])
def api_designs_save():
    """Create (no "id" in body) or update (matching "id") a design session.
    Body is the design record itself, or a subset of it for a new session
    (missing zones/etc. are filled in via _new_design_skeleton()). Returns
    the full saved record, including its "id" — the frontend needs that
    back for a brand-new design so its next autosave targets the same
    record instead of creating a duplicate."""
    if request.method == "OPTIONS":
        return "", 204
    body = request.get_json(force=True, silent=True) or {}

    designs = _load_designs()
    design_id = body.get("id")
    existing = next((d for d in designs if d.get("id") == design_id), None) if design_id else None

    if existing is None:
        record = _new_design_skeleton()
    else:
        record = existing
        designs = [d for d in designs if d.get("id") != design_id]

    for key in ("name", "status", "notes", "zones", "lira_suggestions", "conversation", "concept_ts"):
        if key in body:
            record[key] = body[key]
    record["updated_at"] = datetime.datetime.now().isoformat()

    designs.append(record)
    if not _save_designs(designs):
        return jsonify({"error": "failed to save"}), 500

    _emit_estudio_updated("diseno_armaduras")
    return jsonify({"design": record})


@app.route("/api/designs/<design_id>", methods=["DELETE"])
def api_designs_delete(design_id):
    designs = _load_designs()
    remaining = [d for d in designs if d.get("id") != design_id]
    if len(remaining) == len(designs):
        return jsonify({"error": "not found"}), 404
    if not _save_designs(remaining):
        return jsonify({"error": "failed to save"}), 500
    return jsonify({"ok": True})


@app.route("/api/designs/chat", methods=["POST", "OPTIONS"])
def api_designs_chat():
    """One turn of the workspace's left-panel conversation. Body:
    {"zone": str, "message": str, "design": dict}. `design` is the full
    current record as held by the frontend (not re-read from disk — the
    frontend's in-memory copy may have unsaved edits from this same
    session, e.g. the last few chat turns not yet autosaved) — see
    core.commands.handle_design_mode for what it does with it.

    Returns {"reply": str, "suggestion": dict | None}. This endpoint does
    NOT persist anything itself — the frontend appends the turn (and any
    accepted suggestion) to its in-memory design and lets the normal
    autosave/"Guardar en Estudio" flow write it, same as every other design
    edit."""
    if request.method == "OPTIONS":
        return "", 204
    body = request.get_json(force=True, silent=True) or {}
    zone = body.get("zone", "")
    message = (body.get("message") or "").strip()
    design = body.get("design") or {}

    if not message:
        return jsonify({"error": "message is required"}), 400

    history = list(design.get("conversation") or [])
    history.append({"role": "user", "text": message, "zone": zone})

    try:
        result = commands.handle_design_mode(zone, design, history)
    except Exception as exc:
        logger.error("handle_design_mode failed: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500

    return jsonify(result)


@app.route("/api/designs/name-suggestions", methods=["POST", "OPTIONS"])
def api_designs_name_suggestions():
    """3 name candidates for the 'Guardar en Estudio' flow when the name
    field is left empty — see core.commands.generate_design_name_suggestions.
    Body: the design record (or at least its "zones"/"notes")."""
    if request.method == "OPTIONS":
        return "", 204
    body = request.get_json(force=True, silent=True) or {}
    try:
        names = commands.generate_design_name_suggestions(body)
    except Exception as exc:
        logger.error("generate_design_name_suggestions failed: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500
    return jsonify({"names": names})


# ---------------------------------------------------------------------------
# Phase 2 — interactive zone-by-zone design.
# ---------------------------------------------------------------------------

@app.route("/api/designs/zone-suggestions", methods=["POST", "OPTIONS"])
def api_designs_zone_suggestions():
    """'Pedir sugerencia a LIRA' AND 'Diseñar con LIRA' in the zone detail
    panel both hit this — same 3-distinct-options generation either way
    (Phase 2.5). Body: {"zone": str, "design": dict, "feedback": str?}.
    `feedback` is only sent when Joan discarded all 3 of a previous batch
    and described what to change instead (see core.commands.
    generate_zone_suggestions). Does not persist anything — the frontend
    applies whichever option Joan picks (or none) and saves through the
    normal /api/designs flow, same as the chat suggestion accept path."""
    if request.method == "OPTIONS":
        return "", 204
    body = request.get_json(force=True, silent=True) or {}
    zone = body.get("zone", "")
    design = body.get("design") or {}
    feedback = body.get("feedback")
    if not zone:
        return jsonify({"error": "zone is required"}), 400
    try:
        options = commands.generate_zone_suggestions(zone, design, feedback=feedback)
    except Exception as exc:
        logger.error("generate_zone_suggestions failed: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500
    return jsonify({"options": options})


@app.route("/api/designs/consistency-check", methods=["POST", "OPTIONS"])
def api_designs_consistency_check():
    """On-demand ('Revisar consistencia' button) or auto-triggered (right
    after a zone suggestion is accepted) cross-zone check. Body:
    {"design": dict}. Returns {"flag": str} — "" when LIRA has nothing to
    say, per core.commands.check_design_consistency's own fail-quiet
    convention (never manufactures a complaint)."""
    if request.method == "OPTIONS":
        return "", 204
    body = request.get_json(force=True, silent=True) or {}
    design = body.get("design") or {}
    try:
        flag = commands.check_design_consistency(design)
    except Exception as exc:
        logger.error("check_design_consistency failed: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500
    return jsonify({"flag": flag})


@app.route("/api/designs/summary", methods=["POST", "OPTIONS"])
def api_designs_summary():
    """'Generar resumen y guardar en Estudio' — shown once all 7 zones are
    'diseñado' (DISEÑO COMPLETO). Body: {"design": dict}. Generates the
    document via core.commands.generate_design_summary and appends it
    straight to data/summaries.json (ESTUDIO → RESÚMENES) — a direct save,
    not the Level-3 propose/confirm flow generate_summary() uses for voice
    commands, since this is an explicit button click in the workspace UI,
    already an unambiguous request."""
    if request.method == "OPTIONS":
        return "", 204
    body = request.get_json(force=True, silent=True) or {}
    design = body.get("design") or {}
    try:
        record = commands.generate_design_summary(design)
        commands._append_json_record(commands._SUMMARIES_PATH, record)
    except Exception as exc:
        logger.error("generate_design_summary failed: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500

    _emit_estudio_updated("resumenes")
    return jsonify({"record": record})


# ---------------------------------------------------------------------------
# Phase 2.5 — parts drawer (cajón de piezas). data/parts_library.json is a
# plain JSON array, each entry shaped:
#     {
#         "id": str, "zone": one of _ZONE_KEYS, "name": str, "description": str,
#         "material": str, "mechanism": str,
#         "source": "historical" | "lira" | "session",
#         "source_model": str | None,   # e.g. "Modelo VI" (historical), a
#                                        # design's own name (session), or
#                                        # None (lira-documented, no prior model)
#         "created_at": str (ISO),
#     }
# Seeded with parts extracted from armor_knowledge.json (source: "historical").
# Grows via two paths: 'AÑADIR PIEZA' in the drawer (mode "describe" below,
# LIRA turns Joan's free text into a structured part) and silently whenever
# a zone is marked 'diseñado' in the workspace (mode "manual" below, the
# frontend saves that zone's own material/mechanism/description straight
# as a reusable part, tagged source "session" with the design's name).
# ---------------------------------------------------------------------------

_PARTS_LIBRARY_PATH = os.path.join(_REPO_ROOT, "data", "parts_library.json")


def _load_parts() -> list[dict]:
    try:
        with open(_PARTS_LIBRARY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _save_parts(parts: list[dict]) -> bool:
    try:
        os.makedirs(os.path.dirname(_PARTS_LIBRARY_PATH), exist_ok=True)
        with open(_PARTS_LIBRARY_PATH, "w", encoding="utf-8") as f:
            json.dump(parts, f, ensure_ascii=False, indent=2)
        return True
    except OSError:
        logger.warning("Failed to write parts_library.json", exc_info=True)
        return False


def _norm(s) -> str:
    return (s or "").strip().lower()


@app.route("/api/parts-library", methods=["GET"])
def api_parts_library_list():
    """Full parts drawer contents — fetched once per workspace open (see
    ui/js/design-studio.js's _dsFetchPartsLibrary). Not filtered by zone
    server-side; the drawer groups client-side since it's a small dataset
    and the drawer needs to show all 7 categories at once."""
    try:
        parts = _load_parts()
        parts.sort(key=lambda p: p.get("created_at", ""), reverse=True)
        return jsonify({"parts": parts})
    except Exception as exc:
        logger.error("Failed to load parts library: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/parts-library", methods=["POST", "OPTIONS"])
def api_parts_library_add():
    """Adds one part to the library. Body shapes (by "mode"):
      - {"mode": "describe", "zone": str, "description": str} — the
        'AÑADIR PIEZA' drawer form: Joan's free text goes through
        core.commands.document_new_part (a real Groq call) to come out
        structured. Always saved (an explicit, deliberate add).
      - {"mode": "manual", "zone": str, "part": {"name","description",
        "material","mechanism","source","source_model"}} — the silent
        auto-save-as-part path fired whenever a zone flips to 'diseñado'.
        No Groq call — the frontend already has the structured fields from
        the zone itself. Deduplicated: skipped (existing part returned,
        "duplicate": true) if the same zone already has a part with the
        same name+material — otherwise every minor field tweak on a zone
        the user keeps returning to would spam near-identical entries.
    Returns {"part": {...}, "duplicate": bool}."""
    if request.method == "OPTIONS":
        return "", 204
    body = request.get_json(force=True, silent=True) or {}
    mode = body.get("mode")
    zone = body.get("zone", "")
    if not zone or zone not in _ZONE_KEYS:
        return jsonify({"error": "valid zone is required"}), 400

    if mode == "describe":
        description = (body.get("description") or "").strip()
        if not description:
            return jsonify({"error": "description is required"}), 400
        try:
            doc = commands.document_new_part(zone, description)
        except Exception as exc:
            logger.error("document_new_part failed: %s", exc, exc_info=True)
            return jsonify({"error": str(exc)}), 500
        part = {
            "id": uuid.uuid4().hex[:12],
            "zone": zone,
            "name": doc.get("name", ""),
            "description": doc.get("description", ""),
            "material": doc.get("material", ""),
            "mechanism": doc.get("mechanism", ""),
            "source": "lira",
            "source_model": None,
            "created_at": datetime.datetime.now().isoformat(),
        }
    elif mode == "manual":
        supplied = body.get("part") or {}
        if not supplied.get("name"):
            return jsonify({"error": "part.name is required"}), 400

        parts = _load_parts()
        dup = next((
            p for p in parts
            if p.get("zone") == zone
            and _norm(p.get("name")) == _norm(supplied.get("name"))
            and _norm(p.get("material")) == _norm(supplied.get("material"))
        ), None)
        if dup:
            return jsonify({"part": dup, "duplicate": True})

        part = {
            "id": uuid.uuid4().hex[:12],
            "zone": zone,
            "name": supplied.get("name", "")[:80],
            "description": supplied.get("description", ""),
            "material": supplied.get("material", ""),
            "mechanism": supplied.get("mechanism", ""),
            "source": supplied.get("source", "session"),
            "source_model": supplied.get("source_model"),
            "created_at": datetime.datetime.now().isoformat(),
        }
    else:
        return jsonify({"error": "mode must be 'describe' or 'manual'"}), 400

    parts = _load_parts()
    parts.append(part)
    if not _save_parts(parts):
        return jsonify({"error": "failed to save"}), 500

    return jsonify({"part": part, "duplicate": False})


# ---------------------------------------------------------------------------
# Phase 3 — autopilot mode. See core.commands.run_autopilot_zone.
# Ollama lifecycle mirrors the Sleep System (core.ollama_control): the
# frontend calls autopilot-start once before its zone loop begins and
# autopilot-stop once after every queued zone is designed, so the
# llama-server subprocess doesn't sit resident (and burning CPU) between
# autopilot runs.
# ---------------------------------------------------------------------------

@app.route("/api/designs/autopilot-start", methods=["POST", "OPTIONS"])
def api_designs_autopilot_start():
    """Ensures the `ollama serve` daemon is up before the autopilot zone
    loop makes its first /api/designs/autopilot-zone call. Also writes
    core.ollama_control's autopilot lock FIRST, before starting the daemon
    — so scripts/ollama_guard.py's periodic sweep (every 10 min) never has
    a window where llama-server is up but not yet recognized as legitimately
    in use, and doesn't SIGKILL it mid-zone-generation (real bug this
    fixes: that race silently produced fully-empty zones — the 500 from
    Ollama was caught by core.commands.run_autopilot_zone's except clause
    and swallowed into the honest-empty fallback with no error surfaced to
    the UI at all). Best-effort — never fails the request even if Ollama
    can't be started; a zone call against an unreachable daemon just falls
    back to that same honest empty placeholder."""
    if request.method == "OPTIONS":
        return "", 204
    ollama_control.mark_autopilot_running()
    ollama_control.ensure_ollama_daemon_running()
    return jsonify({"ok": True})


@app.route("/api/designs/autopilot-stop", methods=["POST", "OPTIONS"])
def api_designs_autopilot_stop():
    """Kills the llama-server model-serving subprocess once an autopilot
    run's zone loop is done — same cleanup the Sleep System does after
    every session, so the 3B model doesn't stay resident (and consuming
    CPU/RAM) between autopilot runs. Never kills the `ollama serve` daemon
    itself. Also clears the autopilot lock (see autopilot-start's docstring)
    — if this route never gets called (tab closed mid-run, etc.), the lock's
    own age-based staleness check in core.ollama_control.is_autopilot_running
    is what eventually lets the guard reclaim llama-server instead of the
    lock blocking it forever."""
    if request.method == "OPTIONS":
        return "", 204
    killed = ollama_control.kill_llama_server()
    ollama_control.clear_autopilot_running()
    return jsonify({"ok": True, "killed": killed})


@app.route("/api/designs/autopilot-zone", methods=["POST", "OPTIONS"])
def api_designs_autopilot_zone():
    """One zone of a 'PILOTO AUTOMÁTICO' run. Body: {"zone": str,
    "design": dict, "constraints": dict}. `design` already reflects every
    zone the same autopilot run has completed so far (the frontend updates
    its in-memory design after each zone before calling the next), which is
    what gives a multi-zone run cross-zone consistency without this
    endpoint needing any run-level state of its own — it's stateless,
    called once per zone, same as /zone-suggestions. Does not persist
    anything — the frontend applies the result to its in-memory design and
    saves through the normal /api/designs flow, exactly like every other
    LIRA-authored zone update in this file."""
    if request.method == "OPTIONS":
        return "", 204
    print(f"[AUTOPILOT] /api/designs/autopilot-zone HIT — raw body: {request.get_data(as_text=True)[:500]}")
    body = request.get_json(force=True, silent=True) or {}
    zone = body.get("zone", "")
    design = body.get("design") or {}
    constraints = body.get("constraints") or {}
    logger.info("[AUTOPILOT] zone=%s constraints=%s", zone, constraints)
    if not zone:
        return jsonify({"error": "zone is required"}), 400
    try:
        result = commands.run_autopilot_zone(zone, design, constraints)
    except Exception as exc:
        logger.error("run_autopilot_zone failed: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500
    logger.info("[AUTOPILOT] zone=%s result=%s", zone, result)
    return jsonify(result)


# ---------------------------------------------------------------------------
# Design Studio ↔ Conceptuales integration. The actual concepts.json
# read/write stays entirely client-side, through the SAME _loadConcepts()/
# _saveConcepts() the Conceptuales UI already uses (core/routes_memory.py's
# GET/POST /api/concepts) — see ui/js/design-studio.js's
# _dsSaveDesignToConceptuales(). This endpoint only covers the one piece
# that actually needs a Groq call: LIRA's auto-generated description.
# ---------------------------------------------------------------------------

@app.route("/api/designs/concept-description", methods=["POST", "OPTIONS"])
def api_designs_concept_description():
    """Body: {"design": dict}. Returns {"description": str} — see
    core.commands.generate_concept_description. Pure text generation, no
    persistence — the frontend folds the result into whichever concepts.json
    entry it's creating/updating and saves that through /api/concepts."""
    if request.method == "OPTIONS":
        return "", 204
    body = request.get_json(force=True, silent=True) or {}
    design = body.get("design") or {}
    try:
        description = commands.generate_concept_description(design)
    except Exception as exc:
        logger.error("generate_concept_description failed: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500
    return jsonify({"description": description})
