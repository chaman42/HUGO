# ═══════════════════════════════════════════════════════════════════════════
# MEMORY STATS — memory health/stats endpoints, the full active-memory
# snapshot, the think-log reader, and weekly Layer 1/2 consolidation. Split
# out of core/memory.py (pure refactor, no behavior change).
# ═══════════════════════════════════════════════════════════════════════════
import json
import logging
import os
import datetime

from core.memory_store import (
    MEMORY_SHARED_PATH,
    MEMORY_LIRA_PATH,
    _MEMORY_HEALTH_WARN_THRESHOLD,
    _TEMPORAL_FACT_PATTERNS,
    _memory_lock,
    _now_iso,
    _normalize_fact,
    _dedup_facts,
    _load_fact_file,
    _save_fact_file,
)
from core.memory_context import CONCEPTS_PATH
from core.memory_episodes import _load_episodes

logger = logging.getLogger(__name__)


def _memory_file_map() -> dict[str, str]:
    return {
        "shared": MEMORY_SHARED_PATH,
        "lira":   MEMORY_LIRA_PATH,
    }


def _log_memory_health() -> None:
    """Log a fact count per Layer 1/2 file at startup; warn if any file has
    grown past _MEMORY_HEALTH_WARN_THRESHOLD and likely needs a
    POST /api/memory_clean run.

    Messages are prefixed "[MEMORY]" so core.server's SocketIOLogHandler
    routes them to the maintenance/system panel (via _OP_PREFIXES) instead of
    the main chat — these are operational diagnostics, not assistant replies,
    and were previously leaking into the chat panel on every startup.
    """
    for name, path in _memory_file_map().items():
        count = len(_load_fact_file(path, default_category="personal"))
        if count > _MEMORY_HEALTH_WARN_THRESHOLD:
            logger.warning(
                "[MEMORY] %s has %d facts (> %d) — consider POST /api/memory_clean",
                name, count, _MEMORY_HEALTH_WARN_THRESHOLD,
            )
        else:
            logger.info("[MEMORY] %s has %d facts", name, count)


# Run once at import time so counts show up in the log on every startup.
_log_memory_health()


_STALE_ACCESS_DAYS   = 90   # "not accessed" threshold — matches _fact_usage_score's decay window
_STALE_REVIEW_DAYS   = 60   # sleep's "flag for review" threshold — see core/sleep_phases_memory.py


def _fact_age_days(fact: dict) -> float | None:
    """Age in days since 'created_at' (falls back to 'added') — None if
    neither is present/parseable."""
    created = fact.get("created_at") or fact.get("added")
    if not created:
        return None
    try:
        dt = datetime.datetime.fromisoformat(created)
    except ValueError:
        return None
    return (datetime.datetime.now() - dt).total_seconds() / 86400


def _fact_days_since_used(fact: dict) -> float | None:
    """Days since 'last_used' — falls back to 'added' (never-used facts are
    'stale' relative to when they were last touched at all). None if
    neither is present/parseable."""
    reference = fact.get("last_used") or fact.get("added")
    if not reference:
        return None
    try:
        dt = datetime.datetime.fromisoformat(reference)
    except ValueError:
        return None
    return (datetime.datetime.now() - dt).total_seconds() / 86400


