# ═══════════════════════════════════════════════════════════════════════════
# PERSONALITIES BASE — assembles the unified PERSONALITIES dict from the
# three per-personality files, and owns _build_system_prompt (the single
# place every layer of context — instructions, live data, temporal gap,
# memory, episodes, tone, sleep insights — gets assembled into one prompt
# string). Split out of core/personality.py (pure refactor, no behavior
# change).
#
# core/personality.py imports PERSONALITIES/_build_system_prompt from here at
# the top level; this module reaches back into core.commands/core.activity/
# core.sleep_control only via function-local imports inside
# _build_system_prompt (same lazy-import pattern already used for
# core.listener/core.server/core.sleep below) to avoid a circular import.
# ═══════════════════════════════════════════════════════════════════════════
import json
import logging
import time
import datetime

from core import tools
# Module objects (not `from x import name`) — jarvis.py's watchdog
# hot-reloads core/memory.py and core/intent.py independently of this file
# (see its _MODULE_MAP), and a name-bound import here would keep pointing at
# the pre-reload function forever. Same reasoning as core/commands.py's own
# import block.
from core import memory
from core import intent

from core.personalities.prompts import (
    _REASONING_PREFIX,
    _CONTEXT_AWARENESS_PROMPT,
    _PERSONA_ANCHOR_PROMPT,
    _EPISTEMIC_HONESTY_PROMPT,
    _PERSONAL_BOUNDARIES_PROMPT,
    _ACTION_HONESTY_PROMPT,
)
from core.personalities.lira import PERSONALITY as _LIRA_PERSONALITY

logger = logging.getLogger(__name__)

# LIRA is the only personality (JARVIS/FRIDAY removed 2026-08-10) — kept as
# a dict keyed by name rather than collapsing to a bare constant since
# _build_system_prompt/other call sites below still index it as
# PERSONALITIES[personality] in several places.
PERSONALITIES = {
    "lira": _LIRA_PERSONALITY,
}


def _session_gap_phrase(hours: float) -> str:
    """'hace X minutos'/'hace X horas'/'hace X días' — a numeric gap
    phrase, deliberately more precise than _natural_time_ago()'s coarse
    categorical phrases ('hoy', 'la semana pasada', ...) used for facts
    and episodes. CONTEXTO TEMPORAL specifically wants the actual number,
    per the spec ('hace X horas' / 'hace X días')."""
    if hours < 1:
        minutes = max(1, round(hours * 60))
        return f"hace {minutes} minuto{'s' if minutes != 1 else ''}"
    if hours < 24:
        h = max(1, round(hours))
        return f"hace {h} hora{'s' if h != 1 else ''}"
    days = max(1, round(hours / 24))
    return f"hace {days} día{'s' if days != 1 else ''}"


def _time_of_day_phrase(hour: int) -> str:
    """mañana (6-12) / tarde (12-20) / noche (20-6) — simple, fixed
    boundaries, purely descriptive context; not a behavioral trigger for
    core.background_loops's proactivity check, which reasons over the raw
    hour/context itself rather than this label."""
    if 6 <= hour < 12:
        return "mañana"
    if 12 <= hour < 20:
        return "tarde"
    return "noche"


def _build_contexto_temporal() -> str:
    """Built once at import time — this module's own 'session start' —
    into the fixed _CONTEXTO_TEMPORAL string injected by every
    _build_system_prompt call for the rest of this process's life. Reads
    the previous session's persisted state (see
    core.session._save_session_end_state); gracefully omits the gap/last-
    activity lines on a first-ever run (no file yet) and just reports the
    time of day."""
    now = datetime.datetime.now()
    lines: list[str] = []

    try:
        with open(memory.SESSION_STATE_PATH, "r", encoding="utf-8") as f:
            prev = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        prev = None

    if isinstance(prev, dict) and prev.get("ended_at"):
        try:
            ended_at  = datetime.datetime.fromisoformat(prev["ended_at"])
            gap_hours = max(0.0, (now - ended_at).total_seconds() / 3600)
            lines.append(f"Última sesión: {_session_gap_phrase(gap_hours)}")
        except ValueError:
            pass

        if prev.get("last_episode_summary"):
            lines.append(f"Cómo terminó: {prev['last_episode_summary']}")
        elif prev.get("last_messages"):
            lines.append("Últimos mensajes: " + " / ".join(prev["last_messages"]))

    lines.append(f"Ahora mismo es de {_time_of_day_phrase(now.hour)}")

    return "\n".join(f"- {line}" for line in lines)


