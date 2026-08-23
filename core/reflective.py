"""HUGO's reflective mode — an idle-time background consolidation pass over
existing memory (facts, episodes) that looks for
connections/patterns/inferences an LLM call can surface but that never came
up explicitly in conversation.

Deliberately dependency-light: json/os/re/datetime/logging + the groq SDK
only — no core.commands, core.voice, core.tools. That's what lets
scripts/reflective_mode.py run this engine standalone via launchd, without
jarvis.py or its audio/TTS stack loaded, "while the app is closed". The
"while the app is open" half — core/commands.py's idle-triggered
_reflective_loop — calls this exact same run_reflective_session(), so the
two entry points can never drift into different behavior.

Rate limiting (max 1 session/hour, max 5000 tokens/day) is enforced through
data/reflective_budget.json on disk rather than in-memory, since it's the
only thing shared between the two entry points — they're separate
processes that never talk to each other directly.
"""
import datetime
import json
import logging
import os
import re

from dotenv import load_dotenv

# core.sleep_llm is dependency-light (json/os/re/urllib + groq SDK +
# core.sleep_state, which is itself stdlib+dotenv only) — safe to import
# here without pulling in core.commands/core.voice/core.tools and breaking
# scripts/reflective_mode.py's standalone launchd runnability (see this
# module's own docstring). Gives both the insight-extraction call below and
# the new connection-extraction call the same Ollama-first, Groq-fallback
# behavior every Sleep System phase already uses (bug fix: this module used
# to call the Groq SDK directly, unconditionally — the one LLM call in the
# whole reflective/sleep surface that skipped local-first entirely).
from core import sleep_llm

logger = logging.getLogger(__name__)

# Absolute, anchored to this file's own location — NOT relative "data/..."
# paths like core/commands.py uses. commands.py can rely on relative paths
# because jarvis.py always runs with the repo root as CWD; the launchd-
# invoked standalone script has no such guarantee (launchd agents don't
# default to a project's CWD), so every path here is resolved from
# __file__ instead of trusting the process's working directory.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _p(rel: str) -> str:
    return os.path.join(_REPO_ROOT, rel)


load_dotenv(_p(".env"))

MEMORY_SHARED_PATH   = _p("data/memory_shared.json")
EPISODES_PATH        = _p("data/episodes.json")
BUDGET_PATH          = _p("data/reflective_budget.json")
CONNECTIONS_PATH     = _p("data/mind_map_connections.json")
LOG_PATH             = _p("logs/reflective.log")

MAX_TOKENS_PER_SESSION       = 500          # completion-token cap per API call
DAILY_TOKEN_BUDGET           = 5000         # total (prompt+completion) tokens/day, resets at midnight
MIN_SECONDS_BETWEEN_SESSIONS = 60 * 60      # max 1 session/hour
CONFIDENCE_THRESHOLD         = 0.8          # only insights strictly above this are kept
MAX_INSIGHTS_PER_SESSION     = 3
_RELATED_FACT_THRESHOLD      = 0.15         # keyword-overlap floor for drawing a mind-map edge

_PROMPT_TEMPLATE = (
    "Analiza estos datos sobre Joan. Encuentra: 1) conexiones entre facts "
    "que aún no están explícitas, 2) patrones entre episodios, 3) "
    "inferencias razonables. Genera máximo 3 nuevos insights como facts "
    "para añadir a la memoria. Formato JSON: "
    '[{{"fact": "...", "category": "...", "confidence": 0-1}}]. Solo añade '
    "facts con confidence > 0.8. Sé conciso.\n\n"
    "DATOS:\n{data}"
)


# ---------------------------------------------------------------------------
# Small JSON helpers — intentionally reimplemented here rather than imported
# from core.commands (see module docstring: this file must stay importable
# without pulling in core.commands' heavy dependency chain).
# ---------------------------------------------------------------------------

def _load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return datetime.date.today().isoformat()


