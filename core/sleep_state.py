"""Sleep System — on-disk budget/continuous-state persistence, phase
definitions, and live in-process status. Shared foundation imported by
core/sleep_llm.py, core/sleep_summary.py, core/sleep_insights_store.py,
core/sleep_phases_memory.py, core/sleep_phases_insight.py, and core/sleep.py
itself. Dependency-light (json/os/re/datetime/threading only) so
scripts/reflective_mode.py can run it standalone via launchd."""
import datetime
import json
import logging
import os
import re
import threading

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _p(rel: str) -> str:
    return os.path.join(_REPO_ROOT, rel)

load_dotenv(_p(".env"))

MEMORY_SHARED_PATH       = _p("data/memory_shared.json")
MEMORY_HUGO_PATH         = _p("data/memory_hugo.json")
EPISODES_PATH            = _p("data/episodes.json")
# Same path core/reflective.py's own CONNECTIONS_PATH points at — duplicated
# here rather than imported (see module docstring: dependency isolation).
CONNECTIONS_PATH         = _p("data/mind_map_connections.json")
MEMORY_INSTRUCTIONS_PATH = _p("data/memory_instructions.json")
BUDGET_PATH              = _p("data/sleep_budget.json")
INSIGHTS_PATH            = _p("data/sleep_insights.json")
ERRORS_LOG_PATH          = _p("logs/errors.log")
LOG_PATH                 = _p("logs/sleep.log")

# Cheapest/fastest model available, per spec — every phase (and the
# priority-assessment call) uses this, never a heavier reasoning tier.
MODEL = "llama-3.1-8b-instant"

AUTO_DAILY_BUDGET = 3700        # idle/automatic trigger only — see can_run()
MANUAL_SESSION_BUDGET = 5000    # manual ("Iniciar Sueño") trigger only — its own separate pool, reset every session

# Continuous-mode-only Groq fallback spend cap (core.sleep_llm) — defined here
# rather than there to avoid a sleep_state<->sleep_llm import cycle, since
# _default_continuous_state() below needs it too.
GROQ_FALLBACK_BUDGET = MANUAL_SESSION_BUDGET
IDLE_TRIGGER_SECONDS = 20 * 60   # 20 min — the auto-trigger threshold core/commands.py's idle loop checks against

# Variable-lifespan facts — reimplemented here rather than imported from
# core.commands (see module docstring: dependency isolation). Must stay in
# sync with core.commands's own copy of these same values.
#   permanent — identity, skills, projects, preferences, relationships,
#               achievements. Never expires.
#   weekly    — ongoing situations, current project status, recent decisions.
#   daily     — today's plans, current mood/energy, what happened today.
#   hourly    — current state ('acaba de desayunar', 'tiene sueño ahora').
_LIFESPAN_VALUES = {"permanent", "weekly", "daily", "hourly"}
_LIFESPAN_EXPIRY_HOURS = {"hourly": 3, "daily": 48, "weekly": 240}   # weekly = 10 days

def _is_fact_expired(fact: dict) -> bool:
    """True if *fact*'s lifespan ran out, anchored on 'created_at' (never
    'added' — reinforcement refreshes 'added' but must not reset the expiry
    clock). 'permanent'/unrecognized lifespan never expires."""
    lifespan = fact.get("lifespan", "permanent")
    if lifespan not in _LIFESPAN_EXPIRY_HOURS:
        return False
    created_at = fact.get("created_at") or fact.get("added")
    if not created_at:
        return False
    try:
        created_dt = datetime.datetime.fromisoformat(created_at)
    except ValueError:
        return False
    age_hours = (datetime.datetime.now() - created_dt).total_seconds() / 3600
    return age_hours > _LIFESPAN_EXPIRY_HOURS[lifespan]

# Not in the spec explicitly, but a session-to-session minimum interval is
# a reasonable, self-imposed safety rail: the 7 phases' budgets already sum
# to exactly AUTO_DAILY_BUDGET, so at most one or two full sessions fit in
# a day anyway — this just stops a pathological rapid-fire retrigger (e.g.
# idle detection flapping) from burning the whole daily budget on several
# back-to-back low-value partial sessions instead of one real one. Doesn't
# apply to a manual trigger's own explicit intent — see can_run().
MIN_SECONDS_BETWEEN_AUTO_SESSIONS = 4 * 60 * 60

PRIORITY_ASSESSMENT_MAX_TOKENS = 200

# Phase 0 — 🧠 Mantenimiento de Memoria. Runs BEFORE all other phases, on
# every session (auto or manual), unconditionally — never part of
# _assess_priority()'s reordering (see run_sleep_session). Reviews
# memory_shared.json / memory_hugo.json for lifespan-expired facts,
# near-duplicates to merge, facts mixing two distinct things to split,
# misclassified category/lifespan, and vague repeated facts that deserve
# temporal generalization — see _phase_memory_maintenance.
PHASE_0_MEMORY_MAINTENANCE = {"num": 0, "key": "memory_maintenance", "name": "🧠 Mantenimiento de Memoria", "budget": 300}

