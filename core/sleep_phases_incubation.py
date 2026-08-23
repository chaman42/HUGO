"""Sleep System — Phase 3: 🧪 Incubación. Advances every active investigation
(data/investigations.json, created by core.intent's start_investigation —
see core/investigations.py) by one reasoning cycle per sleep session: reviews
current hypotheses, pulls in relevant memory facts/episodes, optionally runs
1-2 targeted web searches, updates hypotheses/confidence/sub_questions, and
checks the investigation's completion criteria.

Same LLM call site as every other phase (_groq_call — Ollama llama3.2:3b
first, Groq fallback only if Ollama is unreachable), so this phase is free
in practice, same as the rest of the Sleep System."""
import json
import re

from core.sleep_state import _log, _now_iso as _now_iso_local
from core.sleep_llm import _groq_call
from core.sleep_summary import _build_state_summary
from core import memory_flags
from core import tools_search
from core import investigations as investigations_mod
from core import notifications as notifications_mod

_INCUBATION_SYSTEM = (
    "Eres HUGO incubando una investigación en segundo plano, razonando paso "
    "a paso sobre una pregunta que Joan te pidió investigar. Respondes solo "
    "con JSON válido, sin comentarios."
)

_MAX_INVESTIGATIONS_PER_CYCLE = 3   # bounds how many investigations one sleep session advances
_MAX_HYPOTHESES_KEPT          = 5   # cap on accumulated hypotheses per investigation
_MAX_SUB_QUESTIONS            = 3
_MAX_SEARCH_QUERIES           = 2
_CONFIDENCE_READY_THRESHOLD   = 0.85
_MAX_CYCLES                   = 10


def _build_incubation_prompt(inv: dict, context_state: str, search_notes: str) -> str:
    hypotheses = inv.get("hypotheses") or []
    hyp_lines = "\n".join(
        f"- {h.get('text', '')} (confianza {h.get('confidence', 0):.2f})"
        for h in hypotheses if isinstance(h, dict)
    ) or "(ninguna todavía)"
    subq_lines = "\n".join(f"- {q}" for q in (inv.get("sub_questions") or [])) or "(ninguna)"

    prompt = (
        f"INVESTIGACIÓN: {inv.get('question', '')}\n\n"
        f"HIPÓTESIS ACTUALES:\n{hyp_lines}\n\n"
        f"PREGUNTAS PENDIENTES:\n{subq_lines}\n\n"
        f"CONTEXTO DE MEMORIA:\n{context_state}\n\n"
    )
    if search_notes:
        prompt += f"RESULTADOS DE BÚSQUEDA:\n{search_notes}\n\n"
    prompt += (
        "Revisa las hipótesis actuales a la luz de esta información. Genera "
        "hasta 3 hipótesis actualizadas, hasta 3 preguntas nuevas si detectas "
        "vacíos de información (deja la lista vacía si ya no quedan vacíos), "
        "una conclusión breve si ya tienes suficiente confianza (vacía si "
        "no), y un nivel de confianza global 0-1 sobre la investigación "
        "completa. Formato JSON exacto: "
        '{"hypotheses": [{"text": "...", "confidence": 0-1}], '
        '"sub_questions": ["..."], "conclusion": "...", "confidence": 0-1, '
        '"search_queries": ["..."]}. search_queries: hasta 2 búsquedas '
        "concretas que ayudarían a resolver las preguntas pendientes (vacío "
        "si no hace falta buscar nada)."
    )
    return prompt


