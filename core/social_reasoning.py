# ═══════════════════════════════════════════════════════════════════════════
# SOCIAL REASONING — Phase 1 of the conversational intelligence system.
#
# Wake-word detection (core/wake_word.py) only answers "was the name heard?".
# It says nothing about whether the speaker was actually addressing Hugo
# ("Hugo, ¿puedes hacer esto?") vs. talking ABOUT her ("Hugo puede hacer
# esto.", "Creo que Hugo debería aprender esto.") or the name being
# incidental ("Le estaba enseñando Hugo a un amigo."). This module answers
# that question — and, symmetrically, whether an utterance inside the
# post-response context window (core/listener.py's _CONTEXT_WINDOW_SECS) is
# a genuine continuation of the exchange or unrelated speech picked up
# while the mic happened to be open.
#
# Deliberately reasoning-based, not pattern-based: the primary path is a
# small local Ollama model (llama3.2:1b) given a handful of worked examples
# and asked to judge the NEW utterance by analogy — "a list of examples
# trains the behavior but doesn't define it rigidly" (spec requirement).
# The regex fallback below only exists for when Ollama is unreachable; it is
# deliberately coarse and, per spec, always errs toward responding.
#
# Same dependency-light urllib.request approach as core.sleep_llm /
# core.ollama_control (no extra requirements, and this module needs to be
# usable straight from core/listener.py's audio thread without pulling in
# anything heavy).
# ═══════════════════════════════════════════════════════════════════════════
import datetime
import json
import logging
import re
import threading
import urllib.request

logger = logging.getLogger(__name__)

OLLAMA_HOST         = "http://localhost:11434"
OLLAMA_GENERATE_URL = f"{OLLAMA_HOST}/api/generate"
OLLAMA_TAGS_URL     = f"{OLLAMA_HOST}/api/tags"
INTENT_MODEL        = "llama3.2:1b"

# Fast local check — a 1b model on a short prompt should answer in well
# under a second locally; this timeout is a safety ceiling, not a target.
# Reachability probe gets its own short timeout so an unreachable daemon
# fails fast into the regex fallback rather than stalling the wake-word
# pipeline.
_PROBE_TIMEOUT_SECS = 1.0
_CALL_TIMEOUT_SECS  = 2.5


def _ollama_available() -> bool:
    try:
        req = urllib.request.Request(OLLAMA_TAGS_URL, method="GET")
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT_SECS) as resp:
            return resp.status == 200
    except Exception:
        return False


