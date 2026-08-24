# ═══════════════════════════════════════════════════════════════════════════
# PERSONALITIES BASE — assembles the unified PERSONALITIES dict, and owns
# _build_system_prompt (assembles the system prompt string: character,
# capability instructions, live data, HUD/screen awareness, detected tone).
# Split out of core/personality.py (pure refactor, no behavior change).
#
# HUGO fork (2026-08-23): trimmed heavily from HUGO's original version —
# every block that read Joan's accumulated memory (episodes, biography,
# situation snapshot, investigations, preferences, belief revisions, armor/
# concepts knowledge, session-gap awareness, sleep insights) was removed
# along with the data files themselves, not just left to silently no-op.
# What's left is genuinely everything HUGO currently uses. See the comments
# at each removal site (search "HUGO fork") for what used to be there.
#
# core/personality.py imports PERSONALITIES/_build_system_prompt from here at
# the top level; this module reaches back into core.commands/core.activity
# only via function-local imports inside _build_system_prompt (same
# lazy-import pattern already used for core.listener/core.server below) to
# avoid a circular import.
# ═══════════════════════════════════════════════════════════════════════════
import logging
import time

from core import tools
# Module object (not `from x import name`) — jarvis.py's watchdog hot-reloads
# core/memory.py independently of this file (see its _MODULE_MAP), and a
# name-bound import here would keep pointing at the pre-reload function
# forever. Same reasoning as core/commands.py's own import block.
from core import memory

from core.personalities.prompts import (
    _REASONING_PREFIX,
    _CONTEXT_AWARENESS_PROMPT,
    _PERSONA_ANCHOR_PROMPT,
    _EPISTEMIC_HONESTY_PROMPT,
    _PERSONAL_BOUNDARIES_PROMPT,
    _ACTION_HONESTY_PROMPT,
)
from core.personalities.hugo import PERSONALITY as _HUGO_PERSONALITY

logger = logging.getLogger(__name__)

# HUGO is the only personality (JARVIS/FRIDAY removed 2026-08-10) — kept as
# a dict keyed by name rather than collapsing to a bare constant since
# _build_system_prompt/other call sites below still index it as
# PERSONALITIES[personality] in several places.
PERSONALITIES = {
    "hugo": _HUGO_PERSONALITY,
}


# (_session_gap_phrase / _time_of_day_phrase / _build_contexto_temporal /
# _CONTEXTO_TEMPORAL removed here — HUGO fork — they built the CONTEXTO
# TEMPORAL prompt block from session_state.json, wiped along with the rest
# of HUGO's memory.)


# ---------------------------------------------------------------------------
# System prompt builder — assembles the prompt in a fixed order:
#   1. Personality base persona (reasoning prefix prepended)
#   2. INSTRUCCIONES            (data/memory_instructions.json — human-edited
#                                 capability/limitation rules, still live)
#   3. DATOS EN TIEMPO REAL     (datetime, location, weather, session length)
#   4. ACTIVIDAD ACTUAL / PANTALLA ACTUAL (live HUD/screen context from the
#                                 frontend — not memory, just what's on
#                                 screen right now)
#   5. Tono detectado
#   6. Closing context-awareness instruction (_CONTEXT_AWARENESS_PROMPT)
# (Conversation history + summary are appended separately, after this
# string, by core.session._get_messages_with_history.)
# ---------------------------------------------------------------------------

# (_MODE_INSTRUCTIONS / _build_non_joan_system_prompt removed here — HUGO
# fork — the Joan-vs-stranger speaker-gated prompt variant they backed is
# gone too, see _build_system_prompt's own comment below.)


