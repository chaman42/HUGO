"""HUGO's Sleep System — an 8-phase autonomous maintenance routine that runs
during inactivity (20+ minutes idle) or on manual request ("Iniciar Sueño"
in Ajustes). Deliberately dependency-light (json/os/re/datetime/logging +
the groq SDK only — same discipline as core/reflective.py) so
scripts/reflective_mode.py can run it standalone via launchd, without
jarvis.py or its audio/TTS stack loaded.

Distinct from core/reflective.py's own lightweight, continuous background
insight-gathering: this is a heavier, SESSION-based routine with its own
token budget (data/sleep_budget.json), its own insights file
(data/sleep_insights.json), its own log (logs/sleep.log), and 8 named
phases — Phase 0 (memory maintenance) always runs first, unconditionally;
phases 1-7 then run in an LLM-decided priority order — the two systems
share no state and run independently.

The token budget itself is actually two separate, non-shared pools within
that same file — auto_budget (idle/automatic trigger, daily-reset,
AUTO_DAILY_BUDGET) and manual_budget (the button, reset every session,
MANUAL_SESSION_BUDGET) — see can_run() and run_sleep_session(). Manual
sleep always runs Phase 0 and all 7 phases in full and never touches or is
gated by the daily auto budget.

Entry points:
  - run_sleep_session(trigger) — the whole thing, called by:
      * core/commands.py's own idle-trigger thread (trigger='idle')
      * core/server.py's POST /api/sleep/start (trigger='manual')
      * scripts/reflective_mode.py's launchd job (trigger='idle', "while
        the app is closed" — see that script's own comment)
  - get_status() / get_live_status() — for the Estado tab and the Ajustes
    button's live polling.
  - get_unused_question()/mark_question_used(),
    get_unused_curiosity()/mark_curiosity_used() — for core/commands.py's
    system-prompt injection (Phase 5/7's whole point: HUGO surfaces these
    naturally in conversation, not just files nobody reads).
"""
import re
import time

from core.sleep_state import (
    PHASES, _PHASES_BY_NUM, _PRIORITIZABLE_PHASES, PRIORITY_ASSESSMENT_MAX_TOKENS,
    IDLE_TRIGGER_SECONDS, PHASE_0_MEMORY_MAINTENANCE, load_budget, save_budget,
    get_status, get_live_status, _set_live_status, can_run, load_continuous_state,
    save_continuous_state, get_continuous_status, _default_continuous_state,
    _now_iso, _log,
)
from core.sleep_llm import _groq_call
import core.sleep_llm as sleep_llm
from core.sleep_summary import _build_state_summary, _count_recent_errors
from core.sleep_insights_store import (
    get_sleep_summary, get_sleep_insights_summary, get_unused_question,
    mark_question_used, get_unused_curiosity, mark_curiosity_used,
)
import core.sleep_insights_store as sleep_insights_store
from core.sleep_phases_memory import _phase_memory_maintenance, _phase_memory_cleanup
from core.sleep_phases_insight import (
    _phase_pattern_discovery, _phase_idea_generator, _phase_diagnostics,
    _phase_pending_questions, _phase_self_critique,
)
from core.sleep_phases_incubation import _phase_incubation
from core.sleep_curiosity_search import _phase_curiosity_search, _phase_curiosity_deep
from core import linguistic_fingerprint

import logging
logger = logging.getLogger(__name__)

_PRIORITY_SYSTEM = (
    "Eres HUGO evaluando internamente qué mantenimiento necesitas. "
    "Respondes solo con una lista de números separados por comas, sin explicación."
)

def _assess_priority(remaining_budget: int) -> tuple[list[int], int]:
    """Orders phases 1-8 only — Phase 0 (memory maintenance) always runs
    first, separately, before this is even called (see run_sleep_session)."""
    max_tokens = min(PRIORITY_ASSESSMENT_MAX_TOKENS, remaining_budget)
    if max_tokens <= 0:
        return [p["num"] for p in _PRIORITIZABLE_PHASES], 0

    state          = _build_state_summary(max_facts=6, max_episodes=4)
    recent_errors  = _count_recent_errors()
    phase_list     = "\n".join(f"{p['num']}. {p['name']}" for p in _PRIORITIZABLE_PHASES)
    user = (
        f"Estado actual:\n{state}\n\nErrores recientes en logs: {recent_errors}\n\n"
        f"Fases de mantenimiento disponibles:\n{phase_list}\n\n"
        "Dado el estado actual de memoria, conversaciones recientes y logs, "
        "¿qué fases de mantenimiento son más urgentes? Ordénalas de más a "
        "menos prioritaria. Responde solo con lista ordenada de números de fase."
    )
    text, tokens = _groq_call(_PRIORITY_SYSTEM, user, max_tokens)
    if not text:
        return [p["num"] for p in _PRIORITIZABLE_PHASES], tokens

    nums = [int(n) for n in re.findall(r"\b([1-8])\b", text)]
    ordered = []
    for n in nums:
        if n in _PHASES_BY_NUM and n not in ordered:
            ordered.append(n)
    for p in _PRIORITIZABLE_PHASES:   # append any phase the model's answer missed, in default order
        if p["num"] not in ordered:
            ordered.append(p["num"])
    return ordered, tokens

