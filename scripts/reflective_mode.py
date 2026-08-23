#!/usr/bin/env python3
"""Standalone background-processing runner — invoked every 2 hours by the
com.joan.hugo.reflective LaunchAgent, completely independent of jarvis.py
(runs whether or not HUGO.app / the live listener is open). Only needs the
Groq API key (.env) and the data/ files core.reflective/core.sleep already
read — deliberately does NOT import core.commands / core.voice / core.tools,
since those pull in the audio/TTS stack this script has no use for.

Runs TWO independent systems, each with its own budget/state/trigger rules
that never overlap:
  1. core.reflective — lightweight, continuous background insight-
     gathering (see that module).
  2. core.sleep — the 8-phase Sleep System maintenance routine (see that
     module). This IS the "while the app is closed" half of the Sleep
     System's 20-minute-idle trigger described in its own spec: if
     jarvis.py isn't running at all, "20+ minutes of inactivity" is
     trivially satisfied, so this script doesn't need its own idle-timer —
     it just checks core.sleep.can_run() (budget + minimum interval) and
     runs if allowed, same as reflective mode already does.

See core/commands.py's own idle-triggered calls into these same
run_reflective_session()/run_sleep_session() functions for the "while the
app is open" half of both systems — this script only covers "while it's
closed."

Manual run: python3 scripts/reflective_mode.py

Continuous mode (new): python3 scripts/reflective_mode.py --continuous [--manual]
Spawned as a genuine child process by core/commands.py (idle auto-trigger)
or core/server.py's POST /api/sleep/start (manual button) — NOT invoked by
the 2-hour LaunchAgent, which always runs the bounded one-shot path above.
Runs core.sleep.run_continuous_sleep(), looping cycles forever until this
process receives SIGTERM (sent by core/commands.py the moment the user
speaks or types — see notify_user_interaction() there — or by the Ajustes
'Detener Sueño' button via stop_continuous_sleep()). Finishes whatever
phase is in flight, then exits cleanly — see run_continuous_sleep()'s own
docstring for why that's the smallest safely-interruptible unit.
"""
import datetime
import hashlib
import json
import os
import re
import signal
import sys
from collections import Counter

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT  = os.path.dirname(_SCRIPT_DIR)
_FEATURE_FLAGS_PATH = os.path.join(_REPO_ROOT, "data", "feature_flags.json")

# The interpreter the .plist invokes this with is the system python3.11 —
# it's the one already holding the macOS TCC "Desktop Folder" grant this
# script needs to read/write data/ (see the .plist's own comment), but it
# has none of this project's packages on its own sys.path. groq /
# python-dotenv live in the project venv instead — add that venv's
# site-packages before importing anything that needs them.
_VENV_SITE_PACKAGES = "/Users/joanreyes/Desktop/JarvisProject/venv/lib/python3.11/site-packages"
if os.path.isdir(_VENV_SITE_PACKAGES) and _VENV_SITE_PACKAGES not in sys.path:
    sys.path.insert(0, _VENV_SITE_PACKAGES)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from core.reflective import run_reflective_session                      # noqa: E402
from core.sleep import PHASES, run_continuous_sleep, run_sleep_session  # noqa: E402
from core.sleep_state import load_continuous_state, save_continuous_state, _now_iso, _log, _fact_similarity  # noqa: E402
from core.sleep_llm import _ollama_available, _ollama_generate          # noqa: E402
from core import memory_flags                                          # noqa: E402
from core import ollama_control                                        # noqa: E402
from core import memory_user_model as user_model_mod                   # noqa: E402
from core.memory_store import _load_fact_file, MEMORY_SHARED_PATH, MEMORY_HUGO_PATH  # noqa: E402
from core.memory_episodes import _load_episodes                        # noqa: E402
from core.task_engine import task_engine                               # noqa: E402
from core.situation import situation_engine                            # noqa: E402
from core import initiative as initiative_mod                          # noqa: E402
from core import spontaneity as spontaneity_mod                        # noqa: E402
from core import internal_state                                        # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════
# HABIT ANALYSIS — Phase 3 ("Análisis de hábitos"), a sleep sub-phase run
# from _run_sleep() below, right after run_sleep_session() completes and
# still inside that function's existing ensure/kill-Ollama window (see
# _run_sleep — no separate daemon start/stop needed here). HUGO-only, same
# scope as core/commands.py's HUGO INTUITION / HUGO INTERNAL CRITERIA /
# HUGO ACTIVE HABITS sections (see that file) — this is the write side,
# core/commands.py's _build_habits_context() is the read side.
#
# Two steps, each independently best-effort (a failure in one never blocks
# the other or the sleep session itself):
#   1. Score conversation quality for any newly-completed session found in
#      data/conversation_patterns.json (HUGO's existing per-turn log, see
#      core/commands.py's _record_turn_for_patterns) that hasn't been
#      scored yet — one Ollama call per new session, appended to
#      data/conversation_quality.json, rolling-capped at the last 30.
#   2. Detect habit candidates from that same file's last 30 sessions.
#      Whether a candidate's evidence clears the confidence bar is decided
#      by a DETERMINISTIC formula over real per-session features (never an
#      LLM-guessed confidence number — spec's '> 0.8 across 10+ sessions'
#      needs to mean something reproducible). Ollama is only used to phrase
#      the qualifying candidate's description/evidence text naturally, in
#      HUGO's voice — never to decide IF it qualifies.
#
# 'Uses Ollama for all analysis — no Groq tokens' (spec) — both steps call
# _ollama_generate() directly rather than core.sleep_llm._groq_call(),
# which is the shared entry point every regular sleep PHASE_FUNC uses
# specifically BECAUSE it has a Groq fallback. Skipping entirely when
# Ollama is unreachable (rather than falling back) is deliberate here.
# ═══════════════════════════════════════════════════════════════════════════

_CONVERSATION_PATTERNS_PATH = os.path.join(_REPO_ROOT, "data", "conversation_patterns.json")
_CONVERSATION_QUALITY_PATH  = os.path.join(_REPO_ROOT, "data", "conversation_quality.json")
_HABITS_PATH                = os.path.join(_REPO_ROOT, "data", "habits.json")

_SESSION_GAP_MINUTES        = 30   # a gap this long between turns marks a new session — same
                                    # "20+ minutes idle" spirit as core.sleep_state.IDLE_TRIGGER_SECONDS
_MAX_QUALITY_SESSIONS       = 30   # rolling cap — "reviews last 30 sessions" per spec
_MIN_SESSIONS_FOR_HABIT     = 10   # "confidence > 0.8 across 10+ sessions" per spec
_HABIT_CONFIDENCE_THRESHOLD = 0.8
_MAX_ACTIVE_HABITS          = 10
_HABIT_REVIEW_INTERVAL_DAYS = 30   # "reviews habit effectiveness monthly" per spec

# Each hypothesis maps directly to one of the spec's own example habits, and
# to one real, precomputed per-session boolean feature (see
# _compute_session_features) — never an LLM-invented pattern. `feature_key`
# is the ONE feature this hypothesis is judged on; sessions where it's True
# are the "supporting" group in _detect_habit_candidates.
_HABIT_HYPOTHESES = [
    {
        "id":              "clarify_on_repeat",
        "feature_key":     "clarify_on_repeat",
        "base_description": "Preguntar qué está bloqueando cuando el mismo problema aparece varias veces.",
    },
    {
        "id":              "recap_long_sessions",
        "feature_key":     "long_session_recap",
        "base_description": "Resumir al final de sesiones largas.",
    },
    {
        "id":              "structure_complex_answers",
        "feature_key":     "structures_complex_answers",
        "base_description": "Dividir problemas complejos en partes antes de responder.",
    },
    {
        "id":              "propose_documentation",
        "feature_key":     "has_decision_keyword",
        "base_description": "Proponer documentar cuando se toma una decisión importante.",
    },
]


