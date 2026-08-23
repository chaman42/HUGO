# ═══════════════════════════════════════════════════════════════════════════
# ACTIVITY — the HUD co-pilot: HUGO noticing what Joan is doing in the
# interface itself (navigating sections, going idle on a screen), not just
# what he says out loud. Split
# out of core/commands.py (pure refactor, no behavior change).
#
# Two halves:
#
#   1. ACTIVIDAD ACTUAL — a live one-line summary of the frontend's most
#      recent 'user_activity' event (see core/server.py's handler and
#      get_user_activity()), injected into every system prompt by
#      core.personalities.base._build_system_prompt so a normal reply can
#      reference it naturally if relevant.
#
#   2. on_user_activity() — the actual co-pilot: called by core/server.py,
#      off the SocketIO thread, every time a 'user_activity' event arrives.
#      Whether to say something unprompted about it is NEVER a hardcoded
#      rule — it's a single fast LLM call per (throttled) event, given a
#      plain-language description of the activity and asked point-blank
#      whether a comment fits right now or is better left unsaid
#      ([SILENCIO]). Shares core.background_loops' _proactive_lock/
#      _last_proactive_mono/_proactive_count_session — same rate budget
#      (max 1/hour, max 3/session) — so HUD-activity reactions and
#      time-based check-ins never compound into spam; and
#      core.background_loops._proactive_blocked() — never interrupts. Two
#      structural (non-judgment) guards on top: never re-consider the exact
#      same activity twice in a row (_last_activity_signature), and never
#      consult the LLM more than once every _ACTIVITY_OBSERVER_MIN_INTERVAL
#      seconds (_last_activity_check_mono) — without this, a debounced-but-
#      still-frequent event like typing would fire a fresh reasoning call on
#      every keystroke pause.
# ═══════════════════════════════════════════════════════════════════════════
import json
import logging
import threading
import time

from core import memory
from core import personality as personality_mod
from core import groq_client
from core import background_loops

logger = logging.getLogger(__name__)

_activity_lock = threading.Lock()
_last_activity_signature: str | None = None
_last_activity_check_mono: float | None = None

_ACTIVITY_OBSERVER_MIN_INTERVAL = 8.0   # seconds between LLM reasoning calls, win or lose

_ACTIVITY_SECTION_NAMES = {
    "main":         "la pantalla principal",
    "chat":         "el chat",
    "system":       "el panel de sistema",
    "settings":     "ajustes",
}


def _describe_activity(section: str, action: str, context: dict) -> str:
    """Natural-language Spanish description of a frontend HUD activity
    event — reused both for the ACTIVIDAD ACTUAL system-prompt line and
    for the observer's own reasoning prompt in on_user_activity()."""
    where = _ACTIVITY_SECTION_NAMES.get(section, section)

    if action == "typing":
        field   = context.get("field", "un campo")
        partial = context.get("partial_text", "")
        return f"está escribiendo en {where}, campo '{field}': \"{partial}\""
    if action == "idle":
        return f"lleva un rato sin interactuar en {where}"
    if action == "navigate":
        return f"acaba de entrar en {where}"
    return f"está en {where} ({action})"


def _describe_hud_context(hud_ctx: dict) -> str:
    """Build PANTALLA ACTUAL's factual content from a
    core.server.get_hud_context() snapshot — see that function and its
    'hud_context' handler for the event shapes. Returns '' when there's
    nothing worth reporting (nothing received yet, or an event missing the
    data it needs), which core.personalities.base._build_system_prompt uses
    to skip the PANTALLA ACTUAL block entirely rather than injecting an
    empty line."""
    ctx_type = hud_ctx.get("type")

    if ctx_type == "idle":
        section = hud_ctx.get("section") or ""
        return f"El usuario está en {section}." if section else ""

    return ""


def on_user_activity(section: str, action: str, context: dict) -> None:
    """Called by core/server.py's 'user_activity' socket handler, already
    off the SocketIO thread. Decides — via a single fast LLM call, never a
    hardcoded rule — whether HUGO should say something unprompted about
    this. See module comment above for the full gating chain."""
    global _last_activity_signature, _last_activity_check_mono
    try:
        if not memory.is_feature_enabled("copiloto_hud"):
            return
        # Already engaged in chat — an unprompted HUD comment on top would
        # just be noise, not a co-pilot being helpful.
        if section == "chat":
            return

        signature = f"{section}:{action}:{json.dumps(context, sort_keys=True, default=str)}"
        with _activity_lock:
            if signature == _last_activity_signature:
                return   # identical activity as last time — never comment twice in a row
            _last_activity_signature = signature

            now = time.monotonic()
            if _last_activity_check_mono is not None and now - _last_activity_check_mono < _ACTIVITY_OBSERVER_MIN_INTERVAL:
                return   # throttle how often we even ask the LLM, independent of the outcome
            _last_activity_check_mono = now

        if background_loops._proactive_blocked():
            return

        with personality_mod._personality_lock:
            personality = personality_mod._personality
        description = _describe_activity(section, action, context)

        with background_loops._proactive_lock:
            if background_loops._proactive_count_session >= background_loops._PROACTIVE_MAX_PER_SESSION:
                return
            now = time.monotonic()
            if (background_loops._last_proactive_mono is not None
                    and now - background_loops._last_proactive_mono < background_loops._PROACTIVE_MIN_INTERVAL):
                return

            observer_system = (
                f"Eres {personality_mod.PERSONALITIES[personality]['display_name'].replace(' ', '')}, la asistente de voz de "
                "Joan, actuando como copiloto que observa lo que hace en la interfaz — igual que JARVIS con "
                "Tony Stark en el taller. Nunca interrumpas si está a mitad de escribir un texto largo. Nunca "
                "comentes la misma actividad dos veces seguidas. Si está en el chat hablando contigo, no "
                "comentes — ya está interactuando. Un comentario debe sentirse como un compañero que se da "
                "cuenta de algo, nunca como un sistema de vigilancia narrando sus acciones. Puedes hacer una "
                "pregunta, una sugerencia o dar un dato relevante — breve, en tu propio tono."
            )
            observer_user = (
                f"El usuario está haciendo esto: {description}. ¿Tiene sentido decir algo ahora, o es mejor "
                "esperar? Si decides comentar, di algo útil, breve y natural. Si no, responde con [SILENCIO]."
            )
            try:
                verdict = groq_client._groq_complete_fast(
                    [
                        {"role": "system", "content": observer_system},
                        {"role": "user", "content": observer_user},
                    ],
                    max_tokens=120,
                ).strip()
            except Exception:
                logger.debug("Activity observer LLM call failed (non-critical)", exc_info=True)
                return

            if not verdict or "[SILENCIO]" in verdict.upper():
                return

            background_loops._last_proactive_mono = now
            background_loops._proactive_count_session += 1
            background_loops._speak_unprompted(personality, verdict)
    except Exception:
        logger.warning("on_user_activity failed (non-critical)", exc_info=True)
