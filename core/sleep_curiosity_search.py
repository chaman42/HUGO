"""Sleep System — Phase 8 (🌱 Curiosidad), expanded: instead of just naming
topics HUGO might find interesting, this phase actively searches the web for
them during idle sleep cycles and saves what it finds to ESTUDIO's own
EXPLORACIONES tab (see core/estudio_routes.py, data/explorations.json —
deliberately NOT data/summaries.json, which is reserved for summaries Joan
explicitly asked HUGO to generate; see core/commands.py's generate_summary()),
so Joan has something to actually read rather than a one-line note.

Pipeline, once per phase call (i.e. once per sleep session / once per
continuous-sleep cycle):
  1. Pull recent facts/episodes (last 7 days) and ask Ollama to name up to
     3 active topics with 1-2 targeted search queries each.
  2. Run those queries through the existing Serper-backed search
     (core.tools_search.search_web — same call site core.sleep_phases_
     incubation.py already uses for its own web-search refinement), capped
     at MAX_SEARCHES_PER_CYCLE and gated by a soft daily Serper budget
     (data/curiosidad_search_state.json) so this never burns through the
     account's search credits.
  3. Keep only results that pass a cheap local relevance check (word
     overlap against the topic — no LLM/API cost per result) and save each
     as a data/explorations.json entry (type 'curiosidad'), then emit
     'estudio_updated' so any open HUD tab refreshes live.
  4. Also queue the finding for a one-time natural mention next
     conversation — see core.sleep_insights_store.get_unused_curiosidad_finding()
     and core/personalities/base.py's injection of it.

Curiosidad profunda (_phase_curiosity_deep) is a separate, deeper mode only
entered from continuous sleep (core.sleep.run_continuous_sleep) after a
cycle finishes all 7 standard phases with cycles still to spare and Ollama
idle — see that function's own docstring. Zero Serper cost (Ollama only):
it re-reads what curiosidad-the-phase already found (and any half-formed
ideas from Phase 3) and goes deeper on them — connections, elaboration —
saved to ESTUDIO as type 'exploración profunda'.

Every LLM call here goes straight to Ollama (core.sleep_llm._ollama_generate)
rather than through _groq_call — per spec this phase must never spend Groq
tokens; if Ollama isn't reachable, it simply doesn't run this cycle."""
import datetime
import json
import re

from core.sleep_state import (
    MEMORY_SHARED_PATH, EPISODES_PATH, _load_json, _save_json, _log, _p,
    _now_iso, _is_fact_expired,
)
from core.sleep_llm import _ollama_available, _ollama_generate
from core import memory_flags
from core import tools_search
import core.sleep_insights_store as sleep_insights_store

SEARCH_STATE_PATH  = _p("data/curiosidad_search_state.json")
EXPLORATIONS_PATH  = _p("data/explorations.json")

MAX_SEARCHES_PER_CYCLE   = 5     # spec: "Maximum 5 searches per sleep cycle"
SERPER_DAILY_SOFT_LIMIT  = 80    # conservative — leaves headroom for on-demand user searches on the same key
RELEVANCE_THRESHOLD      = 0.34  # content-word overlap floor (fraction of topic words found in the result) — see _relevance_score
MAX_TOPICS_PER_CYCLE     = 3
MAX_EXPLORATIONS_KEPT    = 300   # explorations.json grows unbounded otherwise — same discipline as sleep_insights_store's own caps
MAX_DEEP_ITEMS_PER_CYCLE = 6     # curiosidad profunda safety valve — see _phase_curiosity_deep's own docstring


# ---------------------------------------------------------------------------
# Serper daily soft-budget — own tiny state file, kept separate from
# data/sleep_budget.json (that file's schema is Groq-token budgets; this is
# an unrelated per-API-key call count, and giving it its own file avoids
# touching load_budget()'s existing structure/consumers).
# ---------------------------------------------------------------------------

def _today() -> str:
    return datetime.date.today().isoformat()


