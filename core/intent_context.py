# ═══════════════════════════════════════════════════════════════════════════
# INTENT CONTEXT — web-search confidence/query cleaning, tone detection,
# implicit-context inference, and the weather-icon category mapper. All pure
# functions (only read-only shared state: _WEATHER_QUERY_RE), no mutable
# state written back by callers. Split out of core/intent.py (pure refactor,
# no behavior change).
#
# _recurring_topic reaches back into core.commands only via a function-local
# `import core.commands as commands` (same lazy-import pattern used
# throughout this codebase to avoid a circular import).
# ═══════════════════════════════════════════════════════════════════════════
import re
import datetime

from core import memory

# ---------------------------------------------------------------------------
# Web search — conservative by design, to preserve API credits. Only two
# narrow, deterministic categories reach _detect_intent's web_search branch:
#
#   1. Explicit "search for X" requests — the user directly asked, an
#      unambiguous signal.
#   2. A strict, literal keyword list for current/recent information: hoy,
#      ahora, últimas noticias, actualmente, precio actual, este año, esta
#      semana. Nothing broader (no "noticias" alone, no generic price/quote
#      phrasing, no vague "qué está pasando") — those over-triggered before
#      and cost API credits on questions answerable from training data.
#
# This deliberately does NOT try to regex-detect "a specific event that
# postdates training cutoff" or "LIRA genuinely doesn't know" (task
# categories 2 and 3) — both are semantic judgment calls that would need
# another LLM call to classify, defeating the point of preserving credits.
# They're left to fall through to intent="unknown" and answered from
# training data, which is the conservative-by-default outcome anyway.
# ---------------------------------------------------------------------------
_EXPLICIT_SEARCH_REQUEST_RE = re.compile(
    r"\b(busca(?:me)?(?:\s+en\s+internet)?|puedes\s+buscar|consulta\s+en\s+internet|"
    r"mira\s+en\s+internet|b[uú]scalo|investiga\s+en\s+internet)\b",
    re.IGNORECASE,
)
_CURRENT_INFO_KEYWORD_RE = re.compile(
    r"\b(hoy|ahora|[uú]ltimas\s+noticias|actualmente|precio\s+actual|"
    r"este\s+a[ñn]o|esta\s+semana)\b",
    re.IGNORECASE,
)

# Overlap guard: "hoy"/"ahora" alone can't tell a genuine search need apart
# from a weather/location question (no dedicated intent category — those
# are answered from the live data already injected into every system
# prompt) or a question about the user's own armor/project context (already
# injected into LIRA's prompt separately) — both explicitly listed as
# "never search" cases. Reused by _web_search_confidence() below.
_WEB_SEARCH_EXCLUDE_RE = re.compile(
    r"\b(clima|tiempo\s+hace|pron[oó]stico|temperatura|lluvia|"
    r"d[oó]nde\s+estoy|mi\s+ubicaci[oó]n|d[oó]nde\s+est[aá]|"
    r"armadura|coraza|armor[ií]a|mi\s+proyecto|concepto\s+guardado)\b",
    re.IGNORECASE,
)


def _web_search_confidence(transcript: str) -> float:
    """Heuristic confidence [0, 1] that `transcript` genuinely needs a live
    web search rather than an answer from training data. Only ever called
    for transcripts that already passed _detect_intent's narrow web_search
    regex — this is the second, independent gate: below 0.8 the caller
    skips the search entirely (logs "[SEARCH SKIPPED]") even though the
    regex matched, catching the cases where a bare keyword like "hoy"
    overlapped with weather/location/armor context instead of a genuine
    current-info need."""
    if _WEB_SEARCH_EXCLUDE_RE.search(transcript):
        return 0.0
    if _EXPLICIT_SEARCH_REQUEST_RE.search(transcript):
        return 0.95
    if _CURRENT_INFO_KEYWORD_RE.search(transcript):
        return 0.85
    return 0.5   # shouldn't normally be reached — defensive default, below threshold