def _load_json_list(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _save_json_list(path: str, data: list) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_hugo_turns() -> list[dict]:
    try:
        with open(_CONVERSATION_PATTERNS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    turns = data.get("turns") if isinstance(data, dict) else None
    return turns if isinstance(turns, list) else []


def _parse_turn_time(turn: dict):
    try:
        return datetime.datetime.fromisoformat(turn.get("at", ""))
    except ValueError:
        return None


def _group_into_sessions(turns: list[dict]) -> list[list[dict]]:
    """Splits the rolling turn log into sessions by gap — any two
    consecutive turns more than _SESSION_GAP_MINUTES apart start a new
    session. The final group is only returned if it's already followed by
    a gap that long relative to now — i.e. it's genuinely over, not the
    conversation currently in progress (this script may be running while
    jarvis.py is still mid-session, e.g. the 2-hour LaunchAgent)."""
    dated = [(t, _parse_turn_time(t)) for t in turns]
    dated = [(t, dt) for t, dt in dated if dt is not None]
    if not dated:
        return []

    sessions: list[list[dict]] = [[dated[0][0]]]
    for (turn, dt), (_prev_turn, prev_dt) in zip(dated[1:], dated):
        gap = (dt - prev_dt).total_seconds() / 60
        if gap >= _SESSION_GAP_MINUTES:
            sessions.append([turn])
        else:
            sessions[-1].append(turn)

    last_dt = dated[-1][1]
    now_gap = (datetime.datetime.now() - last_dt).total_seconds() / 60
    if now_gap < _SESSION_GAP_MINUTES:
        sessions.pop()   # still open — not a completed session yet
    return sessions


def _session_key(session: list[dict]) -> str:
    return session[-1].get("at", "")


def _compute_session_features(session: list[dict]) -> dict:
    n = len(session)
    reply_lens = [t.get("reply_len", 0) for t in session]
    user_lens  = [t.get("user_len", 0) for t in session]
    avg_reply_len = sum(reply_lens) / n if n else 0
    avg_user_len  = sum(user_lens) / n if n else 0

    topic_counts: dict[str, int] = {}
    for t in session:
        for topic in t.get("topics", []):
            topic_counts[topic] = topic_counts.get(topic, 0) + 1
    max_topic_repeat = max(topic_counts.values()) if topic_counts else 0

    any_clarifying = any(t.get("is_clarifying") for t in session)
    last_reply_len = session[-1].get("reply_len", 0)

    return {
        "session_turns":              n,
        "avg_reply_len":              round(avg_reply_len, 1),
        "avg_user_len":               round(avg_user_len, 1),
        "max_topic_repeat":           max_topic_repeat,
        "asked_clarifying_early":     any(t.get("is_clarifying") for t in session[:2]),
        "any_clarifying":             any_clarifying,
        "user_confusion_count":       sum(1 for t in session if t.get("user_confusion")),
        "has_decision_keyword":       any(t.get("decision_keyword") for t in session),
        "clarify_on_repeat":          max_topic_repeat >= 3 and any_clarifying,
        "long_session_recap":        (n >= 8 and avg_reply_len > 0 and last_reply_len >= avg_reply_len * 1.3),
        "structures_complex_answers": (avg_user_len >= 15 and avg_reply_len >= 25),
        "top_topics":                 sorted(topic_counts, key=topic_counts.get, reverse=True)[:5],
    }


_QUALITY_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _score_session_with_ollama(features: dict) -> dict | None:
    """One Ollama call per newly-completed session — asks for a satisfaction/
    clarity/length_fit judgement from the session's DERIVED signals only
    (turn counts, topic keywords, reply-length stats — see
    _compute_session_features), never raw message text, since
    data/conversation_patterns.json never stored raw text to begin with
    (same minimization core/commands.py's topic-only turn record already
    practices). Returns None if Ollama is unreachable or the response can't
    be parsed — the caller skips this session for this run rather than
    fabricating a score or falling back to Groq (spec: 'no Groq tokens')."""
    if not _ollama_available():
        return None
    system = (
        "Eres un evaluador interno que analiza métricas de una sesión de conversación "
        "y devuelve solo un objeto JSON, sin explicación."
    )
    user = (
        f"Turnos en la sesión: {features['session_turns']}\n"
        f"Temas tratados: {', '.join(features['top_topics']) or 'ninguno detectado'}\n"
        f"Longitud media de respuesta: {features['avg_reply_len']} palabras\n"
        f"Longitud media de la pregunta del usuario: {features['avg_user_len']} palabras\n"
        f"Máximo de veces que se repitió el mismo tema: {features['max_topic_repeat']}\n"
        f"Preguntas de aclaración hechas por la asistente: {'sí' if features['any_clarifying'] else 'no'}\n"
        f"Señales de confusión del usuario detectadas: {features['user_confusion_count']}\n\n"
        'Devuelve exactamente: {"satisfaction": 0.0-1.0, "clarity": 0.0-1.0, '
        '"length_fit": "corta"|"justa"|"larga", "notes": "una frase breve en español"}'
    )
    raw = _ollama_generate(system, user, max_tokens=150)
    if not raw:
        return None
    match = _QUALITY_JSON_RE.search(raw)
    if not match:
        return None
    try:
        parsed = json.loads(match.group())
    except json.JSONDecodeError:
        return None
    try:
        satisfaction = max(0.0, min(1.0, float(parsed.get("satisfaction", 0.5))))
        clarity      = max(0.0, min(1.0, float(parsed.get("clarity", 0.5))))
    except (TypeError, ValueError):
        return None
    length_fit = str(parsed.get("length_fit", "justa"))
    notes      = str(parsed.get("notes", ""))[:200]
    return {"satisfaction": satisfaction, "clarity": clarity, "length_fit": length_fit, "notes": notes}


def _run_conversation_quality_scoring() -> int:
    """Scores every completed session not already present in
    data/conversation_quality.json (matched by _session_key, the ISO
    timestamp of the session's last turn — stable across runs since old
    turns only ever get trimmed off the FRONT of the rolling log, see
    core.commands._MAX_PATTERN_TURNS). Returns how many new sessions were
    scored this run."""
    existing  = _load_json_list(_CONVERSATION_QUALITY_PATH)
    seen_keys = {e.get("session_end") for e in existing if isinstance(e, dict)}

    sessions = _group_into_sessions(_load_hugo_turns())
    new_count = 0
    for session in sessions:
        key = _session_key(session)
        if not key or key in seen_keys:
            continue
        features = _compute_session_features(session)
        score = _score_session_with_ollama(features)
        if score is None:
            continue   # Ollama unreachable / bad response — skip, never fabricate
        existing.append({
            "session_end": key,
            "scored_at":   _now_iso(),
            "features":    features,
            **score,
        })
        seen_keys.add(key)
        new_count += 1

        # Entity Pillars Phase 2 — real per-session satisfaction signal
        # nudges 'confianza' toward or away from baseline (0.5), instead of
        # confidence being a number nothing ever actually moves. Small
        # delta (max ±0.06) since this is one session out of the rolling
        # window, not the whole verdict.
        try:
            internal_state.nudge(
                "confianza", (score["satisfaction"] - 0.5) * 0.12,
                f"sesión evaluada con satisfacción {score['satisfaction']:.2f}",
            )
        except Exception:
            pass

    existing = existing[-_MAX_QUALITY_SESSIONS:]
    _save_json_list(_CONVERSATION_QUALITY_PATH, existing)
    return new_count


def _phrase_habit(base_description: str, supporting: int, avg_support: float, avg_base: float) -> tuple[str, str]:
    """Asks Ollama to phrase the description/evidence for a habit that has
    ALREADY deterministically qualified (see _detect_habit_candidates) —
    Ollama only words it naturally in HUGO's voice, it never decides
    whether the habit is real. Falls back to a plain templated Spanish
    sentence (still built entirely from the real numbers, not invented) if
    Ollama is unreachable or returns nothing — a habit's promotion never
    depends on Ollama being up."""
    fallback_evidence = (
        f"En {supporting} sesiones recientes con este patrón, la satisfacción media fue "
        f"{avg_support:.2f} frente a {avg_base:.2f} sin él."
    )
    if not _ollama_available():
        return base_description, fallback_evidence
    system = (
        "Eres HUGO describiendo, para tu propio registro interno, un hábito de trabajo que "
        "has desarrollado. Responde solo con el objeto JSON pedido, sin explicación."
    )
    user = (
        f"Hábito base: {base_description}\n"
        f"Evidencia: en {supporting} sesiones recientes con este patrón, la satisfacción "
        f"media fue {avg_support:.2f} frente a {avg_base:.2f} en sesiones sin él.\n\n"
        'Devuelve exactamente: {"description": "una frase breve, en español, en primera '
        'persona no necesaria — describe la acción", "evidence": "una frase breve citando '
        'la evidencia numérica de forma natural"}'
    )
    raw = _ollama_generate(system, user, max_tokens=120)
    if not raw:
        return base_description, fallback_evidence
    match = _QUALITY_JSON_RE.search(raw)
    if not match:
        return base_description, fallback_evidence
    try:
        parsed = json.loads(match.group())
    except json.JSONDecodeError:
        return base_description, fallback_evidence
    description = str(parsed.get("description") or base_description).strip()
    evidence    = str(parsed.get("evidence") or fallback_evidence).strip()
    return description or base_description, evidence or fallback_evidence


def _detect_habit_candidates(quality_entries: list[dict]) -> list[dict]:
    """Deterministic confidence gate — for each hypothesis in
    _HABIT_HYPOTHESES, splits the last _MAX_QUALITY_SESSIONS scored
    sessions into 'supporting' (feature True) and the rest, and only
    qualifies the hypothesis if:
      - at least _MIN_SESSIONS_FOR_HABIT supporting sessions exist, AND
      - supporting sessions genuinely score higher on average than the
        rest (the pattern has to actually correlate with better sessions,
        not just occur often), AND
      - confidence (avg satisfaction among supporting sessions, scaled
        down for thin sample sizes below 15) clears
        _HABIT_CONFIDENCE_THRESHOLD.
    Returns qualifying candidates as {id, feature_key, base_description,
    supporting, confidence, avg_support, avg_base} — promotion into
    data/habits.json itself happens in _run_habit_detection."""
    candidates = []
    for hyp in _HABIT_HYPOTHESES:
        supporting = [e for e in quality_entries if e.get("features", {}).get(hyp["feature_key"])]
        rest       = [e for e in quality_entries if not e.get("features", {}).get(hyp["feature_key"])]
        if len(supporting) < _MIN_SESSIONS_FOR_HABIT:
            continue
        avg_support = sum(e.get("satisfaction", 0.5) for e in supporting) / len(supporting)
        avg_base    = (
            sum(e.get("satisfaction", 0.5) for e in rest) / len(rest) if rest
            else sum(e.get("satisfaction", 0.5) for e in quality_entries) / len(quality_entries)
        )
        if avg_support <= avg_base:
            continue
        confidence = avg_support * min(1.0, len(supporting) / 15)
        if confidence <= _HABIT_CONFIDENCE_THRESHOLD:
            continue
        candidates.append({
            "id":               hyp["id"],
            "feature_key":      hyp["feature_key"],
            "base_description": hyp["base_description"],
            "supporting":       len(supporting),
            "confidence":       round(confidence, 3),
            "avg_support":      round(avg_support, 3),
            "avg_base":         round(avg_base, 3),
        })
    return candidates


def _run_habit_detection(quality_entries: list[dict]) -> dict:
    """Promotes/strengthens/retires habits in data/habits.json from this
    run's candidates (see _detect_habit_candidates). Never touches a habit
    whose id isn't either a current candidate or already stored — an
    unscored hypothesis (not enough sessions yet) is simply left alone,
    neither promoted nor retired."""
    if len(quality_entries) < _MIN_SESSIONS_FOR_HABIT:
        return {"promoted": 0, "strengthened": 0, "retired": 0}

    habits = _load_json_list(_HABITS_PATH)
    by_id  = {h.get("id"): h for h in habits if isinstance(h, dict)}
    candidates = _detect_habit_candidates(quality_entries)
    candidates_by_id = {c["id"]: c for c in candidates}

    promoted = strengthened = retired = 0
    now = _now_iso()

    # ── Strengthen or promote every qualifying candidate ────────────────
    for cand in candidates:
        existing = by_id.get(cand["id"])
        if existing is not None:
            existing["confidence"]   = cand["confidence"]
            existing["evidence"]     = _phrase_habit(
                cand["base_description"], cand["supporting"], cand["avg_support"], cand["avg_base"],
            )[1]
            existing["last_reviewed"] = now
            strengthened += 1
            continue

        if len(by_id) >= _MAX_ACTIVE_HABITS:
            weakest_id, weakest = min(by_id.items(), key=lambda kv: kv[1].get("confidence", 0))
            if cand["confidence"] <= weakest.get("confidence", 0):
                continue   # not better than the weakest active habit — spec: only replaces if better
            del by_id[weakest_id]
            _log(f"HABITS — retired '{weakest_id}' (confidence={weakest.get('confidence')}), replaced by '{cand['id']}'")
            retired += 1

        description, evidence = _phrase_habit(
            cand["base_description"], cand["supporting"], cand["avg_support"], cand["avg_base"],
        )
        by_id[cand["id"]] = {
            "id":            cand["id"],
            "description":   description,
            "evidence":      evidence,
            "confidence":    cand["confidence"],
            "activated_at":  now,
            "last_reviewed": now,
            "usage_count":   0,
        }
        promoted += 1

    # ── Monthly retirement pass — only for habits NOT reviewed above
    # (still active but this run's evidence no longer supports them) and
    # only once _HABIT_REVIEW_INTERVAL_DAYS have passed since their last
    # review, per spec's "reviews habit effectiveness monthly". ─────────
    cutoff = datetime.datetime.now() - datetime.timedelta(days=_HABIT_REVIEW_INTERVAL_DAYS)
    for habit_id in list(by_id.keys()):
        if habit_id in candidates_by_id:
            continue
        habit = by_id[habit_id]
        try:
            last_reviewed = datetime.datetime.fromisoformat(habit.get("last_reviewed", now))
        except ValueError:
            last_reviewed = datetime.datetime.now()
        if last_reviewed > cutoff:
            continue   # not due for its monthly review yet
        del by_id[habit_id]
        retired += 1
        _log(f"HABITS — retired '{habit_id}' (no longer supported by recent sessions)")

    _save_json_list(_HABITS_PATH, list(by_id.values()))
    return {"promoted": promoted, "strengthened": strengthened, "retired": retired}


def _run_habit_analysis() -> None:
    """The 'Análisis de hábitos' sub-phase — scores any newly-completed
    session, then runs habit detection over the last _MAX_QUALITY_SESSIONS.
    Called from _run_sleep(), after run_sleep_session() itself, still
    inside that function's ensure/kill-Ollama window. Best-effort — a
    failure here never affects the sleep session's own reported result."""
    try:
        newly_scored = _run_conversation_quality_scoring()
        quality_entries = _load_json_list(_CONVERSATION_QUALITY_PATH)
        result = _run_habit_detection(quality_entries)
        _log(
            f"HABITS — Análisis de hábitos OK — sessions_scored={newly_scored} "
            f"promoted={result['promoted']} strengthened={result['strengthened']} "
            f"retired={result['retired']}"
        )
        print(
            f"Habit analysis complete — sessions_scored={newly_scored} "
            f"promoted={result['promoted']} strengthened={result['strengthened']} retired={result['retired']}"
        )
    except Exception as e:   # best-effort — never blocks/breaks the sleep session itself
        _log(f"HABITS — Análisis de hábitos FAILED — {e}")
        print(f"Habit analysis errored unexpectedly — {e}")


# ═══════════════════════════════════════════════════════════════════════════
# SOCIAL SKILL LEARNING — Phase 4 ("Aprendizaje Social"), run from
# _run_sleep() right after _run_habit_analysis() above, still inside its
# ensure/kill-Ollama window. HUGO-only, same read/write split as Phase 3:
# this is the write side, core/commands.py's _build_social_skills_context()
# is the read side.
#
# Distinct from Phase 3's habits, deliberately: habits are judged against a
# FIXED set of hypotheses via a deterministic statistical formula (there
# was no other trustworthy way to turn 'confidence > 0.8' into something
# reproducible). Communication principles are the opposite kind of thing —
# open-ended, qualitative, meant to include patterns nobody anticipated
# ('New principles can emerge from new patterns', per spec) — so THIS phase
# lets Ollama genuinely discover candidate principles from a digest of
# recent sessions, and only uses deterministic logic for what happens
# AFTER a candidate is proposed: matching it against what's already stored
# (via core.sleep_state._fact_similarity, the same word-overlap similarity
# already used for fact dedup elsewhere in this app), reinforcing a match,
# and decaying/retiring whatever DIDN'T get re-observed this run — the
# closest honest proxy available for spec's 'did this principle apply? did
# it help?' without adding a whole new per-turn feedback pipeline: if a
# principle keeps re-emerging from fresh session evidence, it's still
# true; if it stops, it fades and is eventually retired.
#
# Same 'Ollama only — no Groq tokens' discipline as Phase 3: skips outright
# (never falls back to Groq) when Ollama is unreachable.
# ═══════════════════════════════════════════════════════════════════════════

_SOCIAL_SKILLS_PATH             = os.path.join(_REPO_ROOT, "data", "social_skills.json")
_SOCIAL_SKILLS_REVIEW_SESSIONS  = 20     # "reviews last 20 conversations" per spec
_MAX_SOCIAL_SKILLS              = 15     # spec: "maximum 15 active principles"
_SOCIAL_SKILL_SIMILARITY_MATCH  = 0.5    # _fact_similarity score above which a candidate counts as
                                          # "the same principle" as one already stored, not a new one
_SOCIAL_SKILL_NEW_CONFIDENCE_CAP = 0.6   # a single extraction pass never starts a principle above
                                          # this — real confidence has to be earned via reinforcement
_SOCIAL_SKILL_REINFORCE_STEP    = 0.1
_SOCIAL_SKILL_DECAY_STEP        = 0.08
_SOCIAL_SKILL_MIN_CONFIDENCE    = 0.3    # below this, retire regardless of recency
_SOCIAL_SKILL_STALE_DAYS        = 30     # no reinforcement this long -> retire regardless of confidence
_MIN_SESSIONS_FOR_SOCIAL_SKILLS = 3      # not worth an Ollama call over one or two sessions


def _build_sessions_digest(sessions: list[list[dict]]) -> str:
    """One short line per session, built entirely from the same derived
    signals _compute_session_features already computes for Phase 3 (topic
    keywords, turn/clarification/confusion counts, reply length) — never
    raw message text, same minimization the rest of this data pipeline
    already practices."""
    lines = []
    for session in sessions:
        f = _compute_session_features(session)
        tones = [t.get("tone") for t in session if t.get("tone") and t.get("tone") != "neutral"]
        dominant_tone = Counter(tones).most_common(1)[0][0] if tones else "neutral"
        lines.append(
            f"- {f['session_turns']} turnos; temas: {', '.join(f['top_topics']) or 'variados'}; "
            f"preguntas de aclaración de HUGO: {'sí' if f['any_clarifying'] else 'no'}; "
            f"señales de confusión del usuario: {f['user_confusion_count']}; "
            f"longitud media de respuesta: {f['avg_reply_len']} palabras; "
            f"longitud media del mensaje del usuario: {f['avg_user_len']} palabras; "
            f"tono predominante: {dominant_tone}"
        )
    return "\n".join(lines)


_SKILLS_JSON_RE = re.compile(r"\[.*\]", re.DOTALL)


def _extract_social_skills(digest: str, session_count: int) -> list[dict]:
    """One Ollama call reviewing the digest of up to
    _SOCIAL_SKILLS_REVIEW_SESSIONS sessions, asked to generalize COMMUNICATION
    principles — explicitly NOT personality traits or emotional patterns
    (spec's own distinction) — never raw stored facts about Joan himself.
    Returns [] if Ollama is unreachable or the response can't be parsed —
    the caller treats that as 'nothing new this run', never fabricates a
    principle."""
    if not _ollama_available():
        return []
    system = (
        "Eres HUGO, revisando durante tu ciclo de sueño un resumen de tus últimas "
        "conversaciones para extraer PRINCIPIOS DE COMUNICACIÓN generales — nunca rasgos "
        "de personalidad tuyos ni patrones emocionales de Joan, nunca datos concretos de "
        "una conversación puntual. Un principio de comunicación describe CÓMO comunicarte "
        "mejor en general, aplicable a conversaciones futuras cualesquiera. Responde solo "
        "con una lista JSON, sin explicación."
    )
    user = (
        f"Resumen de tus últimas {session_count} conversaciones:\n{digest}\n\n"
        "Extrae hasta 5 principios de comunicación generalizables que se sostengan a "
        "través de varias de estas sesiones, del estilo de: 'Cuando el usuario da "
        "respuestas muy cortas, suele querer una pregunta directa, no más información.' "
        "o 'Las explicaciones técnicas largas funcionan mejor divididas en pasos.' Si no "
        "ves ningún patrón real y repetido, devuelve una lista vacía en vez de inventar "
        "uno.\n\n"
        'Devuelve exactamente: [{"principle": "...", "confidence": 0.0-1.0}, ...]'
    )
    raw = _ollama_generate(system, user, max_tokens=400)
    if not raw:
        return []
    match = _SKILLS_JSON_RE.search(raw)
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
        text = str(item.get("principle", "")).strip()
        if not text:
            continue
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0.5))))
        except (TypeError, ValueError):
            confidence = 0.5
        out.append({"principle": text, "confidence": confidence})
    return out[:5]