def get_memory_stats() -> dict:
    """Return fact counts, category breakdown, average weight, and Memory V2
    Part B usage-health metrics (average fact age, most/least used facts,
    facts not accessed in 90+ days) per Layer 1/2 file, plus an overall
    '_usage' summary across every file combined. Backs GET /api/memory_stats."""
    stats = {}
    all_facts: list[dict] = []
    for name, path in _memory_file_map().items():
        facts = _load_fact_file(path, default_category="personal")
        all_facts.extend(facts)
        categories: dict[str, int] = {}
        total_weight = 0
        for f in facts:
            categories[f["category"]] = categories.get(f["category"], 0) + 1
            total_weight += f.get("weight", 1)

        ages = [a for a in (_fact_age_days(f) for f in facts) if a is not None]
        by_use = sorted(facts, key=lambda f: f.get("use_count", 0), reverse=True)
        not_accessed = [
            f for f in facts
            if (d := _fact_days_since_used(f)) is not None and d > _STALE_ACCESS_DAYS
        ]

        stats[name] = {
            "count":         len(facts),
            "categories":    categories,
            "avg_weight":    round(total_weight / len(facts), 2) if facts else 0,
            "needs_cleanup": len(facts) > _MEMORY_HEALTH_WARN_THRESHOLD,
            "avg_fact_age_days":   round(sum(ages) / len(ages), 1) if ages else 0,
            "most_used":     [{"fact": f["fact"], "use_count": f.get("use_count", 0)} for f in by_use[:5] if f.get("use_count", 0) > 0],
            "least_used":    [{"fact": f["fact"], "use_count": f.get("use_count", 0)} for f in by_use[-5:] if f.get("use_count", 0) == 0] if facts else [],
            "not_accessed_90d": len(not_accessed),
        }

    overall_ages = [a for a in (_fact_age_days(f) for f in all_facts) if a is not None]
    by_use_overall = sorted(all_facts, key=lambda f: f.get("use_count", 0), reverse=True)
    not_accessed_overall = [
        f for f in all_facts
        if (d := _fact_days_since_used(f)) is not None and d > _STALE_ACCESS_DAYS
    ]
    stats["_usage"] = {
        "total_facts":         len(all_facts),
        "avg_fact_age_days":   round(sum(overall_ages) / len(overall_ages), 1) if overall_ages else 0,
        "most_used":  [{"fact": f["fact"], "use_count": f.get("use_count", 0)} for f in by_use_overall[:5] if f.get("use_count", 0) > 0],
        "least_used": [{"fact": f["fact"], "use_count": f.get("use_count", 0)} for f in by_use_overall[-5:] if f.get("use_count", 0) == 0] if all_facts else [],
        "not_accessed_90d":    len(not_accessed_overall),
    }
    return stats


def clean_all_memory() -> dict:
    """Run semantic dedup (already applied by _load_fact_file on every read)
    plus temporal-fact removal on every Layer 1/2 file, and rewrite them.
    Backs POST /api/memory_clean. Returns {name: {"before": n, "after": n}}."""
    result = {}
    with _memory_lock:
        for name, path in _memory_file_map().items():
            facts  = _load_fact_file(path, default_category="personal")
            before = len(facts)
            cleaned = [f for f in facts if not any(p.search(f["fact"]) for p in _TEMPORAL_FACT_PATTERNS)]
            _save_fact_file(path, cleaned)
            result[name] = {"before": before, "after": len(cleaned)}
    return result


def get_active_memory() -> dict:
    """Currently stored facts (shared Layer 1 + LIRA's own Layer 2, grouped
    by category), the 5 most recent episodes, current concepts, and the
    live HUD context — backs GET /api/memory_active for both the CORE app's
    Memoria tab and its Mapa Mental graph (see ui/index.html
    _renderCoreMapa). This is the full read-only pool of what COULD be
    injected, not a live relevance-filtered slice tied to one specific
    query — there isn't a "current query" concept outside an active
    dispatch_command call, and this endpoint is a standalone status view,
    not part of the prompt pipeline itself."""
    facts_by_category: dict[str, list[dict]] = {}
    for path in (MEMORY_SHARED_PATH, MEMORY_LIRA_PATH):
        for f in _load_fact_file(path, default_category="personal"):
            facts_by_category.setdefault(f["category"], []).append(f)

    episodes = sorted(_load_episodes(), key=lambda e: e.get("date", ""), reverse=True)[:5]

    # Concepts — same source and 'type' normalization as GET /api/concepts
    # (see core.server.api_concepts_get), read directly here rather than
    # via an HTTP round-trip since this already runs in-process.
    try:
        with open(CONCEPTS_PATH, "r", encoding="utf-8") as f:
            concepts = json.load(f).get("concepts", [])
        for c in concepts:
            if isinstance(c, dict) and c.get("type") != "general":
                c["type"] = "armor"
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        logger.debug("Could not load concepts for active memory: %s", exc)
        concepts = []

    try:
        import core.server as server_mod
        hud_context = server_mod.get_hud_context()
    except Exception:
        hud_context = {}

    return {
        "facts":       facts_by_category,
        "episodes":    episodes,
        "concepts":    concepts,
        "hud_context": hud_context,
    }


ACTIVITY_LOG_PATH = "logs/activity.log"
_THINK_LOG_TAG     = "[THINK_LOG] "