# ---------------------------------------------------------------------------
# Web search query cleaning
#
# Always built from the raw transcript, NEVER from user_content (which may
# carry the conversation-context wrapper) and NEVER from memory/history — so
# the user's name or any personal context can't leak into the search query.
# ---------------------------------------------------------------------------

_SEARCH_FILLER_RE = re.compile(
    r"\b(busca(?:me)?(?:\s+en\s+internet)?|puedes\s+buscar(?:\s+en\s+internet)?|"
    r"consulta\s+en\s+internet|mira\s+en\s+internet|investiga\s+en\s+internet|"
    r"b[uú]scalo|por\s+favor|oye|lira)\b",
    re.IGNORECASE,
)


def _clean_search_query(transcript: str) -> str:
    """Strip explicit search-trigger phrases, wake words and filler so only
    the core question reaches the search API. Falls back to the raw
    transcript if cleaning would otherwise leave nothing."""
    cleaned = _SEARCH_FILLER_RE.sub(" ", transcript)
    cleaned = re.sub(r"[¿?¡!]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or transcript.strip()

# ---------------------------------------------------------------------------
# Tone detection — simple heuristic classifier, run on the raw transcript
# before dispatching to the LLM. Not a hard behavioral gate, just a nudge:
# the detected tone is injected into the system prompt (see
# _build_system_prompt's tone parameter) so LIRA can be gentler with a
# tired user or more energetic with an excited one, without a separate
# LLM call to classify it (would cost latency/credits for no real benefit).
# ---------------------------------------------------------------------------

_STRESSED_WORDS_RE = re.compile(
    r"\b(necesito|urgente|no\s+s[eé]\s+qu[eé]\s+hacer|ay[uú]dame|agobiad[oa]|estresad[oa])\b",
    re.IGNORECASE,
)
_TIRED_WORDS_RE = re.compile(
    r"\b(meh|da\s+igual|nada|cansad[oa]|sin\s+ganas|agotad[oa])\b",
    re.IGNORECASE,
)
_CAPS_WORD_RE = re.compile(r"[A-ZÁÉÍÓÚÑ]{3,}")


def _detect_tone(text: str) -> str:
    """Return one of: 'neutral', 'cansado / con poca energía',
    'emocionado / con energía', 'estresado'. Checked in that priority
    order — stressed language wins over an exclamation mark, excitement
    wins over a short message, so a short-but-urgent "ayúdame" doesn't get
    misread as merely tired."""
    stripped = text.strip()
    if not stripped:
        return "neutral"

    if _STRESSED_WORDS_RE.search(stripped):
        return "estresado"

    exclamations   = stripped.count("!") + stripped.count("¡")
    question_marks = stripped.count("?") + stripped.count("¿")
    if exclamations >= 1 or question_marks >= 2 or _CAPS_WORD_RE.search(stripped):
        return "emocionado / con energía"

    if _TIRED_WORDS_RE.search(stripped) or len(stripped.split()) <= 2:
        return "cansado / con poca energía"

    return "neutral"

# ---------------------------------------------------------------------------
# Implicit context — "what the user hasn't said but might be relevant."
#
# Combines time of day, recent recurring topics, detected tone and memory
# facts into a short, honestly-hedged note (never asserted as fact) — see
# _build_system_prompt's CONTEXTO IMPLÍCITO block. Deliberately brief: this
# is meant to nudge LIRA to read between the lines, not to hand her a
# fabricated psychological profile.
# ---------------------------------------------------------------------------

def _recurring_topic(transcript: str) -> str | None:
    """Simple keyword matching (see _keywords) across the last 3 user turns
    plus the current transcript — returns the most-repeated keyword if any
    appears 2+ times, else None. Deliberately basic: this flags a recurring
    theme (e.g. 'examen' mentioned three messages running), not a deep
    topic model."""
    # Lazy import — core.commands imports this module at top level, so a
    # top-level import here would be circular.
    import core.commands as commands
    history  = commands._get_history_snapshot()
    user_msgs = [h["content"] for h in history if h.get("role") == "user"][-3:]
    if not user_msgs:
        return None

    counts: dict[str, int] = {}
    for msg in user_msgs + [transcript]:
        for kw in memory._keywords(msg):
            counts[kw] = counts.get(kw, 0) + 1

    recurring = [(count, kw) for kw, count in counts.items() if count >= 2]
    if not recurring:
        return None
    recurring.sort(reverse=True)
    return recurring[0][1]


def _infer_implicit_context(transcript: str, tone: str, relevant_facts: list[dict]) -> str:
    """Best-effort, honestly-hedged notes on what might be going on beneath
    the literal message — time of day, tone, a recurring topic across
    recent turns, and the top memory fact already judged relevant to this
    message (see _select_relevant_facts). Returns '' when nothing notable
    stands out, rather than forcing a note — never invents context, only
    flags real signals already available."""
    notes = []

    hour = datetime.datetime.now().hour
    if hour >= 23 or hour < 6:
        notes.append("es tarde — podría estar cansado o preferir una respuesta breve")

    if tone == "estresado":
        notes.append("su tono suena estresado aunque no lo haya dicho explícitamente")
    elif tone == "cansado / con poca energía":
        notes.append("su tono suena bajo de energía")

    recurring = _recurring_topic(transcript)
    if recurring:
        notes.append(
            f"ha mencionado '{recurring}' varias veces en los últimos mensajes — "
            "podría seguir siendo relevante aunque no lo repita ahora"
        )

    if relevant_facts:
        notes.append(f"por memoria, esto podría conectar con: {relevant_facts[0]['fact']}")

    return "; ".join(notes)

# ---------------------------------------------------------------------------
# Contextual panels — 'show_panel' socket event (see core/server.py's
# emit_show_panel()) that lets the main menu animate in a visual side panel
# (weather, time, ...) while LIRA speaks about that topic.
#
# Purely additive: _maybe_emit_panel() (see core/session.py) never changes
# what she actually says or how. Weather questions still fall through
# intent="unknown" straight to _groq_complete() exactly as before (see the
# intent pipeline in _dispatch_command_impl); get_time/get_date still go
# through _execute_action()/_format_response() exactly as before. That
# function just decides, independently and right after intent detection,
# whether today's turn ALSO deserves a panel — gathering the same live data
# that's already available (tools.get_weather(), datetime.now()) into the
# small JSON payload the frontend expects, and emitting it before the reply
# is generated. Any failure there (no location fix, weather fetch down, no
# socket clients) is swallowed — a missing panel is cosmetic, never worth
# breaking or even delaying the actual spoken answer over.
# ---------------------------------------------------------------------------

_WEATHER_QUERY_RE = re.compile(
    r"\b(clima|tiempo\s+hace|el\s+tiempo|qu[eé]\s+tiempo|pron[oó]stico|temperatura|"
    r"llueve|va\s+a\s+llover|lluvia|"
    r"hace\s+(?:mucho\s+|bastante\s+|much[ií]simo\s+)?(?:sol|calor|fr[ií]o|viento))\b",
    re.IGNORECASE,
)


def _weather_icon_category(condition: str) -> str:
    """Map tools.get_weather()'s Spanish condition text to one of the 5
    icon categories the frontend draws (see WEATHER_ICONS in the script).
    Keyword-matched against the condition string rather than the raw WMO
    code — tools.get_weather() computes the code internally but doesn't
    expose it in the returned dict, and this stays self-contained without
    needing to touch core/tools.py. Order matters: checked most-specific-
    first so e.g. 'tormenta con granizo' hits 'stormy' before 'granizo'
    could pull it toward 'rainy'."""
    c = (condition or "").lower()
    if "tormenta" in c:
        return "stormy"
    if "niebla" in c:
        return "foggy"
    if any(w in c for w in ("lluvia", "llovizna", "chubasco", "granizo")):
        return "rainy"
    if "despejado" in c:
        return "sunny"
    return "cloudy"   # nublado, nevada/nieve (no dedicated snow icon), or unmapped
