# ═══════════════════════════════════════════════════════════════════════════
# PREFERENCES — Entity Pillars Phase 4: "Hugo should be able to develop her
# own intellectual tastes... while being able to explain and revise those
# preferences." data/preferences.json — HUGO's own stated leanings toward
# certain kinds of solutions/approaches ("prefiero soluciones modulares
# porque...", never a preference ABOUT Joan — that's core/memory_user_model.py's
# job). Built/updated exclusively by scripts/reflective_mode.py's
# 'Preferencias' sleep sub-phase, which reviews her own accumulated
# sleep-insight 'ideas'/'autocritica' entries (data/sleep_insights.json —
# what she's actually proposed and critiqued about herself over time) and
# synthesizes a recurring theme, if one genuinely exists — same "don't
# fabricate a pattern that isn't there" discipline as
# core/memory_user_model.py's synthesis.
#
# Write path: record_preference() upserts by similarity (reinforces an
# existing preference rather than duplicating it, same spirit as
# core.memory_store._upsert_fact) and revise_preference() explicitly
# supersedes one preference with another, marking the old one 'outdated'
# with an 'outdated_reason' — same convention core/memory_store.py uses for
# facts, which is what lets core/belief_revision.py surface a changed
# preference the same way it surfaces a changed fact or a changed
# hypothesis.
#
# Dependency-light (json/os/re/threading/uuid/datetime only), same
# discipline as core/memory_user_model.py, so this can be imported from
# scripts/reflective_mode.py as well as the live app.
# ═══════════════════════════════════════════════════════════════════════════
import datetime
import json
import os
import re
import threading
import uuid

PREFERENCES_PATH = "data/preferences.json"

_preferences_lock = threading.Lock()

_SIMILARITY_THRESHOLD = 0.5
_MAX_STRENGTH = 1.0


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _keywords(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", (text or "").lower()) if len(w) > 2}


def _similarity(a: str, b: str) -> float:
    wa, wb = _keywords(a), _keywords(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _load() -> dict:
    try:
        with open(PREFERENCES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("items", [])
    if not isinstance(data["items"], list):
        data["items"] = []
    return data


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(PREFERENCES_PATH) or ".", exist_ok=True)
    with open(PREFERENCES_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_bookkeeping(key: str, default=None):
    with _preferences_lock:
        return _load().get(key, default)


def set_bookkeeping(key: str, value) -> None:
    with _preferences_lock:
        data = _load()
        data[key] = value
        _save(data)


def get_preferences(domain: str | None = None) -> list[dict]:
    """Non-outdated preferences, strongest first."""
    with _preferences_lock:
        items = [p for p in _load()["items"] if not p.get("outdated")]
    if domain:
        items = [p for p in items if p.get("domain") == domain]
    return sorted(items, key=lambda p: p.get("strength", 0.5), reverse=True)


def record_preference(statement: str, domain: str, reasoning: str, strength: float = 0.6) -> dict:
    """Upserts *statement* under *domain*: reinforces the closest existing
    non-outdated preference in the same domain (similarity >
    _SIMILARITY_THRESHOLD) instead of duplicating it, else creates a new
    one. Caller (the sleep sub-phase) decides whether this is truly new
    evidence or reinforcement — this function just avoids two near-
    duplicate entries when the answer is 'reinforcement'."""
    statement = (statement or "").strip()
    with _preferences_lock:
        data = _load()
        for p in data["items"]:
            if p.get("outdated") or p.get("domain") != domain:
                continue
            if _similarity(statement, p.get("statement", "")) > _SIMILARITY_THRESHOLD:
                p["strength"] = min(_MAX_STRENGTH, p.get("strength", 0.5) + 0.08)
                p["last_reinforced"] = _now_iso()
                p["reinforced_count"] = p.get("reinforced_count", 0) + 1
                _save(data)
                return p
        new_pref = {
            "id": uuid.uuid4().hex[:12], "statement": statement, "domain": domain,
            "reasoning": reasoning, "strength": max(0.0, min(_MAX_STRENGTH, strength)),
            "created_at": _now_iso(), "last_reinforced": _now_iso(), "reinforced_count": 0,
            "outdated": False, "outdated_at": None, "outdated_reason": None,
        }
        data["items"].append(new_pref)
        _save(data)
        return new_pref


def revise_preference(old_pref_id: str, new_statement: str, domain: str, reasoning: str, strength: float = 0.6) -> dict:
    """Explicitly supersedes the preference with id *old_pref_id* — marks
    it outdated (with 'outdated_reason' = the new statement, same
    convention as core.memory_store._mark_fact_outdated) and records the
    replacement as a new preference. Used only when new evidence genuinely
    contradicts an old preference, not when it merely reinforces it (see
    record_preference for that path)."""
    with _preferences_lock:
        data = _load()
        for p in data["items"]:
            if p.get("id") == old_pref_id and not p.get("outdated"):
                p["outdated"] = True
                p["outdated_at"] = _now_iso()
                p["outdated_reason"] = new_statement
                break
        new_pref = {
            "id": uuid.uuid4().hex[:12], "statement": new_statement.strip(), "domain": domain,
            "reasoning": reasoning, "strength": max(0.0, min(_MAX_STRENGTH, strength)),
            "created_at": _now_iso(), "last_reinforced": _now_iso(), "reinforced_count": 0,
            "outdated": False, "outdated_at": None, "outdated_reason": None,
        }
        data["items"].append(new_pref)
        _save(data)
        return new_pref


def format_preferences_block(domain: str | None = None, limit: int = 4) -> str:
    """Reactive prompt-facing block ('¿tienes alguna preferencia por cierto
    tipo de solución?') — empty when nothing's been synthesized yet."""
    prefs = get_preferences(domain)[:limit]
    if not prefs:
        return ""
    lines = [f"- ({p['domain']}) {p['statement']} — {p['reasoning']}" for p in prefs]
    return "\n".join(lines)
