"""Investigations skill — thin LiraSkill wrapper over core.investigations
(data/investigations.json lifecycle storage). Incubation itself still runs
during sleep (core/sleep_phases_incubation.py) — this skill only starts a
new investigation or lists the active ones.

`context` may carry {"action": "list"} to list active investigations
instead of creating a new one (default)."""
from skills import LiraSkill
from core import investigations


class InvestigationsSkill(LiraSkill):
    name = "investigations"
    description = "Inicia y consulta investigaciones de fondo, incubadas durante el sueño."
    # No explicit `triggers` on purpose. core/intent.py already owns
    # "start an investigation" with properly anchored regexes ('investiga
    # X', 'quiero saber sobre X', 'analiza X en profundidad', 'haz una
    # investigación sobre X' — see _INTENT_INVESTIGATE_RE and friends) and
    # runs before intent ever reaches "unknown" (where skill_dispatch's
    # EXPLICIT path lives). This skill previously duplicated those same
    # phrases here as unanchored substrings via
    # core.skill_dispatch.detect_explicit_skill_request — which matches
    # anywhere in the sentence, no negative lookahead — so any follow-up
    # question merely containing the word "investigación" (e.g. "¿qué tal
    # va esa investigación...?") silently started a brand new investigation
    # instead of being answered from the INVESTIGACIONES context block in
    # core/personalities/base.py. Leaving triggers empty keeps this skill
    # reachable only via the IMPLICIT path (LIRA deciding to use it,
    # signaled by [USAR_SKILL: investigations] in her own reply), which
    # doesn't have this false-positive problem.
    triggers = []

    def execute(self, query: str, context: dict) -> str:
        context = context or {}
        if context.get("action") == "list":
            active = investigations.get_active_investigations()
            if not active:
                return "No hay investigaciones activas ahora mismo."
            return ", ".join(inv["title"] for inv in active)

        inv = investigations.create_investigation(query)
        return f"Investigación iniciada: {inv['title']}."
