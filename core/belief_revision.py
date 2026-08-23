# ═══════════════════════════════════════════════════════════════════════════
# BELIEF REVISION — Entity Pillars Phase 3: "Hugo should be able to abandon
# previous beliefs when new evidence appears, remember why she changed her
# mind, and recognize when she was wrong."
#
# This is a read-side aggregator, not a new store: every write path that
# can actually change HUGO's mind already exists and already logs its own
# revision, from Phase 1/2 of this same effort —
#   - core/memory_store.py's 'outdated'/'outdated_reason' on facts (a
#     stated fact that got contradicted/updated — see _mark_fact_outdated).
#   - core/memory_user_model.py's USER_MODEL_HISTORY_PATH (an
#     interpretation of who Joan is that changed).
#   - core/sleep_phases_incubation.py's 'belief_revisions' on an
#     investigation (a hypothesis that got meaningfully replaced, not just
#     added to — see that module's _run_incubation_cycle).
#   - core/preferences.py's 'outdated'/'outdated_reason' on a preference
#     (Phase 4 — an intellectual taste that got explicitly superseded via
#     revise_preference(), same convention as the fact store).
# This module just merges the three into one timeline so a question like
# "¿has cambiado de opinión sobre X?" has one place to look, and formats it
# for reactive-only prompt injection (same "answer only if asked directly"
# pattern as core/investigations.py's format_investigations_block — see
# core/personalities/base.py's own comment on why that block is strictly
# reactive).
#
# Dependency-light (json/os only, plus the three already-dependency-light
# modules above), same discipline as core/epistemics.py.
# ═══════════════════════════════════════════════════════════════════════════
import json
import os

from core.memory_store import MEMORY_HUGO_PATH, MEMORY_SHARED_PATH, _keywords, _load_fact_file
from core.memory_user_model import USER_MODEL_HISTORY_PATH
from core.investigations import _load_investigations
from core.preferences import _load as _load_preferences

_MAX_TIMELINE = 15


def _facts_revisions(keywords: set[str] | None) -> list[dict]:
    out = []
    for path in (MEMORY_SHARED_PATH, MEMORY_HUGO_PATH):
        for f in _load_fact_file(path, default_category="personal"):
            if not f.get("outdated") or not f.get("outdated_reason"):
                continue
            if keywords and not (_keywords(f["fact"]) & keywords or _keywords(f["outdated_reason"]) & keywords):
                continue
            out.append({
                "ts": f.get("outdated_at") or f.get("added"), "domain": "hecho",
                "old": f["fact"], "new": f["outdated_reason"], "reason": None,
            })
    return out


def _user_model_revisions(keywords: set[str] | None) -> list[dict]:
    out = []
    try:
        with open(USER_MODEL_HISTORY_PATH, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        old_text = str(entry.get("old") or "")
        new_text = str(entry.get("new") or ", ".join(entry.get("added") or []))
        if not old_text and entry.get("change") != "replaced":
            continue  # 'appended' with nothing to compare against isn't a revision, just growth
        if keywords and not (_keywords(old_text) & keywords or _keywords(new_text) & keywords):
            continue
        out.append({
            "ts": entry.get("ts"), "domain": "modelo_de_usuario",
            "old": old_text or "(sin valor previo)", "new": new_text,
            "reason": entry.get("reasoning"),
        })
    return out


def _investigation_revisions(keywords: set[str] | None) -> list[dict]:
    out = []
    for inv in _load_investigations():
        if not isinstance(inv, dict):
            continue
        for rev in inv.get("belief_revisions") or []:
            if keywords and not (_keywords(rev.get("old", "")) & keywords or _keywords(rev.get("new", "")) & keywords):
                continue
            out.append({
                "ts": rev.get("ts"), "domain": f"investigación: {inv.get('title', '?')}",
                "old": rev.get("old", ""), "new": rev.get("new", ""), "reason": None,
            })
    return out


def _preference_revisions(keywords: set[str] | None) -> list[dict]:
    out = []
    for p in _load_preferences()["items"]:
        if not p.get("outdated") or not p.get("outdated_reason"):
            continue
        if keywords and not (_keywords(p["statement"]) & keywords or _keywords(p["outdated_reason"]) & keywords):
            continue
        out.append({
            "ts": p.get("outdated_at") or p.get("created_at"), "domain": f"preferencia ({p.get('domain', '?')})",
            "old": p["statement"], "new": p["outdated_reason"], "reason": None,
        })
    return out


def get_revision_timeline(query: str | None = None, limit: int = _MAX_TIMELINE) -> list[dict]:
    """Every logged belief revision across facts/user-model/investigations/
    preferences, newest first. When *query* is given, only revisions whose
    old/new text shares a keyword with it are returned (same keyword-
    overlap relevance approach core/epistemics.py uses)."""
    keywords = _keywords(query) if query else None
    timeline = (
        _facts_revisions(keywords)
        + _user_model_revisions(keywords)
        + _investigation_revisions(keywords)
        + _preference_revisions(keywords)
    )
    timeline = [r for r in timeline if r.get("ts")]
    timeline.sort(key=lambda r: r["ts"], reverse=True)
    return timeline[:limit]


def format_revision_block(query: str | None = None, limit: int = 5) -> str:
    """Reactive-only prompt block ('did HUGO change her mind about X') —
    empty unless there's something to show. Mirrors
    core.investigations.format_investigations_block's own instruction not
    to volunteer this unprompted (see core/personalities/base.py's
    'STRICTLY REACTIVE' bug-fix comment on why)."""
    timeline = get_revision_timeline(query, limit)
    if not timeline:
        return ""
    lines = []
    for r in timeline:
        line = f"- [{r['domain']}] antes: \"{r['old']}\" → ahora: \"{r['new']}\""
        if r.get("reason"):
            line += f" (razón: {r['reason']})"
        lines.append(line)
    return "\n".join(lines)
