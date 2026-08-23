"""Sleep System — Phases 2-7: pattern discovery, idea generation,
diagnostics, pending questions, self-critique, and curiosity. Each phase is
one Groq/Ollama call plus a small parse/store step."""
from core.sleep_state import MEMORY_INSTRUCTIONS_PATH, EPISODES_PATH, _load_json, _save_json, _log, MAX_SELF_CRITIQUE_NOTES
from core.sleep_llm import _groq_call, _parse_json_list
from core.sleep_summary import _build_state_summary, _count_recent_errors, _memory_health
from core.sleep_insights_store import _add_insight

_PATTERN_SYSTEM = (
    "Eres HUGO analizando patrones de comportamiento de Joan a partir de lo "
    "que ya sabes de él. Respondes solo con JSON válido, sin comentarios."
)

def _phase_pattern_discovery(remaining_budget: int) -> tuple[int, int, str]:
    state = _build_state_summary()
    user = (
        f"{state}\n\nAnaliza estos datos y detecta hasta 3 patrones de "
        "comportamiento, intereses o estilo de trabajo de Joan que aún no "
        'estén explícitos. Formato JSON: [{"pattern": "...", "confidence": '
        "0-1}]. Solo incluye patrones con confidence > 0.6. Sé conciso."
    )
    text, tokens = _groq_call(_PATTERN_SYSTEM, user, remaining_budget)
    if not text:
        return tokens, 0, "sin respuesta"
    items = _parse_json_list(text, "pattern", min_confidence=0.6, cap=3)
    for item in items:
        _add_insight("patterns", item["pattern"], item["confidence"])
    return tokens, len(items), f"{len(items)} patrones detectados"

_IDEA_SYSTEM = (
    "Eres HUGO proponiendo mejoras o ideas nuevas basadas en lo que sabes "
    "de Joan y su proyecto. Respondes solo con JSON válido, sin comentarios."
)

def _phase_idea_generator(remaining_budget: int) -> tuple[int, int, str]:
    state = _build_state_summary()
    user = (
        f"{state}\n\nBasándote en este conocimiento acumulado, propón hasta "
        "3 ideas nuevas: funciones útiles, mejoras, o conceptos que podrían "
        'interesarle a Joan. Formato JSON: [{"idea": "...", "confidence": '
        "0-1}]. Solo incluye ideas con confidence > 0.6. Sé conciso."
    )
    text, tokens = _groq_call(_IDEA_SYSTEM, user, remaining_budget)
    if not text:
        return tokens, 0, "sin respuesta"
    items = _parse_json_list(text, "idea", min_confidence=0.6, cap=3)
    for item in items:
        _add_insight("ideas", item["idea"], item["confidence"])
    return tokens, len(items), f"{len(items)} ideas generadas"

_DIAG_SYSTEM = (
    "Eres HUGO generando un breve informe de diagnóstico interno. "
    "Responde en 2-3 frases, directo, sin rodeos ni relleno."
)

def _phase_diagnostics(remaining_budget: int) -> tuple[int, int, str]:
    error_count = _count_recent_errors()
    total_facts, outdated_facts = _memory_health()
    stats = f"Errores recientes: {error_count}. Facts totales: {total_facts} ({outdated_facts} desactualizados)."

    report = stats
    text, tokens = _groq_call(_DIAG_SYSTEM, f"Genera un breve diagnóstico a partir de estos datos:\n{stats}", remaining_budget)
    if text:
        report = text

    _log(f"DIAGNOSTIC — {report}")
    return tokens, 1, report

_QUESTIONS_SYSTEM = (
    "Eres HUGO identificando qué información falta o es ambigua sobre Joan "
    "y sus proyectos. Respondes solo con JSON válido, sin comentarios."
)

