# ═══════════════════════════════════════════════════════════════════════════
# EPISTEMICS — Entity Pillars Phase 1: read-side unification of the three
# tiers of "what HUGO knows about Joan" that already exist as separate
# stores, each grown independently for its own purpose:
#
#   KNOWN     — data/memory_shared.json / memory_<personality>.json facts
#               with epistemic='stated' (see core/memory_store.py's
#               _normalize_fact) — Joan said this himself.
#   BELIEVED  — data/memory_shared.json facts with epistemic='inferred'
#               (extracted by core/reflective.py from a pattern, not a
#               direct statement), plus data/user_model.json — HUGO's own
#               synthesis of who Joan is (see core/memory_user_model.py).
#   SUSPECTED — data/investigations.json hypotheses (see
#               core/investigations.py) — explicit, confidence-scored, and
#               still being actively tested.
#
# Nothing here writes to any of those three stores — each already has its
# own write path (memory_extract.py, scripts/reflective_mode.py's user-model
# sub-phase, core/sleep_phases_incubation.py) and its own supersession
# mechanism ('outdated'/'outdated_reason' on facts, the append-only
# USER_MODEL_HISTORY_PATH log on the user model, 'confidence' drift on
# hypotheses). This module only reads across all three with one shared
# confidence scale, and formats them for prompt injection so a personality
# can distinguish "I know" from "I believe" from "I suspect" instead of
# presenting everything as flat fact.
#
# Dependency-light (stdlib + the three already-dependency-light modules it
# reads from), same discipline as core/memory_user_model.py, so this can be
# imported from scripts/reflective_mode.py as easily as from the live app.
# ═══════════════════════════════════════════════════════════════════════════
from core.investigations import get_active_investigations
from core.memory_store import MEMORY_SHARED_PATH, _keywords, _load_fact_file
from core.memory_user_model import get_user_model

# Baseline confidence per tier, before any per-item adjustment — a stated
# fact Joan said outright is treated as ground truth; an inferred fact or
# user-model claim is HUGO's own synthesis and starts lower; a hypothesis
# carries its own explicit confidence from core/investigations.py.
_STATED_CONFIDENCE = 1.0
_INFERRED_BASE_CONFIDENCE = 0.6


def confidence_of(fact: dict) -> float:
    """Normalized 0-1 confidence for a memory_shared/memory_<personality>
    fact dict, on the same scale core/investigations.py already uses for
    hypotheses. 'stated' facts are ground truth (1.0) unless marked
    outdated (superseded, so no longer trusted at all). 'inferred' facts
    start at _INFERRED_BASE_CONFIDENCE and are nudged by 'weight' —
    repeated reinforcement is HUGO's only signal that a pattern she noticed
    keeps holding up (deliberately NOT 'importance', which measures
    salience — how much a fact matters if true — not how sure she is it IS
    true)."""
    if fact.get("outdated"):
        return 0.0
    if fact.get("epistemic") != "inferred":
        return _STATED_CONFIDENCE
    weight = int(fact.get("weight") or 1)
    return min(1.0, _INFERRED_BASE_CONFIDENCE + 0.05 * (weight - 1))


def _matches(fact_text: str, keywords: set[str] | None) -> bool:
    return not keywords or bool(_keywords(fact_text) & keywords)


def get_belief_summary(query: str | None = None, max_per_tier: int = 3) -> dict:
    """Cross-tier snapshot: {'known': [...], 'believed': [...],
    'suspected': [...]}, each a list of {'text', 'confidence'} dicts sorted
    by confidence descending, capped at *max_per_tier*. When *query* is
    given, every tier is filtered to items sharing a keyword with it (same
    keyword-overlap approach core.memory_select already uses for relevance)
    — otherwise the highest-confidence items across the board are
    returned. Outdated facts are always excluded."""
    keywords = _keywords(query) if query else None

    shared_facts = [
        f for f in _load_fact_file(MEMORY_SHARED_PATH, default_category="personal")
        if not f.get("outdated") and _matches(f["fact"], keywords)
    ]
    known = sorted(
        ({"text": f["fact"], "confidence": confidence_of(f)}
         for f in shared_facts if f.get("epistemic") != "inferred"),
        key=lambda x: x["confidence"], reverse=True,
    )[:max_per_tier]
    believed = [
        {"text": f["fact"], "confidence": confidence_of(f)}
        for f in shared_facts if f.get("epistemic") == "inferred"
    ]

    model = get_user_model()
    for field in ("thinking_style", "work_style", "communication_preferences", "relationship_with_hugo"):
        text = model.get(field)
        if text and _matches(text, keywords):
            believed.append({"text": text, "confidence": _INFERRED_BASE_CONFIDENCE})
    believed = sorted(believed, key=lambda x: x["confidence"], reverse=True)[:max_per_tier]

    suspected = []
    for inv in get_active_investigations():
        for h in inv.get("hypotheses") or []:
            text = h.get("text", "")
            if text and _matches(text, keywords):
                suspected.append({"text": text, "confidence": float(h.get("confidence") or 0.0)})
    suspected = sorted(suspected, key=lambda x: x["confidence"], reverse=True)[:max_per_tier]

    return {"known": known, "believed": believed, "suspected": suspected}


def format_epistemic_block(query: str | None = None, max_per_tier: int = 2) -> str:
    """Compact prompt-facing block distinguishing what HUGO knows/believes/
    suspects about *query* (or, if omitted, in general). Returns '' when
    there's nothing to say in any tier — never injects an empty section."""
    summary = get_belief_summary(query, max_per_tier)
    lines = []
    if summary["known"]:
        lines.append("Sabes (Joan lo dijo): " + "; ".join(i["text"] for i in summary["known"]))
    if summary["believed"]:
        lines.append("Crees (tu propia lectura, no confirmada): " + "; ".join(i["text"] for i in summary["believed"]))
    if summary["suspected"]:
        lines.append("Sospechas (hipótesis activa, aún en duda): " + "; ".join(i["text"] for i in summary["suspected"]))
    return "\n".join(lines)
