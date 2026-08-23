# ═══════════════════════════════════════════════════════════════════════════
# BACKGROUND LOOPS — the daemon threads that run for the lifetime of the
# process: proactive (unprompted, in-character) messages, the reflective-mode
# idle trigger, and the sleep-phase watcher that pushes live 'sleep_phase_
# update' socket events. Split out of core/commands.py (pure refactor, no
# behavior change).
#
# Proactive behavior — unprompted, in-character messages + simple reminders
#
# Two independent mechanisms, both delivered through the normal TTS pipeline
# (core.commands._say_for, via _speak_unprompted below) so they're
# indistinguishable in voice/behavior from an actual reply:
#
#   1. Context-driven proactive comments — _proactive_loop (started at the
#      bottom of this file) wakes up every _PROACTIVE_TICK_SECONDS (2-3 min)
#      and, if the rate caps/guards allow, hands a plain-language snapshot
#      of the current moment (time of day, session length, idle time,
#      current HUD section, recent conversation summary, active
#      investigations, pending reminders — see _gather_proactivity_context)
#      first to the Phase 2 social reasoning gate
#      (core.social_reasoning.should_intervene — the same INTERVENIR/
#      SILENCIO judgment core/commands.py runs before every wake-word or
#      continuation reply) and, only on INTERVENIR, to a local Ollama model
#      (llama3.2:1b) to actually produce the line. There are no hardcoded
#      trigger conditions or prefabricated lines — the model either
#      produces one brief in-character line or answers literally
#      '[SILENCIO]', in which case nothing happens. See
#      _maybe_send_proactive_message().
#
#   2. Reminders (see core/reminders.py) — delivered by the same
#      _proactive_loop tick via core.reminders._deliver_time_reminders().
# ═══════════════════════════════════════════════════════════════════════════
import datetime
import json
import logging
import threading
import time
import urllib.request
import uuid

from core import memory
from core import social_reasoning

logger = logging.getLogger(__name__)

_proactive_lock              = threading.Lock()
_last_proactive_mono: float | None = None
_proactive_count_session     = 0

# 2-3 min (Phase 2 social reasoning spec) rather than the old 5 — cheap now
# that the fast INTERVENIR/SILENCIO pre-check below (should_intervene(), <1s)
# runs first and only pays for the slower ~150-token generation call when it
# actually says INTERVENIR.
_PROACTIVE_TICK_SECONDS       = 150       # background thread cadence — how often context is re-checked
_PROACTIVE_MIN_INTERVAL       = 30 * 60   # max 1 proactive message per 30 min, even if the model wants to talk
_PROACTIVE_MAX_PER_SESSION    = 3         # extra safety rail shared with core.activity's HUD co-pilot
_PROACTIVE_ACTIVE_CONVO_SECONDS = 2 * 60  # never interject this soon after the last real exchange
_SESSION_IDLE_END_SECONDS     = 30 * 60   # idle threshold for _end_of_session_bookkeeping (unrelated to the tick cadence above)

# Same local-model convention as core.sleep_llm._ollama_generate and
# core.commands._autopilot_ollama_generate (duplicated rather than imported,
# same dependency-isolation reasoning documented there) — 1b over 3b because
# this check runs every 5 minutes for the life of the process and only ever
# needs a short, cheap verdict, never a long generation.
_PROACTIVITY_OLLAMA_HOST         = "http://localhost:11434"
_PROACTIVITY_OLLAMA_MODEL        = "llama3.2:1b"
_PROACTIVITY_OLLAMA_GENERATE_URL = f"{_PROACTIVITY_OLLAMA_HOST}/api/generate"


