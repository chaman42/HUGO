# Memory V2 — one-time migration of legacy Layer 1/2 facts (plain 'fact' +
# 'category' only, no genuine structured content) to the structured-knowledge
# schema added in core/memory_store.py ('type'/'content'/'date_event'/
# 'importance'/'tags' — see _CONTENT_TYPES there). Uses Ollama (local, free)
# rather than Groq, since this is a one-shot bulk pass over every stored fact
# and not worth spending Groq budget on — new facts going forward are still
# extracted with full structure directly by Groq (see
# core/memory_extract.py's _extract_and_save_memory).
#
# Runs once at import time (same pattern as core.memory_setup's legacy-path
# migration), guarded by a marker file so it doesn't redo work every
# startup. If Ollama isn't reachable yet, the marker is deliberately NOT
# written, so the next startup retries instead of silently giving up
# forever. A fact Ollama can't parse into valid JSON is simply left as-is
# (still plain 'fact'/'category', 'structured' stays False) — backward
# compatible by construction, never a hard failure.
import datetime
import json
import logging
import os
import re

from core.memory_store import (
    MEMORY_HUGO_PATH,
    MEMORY_SHARED_PATH,
    _CONTENT_TYPES,
    _load_fact_file,
    _memory_lock,
    _save_fact_file,
)

logger = logging.getLogger(__name__)

_MIGRATION_MARKER_PATH = "data/.memory_v2_migrated"

_MIGRATE_SYSTEM_PROMPT = (
    "Extraes conocimiento estructurado de un hecho ya guardado sobre Joan. "
    "Respondes solo con JSON válido, sin comentarios ni texto extra."
)


def _build_migrate_prompt(fact_text: str, category: str, today_iso: str) -> str:
    content_types_str = ", ".join(sorted(_CONTENT_TYPES))
    return (
        f"Hoy es {today_iso}.\n"
        f"Hecho: \"{fact_text}\"\n"
        f"Categoría original: {category}\n\n"
        f"Devuelve SOLO JSON: {{\"type\": UNA de [{content_types_str}], "
        '"content": {"summary": "resumen corto", "place": "lugar o null", '
        '"people": ["..."], "context": "contexto breve o null"}, '
        '"date_event": "YYYY-MM-DD absoluta (calculada desde hoy si el hecho '
        'menciona una fecha relativa) o null si no aplica", '
        '"importance": 1-5, "tags": ["2 a 4 palabras clave"]}'
    )


def _parse_migrated_fields(raw: str | None) -> dict | None:
    """Validate an Ollama migration response the same way
    core/memory_extract.py validates Groq's — any invalid/missing field is
    just dropped rather than rejecting the whole response, since a partial
    upgrade (e.g. type + tags but no date_event) is still strictly better
    than leaving the fact fully unstructured."""
    if not raw:
        return None
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group())
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None

    result: dict = {}

    type_ = parsed.get("type")
    if type_ in _CONTENT_TYPES:
        result["type"] = type_

    content = parsed.get("content")
    if isinstance(content, dict) and content:
        result["content"] = content

    date_event = parsed.get("date_event")
    if isinstance(date_event, str):
        try:
            datetime.date.fromisoformat(date_event)
            result["date_event"] = date_event
        except ValueError:
            pass

    importance = parsed.get("importance")
    try:
        importance = int(importance)
        result["importance"] = max(1, min(5, importance))
    except (TypeError, ValueError):
        pass

    tags = parsed.get("tags")
    if isinstance(tags, list):
        tags = [str(t).strip() for t in tags if str(t).strip()]
        if tags:
            result["tags"] = tags

    return result or None


def _migrate_file(path: str, ollama_generate) -> tuple[int, int]:
    """Returns (migrated_count, candidate_count) for one Layer 1/2 file."""
    with _memory_lock:
        facts = _load_fact_file(path, default_category="personal")
    candidates = [f for f in facts if not f.get("structured")]
    if not candidates:
        return 0, 0

    today_iso = datetime.date.today().isoformat()
    migrated = 0
    for f in candidates:
        prompt = _build_migrate_prompt(f["fact"], f.get("category", "personal"), today_iso)
        raw = ollama_generate(_MIGRATE_SYSTEM_PROMPT, prompt, max_tokens=250)
        fields = _parse_migrated_fields(raw)
        if fields is None:
            logger.debug("[MEMORY V2] Could not migrate fact (kept as plain text): %s", f["fact"])
            continue
        f.update(fields)
        f["raw"] = f.get("raw") or f["fact"]
        f["structured"] = True
        migrated += 1

    if migrated:
        with _memory_lock:
            _save_fact_file(path, facts)
    return migrated, len(candidates)


def run_memory_v2_migration() -> None:
    """Entry point — called once at import time, below. Safe to call again
    manually (e.g. from a route) since it's fully idempotent: already-
    structured facts are never re-sent to Ollama."""
    if os.path.exists(_MIGRATION_MARKER_PATH):
        return

    try:
        from core.sleep_llm import _ollama_available, _ollama_generate
    except Exception:
        logger.debug("[MEMORY V2] Ollama helpers unavailable — skipping migration this startup.", exc_info=True)
        return

    if not _ollama_available():
        logger.info("[MEMORY V2] Ollama not reachable — structured migration deferred to next startup.")
        return

    total_migrated = 0
    total_candidates = 0
    for path in (MEMORY_SHARED_PATH, MEMORY_HUGO_PATH):
        migrated, candidates = _migrate_file(path, _ollama_generate)
        total_migrated += migrated
        total_candidates += candidates

    logger.info(
        "[MEMORY V2] Migration pass complete: %d/%d legacy facts upgraded to structured format.",
        total_migrated, total_candidates,
    )

    try:
        os.makedirs(os.path.dirname(_MIGRATION_MARKER_PATH) or ".", exist_ok=True)
        with open(_MIGRATION_MARKER_PATH, "w", encoding="utf-8") as fh:
            fh.write(datetime.datetime.now().isoformat())
    except OSError:
        logger.debug("[MEMORY V2] Could not write migration marker.", exc_info=True)


# Run migration at import time — cheap no-op after the first successful pass
# (marker file check above returns immediately).
run_memory_v2_migration()
