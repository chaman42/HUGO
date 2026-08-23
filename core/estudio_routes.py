"""Flask routes: GET /api/estudio — all ESTUDIO app data in one call.

ESTUDIO has 6 subsections. Five of them (investigaciones/resumenes/
esquemas/explorations/documentos) read straight from their own data/*.json
files — each just a plain JSON array, empty until something actually
starts writing to it (no generator exists yet for documentos; this
endpoint is read-only display plumbing, same spirit as
core/routes_memory.py's concepts endpoints). The sixth (ideas) is NOT a
new data source: it reads the existing data/sleep_insights.json 'ideas'
array that core.sleep's Phase 3 (Generador de Ideas) already populates
during every sleep cycle — see _load_ideas() below.

Expected shape for each investigations.json entry (used by the frontend's
card + expanded-detail view — see ui/js/estudio.js). Written by
core/investigations.py (create_investigation/save_investigation), created
via core.intent's start_investigation ("investiga X" / "quiero saber sobre
X" / "analiza X en profundidad") and advanced by the Sleep System's
Incubación phase (core/sleep_phases_incubation.py):
    {
        "id": str, "title": str,
        "status": "activa" | "incubando" | "lista_para_revision" | "completada",
        "date": str (ISO), "created_at": str (ISO), "summary": str,
        "question": str, "methodology": str, "sources": list[str],
        "hypotheses": [{"text": str, "confidence": float (0-1)}],
        "sub_questions": list[str], "cycles_processed": int,
        "conclusions": str, "confidence": float (0-1),
    }
summaries.json — ONLY summaries Joan explicitly asked HUGO to generate
("hazme un resumen de X" etc., core/commands.py's generate_summary()):
    {"title": str, "date": str, "type": "conversación"|"tema"|"diario"|"semanal", "excerpt": str}
explorations.json — HUGO's OWN autonomous sleep-time discoveries, never
user-requested; kept deliberately separate from summaries.json above so
RESÚMENES only ever shows what Joan actually asked for. Written by
core/sleep_curiosity_search.py's expanded Phase 8 (active web search
during sleep, type 'curiosidad') and its continuous-sleep-only "curiosidad
profunda" deep-dive (type 'exploración profunda'):
    {"title": str, "date": str, "type": "curiosidad"|"exploración profunda", "excerpt": str,
     "url": str, "summary": str (same text as "excerpt", kept for API-shape
     parity with the feature spec), "topic": str,
     "relevance": float (0-1, 'curiosidad' only), "found_during_sleep_cycle": int | None}
    The frontend (ui/js/estudio.js) only reads title/date/type/excerpt(/topic)
    — the rest is for anything that wants the richer record without a
    schema change.
schemas.json:
    {"title": str, "date": str, "type": "mapa conceptual"|"outline"|"estructura", "topic": str}
documents.json:
    {"title": str, "date": str, "type": str, "word_count": int}
"""
import json
import logging
import os

from flask import jsonify, request

from core.server import app
from core import sleep_insights_store

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR  = os.path.join(_REPO_ROOT, "data")

_INVESTIGATIONS_PATH = os.path.join(_DATA_DIR, "investigations.json")
_SUMMARIES_PATH      = os.path.join(_DATA_DIR, "summaries.json")
_EXPLORATIONS_PATH   = os.path.join(_DATA_DIR, "explorations.json")
_SCHEMAS_PATH        = os.path.join(_DATA_DIR, "schemas.json")
_DOCUMENTS_PATH      = os.path.join(_DATA_DIR, "documents.json")

# Ideas grow unbounded over many sleep cycles (720+ already on file) — same
# "always return a manageable page" reasoning as
# sleep_insights_store.get_sleep_insights_summary()'s own limit.
_MAX_IDEAS = 50


def _load_json_array(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _load_ideas() -> list[dict]:
    """Ideas tab — a relevance-sorted view onto data.sleep_insights.json's
    existing 'ideas' array, not a new store. 'source' distinguishes the
    two systems that can write an idea entry: core.sleep's cycle-tracked
    Phase 3 (tagged with 'cycle' — see core.sleep_insights_store._add_insight())
    vs. core.reflective's lighter continuous gathering, which doesn't tag
    a cycle number at all."""
    try:
        data = sleep_insights_store.load_insights()
    except Exception:
        logger.warning("Failed to load sleep insights for ESTUDIO ideas", exc_info=True)
        return []
    ideas = [i for i in data.get("ideas", []) if isinstance(i, dict) and i.get("text")]
    ideas.sort(key=lambda i: i.get("confidence", 0), reverse=True)
    return [
        {
            "description": idea["text"],
            "date":        idea.get("added"),
            "source":      f"Sueño ciclo {idea['cycle']}" if idea.get("cycle") is not None else "Reflexión",
            "relevance":   idea.get("confidence", 0),
        }
        for idea in ideas[:_MAX_IDEAS]
    ]


@app.route("/api/estudio")
def api_estudio():
    """Backs the ESTUDIO app launcher section — one call for all 6 tabs
    (investigaciones/resumenes/esquemas/explorations/ideas/documentos)
    rather than 6 round trips, since they all render together on
    section-open."""
    try:
        return jsonify({
            "investigations": _load_json_array(_INVESTIGATIONS_PATH),
            "summaries":      _load_json_array(_SUMMARIES_PATH),
            "explorations":   _load_json_array(_EXPLORATIONS_PATH),
            "schemas":        _load_json_array(_SCHEMAS_PATH),
            "ideas":          _load_ideas(),
            "documents":      _load_json_array(_DOCUMENTS_PATH),
        })
    except Exception as exc:
        logger.error("Failed to load ESTUDIO data: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/estudio/explorations/read", methods=["POST", "OPTIONS"])
def api_estudio_mark_exploration_read():
    """'MARCAR COMO LEÍDO' in the EXPLORACIONES detail view (see
    ui/js/estudio.js's _markExplorationRead()). Body: {"index": int} — the
    entry's position in data/explorations.json (append-only, so this is
    stable as long as nothing else reorders the file). Sets "read": true
    on that entry; the card renders dimmer and drops its unread dot next
    render (see ui/css/estudio.css's .estudio-card.read)."""
    if request.method == "OPTIONS":
        return "", 204
    data = request.get_json(force=True, silent=True) or {}
    idx = data.get("index")
    if not isinstance(idx, int):
        return jsonify({"error": "index must be an integer"}), 400

    records = _load_json_array(_EXPLORATIONS_PATH)
    if not (0 <= idx < len(records)) or not isinstance(records[idx], dict):
        return jsonify({"error": "index out of range"}), 404

    records[idx]["read"] = True
    try:
        with open(_EXPLORATIONS_PATH, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        logger.error("Failed to write explorations.json: %s", exc)
        return jsonify({"error": "failed to save"}), 500

    try:
        import core.server as server_mod
        server_mod.socketio.emit("estudio_updated", {"section": "exploraciones"})
    except Exception:
        pass

    return jsonify({"ok": True})