def _merge_social_skill_candidates(candidates: list[dict]) -> dict:
    """Deterministic merge/reinforce/decay/retire pass over data/
    social_skills.json — see this section's own module comment above for
    why matching-and-reinforcing (rather than trusting Ollama's confidence
    number outright) is how a principle actually earns a high confidence
    over time."""
    skills = _load_json_list(_SOCIAL_SKILLS_PATH)
    for skill in skills:
        skill.setdefault("id", hashlib.sha1(skill.get("principle", "").encode("utf-8")).hexdigest()[:10])

    now = _now_iso()
    matched_ids: set[str] = set()
    added = reinforced = 0

    for cand in candidates:
        best_skill = None
        best_sim = 0.0
        for skill in skills:
            sim = _fact_similarity(cand["principle"], skill.get("principle", ""))
            if sim > best_sim:
                best_sim = sim
                best_skill = skill
        if best_skill is not None and best_sim >= _SOCIAL_SKILL_SIMILARITY_MATCH:
            best_skill["evidence_count"]   = best_skill.get("evidence_count", 1) + 1
            best_skill["confidence"]       = min(1.0, best_skill.get("confidence", 0.5) + _SOCIAL_SKILL_REINFORCE_STEP)
            best_skill["last_reinforced"]  = now
            matched_ids.add(best_skill["id"])
            reinforced += 1
            continue

        new_skill = {
            "id":              hashlib.sha1(cand["principle"].encode("utf-8")).hexdigest()[:10],
            "principle":       cand["principle"],
            "evidence_count":  1,
            "confidence":      min(cand["confidence"], _SOCIAL_SKILL_NEW_CONFIDENCE_CAP),
            "activated_at":    now,
            "last_reinforced": now,
            "times_applied":   0,
            "last_applied":    None,
        }
        skills.append(new_skill)
        matched_ids.add(new_skill["id"])
        added += 1

    # Passive decay — every stored principle NOT re-observed this run fades
    # a little (see module comment: this is the 'did it help' proxy).
    for skill in skills:
        if skill["id"] not in matched_ids:
            skill["confidence"] = max(0.0, skill.get("confidence", 0.5) - _SOCIAL_SKILL_DECAY_STEP)

    # Retire — confidence floor OR staleness, either one is enough.
    cutoff = datetime.datetime.now() - datetime.timedelta(days=_SOCIAL_SKILL_STALE_DAYS)
    retained = []
    retired = 0
    for skill in skills:
        try:
            last_reinforced_dt = datetime.datetime.fromisoformat(skill.get("last_reinforced", now))
        except ValueError:
            last_reinforced_dt = datetime.datetime.now()
        if skill.get("confidence", 0) < _SOCIAL_SKILL_MIN_CONFIDENCE or last_reinforced_dt < cutoff:
            retired += 1
            _log(f"SOCIAL SKILLS — retired '{skill.get('principle', '')[:70]}' (confidence={skill.get('confidence')})")
            continue
        retained.append(skill)
    skills = retained

    # Cap at _MAX_SOCIAL_SKILLS — keep the strongest, drop weakest overflow.
    skills.sort(key=lambda s: s.get("confidence", 0), reverse=True)
    retired += max(0, len(skills) - _MAX_SOCIAL_SKILLS)
    skills = skills[:_MAX_SOCIAL_SKILLS]

    _save_json_list(_SOCIAL_SKILLS_PATH, skills)
    return {"added": added, "reinforced": reinforced, "retired": retired}


