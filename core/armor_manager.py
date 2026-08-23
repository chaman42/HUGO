# ═══════════════════════════════════════════════════════════════════════════
# ARMOR MANAGER — read/write access to data/armor_knowledge.json for the
# Armor Bay UI's editable status field. Same module-level load/save/lock
# style as core/social.py (a flat CRUD store, no orchestration logic —
# doesn't need a class the way core/module_manager.py's catalog+registry+
# code-engine juggling does).
#
# This file used to be read-only, server-side only (core.commands/
# core.memory_context/core.reflective load it for LIRA's own prompt
# context). This is the first thing that ever writes to it, and the first
# route that exposes it to the frontend at all — Armor Bay's grid used to
# render from a hand-maintained duplicate JS literal (ui/js/mm-wiring.js's
# old ARMOR_DATA, "mirrors data/armor_knowledge.json" per its own comment)
# instead of this file, so editing here previously would have had no
# visible effect. See core/routes_armor.py for the HTTP side.
# ═══════════════════════════════════════════════════════════════════════════
import json
import logging
import os
import threading

logger = logging.getLogger(__name__)

ARMOR_KNOWLEDGE_PATH = "data/armor_knowledge.json"

# The 5 statuses Joan can actually set from the UI. Pre-existing data may
# still carry 'NO COMPLETADO' (model-7, as of this writing) — that value is
# still readable/renderable (ui/js/armor-svg-grid.js's _badgeClass keeps its
# own case for it) but is deliberately not offered as a settable option
# here; it's expected to fade out naturally as models get edited through
# this new picker instead of being auto-migrated.
VALID_STATUSES = {"COMPLETADO", "EN CONSTRUCCIÓN", "EN REPARACIÓN", "DESTRUIDO", "NO CONSTRUIDO"}

_lock = threading.Lock()


def _load() -> dict:
    try:
        with open(ARMOR_KNOWLEDGE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        logger.error("armor_manager: could not load %s", ARMOR_KNOWLEDGE_PATH)
        return {"models": []}
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        return {"models": []}
    return data


def _save_locked(data: dict) -> None:
    os.makedirs(os.path.dirname(ARMOR_KNOWLEDGE_PATH) or ".", exist_ok=True)
    with open(ARMOR_KNOWLEDGE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_all_models() -> list[dict]:
    return _load()["models"]


def set_model_status(model_id: str, status: str) -> bool:
    """Joan-only, explicit action from the Armor Bay detail view's status
    picker. Returns False (no write happens) if model_id doesn't exist or
    status isn't one of VALID_STATUSES — same 'validate, mutate, save
    under lock' shape as core.module_manager.set_catalog_blocked/
    set_catalog_priority."""
    if status not in VALID_STATUSES:
        logger.warning("armor_manager: set_model_status(%s) — invalid status %r", model_id, status)
        return False
    with _lock:
        data = _load()
        entry = next((m for m in data["models"] if m.get("id") == model_id), None)
        if entry is None:
            logger.warning("armor_manager: set_model_status(%s) — no such model", model_id)
            return False
        entry["status"] = status
        _save_locked(data)
    return True