_PHASE_FUNCS = {
    "memory_maintenance": _phase_memory_maintenance,
    "memory_cleanup":    _phase_memory_cleanup,
    "pattern_discovery": _phase_pattern_discovery,
    "incubation":        _phase_incubation,
    "idea_generator":    _phase_idea_generator,
    "diagnostics":       _phase_diagnostics,
    "pending_questions": _phase_pending_questions,
    "self_critique":     _phase_self_critique,
    "curiosity":         _phase_curiosity_search,
}

def run_sleep_session(trigger: str = "idle", api_key: str | None = None) -> dict:
    """Runs one sleep session, if allowed (see can_run()) — Phase 0 (memory
    maintenance) always runs first, unconditionally, followed by a full pass
    through phases 1-7 in LLM-decided priority order, or a partial pass that
    stops gracefully the moment its budget runs out before the next phase.
    Always returns a result dict, never raises.

    trigger is 'idle' or 'manual' — recorded in the budget file for the
    Estado tab / Ajustes button to display. Each draws from its own
    separate pool (see can_run() / module docstring): 'idle' spends from
    auto_budget (shared, daily-reset, capped at AUTO_DAILY_BUDGET) and
    respects the minimum-interval rail; 'manual' spends from manual_budget,
    which is reset to zero right here at the start of every manual session
    and is large enough (MANUAL_SESSION_BUDGET) that a manual run always
    completes Phase 0 and all 7 phases in full, per spec.
    """
    budget = load_budget()
    allowed, reason = can_run(budget, trigger=trigger)
    if not allowed:
        _log(f"SKIP — {reason}")
        return {"ran": False, "reason": reason}

    bucket = "manual_budget" if trigger == "manual" else "auto_budget"
    if trigger == "manual":
        budget["manual_budget"]["used"] = 0   # fresh pool for this session — see docstring

    # Reserve BEFORE any tokens are spent (same race-guard reasoning as
    # core.reflective's own _reserve()) — stamps last_session_at so a
    # concurrent trigger (e.g. the idle loop firing right as the button is
    # clicked) sees the reservation via can_run() and backs off instead of
    # both spending a session's worth of budget. Also flips the live
    # status to 'running' so get_live_status() reflects reality
    # immediately, not just after the first phase starts.
    budget["last_session_at"] = _now_iso()
    save_budget(budget)
    _set_live_status(running=True, phase_num=0, phase_name="Evaluando prioridad…",
                      total_phases=len(PHASES), trigger=trigger)

    session_limit     = budget[bucket]["limit"]
    total_tokens      = 0
    phases_completed  = []
    total_insights    = 0
    try:
        # Phase 0 always runs first, unconditionally — never subject to
        # _assess_priority()'s reordering (see PHASE_0_MEMORY_MAINTENANCE).
        remaining = session_limit - budget[bucket]["used"]
        if remaining > 0:
            phase0 = PHASE_0_MEMORY_MAINTENANCE
            _set_live_status(phase_num=1, phase_name=phase0["name"])
            phase0_budget = min(phase0["budget"], remaining)
            try:
                tokens, insights, summary = _phase_memory_maintenance(phase0_budget)
                total_tokens   += tokens
                total_insights += insights
                phases_completed.append(phase0["name"])
                _log(f"PHASE 0 ({phase0['name']}) OK — tokens={tokens} insights={insights} — {summary}")
            except Exception as e:
                logger.warning("Sleep phase %s failed", phase0["key"], exc_info=True)
                _log(f"PHASE 0 ({phase0['name']}) FAILED — {e}")
        else:
            _log(f"STOP — {bucket} exhausted before Phase 0")

        # Phase 5 — 'Actualización de huella lingüística'. Not one of the
        # budgeted/prioritizable PHASES: pure local text statistics over
        # this session's own history (core.linguistic_fingerprint), zero
        # Groq tokens, so it always runs, every session, regardless of
        # remaining budget — same "always runs" treatment as Phase 0, just
        # free instead of unconditional-but-budgeted.
        try:
            folded = linguistic_fingerprint.update_from_session()
            _log(f"ACTUALIZACIÓN DE HUELLA LINGÜÍSTICA — {folded} turnos incorporados")
        except Exception as e:
            logger.warning("Linguistic fingerprint sub-phase failed", exc_info=True)
            _log(f"ACTUALIZACIÓN DE HUELLA LINGÜÍSTICA FAILED — {e}")

        remaining = session_limit - (budget[bucket]["used"] + total_tokens)
        order, priority_tokens = _assess_priority(remaining)
        total_tokens += priority_tokens
        _log(f"PRIORITY — order={order} tokens={priority_tokens}")

        for num in order:
            remaining = session_limit - (budget[bucket]["used"] + total_tokens)
            if remaining <= 0:
                _log(f"STOP — {bucket} exhausted mid-session")
                break

            phase = _PHASES_BY_NUM[num]
            _set_live_status(phase_num=len(phases_completed) + 1, phase_name=phase["name"])

            phase_budget = min(phase["budget"], remaining)
            try:
                tokens, insights, summary = _PHASE_FUNCS[phase["key"]](phase_budget)
            except Exception as e:
                logger.warning("Sleep phase %s failed", phase["key"], exc_info=True)
                _log(f"PHASE {num} ({phase['name']}) FAILED — {e}")
                continue

            total_tokens     += tokens
            total_insights   += insights
            phases_completed.append(phase["name"])
            _log(f"PHASE {num} ({phase['name']}) OK — tokens={tokens} insights={insights} — {summary}")

        budget = load_budget()
        if trigger == "manual":
            budget["manual_budget"]["used"] = total_tokens   # this session's spend IS the whole pool — see docstring
        else:
            budget["auto_budget"]["used"] += total_tokens
        budget["last_session_tokens"]   = total_tokens
        budget["last_session_phases"]   = phases_completed
        budget["last_session_insights"] = total_insights
        budget["last_session_trigger"]  = trigger
        save_budget(budget)

        _log(f"SESSION COMPLETE — phases={len(phases_completed)}/{len(PHASES)} tokens={total_tokens} insights={total_insights}")
        return {
            "ran": True, "phases_completed": phases_completed,
            "tokens_used": total_tokens, "insights": total_insights,
        }
    except Exception as e:
        _log(f"SESSION FAILED — {e}")
        logger.warning("Sleep session failed (non-critical)", exc_info=True)
        return {"ran": False, "reason": str(e)}
    finally:
        _set_live_status(running=False, phase_num=0, phase_name="", trigger=None)