# The 8 sleep phases, in their DEFAULT order — actual execution order for a
# given session is decided by _assess_priority() instead (see
# run_sleep_session). Budgets sum to exactly AUTO_DAILY_BUDGET
# (300 for Phase 0 + 400+500+400+400+300+300+400+500 + 200 for priority
# assessment = 3700) — a manual session's MANUAL_SESSION_BUDGET (5000) has
# room to spare on top of that, which is what lets it always complete Phase
# 0 and all 8 phases in full.
#
# Phase 3 (🧪 Incubación) sits between Pattern Discovery and Idea Generator
# per spec — it advances any investigation started via core.intent's
# start_investigation (core/investigations.py, "investiga X" / "quiero
# saber sobre X" / "analiza X en profundidad"), one reasoning cycle per
# active investigation per sleep session. See core/sleep_phases_incubation.py.
PHASES = [
    PHASE_0_MEMORY_MAINTENANCE,
    {"num": 1, "key": "memory_cleanup",    "name": "🧹 Limpieza de Memoria",        "budget": 400},
    {"num": 2, "key": "pattern_discovery", "name": "🔍 Descubrimiento de Patrones", "budget": 500},
    {"num": 3, "key": "incubation",        "name": "🧪 Incubación",                 "budget": 400},
    {"num": 4, "key": "idea_generator",    "name": "💡 Generador de Ideas",         "budget": 400},
    {"num": 5, "key": "diagnostics",       "name": "⚙️ Diagnóstico",                "budget": 300},
    {"num": 6, "key": "pending_questions", "name": "🤔 Preguntas Pendientes",       "budget": 300},
    {"num": 7, "key": "self_critique",     "name": "🪞 Autocrítica",                "budget": 400},
    {"num": 8, "key": "curiosity",         "name": "🌱 Curiosidad",                 "budget": 500},
]
_PHASES_BY_NUM = {p["num"]: p for p in PHASES}

# Phases 1-8 only — used by _assess_priority()'s reordering. Phase 0 is
# deliberately excluded: it always runs first, unconditionally, and is never
# subject to LLM reprioritization (see run_sleep_session).
_PRIORITIZABLE_PHASES = [p for p in PHASES if p["num"] != 0]

MAX_SELF_CRITIQUE_NOTES = 8   # cap on accumulated Phase 6 notes in memory_instructions.json — never grows unbounded

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
    try:
        os.makedirs(os.path.dirname(LOG_PATH) or ".", exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{_now_iso()} {line}\n")
    except Exception:
        logger.warning("Failed to write logs/sleep.log", exc_info=True)

def _fact_similarity(a: str, b: str) -> float:
    wa = set(re.findall(r"\w+", a.lower()))
    wb = set(re.findall(r"\w+", b.lower()))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)

def _default_budget() -> dict:
    return {
        "auto_budget": {
            "used":       0,
            "limit":      AUTO_DAILY_BUDGET,
            "reset_date": _today(),
        },
        "manual_budget": {
            "used":             0,
            "limit":            MANUAL_SESSION_BUDGET,
            "reset_per_session": True,
        },
        "last_session_at":         None,
        "last_session_tokens":     0,
        "last_session_phases":     [],     # phase names completed, most recent session
        "last_session_insights":   0,
        "last_session_trigger":    None,   # 'idle' | 'manual'
        "continuous":              _default_continuous_state(),
    }

def _default_continuous_state() -> dict:
    """See run_continuous_sleep(). Persisted inside data/sleep_budget.json
    (under the 'continuous' key) rather than a separate file, since it's
    conceptually the same budget/session-tracking concern — just the
    continuous-cycle mechanism instead of the old one-shot session. This is
    the only channel core/server.py (running in jarvis.py's process) has
    into the standalone sleep subprocess's live progress — see
    scripts/reflective_mode.py's --continuous mode, which is the sole
    writer of this state while a continuous run is active."""
    return {
        "running":              False,
        "trigger":              None,   # 'idle' | 'manual'
        "current_cycle":        0,
        "current_phase_num":    0,
        "total_cycles_completed": 0,
        "current_phase":        "",
        "started_at":           None,
        "stopped_at":           None,
        "last_wake":            None,
        "last_heartbeat":       None,
        "stop_reason":          None,   # 'interaction' | 'manual_stop' | None
        "groq_fallback_used":   0,
        "groq_fallback_limit":  GROQ_FALLBACK_BUDGET,
        # Cumulative totals for THIS run only (reset to 0 by the fresh
        # _default_continuous_state() every run_continuous_sleep() call,
        # same convention as total_cycles_completed above) — summed cycle
        # by cycle from each cycle's own local deltas, which otherwise only
        # ever existed transiently in the log line (see run_continuous_sleep).
        # Backs GET /api/sleep_summary's "ÚLTIMO SUEÑO" stats.
        "total_deleted":            0,
        "total_merged":             0,
        "total_promoted":           0,
        "total_insights_generated": 0,
        "total_mind_map_updates":   0,
        # Immediate-interrupt resume support (see scripts/reflective_mode.py's
        # SIGTERM handler and run_continuous_sleep's own docstring below).
        # 'phases_done_this_cycle' is updated + saved to disk after every
        # phase that finishes successfully in the CURRENT cycle, so an
        # interrupt landing mid-phase always has an accurate on-disk record
        # of what was actually completed before it. If the process is killed
        # via SIGTERM, the handler copies 'current_cycle'/'phases_done_this_cycle'
        # into 'resume_cycle'/'resume_phases_done' right before exiting — the
        # NEXT run_continuous_sleep() call reads those (once) to pick up that
        # same cycle where it left off instead of starting over at Phase 0.
        "phases_done_this_cycle": [],
        "resume_cycle":           None,
        "resume_phases_done":     [],
    }