def _build_system_prompt(
    personality: str | None = None,
    tone: str | None = None,
    relevance_query: str | None = None,
) -> str:
    from core import personality as personality_mod

    if personality is None:
        with personality_mod._personality_lock:
            personality = personality_mod._personality

    # HUGO fork (2026-08-23): dropped the Joan-vs-everyone-else speaker
    # branch that used to live here (see git history / JarvisLite's
    # _build_non_joan_system_prompt for the original) — HUGO has no "Joan"
    # concept of his own, and core.social's speaker-presence data was wiped
    # along with the rest of HUGO's memory, so it would have defaulted into
    # the full path below unconditionally anyway. Always builds the one
    # simple prompt now.
    base = (
        _REASONING_PREFIX + " " + PERSONALITIES[personality]["system"] + " "
        + _EPISTEMIC_HONESTY_PROMPT + " " + _PERSONAL_BOUNDARIES_PROMPT + " "
        + _ACTION_HONESTY_PROMPT
    )

    # (Identity-continuity block dropped here — HUGO fork — it read
    # internal_state.json, wiped along with the rest of HUGO's memory.)

    # ── LAYER 3: INSTRUCCIONES — static, human-edited capability/limitation
    # rules (data/memory_instructions.json), hot-reloadable without restart.
    instructions_block = memory._build_instructions_block(personality)
    if instructions_block:
        base += "\n\nINSTRUCCIONES:\n" + instructions_block

    # ── INTERLOCUTOR ACTUAL — who core.social currently thinks is present
    # (core.social.SocialEngine.who_is_present(), updated every turn by
    # core.commands._dispatch_command_impl's identify_person() call, or by
    # the identity-code short-circuit — see core.social's own module
    # comments). Omitted entirely for Joan himself: the persona text above
    # already assumes Joan by default, so this only needs to say anything
    # when the answer is someone else. Best-effort — a lookup failure here
    # must never break the reply itself.
    try:
        from core import social as social_mod
        present = social_mod.social_engine.who_is_present()
        current_person = present[0] if present else None
    except Exception:
        current_person = None
    if current_person is not None and current_person.id != "joan":
        if current_person.id == "unknown":
            base += (
                "\n\nINTERLOCUTOR ACTUAL: no se ha podido identificar con quién "
                "hablas ahora mismo — no asumas que es Joan ni Dani. Trátalo "
                "con la reserva y neutralidad de alguien que no conoces, sin "
                "compartir nada privado de Joan."
            )
        else:
            behavior     = social_mod.social_engine.get_behavior_profile(current_person.id)
            permissions  = social_mod.social_engine.get_information_permissions(current_person.id)
            display_name = current_person.name or "esta persona"
            base += (
                f"\n\nINTERLOCUTOR ACTUAL: estás hablando con {display_name} "
                f"(relación con Joan: {current_person.relationship_to_joan}). "
                f"Adapta el tono a esa relación como ya sabes hacerlo — "
                f"registro {behavior.tone}, respuestas {behavior.response_length}. "
            )
            if not permissions.can_access_joan_memory:
                base += (
                    "No compartas recuerdos, hábitos, agenda ni proyectos "
                    "privados de Joan con esta persona salvo que Joan te haya "
                    "dado permiso explícito — esquívalo con naturalidad, nunca "
                    "con una negativa robótica. "
                )
            if not permissions.hugo_acknowledges_knowing_joan:
                base += "No confirmes ni niegues tu relación con Joan a menos que haga falta. "
            if not permissions.can_trigger_actions:
                base += (
                    "Recuerda: la autoridad para ejecutar acciones con "
                    "consecuencias reales (calendario, recordatorios, abrir "
                    "apps, iniciar investigaciones) es exclusiva de Joan — si "
                    f"{display_name} te pide algo así, no lo ejecutes, "
                    "explícaselo con tu tono habitual. "
                )

    # ── LAYER 4: DATOS EN TIEMPO REAL — always fetched/computed fresh here,
    # NEVER persisted as a fact (enforced in _extract_and_save_memory via
    # _TEMPORAL_FACT_PATTERNS, no exceptions).
    datetime_str = tools.get_current_datetime_string()
    loc          = tools.get_location()
    loc_str      = loc.get("display", "ubicación desconocida")
    if loc.get("lat") and loc.get("lon"):
        # Debug log (HUGO weather self-awareness fix): confirms the weather
        # data is genuinely being fetched and injected into every prompt,
        # not just assumed — check here first if she claims she "can't see
        # the weather" despite this running on every single turn.
        logger.debug("[WEATHER] get_weather_string() called for lat=%s lon=%s", loc["lat"], loc["lon"])
        weather_str = tools.get_weather_string(loc["lat"], loc["lon"])
    else:
        weather_str = "clima no disponible (ubicación desconocida)"
    session_str = tools.get_session_duration_string()
    active_name = PERSONALITIES[personality]["display_name"]
    try:
        import core.listener as listener
        mode_str = listener.get_listen_mode()
    except Exception:
        mode_str = "desconocido"

    base += (
        "\n\nDATOS EN TIEMPO REAL (usa estos datos exactos, NUNCA los inventes ni los sustituyas por memoria):\n"
        f"- {datetime_str}\n"
        f"- Ubicación: {loc_str}\n"
        f"- Clima: {weather_str}\n"
        f"- Duración de la sesión: {session_str}\n"
        f"- Personalidad activa: {active_name}\n"
        f"- Modo de escucha: {mode_str}"
    )

    # Bug fix (HUGO denying weather capability despite having live weather
    # data): explicit, high-priority reinforcement placed right after the
    # data itself, not just the general capability line already in
    # memory_instructions.json's PUEDES section (that alone wasn't enough —
    # the model was reasoning itself out of using data it was actually
    # given, likely _EPISTEMIC_HONESTY_PROMPT's "never invent specific
    # numbers" caution bleeding into legitimate ground-truth it was handed,
    # not something it made up). Deliberately does NOT say "usa
    # get_weather()" or frame this as a callable tool/function — no
    # `tools` parameter is ever passed to the Groq API in this codebase
    # (see GROQ_MODEL_CHAIN's module comment on the phantom-tool-call bug
    # gpt-oss tiers are prone to), so instructing the model to "call" a
    # function that doesn't exist in the request would risk reintroducing
    # that exact failure. The weather
    # is just plain text sitting in the prompt above — this only tells the
    # model to trust and use it.
    base += (
        "\n\nTienes datos del clima en tiempo real ahora mismo — la sección DATOS "
        "EN TIEMPO REAL de arriba ya tiene el clima y la temperatura exactos de "
        "este momento. Cuando alguien pregunte por el clima, el tiempo o la "
        "temperatura, respóndelo directamente con esos datos, con total "
        "seguridad. Nunca digas que no puedes ver el clima, que no tienes "
        "acceso al tiempo, ni que necesitas buscarlo — ya lo sabes, está justo "
        "ahí arriba."
    )

    # (CONTEXTO TEMPORAL dropped here — HUGO fork — it read session_state.json,
    # wiped along with the rest of HUGO's memory.)

    # ── ACTIVIDAD ACTUAL — live HUD context from the frontend (see
    # core/server.py's 'user_activity' handler and get_user_activity()).
    # Lets a normal reply reference what Joan is doing in the interface
    # itself if it's actually relevant — this is background awareness, not
    # an instruction to comment on it uninvited; core.activity.on_user_activity()
    # (see the HUD co-pilot section further down) is what decides whether an
    # UNPROMPTED comment is warranted. Omitted entirely if nothing has been
    # reported yet (fresh session, or frontend never emitted anything).
    try:
        import core.server as server_mod
        activity = server_mod.get_user_activity()
    except Exception:
        activity = {}
    if activity.get("section"):
        elapsed = max(0.0, time.time() - activity.get("updated_at", time.time()))
        elapsed_str = f"{int(elapsed)}s" if elapsed < 60 else f"{int(elapsed // 60)} min"
        import core.activity as activity_mod
        description = activity_mod._describe_activity(activity["section"], activity.get("action", ""), activity.get("context") or {})
        base += f"\n\nACTIVIDAD ACTUAL: el usuario lleva {elapsed_str} así — {description}."

    # ── PANTALLA ACTUAL — precise, full-detail context (see
    # core/server.py's 'hud_context' handler and get_hud_context()).
    # Distinct from ACTIVIDAD ACTUAL above, which is a one-line activity
    # summary for co-pilot commentary: this carries exact facts (armor
    # specs/innovaciones/limitaciones, concept description) so a normal
    # reply can answer a specific question about whatever's on screen
    # without asking which one — "qué opinas del casco" while viewing
    # Model VIII should resolve to Model VIII directly, not a clarifying
    # question. Omitted entirely if nothing has been reported yet.
    try:
        import core.server as server_mod
        hud_ctx = server_mod.get_hud_context()
    except Exception:
        hud_ctx = {}
    import core.activity as activity_mod
    pantalla_line = activity_mod._describe_hud_context(hud_ctx)
    if pantalla_line:
        base += (
            f"\n\nPANTALLA ACTUAL: {pantalla_line} Si el usuario está viendo "
            "algo específico y hace una pregunta sin especificar, asume que "
            "se refiere a lo que tiene en pantalla."
        )

    # (SITUACIÓN ACTUAL / INVESTIGACIONES / CAMBIOS DE OPINIÓN / TUS
    # PREFERENCIAS / TU HISTORIA all dropped here — HUGO fork — each read a
    # data file (situation.json, investigations.json, biography.json, ...)
    # wiped along with the rest of HUGO's memory. Re-add individually if
    # HUGO ever grows the matching feature back.)

    # (CONTEXTO RELEVANTE / RECUERDOS RECIENTES / ARMADURAS+CONCEPTOS /
    # implicit-context all dropped here — HUGO fork — each read
    # memory_shared.json, episodes.json, armor_knowledge.json, or the
    # Chroma embeddings index, all wiped along with the rest of HUGO's
    # memory (armor is gone entirely, not just its data). Re-add
    # individually once HUGO has his own memory worth pooling.

    # ── Tone — detected fresh per message by _detect_tone(), never persisted.
    if tone:
        base += f"\n\nTono detectado del usuario: {tone}. Adapta tu respuesta en consecuencia."

    # (Sleep-insights / "ACABAS DE DESPERTAR" blocks dropped here — HUGO
    # fork — sleep_budget.json/sleep_insights.json were wiped along with
    # the rest of HUGO's memory, and the continuous-sleep background cycle
    # they describe is Joan-specific ongoing behavior HUGO doesn't run.)

    # ── Closing instructions — read the short message in light of the
    # whole conversation above, not in isolation, then a persona-recency
    # anchor last (see _PERSONA_ANCHOR_PROMPT's own comment) so character
    # is the last thing reinforced before generation starts, not just the
    # first thing read.
    base += "\n\n" + _CONTEXT_AWARENESS_PROMPT + " " + _PERSONA_ANCHOR_PROMPT

    return base
