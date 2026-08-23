# ═══════════════════════════════════════════════════════════════════════════
# SESSION — rolling conversation-history buffer, session-end bookkeeping, and
# assembling the full message list (system prompt + history + user turn) sent
# to Groq. Split out of core/commands.py (pure refactor, no behavior change).
# ═══════════════════════════════════════════════════════════════════════════
import atexit
import datetime
import json
import logging
import os
import threading

from core import memory
from core import personality as personality_mod
from core import groq_client

logger = logging.getLogger(__name__)

MAX_HISTORY = 40   # conversation turns kept in the rolling history buffer

# When _history exceeds MAX_HISTORY, the oldest HISTORY_COMPRESS_CHUNK turns
# are summarized (see _compress_oldest_history) instead of just discarded —
# so long sessions never truly lose earlier context, it just gets
# progressively more compressed into _history_summary.
HISTORY_COMPRESS_CHUNK = 20

# ---------------------------------------------------------------------------
# Conversation history
#
# When _history grows past MAX_HISTORY, the oldest HISTORY_COMPRESS_CHUNK
# turns are summarized into _history_summary (a running paragraph, folded
# together with any prior summary on each compression pass — see
# _compress_oldest_history) instead of just being dropped, so long sessions
# never truly lose earlier context. Compression runs in a background
# thread, same fire-and-forget pattern as _extract_and_save_memory below,
# so it never blocks the response the user is waiting for — _history may
# transiently exceed MAX_HISTORY by a bit until it finishes.
# ---------------------------------------------------------------------------

_history: list[dict] = []
_history_summary: str = ""
_history_compressing = False   # guards against overlapping compression threads
_history_lock = threading.Lock()


def _add_history(role: str, content: str) -> None:
    global _history_compressing
    should_compress = False
    with _history_lock:
        _history.append({"role": role, "content": content})
        if len(_history) > MAX_HISTORY and not _history_compressing:
            _history_compressing = True
            should_compress = True
    if should_compress:
        threading.Thread(
            target=_compress_oldest_history, daemon=True, name="history-compressor",
        ).start()


def _get_history_snapshot() -> list[dict]:
    with _history_lock:
        return list(_history)


def _get_history_summary() -> str:
    with _history_lock:
        return _history_summary


def _compress_oldest_history() -> None:
    """Summarize the oldest HISTORY_COMPRESS_CHUNK turns into one compact
    paragraph — a quick, fast-model call (GROQ_MODEL_FALLBACK via
    _groq_complete_fast, capped at 200 tokens) — then splice them out of
    _history. Any existing _history_summary is folded into the same call so
    the running summary stays a single coherent paragraph across many
    compression passes instead of concatenating indefinitely. Never raises;
    on any failure the oldest turns are left in place and retried on the
    next _add_history call that finds _history over the cap.
    """
    global _history_summary, _history_compressing
    try:
        with _history_lock:
            if len(_history) <= MAX_HISTORY:
                return
            oldest           = _history[:HISTORY_COMPRESS_CHUNK]
            existing_summary = _history_summary

        transcript_block = "\n".join(
            f"{'Usuario' if h['role'] == 'user' else 'LIRA'}: {h['content']}" for h in oldest
        )
        prompt = (
            (f"Resumen previo:\n{existing_summary}\n\n" if existing_summary else "")
            + "Mensajes a incorporar al resumen:\n" + transcript_block
        )
        new_summary = groq_client._groq_complete_fast(
            [
                {"role": "system", "content": (
                    "Resumes conversaciones de forma breve y neutral, en tercera "
                    "persona, sin opiniones ni relleno. Devuelve UN solo párrafo "
                    "compacto con lo esencial (temas tratados, decisiones, contexto "
                    "relevante) para que otra IA lo use como memoria de contexto. "
                    "Si te doy un resumen previo, intégralo en el nuevo resumen "
                    "actualizado en vez de repetirlo aparte."
                )},
                {"role": "user", "content": prompt},
            ],
            max_tokens=200,
        ).strip()

        with _history_lock:
            if new_summary:
                _history_summary = new_summary
                del _history[:HISTORY_COMPRESS_CHUNK]
    except Exception:
        logger.warning("History compression failed (non-critical)", exc_info=True)
    finally:
        with _history_lock:
            _history_compressing = False


_session_state_lock = threading.Lock()