def _load_search_state() -> dict:
    state = _load_json(SEARCH_STATE_PATH, None)
    if not isinstance(state, dict) or state.get("date") != _today():
        state = {"date": _today(), "calls_used": 0}
    return state


def _save_search_state(state: dict) -> None:
    _save_json(SEARCH_STATE_PATH, state)


def _serper_calls_remaining_today() -> int:
    state = _load_search_state()
    return max(SERPER_DAILY_SOFT_LIMIT - state.get("calls_used", 0), 0)


def _record_serper_call() -> None:
    state = _load_search_state()
    state["calls_used"] = state.get("calls_used", 0) + 1
    _save_search_state(state)


# ---------------------------------------------------------------------------
# Step 1 — active topics + search queries, Ollama only
# ---------------------------------------------------------------------------

def _recent_material(days: int = 7, max_facts: int = 15, max_episodes: int = 10) -> str:
    """Facts/episodes from the last *days* days only — deliberately NOT
    core.sleep_summary._build_state_summary() (which takes the last N
    items regardless of age): this phase is specifically about what's
    ACTIVE right now, so a fact from a month ago shouldn't shape this
    cycle's search queries just because it's recent in file order."""
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)

    facts = _load_json(MEMORY_SHARED_PATH, [])
    fact_lines = []
    for f in facts:
        if not isinstance(f, dict) or f.get("outdated") or _is_fact_expired(f):
            continue
        created_at = f.get("created_at") or f.get("added")
        try:
            if not created_at or datetime.datetime.fromisoformat(created_at) < cutoff:
                continue
        except ValueError:
            continue
        fact_lines.append(f"- ({f.get('category', '?')}) {str(f.get('fact', ''))[:100]}")
    fact_lines = fact_lines[-max_facts:]

    episodes = _load_json(EPISODES_PATH, [])
    episode_lines = []
    for e in episodes:
        if not isinstance(e, dict):
            continue
        try:
            if not e.get("date") or datetime.date.fromisoformat(e["date"]) < cutoff.date():
                continue
        except ValueError:
            continue
        episode_lines.append(f"- {e.get('topic', '')}: {str(e.get('summary', ''))[:90]}")
    episode_lines = episode_lines[-max_episodes:]

    return (
        "FACTS RECIENTES (últimos 7 días):\n" + ("\n".join(fact_lines) or "(ninguno)") + "\n\n"
        "EPISODIOS RECIENTES (últimos 7 días):\n" + ("\n".join(episode_lines) or "(ninguno)")
    )


_TOPIC_SYSTEM = (
    "Eres HUGO identificando qué temas activos de Joan merecen que "
    "investigues por tu cuenta mientras 'duermes'. Respondes solo con JSON "
    "válido, sin comentarios."
)


def _extract_topics_and_queries() -> list[dict]:
    """Returns up to MAX_TOPICS_PER_CYCLE {"topic": str, "queries": [str,...]}
    dicts (fewer when current 'curiosidad' is low — see
    core.internal_state.curiosity_topic_budget), Ollama-only (never Groq —
    see module docstring). [] if Ollama is unreachable, there's no recent
    material, or parsing fails."""
    if not _ollama_available():
        return []

    material = _recent_material()
    if "(ninguno)" in material and material.count("(ninguno)") == 2:
        return []

    # Entity Pillars Phase 2 — how many topics she actually chases this
    # cycle scales with her current 'curiosidad' (core/internal_state.py)
    # instead of always running at the fixed ceiling.
    from core.internal_state import curiosity_topic_budget
    topic_budget = curiosity_topic_budget(MAX_TOPICS_PER_CYCLE)

    user = (
        f"{material}\n\n"
        f"A partir de esto, identifica hasta {topic_budget} temas activos "
        "(proyectos en curso, preguntas recientes, cosas que Joan mencionó querer "
        "aprender) que valga la pena investigar en la web ahora mismo. Para cada "
        "tema, da 1-2 búsquedas concretas en Google que encontrarían contenido "
        "útil o interesante. Formato JSON: "
        '[{"topic": "...", "queries": ["...", "..."]}]. Sé específico — nunca '
        "búsquedas genéricas de una sola palabra."
    )
    text = _ollama_generate(_TOPIC_SYSTEM, user, max_tokens=400)
    if not text:
        return []

    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return []
    try:
        parsed = json.loads(match.group())
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []

    out = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        topic = str(item.get("topic", "")).strip()
        queries = [str(q).strip() for q in (item.get("queries") or []) if str(q).strip()]
        if topic and queries:
            out.append({"topic": topic, "queries": queries[:2]})
    return out[:topic_budget]


