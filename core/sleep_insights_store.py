"""Sleep System — insights file (data/sleep_insights.json) storage:
patterns/ideas/questions/curiosity/autocritica records, plus the
continuous-sleep run summary used by NUCLEO LIRA's Estado/Pensamiento tabs."""
import datetime

from core.sleep_state import (
    INSIGHTS_PATH, _load_json, _save_json, _now_iso, load_continuous_state,
)

# Which cycle is currently in flight (continuous mode only) — set directly on
# this module by core/sleep.py's run_continuous_sleep() right before each
# cycle starts (`sleep_insights_store._current_cycle = n`), read here by
# _add_insight() so every note written during that cycle is tagged with it.
# None outside continuous mode.
_current_cycle: int | None = None


# Category -> display label, for get_sleep_insights_summary()'s
# "reflections" list (patterns + ideas + autocritica combined — curiosity
# is deliberately excluded, it's not "insight"/"autocritical" in nature
# per spec's own wording for the REFLEXIONES DEL SUEÑO panel).
_REFLECTION_PHASE_LABEL = {
    "patterns":    "🔍 Descubrimiento de Patrones",
    "ideas":       "💡 Generador de Ideas",
    "autocritica": "🪞 Autocrítica",
}

def get_sleep_insights_summary(limit: int = 20) -> dict:
    """Backs GET /api/sleep_insights — NÚCLEO LIRA's Pensamiento tab
    ('PREGUNTAS DURANTE EL SUEÑO' / 'REFLEXIONES DEL SUEÑO'). Questions are
    ordered by confidence (importance) descending, per spec; reflections
    (patterns + ideas + autocritica combined) by recency descending. Both
    capped at *limit* — these lists grow unbounded over many sleep cycles
    (real data already has 90+ questions on file), so this always returns
    a manageable page rather than the entire history.

    'resolved' mirrors the existing 'used' status (flipped by
    mark_question_used() the moment a question is actually surfaced to
    Joan in conversation — see core/commands.py's system-prompt
    injection) — there's no separate "did he actually answer it" signal
    today, so "asked" is treated as "resolved" for display purposes.
    'cycle' is None for any entry written before this feature (or by the
    old one-shot run_sleep_session() path) — see _add_insight()."""
    data = load_insights()

    questions = sorted(
        (q for q in data.get("questions", []) if isinstance(q, dict) and q.get("text")),
        key=lambda q: q.get("confidence", 0), reverse=True,
    )[:limit]
    questions_out = [
        {
            "text":       q["text"],
            "confidence": q.get("confidence"),
            "cycle":      q.get("cycle"),
            "resolved":   q.get("status") == "used",
            "added":      q.get("added"),
        }
        for q in questions
    ]

    reflections = []
    for category, label in _REFLECTION_PHASE_LABEL.items():
        for item in data.get(category, []):
            if not isinstance(item, dict) or not item.get("text"):
                continue
            reflections.append({
                "text":       item["text"],
                "confidence": item.get("confidence"),
                "cycle":      item.get("cycle"),
                "phase":      label,
                "added":      item.get("added"),
            })
    reflections.sort(key=lambda r: r.get("added") or "", reverse=True)

    return {"questions": questions_out, "reflections": reflections[:limit]}

