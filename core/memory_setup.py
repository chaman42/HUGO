# One-time legacy-memory migration and Layer 3 instructions loading — both
# run at import time. Split out of core/memory.py (pure refactor, no
# behavior change). Imported by core.memory right after core.memory_store,
# preserving the original file's execution order: migration first, then
# instructions.
import json
import logging
import os
import threading

from core.memory_store import (
    MEMORY_SHARED_PATH,
    _dedup_facts,
    _load_fact_file,
    _memory_lock,
    _save_fact_file,
)

logger = logging.getLogger(__name__)


def _migrate_legacy_memory() -> None:
    """One-time migration: move data/memory.json → data/memory_shared.json.
    Dead in practice today (both legacy paths were already renamed to
    *.migrated by a previous run) — kept so dropping an old file back in
    still merges cleanly instead of being silently ignored."""
    legacy_paths = ["data/memory.json", "data/memoria.json"]
    for legacy in legacy_paths:
        if os.path.exists(legacy):
            with _memory_lock:
                existing = _load_fact_file(MEMORY_SHARED_PATH, default_category="personal")
                legacy_facts = _load_fact_file(legacy, default_category="personal")
                merged = _dedup_facts(existing + legacy_facts)
                if merged:
                    _save_fact_file(MEMORY_SHARED_PATH, merged)
            try:
                os.rename(legacy, legacy + ".migrated")
            except OSError:
                pass
            logger.info("Migrated %s → %s (%d facts)", legacy, MEMORY_SHARED_PATH, len(merged))


# Run migration at import time
_migrate_legacy_memory()

# ---------------------------------------------------------------------------
# LAYER 3 — Instructions memory (data/memory_instructions.json)
#
# Static behavioral rules: capabilities, limitations, roadmap. Structure:
# {"global": [...], "lira": [...]}.
# Human-editable, NEVER written by _extract_and_save_memory(). Hot-reloadable
# via reload_instructions() / POST /api/reload_instructions — no jarvis.py
# restart needed for edits to take effect.
# ---------------------------------------------------------------------------

MEMORY_INSTRUCTIONS_PATH = "data/memory_instructions.json"

_instructions_lock = threading.Lock()


def _load_instructions_file() -> dict:
    try:
        with open(MEMORY_INSTRUCTIONS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.debug("Could not load memory_instructions.json: %s", exc)
    return {}


_INSTRUCTIONS: dict = _load_instructions_file()


def reload_instructions() -> None:
    """Re-read data/memory_instructions.json and refresh the in-memory cache.

    Called by POST /api/reload_instructions, so edits to the behavioral-rules
    file apply to the very next request — no restart needed.
    """
    global _INSTRUCTIONS
    with _instructions_lock:
        _INSTRUCTIONS = _load_instructions_file()


def _build_instructions_block(personality: str) -> str:
    """Combine 'global' rules with this personality's own array."""
    with _instructions_lock:
        data = _INSTRUCTIONS
    rules = list(data.get("global", [])) + list(data.get(personality, []))
    if not rules:
        return ""
    return "\n".join(f"- {r}" for r in rules)