_CYCLE_SUMMARY_RE = re.compile(
    r"deleted=(\d+).*?merged=(\d+).*?split=(\d+).*?recategorized=(\d+).*?reworded=(\d+).*?"
    r"promoted=(\d+).*?mind_map_updates=(\d+)"
)

def run_continuous_sleep(trigger: str, stop_check, on_cycle_complete=None) -> dict:
    """Runs cycle after cycle until stop_check() returns True or an
    unhandled error occurs. Always returns a small summary dict; never
    raises. trigger is 'idle' or 'manual', recorded in the continuous state
    (data/sleep_budget.json's 'continuous' key) purely for display —
    see _default_continuous_state().

    on_cycle_complete: optional zero-arg callback fired once per FULLY
    completed cycle (all PHASES done, same condition that increments
    total_cycles_completed below) — never on a partial/interrupted one.
    Bug fix (2026-08-10): scripts/reflective_mode.py's habit-analysis/
    social-skill-learning/user-model-update sub-phases were only ever
    wired into the older one-shot _run_sleep() path
    (run_sleep_session()), which main() only calls when NOT run with
    --continuous. In practice this process is always launched with
    --continuous (see scripts/reflective_mode.py's own entry point), so
    those three sub-phases had never fired since the single manual test
    that built them (confirmed: zero 'HABITS'/'SOCIAL'/'USER MODEL' log
    lines anywhere in logs/sleep.log except that one). This callback lets
    reflective_mode.py run them here too, once per completed cycle — same
    cadence as the one-shot path ran them once per session. Best-effort:
    any exception from the callback is caught and logged, never allowed
    to break the continuous loop itself.

    stop_check: a zero-arg callable returning True once this run should
    stop — kept as a defensive fallback, but in practice a real stop now
    happens via SIGTERM instead (see scripts/reflective_mode.py's
    --continuous handler): that handler reads/writes the continuous state
    directly and calls os._exit() the instant the signal arrives, without
    ever returning here to let this loop notice stop_check() — see its own
    docstring for why an immediate exit, rather than finishing whatever
    phase is in flight, needs to happen entirely inside the handler.

    Resume: if the PREVIOUS run was interrupted mid-cycle, that handler
    left 'resume_cycle'/'resume_phases_done' set in the continuous state
    (data/sleep_budget.json) — read once, here, before _default_continuous_state()
    would otherwise wipe them, so THIS run's first cycle picks up at
    resume_cycle and skips whatever phases resume_phases_done already
    lists, instead of starting over at Phase 0."""
    sleep_llm._continuous_mode_active = True
    sleep_llm._continuous_groq_used   = 0

    old_state    = load_continuous_state()
    resume_cycle = old_state.get("resume_cycle")
    resume_done  = list(old_state.get("resume_phases_done") or [])

    state = _default_continuous_state()
    state["running"]        = True
    state["trigger"]        = trigger
    state["started_at"]     = _now_iso()
    state["last_heartbeat"] = _now_iso()
    if resume_cycle is not None:
        # current_cycle is incremented to cycle_num at the TOP of the loop
        # below — pre-set one below the cycle being resumed so that first
        # increment lands back on resume_cycle exactly.
        state["current_cycle"] = resume_cycle - 1
    save_continuous_state(state)

    engine = "ollama" if sleep_llm._ollama_available() else "groq-fallback"
    resume_note = (
        f" — resuming interrupted cycle {resume_cycle} ({len(resume_done)} phase(s) already done)"
        if resume_cycle is not None else ""
    )
    _log(f"CONTINUOUS SLEEP START — trigger={trigger} engine={engine}{resume_note}")

    stop_reason = "unknown"
    try:
        while not stop_check():
            state["current_cycle"] += 1
            cycle_num   = state["current_cycle"]
            # Phases already completed before an interruption resumed here —
            # only applies to the very first cycle of this run; every cycle
            # after starts with a clean slate.
            completed_keys = resume_done if cycle_num == resume_cycle else []
            resume_cycle = None
            # Read by sleep_insights_store._add_insight() so every insight/
            # reflection this cycle writes is tagged with it.
            sleep_insights_store._current_cycle = cycle_num
            cycle_start = time.monotonic()
            phases_done: list[str] = []
            cycle_tokens = cycle_insights = 0
            cycle_deleted = cycle_merged = cycle_promoted = cycle_mind_map_updates = 0

            # Phase 0 — always first, every cycle, never subject to priority
            # reordering. Skipped if a resumed cycle already completed it.
            phase0 = PHASE_0_MEMORY_MAINTENANCE
            if phase0["key"] in completed_keys:
                phases_done.append(phase0["name"])
                _log(f"CYCLE {cycle_num} PHASE 0 ({phase0['name']}) SKIPPED — already done before interruption")
            else:
                state["current_phase"]     = phase0["name"]
                state["current_phase_num"] = 0
                state["last_heartbeat"]    = _now_iso()
                save_continuous_state(state)
                try:
                    tokens, insights, summary = _phase_memory_maintenance(phase0["budget"])
                    cycle_tokens += tokens
                    cycle_insights += insights
                    phases_done.append(phase0["name"])
                    completed_keys.append(phase0["key"])
                    state["phases_done_this_cycle"] = list(completed_keys)
                    m = _CYCLE_SUMMARY_RE.search(summary)
                    if m:
                        cycle_deleted  += int(m.group(1))
                        cycle_merged   += int(m.group(2))
                        cycle_promoted += int(m.group(6))
                        cycle_mind_map_updates += int(m.group(7))
                    _log(f"CYCLE {cycle_num} PHASE 0 ({phase0['name']}) OK — {summary}")
                except Exception as e:
                    logger.warning("Continuous sleep phase 0 failed", exc_info=True)
                    _log(f"CYCLE {cycle_num} PHASE 0 FAILED — {e}")

            # Phase 5 sub-step — see the single-session run_sleep_session's
            # own comment above for why this runs unconditionally, every
            # cycle, free of the token budget entirely.
            try:
                folded = linguistic_fingerprint.update_from_session()
                _log(f"CYCLE {cycle_num} ACTUALIZACIÓN DE HUELLA LINGÜÍSTICA — {folded} turnos incorporados")
            except Exception as e:
                logger.warning("Linguistic fingerprint sub-phase failed", exc_info=True)
                _log(f"CYCLE {cycle_num} ACTUALIZACIÓN DE HUELLA LINGÜÍSTICA FAILED — {e}")

            if not stop_check():
                order, _priority_tokens = _assess_priority(PRIORITY_ASSESSMENT_MAX_TOKENS)
                for num in order:
                    if stop_check():
                        break
                    phase = _PHASES_BY_NUM[num]
                    if phase["key"] in completed_keys:
                        phases_done.append(phase["name"])
                        _log(f"CYCLE {cycle_num} PHASE {num} ({phase['name']}) SKIPPED — already done before interruption")
                        continue
                    state["current_phase"]     = phase["name"]
                    state["current_phase_num"] = num
                    state["last_heartbeat"]    = _now_iso()
                    save_continuous_state(state)
                    try:
                        tokens, insights, summary = _PHASE_FUNCS[phase["key"]](phase["budget"])
                        cycle_tokens += tokens
                        cycle_insights += insights
                        phases_done.append(phase["name"])
                        completed_keys.append(phase["key"])
                        state["phases_done_this_cycle"] = list(completed_keys)
                        _log(f"CYCLE {cycle_num} PHASE {num} ({phase['name']}) OK — tokens={tokens} insights={insights} — {summary}")
                    except Exception as e:
                        logger.warning("Continuous sleep phase %s failed", phase["key"], exc_info=True)
                        _log(f"CYCLE {cycle_num} PHASE {num} ({phase['name']}) FAILED — {e}")

            # Curiosidad profunda — only once ALL 7 standard phases (+ Phase 0)
            # finished this cycle with no interruption pending, and only if
            # Ollama actually has capacity right now. Zero Serper cost, zero
            # Groq/token budget — see core.sleep_curiosity_search._phase_
            # curiosity_deep's own docstring for why this has no time limit
            # of its own beyond stop_check() and its internal safety valve.
            if (
                len(phases_done) >= len(PHASES)
                and not stop_check()
                and sleep_llm._ollama_available()
            ):
                state["current_phase"]     = "🌌 Curiosidad Profunda"
                state["current_phase_num"] = len(PHASES) + 1
                state["last_heartbeat"]    = _now_iso()
                save_continuous_state(state)
                try:
                    deep_result = _phase_curiosity_deep(stop_check)
                    cycle_insights += deep_result.get("explored", 0)
                    _log(f"CYCLE {cycle_num} CURIOSIDAD PROFUNDA — {deep_result.get('explored', 0)} exploraciones")
                except Exception:
                    logger.warning("Curiosidad profunda failed", exc_info=True)
                    _log(f"CYCLE {cycle_num} CURIOSIDAD PROFUNDA FAILED")

            duration = time.monotonic() - cycle_start
            if len(phases_done) >= len(PHASES):
                state["total_cycles_completed"] += 1
                state["phases_done_this_cycle"] = []   # cycle fully wrapped — nothing left to resume
                if on_cycle_complete is not None:
                    try:
                        on_cycle_complete()
                    except Exception:
                        logger.warning("on_cycle_complete callback failed (non-critical)", exc_info=True)
            state["groq_fallback_used"]        = sleep_llm._continuous_groq_used
            state["total_deleted"]            += cycle_deleted
            state["total_merged"]             += cycle_merged
            state["total_promoted"]           += cycle_promoted
            state["total_insights_generated"] += cycle_insights
            state["total_mind_map_updates"]   += cycle_mind_map_updates
            state["last_heartbeat"]            = _now_iso()
            save_continuous_state(state)

            _log(
                f"CYCLE {cycle_num} COMPLETE — phases={len(phases_done)}/{len(PHASES)} "
                f"deleted={cycle_deleted} merged={cycle_merged} promoted={cycle_promoted} "
                f"insights={cycle_insights} tokens={cycle_tokens} duration={duration:.1f}s"
            )

        # Loop exited because stop_check() returned True — read back whatever
        # reason the caller (core/commands.py, via the state file) already
        # recorded for this stop BEFORE signaling us, so we report the real
        # cause instead of guessing. Never clobber it with our own default.
        # In practice unreachable today (see docstring — a real stop exits
        # via os._exit() before ever getting back here), kept as a fallback.
        stop_reason = load_continuous_state().get("stop_reason") or "interrupted"
    except Exception as e:
        _log(f"CONTINUOUS SLEEP FAILED — {e}")
        logger.warning("Continuous sleep failed (non-critical)", exc_info=True)
        stop_reason = "error"
    finally:
        sleep_llm._continuous_mode_active = False
        sleep_insights_store._current_cycle = None
        state = load_continuous_state()
        state["running"]       = False
        state["current_phase"] = ""
        state["stop_reason"]   = stop_reason
        state["stopped_at"]    = _now_iso()
        save_continuous_state(state)
        _log(f"CONTINUOUS SLEEP STOPPED — reason={stop_reason} cycles_completed={state['total_cycles_completed']}")

    return {"cycles_completed": state["total_cycles_completed"], "reason": stop_reason}