def get_think_log(limit: int = 10) -> list[dict]:
    """Last `limit` thinking blocks, newest first — parsed back out of the
    [THINK_LOG] lines _groq_complete() writes to logs/activity.log (a
    dedicated INFO-level tag; the plain [THINK] line stays DEBUG-only and
    never reaches that file — see jarvis.py's RotatingFileHandler level).
    Backs GET /api/think_log for the CORE app's Pensamiento tab. Returns []
    if the log is missing or nothing has been logged yet — e.g. every
    reply so far came from a non-reasoning GROQ_MODEL_CHAIN tier, which the
    frontend shows as 'Modelo sin razonamiento visible'."""
    try:
        with open(ACTIVITY_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []

    entries: list[dict] = []
    for line in reversed(lines):
        idx = line.find(_THINK_LOG_TAG)
        if idx == -1:
            continue
        try:
            entries.append(json.loads(line[idx + len(_THINK_LOG_TAG):]))
        except (json.JSONDecodeError, ValueError):
            continue
        if len(entries) >= limit:
            break
    return entries


# ---------------------------------------------------------------------------
# Weekly memory consolidation — checked from _proactive_loop, so "if the Mac
# is on" is automatically satisfied: it can only run while this process is
# alive, once per week, the first tick that lands on Sunday 03:00–03:59.
# Reviews memory_shared.json and LIRA's memory_lira.json (per spec — Layer 2
# for the other personalities stays manually curated, same as everywhere
# else in this file), permanently dropping outdated facts (see
# _mark_fact_outdated) and merging any remaining near-duplicates — the same
# semantic dedup _load_fact_file already applies transiently on every read;
# consolidation just makes it permanent by writing the cleaned set back to
# disk. Logs a brief summary to logs/memory_consolidation.log.
# ---------------------------------------------------------------------------

CONSOLIDATION_LOG_PATH = "logs/memory_consolidation.log"
_last_consolidation_date: str | None = None


def _consolidate_memory_file(path: str, default_category: str) -> tuple[int, int, int]:
    """Returns (outdated_removed, duplicates_merged, remaining_count).
    Caller must hold _memory_lock."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return (0, 0, 0)
    if not isinstance(raw, list):
        return (0, 0, 0)

    facts        = [nf for nf in (_normalize_fact(x, default_category) for x in raw) if nf]
    before_count = len(facts)

    current           = [f for f in facts if not f.get("outdated")]
    outdated_removed  = before_count - len(current)

    deduped            = _dedup_facts(current)   # keeps newest per similarity cluster
    duplicates_merged  = len(current) - len(deduped)

    _save_fact_file(path, deduped)
    return (outdated_removed, duplicates_merged, len(deduped))


def _consolidate_memory() -> None:
    """Weekly pass over Layer 1 (memory_shared.json) and LIRA's Layer 2
    (memory_lira.json) — see module comment above."""
    lines = [f"=== Memory consolidation — {_now_iso()} ==="]
    with _memory_lock:
        for label, path, default_category in (
            ("shared", MEMORY_SHARED_PATH, "personal"),
            ("lira",   MEMORY_LIRA_PATH,   "context"),
        ):
            try:
                outdated_removed, duplicates_merged, remaining = _consolidate_memory_file(path, default_category)
                lines.append(
                    f"{label}: removed {outdated_removed} outdated, merged {duplicates_merged} "
                    f"duplicate(s), {remaining} fact(s) remain"
                )
            except Exception as e:
                lines.append(f"{label}: FAILED — {e}")

    try:
        os.makedirs(os.path.dirname(CONSOLIDATION_LOG_PATH) or ".", exist_ok=True)
        with open(CONSOLIDATION_LOG_PATH, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n\n")
    except Exception:
        logger.warning("Could not write memory_consolidation.log", exc_info=True)

    logger.info("[MEMORY] Weekly consolidation complete")


def _maybe_run_weekly_consolidation() -> None:
    """Checked once per _proactive_loop tick (every 30 min). Fires at most
    once per week — the first tick landing on Sunday 03:00–03:59."""
    global _last_consolidation_date
    now = datetime.datetime.now()
    if now.weekday() != 6 or now.hour != 3:   # Sunday, 03:00–03:59
        return
    today = now.date().isoformat()
    if _last_consolidation_date == today:
        return
    _last_consolidation_date = today