def _log(line: str) -> None:
    """Appends one timestamped line to logs/reflective.log. Best-effort —
    a logging hiccup must never be why a reflective session fails."""
    try:
        os.makedirs(os.path.dirname(LOG_PATH) or ".", exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{_now_iso()} {line}\n")
    except Exception:
        logger.warning("Failed to write logs/reflective.log", exc_info=True)


def _fact_similarity(a: str, b: str) -> float:
    """Jaccard similarity over lowercased word sets — same cheap heuristic
    core/commands.py's own _fact_similarity uses, reimplemented here for
    the same dependency-isolation reason as the JSON helpers above."""
    wa = set(re.findall(r"\w+", a.lower()))
    wb = set(re.findall(r"\w+", b.lower()))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


# ---------------------------------------------------------------------------
# Token budget — shared on-disk state (data/reflective_budget.json)
# ---------------------------------------------------------------------------

def _default_budget() -> dict:
    return {
        "date":                   _today(),
        "tokens_used_today":      0,
        "daily_budget":           DAILY_TOKEN_BUDGET,
        "last_session_at":        None,
        "last_session_tokens":    0,
        "last_session_insights":  0,
    }


def load_budget() -> dict:
    """Loads data/reflective_budget.json, resetting tokens_used_today the
    first time it's read on a new calendar day — this lazy reset (checked
    on every load rather than via a separate midnight cron) is the same
    pattern core/commands.py's own weekly-consolidation date check uses."""
    budget = _load_json(BUDGET_PATH, None)
    if not isinstance(budget, dict):
        budget = _default_budget()
    for key, value in _default_budget().items():
        budget.setdefault(key, value)
    if budget.get("date") != _today():
        budget["date"] = _today()
        budget["tokens_used_today"] = 0
    budget["daily_budget"] = DAILY_TOKEN_BUDGET   # always the current constant, never a stale stored value
    return budget


def save_budget(budget: dict) -> None:
    _save_json(BUDGET_PATH, budget)


def get_status() -> dict:
    """Snapshot for the UI — GET /api/info folds this into its response for
    NÚCLEO HUGO's Estado tab (see core/server.py's api_info)."""
    return load_budget()


def get_connections() -> list:
    """data/mind_map_connections.json's current contents — backs GET
    /api/mind_map_connections, which ui/index.html's Mapa Mental reads."""
    connections = _load_json(CONNECTIONS_PATH, [])
    return connections if isinstance(connections, list) else []


def _seconds_since_last_session(budget: dict) -> float | None:
    last = budget.get("last_session_at")
    if not last:
        return None
    try:
        then = datetime.datetime.fromisoformat(last)
    except ValueError:
        return None
    return (datetime.datetime.now() - then).total_seconds()


def can_run(budget: dict | None = None) -> tuple[bool, str]:
    """Whether a reflective session is allowed right now — checked by both
    entry points before spending a single token."""
    budget = budget or load_budget()
    if budget["tokens_used_today"] >= budget["daily_budget"]:
        return False, "daily token budget exhausted"
    elapsed = _seconds_since_last_session(budget)
    if elapsed is not None and elapsed < MIN_SECONDS_BETWEEN_SESSIONS:
        return False, f"last session {elapsed:.0f}s ago (< {MIN_SECONDS_BETWEEN_SESSIONS}s cap)"
    return True, ""


def _reserve(budget: dict) -> None:
    """Two-phase reserve: stamp last_session_at BEFORE the Groq call and
    save immediately. The live-app trigger and the standalone launchd
    script are separate processes that could in principle both decide to
    run within the same instant; this reservation, written before any
    tokens are spent, means whichever one loses the race sees an
    up-to-date last_session_at via can_run() and backs off instead of both
    spending a session's worth of budget."""
    budget["last_session_at"] = _now_iso()
    save_budget(budget)


# ---------------------------------------------------------------------------
# Context gathering — deliberately terse per-item lines and small caps on
# every list. Keeping "minimize token usage" real given the 5000/day total
# means the CONTEXT sent in, not just the completion, has to stay small.
# ---------------------------------------------------------------------------

def _build_context_block() -> str:
    facts    = _load_json(MEMORY_SHARED_PATH, [])
    episodes = _load_json(EPISODES_PATH, [])

    fact_lines = [
        f"- ({f.get('category', '?')}) {f.get('fact', '')}"
        for f in facts if isinstance(f, dict) and not f.get("outdated")
    ][-15:]

    episode_lines = [
        f"- [{e.get('date', '?')}] {e.get('topic', '')}: {str(e.get('summary', ''))[:80]}"
        for e in episodes if isinstance(e, dict)
    ][-8:]

    return (
        "FACTS:\n" + ("\n".join(fact_lines) or "(ninguno)") + "\n\n"
        "EPISODIOS:\n" + ("\n".join(episode_lines) or "(ninguno)")
    )


def _parse_insights(raw: str) -> list[dict]:
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        return []
    try:
        parsed = json.loads(match.group())
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []

    insights = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        fact = str(item.get("fact", "")).strip()
        try:
            confidence = float(item.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        if fact and confidence > CONFIDENCE_THRESHOLD:
            insights.append({
                "fact": fact,
                "category": item.get("category") or "personal",
                "confidence": confidence,
            })
    return insights[:MAX_INSIGHTS_PER_SESSION]


# ---------------------------------------------------------------------------
# Writing results back — new facts (tagged source: 'reflective') and mind
# map connections.
# ---------------------------------------------------------------------------

def _add_reflective_fact(fact_text: str, category: str) -> bool:
    """Adds *fact_text* to memory_shared.json tagged source: 'reflective',
    skipping it if a near-duplicate is already stored (same similarity
    threshold core/commands.py's own dedup uses) — a reflective insight
    should reinforce existing knowledge, not pad the file with a
    restatement of it. Returns True if a new fact was actually written."""
    facts = _load_json(MEMORY_SHARED_PATH, [])
    if not isinstance(facts, list):
        facts = []
    for f in facts:
        if isinstance(f, dict) and _fact_similarity(fact_text, f.get("fact", "")) > 0.8:
            return False
    facts.append({
        "fact": fact_text,
        "category": category,
        "added": _now_iso(),
        "weight": 1,
        "outdated": False,
        "outdated_at": None,
        "source": "reflective",
    })
    _save_json(MEMORY_SHARED_PATH, facts)
    return True


def _add_connection(from_node: str, to_node: str, relationship: str, strength: float) -> None:
    connections = _load_json(CONNECTIONS_PATH, [])
    if not isinstance(connections, list):
        connections = []
    connections.append({
        "from":         from_node,
        "to":           to_node,
        "relationship": relationship,
        "strength":     max(0.0, min(1.0, strength)),
        "source":       "reflective",
        "added":        _now_iso(),
    })
    _save_json(CONNECTIONS_PATH, connections)


def _link_to_most_related_fact(new_fact_text: str, existing_facts: list[str]) -> None:
    """Fallback path only now (see _link_new_fact) — pure Jaccard word-
    overlap, so it can't catch a genuine connection phrased with different
    words ('practica natación' / 'le gusta nadar' share zero words and
    score 0 here). Kept as the degrade target when the LLM-based
    _extract_connections_llm is unavailable (Ollama down, Groq fallback
    also fails) rather than dropping the mind-map edge entirely for that
    insight. Connects to at most the single best-overlapping existing
    fact (see _RELATED_FACT_THRESHOLD); silently no-ops if nothing
    overlaps enough."""
    best, best_score = None, 0.0
    for existing in existing_facts:
        if existing == new_fact_text:
            continue
        score = _fact_similarity(new_fact_text, existing)
        if score > best_score:
            best_score, best = score, existing
    if best is not None and best_score > _RELATED_FACT_THRESHOLD:
        _add_connection(new_fact_text, best, "insight", best_score)


MAX_CONNECTION_CANDIDATES = 30   # prompt-size cap — most recent N existing facts considered per insight
CONNECTION_MAX_TOKENS     = 150  # small: at most a handful of short JSON relations


def _extract_connections_llm(
    new_fact_text: str, existing_facts: list[str],
) -> tuple[list[tuple[str, str, float]] | None, int]:
    """LLM-judged alternative to _link_to_most_related_fact's pure keyword
    overlap — asks whether any of the most recent MAX_CONNECTION_CANDIDATES
    existing facts are genuinely related to the new one, in meaning, not
    shared words, and what the relationship actually is (not just the
    generic 'insight' label the Jaccard path always uses). Can return
    multiple edges per insight, unlike the single-best-match fallback.

    Returns (connections, tokens_used). connections is None (not []) on any
    failure/empty response, so the caller can tell 'the LLM found zero
    genuine connections' (a valid, empty list) apart from 'the LLM call
    itself didn't work' (None — fall back to _link_to_most_related_fact
    instead of silently adding no edge at all). tokens_used is always 0
    unless sleep_llm._groq_call's real Groq fallback fired (Ollama down) —
    surfaced so the caller can fold it into the session's daily budget
    instead of this call's cost going untracked.
    """
    candidates = [f for f in existing_facts if f and f != new_fact_text][-MAX_CONNECTION_CANDIDATES:]
    if not candidates:
        return [], 0

    numbered = "\n".join(f"{i}. {f}" for i, f in enumerate(candidates))
    prompt = (
        f'Hecho nuevo: "{new_fact_text}"\n\n'
        f"Hechos existentes:\n{numbered}\n\n"
        "¿Cuáles de estos hechos existentes están genuinamente relacionados "
        "con el hecho nuevo — misma persona, tema o actividad — aunque usen "
        "palabras distintas? Responde SOLO con JSON: una lista de hasta 3 "
        'objetos [{"index": N, "relationship": "palabra o frase corta que '
        'describe la relación, ej. \'misma actividad\', \'causa\', '
        '\'contradice\'", "strength": 0.0-1.0}]. Si ninguno está '
        "relacionado, responde []."
    )
    raw, tokens_used = sleep_llm._groq_call(
        "Respondes solo con JSON válido, sin comentarios ni rodeos.",
        prompt, CONNECTION_MAX_TOKENS,
    )
    if not raw:
        return None, tokens_used

    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        return None, tokens_used
    try:
        parsed = json.loads(match.group())
    except json.JSONDecodeError:
        return None, tokens_used
    if not isinstance(parsed, list):
        return None, tokens_used

    results: list[tuple[str, str, float]] = []
    for item in parsed[:3]:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < len(candidates)):
            continue
        relationship = str(item.get("relationship") or "relacionado").strip()
        try:
            strength = float(item.get("strength", 0.5))
        except (TypeError, ValueError):
            strength = 0.5
        results.append((candidates[idx], relationship or "relacionado", strength))
    return results, tokens_used


def _link_new_fact(new_fact_text: str, existing_facts: list[str]) -> int:
    """Populates data/mind_map_connections.json's edges for a freshly-added
    insight — LLM-judged semantic relations first (_extract_connections_llm,
    can find multiple genuinely-related facts with a real relationship
    label, even phrased with different words), falling back to the old
    single-best keyword-overlap match (_link_to_most_related_fact) only if
    the LLM path is unavailable, so a background Ollama outage degrades the
    mind map's quality rather than the whole reflective session failing.
    Returns tokens_used (0 unless the Groq fallback fired) so the caller
    can fold it into the session's daily budget."""
    connections, tokens_used = _extract_connections_llm(new_fact_text, existing_facts)
    if connections is None:
        _link_to_most_related_fact(new_fact_text, existing_facts)
        return tokens_used
    for target, relationship, strength in connections:
        _add_connection(new_fact_text, target, relationship, strength)
    return tokens_used


# ---------------------------------------------------------------------------
# Entry point — called by both core/commands.py's idle trigger and
# scripts/reflective_mode.py's launchd job.
# ---------------------------------------------------------------------------

def run_reflective_session(api_key: str | None = None) -> dict:
    """Runs one reflective consolidation pass, if allowed (see can_run()).
    Always returns a small result dict — never raises — so both callers can
    log/display the outcome without their own try/except around this."""
    budget = load_budget()
    allowed, reason = can_run(budget)
    if not allowed:
        _log(f"SKIP — {reason}")
        return {"ran": False, "reason": reason}

    _reserve(budget)   # stamp last_session_at BEFORE spending tokens (race guard, see _reserve)

    try:
        key = api_key or os.getenv("GROQ_API_KEY")
        if not key:
            raise RuntimeError("GROQ_API_KEY is not set in .env")

        remaining_budget = budget["daily_budget"] - budget["tokens_used_today"]
        max_tokens = max(0, min(MAX_TOKENS_PER_SESSION, remaining_budget))
        if max_tokens <= 0:
            raise RuntimeError("no token budget remaining today")

        existing_facts = [
            f.get("fact", "") for f in _load_json(MEMORY_SHARED_PATH, [])
            if isinstance(f, dict) and not f.get("outdated")
        ]

        # Ollama-first, Groq-fallback (see this module's import comment) —
        # tokens_used is 0 when Ollama actually answered, since only the
        # Groq fallback path has a real cost to track against the daily
        # budget below.
        raw, tokens_used = sleep_llm._groq_call(
            (
                "Eres HUGO reflexionando en segundo plano sobre lo que sabes de "
                "Joan. Respondes solo con JSON válido, sin comentarios ni rodeos."
            ),
            _PROMPT_TEMPLATE.format(data=_build_context_block()),
            max_tokens,
            api_key=key,
        )
        raw = raw or ""

        insights = _parse_insights(raw)
        added = 0
        for insight in insights:
            if _add_reflective_fact(insight["fact"], insight["category"]):
                added += 1
                # Return value folded into tokens_used below — a connection
                # call only has real cost if its own Ollama attempt failed
                # and it fell through to Groq (see _link_new_fact/
                # _extract_connections_llm), but that cost still has to
                # count against today's budget same as the main call above.
                tokens_used += _link_new_fact(insight["fact"], existing_facts)

        budget = load_budget()
        budget["tokens_used_today"]     += tokens_used
        budget["last_session_tokens"]    = tokens_used
        budget["last_session_insights"]  = added
        save_budget(budget)

        # Entity Pillars Phase 2 — this session's share of the daily
        # reflective budget nudges 'fatiga' up proportionally (a session
        # that ate 10% of the daily budget in one go moves it noticeably
        # more than one that used 1%); it recovers on its own between
        # sessions via core.internal_state's own time-decay, no explicit
        # "rest" call needed.
        try:
            from core import internal_state
            share = tokens_used / max(1, budget.get("daily_budget", 5000))
            internal_state.nudge("fatiga", min(0.15, share * 0.3), f"sesión reflexiva: {tokens_used} tokens")
        except Exception:
            pass

        _log(f"OK — tokens_used={tokens_used} insights_generated={added}")
        return {"ran": True, "tokens_used": tokens_used, "insights": added}

    except Exception as e:
        _log(f"FAILED — {e}")
        logger.warning("Reflective session failed (non-critical)", exc_info=True)
        return {"ran": False, "reason": str(e)}