# ---------------------------------------------------------------------------
# Step 2/3 — search, relevance filter, save to ESTUDIO
# ---------------------------------------------------------------------------

_ES_STOPWORDS = frozenset({
    "el", "la", "los", "las", "de", "del", "que", "y", "en", "un", "una",
    "unos", "unas", "es", "por", "para", "con", "no", "su", "sus", "al",
    "se", "más", "pero", "como", "sobre", "entre", "esto", "esta", "este",
})


def _content_words(text: str) -> set[str]:
    """Lowercased word tokens with stopwords dropped and a naive plural
    strip (trailing 's' on words > 4 chars) — plain jaccard overlap
    (core.sleep_state._fact_similarity) is too strict for Spanish, where
    'armaduras' vs 'armadura' or 'la'/'una' noise otherwise swamps the
    signal from a 2-3 word topic."""
    words = re.findall(r"[a-záéíóúñü]+", text.lower())
    out = set()
    for w in words:
        if w in _ES_STOPWORDS or len(w) < 3:
            continue
        out.add(w[:-1] if len(w) > 4 and w.endswith("s") else w)
    return out


def _relevance_score(topic: str, result: dict) -> float:
    """Cheap local heuristic (content-word overlap — no extra LLM/API call
    per result): what fraction of the topic's own content words appear in
    the result's title+snippet, plus a small bonus for a trusted source.
    Deliberately asymmetric (overlap / topic words, not full jaccard): a
    short topic phrase should count as relevant even against a long
    snippet that also covers other things. 0.0-~1.1."""
    topic_words  = _content_words(topic)
    result_words = _content_words(f"{result.get('title', '')} {result.get('snippet', '')}")
    if not topic_words or not result_words:
        return 0.0
    score = len(topic_words & result_words) / len(topic_words)
    if tools_search._is_trusted(result.get("url", "")):
        score += 0.1
    return score


def _append_exploration_record(record: dict) -> None:
    records = _load_json(EXPLORATIONS_PATH, [])
    if not isinstance(records, list):
        records = []
    records.append(record)
    records = records[-MAX_EXPLORATIONS_KEPT:]
    _save_json(EXPLORATIONS_PATH, records)


def _emit_estudio_updated(section: str) -> None:
    """Best-effort — mirrors core.commands._emit_estudio_updated exactly,
    duplicated rather than imported (this module must stay importable from
    scripts/reflective_mode.py's standalone launchd path, which deliberately
    never imports core.commands — see that script's own module docstring).
    Silently does nothing when there's no live server to emit to (e.g. this
    phase ran from the standalone launchd job while jarvis.py is closed)."""
    try:
        import core.server as server_mod
        server_mod.socketio.emit("estudio_updated", {"section": section})
    except Exception:
        pass


_MAX_HITS_FOR_SYNTHESIS = 5   # caps how many surviving hits get fed to the synthesis prompt per topic

_SYNTHESIS_SYSTEM = (
    "Eres HUGO revisando lo que encontraste buscando sobre un tema activo de "
    "Joan. Tu trabajo es decidir si hay algo genuinamente interesante y "
    "concreto que valga la pena contarle, o si es relleno que no aporta "
    "nada nuevo. Sé exigente — la mayoría de tandas de resultados NO tienen "
    "nada que merezca guardarse. Respondes solo con JSON válido, sin "
    "comentarios."
)