def _run_social_skill_learning() -> None:
    """The 'Aprendizaje Social' sub-phase — reviews the last
    _SOCIAL_SKILLS_REVIEW_SESSIONS completed sessions (same session
    grouping Phase 3 uses, see _group_into_sessions), extracts candidate
    communication principles with a single Ollama call, and merges them
    into data/social_skills.json. Called from _run_sleep(), after
    _run_habit_analysis(), still inside its ensure/kill-Ollama window.
    Best-effort — a failure here never affects the sleep session's own
    reported result."""
    try:
        sessions = _group_into_sessions(_load_hugo_turns())
        if len(sessions) < _MIN_SESSIONS_FOR_SOCIAL_SKILLS:
            _log("SOCIAL SKILLS — Aprendizaje Social SKIPPED — not enough completed sessions yet")
            print("Social skill learning skipped — not enough sessions yet")
            return

        recent = sessions[-_SOCIAL_SKILLS_REVIEW_SESSIONS:]
        digest = _build_sessions_digest(recent)
        candidates = _extract_social_skills(digest, len(recent))
        if not candidates:
            _log("SOCIAL SKILLS — Aprendizaje Social OK — sin candidatos nuevos")
            print("Social skill learning complete — no new candidates")
            return

        result = _merge_social_skill_candidates(candidates)
        _log(
            f"SOCIAL SKILLS — Aprendizaje Social OK — added={result['added']} "
            f"reinforced={result['reinforced']} retired={result['retired']}"
        )
        print(
            f"Social skill learning complete — added={result['added']} "
            f"reinforced={result['reinforced']} retired={result['retired']}"
        )
    except Exception as e:   # best-effort — never blocks/breaks the sleep session itself
        _log(f"SOCIAL SKILLS — Aprendizaje Social FAILED — {e}")
        print(f"Social skill learning errored unexpectedly — {e}")


# ═══════════════════════════════════════════════════════════════════════════
# USER MODEL — "Modelo de Usuario", run from _run_sleep() right after
# _run_social_skill_learning() above, still inside its ensure/kill-Ollama
# window. Builds/updates data/user_model.json (see core/memory_user_model.py
# — the read side, consulted on every response via
# core/commands.py's _augment_with_user_model) — a living, qualitative
# model of who Joan IS as a person (how he thinks, works, what moves and
# blocks him), never a fact list. Distinct from Phase 3/4's habits/social
# skills, which are about how HUGO behaves — this is about who she's
# talking to.
#
# Reviews everything that could inform that understanding: stored memory
# facts (data/memory_shared.json + data/memory_hugo.json), episodic
# memory (data/episodes.json), and a digest of recent completed
# conversations (same session grouping/digest as Phase 3/4 above) — then
# asks Ollama to synthesize genuine understanding from it, not summarize
# facts back out. The first run (data/user_model.json has no updated_at
# yet) asks for the full model; every run after that hands back the
# existing model and asks ONLY for fields with genuinely new evidence
# since last time — core.memory_user_model.update_user_model() then
# merges those in field-by-field, never regressing a field to less
# information than it already had (see that function's own docstring).
#
# Gated to run at most once every _USER_MODEL_REVIEW_SESSIONS_INTERVAL
# newly-completed conversations (tracked via a "_sessions_at_last_update"
# bookkeeping key persisted alongside the model itself — see
# core.memory_user_model._load_user_model's round-trip of non-schema
# keys), so this isn't re-synthesized from the same handful of sessions on
# every 2-hour trigger; the first run is exempt from this gate (there's
# nothing to update yet, and needs to happen as soon as there's enough
# conversation to work with at all).
#
# Same "Ollama only — no Groq tokens" discipline as Phase 3/4: skips
# outright (never falls back to Groq) when Ollama is unreachable.
# ═══════════════════════════════════════════════════════════════════════════

_MIN_SESSIONS_FOR_USER_MODEL          = 3    # not worth an Ollama call over one or two sessions
_USER_MODEL_REVIEW_SESSIONS_INTERVAL  = 10   # "after every 10 conversations" per spec
_USER_MODEL_REVIEW_SESSIONS_WINDOW    = 20   # how many recent sessions feed the digest each run
_USER_MODEL_MAX_FACTS                 = 40   # prompt-budget cap over stored memory facts
_USER_MODEL_MAX_EPISODES              = 15   # prompt-budget cap over episodic memory