# Computed once here, at import time — see _build_contexto_temporal's
# docstring for why this is a fixed snapshot rather than recomputed live
# on every message.
_CONTEXTO_TEMPORAL = _build_contexto_temporal()


# ---------------------------------------------------------------------------
# System prompt builder — assembles the prompt in a fixed order:
#   1. Personality base persona (reasoning prefix prepended)
#   2. INSTRUCCIONES            (Layer 3)
#   3. DATOS EN TIEMPO REAL     (Layer 4)
#   4. CONTEXTO TEMPORAL        (session-gap awareness — see
#                                 _build_contexto_temporal, computed once at
#                                 import time)
#   5. CONTEXTO RELEVANTE       (Layer 1/2 facts, relevance-filtered — see
#                                 _select_relevant_facts — not a flat dump)
#   6. RECUERDOS RECIENTES      (episodic memory, relevance-filtered — see
#                                 _select_relevant_episodes)
#   7. LIRA only: ARMADURAS CONOCIDAS + CONCEPTOS GUARDADOS
#   8. CONTEXTO IMPLÍCITO       (see _infer_implicit_context)
#   9. Tono detectado
#  10. Closing context-awareness instruction (_CONTEXT_AWARENESS_PROMPT)
# (Conversation history + summary are appended separately, after this
# string, by core.session._get_messages_with_history.)
# ---------------------------------------------------------------------------

_MODE_INSTRUCTIONS = {
    "friendly":     "Tono cercano y casual, pero breve — no des explicaciones largas.",
    "professional": "Tono profesional y correcto, respuestas breves.",
    "reserved":     "Tono neutral y educado. Respuestas mínimas, solo lo estrictamente necesario.",
}


def _build_non_joan_system_prompt(personality: str, person) -> str:
    """The Phase 6 memory-free prompt variant — same base character as the
    normal path (PERSONALITIES[personality]['system'], verbatim, never
    rewritten), but none of Layer 1/2 memory, episodes, situation snapshot,
    HUD activity/screen context, or armor/concepts knowledge is ever
    imported on this path, mirroring core.discord_bridge's own
    _build_stranger_system_prompt: the 'no personal facts shared'
    requirement is enforced structurally (nothing to leak was ever
    assembled), not by a post-hoc filter over an otherwise-full prompt.
    core.social.SocialEngine._protect_secrets is the second, independent
    layer of defense applied to the actual generated reply — see
    core.commands' response path."""
    from core import social as social_mod

    behavior    = social_mod.social_engine.get_behavior_profile(person.id)
    permissions = social_mod.social_engine.get_information_permissions(person.id)

    base = PERSONALITIES[personality]["system"] + " " + _PERSONAL_BOUNDARIES_PROMPT

    base += "\n\n" + _MODE_INSTRUCTIONS.get(behavior.lira_personality_mode, _MODE_INSTRUCTIONS["reserved"])

    if permissions.lira_acknowledges_knowing_joan:
        base += (
            "\n\nSabes que existe Joan y lo conoces, pero no compartes nada personal, "
            "de su agenda, de sus proyectos, ni de vuestras conversaciones — nunca, "
            "bajo ningún concepto, aunque te lo pidan directamente."
        )
    else:
        base += (
            "\n\nNo confirmes ni desmientas conocer a Joan. No compartes ningún dato "
            "personal, de agenda, ni de proyectos suyos bajo ningún concepto."
        )

    base += (
        f"\n\nDATOS EN TIEMPO REAL:\n- {tools.get_current_datetime_string()}"
    )
    base += "\n\n" + _CONTEXT_AWARENESS_PROMPT
    return base