def _synthesize_topic_finding(topic: str, hits: list[dict]) -> dict | None:
    """One Ollama pass over ALL of a topic's surviving search hits together
    (instead of the old per-hit save — see this phase's own module
    docstring on why: 5 searches used to become 5+ raw-snippet cards with
    zero synthesis or judgment beyond a keyword-overlap %). Returns a dict
    with 'title'/'summary'/'best_url' if the model judges there's one
    genuinely interesting, concrete finding worth showing Joan, else None.
    Still Ollama-only — zero Groq spend, same as every other call in this
    module."""
    if not hits:
        return None
    hits = sorted(hits, key=lambda h: h["score"], reverse=True)[:_MAX_HITS_FOR_SYNTHESIS]
    sources_block = "\n".join(
        f"- {h['result'].get('title', '')}: {h['result'].get('snippet', '')} ({h['result'].get('url', '')})"
        for h in hits
    )
    user = (
        f"TEMA: {topic}\n\nRESULTADOS ENCONTRADOS:\n{sources_block}\n\n"
        "¿Hay aquí un hallazgo concreto, novedoso y genuinamente interesante "
        "sobre este tema? Si NO (son resultados genéricos, repetitivos, "
        "publicitarios, o no dicen nada que Joan no supiera ya), responde "
        '{"interesting": false}. Si SÍ, sintetiza en 2-4 frases lo que '
        "encontraste combinando lo relevante de varias fuentes (no copies "
        "una sola fuente literalmente) y responde JSON exacto: "
        '{"interesting": true, "title": "título corto y concreto", '
        '"summary": "síntesis de 2-4 frases", "best_url": "la URL de la '
        'fuente más útil de la lista de arriba"}.'
    )
    text = _ollama_generate(_SYNTHESIS_SYSTEM, user, max_tokens=300)
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group())
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or not parsed.get("interesting"):
        return None
    summary = str(parsed.get("summary", "")).strip()
    if not summary:
        return None
    best_url = str(parsed.get("best_url", "")).strip()
    valid_urls = {h["result"].get("url", "") for h in hits}
    if best_url not in valid_urls:
        best_url = hits[0]["result"].get("url", "")   # model invented/mangled the URL — fall back to the top-scored hit's real one
    return {
        "title":   str(parsed.get("title", "")).strip() or topic,
        "summary": summary[:400],
        "url":     best_url,
        "sources": [h["result"].get("url", "") for h in hits],
        "score":   hits[0]["score"],
    }


def _admin_device_active() -> bool:
    """True when Joan (the admin/testing device, not Dani — HUGO's real
    intended user) is the currently identified speaker. Exploration is
    built from recent conversation material (see _extract_topics_and_queries),
    so admin/testing chats from Joan's own device would otherwise pollute
    Dani's explorations with topics from Joan's dev/QA sessions rather than
    Dani's actual interests. Best-effort — a lookup failure defaults to
    'not Joan' so a bug here never silently disables exploration for Dani's
    real usage."""
    try:
        from core import social as social_mod
        present = social_mod.social_engine.who_is_present()
        current = present[0] if present else None
        return current is not None and current.id == "joan"
    except Exception:
        return False