def load_budget() -> dict:
    budget = _load_json(BUDGET_PATH, None)
    if not isinstance(budget, dict):
        budget = _default_budget()
    defaults = _default_budget()
    for key, value in defaults.items():
        budget.setdefault(key, value)
    if not isinstance(budget.get("auto_budget"), dict):
        budget["auto_budget"] = dict(defaults["auto_budget"])
    if not isinstance(budget.get("manual_budget"), dict):
        budget["manual_budget"] = dict(defaults["manual_budget"])
    if not isinstance(budget.get("continuous"), dict):
        budget["continuous"] = dict(defaults["continuous"])
    for key, value in defaults["auto_budget"].items():
        budget["auto_budget"].setdefault(key, value)
    for key, value in defaults["manual_budget"].items():
        budget["manual_budget"].setdefault(key, value)
    for key, value in defaults["continuous"].items():
        budget["continuous"].setdefault(key, value)

    if budget["auto_budget"].get("reset_date") != _today():
        budget["auto_budget"]["reset_date"] = _today()
        budget["auto_budget"]["used"] = 0
    # Always the current constants, never a stale stored value
    budget["auto_budget"]["limit"]   = AUTO_DAILY_BUDGET
    budget["manual_budget"]["limit"] = MANUAL_SESSION_BUDGET
    return budget

def load_continuous_state() -> dict:
    return load_budget()["continuous"]

def save_continuous_state(state: dict) -> None:
    budget = load_budget()
    budget["continuous"] = state
    save_budget(budget)

def get_continuous_status() -> dict:
    """Backs GET /api/sleep/status's 'continuous' field — see
    core/server.py. Purely a file read; whether the subprocess that WRITES
    this is actually still alive is for the caller to judge (core/commands.py
    holds the real Popen handle) — see that module's
    is_continuous_sleep_running()."""
    return load_continuous_state()

def save_budget(budget: dict) -> None:
    _save_json(BUDGET_PATH, budget)

def get_status() -> dict:
    """Static snapshot — budget + last-session summary. Backs the Estado
    tab and the Ajustes button's initial render. Live in-process running
    state (current phase, while one is actually in flight) is separate —
    see get_live_status()."""
    return load_budget()

def _seconds_since_last_session(budget: dict) -> float | None:
    last = budget.get("last_session_at")
    if not last:
        return None
    try:
        then = datetime.datetime.fromisoformat(last)
    except ValueError:
        return None
    return (datetime.datetime.now() - then).total_seconds()

def can_run(budget: dict | None = None, trigger: str = "idle") -> tuple[bool, str]:
    """Whether a sleep session is allowed right now.

    Manual sleep bypasses the daily auto_budget entirely and always runs in
    full — it's gated only by its own separate manual_budget (reset fresh
    at the start of every manual session, see run_sleep_session) and by
    "not already running". The minimum-interval rail and the daily
    auto_budget cap only apply to the idle/automatic trigger — a manual
    click is an explicit ask, not an autonomous retrigger to guard
    against."""
    budget = budget or load_budget()
    if get_live_status()["running"]:
        return False, "a sleep session is already running"
    if trigger == "manual":
        return True, ""
    if budget["auto_budget"]["used"] >= budget["auto_budget"]["limit"]:
        return False, "daily token budget exhausted"
    elapsed = _seconds_since_last_session(budget)
    if elapsed is not None and elapsed < MIN_SECONDS_BETWEEN_AUTO_SESSIONS:
        return False, f"last session {elapsed:.0f}s ago (< {MIN_SECONDS_BETWEEN_AUTO_SESSIONS}s cap)"
    return True, ""

_live_status_lock = threading.Lock()
_live_status: dict = {
    "running":      False,
    "phase_num":    0,
    "phase_name":   "",
    "total_phases": len(PHASES),
    "trigger":      None,
}

def get_live_status() -> dict:
    with _live_status_lock:
        return dict(_live_status)

def _set_live_status(**kwargs) -> None:
    with _live_status_lock:
        _live_status.update(kwargs)
