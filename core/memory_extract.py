# Layer 1 fact extraction — asks Groq to pull new, lasting facts about the
# user out of a conversation turn. Split out of core/memory.py (pure
# refactor, no behavior change).
import datetime
import json
import logging
import re
import threading

from core.memory_flags import is_feature_enabled
from core.memory_store import (
    MEMORY_SHARED_PATH,
    _CONTENT_TYPES,
    _LIFESPAN_VALUES,
    _MEMORY_BLACKLIST,
    _SHARED_CATEGORIES,
    _TEMPORAL_FACT_PATTERNS,
    _load_fact_file,
    _mark_fact_outdated,
    _memory_lock,
    _upsert_fact,
)

logger = logging.getLogger(__name__)


def _extract_and_save_memory(user_msg: str, assistant_msg: str, personality: str) -> None:
    """
    Fire-and-forget: ask Groq to extract genuinely new, lasting facts about
    the USER (never the assistant) from this exchange, and upsert them into
    LAYER 1 (memory_shared.json) ONLY.

    `personality` is kept for logging context only — Layer 2
    (memory_<personality>.json), Layer 3 (memory_instructions.json) and
    Layer 4 (live time/location/weather/session data) are NEVER written here.
    Layer 2 is manually curated; Layer 3 is manually curated; Layer 4 is
    always fetched fresh and must never be persisted as a fact.
    """
    if is_feature_enabled("modo_test"):
        logger.info("[TEST MODE] memory extraction skipped")
        return

    def _run():
        # Lazy import — core.commands imports this module at top level, so a
        # top-level import here would be circular.
        import core.commands as commands
        try:
            with _memory_lock:
                existing = _load_fact_file(MEMORY_SHARED_PATH, default_category="personal")

            existing_block = "\n".join(f"- ({f['category']}) {f['fact']}" for f in existing) or "(ninguno)"
            blacklist_str  = ", ".join(sorted(_MEMORY_BLACKLIST))
            today_iso      = datetime.date.today().isoformat()
            content_types_str = ", ".join(sorted(_CONTENT_TYPES))

            prompt = (
                f"Lo que ya sabes de Joan:\n{existing_block}\n\n"
                f"Joan dijo: {user_msg}\n"
                f"Le respondiste: {assistant_msg}\n\n"
                "Quédate SOLO con lo NUEVO, concreto y duradero sobre JOAN — jamás sobre "
                "ti misma. Clasifica cada hecho en UNA categoría: "
                "'personal' (identidad, ubicación, rasgos estables), 'preference' (gustos, "
                "preferencias), 'project' (proyectos o planes en curso), 'skill' (habilidades "
                "o metas de aprendizaje/mejora), 'relationship' (personas o relaciones mencionadas).\n\n"
                "Además, asígnale a cada hecho un 'lifespan' (cuánto tiempo sigue siendo "
                "válido) — SOLO uno de estos cuatro valores:\n"
                "- 'permanent': identidad, habilidades, proyectos (el proyecto en sí, no su "
                "estado puntual), preferencias, relaciones, logros. No caduca nunca.\n"
                "- 'weekly': situaciones en curso, el ESTADO ACTUAL de un proyecto, "
                "decisiones recientes.\n"
                "- 'daily': planes de hoy, ánimo o energía actuales, algo que pasó hoy.\n"
                "- 'hourly': estado del momento ('acaba de desayunar', 'tiene sueño ahora', "
                "'está en Madrid hoy').\n"
                "Ante la duda entre dos, elige el lifespan MÁS CORTO que siga siendo cierto — "
                "mejor que un hecho caduque un poco pronto a que quede para siempre si no lo es.\n\n"
                f"Hoy es {today_iso}. Además del texto plano del hecho, extrae su versión "
                "estructurada:\n"
                f"- 'type': UNA de estas categorías de contenido: {content_types_str}.\n"
                "- 'content': objeto con 'summary' (resumen corto del hecho), 'place' (lugar "
                "mencionado o null), 'people' (lista de personas mencionadas, puede ser vacía), "
                "'context' (contexto breve o null).\n"
                "- 'date_event': la fecha ABSOLUTA (YYYY-MM-DD) en que ocurrió o ocurre el "
                "evento/hecho, calculada a partir de hoy — NUNCA guardes expresiones relativas "
                "como 'hace dos semanas' o 'el mes pasado', conviértelas siempre a fecha "
                "absoluta usando hoy como referencia. Si el hecho no tiene una fecha asociada "
                "(p. ej. una preferencia o habilidad), usa null.\n"
                "- 'importance': número del 1 al 5, qué tan importante es este hecho para "
                "recordar a Joan a futuro (5 = muy importante).\n"
                "- 'tags': lista corta de palabras clave en español (2-4 tags).\n\n"
                "Guárdalo SOLO si:\n"
                "- Revela algo genuinamente personal: preferencias, proyectos, relaciones, "
                "habilidades, metas o identidad.\n"
                "- Joan lo afirma explícitamente sobre sí mismo ('me gusta X', "
                "'trabajo en Y', 'tengo Z').\n"
                "- Le sirve para futuras conversaciones, no solo para este intercambio.\n\n"
                "Nunca guardes:\n"
                "- Temas sobre los que Joan PREGUNTA — preguntar por el iPhone 6 no "
                "significa que le encanten los iPhone.\n"
                "- Preguntas, búsquedas o solicitudes de información de cualquier tipo.\n"
                "- Frases de prueba, consultas repetidas o cualquier cosa que huela a "
                "que te está probando.\n"
                "- Nada que hayas dicho o sepas TÚ — solo hechos sobre Joan.\n"
                "- Datos temporales, eventos del momento, precios, o cualquier cosa que cambie "
                "con el tiempo — eso vive en datos en tiempo real, jamás en memoria.\n\n"
                "Esto no se negocia:\n"
                "- Jarvis, Friday, LIRA, Lyra, Leera, Siri, Alexa son NOMBRES DE ASISTENTES, "
                "no el nombre de Joan. Jamás guardes ninguno de estos como su nombre ni como "
                "hecho personal suyo.\n"
                f"- Ignora por completo estas palabras: {blacklist_str}\n"
                "- Ignora palabras de activación, comandos de voz o nombres de asistentes.\n"
                "- Si no hay nada concreto y duradero sobre Joan, la persona real, devuelve "
                "una lista vacía — mejor no guardar nada que guardar algo falso o de usar y tirar.\n\n"
                "Ejemplos (para que calibres, no los repitas literalmente):\n"
                "- Joan: \"¿qué te parece el Audi A4?\" → NO guardar nada. Es una pregunta "
                "sobre un tema, no una afirmación sobre Joan — no revela que le guste, lo tenga "
                "o lo esté considerando comprar.\n"
                "- Joan: \"me acabo de comprar un Audi A4\" → SÍ guardar (category=personal, "
                "lifespan=permanent): \"Joan tiene un Audi A4\".\n"
                "- Joan: \"¿sabes si va a llover mañana?\" → NO guardar. Pregunta de información "
                "en tiempo real, no un hecho sobre Joan.\n"
                "- Joan: \"llevo tres meses currando en el proyecto de LIRA por las noches, ya "
                "tengo el motor de memoria funcionando\" → SÍ guardar (category=project, "
                "lifespan=weekly): \"El proyecto LIRA de Joan ya tiene el motor de memoria "
                "funcionando\" — esto es justo el tipo de hecho concreto y duradero que MÁS "
                "importa capturar, no lo dejes pasar por venir mezclado con charla informal.\n\n"
                "Memoria que evoluciona, no solo se acumula:\n"
                "- Si un hecho nuevo CONTRADICE o ACTUALIZA algo de la lista de arriba (cambió "
                "de opinión, un proyecto avanzó de fase, algo dejó de ser cierto), incluye en "
                "'replaces' el texto EXACTO del hecho antiguo tal cual aparece arriba — así lo "
                "marco como desactualizado en vez de guardar las dos versiones como si fueran "
                "cosas distintas. Los proyectos en curso se actualizan en el sitio, nunca crean "
                "una versión paralela.\n"
                "- Si no reemplaza nada, deja 'replaces' como null.\n\n"
                'Devuelve SOLO JSON válido: {"facts": [{"fact": "...", "category": "...", '
                '"lifespan": "...", "replaces": "..." | null, "type": "...", '
                '"content": {"summary": "...", "place": "..." | null, "people": [...], '
                '"context": "..." | null}, "date_event": "YYYY-MM-DD" | null, '
                '"importance": 1-5, "tags": ["...", "..."]}, ...]}. Si no hay hechos '
                'nuevos, {"facts": []}.'
            )
            raw = commands._groq_complete_extract(
                [
                    {"role": "system", "content": (
                        "Eres LIRA. Repasas lo que Joan acaba de decir y te quedas solo con "
                        "lo que de verdad vale la pena recordar de él — nada de relleno, nada "
                        "inventado. Respondes solo con JSON válido, sin comentarios ni rodeos."
                    )},
                    {"role": "user",   "content": prompt},
                ],
                max_tokens=500,
            )
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if not match:
                return
            result = json.loads(match.group())
            if not isinstance(result, dict):
                return
            candidates = result.get("facts", [])
            if not isinstance(candidates, list) or not candidates:
                return

            with _memory_lock:
                for item in candidates:
                    if not isinstance(item, dict):
                        continue
                    fact_text = str(item.get("fact", "")).strip()
                    if not fact_text:
                        continue
                    category = item.get("category")
                    if category not in _SHARED_CATEGORIES:
                        category = "personal"
                    lifespan = item.get("lifespan")
                    if lifespan not in _LIFESPAN_VALUES:
                        lifespan = "permanent"

                    fact_words = set(re.findall(r"\w+", fact_text.lower()))
                    if fact_words & _MEMORY_BLACKLIST:
                        logger.debug("Memory fact rejected (blacklisted): %s", fact_text)
                        continue
                    # Layer 4 boundary — strict, no exceptions: a temporal/
                    # ephemeral snapshot must never become a permanent fact.
                    if any(p.search(fact_text) for p in _TEMPORAL_FACT_PATTERNS):
                        logger.warning("Memory fact rejected (temporal, no exceptions): %s", fact_text)
                        continue

                    replaces = item.get("replaces")
                    if isinstance(replaces, str) and replaces.strip():
                        if _mark_fact_outdated(MEMORY_SHARED_PATH, replaces.strip(), reason=fact_text):
                            logger.debug("Memory fact marked outdated in %s: %s", MEMORY_SHARED_PATH, replaces.strip())

                    # Structured-knowledge fields (Memory V2) — validated the
                    # same way category/lifespan are above: an invalid/absent
                    # value falls through to _normalize_fact's own defaults
                    # (see core/memory_store.py) rather than being rejected,
                    # since these enrich the fact but were never required for
                    # it to be worth saving.
                    type_ = item.get("type")
                    if type_ not in _CONTENT_TYPES:
                        type_ = None
                    content = item.get("content")
                    if not isinstance(content, dict):
                        content = None
                    date_event = item.get("date_event")
                    if isinstance(date_event, str):
                        try:
                            datetime.date.fromisoformat(date_event)
                        except ValueError:
                            date_event = None
                    else:
                        date_event = None
                    importance = item.get("importance")
                    try:
                        importance = int(importance)
                    except (TypeError, ValueError):
                        importance = None
                    tags = item.get("tags")
                    if not isinstance(tags, list):
                        tags = None

                    _upsert_fact(
                        MEMORY_SHARED_PATH, fact_text, category, default_category="personal",
                        lifespan=lifespan, type_=type_, content=content, date_event=date_event,
                        importance=importance, tags=tags, raw=fact_text, structured=True,
                    )
                    logger.debug("Memory upserted in %s: (%s/%s, type=%s) %s", MEMORY_SHARED_PATH, category, lifespan, type_, fact_text)

        except Exception:
            # Bug fix (Bug 3): raised to WARNING so memory failures appear in the
            # log feed and are not silently swallowed as DEBUG noise.
            logger.warning("Memory extraction failed (non-critical)", exc_info=True)

    threading.Thread(target=_run, daemon=True, name="memory-extractor").start()
