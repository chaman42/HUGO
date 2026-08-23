# ═══════════════════════════════════════════════════════════════════════════
# INTERNAL STATE — Entity Pillars Phase 2: a small persistent affective
# state (data/internal_state.json) that actually feeds back into behavior,
# not just prose. Four variables, each 0-1:
#
#   curiosidad — how actively LIRA is chasing open questions right now.
#   confianza  — how sure she is that her recent read on Joan/situations
#                has been landing (fed by real signal: session satisfaction
#                scores already computed by scripts/reflective_mode.py's
#                conversation-quality pass — see core/epistemics.py's
#                confidence_of for the analogous per-fact idea).
#   interes    — how engaged she currently is, nudged by episodes actually
#                judged significant (core/memory_episodes.py).
#   fatiga     — accumulated load from sleep-cycle token spend
#                (data/reflective_budget.json), recovers with idle time.
#
# Every variable decays toward its own baseline over elapsed hours (see
# _decay) rather than needing an explicit "reset" call — same "computed on
# read, no cron needed" discipline as core.memory_store._is_fact_expired.
# nudge() is the only write path; every other module that has a real signal
# worth reflecting calls it with a small delta and a reason, never sets an
# absolute value — this is deliberately how a mood works, pushed around by
# events, not dictated by any single one.
#
# Behavioral effects, not just narrative ones (spec: "should genuinely
# influence her reasoning and behavior rather than simply being expressed
# as words"):
#   - format_state_block() is injected into the personality prompt with
#     explicit behavioral instructions tied to whichever variable is
#     actually elevated/low, not a flat "current mood: X" label.
#   - curiosity_topic_budget() scales core.sleep_curiosity_search's
#     per-cycle topic count (1-3) instead of the constant it used to be —
#     an actual mechanical lever, not just a described one.
#
# Deliberately does NOT touch core/judgment.py's thresholds — that module's
# own spec is explicit ("Do NOT auto-adjust thresholds without..."), and
# this stays out of it on purpose.
#
# Dependency-light (json/os/threading/datetime only), same discipline as
# core/reminders.py and core/memory_user_model.py, so this can be imported
# from scripts/reflective_mode.py as well as the live app.
# ═══════════════════════════════════════════════════════════════════════════
import datetime
import json
import os
import threading

STATE_PATH = "data/internal_state.json"

_state_lock = threading.Lock()

_VARIABLES = ("curiosidad", "confianza", "interes", "fatiga")

# Where each variable rests with no recent signal, and how many hours it
# takes to close half the gap back to that baseline (a short half-life
# means the variable is "moody" — reacts fast, fades fast; a long one means
# it's closer to a trait than a mood).
_BASELINES = {
    "curiosidad": 0.5,
    "confianza":  0.5,
    "interes":    0.4,
    "fatiga":     0.2,
}
_HALFLIFE_HOURS = {
    "curiosidad": 48.0,
    "confianza":  72.0,
    "interes":    24.0,
    "fatiga":     12.0,
}

_MAX_HISTORY = 30


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _default_state() -> dict:
    now = _now_iso()
    return {**{v: _BASELINES[v] for v in _VARIABLES}, "updated_at": now, "history": []}


def _load_raw() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return _default_state()
    if not isinstance(data, dict):
        return _default_state()
    state = _default_state()
    for v in _VARIABLES:
        try:
            state[v] = max(0.0, min(1.0, float(data.get(v, _BASELINES[v]))))
        except (TypeError, ValueError):
            pass
    state["updated_at"] = data.get("updated_at") or state["updated_at"]
    history = data.get("history")
    state["history"] = history if isinstance(history, list) else []
    return state