def get_sleep_summary() -> dict:
    """Backs GET /api/sleep_summary — a purpose-built summary of the LAST
    (or currently running) continuous-sleep run, for NÚCLEO LIRA's Estado
    "ÚLTIMO SUEÑO" section: when it happened, how many cycles, how long,
    and what it actually did (facts deleted/merged/promoted, insights
    generated, mind-map connections touched). Every cumulative field here
    resets to 0 at the start of each new continuous run (same convention
    as total_cycles_completed, see _default_continuous_state) — this
    describes the most recent run, not an all-time lifetime total.

    'has_ever_slept' is False only if no continuous run has ever started
    (started_at is None) — the frontend shows 'Sin ciclos de sueño
    registrados aún' instead of a zeroed-out summary in that case."""
    state = load_continuous_state()
    has_ever_slept = state.get("started_at") is not None

    duration_seconds = None
    if state.get("started_at"):
        try:
            start = datetime.datetime.fromisoformat(state["started_at"])
            if state.get("running"):
                duration_seconds = max(0.0, (datetime.datetime.now() - start).total_seconds())
            elif state.get("stopped_at"):
                # Only compute a finished-run duration when we actually
                # recorded when it stopped — a run that completed before
                # this field existed (stopped_at missing) leaves this None
                # rather than fabricating a duration against "now", which
                # would read as many hours/days for an old run.
                end = datetime.datetime.fromisoformat(state["stopped_at"])
                duration_seconds = max(0.0, (end - start).total_seconds())
        except (ValueError, TypeError):
            duration_seconds = None

    return {
        "has_ever_slept":           has_ever_slept,
        "started_at":               state.get("started_at"),
        "stopped_at":               state.get("stopped_at"),
        "duration_seconds":         duration_seconds,
        "total_cycles_completed":   state.get("total_cycles_completed", 0),
        "total_deleted":            state.get("total_deleted", 0),
        "total_merged":             state.get("total_merged", 0),
        "total_promoted":           state.get("total_promoted", 0),
        "total_insights_generated": state.get("total_insights_generated", 0),
        "total_mind_map_updates":   state.get("total_mind_map_updates", 0),
        "stop_reason":              state.get("stop_reason"),
        "trigger":                  state.get("trigger"),
        "current": {
            "running":           state.get("running", False),
            "current_cycle":     state.get("current_cycle", 0),
            "current_phase_num": state.get("current_phase_num", 0),
            "current_phase":     state.get("current_phase", ""),
        },
    }

def _default_insights() -> dict:
    return {
        "patterns": [], "ideas": [], "questions": [], "curiosity": [],
        "autocritica": [], "curiosidad_findings": [],
    }

def load_insights() -> dict:
    data = _load_json(INSIGHTS_PATH, None)
    if not isinstance(data, dict):
        data = _default_insights()
    for key in _default_insights():
        if not isinstance(data.get(key), list):
            data[key] = []
    return data

def save_insights(data: dict) -> None:
    _save_json(INSIGHTS_PATH, data)

def _add_insight(category: str, text: str, confidence: float) -> None:
    data = load_insights()
    data[category].append({
        "text": text, "added": _now_iso(), "status": "new",
        "confidence": round(confidence, 2),
        "cycle": _current_cycle,   # which continuous-sleep cycle wrote this — None outside continuous mode
    })
    save_insights(data)

def get_unused_question() -> tuple[int | None, str | None]:
    """Oldest not-yet-surfaced pending question, as (index, text) —
    (None, None) if there isn't one. See mark_question_used()."""
    data = load_insights()
    for i, q in enumerate(data["questions"]):
        if q.get("status") != "used":
            return i, q.get("text")
    return None, None

def mark_question_used(idx: int) -> None:
    data = load_insights()
    if 0 <= idx < len(data["questions"]):
        data["questions"][idx]["status"] = "used"
        save_insights(data)

def get_unused_curiosity() -> tuple[int | None, str | None]:
    """Oldest not-yet-surfaced curiosity note, as (index, text) —
    (None, None) if there isn't one. See mark_curiosity_used()."""
    data = load_insights()
    for i, c in enumerate(data["curiosity"]):
        if c.get("status") != "used":
            return i, c.get("text")
    return None, None

def mark_curiosity_used(idx: int) -> None:
    data = load_insights()
    if 0 <= idx < len(data["curiosity"]):
        data["curiosity"][idx]["status"] = "used"
        save_insights(data)

def get_unused_curiosidad_finding() -> tuple[int | None, str | None]:
    """Oldest not-yet-mentioned web-search curiosidad finding (see
    core.sleep_curiosity_search._phase_curiosity_search), as (index, text) —
    (None, None) if there isn't one. Distinct from get_unused_curiosity()
    above (Phase 8's old plain-topic suggestions, no web search behind
    them) — both get injected into LIRA's prompt, see
    core/personalities/base.py."""
    data = load_insights()
    for i, c in enumerate(data["curiosidad_findings"]):
        if c.get("status") != "used":
            return i, c.get("text")
    return None, None

def mark_curiosidad_finding_used(idx: int) -> None:
    data = load_insights()
    if 0 <= idx < len(data["curiosidad_findings"]):
        data["curiosidad_findings"][idx]["status"] = "used"
        save_insights(data)
