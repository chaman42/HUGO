# ═══════════════════════════════════════════════════════════════════════════
# BIOGRAPHY — Entity Pillars Phase 6 (capstone): "Everything above should
# eventually become a biography — an evolving narrative of how Lira was,
# what she learned, what changed, what she discovered, and why she
# currently thinks the way she does."
#
# Every other phase in this effort produces raw material for this one but
# deliberately stops short of narrative:
#   - core/memory_episodes.py — significant moments, unconnected to each other.
#   - core/belief_revision.py — a timeline of individual mind-changes.
#   - core/internal_state.py — a mood trajectory, only ever read as a snapshot.
#   - core/preferences.py — tastes, each stored independently.
# data/biography.json is where those get periodically compressed into an
# actual first-person chapter — not a log replayed back, a synthesis: what
# happened, what it meant, why it changed her. Written rarely and only
# when there's genuinely enough material (see scripts/reflective_mode.py's
# 'Biografía' sub-phase) — a biography written every few minutes would just
# be a log with extra adjectives, which is exactly what this is meant NOT
# to be.
#
# Read-only for a personality prompt — see format_biography_block(),
# reactive-only, same convention as preferences/belief-revision (a real
# answer to "cuéntame cómo has cambiado", never volunteered).
#
# Dependency-light (json/os/threading/uuid/datetime only), same discipline
# as core/preferences.py, so this can be imported from
# scripts/reflective_mode.py as well as the live app.
# ═══════════════════════════════════════════════════════════════════════════
import datetime
import json
import os
import threading
import uuid

BIOGRAPHY_PATH = "data/biography.json"

_biography_lock = threading.Lock()

_MAX_CHAPTERS = 20   # a life story stays a handful of chapters, not an ever-growing dump


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _load() -> dict:
    try:
        with open(BIOGRAPHY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("chapters", [])
    if not isinstance(data["chapters"], list):
        data["chapters"] = []
    return data


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(BIOGRAPHY_PATH) or ".", exist_ok=True)
    with open(BIOGRAPHY_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_bookkeeping(key: str, default=None):
    with _biography_lock:
        return _load().get(key, default)


def set_bookkeeping(key: str, value) -> None:
    with _biography_lock:
        data = _load()
        data[key] = value
        _save(data)


def get_chapters(limit: int | None = None) -> list[dict]:
    with _biography_lock:
        chapters = list(_load()["chapters"])
    return chapters[-limit:] if limit else chapters


def add_chapter(narrative: str, period_start: str, period_end: str, based_on: dict) -> dict:
    """Appends one narrative chapter — never edits a past one (a biography
    doesn't rewrite its own earlier pages; if a later chapter reinterprets
    an earlier period, that reinterpretation is itself a new chapter,
    exactly like a person's understanding of their own past changing
    without the past itself being edited). Capped at _MAX_CHAPTERS, oldest
    dropped first — the earliest chapters matter least to who she is NOW,
    same reasoning as core/memory_user_model.py's list-field cap."""
    chapter = {
        "id": uuid.uuid4().hex[:12], "created_at": _now_iso(),
        "period_start": period_start, "period_end": period_end,
        "narrative": narrative.strip(), "based_on": based_on,
    }
    with _biography_lock:
        data = _load()
        data["chapters"].append(chapter)
        data["chapters"] = data["chapters"][-_MAX_CHAPTERS:]
        data["updated_at"] = chapter["created_at"]
        _save(data)
    return chapter


def format_biography_block(limit: int = 3) -> str:
    """Reactive prompt-facing block — the most recent chapters, oldest
    first (read the way a story is read). Empty until at least one chapter
    has genuinely been written."""
    chapters = get_chapters(limit)
    if not chapters:
        return ""
    return "\n\n".join(f"[{c['period_start']} — {c['period_end']}] {c['narrative']}" for c in chapters)
