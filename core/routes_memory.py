"""Flask routes: HUD concepts CRUD, four-layer memory hot-reload/stats/
cleanup, think log, mind-map connections, and the /api/info diagnostics
snapshot."""
import json
import logging

from flask import jsonify, request

from core.server import app, _CONCEPTS_FILE

logger = logging.getLogger(__name__)

@app.route("/api/concepts", methods=["GET"])
def api_concepts_get():
    """Return the full HUD Conceptuales list from data/concepts.json.

    This is now the source of truth for concepts (see ui/index.html
    _fetchConcepts) instead of the browser's localStorage.

    Normalizes 'type' on every concept — 'armor' or 'general' (Armaduras /
    Conceptos Generales subsections). Any concept saved before this field
    existed, or with an unrecognized value, defaults to 'armor' — the same
    migration rule ui/index.html's own _normalizeConceptTypes() applies, so
    a fresh GET (e.g. after clearing localStorage, or a non-browser
    consumer) never sees an un-migrated concept.
    """
    try:
        with open(_CONCEPTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to read concepts.json: %s", exc)
        data = {"concepts": []}
    concepts = data.get("concepts", [])
    for c in concepts:
        if isinstance(c, dict) and c.get("type") != "general":
            c["type"] = "armor"
    return jsonify({"concepts": concepts})

@app.route("/api/concepts", methods=["POST", "OPTIONS"])
def api_concepts_post():
    """Persist the full Conceptuales list and hot-reload LIRA's live memory.

    Body: {"concepts": [...]}. Called by ui/index.html _saveConcepts() on
    every create/edit/delete — always sends the whole list, so this simply
    overwrites data/concepts.json rather than diffing/merging entries.
    """
    if request.method == "OPTIONS":
        return "", 204
    data = request.get_json(force=True, silent=True) or {}
    concepts = data.get("concepts", [])
    try:
        with open(_CONCEPTS_FILE, "w", encoding="utf-8") as f:
            json.dump({"concepts": concepts}, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        logger.error("Failed to write concepts.json: %s", exc)
        return jsonify({"error": "failed to save concepts"}), 500

    # Hot-reload LIRA's in-memory concepts summary so the change is usable in
    # conversation immediately — no jarvis.py restart needed.
    try:
        import core.memory as memory
        memory.reload_concepts()
    except Exception as exc:
        logger.error("Failed to reload concepts into LIRA's memory: %s", exc)

    return jsonify({"ok": True, "count": len(concepts)})

@app.route("/api/reload_instructions", methods=["POST"])
def api_reload_instructions():
    """Re-read data/memory_instructions.json (Layer 3) without restarting
    jarvis.py. Call this after hand-editing the file."""
    import core.memory as memory
    try:
        memory.reload_instructions()
        return jsonify({"ok": True})
    except Exception as exc:
        logger.error("Failed to reload instructions: %s", exc)
        return jsonify({"error": str(exc)}), 500

@app.route("/api/memory_stats")
def api_memory_stats():
    """Fact counts, category breakdown and average weight per Layer 1/2 file."""
    import core.memory as memory
    try:
        return jsonify(memory.get_memory_stats())
    except Exception as exc:
        logger.error("Failed to compute memory stats: %s", exc)
        return jsonify({"error": str(exc)}), 500

@app.route("/api/memory_clean", methods=["POST"])
def api_memory_clean():
    """Run semantic dedup + temporal-fact removal on every Layer 1/2 file."""
    import core.memory as memory
    try:
        return jsonify(memory.clean_all_memory())
    except Exception as exc:
        logger.error("Failed to clean memory: %s", exc)
        return jsonify({"error": str(exc)}), 500

@app.route("/api/memory_active")
def api_memory_active():
    """Currently stored facts (grouped by category), the 5 most recent
    episodes, current concepts, and the live HUD context — backs the
    CORE app's Memoria tab and its Mapa Mental graph."""
    import core.memory as memory
    try:
        return jsonify(memory.get_active_memory())
    except Exception as exc:
        logger.error("Failed to compute active memory: %s", exc)
        return jsonify({"error": str(exc)}), 500

@app.route("/api/think_log")
def api_think_log():
    """Last 10 thinking blocks (newest first) — backs the CORE app's
    Pensamiento tab. See core.memory.get_think_log()."""
    import core.memory as memory
    try:
        return jsonify({"entries": memory.get_think_log(limit=10)})
    except Exception as exc:
        logger.error("Failed to read think log: %s", exc)
        return jsonify({"error": str(exc)}), 500

@app.route("/api/info")
def api_info():
    """Runtime info for the settings panel and the CORE app's Estado tab."""
    try:
        import core.personality as personality
        p_name = personality._personality
        p      = personality.PERSONALITIES.get(p_name, {})
    except Exception:
        p_name = "lira"
        p      = {}

    from core.voice import KOKORO_VOICE_LIRA, KOKORO_FALLBACK_LIRA

    # Estado tab additions — session uptime and last-call model/latency.
    # Best-effort: none of these existed on this endpoint before, so a
    # failure here must never break the settings panel's existing fields.
    session_uptime = ""
    last_latency   = {}
    model_chain    = []
    try:
        import core.tools as tools
        session_uptime = tools.get_session_duration_string()
    except Exception:
        pass
    try:
        import core.commands as commands
        last_latency = dict(commands._last_latency)
        model_chain  = list(commands.GROQ_MODEL_CHAIN)
    except Exception:
        pass

    # Reflective mode's token budget snapshot — see core.reflective and
    # ui/index.html's _renderCoreEstado(). Same best-effort pattern as the
    # rest of this endpoint: a failure here must never break Estado.
    reflective_status = {}
    try:
        import core.reflective as reflective
        reflective_status = reflective.get_status()
    except Exception:
        pass

    # Sleep System's last-session summary — see core.sleep and
    # ui/index.html's _renderCoreEstado(). The Ajustes button's own LIVE
    # polling while a session is running uses the dedicated
    # GET /api/sleep/status below instead (this static snapshot here is
    # enough for Estado's "last sleep session" line).
    sleep_status = {}
    try:
        import core.sleep as sleep_mod
        sleep_status = sleep_mod.get_status()
    except Exception:
        pass

    return jsonify({
        "personality":     p_name,
        "display_name":    p.get("display_name", "L I R A"),
        "tts":             p.get("tts", "kokoro_lira"),
        "kokoro_voice":    KOKORO_VOICE_LIRA,
        "fallback_voice":  KOKORO_FALLBACK_LIRA,
        "vosk_model":      "vosk-model-es-0.42",
        "session_uptime":  session_uptime,
        "last_latency":    last_latency,
        "groq_model_chain": model_chain,
        "reflective":      reflective_status,
        "sleep":           sleep_status,
    })

@app.route("/api/mind_map_connections")
def api_mind_map_connections():
    """Reflective-mode-generated connections between Mapa Mental nodes —
    see core.reflective and data/mind_map_connections.json. Backs
    ui/index.html's _renderCoreMapa()."""
    try:
        import core.reflective as reflective
        return jsonify(reflective.get_connections())
    except Exception as exc:
        logger.error("Failed to read mind map connections: %s", exc)
        return jsonify([])