def _ollama_judge(system: str, prompt: str) -> bool | None:
    """One /api/generate call, expecting a single SI/NO token back. Returns
    None (not False) on any failure so the caller can fall through to the
    regex heuristic instead of silently treating an unreachable Ollama as
    'don't respond'. Never raises."""
    try:
        payload = json.dumps({
            "model":   INTENT_MODEL,
            "prompt":  prompt,
            "system":  system,
            "stream":  False,
            "options": {"num_predict": 5, "temperature": 0.0},
        }).encode("utf-8")
        req = urllib.request.Request(
            OLLAMA_GENERATE_URL, data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=_CALL_TIMEOUT_SECS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        answer = str(data.get("response", "")).strip().upper()
        if answer.startswith(("SI", "SÍ", "YES")):
            return True
        if answer.startswith("NO"):
            return False
        logger.debug("[SOCIAL] Ambiguous Ollama answer %r — falling back to regex", answer)
        return None
    except Exception as e:
        logger.debug("[SOCIAL] Ollama intent check failed: %s", e)
        return None


# ── Wake-word path: "is Hugo actually being addressed?" ─────────────────────

_ADDRESSED_SYSTEM = (
    "Eres el módulo de razonamiento social de un asistente de voz llamado "
    "Hugo. Tu única tarea es decidir, para la frase que se te da, si "
    "quien habla se está dirigiendo DIRECTAMENTE al asistente — pidiéndole "
    "algo, saludándolo, invocándolo — o si en cambio está hablando SOBRE el "
    "asistente en tercera persona, mencionando su nombre de forma "
    "incidental, o dirigiéndose a otra persona. Responde con una sola "
    "palabra: SI si se dirige al asistente, NO si no.\n\n"
    "Ejemplos:\n"
    "\"Hugo, ¿puedes hacer esto?\" -> SI\n"
    "\"¿Hugo?\" -> SI\n"
    "\"Hugo...\" -> SI\n"
    "\"Hugo busca esto en internet\" -> SI\n"
    "\"Hugo puede hacer esto.\" -> NO\n"
    "\"Creo que Hugo debería aprender esto.\" -> NO\n"
    "\"Le estaba enseñando Hugo a un amigo.\" -> NO\n"
)

# Regex fallback (Ollama unreachable only): catch the clearest "talking
# ABOUT Hugo" shape — name followed by a third-person verb — so the most
# obvious false triggers are still filtered without the LLM. Anything less
# clear-cut passes through as addressed, per the spec's err-on-the-side-of-
# responding rule.
_ABOUT_RE = re.compile(
    r"\bhugo\b\s+"
    r"(puede|podr[ií]a|deber[ií]a|debe|es|era|fue|tiene|sabe|aprende|aprender[ií]a)\b",
    re.IGNORECASE,
)
_THIRD_PARTY_RE = re.compile(
    r"\b(le\s+esta(ba)?|le\s+ense[ñn]|a\s+(un|una)\s+amig[oa])\b.{0,40}"
    r"\bhugo\b",
    re.IGNORECASE,
)


def _regex_addressed(utterance: str) -> bool:
    if _ABOUT_RE.search(utterance) or _THIRD_PARTY_RE.search(utterance):
        return False
    return True


def is_addressed(utterance: str) -> bool:
    """True if a wake-word-triggered utterance appears to actually address
    the assistant, vs. merely mentioning its name. Uncertain → True (per
    spec: better to respond unnecessarily than miss a genuine request)."""
    utterance = (utterance or "").strip()
    if not utterance:
        return True
    if _ollama_available():
        result = _ollama_judge(_ADDRESSED_SYSTEM, f'Frase: "{utterance}"\nRespuesta (SI/NO):')
        if result is not None:
            return result
    return _regex_addressed(utterance)


# ── Context-window path: "is this a continuation of our conversation?" ──────

_CONTINUATION_SYSTEM = (
    "Eres el módulo de razonamiento social de un asistente de voz llamado "
    "Hugo. Acabas de responder al usuario hace pocos segundos, y ahora se "
    "detectó una frase nueva SIN que el usuario diga tu nombre. Decide si "
    "esa frase es una continuación natural de la conversación que sigue "
    "dirigida a ti (una respuesta, una instrucción de seguimiento, una "
    "corrección) o si es habla no relacionada que el micrófono capturó de "
    "fondo (hablando con otra persona, un comentario ajeno). Responde con "
    "una sola palabra: SI si es continuación, NO si no.\n\n"
    "Ejemplos:\n"
    "\"Ahora mismo no puedo\" -> SI\n"
    "\"Vale pues haz aquello\" -> SI\n"
    "\"busca esto en internet\" -> SI\n"
)


def should_continue(utterance: str) -> bool:
    """True if an utterance inside the post-response context window (no
    wake word present) reads as a continuation of the ongoing exchange.
    Uncertain → True, same bias as is_addressed(), and the regex fallback
    has no reliable signal here so it always returns True — only Ollama can
    meaningfully distinguish background chatter from a real continuation."""
    utterance = (utterance or "").strip()
    if not utterance:
        return False
    if _ollama_available():
        result = _ollama_judge(_CONTINUATION_SYSTEM, f'Frase: "{utterance}"\nRespuesta (SI/NO):')
        if result is not None:
            return result
    return True


# ═══════════════════════════════════════════════════════════════════════════
# PHASE 2 — pre-intervention gate: "does it actually make sense to speak
# right now?"
#
# Distinct from is_addressed/should_continue above (which only answer "was
# Hugo's name actually meant for her"). This is the broader judgment call
# run right before ANY intervention happens — wake word, context-window
# continuation, or core.background_loops' periodic proactive tick all funnel
# through the same should_intervene() below. Same reasoning-over-rules
# approach as the rest of this module: one short Ollama call weighing four
# questions, never a checklist of regex conditions.
#
#   ¿Me han hablado realmente?               — is she actually being addressed
#   ¿Estoy invitada a participar?             — is her voice expected/welcome
#   ¿Tengo algo útil que aportar?             — does she genuinely have something to say
#   ¿Mi intervención mejora la conversación?  — does speaking help, or just fill air
#
# INTERVENIR only when the answers clearly favor speaking (the first two
# alone are enough when unambiguous, per spec). SILENCIO is a considered
# decision, logged as one — not a failure, and not the same thing as never
# responding: see cap_consecutive_silence below for why a direct question
# never gets ignored twice in a row.
# ═══════════════════════════════════════════════════════════════════════════

# Spec requirement: the check itself must complete in well under a second —
# a hard ceiling, not a target, same role _PROBE_TIMEOUT_SECS/_CALL_TIMEOUT_SECS
# play above. Kept separate from those because this call also skips the
# system-prompt few-shot examples (there's no fixed set of INTERVENIR/
# SILENCIO examples the way is_addressed/should_continue have — this is a
# live judgment call each time), so it stays fast even before considering
# the timeout.
_INTERVENE_TIMEOUT_SECS = 1.0

_INTERVENE_SYSTEM = (
    "Eres el módulo de razonamiento social de Hugo, una asistente de voz. Antes de "
    "intervenir te preguntas: ¿me han hablado realmente?, ¿estoy invitada a "
    "participar?, ¿tengo algo útil que aportar?, ¿mi intervención mejora la "
    "situación? Responde con una sola palabra: INTERVENIR o SILENCIO. Nada más, "
    "sin explicación."
)

# Consecutive-SILENCIO streak for the reactive gate only (cap_consecutive_
# silence=True callers) — see should_intervene's docstring. Deliberately
# module-level/global rather than per-conversation-object state: this
# process only ever holds one live conversation with Joan at a time, same
# assumption core.listener's _last_response_mono/_last_response_personality
# already make.
_consecutive_silences      = 0
_silence_streak_lock       = threading.Lock()


def _ollama_intervene_verdict(prompt: str) -> bool | None:
    """Same shape as _ollama_judge above but parses INTERVENIR/SILENCIO
    instead of SI/NO — kept as its own function rather than a shared parser
    since the two gates use different vocabularies, different system
    prompts, and (per spec item 5) a different failure default: this one
    always defaults to INTERVENIR on any failure, with no regex fallback,
    rather than falling through to a secondary heuristic."""
    try:
        payload = json.dumps({
            "model":   INTENT_MODEL,
            "prompt":  prompt,
            "system":  _INTERVENE_SYSTEM,
            "stream":  False,
            "options": {"num_predict": 5, "temperature": 0.0},
        }).encode("utf-8")
        req = urllib.request.Request(
            OLLAMA_GENERATE_URL, data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=_INTERVENE_TIMEOUT_SECS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        answer = str(data.get("response", "")).strip().upper()
        if answer.startswith("INTERV"):
            return True
        if answer.startswith("SIL"):
            return False
        logger.debug("[SOCIAL] Ambiguous intervene verdict %r — defaulting to INTERVENIR", answer)
        return None
    except Exception as e:
        logger.debug("[SOCIAL] Intervene check failed (defaulting to INTERVENIR): %s", e)
        return None


def should_intervene(context: str, section: str = "", *, cap_consecutive_silence: bool = True) -> bool:
    """The Phase 2 gate — run right before any intervention actually
    happens, whatever triggered it.

    Args:
        context: Plain-language snapshot of what's just been said (e.g. the
                 last ~30s of transcribed conversation for the reactive
                 gate, or the fuller session snapshot core.background_loops
                 already builds for the proactive tick).
        section: Active HUD section, if any.
        cap_consecutive_silence: True (default) enforces spec item 3 —
                 "maximum one silence per conversation before responding":
                 a second SILENCIO in a row is overridden to INTERVENIR so a
                 repeated direct question is never ignored twice. Pass
                 False for the periodic proactive tick, where SILENCIO is
                 the normal, expected outcome (already rate-limited by its
                 own caps in core.background_loops) and has nothing to do
                 with ignoring a question.

    Ollama unreachable or an ambiguous answer -> True (spec item 5: never
    block a response over an infra hiccup; there's no regex fallback here
    the way is_addressed/should_continue have, since a generic 'does it
    make sense to speak' judgment has no reliable pattern to fall back on)."""
    global _consecutive_silences

    now_str = datetime.datetime.now().strftime("%H:%M")
    prompt = (
        f"Contexto: {context or '(sin conversación reciente)'}. "
        f"Situación: {section or 'sin sección activa'}. Hora: {now_str}. "
        "¿Tiene sentido que intervenga ahora? ¿Me están hablando? ¿Tengo algo útil "
        "que aportar? ¿Mi intervención mejora la situación? Responde solo: "
        "INTERVENIR o SILENCIO."
    )

    verdict = _ollama_intervene_verdict(prompt) if _ollama_available() else None
    result  = True if verdict is None else verdict

    if cap_consecutive_silence:
        with _silence_streak_lock:
            if not result and _consecutive_silences >= 1:
                logger.info("[SOCIAL] overriding repeated SILENCIO — direct question, responding anyway")
                result = True
            _consecutive_silences = 0 if result else _consecutive_silences + 1

    logger.info("[SOCIAL] decided: %s", "intervene" if result else "silence")
    return result


def recent_conversation_snippet(transcript: str, max_chars: int = 500) -> str:
    """Best-effort 'last ~30s of conversation' snapshot for the reactive
    gate. core.session's history buffer has no per-turn timestamps (see its
    own module comment) so there's no exact 30-second slice to pull — the
    last couple of turns plus the current utterance covers the same span in
    practice for a live back-and-forth, which is what this judgment call
    actually needs (recency, not a precise clock)."""
    turns = ""
    try:
        from core import session as session_mod
        snapshot = session_mod._get_history_snapshot()[-2:]
        turns = " / ".join(f"{t['role']}: {t['content'][:150]}" for t in snapshot)
    except Exception:
        logger.debug("recent_conversation_snippet: history lookup failed", exc_info=True)
    parts = [p for p in (turns, f"user: {transcript}") if p]
    return " / ".join(parts)[-max_chars:]


def current_hud_section() -> str:
    """Active HUD section label, if any — same source core.background_loops'
    _gather_proactivity_context already reads from for its own snapshot."""
    try:
        import core.server as server_mod
        activity = server_mod.get_user_activity()
        return activity.get("section") or ""
    except Exception:
        return ""