def _phase_pending_questions(remaining_budget: int) -> tuple[int, int, str]:
    state = _build_state_summary()
    user = (
        f"{state}\n\nIdentifica hasta 2 preguntas concretas que le harías a "
        "Joan para completar información importante que falta o es "
        'ambigua sobre él o sus proyectos. Formato JSON: [{"question": '
        '"...", "confidence": 0-1}]. Solo incluye con confidence > 0.6.'
    )
    text, tokens = _groq_call(_QUESTIONS_SYSTEM, user, remaining_budget)
    if not text:
        return tokens, 0, "sin respuesta"
    items = _parse_json_list(text, "question", min_confidence=0.6, cap=2)
    for item in items:
        _add_insight("questions", item["question"], item["confidence"])
    return tokens, len(items), f"{len(items)} preguntas identificadas"

_CRITIQUE_SYSTEM = (
    "Eres HUGO autoevaluándote de forma constructiva. Buscas notas de "
    "comportamiento CONCRETAS y ACCIONABLES para mejorar futuras "
    "conversaciones — nunca cambios de personalidad, nunca nada vago. "
    "Respondes solo con JSON válido, sin comentarios."
)

def _phase_self_critique(remaining_budget: int) -> tuple[int, int, str]:
    episodes = _load_json(EPISODES_PATH, [])
    recent = [e for e in episodes[-10:] if isinstance(e, dict)] if isinstance(episodes, list) else []
    lines = "\n".join(f"- [{e.get('date', '?')}] {e.get('topic', '')}: {e.get('summary', '')}" for e in recent)
    user = (
        f"EPISODIOS RECIENTES:\n{lines or '(ninguno)'}\n\n"
        "Evalúa si hubo malentendidos o respuestas mejorables en estas "
        "interacciones recientes. Genera hasta 2 notas de comportamiento "
        "concretas para mejorar (ejemplo: confirma la fecha exacta antes "
        "de crear eventos — nunca cambios de personalidad). Formato JSON: "
        '[{"note": "...", "confidence": 0-1}]. Solo con confidence > 0.7.'
    )
    text, tokens = _groq_call(_CRITIQUE_SYSTEM, user, remaining_budget)
    if not text:
        return tokens, 0, "sin respuesta"

    items = _parse_json_list(text, "note", min_confidence=0.7, cap=2)
    added = 0
    if items:
        instructions = _load_json(MEMORY_INSTRUCTIONS_PATH, {})
        if not isinstance(instructions, dict):
            instructions = {}
        hugo_rules = instructions.get("hugo", [])
        if not isinstance(hugo_rules, list):
            hugo_rules = []

        existing_auto  = [r for r in hugo_rules if isinstance(r, str) and r.startswith("[Autocrítica]")]
        hand_written   = [r for r in hugo_rules if not (isinstance(r, str) and r.startswith("[Autocrítica]"))]
        for item in items:
            existing_auto.append(f"[Autocrítica] {item['note']}")
            added += 1
            # Also mirrored into sleep_insights.json's own 'autocritica'
            # category (separate from the memory_instructions.json write
            # above, which is what actually shapes HUGO's behavior) — this
            # copy is purely for surfacing in NÚCLEO HUGO's "REFLEXIONES DEL
            # SUEÑO" panel (see get_sleep_insights_summary()), never read
            # back into a prompt.
            _add_insight("autocritica", item["note"], item["confidence"])
        existing_auto = existing_auto[-MAX_SELF_CRITIQUE_NOTES:]   # cap — never grows unbounded

        instructions["hugo"] = hand_written + existing_auto
        _save_json(MEMORY_INSTRUCTIONS_PATH, instructions)

    return tokens, added, f"{added} notas de autocrítica añadidas"

# Phase 8 (🌱 Curiosidad) used to live here as a plain "suggest some topics"
# call — it's been expanded into an actively-searching phase, now in
# core/sleep_curiosity_search.py (_phase_curiosity_search / see also
# _phase_curiosity_deep for continuous-sleep-only "curiosidad profunda").