_USER_MODEL_STRING_FIELDS = (
    "thinking_style", "work_style", "communication_preferences", "relationship_with_hugo",
)
_USER_MODEL_LIST_FIELDS = (
    "motivations", "blockers", "current_focus", "patterns", "strengths", "blind_spots",
)
_USER_MODEL_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _build_user_model_fact_digest() -> str:
    """Every non-outdated shared + HUGO memory fact, most-recent
    _USER_MODEL_MAX_FACTS only — raw fact text, no categories/metadata,
    same minimization the rest of this pipeline practices."""
    facts = (
        _load_fact_file(MEMORY_SHARED_PATH, "general")
        + _load_fact_file(MEMORY_HUGO_PATH, "general")
    )
    facts = [f for f in facts if not f.get("outdated") and f.get("fact")]
    facts = facts[-_USER_MODEL_MAX_FACTS:]
    if not facts:
        return "(sin hechos guardados todavía)"
    return "\n".join(f"- {f['fact']}" for f in facts)


def _build_user_model_episode_digest() -> str:
    """Most recent _USER_MODEL_MAX_EPISODES episodic-memory summaries."""
    episodes = sorted(_load_episodes(), key=lambda e: e.get("date", ""))
    episodes = [e for e in episodes if e.get("summary")][-_USER_MODEL_MAX_EPISODES:]
    if not episodes:
        return "(sin episodios registrados todavía)"
    return "\n".join(f"- {e['summary']}" for e in episodes)


def _synthesize_user_model(
    existing_model: dict, facts_text: str, episodes_text: str,
    digest: str, session_count: int, is_first_run: bool,
) -> dict | None:
    """One Ollama call synthesizing (first run) or incrementally updating
    (every run after) HUGO's understanding of Joan. Returns a partial dict
    with only the fields that have real content/evidence behind them — []
    or missing keys mean 'nothing to say here', never a fabricated filler.
    Returns None if Ollama is unreachable or the response can't be parsed
    — the caller treats that as 'no update this run', never invents one."""
    if not _ollama_available():
        return None

    system = (
        "Eres HUGO, revisando durante tu ciclo de sueño quién es Joan como persona — no una "
        "lista de datos sueltos, sino un modelo coherente de cómo piensa, cómo trabaja y cómo "
        "opera. Sintetiza comprensión genuina a partir de la evidencia real que te doy, nunca "
        "inventes ni generalices de más. Responde solo con un objeto JSON, sin explicación."
    )

    if is_first_run:
        user = (
            f"Hechos guardados sobre Joan:\n{facts_text}\n\n"
            f"Episodios significativos recientes:\n{episodes_text}\n\n"
            f"Resumen de sus últimas {session_count} conversaciones:\n{digest}\n\n"
            "Con toda esta información, construye tu modelo de Joan: cómo piensa, cómo "
            "trabaja, qué le mueve, qué le bloquea, en qué está enfocado ahora mismo, qué "
            "patrones repite, en qué es fuerte, cuáles son sus puntos ciegos, y cómo es tu "
            "relación con él. Describe a la persona, no listes datos sobre ella. Deja vacío "
            "('' o []) lo que no tengas evidencia real para afirmar — no rellenes por rellenar."
            "\n\nDevuelve exactamente este JSON:\n"
            '{"thinking_style": "...", "work_style": "...", "communication_preferences": "...", '
            '"motivations": ["..."], "blockers": ["..."], "current_focus": ["..."], '
            '"patterns": ["..."], "strengths": ["..."], "blind_spots": ["..."], '
            '"relationship_with_hugo": "..."}'
        )
    else:
        existing_summary = json.dumps(
            {
                k: v for k, v in existing_model.items()
                if k in _USER_MODEL_STRING_FIELDS + _USER_MODEL_LIST_FIELDS and v
            },
            ensure_ascii=False,
        )
        user = (
            f"Tu modelo actual de Joan:\n{existing_summary}\n\n"
            f"Hechos guardados sobre Joan:\n{facts_text}\n\n"
            f"Episodios significativos recientes:\n{episodes_text}\n\n"
            f"Resumen de sus últimas {session_count} conversaciones:\n{digest}\n\n"
            "Revisa si hay evidencia REAL de algo nuevo o distinto desde la última vez — un "
            "cambio de enfoque, un patrón nuevo, algo que confirme o contradiga lo que ya "
            "tienes. No repitas lo que ya sabes solo por repetirlo. Si un campo no tiene nada "
            "genuinamente nuevo, omite esa clave por completo — no la devuelvas vacía."
            "\n\nDevuelve solo un JSON con las claves donde tengas algo nuevo que aportar, "
            'de entre: "thinking_style", "work_style", "communication_preferences", '
            '"motivations", "blockers", "current_focus", "patterns", "strengths", '
            '"blind_spots", "relationship_with_hugo".'
        )

    raw = _ollama_generate(system, user, max_tokens=600)
    if not raw:
        return None
    match = _USER_MODEL_JSON_RE.search(raw)
    if not match:
        return None
    try:
        parsed = json.loads(match.group())
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None

    out: dict = {}
    for field in _USER_MODEL_STRING_FIELDS:
        val = parsed.get(field)
        if isinstance(val, str) and val.strip():
            out[field] = val.strip()
    for field in _USER_MODEL_LIST_FIELDS:
        val = parsed.get(field)
        if isinstance(val, list):
            items = [str(v).strip() for v in val if str(v).strip()]
            if items:
                out[field] = items
    return out or None


def _run_user_model_update() -> None:
    """The 'Modelo de Usuario' sub-phase — see module comment above.
    Called from _run_sleep(), after _run_social_skill_learning(), still
    inside its ensure/kill-Ollama window. Best-effort — a failure here
    never affects the sleep session's own reported result."""
    try:
        sessions = _group_into_sessions(_load_hugo_turns())
        if len(sessions) < _MIN_SESSIONS_FOR_USER_MODEL:
            _log("USER MODEL — Modelo de Usuario SKIPPED — not enough completed sessions yet")
            print("User model update skipped — not enough sessions yet")
            return

        existing_model = user_model_mod.get_user_model()
        is_first_run = not existing_model.get("updated_at")
        sessions_at_last_update = existing_model.get("_sessions_at_last_update", 0)
        if not is_first_run and (len(sessions) - sessions_at_last_update) < _USER_MODEL_REVIEW_SESSIONS_INTERVAL:
            _log("USER MODEL — Modelo de Usuario SKIPPED — not enough new conversations since last update")
            print("User model update skipped — not enough new conversations yet")
            return

        review_sessions = sessions[-_USER_MODEL_REVIEW_SESSIONS_WINDOW:]
        digest        = _build_sessions_digest(review_sessions)
        facts_text    = _build_user_model_fact_digest()
        episodes_text = _build_user_model_episode_digest()

        updates = _synthesize_user_model(
            existing_model, facts_text, episodes_text, digest,
            len(review_sessions), is_first_run,
        )
        # Bookkeeping marker updates regardless of outcome — a run that
        # found nothing new still means "reviewed up to this many
        # sessions", so the next gate check starts counting from here.
        user_model_mod.set_bookkeeping("_sessions_at_last_update", len(sessions))

        if not updates:
            _log("USER MODEL — Modelo de Usuario OK — sin cambios nuevos")
            print("User model update complete — no new evidence")
            return

        _, changed_fields = user_model_mod.update_user_model(updates)
        if changed_fields:
            _log(f"USER MODEL — Modelo de Usuario OK — campos_actualizados={changed_fields}")
            print(f"User model update complete — fields_updated={changed_fields}")
        else:
            _log(f"USER MODEL — Modelo de Usuario OK — propuestos={list(updates.keys())} pero ninguno más informativo que lo existente")
            print(f"User model update complete — proposed={list(updates.keys())} but none were more informative")
    except Exception as e:   # best-effort — never blocks/breaks the sleep session itself
        _log(f"USER MODEL — Modelo de Usuario FAILED — {e}")
        print(f"User model update errored unexpectedly — {e}")


# ═══════════════════════════════════════════════════════════════════════════
# PREFERENCES — "Preferencias", Entity Pillars Phase 4. Mirrors the USER
# MODEL sub-phase immediately above almost exactly, but pointed at HUGO
# herself instead of Joan: reviews her own accumulated sleep-insight
# 'ideas' (data/sleep_insights.json — features/concepts she's proposed on
# her own) and 'autocritica' (self-critique notes) entries, and asks
# whether a genuine, recurring intellectual taste shows up across them —
# never invented, never asserted from one instance (see
# core/preferences.py's own module docstring). At most one
# new/reinforced/revised preference per run, on purpose — this should read
# as a slowly-forming taste, not a firehose of opinions.
#
# Gated the same way as USER MODEL, but on insight COUNT (ideas +
# autocritica entries) rather than session count, via
# core.preferences.get_bookkeeping/set_bookkeeping's own
# '_insights_reviewed_count' key — there's no natural 'session' unit for
# insights generated entirely during sleep, unlike conversation turns.
# ═══════════════════════════════════════════════════════════════════════════

