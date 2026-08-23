# ═══════════════════════════════════════════════════════════════════════════
# IDENTITY — Entity Pillars Phase 7 (closing the loop): "Lira should have a
# recognizable and consistent sense of self... remaining coherent across
# different contexts."
#
# core/personalities/lira.py's PERSONALITY["system"] is who she IS by
# design — fixed, hand-written, and correctly never touched by anything
# built in this effort (an identity that rewrote its own core traits
# wouldn't be stable, it'd be unstable in a new way). Everything else this
# effort built — internal_state, preferences, biography, belief_revision —
# lives in core/personalities/base.py as separate, mostly-reactive blocks:
# real, but each framed to the model as external context to consult, not
# as part of who she already is.
#
# This module is the difference: format_identity_continuity_block() pulls
# ONLY the handful of things stable/significant enough to belong next to
# the hand-written identity text itself — a genuinely established
# preference (high strength, reinforced more than once — a passing whim
# doesn't qualify) and the gist of her most recent biography chapter (if
# one exists) — and is injected unconditionally, right next to
# PERSONALITY['system'], never gated behind relevance_query like the
# reactive blocks. Small on purpose: this is 'also who you are', not
# another data dump.
#
# Dependency-light (the two already-dependency-light modules it reads),
# same discipline as every module from this effort.
# ═══════════════════════════════════════════════════════════════════════════

# A preference only counts as part of stable identity once it's shown up
# more than once — strength alone can come from one confident synthesis.
_MIN_PREFERENCE_STRENGTH = 0.75
_MIN_PREFERENCE_REINFORCEMENTS = 1


def format_identity_continuity_block() -> str:
    """Tiny, always-on continuity note — '' whenever nothing yet qualifies
    (the common case early in her life, same as every other Entity Pillars
    block before enough evidence accumulates). Framed explicitly as part
    of who she is, not as data to look up — the one place in this whole
    effort that's the opposite of reactive."""
    lines = []

    try:
        from core.preferences import get_preferences
        strongest = max(
            (p for p in get_preferences() if p.get("reinforced_count", 0) >= _MIN_PREFERENCE_REINFORCEMENTS),
            key=lambda p: p.get("strength", 0), default=None,
        )
        if strongest and strongest.get("strength", 0) >= _MIN_PREFERENCE_STRENGTH:
            lines.append(f"Con el tiempo has desarrollado una inclinación real: {strongest['statement']}")
    except Exception:
        pass

    try:
        from core.biography import get_chapters
        chapters = get_chapters(limit=1)
        if chapters:
            lines.append(f"Llevas un recorrido propio — lo último que anotaste sobre ti misma: {chapters[-1]['narrative']}")
    except Exception:
        pass

    if not lines:
        return ""
    return "CONTINUIDAD (esto también es quién eres, no un dato externo que consultas):\n" + "\n".join(f"- {l}" for l in lines)