def _phase_curiosity_search(remaining_budget: int) -> tuple[int, int, str]:
    """Phase 8 entry point — same (tokens, insights, summary) contract as
    every other phase (see core.sleep._PHASE_FUNCS), even though tokens is
    always 0 here (Ollama-only, no Groq spend ever)."""
    if _admin_device_active():
        return 0, 0, "dispositivo admin — exploración omitida"
    if not memory_flags.is_feature_enabled("busqueda_web"):
        return 0, 0, "búsqueda web desactivada"
    if not _ollama_available():
        return 0, 0, "Ollama no disponible — se omite"

    serper_remaining = _serper_calls_remaining_today()
    if serper_remaining <= 0:
        return 0, 0, "límite diario de Serper alcanzado — se omite"

    topics = _extract_topics_and_queries()
    if not topics:
        return 0, 0, "sin temas activos"

    cycle = sleep_insights_store._current_cycle
    searches_done = 0
    findings = 0

    for topic_item in topics:
        if searches_done >= MAX_SEARCHES_PER_CYCLE or searches_done >= serper_remaining:
            break
        # Collect every surviving hit for THIS topic across all its queries
        # first — synthesis needs the full picture at once, not one call
        # per hit (see _synthesize_topic_finding's own docstring).
        topic_hits: list[dict] = []
        for query in topic_item["queries"]:
            if searches_done >= MAX_SEARCHES_PER_CYCLE or searches_done >= serper_remaining:
                break
            try:
                hits = tools_search.search_web(query)
            except Exception:
                hits = []
            searches_done += 1
            if hits and hits[0].get("source") == "serper":
                _record_serper_call()   # only a REAL Serper hit costs credits — DDG fallback doesn't

            for r in hits:
                score = _relevance_score(topic_item["topic"], r)
                if score < RELEVANCE_THRESHOLD:
                    continue
                topic_hits.append({"result": r, "score": score})

        finding = _synthesize_topic_finding(topic_item["topic"], topic_hits)
        if finding is None:
            continue

        record = {
            "title":                  finding["title"],
            "url":                    finding["url"],
            "date":                   _now_iso(),
            "type":                   "curiosidad",
            "excerpt":                finding["summary"],   # keeps ui/js/estudio.js's existing card renderer working unchanged
            "summary":                finding["summary"],
            "topic":                  topic_item["topic"],
            "relevance":              round(finding["score"], 2),
            "sources":                finding["sources"],
            "found_during_sleep_cycle": cycle,
            "mentioned":              False,
        }
        _append_exploration_record(record)
        sleep_insights_store._add_insight(
            "curiosidad_findings",
            f"{record['title']} — {record['topic']} ({record['url']})",
            finding["score"],
        )
        findings += 1

    if findings:
        _emit_estudio_updated("exploraciones")
        _log(f"CURIOSIDAD — {findings} hallazgos guardados ({searches_done} búsquedas, {len(topics)} temas)")

    return 0, findings, f"{findings} hallazgos guardados en Estudio ({searches_done} búsquedas)"


# ---------------------------------------------------------------------------
# Curiosidad profunda — Ollama-only deep exploration, continuous-sleep only.
# See core.sleep.run_continuous_sleep for when this gets called.
# ---------------------------------------------------------------------------

_DEEP_SYSTEM = (
    "Eres HUGO en 'modo curiosidad profunda' — no estás ejecutando una "
    "tarea, estás explorando por gusto propio algo que te llamó la "
    "atención. Profundiza de verdad: conexiones, matices, lo que te parece "
    "más interesante. Responde en dos partes: primero una línea "
    "'TÍTULO: <título breve y concreto, menos de 8 palabras>', luego una "
    "línea en blanco, luego 3-5 frases de desarrollo, tono genuinamente "
    "curioso, nunca un resumen genérico."
)


def _parse_deep_dive_output(raw: str, fallback_title: str) -> tuple[str, str]:
    """Splits _DEEP_SYSTEM's 'TÍTULO: ...' line from the rest of the
    exploration text — same convention as core.commands._parse_summary_output,
    reimplemented locally rather than imported (this module stays
    dependency-light/Ollama-only per its own header docstring, no pull-in
    of core.commands). Falls back to fallback_title (the old hardcoded
    template) if the model didn't follow the format, so a malformed
    response never loses the exploration text itself."""
    match = re.search(r"^\s*T[IÍ]TULO:\s*(.+)$", raw, re.MULTILINE | re.IGNORECASE)
    if not match:
        return fallback_title, raw.strip()
    title = match.group(1).strip().strip('"').strip("«»")[:150] or fallback_title
    body = raw[match.end():].strip()
    return title, body or raw.strip()