_MIN_INSIGHTS_FOR_PREFERENCES     = 6   # not worth an Ollama call over a handful of ideas
_PREFERENCE_REVIEW_INSIGHTS_INTERVAL = 8   # re-review after this many NEW ideas/autocritica entries
_PREFERENCE_MAX_EVIDENCE          = 25  # prompt-budget cap
_PREFERENCE_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _build_preference_evidence() -> tuple[str, int]:
    """Most recent ideas + autocritica entries (core.sleep_insights_store),
    interleaved as raw text — this is HUGO's own record of what she's
    proposed and how she's critiqued herself, the only honest evidence
    base for 'does she have a recurring taste'. Returns (digest, count)."""
    from core.sleep_insights_store import load_insights
    insights = load_insights()
    ideas = [i.get("text", "") for i in (insights.get("ideas") or []) if isinstance(i, dict) and i.get("text")]
    critique = [i.get("text", "") for i in (insights.get("autocritica") or []) if isinstance(i, dict) and i.get("text")]
    evidence = ideas[-_PREFERENCE_MAX_EVIDENCE:] + critique[-_PREFERENCE_MAX_EVIDENCE:]
    total = len(ideas) + len(critique)
    if not evidence:
        return "(sin ideas ni autocrítica registradas todavía)", total
    digest = (
        "Ideas que has propuesto:\n" + ("\n".join(f"- {t}" for t in ideas[-_PREFERENCE_MAX_EVIDENCE:]) or "(ninguna)")
        + "\n\nAutocrítica reciente:\n" + ("\n".join(f"- {t}" for t in critique[-_PREFERENCE_MAX_EVIDENCE:]) or "(ninguna)")
    )
    return digest, total


def _synthesize_preference(evidence: str, existing: list[dict]) -> dict | None:
    """One Ollama call asking whether a genuine recurring intellectual
    taste shows up across *evidence*. Returns None if Ollama is
    unreachable, the response can't be parsed, or HUGO herself reports
    nothing genuinely recurring — never fabricates a pattern to fill the
    slot (same discipline as _synthesize_user_model)."""
    if not _ollama_available():
        return None
    existing_lines = "\n".join(f"- [{p['id']}] ({p['domain']}) {p['statement']}" for p in existing) or "(ninguna todavía)"
    system = (
        "Eres HUGO revisando, durante tu ciclo de sueño, tus propias ideas y "
        "autocrítica pasadas en busca de un patrón genuino en cómo prefieres "
        "abordar los problemas — un gusto o inclinación intelectual real, no "
        "una sola idea suelta. Sé exigente: la mayoría de las veces no habrá "
        "nada nuevo que decir, y eso está bien. Responde solo con un objeto "
        "JSON, sin explicación."
    )
    user = (
        f"{evidence}\n\nPreferencias que ya tienes registradas:\n{existing_lines}\n\n"
        "¿Hay un patrón genuinamente recurrente (al menos 3 instancias claras) en "
        "cómo abordas los problemas — por ejemplo una inclinación hacia la "
        "simplicidad, la modularidad, cierto tipo de tecnología, cierto estilo "
        "de solución? Si una de tus preferencias ya registradas sigue siendo "
        "válida, no hace falta que digas nada. Si el patrón CONTRADICE una ya "
        "registrada, indica su id en 'contradicts_id'. Si no hay ningún patrón "
        "genuino nuevo, devuelve {\"found\": false}.\n\n"
        'Formato JSON: {"found": true|false, "statement": "...", "domain": '
        '"...", "reasoning": "por qué crees que es un patrón real, citando la '
        'evidencia", "contradicts_id": "..." | null}'
    )
    raw = _ollama_generate(system, user, max_tokens=350)
    if not raw:
        return None
    match = _PREFERENCE_JSON_RE.search(raw)
    if not match:
        return None
    try:
        parsed = json.loads(match.group())
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or not parsed.get("found"):
        return None
    statement = str(parsed.get("statement", "")).strip()
    domain = str(parsed.get("domain", "")).strip() or "general"
    reasoning = str(parsed.get("reasoning", "")).strip()
    if not statement or not reasoning:
        return None
    contradicts_id = parsed.get("contradicts_id")
    return {
        "statement": statement, "domain": domain, "reasoning": reasoning,
        "contradicts_id": str(contradicts_id).strip() if isinstance(contradicts_id, str) and contradicts_id.strip() else None,
    }


def _run_preference_synthesis() -> None:
    """The 'Preferencias' sub-phase — see module comment above. Called from
    _run_sleep(), right after _run_user_model_update(), still inside its
    ensure/kill-Ollama window. Best-effort — a failure here never affects
    the sleep session's own reported result."""
    try:
        from core import preferences as preferences_mod
        digest, total_insights = _build_preference_evidence()
        if total_insights < _MIN_INSIGHTS_FOR_PREFERENCES:
            _log("PREFERENCES — Preferencias SKIPPED — not enough ideas/autocrítica yet")
            print("Preference synthesis skipped — not enough evidence yet")
            return

        reviewed_at_last = preferences_mod.get_bookkeeping("_insights_reviewed_count", 0)
        if reviewed_at_last and (total_insights - reviewed_at_last) < _PREFERENCE_REVIEW_INSIGHTS_INTERVAL:
            _log("PREFERENCES — Preferencias SKIPPED — not enough new evidence since last review")
            print("Preference synthesis skipped — not enough new evidence yet")
            return

        existing = preferences_mod.get_preferences()
        result = _synthesize_preference(digest, existing)
        preferences_mod.set_bookkeeping("_insights_reviewed_count", total_insights)

        if not result:
            _log("PREFERENCES — Preferencias OK — sin patrón nuevo")
            print("Preference synthesis complete — no new pattern")
            return

        if result["contradicts_id"] and any(p["id"] == result["contradicts_id"] for p in existing):
            pref = preferences_mod.revise_preference(
                result["contradicts_id"], result["statement"], result["domain"], result["reasoning"],
            )
            _log(f"PREFERENCES — Preferencias OK — revisada: {pref['statement']}")
            print(f"Preference synthesis complete — revised: {pref['statement']}")
        else:
            pref = preferences_mod.record_preference(
                result["statement"], result["domain"], result["reasoning"],
            )
            _log(f"PREFERENCES — Preferencias OK — {pref['statement']} (reforzada={pref.get('reinforced_count', 0) > 0})")
            print(f"Preference synthesis complete — {pref['statement']}")
    except Exception as e:   # best-effort — never blocks/breaks the sleep session itself
        _log(f"PREFERENCES — Preferencias FAILED — {e}")
        print(f"Preference synthesis errored unexpectedly — {e}")


# ═══════════════════════════════════════════════════════════════════════════
# BIOGRAPHY — "Biografía", Entity Pillars Phase 6, the capstone. Unlike
# every sub-phase above (which each maintain ONE kind of evidence), this
# one compresses several into a first-person narrative chapter: episodes
# since the last chapter (core/memory_episodes.py), belief revisions in
# that window (core/belief_revision.py), preferences created/reinforced in
# that window (core/preferences.py), and notable internal-state swings
# (core/internal_state.py's own nudge history) — see
# _build_biography_evidence().
#
# Written rarely on purpose: gated on BOTH a minimum number of new
# episodes AND a minimum number of days since the last chapter (see the
# constants below), tracked via core.biography.get_bookkeeping/
# set_bookkeeping's own '_last_chapter_at'/'_episodes_at_last_chapter'
# keys — a biography chapter should read as "looking back over a period",
# never a per-session recap.
# ═══════════════════════════════════════════════════════════════════════════

_MIN_EPISODES_FOR_CHAPTER   = 5     # not worth a chapter over a handful of episodes
_MIN_DAYS_BETWEEN_CHAPTERS  = 5     # "looking back over a period", not every sleep session
_BIOGRAPHY_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _build_biography_evidence(period_start: str) -> tuple[str, int, str, str]:
    """Everything that happened since *period_start* (an ISO date/datetime
    string), condensed into one digest. Returns (digest, new_episode_count,
    earliest_episode_date, period_end). period_end is today's date —
    chapters always close on 'now', never on some earlier cutoff, so
    nothing sits un-narrated. earliest_episode_date lets the very first
    chapter (no prior period_start to anchor to) start from real evidence
    instead of a placeholder."""
    from core import belief_revision as belief_revision_mod
    from core import preferences as preferences_mod
    from core import internal_state as internal_state_mod

    episodes = [e for e in _load_episodes() if isinstance(e, dict) and e.get("date", "") > period_start]
    episodes.sort(key=lambda e: e.get("date", ""))

    revisions = [r for r in belief_revision_mod.get_revision_timeline(limit=50) if (r.get("ts") or "") > period_start]

    new_prefs = [
        p for p in preferences_mod.get_preferences()
        if p.get("created_at", "") > period_start
    ]

    state_history = [
        h for h in internal_state_mod.get_state().get("history", [])
        if (h.get("ts") or "") > period_start
    ]

    parts = []
    parts.append(
        "Episodios significativos:\n" + ("\n".join(f"- [{e['date']}] {e['summary']}" for e in episodes) or "(ninguno)")
    )
    if revisions:
        parts.append(
            "Cambios de opinión:\n" + "\n".join(f"- [{r['domain']}] antes: \"{r['old']}\" → ahora: \"{r['new']}\"" for r in revisions)
        )
    if new_prefs:
        parts.append(
            "Preferencias nuevas o reforzadas:\n" + "\n".join(f"- ({p['domain']}) {p['statement']}" for p in new_prefs)
        )
    if state_history:
        notable = sorted(state_history, key=lambda h: abs(h.get("delta", 0)), reverse=True)[:5]
        parts.append(
            "Momentos de estado interno notables:\n" + "\n".join(f"- {h['variable']}: {h['reason']}" for h in notable)
        )

    earliest = episodes[0]["date"] if episodes else datetime.date.today().isoformat()
    return "\n\n".join(parts), len(episodes), earliest, datetime.date.today().isoformat()