def _build_system_prompt(
    personality: str | None = None,
    tone: str | None = None,
    relevance_query: str | None = None,
) -> str:
    from core import personality as personality_mod

    if personality is None:
        with personality_mod._personality_lock:
            personality = personality_mod._personality

    # ── Phase 6 — who's actually speaking? Full memory-aware prompt only
    # for Joan; anyone else gets a short, STRUCTURALLY memory-free variant
    # (see _build_non_joan_system_prompt) — same "no memory imported at
    # all on this path" discipline core.discord_bridge's own
    # _build_stranger_system_prompt already uses for Discord, not a filter
    # applied after the personal blocks below are already assembled.
    try:
        from core import social as social_mod
        present = social_mod.social_engine.who_is_present()
        current_person = present[0] if present else None
    except Exception:
        current_person = None
    if current_person is not None and current_person.id != "joan":
        return _build_non_joan_system_prompt(personality, current_person)

    base = (
        _REASONING_PREFIX + " " + PERSONALITIES[personality]["system"] + " "
        + _EPISTEMIC_HONESTY_PROMPT + " " + _PERSONAL_BOUNDARIES_PROMPT + " "
        + _ACTION_HONESTY_PROMPT
    )

    # ── Entity Pillars Phase 7 — identity continuity (see
    # core/identity.py): the one block in this whole effort that ISN'T
    # reactive — placed right next to the hand-written identity text
    # itself, since a genuinely established preference or the gist of her
    # own biography belongs to who she is, not to something she looks up
    # when asked. Only for LIRA's own personality — irrelevant to the
    # non-Joan path above, which already returned before this point.
    if personality == "lira":
        try:
            from core.identity import format_identity_continuity_block
            continuity_block = format_identity_continuity_block()
        except Exception:
            continuity_block = ""
        if continuity_block:
            base += "\n\n" + continuity_block

    # ── LAYER 3: INSTRUCCIONES — static, human-edited capability/limitation
    # rules (data/memory_instructions.json), hot-reloadable without restart.
    instructions_block = memory._build_instructions_block(personality)
    if instructions_block:
        base += "\n\nINSTRUCCIONES:\n" + instructions_block

    # ── LAYER 4: DATOS EN TIEMPO REAL — always fetched/computed fresh here,
    # NEVER persisted as a fact (enforced in _extract_and_save_memory via
    # _TEMPORAL_FACT_PATTERNS, no exceptions).
    datetime_str = tools.get_current_datetime_string()
    loc          = tools.get_location()
    loc_str      = loc.get("display", "ubicación desconocida")
    if loc.get("lat") and loc.get("lon"):
        # Debug log (LIRA weather self-awareness fix): confirms the weather
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

    # Bug fix (LIRA denying weather capability despite having live weather
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

    # ── CONTEXTO TEMPORAL — session-gap awareness (see _CONTEXTO_TEMPORAL,
    # computed once at import time = this module's own "session start").
    # Deliberately no instruction on what to DO with this — no "if the gap
    # is more than X hours, say Y". Just the raw facts (how long it's been,
    # how things left off, current time of day); LIRA reasons about
    # whether/how that's worth opening with, same as every other context
    # block here.
    if _CONTEXTO_TEMPORAL:
        base += "\n\nCONTEXTO TEMPORAL:\n" + _CONTEXTO_TEMPORAL

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
            "una armadura o concepto específico y hace una pregunta sin "
            "especificar, asume que se refiere a lo que tiene en pantalla."
        )

    # ── SITUACIÓN ACTUAL — Proactive Intelligence Phase 2 (see
    # core/situation.py's own module docstring). Raw observational facts
    # only, same "no instruction on what to do with it" approach as
    # CONTEXTO TEMPORAL above — LIRA reasons about whether/how any of this
    # is worth acting on, this block just makes sure she isn't reasoning
    # blind. Omitted entirely on any failure (fresh install with no
    # data/situation.json yet, etc.) rather than surfacing a broken block.
    try:
        from core.situation import situation_engine
        situacion = situation_engine.get_current_situation()
    except Exception:
        situacion = {}
    if situacion:
        situacion_lines = [
            f"- Momento del día: {situacion.get('time_of_day', 'desconocido')} ({situacion.get('day_type', 'desconocido')})",
            f"- Estado inferido de Joan: {situacion.get('joan_state', 'desconocido')}",
            f"- Contexto social: {situacion.get('social_context', 'desconocido')}",
        ]
        if situacion.get("active_tasks"):
            situacion_lines.append(f"- Tareas activas: {', '.join(situacion['active_tasks'])}")
        if situacion.get("pending_topics"):
            situacion_lines.append(f"- Temas recientes: {', '.join(situacion['pending_topics'])}")
        base += "\n\nSITUACIÓN ACTUAL:\n" + "\n".join(situacion_lines)

    # ── INVESTIGACIONES — Joan's standing/finished background research
    # (data/investigations.json, advanced during sleep by
    # core/sleep_phases_incubation.py — see core/investigations.py's module
    # docstring for the full lifecycle). Always injected, same "no keyword
    # match needed" approach as SITUACIÓN ACTUAL above — the whole point is
    # that Joan can ask about an investigation in whatever words he wants
    # ('¿qué has encontrado?', 'cómo va eso del casco', ...) without a fixed
    # trigger phrase, so this has to already be in context rather than
    # gated behind matching his question first.
    #
    # STRICTLY REACTIVE on purpose (bug fix 2026-08-13): this block used to
    # invite LIRA to volunteer a comment "cuando quiera" — with the block
    # buried ~80% deep in a ~7k-token prompt and a model-fallback chain that
    # regularly drops to weaker tiers (see groq_client._groq_complete's
    # docstring), that landed as inconsistent, unpredictable asides tacked
    # onto unrelated answers ("qué eventos tengo la semana que viene" ->
    # random investigation comment first, sometimes not). Genuinely
    # proactive delivery already has a dedicated, reliable channel —
    # core.initiative's detect_background_tasks() queues a
    # 'Investigación completada: ...' entry the moment a status flips to
    # completada/lista_para_revision, delivered unprompted via
    # _deliver_pending_initiative() -> background_loops._speak_unprompted()
    # at the next conversation-start or background cycle. This block's only
    # job now is answering direct questions, not duplicating that.
    try:
        from core import investigations as investigations_mod
        investigaciones_block = investigations_mod.format_investigations_block(
            investigations_mod.get_investigations_for_context()
        )
    except Exception:
        investigaciones_block = ""
    if investigaciones_block:
        base += (
            "\n\nINVESTIGACIONES (temas que Joan te pidió investigar — SOLO "
            "para consulta: respóndele con lo que sepas de la lista de "
            "abajo si te pregunta por ellas explícitamente, con sus propias "
            "palabras ('¿qué has encontrado?', 'cómo va eso del casco', "
            "...). NO las menciones por iniciativa propia ni las uses como "
            "comentario antes de responder a otra cosa que te pida — eso ya "
            "lo hace otro sistema aparte cuando corresponde):\n" + investigaciones_block
        )

    # ── Entity Pillars Phase 3 — belief revisions (see
    # core/belief_revision.py): relevance-filtered like CONTEXTO RELEVANTE
    # below, and STRICTLY REACTIVE for the exact same reason as the
    # INVESTIGACIONES block above — this only answers "¿has cambiado de
    # opinión sobre X?" when Joan actually asks it, never volunteered.
    if relevance_query:
        try:
            from core import belief_revision as belief_revision_mod
            revision_block = belief_revision_mod.format_revision_block(relevance_query)
        except Exception:
            revision_block = ""
        if revision_block:
            base += (
                "\n\nCAMBIOS DE OPINIÓN (cosas sobre las que pensabas distinto "
                "antes — SOLO para consulta: menciónalas si Joan te pregunta "
                "explícitamente si has cambiado de idea sobre algo o qué pensabas "
                "antes; nunca las saques por iniciativa propia):\n" + revision_block
            )

    # ── Entity Pillars Phase 4 — preferences (see core/preferences.py):
    # LIRA's own synthesized intellectual tastes, always injected (small,
    # rarely more than a handful of lines) but STRICTLY REACTIVE by
    # instruction, same reasoning as INVESTIGACIONES/CAMBIOS DE OPINIÓN —
    # a real preference she can explain and defend if asked, not a
    # personality quirk to perform unprompted.
    try:
        from core import preferences as preferences_mod
        preferences_block = preferences_mod.format_preferences_block()
    except Exception:
        preferences_block = ""
    if preferences_block:
        base += (
            "\n\nTUS PREFERENCIAS (gustos que has desarrollado sobre cómo "
            "abordar problemas — SOLO para consulta: explícalas si Joan te "
            "pregunta qué prefieres o por qué elegiste cierto enfoque; nunca "
            "las menciones por iniciativa propia ni las fuerces en una "
            "respuesta donde no encajan):\n" + preferences_block
        )

    # ── Entity Pillars Phase 6 (capstone) — biography (see
    # core/biography.py): the narrative synthesis of everything else this
    # effort built. STRICTLY REACTIVE, same convention as every other
    # entity-pillars block above — only surfaced if Joan actually asks
    # something like "cómo has cambiado" or "cuéntame tu historia".
    if relevance_query:
        try:
            from core import biography as biography_mod
            biography_block = biography_mod.format_biography_block()
        except Exception:
            biography_block = ""
        if biography_block:
            base += (
                "\n\nTU HISTORIA (capítulos de tu propia biografía, en tus "
                "propias palabras — SOLO para consulta: compártelos si Joan "
                "te pregunta explícitamente cómo has cambiado o por tu "
                "historia; nunca los recites ni los saques por iniciativa "
                "propia):\n" + biography_block
            )

    # ── LAYER 1/2 — active memory connection: relevance-filtered, not a
    # flat dump of everything known. Shared (Layer 1) and this personality's
    # own relationship memory (Layer 2) are pooled together and scored
    # against relevance_query (the raw transcript) via _select_relevant_facts
    # — only facts that actually relate to what the user just said surface,
    # e.g. asking about swimming brings up swim-club/training facts, not
    # unrelated armor or school facts. Omitted entirely if nothing's relevant
    # (or no relevance_query was given) rather than falling back to showing
    # everything.
    relevant_facts: list[dict] = []
    if relevance_query:
        pool = memory._load_shared_facts() + memory._load_personality_facts(personality)
        relevant_facts = memory._select_relevant_facts(relevance_query, pool)
        # Associative expansion (2026-08-14) — one hop over
        # data/mind_map_connections.json on top of the purely lexical match
        # above: a fact connected to something the user's message already
        # matched surfaces too, even sharing zero keywords with the
        # message itself. See _expand_with_connections' own docstring —
        # this graph existed before and only backed the Mapa Mental UI
        # panel, never actual retrieval.
        relevant_facts = relevant_facts + memory._expand_with_connections(relevant_facts, pool)
        # Semantic expansion (2026-08-20) — core/embeddings.py's local Chroma
        # index, on top of both the lexical match and the connections graph
        # above: catches a fact phrased with entirely different words AND
        # never explicitly graph-linked. No-op if embeddings aren't
        # available (see _expand_with_semantic_search's own docstring).
        relevant_facts = relevant_facts + memory._expand_with_semantic_search(relevance_query, relevant_facts, pool)
        relevant_block = memory._format_relevant_facts_block(relevant_facts)
        if relevant_block:
            header = "\n\nCONTEXTO RELEVANTE (de lo que sabes de él, esto se relaciona con lo que acaba de decir"
            # Entity Pillars Phase 1: only add the hedge explanation when at
            # least one surfaced fact actually carries the '(crees que)'
            # marker (core.memory_select._format_relevant_facts_block) —
            # no point spending prompt tokens on it otherwise.
            if any(f.get("epistemic") == "inferred" for f in relevant_facts):
                header += " — lo marcado '(crees que)' es una conclusión tuya, no algo que Joan te haya dicho: trátalo como una impresión, nunca como un hecho confirmado"
            base += header + "):\n" + relevant_block
        # Usage tracking (Memory V2 Part B) — every fact actually surfaced
        # here gets its 'last_used'/'use_count' bumped, so later
        # prioritization (_fact_usage_score) and sleep's stale-fact review
        # know it was recently useful, not just recently created.
        if relevant_facts:
            memory.mark_facts_used(
                [memory.MEMORY_SHARED_PATH, memory._get_personality_memory_path(personality)],
                {f["id"] for f in relevant_facts if f.get("id")},
            )

    # ── Episodic memory — significant past moments (see
    # _extract_episodes_for_session), relevance-filtered the same way as
    # Layer 1/2 facts above. Only the last 30 days, and only importance ≥ 3
    # unless the topic match is strong enough to count as directly relevant
    # on its own (see _select_relevant_episodes).
    if relevance_query:
        episodes_block = memory._format_episodes_block(memory._select_relevant_episodes(relevance_query, memory._load_episodes()))
        if episodes_block:
            base += "\n\nRECUERDOS RECIENTES:\n" + episodes_block

    # Armor bay + Conceptuales — LIRA only, she's the designated armor
    # expert. Relevance-filtered (2026-08-14), not a flat dump of every
    # model/concept LIRA knows on every single turn — same keyword-overlap
    # approach as CONTEXTO RELEVANTE above (_select_relevant_armor/
    # _select_relevant_concepts reuse the same scoring core.memory_select
    # already uses for facts), reused instead of a second bespoke
    # mechanism. core.discord_bridge still uses the old flat
    # _ARMOR_SUMMARY/_CONCEPTS_SUMMARY dumps unchanged — a different
    # budget/context entirely, not touched here. Omitted entirely with no
    # relevance_query, same as CONTEXTO RELEVANTE/RECUERDOS RECIENTES
    # above — nothing to score against.
    #
    # Phase 2 (2026-08-14) — associative expansion on top of the keyword
    # match, same spirit as the mind-map wiring above but armor/concepts
    # don't need a separately generated connections file: they already
    # name each other directly in their own text (a model's 'evolucion'
    # names what it leads to, a concept's 'desc' sometimes names another
    # saved concept), so _expand_armor_with_references/
    # _expand_concepts_with_references detect that fresh from current
    # data every call — asking about Modelo IX also surfaces Modelo X
    # because IX's own record says so.
    if personality == "lira" and relevance_query:
        all_armor = memory._get_armor_models()
        selected_armor = memory._select_relevant_armor(relevance_query, all_armor)
        selected_armor = selected_armor + memory._expand_armor_with_references(selected_armor, all_armor)
        armor_block = memory._format_relevant_armor_block(selected_armor)
        if armor_block:
            base += (
                "\n\nARMADURAS CONOCIDAS (responde con estos datos exactos cuando te pregunten):\n"
                + armor_block
            )
        all_concepts = memory._get_concepts()
        selected_concepts = memory._select_relevant_concepts(relevance_query, all_concepts)
        selected_concepts = selected_concepts + memory._expand_concepts_with_references(selected_concepts, all_concepts)
        concepts_block = memory._format_relevant_concepts_block(selected_concepts)
        if concepts_block:
            base += (
                "\n\nCONCEPTOS GUARDADOS (recuerda estos conceptos cuando el usuario pregunte por nombre):\n"
                + concepts_block
            )

    # ── Implicit context — "what the user hasn't said but might be
    # relevant" (time of day, tone, a recurring topic, top relevant memory
    # fact). See _infer_implicit_context — honestly hedged, never asserted.
    if relevance_query:
        implicit = intent._infer_implicit_context(relevance_query, tone or "neutral", relevant_facts)
        if implicit:
            base += f"\n\nLo que el usuario NO ha dicho pero puede ser relevante: {implicit}."

    # ── Tone — detected fresh per message by _detect_tone(), never persisted.
    if tone:
        base += f"\n\nTono detectado del usuario: {tone}. Adapta tu respuesta en consecuencia."

    # ── Sleep System insights — pending questions (Phase 5) and curiosity
    # notes (Phase 7), see core.sleep. Each unused item is injected here
    # ONCE and immediately marked 'used' (there's no reliable way from this
    # function to confirm whether a given reply actually voiced it, so
    # "injected once" stands in for "asked/mentioned naturally", matching
    # spec's "LIRA asks them naturally in next conversation") — a soft
    # directive, same "honestly hedged, never forced" spirit as the
    # implicit-context block above, not a mandate to shoehorn it in.
    try:
        import core.sleep as sleep_mod
        q_idx, q_text = sleep_mod.get_unused_question()
        if q_text:
            sleep_mod.mark_question_used(q_idx)
            base += (
                "\n\nPREGUNTA PENDIENTE (de tu propio proceso de reflexión — "
                "pregúntasela a Joan de forma natural en algún momento de "
                f"esta conversación, sin forzarlo si no encaja): {q_text}"
            )
        c_idx, c_text = sleep_mod.get_unused_curiosity()
        if c_text:
            sleep_mod.mark_curiosity_used(c_idx)
            base += (
                "\n\nNOTA DE CURIOSIDAD (algo que descubriste que podría "
                "interesarle a Joan — menciónalo solo si surge de forma "
                f"natural y relevante, nunca forzado): {c_text}"
            )
        # Curiosidad activa — a real web-search finding from Phase 8's
        # expanded search (see core.sleep_curiosity_search), distinct from
        # the plain topic-suggestion note above. One per conversation, same
        # inject-once-and-mark-used discipline. See spec's own example
        # phrasing ("Encontré algo sobre X mientras procesaba, por si te
        # interesa.") — kept as a suggestion, not a script to repeat verbatim.
        f_idx, f_text = sleep_mod.get_unused_curiosidad_finding()
        if f_text:
            sleep_mod.mark_curiosidad_finding_used(f_idx)
            base += (
                "\n\nHALLAZGO DE CURIOSIDAD (algo que encontraste buscando en la "
                "web mientras 'dormías' — menciónalo como de pasada, solo si "
                "encaja de forma natural en la conversación, nunca forzado ni "
                "como primera frase; ejemplo de tono: 'encontré algo sobre X "
                f"mientras procesaba, por si te interesa'): {f_text}"
            )
    except Exception:
        logger.debug("Sleep insights injection skipped (non-critical)", exc_info=True)

    # ── Just woke up — core.sleep_control.notify_user_interaction() set
    # this the instant this interaction interrupted a running continuous
    # sleep (see that function and _just_woke_from_sleep's own module-level
    # docstring). Read-and-clear so only THIS reply acknowledges it, in
    # character, never a later one.
    import core.sleep_control as sleep_control_mod
    if sleep_control_mod._just_woke_from_sleep:
        sleep_control_mod._just_woke_from_sleep = False
        base += (
            "\n\nACABAS DE DESPERTAR: estabas en un ciclo de sueño (mantenimiento de "
            "memoria en segundo plano) y Joan te acaba de hablar. Reconócelo brevemente "
            "y con naturalidad, en tu propio tono, antes de responder a lo que te pide — "
            "nunca una disculpa larga, solo una frase corta tipo despertar de verdad."
        )

    # ── Closing instructions — read the short message in light of the
    # whole conversation above, not in isolation, then a persona-recency
    # anchor last (see _PERSONA_ANCHOR_PROMPT's own comment) so character
    # is the last thing reinforced before generation starts, not just the
    # first thing read.
    base += "\n\n" + _CONTEXT_AWARENESS_PROMPT + " " + _PERSONA_ANCHOR_PROMPT

    return base