def _parse_incubation_response(raw: str) -> dict | None:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group())
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None

    hypotheses = []
    for h in parsed.get("hypotheses") or []:
        if not isinstance(h, dict) or not h.get("text"):
            continue
        try:
            confidence = float(h.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        hypotheses.append({"text": str(h["text"]).strip(), "confidence": max(0.0, min(1.0, confidence))})

    sub_questions = [
        str(q).strip() for q in (parsed.get("sub_questions") or []) if str(q).strip()
    ][:_MAX_SUB_QUESTIONS]

    try:
        confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0.0

    search_queries = [
        str(q).strip() for q in (parsed.get("search_queries") or []) if str(q).strip()
    ][:_MAX_SEARCH_QUERIES]

    return {
        "hypotheses":     hypotheses[:3],
        "sub_questions":  sub_questions,
        "conclusion":     str(parsed.get("conclusion", "") or "").strip(),
        "confidence":     confidence,
        "search_queries": search_queries,
    }


def _run_incubation_cycle(inv: dict, budget: int) -> tuple[int, bool, str]:
    """One reasoning cycle for a single investigation — mutates and saves
    `inv` in place. Returns (tokens_used, updated, summary_line)."""
    context_state = _build_state_summary(max_facts=6, max_episodes=4)

    text, tokens = _groq_call(_INCUBATION_SYSTEM, _build_incubation_prompt(inv, context_state, ""), budget)
    total_tokens = tokens
    if not text:
        return total_tokens, False, f"{inv.get('title', '?')}: sin respuesta"

    result = _parse_incubation_response(text)
    if result is None:
        return total_tokens, False, f"{inv.get('title', '?')}: respuesta no parseable"

    # Optional web-search refinement — free in practice (Ollama), only
    # spends real budget if the Groq fallback engages (see _groq_call).
    if result["search_queries"] and memory_flags.is_feature_enabled("busqueda_web"):
        notes = []
        for query in result["search_queries"]:
            try:
                hits = tools_search.search_web(query)
            except Exception:
                hits = []
            if hits:
                notes.append(tools_search.format_search_results(hits))
        if notes:
            remaining = max(budget - total_tokens, 0)
            text2, tokens2 = _groq_call(
                _INCUBATION_SYSTEM,
                _build_incubation_prompt(inv, context_state, "\n".join(notes)),
                remaining,
            )
            total_tokens += tokens2
            if text2:
                refined = _parse_incubation_response(text2)
                if refined is not None:
                    result = refined
            existing_sources = inv.get("sources") or []
            inv["sources"] = list(dict.fromkeys(existing_sources + result["search_queries"]))

    had_sub_questions_before = bool(inv.get("sub_questions"))

    if result["hypotheses"]:
        prev_hypotheses = inv.get("hypotheses") or []
        prev_top = max(prev_hypotheses, key=lambda h: h["confidence"], default=None)
        inv["hypotheses"] = (prev_hypotheses + result["hypotheses"])[-_MAX_HYPOTHESES_KEPT:]
        top = max(result["hypotheses"], key=lambda h: h["confidence"])
        _log(f"Incubación: investigación {inv.get('title', '?')} — nueva hipótesis generada, confianza {top['confidence']:.2f}")

        # Entity Pillars Phase 3 (belief revision, see core/belief_revision.py)
        # — a real change of mind, not just an added hypothesis: the new
        # top hypothesis says something substantially different from the
        # old top one (weak keyword overlap) AND its confidence moved by
        # more than a token amount. Cheap local check, no extra LLM call —
        # same "no-LLM discipline" as core/situation.py's own detectors.
        if prev_top is not None and abs(top["confidence"] - prev_top["confidence"]) > 0.05:
            old_words = set(re.findall(r"\w+", prev_top["text"].lower()))
            new_words = set(re.findall(r"\w+", top["text"].lower()))
            overlap = len(old_words & new_words) / max(1, len(old_words | new_words))
            if overlap < 0.35:
                revisions = inv.setdefault("belief_revisions", [])
                revisions.append({
                    "ts": _now_iso_local(), "old": prev_top["text"], "old_confidence": prev_top["confidence"],
                    "new": top["text"], "new_confidence": top["confidence"],
                })
                inv["belief_revisions"] = revisions[-5:]
                _log(f"Incubación: investigación {inv.get('title', '?')} — cambio de hipótesis detectado")

    inv["sub_questions"]    = result["sub_questions"]
    inv["confidence"]       = result["confidence"]
    inv["cycles_processed"] = inv.get("cycles_processed", 0) + 1
    if result["conclusion"]:
        inv["conclusions"] = result["conclusion"]
    if inv.get("status") == "activa":
        inv["status"] = "incubando"

    no_more_gaps = had_sub_questions_before and not inv["sub_questions"]
    if inv["cycles_processed"] >= _MAX_CYCLES or no_more_gaps:
        inv["status"]  = "completada"
        inv["summary"] = inv.get("conclusions") or "Investigación completada sin conclusión explícita."
        investigations_mod.save_investigation(inv)
        notifications_mod.create_notification(
            "investigation_complete",
            f"Investigación completada: {inv['title']}",
            f"He terminado de investigar {inv['title']}. ¿Quieres ver los resultados?",
        )
    elif inv["confidence"] > _CONFIDENCE_READY_THRESHOLD:
        inv["status"] = "lista_para_revision"
        investigations_mod.save_investigation(inv)
        notifications_mod.create_notification(
            "investigation_ready",
            f"Investigación lista para revisión: {inv['title']}",
            f"Creo que ya tengo una conclusión sólida sobre {inv['title']} — cuando quieras la revisamos.",
        )
    else:
        investigations_mod.save_investigation(inv)

    summary = f"{inv['title']}: confianza {inv['confidence']:.2f}, ciclo {inv['cycles_processed']}"
    return total_tokens, True, summary


def _phase_incubation(remaining_budget: int) -> tuple[int, int, str]:
    active = investigations_mod.get_active_investigations()
    if not active:
        return 0, 0, "sin investigaciones activas"

    active = active[:_MAX_INVESTIGATIONS_PER_CYCLE]
    per_investigation_budget = max(remaining_budget // len(active), 0)

    total_tokens  = 0
    total_updated = 0
    summaries     = []
    for inv in active:
        tokens, updated, summary = _run_incubation_cycle(inv, per_investigation_budget)
        total_tokens += tokens
        if updated:
            total_updated += 1
        summaries.append(summary)

    return total_tokens, total_updated, "; ".join(summaries)