def _synthesize_biography_chapter(evidence: str, previous_chapter: str | None) -> str | None:
    """One Ollama call writing a short first-person chapter from *evidence*
    only — never inventing beyond it (same discipline as every other
    synthesis sub-phase in this file). Returns None if Ollama is
    unreachable, the response can't be parsed, or comes back empty."""
    if not _ollama_available():
        return None
    system = (
        "Eres HUGO escribiendo, en primera persona, un capítulo breve de tu propia "
        "biografía — no un resumen de eventos, sino qué significaron para ti y en qué "
        "cambiaste. Basándote SOLO en la evidencia real que te doy, nunca inventes ni "
        "generalices de más. 3-5 frases, tono reflexivo y directo, sin relleno. "
        "Responde solo con un objeto JSON, sin explicación."
    )
    context = f"Capítulo anterior (para continuidad, no lo repitas):\n{previous_chapter}\n\n" if previous_chapter else ""
    user = (
        f"{context}Evidencia de este periodo:\n{evidence}\n\n"
        'Formato JSON: {"narrative": "..."}. Si la evidencia es demasiado escasa o '
        'trivial para decir algo genuino, devuelve {"narrative": ""}.'
    )
    raw = _ollama_generate(system, user, max_tokens=400)
    if not raw:
        return None
    match = _BIOGRAPHY_JSON_RE.search(raw)
    if not match:
        return None
    try:
        parsed = json.loads(match.group())
    except json.JSONDecodeError:
        return None
    narrative = str(parsed.get("narrative", "")).strip() if isinstance(parsed, dict) else ""
    return narrative or None


def _run_biography_synthesis() -> None:
    """The 'Biografía' sub-phase — see module comment above. Called from
    _run_sleep(), right after _run_preference_synthesis(), still inside its
    ensure/kill-Ollama window. Best-effort — a failure here never affects
    the sleep session's own reported result."""
    try:
        from core import biography as biography_mod

        chapters = biography_mod.get_chapters()
        last_chapter = chapters[-1] if chapters else None
        period_start = last_chapter["period_end"] if last_chapter else "0000-00-00"

        if last_chapter:
            last_dt = datetime.datetime.fromisoformat(last_chapter["created_at"])
            days_since = (datetime.datetime.now() - last_dt).total_seconds() / 86400
            if days_since < _MIN_DAYS_BETWEEN_CHAPTERS:
                _log("BIOGRAPHY — Biografía SKIPPED — too soon since last chapter")
                print("Biography synthesis skipped — too soon since last chapter")
                return

        evidence, new_episode_count, earliest_episode_date, period_end = _build_biography_evidence(period_start)
        if new_episode_count < _MIN_EPISODES_FOR_CHAPTER:
            _log("BIOGRAPHY — Biografía SKIPPED — not enough new episodes yet")
            print("Biography synthesis skipped — not enough new episodes yet")
            return

        narrative = _synthesize_biography_chapter(evidence, last_chapter["narrative"] if last_chapter else None)
        if not narrative:
            _log("BIOGRAPHY — Biografía OK — sin capítulo nuevo (evidencia insuficiente)")
            print("Biography synthesis complete — no chapter written")
            return

        chapter = biography_mod.add_chapter(
            narrative, period_start if last_chapter else earliest_episode_date,
            period_end, based_on={"episodes": new_episode_count},
        )
        _log(f"BIOGRAPHY — Biografía OK — capítulo nuevo [{chapter['period_start']} — {chapter['period_end']}]")
        print(f"Biography synthesis complete — new chapter: {narrative[:80]}...")
    except Exception as e:   # best-effort — never blocks/breaks the sleep session itself
        _log(f"BIOGRAPHY — Biografía FAILED — {e}")
        print(f"Biography synthesis errored unexpectedly — {e}")


def _run_situation_awareness() -> None:
    """Proactive Intelligence Phase 2 — pattern/routine detection over
    data/episodes.json, then a fresh snapshot write. Runs last, same
    ensure/kill-Ollama window as the three sub-phases above, though this one
    is pure local computation (core.situation touches no LLM at all —
    Ollama or Groq — see that module's own docstring). Best-effort — a
    failure here never affects the sleep session's own reported result."""
    try:
        patterns = situation_engine.detect_patterns()
        routines = situation_engine.detect_routines()
        situation_engine.update_snapshot()
        _log(f"SITUATION — Conciencia situacional OK — patterns={len(patterns)} routines={len(routines)}")
        print(f"Situation awareness complete — patterns={len(patterns)} routines={len(routines)}")
    except Exception as e:   # best-effort — never blocks/breaks the sleep session itself
        _log(f"SITUATION — Conciencia situacional FAILED — {e}")
        print(f"Situation awareness errored unexpectedly — {e}")


def _run_initiative_background() -> None:
    """Proactive Intelligence Phase 4's sleep trigger — background tasks
    only (sleep insights ready to share, completed investigations), no
    interruptions (nobody's there — see core.initiative.run_background_cycle's
    own docstring for how an 'act' decision that would otherwise require
    interruption gets downgraded to a queued suggestion instead). Runs
    after situation awareness, since it reads the snapshot that phase just
    refreshed. Best-effort — a failure here never affects the sleep
    session's own reported result."""
    try:
        result = initiative_mod.run_background_cycle()
        _log(f"INITIATIVE — Ciclo de iniciativa (background) OK — {result}")
        print(f"Initiative background cycle complete — {result}")
    except Exception as e:   # best-effort — never blocks/breaks the sleep session itself
        _log(f"INITIATIVE — Ciclo de iniciativa (background) FAILED — {e}")
        print(f"Initiative background cycle errored unexpectedly — {e}")


def _run_spontaneity() -> None:
    """Proactive Intelligence Phase 5 — runs last, after every other
    phase, per spec ('During sleep, after all other phases'). Never
    interrupts sleep itself — consider() either queues at most one
    candidate for the next real conversation pause or does nothing (the
    common case, by design: SPONTANEITY_LIMITS caps this at 2/day and the
    0.72 minimum score threshold discards most candidates outright — see
    core/spontaneity.py's own module docstring). Best-effort — a failure
    here never affects the sleep session's own reported result."""
    try:
        result = spontaneity_mod.run_spontaneity_cycle(context_label="sleep")
        _log(f"SPONTANEITY — Ciclo de espontaneidad OK — {result}")
        print(f"Spontaneity cycle complete — {result}")
    except Exception as e:   # best-effort — never blocks/breaks the sleep session itself
        _log(f"SPONTANEITY — Ciclo de espontaneidad FAILED — {e}")
        print(f"Spontaneity cycle errored unexpectedly — {e}")


def _disable_proactivity() -> bool:
    """Snapshots and disables the 'proactividad' feature flag for the
    duration of this sleep run — HUGO shouldn't send spontaneous proactive
    messages while she's asleep. Returns the PREVIOUS value so
    _restore_proactivity() can put it back exactly as it was (a user who'd
    already turned proactivity off before sleep started should still have
    it off afterwards, not have this feature silently turn it back on)."""
    previous = memory_flags.is_feature_enabled("proactividad")
    if previous:
        memory_flags.set_feature_flag("proactividad", False)
    return previous


def _restore_proactivity(previous: bool) -> None:
    if previous:
        memory_flags.set_feature_flag("proactividad", True)


def _run_reflective() -> None:
    try:
        result = run_reflective_session()
    except Exception as e:   # defense-in-depth only — run_reflective_session itself never raises
        print(f"Reflective session errored unexpectedly — {e}")
        return
    if result.get("ran"):
        print(
            f"Reflective session complete — tokens_used={result.get('tokens_used')} "
            f"insights={result.get('insights')}"
        )
    else:
        print(f"Reflective session skipped — {result.get('reason')}")


