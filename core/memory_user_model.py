# ═══════════════════════════════════════════════════════════════════════════
# USER MODEL — a living, compact document (data/user_model.json) capturing
# LIRA's understanding of Joan as a person: how he thinks, works, and
# operates — not a fact dump. Built/updated exclusively by
# scripts/reflective_mode.py's 'Modelo de Usuario' sleep sub-phase (Ollama
# only, see that script), which reviews memory facts, episodes, and recent
# conversation history and synthesizes updates. This module is the read
# side (get_user_model/format_user_model_block, consulted by
# core/commands.py on every response) plus the shared write side
# (update_user_model) both that sub-phase and this module's own callers use.
#
# Same dependency-light discipline as core/reminders.py and
# core/memory_episodes.py — no imports beyond the stdlib, so
# scripts/reflective_mode.py (which deliberately avoids core.commands/
# core.voice/core.tools) can import this module directly.
# ═══════════════════════════════════════════════════════════════════════════
import datetime
import json
import os
import threading

USER_MODEL_PATH = "data/user_model.json"
# Append-only revision trail — one line per (field, update) that actually
# changed the live document. The document itself (USER_MODEL_PATH) is a
# snapshot of LIRA's *current* read of Joan; this is how she got there —
# same "kept for history, not silently overwritten" spirit as
# core.memory_store._mark_fact_outdated's 'outdated'/'outdated_reason' on
# facts, just for the interpretation layer instead of the fact layer (see
# core/epistemics.py, which reads both).
USER_MODEL_HISTORY_PATH = "data/user_model_history.jsonl"

_user_model_lock = threading.Lock()

# String fields: replaced only when the new value is at least as
# substantial as what's already there (see _is_more_informative) — the
# model never regresses to a shorter/emptier description.
_STRING_FIELDS = (
    "thinking_style",
    "work_style",
    "communication_preferences",
    "relationship_with_lira",
)
# List fields: unioned, never shrunk — new items are appended, nothing
# already stored is ever dropped by an update, only by the length cap
# below (oldest entries first) so the model stays compact.
_LIST_FIELDS = (
    "motivations",
    "blockers",
    "current_focus",
    "patterns",
    "strengths",
    "blind_spots",
)
_MAX_LIST_ITEMS = 8   # per field — keeps the model a living summary, not an ever-growing log