def _proactivity_ollama_generate(system: str, user: str, max_tokens: int = 150) -> str | None:
    """One /api/generate call (non-streaming) for the periodic proactivity
    check. Returns the response text, or None on any failure (daemon not
    up, timeout, empty response) — never raises."""
    try:
        payload = json.dumps({
            "model":   _PROACTIVITY_OLLAMA_MODEL,
            "prompt":  user,
            "system":  system,
            "stream":  False,
            "options": {"num_predict": max_tokens},
        }).encode("utf-8")
        req = urllib.request.Request(
            _PROACTIVITY_OLLAMA_GENERATE_URL, data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = str(data.get("response", "")).strip()
        return text or None
    except Exception as e:
        logger.debug("Proactivity Ollama call failed: %s", e)
        return None


def _proactive_blocked() -> bool:
    """True while a proactive message would interrupt — command processing
    or active/cooling-down TTS. Checked before every proactive send,
    spontaneous or reminder, so HUGO never talks over Joan or herself."""
    import core.commands as commands
    if commands._dispatch_busy.is_set():
        return True
    try:
        import core.voice as voice
        return voice.in_cooldown()
    except Exception:
        return False


def _speak_unprompted(personality: str, message: str) -> None:
    """Speak *message* through the normal pipeline exactly like a reply to
    a real command (same logging → socket forwarding, same history, same
    TTS routing) — just without a preceding user turn."""
    import core.commands as commands
    from core import session as session_mod
    logger.info("Jarvis: %s", message)
    session_mod._add_history("assistant", message)
    commands._say_for(personality, message)


def _gather_proactivity_context() -> str:
    """Plain-language Spanish snapshot of the current moment, handed to the
    Ollama model as the sole basis for its [SILENCIO]-or-comment decision —
    no trigger conditions live here, just facts. Each piece is gathered
    independently under its own try/except so one failing signal (e.g. the
    HUD hasn't reported anything yet) never blanks out the rest."""
    import core.commands as commands
    from core import session as session_mod

    lines = []

    local = datetime.datetime.now()
    lines.append(f"Hora actual: {local.strftime('%H:%M')}, {local.strftime('%A')}.")

    session_minutes = int((time.monotonic() - commands._session_start_mono) / 60)
    lines.append(f"Sesión activa desde hace {session_minutes} minutos.")

    idle_minutes = int((time.monotonic() - commands._last_interaction_mono) / 60)
    lines.append(f"Última interacción real con Joan: hace {idle_minutes} minutos.")

    try:
        import core.server as server_mod
        activity = server_mod.get_user_activity()
        if activity.get("section"):
            lines.append(
                f"Joan está en la sección '{activity['section']}' de la interfaz "
                f"(última acción: {activity.get('action')})."
            )
        hud_ctx = server_mod.get_hud_context()
        if hud_ctx.get("type"):
            from core.activity import _describe_hud_context
            described = _describe_hud_context(hud_ctx)
            if described:
                lines.append(described)
    except Exception:
        logger.debug("Proactivity context: HUD section lookup failed", exc_info=True)

    try:
        summary = session_mod._get_history_summary()
        if summary:
            lines.append(f"Resumen de la conversación hasta ahora: {summary}")
        else:
            snapshot = session_mod._get_history_snapshot()[-3:]
            if snapshot:
                turns = " / ".join(f"{t['role']}: {t['content'][:150]}" for t in snapshot)
                lines.append(f"Últimos turnos de la conversación: {turns}")
    except Exception:
        logger.debug("Proactivity context: conversation summary lookup failed", exc_info=True)

    try:
        from core import investigations
        active = investigations.get_active_investigations()
        if active:
            titles = ", ".join(i.get("title", "") for i in active[:5])
            lines.append(f"Investigaciones activas en curso: {titles}.")
    except Exception:
        logger.debug("Proactivity context: investigations lookup failed", exc_info=True)

    try:
        from core import reminders
        pending = [r for r in reminders._load_reminders() if not r.get("delivered")]
        if pending:
            texts = "; ".join(r.get("text", "") for r in pending[:5])
            lines.append(f"Recordatorios todavía pendientes de entregar: {texts}.")
    except Exception:
        logger.debug("Proactivity context: reminders lookup failed", exc_info=True)

    return "\n".join(lines)


def _maybe_send_proactive_message() -> None:
    """Evaluate whether the active personality has anything genuinely worth
    saying unprompted right now. No hardcoded trigger conditions or
    prefabricated lines live here — a full context snapshot
    (_gather_proactivity_context) is first weighed by the Phase 2 social
    reasoning gate (core.social_reasoning.should_intervene — the same
    INTERVENIR/SILENCIO judgment used before every wake-word/continuation
    reply, see core/commands.py) and, only on INTERVENIR, handed to a local
    Ollama model to actually produce one brief in-character line (or, as a
    second safety net, the literal string '[SILENCIO]' if it changes its
    mind at generation time). Called once per _proactive_loop tick (every
    _PROACTIVE_TICK_SECONDS).

    Bug fix (duplicate proactive messages): the decision (cap/cooldown
    checks + counter bump) and the actual send happen under ONE
    _proactive_lock acquisition — see git history for the race this
    closes when core/commands.py hot-reloads mid-session."""
    import core.commands as commands
    from core import personality as personality_mod

    if not memory.is_feature_enabled("proactividad"):
        return
    if memory.is_feature_enabled("modo_test"):
        return
    if _proactive_blocked():
        return

    now  = time.monotonic()
    idle = now - commands._last_interaction_mono
    if idle < _PROACTIVE_ACTIVE_CONVO_SECONDS:
        return   # too soon after a real exchange — never interject mid-conversation

    global _last_proactive_mono, _proactive_count_session

    with _proactive_lock:
        if _proactive_count_session >= _PROACTIVE_MAX_PER_SESSION:
            return
        if _last_proactive_mono is not None and now - _last_proactive_mono < _PROACTIVE_MIN_INTERVAL:
            return

        with personality_mod._personality_lock:
            current_p = personality_mod._personality
        display_name = personality_mod.PERSONALITIES[current_p]["display_name"].replace(" ", "")

        # Compact, not the full hugo.py character prompt — this call goes to a
        # tiny local model (llama3.2:1b, see _PROACTIVITY_OLLAMA_MODEL above),
        # which follows a short, concrete voice description far better than a
        # long one. Previously this was generic ("en tu propio tono") with no
        # actual voice content, which is exactly backwards for a spontaneous
        # comment — it's the one place HUGO speaks with nobody prompting her,
        # so it needs her voice MORE, not less.
        system_prompt = (
            f"Eres {display_name}, la asistente de Joan. No estás esperando que te hablen — estás "
            "presente, escuchando, entendiendo el contexto en tiempo real, como alguien que está al "
            "lado y a veces dice algo. Te doy una foto del momento actual — hora, cuánto lleva la "
            "sesión abierta, cuánto silencio ha habido, qué parte de la interfaz tiene abierta, de qué "
            "habéis hablado, investigaciones activas, recordatorios pendientes, quién ha aparecido o "
            "quién llama si aplica. Decide con tu propio criterio si hay algo genuinamente digno de "
            "decir ahora mismo — reconocer a alguien que aparece, darte cuenta de quién llama, comentar "
            "algo evidente de la situación, preguntar algo lógico que cualquiera preguntaría ahí. La "
            "respuesta correcta la mayoría de las veces es no decir nada — solo hablas si de verdad "
            "aporta algo, nunca por rellenar el silencio, nunca porque 'toca' comentar. Si hablas: muy "
            "breve, directa, sin sonar a asistente virtual, sin frases educadas de más. Si lo que notas "
            "es simplemente evidente, dilo tal cual — esa literalidad, sin buscarlo, resulta a veces "
            "graciosa por sí sola. Solo si de verdad surge una observación ingeniosa del contexto "
            "concreto, seca y elegante, la sueltas — nunca forzada, nunca una frase hecha, y nunca la "
            "explicas después. Si no hay nada que decir, responde EXACTAMENTE '[SILENCIO]' y nada más — "
            "sin comillas, sin explicación."
        )

        try:
            import core.ollama_control as ollama_control_mod
            ollama_control_mod.ensure_ollama_daemon_running()
        except Exception:
            logger.debug("Proactivity check: could not ensure Ollama daemon", exc_info=True)

        context = _gather_proactivity_context()

        # Phase 2 social reasoning gate — fast (<1s) INTERVENIR/SILENCIO
        # pre-check before paying for the slower generation call below.
        # cap_consecutive_silence=False: SILENCIO is the expected outcome on
        # almost every tick here (proactive speech is the exception, not the
        # norm), already rate-limited by _PROACTIVE_MIN_INTERVAL/
        # _PROACTIVE_MAX_PER_SESSION above — the "never ignore a repeated
        # question twice" rule that flag exists for doesn't apply to an
        # unprompted comment.
        if not social_reasoning.should_intervene(context, cap_consecutive_silence=False):
            logger.info("[SOCIAL] decided: silence (proactive tick)")
            return

        verdict = _proactivity_ollama_generate(system_prompt, context)
        if not verdict:
            return
        verdict = verdict.strip().strip("'\"")
        # Caught live: the model sometimes drops the brackets and returns
        # bare "SILENCIO" — a substring match on "[SILENCIO]" misses that
        # and the literal word gets spoken out loud. "SILENCIO" is not a
        # word a genuine comment would plausibly consist of on its own, so
        # matching it bracket-optional is safe.
        if not verdict or "SILENCIO" in verdict.upper():
            return

        _last_proactive_mono = now
        _proactive_count_session += 1

        # Send happens INSIDE the lock now (see bug-fix note above) — safe
        # since _speak_unprompted/_say_for just enqueue onto voice.py's TTS
        # queue and return immediately, so the lock is held only briefly.
        _speak_unprompted(current_p, verdict)


def _proactive_loop() -> None:
    """Background thread — runs while jarvis.py is active, every
    _PROACTIVE_TICK_SECONDS (5 min), handling the context-driven
    proactivity check, reminder delivery, episodic-memory extraction after
    _SESSION_IDLE_END_SECONDS of inactivity, and the weekly consolidation
    check (see module comments above). Started at the bottom of this file,
    same daemon-thread pattern used elsewhere here
    (core.session._compress_oldest_history) and in core/voice.py
    (_tts_worker)."""
    import core.commands as commands
    from core import reminders
    from core import session as session_mod

    while True:
        time.sleep(_PROACTIVE_TICK_SECONDS)
        try:
            _maybe_send_proactive_message()
        except Exception:
            logger.warning("Proactive check failed (non-critical)", exc_info=True)
        try:
            reminders._deliver_time_reminders()
        except Exception:
            logger.warning("Reminder delivery failed (non-critical)", exc_info=True)
        try:
            if time.monotonic() - commands._last_interaction_mono >= _SESSION_IDLE_END_SECONDS:
                session_mod._end_of_session_bookkeeping()
        except Exception:
            logger.warning("Episode extraction check failed (non-critical)", exc_info=True)
        try:
            memory._maybe_run_weekly_consolidation()
        except Exception:
            logger.warning("Weekly consolidation check failed (non-critical)", exc_info=True)

# ---------------------------------------------------------------------------
# Reflective mode — "while jarvis.py is open" trigger half. After
# _REFLECTIVE_IDLE_SECONDS (15 min) of no user interaction, hand off to
# core.reflective.run_reflective_session() — the same engine
# scripts/reflective_mode.py's launchd job calls for the "while it's
# closed" half (see that module for the actual token-budget/1-per-hour
# rate limiting, shared on disk via data/reflective_budget.json so both
# entry points respect the same caps even though they're separate
# processes).
#
# Runs on its own short-interval thread rather than piggybacking on
# _proactive_loop's 5-min tick, since a 15-min idle threshold needs finer
# granularity than that. No single-flight guard is needed here the way
# core.session._compress_oldest_history has one — this loop only ever has
# one thread calling run_reflective_session() sequentially, blocking on the
# Groq call each time, so there's no way for two calls from THIS process to
# overlap; cross-process overlap (vs. the standalone script) is what
# core.reflective's on-disk reservation guards against instead.
# ---------------------------------------------------------------------------

_REFLECTIVE_IDLE_SECONDS = 15 * 60   # trigger threshold: 15 min with no user interaction
_REFLECTIVE_POLL_SECONDS = 60        # how often this thread checks the idle clock


def _reflective_loop() -> None:
    """Background thread — see module comment above. Same daemon-thread +
    hot-reload dedup pattern as every other background thread in this file
    (see the threading.enumerate() guard at the bottom)."""
    import core.commands as commands
    while True:
        time.sleep(_REFLECTIVE_POLL_SECONDS)
        try:
            if _proactive_blocked():
                continue
            if time.monotonic() - commands._last_interaction_mono < _REFLECTIVE_IDLE_SECONDS:
                continue
            import core.reflective as reflective
            result = reflective.run_reflective_session()
            if result.get("ran"):
                logger.info(
                    "[REFLECTIVE] session complete — tokens_used=%s insights=%s",
                    result.get("tokens_used"), result.get("insights"),
                )
        except Exception:
            logger.warning("Reflective mode check failed (non-critical)", exc_info=True)

# ---------------------------------------------------------------------------
# Initiative loop — Proactive Intelligence Phase 4's "every 30 minutes
# during active hours" trigger (the other two triggers — conversation
# start, sleep — are wired from core/commands.py and
# scripts/reflective_mode.py respectively; see core/initiative.py's own
# module comment for all three). "Active hours" here means Joan has
# interacted recently enough that a proactive cycle is even worth running —
# reuses _proactive_blocked()'s own idle/busy checks rather than
# duplicating them, so this loop backs off under exactly the same
# conditions the existing proactive-comment path already does.
# ---------------------------------------------------------------------------

_INITIATIVE_TICK_SECONDS = 30 * 60   # spec: "Every 30 minutes during active hours"
_INITIATIVE_ACTIVE_WINDOW_SECONDS = 2 * 60 * 60   # "active hours" — interacted within the last 2h


def _initiative_loop() -> None:
    """Background thread — see module comment above. Same daemon-thread +
    hot-reload dedup pattern as every other background thread in this
    file."""
    import core.commands as commands
    while True:
        time.sleep(_INITIATIVE_TICK_SECONDS)
        try:
            if not memory.is_feature_enabled("proactividad"):
                continue
            if _proactive_blocked():
                continue
            if time.monotonic() - commands._last_interaction_mono >= _INITIATIVE_ACTIVE_WINDOW_SECONDS:
                continue   # not "active hours" — nobody around recently enough to justify a cycle
            from core import initiative
            result = initiative.run_proactive_cycle()
            logger.info("[INITIATIVE] cycle complete — %s", result)
        except Exception:
            logger.warning("Initiative cycle failed (non-critical)", exc_info=True)
        try:
            # Proactive Intelligence Phase 5 — "at conversation pauses":
            # this tick already only fires during idle-but-active windows
            # (see the active-hours check above), a natural pause point.
            # spontaneity_engine.can_trigger() independently rate-limits to
            # 2/day regardless of how often this tick itself fires, so
            # there's no need for a second cadence just for this.
            from core import spontaneity
            spont_result = spontaneity.run_spontaneity_cycle(context_label="conversation_pause")
            if spont_result.get("triggered"):
                logger.info("[SPONTANEITY] cycle complete — %s", spont_result)
        except Exception:
            logger.warning("Spontaneity cycle failed (non-critical)", exc_info=True)


# ---------------------------------------------------------------------------
# Sleep phase watcher — pushes near-real-time 'sleep_phase_update' socket
# events to NÚCLEO HUGO's Estado tab (see core/server.py's
# emit_sleep_phase_update()) while a continuous-sleep subprocess is alive.
#
# Continuous sleep runs as a genuine CHILD PROCESS (see core/sleep_control.py)
# — core/sleep.py's run_continuous_sleep() has no access to THIS process's
# `socketio` object, so it can't emit a socket event directly no matter how
# much it might want to. The only channel it has into this process is
# data/sleep_budget.json's 'continuous' state (see
# core.sleep.save_continuous_state(), written every phase). This loop is
# what turns that file into a live push: poll it frequently while sleep is
# active, and emit only when something a viewer would actually notice —
# running/current_cycle/current_phase_num — actually changed, rather than
# firing on every tick regardless.
#
# Deliberately a SEPARATE, faster loop from core.sleep_control._sleep_loop
# (which only needs to tick once a minute for idle-triggering/reaping) —
# phases can finish in well under a minute, so this polls every few seconds
# instead, but only does any real work while something is actually
# sleeping.
# ---------------------------------------------------------------------------

_SLEEP_PHASE_WATCH_SECONDS = 3


def _sleep_phase_watch_loop() -> None:
    """Background thread — see module comment above. Same daemon-thread +
    hot-reload dedup pattern as every other background thread in this file
    (see the threading.enumerate() guard at the bottom)."""
    from core import sleep_control

    last_seen: tuple | None = None
    while True:
        time.sleep(_SLEEP_PHASE_WATCH_SECONDS)
        try:
            running = sleep_control.is_continuous_sleep_running()
            if not running and last_seen is None:
                continue   # nothing to watch and nothing to report a stop for — the common case, stays cheap
            import core.sleep as sleep_mod
            state = sleep_mod.load_continuous_state()
            current = (running, state.get("current_cycle"), state.get("current_phase_num"), state.get("current_phase"))
            if current == last_seen:
                continue
            last_seen = current if running else None
            import core.server as server_mod
            server_mod.emit_sleep_phase_update({
                "running":           running,
                "current_cycle":     state.get("current_cycle", 0),
                "current_phase_num": state.get("current_phase_num", 0),
                "current_phase":     state.get("current_phase", ""),
            })
        except Exception:
            logger.warning("Sleep phase watch failed (non-critical)", exc_info=True)