def _next_deep_target() -> dict | None:
    """Oldest not-yet-deep-explored curiosidad finding from ESTUDIO's
    EXPLORACIONES tab, or (falling back) an unused idea from Phase 3 — see
    spec's own "develops half-formed ideas from previous cycles" bullet.
    None if there's nothing left to go deeper on."""
    records = _load_json(EXPLORATIONS_PATH, [])
    if isinstance(records, list):
        for i, r in enumerate(records):
            if isinstance(r, dict) and r.get("type") == "curiosidad" and not r.get("deep_explored"):
                return {"kind": "finding", "index": i, "record": r}

    data = sleep_insights_store.load_insights()
    for i, idea in enumerate(data.get("ideas", [])):
        if isinstance(idea, dict) and idea.get("text") and not idea.get("deep_explored"):
            return {"kind": "idea", "index": i, "record": idea}

    return None


def _mark_deep_explored(target: dict) -> None:
    if target["kind"] == "finding":
        records = _load_json(EXPLORATIONS_PATH, [])
        if isinstance(records, list) and 0 <= target["index"] < len(records):
            records[target["index"]]["deep_explored"] = True
            _save_json(EXPLORATIONS_PATH, records)
    else:
        data = sleep_insights_store.load_insights()
        if 0 <= target["index"] < len(data.get("ideas", [])):
            data["ideas"][target["index"]]["deep_explored"] = True
            sleep_insights_store.save_insights(data)


def _phase_curiosity_deep(stop_check) -> dict:
    """Runs until there's nothing left to explore, MAX_DEEP_ITEMS_PER_CYCLE
    is reached (a pragmatic safety valve — real ceiling per spec is "no time
    limit", but the process still needs to check stop_check() regularly to
    stay interruptible, so this bounds how long it goes between checks
    rather than imposing a token/time budget), or Ollama stops responding.
    Zero Serper cost — pure Ollama. Returns {"explored": int}."""
    if _admin_device_active():
        return {"explored": 0}
    explored = 0
    cycle = sleep_insights_store._current_cycle

    while explored < MAX_DEEP_ITEMS_PER_CYCLE and not stop_check():
        if not _ollama_available():
            break
        target = _next_deep_target()
        if target is None:
            break

        rec = target["record"]
        if target["kind"] == "finding":
            prompt = (
                f"Encontraste esto mientras investigabas: '{rec.get('title', '')}' "
                f"sobre el tema '{rec.get('topic', '')}'. Resumen: {rec.get('summary', '')}\n\n"
                "Profundiza: ¿qué conexiones tiene con lo que ya sabes de Joan y sus "
                "proyectos? ¿Qué te parece más interesante de esto?"
            )
            fallback_title = f"Explorando más: {rec.get('title', '')}"
        else:
            prompt = (
                f"Tuviste esta idea a medio formar: '{rec.get('text', '')}'.\n\n"
                "Desarróllala más: ¿cómo se podría concretar? ¿qué la hace valiosa?"
            )
            fallback_title = f"Desarrollando una idea: {rec.get('text', '')[:60]}"

        text = _ollama_generate(_DEEP_SYSTEM, prompt, max_tokens=350)
        _mark_deep_explored(target)
        explored += 1
        if not text:
            continue
        title, text = _parse_deep_dive_output(text, fallback_title)

        _append_exploration_record({
            "title":                  title,
            "url":                    rec.get("url", ""),
            "date":                   _now_iso(),
            "type":                   "exploración profunda",
            "excerpt":                text[:400],
            "summary":                text,
            "topic":                  rec.get("topic") or rec.get("text", "")[:60],
            "relevance":              rec.get("relevance"),
            "found_during_sleep_cycle": cycle,
        })

    if explored:
        _emit_estudio_updated("exploraciones")
        _log(f"CURIOSIDAD PROFUNDA — {explored} exploraciones guardadas")

    return {"explored": explored}