_DEFAULT_USER_MODEL = {
    "updated_at":                 "",
    "thinking_style":             "",
    "work_style":                 "",
    "communication_preferences":  "",
    "motivations":                [],
    "blockers":                   [],
    "current_focus":              [],
    "patterns":                   [],
    "strengths":                  [],
    "blind_spots":                [],
    "relationship_with_lira":     "",
}


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _load_user_model() -> dict:
    try:
        with open(USER_MODEL_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    model = dict(_DEFAULT_USER_MODEL)
    model.update({k: v for k, v in data.items() if k in _DEFAULT_USER_MODEL})
    # Bookkeeping keys outside the documented schema (e.g. the sleep
    # phase's own "_sessions_at_last_update" gate marker) round-trip as-is
    # rather than being stripped, so the write side can keep state in the
    # same file without a second data/*.json for one integer.
    for k, v in data.items():
        if k not in _DEFAULT_USER_MODEL:
            model[k] = v
    return model


def _save_user_model(model: dict) -> None:
    os.makedirs(os.path.dirname(USER_MODEL_PATH) or ".", exist_ok=True)
    with open(USER_MODEL_PATH, "w", encoding="utf-8") as f:
        json.dump(model, f, ensure_ascii=False, indent=2)


def _append_history(entries: list[dict]) -> None:
    """Best-effort append to USER_MODEL_HISTORY_PATH — one JSON object per
    line, one line per changed field. Never raises: a history-log failure
    must not block the model update itself (same discipline as
    core/reflective.py's rate-limit bookkeeping)."""
    if not entries:
        return
    try:
        os.makedirs(os.path.dirname(USER_MODEL_HISTORY_PATH) or ".", exist_ok=True)
        with open(USER_MODEL_HISTORY_PATH, "a", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def get_user_model() -> dict:
    """Full snapshot of the current user model, schema-defaulted so every
    documented key is always present even before the first sleep run."""
    with _user_model_lock:
        return _load_user_model()


def _is_more_informative(new: str, old: str) -> bool:
    new = (new or "").strip()
    old = (old or "").strip()
    return bool(new) and len(new) >= len(old)


def update_user_model(updates: dict, reasoning: str | None = None) -> tuple[dict, list[str]]:
    """Merges `updates` (a partial dict shaped like the schema) into the
    persisted model — the only write path, used both by
    scripts/reflective_mode.py's sleep sub-phase and anything else that
    ever needs to nudge the model. String fields only replace what's
    stored when the new text is at least as substantial (never overwrite
    with less information than what was there); list fields are unioned
    (existing items are never removed by an update), capped at
    _MAX_LIST_ITEMS per field with the oldest entries dropped first once
    that cap is hit. `updated_at` is only bumped if something actually
    changed. Returns (resulting full model, fields actually changed) —
    the second element used to matter (bug fix 2026-08-10: the caller was
    logging updates.keys() — what was PROPOSED — instead of what this
    function actually determined was more informative and wrote, so a run
    where every proposed field lost to `_is_more_informative` logged as a
    full success while silently writing nothing).

    Every field that actually changes gets one line appended to
    USER_MODEL_HISTORY_PATH (old value, new value, `reasoning` — the
    caller's one-line account of why, if it has one — and timestamp)
    instead of the previous value being silently clobbered. This is what
    lets a later pass answer "what did LIRA used to think about Joan, and
    why did that change" instead of only ever seeing the current snapshot."""
    with _user_model_lock:
        model = _load_user_model()
        changed_fields: list[str] = []
        history_entries: list[dict] = []
        now = _now_iso()

        for field in _STRING_FIELDS:
            new_val = updates.get(field)
            if isinstance(new_val, str) and _is_more_informative(new_val, model.get(field, "")):
                old_val = model.get(field, "")
                model[field] = new_val.strip()
                changed_fields.append(field)
                history_entries.append({
                    "ts": now, "field": field, "change": "replaced",
                    "old": old_val, "new": model[field], "reasoning": reasoning,
                })

        for field in _LIST_FIELDS:
            new_items = updates.get(field)
            if not isinstance(new_items, list):
                continue
            merged = list(model.get(field, []))
            added_items: list[str] = []
            for item in new_items:
                item = str(item).strip()
                if item and item not in merged:
                    merged.append(item)
                    added_items.append(item)
            if added_items:
                dropped = merged[:-_MAX_LIST_ITEMS] if len(merged) > _MAX_LIST_ITEMS else []
                if len(merged) > _MAX_LIST_ITEMS:
                    merged = merged[-_MAX_LIST_ITEMS:]
                model[field] = merged
                changed_fields.append(field)
                history_entries.append({
                    "ts": now, "field": field, "change": "appended",
                    "added": added_items, "dropped": dropped, "reasoning": reasoning,
                })

        if changed_fields:
            model["updated_at"] = now
            _save_user_model(model)
            _append_history(history_entries)
        return model, changed_fields


def set_bookkeeping(key: str, value) -> None:
    """Writes one non-schema bookkeeping key (e.g. the sleep phase's own
    session-count gate marker) directly, bypassing the 'never regress'
    merge rules above — those only make sense for the actual model fields,
    not internal counters."""
    with _user_model_lock:
        model = _load_user_model()
        model[key] = value
        _save_user_model(model)


def _has_content(model: dict) -> bool:
    return any(model.get(f) for f in _STRING_FIELDS) or any(model.get(f) for f in _LIST_FIELDS)


def format_user_model_block(model: dict | None = None) -> str:
    """Compact 'MODELO DE JOAN' block for injection into the prompt — LIRA's
    understanding of who she's talking to, never a data dump. Returns ''
    until the model has genuinely been built (the common case before the
    first qualifying sleep session), so nothing empty is ever injected.
    Capped at 7 lines total (a header plus at most 6 bullets, picked in a
    fixed priority order) per spec."""
    model = model if model is not None else get_user_model()
    if not _has_content(model):
        return ""

    candidates = []
    if model.get("thinking_style"):
        candidates.append(f"Piensa así: {model['thinking_style']}")
    if model.get("work_style"):
        candidates.append(f"Trabaja así: {model['work_style']}")
    if model.get("current_focus"):
        candidates.append(f"Enfocado ahora en: {', '.join(model['current_focus'][:3])}")
    if model.get("motivations"):
        candidates.append(f"Le mueve: {', '.join(model['motivations'][:3])}")
    if model.get("blockers"):
        candidates.append(f"Se bloquea con: {', '.join(model['blockers'][:3])}")
    if model.get("patterns"):
        candidates.append(f"Patrón recurrente: {', '.join(model['patterns'][:2])}")
    if model.get("strengths"):
        candidates.append(f"Fuerte en: {', '.join(model['strengths'][:2])}")
    if model.get("blind_spots"):
        candidates.append(f"Punto ciego: {', '.join(model['blind_spots'][:2])}")
    if model.get("communication_preferences"):
        candidates.append(f"Prefiere que le hables: {model['communication_preferences']}")
    if model.get("relationship_with_lira"):
        candidates.append(f"Con LIRA: {model['relationship_with_lira']}")

    lines = candidates[:6]
    if not lines:
        return ""
    return (
        "MODELO DE JOAN (tu comprensión de quién es, no una lista de datos — "
        "úsala para entender de dónde viene, nunca la recites):\n"
        + "\n".join(f"- {l}" for l in lines)
    )