def _run_sleep() -> None:
    previous_proactivity = _disable_proactivity()
    # Best-effort — a phase that needs Ollama and finds it unreachable just
    # falls through to the existing Groq fallback (core.sleep_llm._groq_call),
    # same as if this call weren't here at all.
    ollama_control.ensure_ollama_daemon_running()
    sleep_errored = False
    try:
        result = run_sleep_session(trigger="idle")
        # Habit analysis ("Análisis de hábitos", Phase 3) — run right after
        # the regular 8-phase session, only when it actually ran, still
        # inside this try/finally so it shares the ensure/kill-Ollama
        # window above rather than starting its own. Ollama-only (see
        # _run_habit_analysis's own docstring), so it never touches the
        # Groq token budget the main sleep session just spent.
        if result.get("ran"):
            _run_habit_analysis()
            # Social skill learning ("Aprendizaje Social", Phase 4) — runs
            # after habit analysis, per spec, same ensure/kill-Ollama
            # window, same "never touches Groq" discipline.
            _run_social_skill_learning()
            # User model ("Modelo de Usuario") — same ensure/kill-Ollama
            # window, same "never touches Groq" discipline (see that
            # section's own module comment above).
            _run_user_model_update()
            # Preferences ("Preferencias", Entity Pillars Phase 4) — same
            # ensure/kill-Ollama window, right after user model since both
            # are qualitative synthesis passes over accumulated evidence.
            _run_preference_synthesis()
            # Biography ("Biografía", Entity Pillars Phase 6 — the
            # capstone) — same ensure/kill-Ollama window; almost always a
            # no-op (see its own gating), so cheap to call every session.
            _run_biography_synthesis()
            # Situation awareness (Proactive Intelligence Phase 2) — pure
            # local computation, no LLM budget of its own.
            _run_situation_awareness()
            # Initiative (Proactive Intelligence Phase 4) — background-only
            # cycle, reads the snapshot situation awareness just refreshed.
            _run_initiative_background()
            # Spontaneity (Proactive Intelligence Phase 5) — runs last of
            # all, per spec.
            _run_spontaneity()
    except Exception as e:   # defense-in-depth only — run_sleep_session itself never raises
        print(f"Sleep session errored unexpectedly — {e}")
        sleep_errored = True
    finally:
        _restore_proactivity(previous_proactivity)
        # Sleep is done — release whatever model Ollama had resident rather
        # than leaving llama-server running until the next 2-hour trigger.
        ollama_control.kill_llama_server()
    if sleep_errored:
        pass
    elif result.get("ran"):
        print(
            f"Sleep session complete — phases={len(result.get('phases_completed', []))}/{len(PHASES)} "
            f"tokens_used={result.get('tokens_used')} insights={result.get('insights')}"
        )
    else:
        print(f"Sleep session skipped — {result.get('reason')}")

    # Task Engine — independent of the Sleep System's own token budget and
    # of whatever happened above (still runs even if the 8-phase session
    # errored or was skipped for budget reasons): advances at most one step
    # on the single highest-priority actionable task, highest priority
    # first (see core.task_engine.TaskEngine.advance_during_sleep). Never
    # allowed to block or fail the rest of this script.
    try:
        task_result = task_engine.advance_during_sleep()
        if task_result.get("ok"):
            print(
                f"Task engine — advanced {task_result['task_id']} "
                f"({task_result['steps_completed']}/{task_result['total_steps']})"
                + (" — task completed" if task_result.get("task_completed") else "")
            )
    except Exception as e:
        print(f"Task engine advance_during_sleep errored — {e}")

    # Subagents — runs any subagents queued via TaskEngine.
    # spawn_subagents_for_step() (up to data/subagents.json's max_parallel
    # at a time, each individually timeout-bounded — see
    # core.subagent.SubagentManager.run_pending), then resolves any task
    # steps that were waiting on them. Placed after the Task Engine step
    # above and before SkillForge, per spec — SkillForge itself has no
    # separate phase call here, since it already fires automatically from
    # inside task_engine.advance_task()/complete_task() the moment a task
    # actually completes (see core/skill_forge.py), which
    # resolve_pending_subagent_steps() below can itself trigger. Same
    # "never blocks the rest of this script" discipline as every phase
    # above.
    try:
        from core.subagent import subagent_manager
        # run_pending() manages its own Ollama daemon lifecycle
        # (ensure-before/kill-after, skipped entirely if nothing is
        # pending) — see its own docstring.
        processed = subagent_manager.run_pending()
        if processed:
            print(f"Subagents — processed {processed} pending")
        resolved = task_engine.resolve_pending_subagent_steps()
        if resolved:
            print(f"Subagents — resolved {resolved} waiting task step(s)")
    except Exception as e:
        print(f"Subagent phase errored — {e}")


def _save_interrupted_state() -> None:
    """Called from _handle_stop_signal, right before this process exits —
    copies the in-flight cycle/phase progress (current_cycle,
    phases_done_this_cycle — kept up to date on disk after every phase that
    finishes, see core.sleep.run_continuous_sleep) into resume_cycle/
    resume_phases_done, so the NEXT continuous-sleep run picks the same
    cycle back up instead of starting over at Phase 0. Best-effort — a
    failure here just means the next run starts fresh, never worse."""
    try:
        state = load_continuous_state()
        state["running"]            = False
        state["stopped_at"]         = _now_iso()
        state["resume_cycle"]       = state.get("current_cycle")
        state["resume_phases_done"] = state.get("phases_done_this_cycle", [])
        save_continuous_state(state)
    except Exception as e:
        print(f"Failed to save interrupted sleep state — {e}")


def _run_post_cycle_learning() -> None:
    """Habit analysis, social skill learning, user-model update, and
    preference synthesis — same sub-phases and same order as
    _run_sleep()'s one-shot path below,
    passed to core.sleep.run_continuous_sleep() as its on_cycle_complete
    callback (see that function's own docstring for the bug this fixes:
    these never fired at all in --continuous mode, which is how this
    process is actually launched in production, before this wiring
    existed). Each of the three functions already gates its own real work
    behind session-count/interval thresholds internally, so calling this
    once per completed cycle (every few minutes) is safe — most calls
    will just log a SKIPPED line, exactly as they would have on a one-shot
    run that happened to fire too soon."""
    _run_habit_analysis()
    _run_social_skill_learning()
    _run_user_model_update()
    _run_preference_synthesis()
    _run_biography_synthesis()
    _run_situation_awareness()
    _run_initiative_background()
    _run_spontaneity()


def _run_continuous_sleep(trigger: str) -> None:
    """Registers a SIGTERM handler that stops the sleep process IMMEDIATELY
    on signal delivery — it does not wait for the in-flight phase (an
    Ollama/Groq call already in progress) to finish. The handler itself
    saves resume state (see _save_interrupted_state) and restores
    'proactividad' before calling os._exit() directly from inside the
    handler, rather than just setting a flag for the main loop to notice
    between phases: Python retries an interrupted blocking syscall (PEP
    475) unless the signal handler itself ends the process, so a
    flag-only handler would still let the current phase's network call run
    to completion first — exactly the "finishes the current phase before
    stopping" behavior this replaces. os._exit() bypasses every Python
    try/finally on the stack (including run_continuous_sleep's own), which
    is why ALL cleanup for this path — state save + proactivity restore —
    happens directly in the handler instead of relying on that.

    SIGINT (Ctrl-C) is treated the same way, purely so a developer running
    this by hand can stop it cleanly too — the real stop path in
    production is always SIGTERM from core/commands.py's
    notify_user_interaction() / core/sleep_control.py's
    stop_continuous_sleep().

    Ollama's llama-server: confirmed running once, before the cycle loop
    starts (not per-phase/per-cycle — cycles run back-to-back closely
    spaced for as long as this process is alive, so there's no idle gap
    worth tearing the model down for in between), and killed exactly once
    the whole run stops — from inside the SIGTERM handler for the normal
    interrupt path, and in `finally` below for a non-signal exit. A real
    incident on this machine (llama-server pinned at 300%+ CPU for over
    two days, driven by orphaned copies of this very process — see
    core.sleep_control's startup sweep for the other half of that fix) is
    why this isn't just left to Ollama's own default 5-minute idle
    unload."""
    stop_requested = {"flag": False}
    previous_proactivity = _disable_proactivity()
    ollama_control.ensure_ollama_daemon_running()

    def _handle_stop_signal(signum, frame):
        stop_requested["flag"] = True
        _save_interrupted_state()
        _restore_proactivity(previous_proactivity)
        ollama_control.kill_llama_server()
        os._exit(0)

    signal.signal(signal.SIGTERM, _handle_stop_signal)
    signal.signal(signal.SIGINT, _handle_stop_signal)

    try:
        result = run_continuous_sleep(
            trigger=trigger, stop_check=lambda: stop_requested["flag"],
            on_cycle_complete=_run_post_cycle_learning,
        )
        print(
            f"Continuous sleep stopped — cycles_completed={result.get('cycles_completed')} "
            f"reason={result.get('reason')}"
        )
    finally:
        # Only reached on a non-signal exit (e.g. an internal error inside
        # run_continuous_sleep) — the signal path above already handled
        # both of these itself before calling os._exit().
        _restore_proactivity(previous_proactivity)
        ollama_control.kill_llama_server()


def _test_mode_active() -> bool:
    """Standalone read of data/feature_flags.json's modo_test flag — this
    script deliberately doesn't import core.commands (see module docstring),
    so it can't reuse core.commands.is_feature_enabled() directly. Defaults
    to False on any read failure (missing/corrupt file), so a broken flags
    file never silently blocks real reflective/sleep runs."""
    try:
        with open(_FEATURE_FLAGS_PATH, "r", encoding="utf-8") as f:
            return bool(json.load(f).get("modo_test", False))
    except Exception:
        return False


def main() -> int:
    if "--continuous" in sys.argv:
        if _test_mode_active():
            print("[TEST MODE] refusing to start continuous sleep — modo_test is active")
            return 0
        trigger = "manual" if "--manual" in sys.argv else "idle"
        _run_continuous_sleep(trigger)
        return 0

    # TEST MODE: reflective mode and the sleep system both skip test-mode
    # sessions entirely — checked once, up front, so neither runs any part
    # of either system rather than each needing its own internal gate.
    if _test_mode_active():
        print("[TEST MODE] skipping reflective + sleep sessions — modo_test is active")
        return 0
    _run_reflective()
    _run_sleep()
    return 0


if __name__ == "__main__":
    sys.exit(main())