def _save_session_end_state() -> None:
    """Persist how this session ended: the wall-clock time of the last
    genuine interaction (_last_interaction_wall — NOT 'now', so repeated
    30-min idle ticks don't keep pushing the timestamp forward while
    nothing is actually happening), plus either the most recently
    extracted episode's summary or the last 2-3 raw messages as a
    fallback. Called by _end_of_session_bookkeeping() at the same
    'session end' signals as episode extraction (atexit, 30-min idle) —
    see that function. Best-effort; never raises."""
    try:
        import core.commands as commands   # last-interaction timestamp lives on the dispatch loop
        history = _get_history_snapshot()
        last_messages = [
            f"{'Usuario' if h['role'] == 'user' else 'LIRA'}: {h['content']}"
            for h in history[-3:]
        ]

        last_episode_summary = None
        try:
            episodes = memory._load_episodes()
            if episodes:
                last_episode_summary = episodes[-1].get("summary")
        except Exception:
            pass

        state = {
            "ended_at":             commands._last_interaction_wall,
            "last_episode_summary": last_episode_summary,
            "last_messages":        last_messages,
        }
        os.makedirs(os.path.dirname(memory.SESSION_STATE_PATH) or ".", exist_ok=True)
        with _session_state_lock:
            with open(memory.SESSION_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.debug("Could not save session-end state (non-critical)", exc_info=True)


def _end_of_session_bookkeeping() -> None:
    """Combined 'this session looks like it's ending' hook — episode
    extraction, then the CONTEXTO TEMPORAL snapshot for next time (in that
    order, so a freshly extracted episode is available to save if one was
    just pulled out). Used at both session-boundary signals: 30-min idle
    (_proactive_loop) and process shutdown (atexit, registered at the
    bottom of core/commands.py).

    Gated as a whole in TEST MODE — _save_session_end_state() persists raw
    conversation excerpts (last_messages) even without a real episode, so
    skipping only memory._extract_episodes_for_session() wouldn't be enough to
    keep a test-mode conversation fully ephemeral."""
    if memory.is_feature_enabled("modo_test"):
        logger.info("[TEST MODE] memory extraction skipped")
        return
    memory._extract_episodes_for_session()
    _save_session_end_state()


def _get_messages_with_history(
    user_content: str,
    personality: str | None = None,
    tone: str | None = None,
    relevance_query: str | None = None,
) -> list[dict]:
    if personality is None:
        with personality_mod._personality_lock:
            personality = personality_mod._personality
    msgs = [{
        "role": "system",
        "content": personality_mod._build_system_prompt(personality, tone=tone, relevance_query=relevance_query),
    }]

    # Compressed summary of older turns (see _compress_oldest_history) goes
    # in as its own system message, right at the start of history — the
    # raw recent turns below it stay verbatim.
    summary = _get_history_summary()
    if summary:
        msgs.append({"role": "system", "content": f"Resumen de conversación anterior: {summary}"})

    msgs.extend(_get_history_snapshot())
    msgs.append({"role": "user", "content": user_content})
    return msgs


def _maybe_emit_panel(intent: str, transcript: str) -> None:
    """Best-effort: emit a 'show_panel' event for weather/time queries so
    the frontend can animate in a contextual panel while LIRA speaks. Never
    raises, never affects the actual reply (see module comment above).
    Called right after intent detection, before the reply is generated —
    see core.commands._dispatch_command_impl."""
    if not memory.is_feature_enabled("paneles_dinamicos"):
        return
    try:
        import core.server as server_mod
        from core import tools
        from core import intent as intent_mod

        if intent in ("get_time", "get_date"):
            now = datetime.datetime.now()
            server_mod.emit_show_panel({
                "type": "time",
                "time": now.strftime("%H:%M"),
                "date": f"{memory._DAYS_ES[now.weekday()]}, {now.day} de {memory._MONTHS_ES[now.month - 1]} de {now.year}",
            })
            return

        if intent_mod._WEATHER_QUERY_RE.search(transcript):
            loc = tools.get_location()
            if not (loc.get("lat") and loc.get("lon")):
                return
            # Debug log (LIRA weather self-awareness fix) — see the matching
            # log in core.personalities.base._build_system_prompt. Confirms
            # weather was detected and actually fetched for this turn's
            # panel, independent of what the model ends up saying out loud.
            logger.debug("[WEATHER] weather intent detected — get_weather() called for lat=%s lon=%s", loc["lat"], loc["lon"])
            w = tools.get_weather(loc["lat"], loc["lon"])
            if not w:
                return
            server_mod.emit_show_panel({
                "type":       "weather",
                "temp":       w["temperature"],
                "feels_like": w["feels_like"],
                "condition":  w["condition"],
                "icon":       intent_mod._weather_icon_category(w["condition"]),
                "humidity":   w["humidity"],
                "wind":       w["wind_speed"],
            })
    except Exception:
        logger.debug("Panel emit skipped (non-critical)", exc_info=True)


# Final episode extraction + CONTEXTO TEMPORAL snapshot on process shutdown
# ("when jarvis.py shuts down" — this fires on normal interpreter exit
# regardless of which module triggered it, so it works without touching
# jarvis.py itself). See _end_of_session_bookkeeping() — episode extraction
# no-ops if nothing new happened since the last one, but the session-state
# save always runs so the NEXT session's CONTEXTO TEMPORAL block has an
# accurate "ended_at" timestamp.
atexit.register(_end_of_session_bookkeeping)
