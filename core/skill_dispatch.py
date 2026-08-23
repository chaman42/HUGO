# SKILL DISPATCH — wires the skills/ package (see skills/__init__.py) into
# the live conversation pipeline in core/commands.py. Two paths, per spec
# ("a bit of both, she can decide if i ask something to use various skills
# to acomplish the request, and the user can also ask for a specific
# skill"):
#
#   EXPLICIT — the user names a skill directly (their wording matches one
#   of its `triggers` phrases). Deterministic substring match, no LLM
#   call — same tier as intent._detect_intent's other regex/substring
#   shortcuts, just checked one step later (only once intent detection has
#   already come back "unknown", so a real deterministic intent like
#   volume_control is never shadowed by an accidental trigger overlap).
#
#   IMPLICIT — for an open-ended request with no explicit trigger match,
#   LIRA herself may decide a loaded skill fits. She's told what's
#   available via a "CONTEXTO OPCIONAL" block (same pattern as
#   core.commands._build_social_skills_context et al.) appended to
#   user_content, and signals her decision back with a `[USAR_SKILL:
#   nombre]` line in her own reply — see build_skills_awareness_context /
#   extract_skill_directive.
import logging
import re

import skills

logger = logging.getLogger(__name__)

_SKILL_DIRECTIVE_RE = re.compile(r"\[USAR_SKILL:\s*([a-zA-Z0-9_\-]+)\]")


def detect_explicit_skill_request(transcript: str) -> str | None:
    """Name of the first currently-enabled skill whose trigger phrase
    appears (case-insensitive substring) in `transcript`, or None."""
    low = transcript.lower()
    for skill in skills.list_skills(enabled_only=True):
        for trigger in skill.triggers:
            if trigger and trigger.lower() in low:
                return skill.name
    return None


def build_skills_awareness_context() -> str | None:
    """None if no skill is currently loaded+enabled; otherwise a
    'CONTEXTO OPCIONAL' block listing each one's name/description, so
    LIRA can autonomously pick one for an open-ended request without the
    user naming it. She replies with a bare `[USAR_SKILL: nombre]` line
    when she wants to invoke one — see extract_skill_directive."""
    loaded = skills.list_skills(enabled_only=True)
    if not loaded:
        return None
    lines = "\n".join(f"- {s.name}: {s.description}" for s in loaded)
    return (
        "[CONTEXTO OPCIONAL — capacidades adicionales disponibles: si la petición "
        "del usuario se resuelve mejor con una de estas, responde ÚNICAMENTE con "
        f"una línea `[USAR_SKILL: nombre]` y nada más (ni explicación, ni texto "
        f"antes o después):\n{lines}]"
    )


def extract_skill_directive(reply: str) -> str | None:
    """The skill name LIRA's own reply asked to invoke, if any — only
    honored if that skill is still loaded+enabled right now (flags can
    flip between the context being built and the reply coming back)."""
    if not reply:
        return None
    m = _SKILL_DIRECTIVE_RE.search(reply)
    if not m:
        return None
    name = m.group(1)
    return name if skills.get_skill(name) else None


def run_skill(name: str, query: str, context: dict | None = None) -> str | None:
    """Executes the named skill, returning its reply text — None if the
    skill isn't loaded/enabled, or if execute() raised."""
    skill = skills.get_skill(name)
    if skill is None:
        return None
    try:
        return skill.execute(query, context or {})
    except Exception:
        logger.warning("skill_dispatch: %s.execute() failed", name, exc_info=True)
        return None
