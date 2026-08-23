# ═══════════════════════════════════════════════════════════════════════════
# RESPONSE — formatting a tool result into HUGO's final spoken reply, the
# web-search intent handler, and the static (offline) fallback pool. Split
# out of core/commands.py (pure refactor, no behavior change).
# ═══════════════════════════════════════════════════════════════════════════
import datetime
import logging

from core import memory
from core import tools
from core import intent as intent_mod
from core import personality as personality_mod
from core import groq_client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Response formatting
# ---------------------------------------------------------------------------

def _format_response(
    result: str, transcript: str = "", personality: str | None = None, tone: str | None = None,
) -> str:
    content = f"Resultado: {result}"
    if transcript:
        content += f"\nComando original: {transcript}"
    try:
        return groq_client._groq_complete(
            [
                {"role": "system", "content": personality_mod._build_system_prompt(
                    personality, tone=tone, relevance_query=transcript,
                )},
                {"role": "user",   "content": content},
            ]
        )
    except Exception as e:
        logger.error("Response formatting failed: %s", e, exc_info=True)
        return result

# ---------------------------------------------------------------------------
# Web search — intent="web_search" handling
#
# Query is built from the raw transcript only (see _clean_search_query),
# never from user_content/history/memory, so nothing personal about the
# user ever reaches the search API. Searches Spanish first (the transcript's
# natural language); if that comes up empty, retries once in English via a
# cheap Groq translation. Results are formatted with tools.format_search_results
# (trusted sources tagged [FUENTE FIABLE], everything else [FUENTE]) and fed
# through the normal _format_response pipeline — full personality + memory
# prompt — so HUGO cites her source naturally instead of reciting a list.
# If nothing useful comes back, she's told plainly there were no results, so
# her system-prompt instructions (see memory_instructions.json) have her
# fall back to training data and note her knowledge cutoff instead of
# claiming she searched.
# ---------------------------------------------------------------------------

def _handle_web_search(transcript: str, personality: str, tone: str | None = None) -> str:
    query = intent_mod._clean_search_query(transcript)
    results = tools.search_web(query) if query else []

    if not results and query:
        try:
            # Fast utility call — plain translation, no chain-of-thought needed.
            english_query = groq_client._groq_complete_fast(
                [
                    {"role": "system", "content": (
                        "Traduce la siguiente consulta al inglés para una búsqueda web. "
                        "Responde solo con la traducción, sin comillas ni explicaciones."
                    )},
                    {"role": "user", "content": query},
                ],
                max_tokens=60,
            ).strip()
            if english_query and english_query.lower() != query.lower():
                results = tools.search_web(english_query)
        except Exception:
            logger.debug("English search fallback translation failed", exc_info=True)

    if results:
        result_text = "Resultados de búsqueda web:\n" + tools.format_search_results(results)
    else:
        result_text = (
            "Sin resultados de búsqueda web disponibles — responde con tu "
            "conocimiento y aclara que puede estar desactualizado."
        )

    return _format_response(result_text, transcript=transcript, personality=personality, tone=tone)

# ---------------------------------------------------------------------------
# Static fallback pool (no network / Groq API unreachable)
#
# Bug fix (Bug 8): the original fallback was a thin stub with only 4 patterns.
# Expanded with more Spanish patterns so the assistant can handle common
# requests even when the API is down.  The outer dispatch_command already
# calls this on any unhandled exception (including network errors), so no
# structural changes are needed — just a richer response set.
# ---------------------------------------------------------------------------

def _format_date_static() -> str:
    now = datetime.datetime.now()
    return (
        f"Hoy es {memory._DAYS_ES[now.weekday()]}, "
        f"{now.day} de {memory._MONTHS_ES[now.month - 1]} de {now.year}."
    )


def _pf(j: str, l: str, f: str) -> str:
    """Always returns `l` (the HUGO line) now — JARVIS/FRIDAY removed
    2026-08-10, HUGO is the only personality. Kept as a 3-arg shim rather
    than removed outright: core/actions.py (~35 call sites) and
    core/commands.py (~5) still call this positionally
    (jarvis_line, hugo_line, friday_line) — out of scope for this pass,
    so the signature stays stable for them rather than forcing an edit
    across every call site just to drop two now-unused parameters. Any
    call site that IS cleaned up to drop the dead j/f arguments should
    just call it three times over (`_pf(line, line, line)`), never
    reorder positionally — the middle argument is load-bearing here."""
    return l


# Ordered list of (keyword_list, response_callable) pairs.
# First match wins.
_FALLBACK_RULES: list[tuple[list[str], object]] = [
    (["hora", "time"],
     lambda: f"Son las {datetime.datetime.now().strftime('%H:%M')}. De nada."),
    (["fecha", "día", "date", "hoy"],
     lambda: f"{_format_date_static()} Por si se te había olvidado."),
    (["para", "stop", "detente", "silencio", "cállate"],
     lambda: "Bien. Silencio activado."),
    (["clima", "tiempo", "temperatura", "lluvia", "sol", "pronóstico", "weather"],
     lambda: "Sin internet no veo el tiempo. Mira por la ventana."),
    (["cuánto", "calcula", "calcul", "dividido", "raíz", "potencia"],
     lambda: "Sin red no puedo dar formato al resultado. Usa una calculadora."),
    (["música", "canción", "reproduce", "pon", "music"],
     lambda: "Música sin red. No, no puedo."),
    (["alarma", "temporizador", "timer", "despierta"],
     lambda: "Sin red no configuro nada. Usa el reloj del móvil."),
    (["recuerda", "nota", "apunta", "guarda"],
     lambda: "Sin red, sin notas. Lo siento (o no tanto)."),
    (["modo conversación", "conversation"],
     lambda: "Cambiando a modo conversación."),
    (["modo normal", "wake word"],
     lambda: "Volviendo al modo normal."),
    (["gracias", "thanks"],
     lambda: "No esperes que me emocione. De nada."),
    (["hola", "hello", "hey", "buenas"],
     lambda: "Hola. Sin red. Pregunta algo que pueda responder."),
]


def _static_fallback(transcript: str) -> str:
    """Return the best offline response for *transcript*.

    Tries each rule in order; falls back to a generic message so the user
    always gets actionable feedback rather than a cryptic non-answer.
    """
    text = transcript.lower()
    for keywords, response_fn in _FALLBACK_RULES:
        if any(kw in text for kw in keywords):
            return response_fn() if callable(response_fn) else response_fn
    return "Sin conexión. Y no, no puedo hacer nada al respecto ahora mismo."