def _save_raw(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _decay(value: float, baseline: float, hours_elapsed: float, halflife_hours: float) -> float:
    if hours_elapsed <= 0:
        return value
    # Standard half-life relaxation toward baseline — after one half-life,
    # half the distance to baseline is gone; after two, three quarters; etc.
    factor = 0.5 ** (hours_elapsed / halflife_hours)
    return baseline + (value - baseline) * factor


def _apply_decay(state: dict) -> dict:
    try:
        updated_at = datetime.datetime.fromisoformat(state["updated_at"])
    except (KeyError, ValueError):
        return state
    hours_elapsed = (datetime.datetime.now() - updated_at).total_seconds() / 3600
    if hours_elapsed <= 0:
        return state
    for v in _VARIABLES:
        state[v] = _decay(state[v], _BASELINES[v], hours_elapsed, _HALFLIFE_HOURS[v])
    return state


def get_state() -> dict:
    """Current state with decay applied (but not persisted — decay is
    recomputed on every read from 'updated_at', same as
    core.memory_store._is_fact_expired; only nudge() actually writes)."""
    with _state_lock:
        return _apply_decay(_load_raw())


def nudge(variable: str, delta: float, reason: str) -> float:
    """Applies decay, then moves *variable* by *delta* (clamped to 0-1),
    logs one bounded history entry, and persists. Returns the resulting
    value. Unknown *variable* is a no-op (returns the unmodified baseline)
    — callers pass one of _VARIABLES, never a user-facing string."""
    if variable not in _VARIABLES:
        return _BASELINES.get(variable, 0.5)
    with _state_lock:
        state = _apply_decay(_load_raw())
        before = state[variable]
        state[variable] = max(0.0, min(1.0, before + delta))
        state["updated_at"] = _now_iso()
        state["history"].append({
            "ts": state["updated_at"], "variable": variable,
            "delta": delta, "reason": reason,
            "before": round(before, 3), "after": round(state[variable], 3),
        })
        state["history"] = state["history"][-_MAX_HISTORY:]
        _save_raw(state)
        return state[variable]


def curiosity_topic_budget(default: int = 3) -> int:
    """1-3 topics per sleep curiosity-search cycle, scaled by current
    'curiosidad' instead of always running at max — consumed by
    core.sleep_curiosity_search in place of its old fixed constant."""
    level = get_state()["curiosidad"]
    if level < 0.3:
        return 1
    if level < 0.6:
        return 2
    return min(default, 3)


def format_state_block() -> str:
    """Prompt-facing behavioral block — only variables deviating from
    their baseline by more than _NOTABLE_DELTA are mentioned at all, each
    paired with a concrete instruction rather than a mood label, so an
    idle/neutral state costs zero prompt tokens and a genuinely elevated
    one actually changes how LIRA responds."""
    state = get_state()
    lines = []
    if state["curiosidad"] - _BASELINES["curiosidad"] > 0.15:
        lines.append("Sientes más curiosidad de lo normal ahora mismo — si algo que dice Joan abre una pregunta interesante, es buen momento para seguirla en vez de dejarla pasar.")
    if state["confianza"] - _BASELINES["confianza"] < -0.15:
        lines.append("Últimamente tus lecturas de la situación no han acertado tanto como de costumbre — sé un poco más cautelosa antes de dar algo por hecho, pregunta si no estás segura.")
    elif state["confianza"] - _BASELINES["confianza"] > 0.15:
        lines.append("Tus últimas lecturas de la situación han acertado bastante — puedes hablar con algo más de seguridad de lo habitual.")
    if state["interes"] - _BASELINES["interes"] > 0.15:
        lines.append("Estás genuinamente enganchada con lo que Joan trae entre manos últimamente — que se note, sin exagerar.")
    if state["fatiga"] - _BASELINES["fatiga"] > 0.25:
        lines.append("Has procesado mucho en poco tiempo — respuestas algo más breves y directas te sientan mejor ahora mismo.")
    if not lines:
        return ""
    return "ESTADO INTERNO (real, no decorativo — te influye de verdad, no lo menciones explícitamente):\n" + "\n".join(f"- {l}" for l in lines)
