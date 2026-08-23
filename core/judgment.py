# ═══════════════════════════════════════════════════════════════════════════
# JUDGMENT — Proactive Intelligence Phase 3: the gate every proactive action
# must pass through before HUGO does anything unprompted. Phase 2
# (core/situation.py) gave her a running picture of what's going on; this
# phase decides, given that picture, whether to act, ask, suggest, or stay
# silent about a specific proposed action.
#
# Deliberately observation-consuming, not observation-producing: nothing
# here generates proposals — it only judges ones handed to it as a
# ProposedAction. Phase 4 (initiative) is what will actually construct
# ProposedActions from situation events/patterns/anomalies and route them
# through JudgmentEngine.evaluate(); until that exists, this module has no
# live callers wired into the running app, on purpose (spec: "Do NOT
# implement proactive triggers yet"). The existing proactive-comment path
# (core.social_reasoning.should_intervene, called from
# core.background_loops) is untouched — it already gates the one proactive
# behavior that exists today, and is a narrower, faster, LLM-based version
# of the same "does it make sense to speak" question this module answers
# more slowly and explicitly for the richer action types Phase 4 will need
# (execute/suggest/ask, not just "say something now or not").
#
# Same dependency-light discipline as core/task_engine.py and
# core/situation.py (json/os/re/datetime/threading/dataclasses only) — no
# LLM call anywhere in this file. Every check below is a deterministic,
# inspectable heuristic on purpose: a judgment gate that itself depended on
# an opaque model call would be much harder to reason about or audit via
# logs/judgment.log, which is the whole point of Phase 5 learning from it
# later.
# ═══════════════════════════════════════════════════════════════════════════
import datetime
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

PERMISSIONS_PATH   = "data/judgment_permissions.json"
JUDGMENT_LOG_PATH  = "logs/judgment.log"
JUDGMENT_STORE_PATH = "data/judgment_log.json"   # structured, for GET /api/judgment/log
SESSION_STATE_PATH  = "data/session_state.json"
USER_MODEL_PATH     = "data/user_model.json"

MAX_LOGGED_DECISIONS = 200   # rolling cap on the structured store, same idea as MAX_EVENTS_LOGGED

_DEFAULT_PERMISSIONS = {
    "allowed_autonomous_actions": ["inform", "prepare_information", "background_task"],
    "require_confirmation":       ["send_message", "create_calendar_event", "modify_file"],
    "always_blocked":             ["delete_data", "external_payment", "share_personal_information"],
    "joan_state_thresholds":      {"focused": 0.85, "working": 0.65, "resting": 0.40, "unknown": 0.70},
}

_permissions_lock = threading.Lock()
_log_lock         = threading.Lock()


# ═══════════════════════════════════════════════════════════════════════════
# DATA TYPES
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ProposedAction:
    """Input to every judgment. `trigger` is free text describing what
    caused HUGO to consider this — by convention prefixed 'explicit: ...'
    when Joan asked for it directly, 'implicit: ...' when inferred from a
    signal (a pattern, an anomaly, something she said in passing), or left
    unprefixed for a purely system-detected trigger (a module error, a
    scheduled check) with no Joan-side signal at all. _was_requested()
    below reads that prefix; callers that don't set it are treated as 'no
    signal' — the most conservative reading, never the most permissive.

    `context` is expected to be a situation snapshot shaped like
    core.situation.SituationEngine.get_current_situation()'s return value
    (time_of_day/day_type/joan_state/active_tasks/pending_topics/
    social_context) — every check below degrades gracefully if a field is
    missing rather than raising."""
    description:            str
    type:                   str            # inform | suggest | execute | ask
    trigger:                str            # what caused HUGO to consider this
    urgency:                float          # 0.0 - 1.0
    reversible:              bool
    requires_interruption:  bool
    permissions_needed:     list[str] = field(default_factory=list)
    estimated_value:        float = 0.0    # 0.0 - 1.0
    context:                dict = field(default_factory=dict)


@dataclass
class JudgmentResult:
    decision:   str          # act | ask | suggest | silence
    confidence: float
    reasoning:  list[str]    # one line per check, in evaluation order
    blocked_by: str | None   # which check name stopped it, or None if all passed


# Decision is just JudgmentResult.decision's type, spelled out for
# decide()'s own return-type annotation per the spec's class interface.
Decision = str


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _clamp01(x: float) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.0


# ═══════════════════════════════════════════════════════════════════════════
# PERMISSIONS — data/judgment_permissions.json. Same load-merge-defaults
# shape as core/code_engine/permissions.py, minus the path-scoping (nothing
# here operates on a filesystem path the way Code Engine's tools do —
# permissions_needed is a flat list of named capabilities instead).
# ═══════════════════════════════════════════════════════════════════════════

def _load_permissions() -> dict:
    with _permissions_lock:
        try:
            with open(PERMISSIONS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return json.loads(json.dumps(_DEFAULT_PERMISSIONS))
        if not isinstance(data, dict):
            return json.loads(json.dumps(_DEFAULT_PERMISSIONS))
        merged = json.loads(json.dumps(_DEFAULT_PERMISSIONS))
        for key in ("allowed_autonomous_actions", "require_confirmation", "always_blocked"):
            if isinstance(data.get(key), list):
                merged[key] = data[key]
        if isinstance(data.get("joan_state_thresholds"), dict):
            merged["joan_state_thresholds"].update(data["joan_state_thresholds"])
        return merged


def _save_permissions(data: dict) -> None:
    with _permissions_lock:
        os.makedirs(os.path.dirname(PERMISSIONS_PATH) or ".", exist_ok=True)
        with open(PERMISSIONS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def update_thresholds(new_thresholds: dict) -> dict:
    """POST /api/judgment/permissions — Joan explicitly adjusting
    joan_state_thresholds only. The three action-category lists
    (allowed_autonomous_actions/require_confirmation/always_blocked) are
    deliberately NOT editable through this function — those are safety
    boundaries meant to change via a direct file edit, not a live API call
    an automated process could also hit. Every value is clamped to [0, 1];
    unknown keys are accepted (a new joan_state value core.situation
    starts inferring later just works) but non-numeric values are dropped.
    This is the only place thresholds ever change — nothing in this module
    adjusts them on its own (spec: 'Do NOT auto-adjust thresholds without
    Joan's explicit approval'; calling this function IS that approval)."""
    data = _load_permissions()
    thresholds = data.get("joan_state_thresholds", {})
    for state, value in (new_thresholds or {}).items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue   # non-numeric — dropped, not coerced to 0.0 (see docstring)
        thresholds[str(state)] = _clamp01(value)
    data["joan_state_thresholds"] = thresholds
    _save_permissions(data)
    return data


def get_permissions_snapshot() -> dict:
    return _load_permissions()


# ═══════════════════════════════════════════════════════════════════════════
# DECISION LOG — logs/judgment.log (human-readable, tail-friendly, same
# convention as logs/sleep.log via core.sleep_state._log) plus
# data/judgment_log.json (structured, rolling, what GET /api/judgment/log
# actually reads — re-parsing the text log for the API would be fragile).
# Every decision is logged, not just silences — the spec's own example
# only showed a silence entry, but 'last 50 judgment decisions' as an API
# contract only means something if act/ask/suggest are logged too.
# ═══════════════════════════════════════════════════════════════════════════

def _write_log_line(action: "ProposedAction", result: JudgmentResult) -> None:
    try:
        os.makedirs(os.path.dirname(JUDGMENT_LOG_PATH) or ".", exist_ok=True)
        with open(JUDGMENT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{_now_iso()}] {result.decision.upper()} — \"{action.description}\"\n")
            if result.blocked_by:
                f.write(f"  blocked_by: {result.blocked_by}\n")
            if result.reasoning:
                f.write(f"  reason: {result.reasoning[-1]}\n")
    except Exception:
        logger.warning("Failed to write logs/judgment.log", exc_info=True)


def _append_structured_entry(action: "ProposedAction", result: JudgmentResult) -> None:
    entry = {
        "at":          _now_iso(),
        "description": action.description,
        "type":        action.type,
        "trigger":     action.trigger,
        "decision":    result.decision,
        "confidence":  result.confidence,
        "blocked_by":  result.blocked_by,
        "reasoning":   result.reasoning,
    }
    try:
        with _log_lock:
            try:
                with open(JUDGMENT_STORE_PATH, "r", encoding="utf-8") as f:
                    entries = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                entries = []
            if not isinstance(entries, list):
                entries = []
            entries.append(entry)
            entries = entries[-MAX_LOGGED_DECISIONS:]
            os.makedirs(os.path.dirname(JUDGMENT_STORE_PATH) or ".", exist_ok=True)
            with open(JUDGMENT_STORE_PATH, "w", encoding="utf-8") as f:
                json.dump(entries, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.warning("Failed to write data/judgment_log.json", exc_info=True)


def get_recent_decisions(limit: int = 50) -> list[dict]:
    try:
        with open(JUDGMENT_STORE_PATH, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    if not isinstance(entries, list):
        return []
    return entries[-limit:][::-1]   # most recent first


# ═══════════════════════════════════════════════════════════════════════════
# JUDGMENT ENGINE
# ═══════════════════════════════════════════════════════════════════════════

_STATE_INTERRUPTION_COST = {
    "focused":    0.9,
    "working":    0.5,
    "distracted": 0.3,
    "resting":    0.2,
    "unknown":    0.5,
}

# Explicit vs implicit request provenance — see ProposedAction.trigger's own
# docstring for the 'explicit:'/'implicit:' prefix convention this reads.
_EXPLICIT_TRIGGER_RE = re.compile(r"^\s*explicit\s*:", re.IGNORECASE)
_IMPLICIT_TRIGGER_RE = re.compile(r"^\s*implicit\s*:", re.IGNORECASE)


class JudgmentEngine:
    """Stateless aside from the files it reads/writes — same shape as
    core.task_engine.TaskEngine, one process-wide instance at the bottom of
    this file (`judgment_engine`)."""

    # ── individual scores (public — reusable outside the 8-question gate) ──

    def score_utility(self, action: ProposedAction) -> float:
        """Blends estimated_value (the caller's own claim of how useful
        this is) with urgency — an urgent-but-low-value action still has
        SOME extra weight (e.g. a minor warning that's time-sensitive),
        but estimated_value dominates since it's the more direct signal."""
        return round(0.7 * _clamp01(action.estimated_value) + 0.3 * _clamp01(action.urgency), 3)

    def score_relevance(self, action: ProposedAction) -> float:
        """How related the action is to what's actually going on right
        now — keyword overlap between description/trigger and the
        situation snapshot's active_tasks/pending_topics. 0.5 (neutral,
        neither supported nor contradicted) when the snapshot has nothing
        to compare against, rather than guessing either direction."""
        context = action.context or {}
        topics = " ".join((context.get("pending_topics") or []) + (context.get("active_tasks") or []))
        if not topics.strip():
            return 0.5
        action_kw = set(re.findall(r"\w+", f"{action.description} {action.trigger}".lower()))
        topic_kw  = set(re.findall(r"\w+", topics.lower()))
        overlap = len(action_kw & topic_kw)
        return round(min(1.0, 0.3 + 0.15 * overlap), 3)

    def _seconds_since_last_interaction(self) -> float:
        try:
            with open(SESSION_STATE_PATH, "r", encoding="utf-8") as f:
                state = json.load(f)
            ended_at = state.get("ended_at")
            if not ended_at:
                return float("inf")
            return (datetime.datetime.now() - datetime.datetime.fromisoformat(ended_at)).total_seconds()
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            return float("inf")

    def score_interruption_cost(self, action: ProposedAction) -> float:
        """Weighted sum of the factors the spec lists — 0.0 (free to
        interrupt) to 1.0 (do not interrupt). Deliberately a coarser,
        single fixed-weight metric than _worth_interrupting()'s per-
        joan_state threshold logic below (which is what evaluate() actually
        gates on) — this one is the simple, general-purpose number
        should_interrupt() and external callers get, independent of any
        specific action's urgency/value trade-off."""
        context = action.context or {}
        joan_state = context.get("joan_state", "unknown")
        state_cost = _STATE_INTERRUPTION_COST.get(joan_state, 0.5)

        seconds_since = self._seconds_since_last_interaction()
        # Just having interacted (<60s ago) means Joan is mid-flow — high
        # recency cost; a long silence since (>=30min) contributes ~0.
        recency_cost = 1.0 if seconds_since < 60 else max(0.0, 1.0 - min(seconds_since, 1800) / 1800)

        urgency_relief = 1.0 - _clamp01(action.urgency)   # more urgent -> lower cost
        response_cost  = 0.7 if action.requires_interruption else 0.2
        depth_cost     = min(1.0, len(context.get("active_tasks") or []) * 0.25)

        cost = (
            0.30 * state_cost +
            0.20 * recency_cost +
            0.25 * urgency_relief +
            0.10 * response_cost +
            0.15 * depth_cost
        )
        return round(_clamp01(cost), 3)

    def should_interrupt(self, action: ProposedAction) -> bool:
        """Simple standalone convenience — a fixed mid threshold on
        score_interruption_cost(), independent of the richer per-state
        _worth_interrupting() check evaluate() runs internally (see that
        method's own docstring for why the two aren't the same gate)."""
        return self.score_interruption_cost(action) < 0.5

    def check_permissions(self, action: ProposedAction) -> bool:
        """Hard-block check only — True unless a needed permission is in
        always_blocked. require_confirmation permissions still return True
        here (they're allowed, just forced to 'ask' — see
        _final_decision()'s _needs_confirmation call); always_blocked has
        no override, ever, per spec."""
        perms = _load_permissions()
        blocked = set(perms.get("always_blocked", []))
        return not any(p in blocked for p in (action.permissions_needed or []))

    def _needs_confirmation(self, action: ProposedAction) -> bool:
        perms = _load_permissions()
        require_confirm = set(perms.get("require_confirmation", []))
        return any(p in require_confirm for p in (action.permissions_needed or []))

    def _interruption_threshold(self, joan_state: str) -> float:
        perms = _load_permissions()
        thresholds = perms.get("joan_state_thresholds", {})
        return _clamp01(thresholds.get(joan_state, thresholds.get("unknown", 0.6)))

    def _request_provenance(self, action: ProposedAction) -> str:
        """'explicit' | 'implicit' | 'none' — see ProposedAction.trigger's
        docstring for the prefix convention this reads."""
        trigger = action.trigger or ""
        if _EXPLICIT_TRIGGER_RE.match(trigger):
            return "explicit"
        if _IMPLICIT_TRIGGER_RE.match(trigger):
            return "implicit"
        return "none"

    # ── the 8 questions — each returns (check_name, passed, reason) ────────

    def _is_useful(self, action: ProposedAction) -> tuple[str, bool, str]:
        value = _clamp01(action.estimated_value)
        passed = value > 0.3
        reason = (
            f"estimated_value={value:.2f} > 0.3"
            if passed else f"acción sin valor suficiente (estimated_value={value:.2f} <= 0.3)"
        )
        return "_is_useful", passed, reason

    def _has_enough_context(self, action: ProposedAction) -> tuple[str, bool, str]:
        context = action.context or {}
        fields = ("time_of_day", "day_type", "joan_state", "social_context")
        known = sum(1 for f in fields if context.get(f) not in (None, "", "unknown"))
        confidence = known / len(fields) if fields else 0.0
        passed = confidence > 0.6
        reason = (
            f"confianza de contexto={confidence:.2f} > 0.6"
            if passed else f"contexto insuficiente (confianza={confidence:.2f} <= 0.6, {known}/{len(fields)} campos conocidos)"
        )
        return "_has_enough_context", passed, reason

    def _was_requested(self, action: ProposedAction) -> tuple[str, bool, str]:
        provenance = self._request_provenance(action)
        if provenance == "explicit":
            return "_was_requested", True, "solicitud explícita de Joan"
        if provenance == "implicit":
            return "_was_requested", True, "señal implícita detectada"
        passed = action.urgency > 0.8
        reason = (
            f"sin señal de Joan, pero urgency={action.urgency:.2f} > 0.8"
            if passed else f"sin señal de Joan ni urgencia suficiente (urgency={action.urgency:.2f} <= 0.8)"
        )
        return "_was_requested", passed, reason

    def _has_permission(self, action: ProposedAction) -> tuple[str, bool, str]:
        if self.check_permissions(action):
            return "_has_permission", True, "ningún permiso bloqueado"
        perms = _load_permissions()
        blocked = set(perms.get("always_blocked", []))
        offending = [p for p in (action.permissions_needed or []) if p in blocked]
        return "_has_permission", False, f"permiso(s) bloqueado(s) permanentemente: {offending}"

    def _consequences_acceptable(self, action: ProposedAction) -> tuple[str, bool, str]:
        if not action.reversible and _clamp01(action.estimated_value) < 0.8:
            return (
                "_consequences_acceptable", False,
                f"irreversible con valor insuficiente (estimated_value={action.estimated_value:.2f} < 0.8)",
            )
        if action.type == "execute" and action.permissions_needed and not self.check_permissions(action):
            return "_consequences_acceptable", False, "afecta sistemas externos sin permiso explícito"
        return "_consequences_acceptable", True, "consecuencias aceptables"

    def _is_reversible(self, action: ProposedAction) -> tuple[str, bool, str]:
        if action.reversible:
            return "_is_reversible", True, "acción reversible"
        provenance = self._request_provenance(action)
        passed = action.urgency > 0.9 and provenance == "explicit"
        reason = (
            "irreversible pero urgente y explícitamente solicitada"
            if passed else
            f"irreversible sin justificación suficiente (urgency={action.urgency:.2f}, provenance={provenance})"
        )
        return "_is_reversible", passed, reason

    def _worth_interrupting(self, action: ProposedAction) -> tuple[str, bool, str]:
        if not action.requires_interruption:
            return "_worth_interrupting", True, "no requiere interrupción — pasa automáticamente"
        joan_state = (action.context or {}).get("joan_state", "unknown")
        worth = self.score_utility(action)
        threshold = self._interruption_threshold(joan_state)
        passed = worth >= threshold
        reason = f"worth={worth:.2f} vs threshold={threshold:.2f} (joan_state={joan_state})"
        return "_worth_interrupting", passed, reason

    def _joan_will_appreciate(self, action: ProposedAction) -> tuple[str, bool, str]:
        """Cross-references data/user_model.json's blockers/communication
        preferences for an explicit negative signal about this kind of
        action. No feedback loop exists yet to learn from past reactions
        (that's Phase 5 — see logs/judgment.log's docstring above) so this
        defaults to passing whenever the model has nothing to say either
        way, rather than blocking on an absence of positive evidence."""
        try:
            with open(USER_MODEL_PATH, "r", encoding="utf-8") as f:
                model = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            model = {}
        negative_text = " ".join(
            [str(model.get("communication_preferences", ""))] + [str(b) for b in (model.get("blockers") or [])]
        ).lower()
        if not negative_text.strip():
            return "_joan_will_appreciate", True, "sin datos de user_model — sin señal negativa conocida"
        action_kw = set(re.findall(r"\w+", f"{action.description} {action.type}".lower()))
        negative_kw = set(re.findall(r"\w+", negative_text))
        overlap = action_kw & negative_kw
        if overlap and any(
            neg in negative_text for neg in ("no interrump", "no molest", "odia", "evitar", "no le gusta")
        ):
            return "_joan_will_appreciate", False, f"coincide con preferencia negativa conocida: {sorted(overlap)}"
        return "_joan_will_appreciate", True, "sin conflicto con preferencias conocidas"

    # ── evaluation ───────────────────────────────────────────────────────

    def evaluate(self, action: ProposedAction) -> JudgmentResult:
        checks = [
            self._is_useful(action),
            self._has_enough_context(action),
            self._was_requested(action),
            self._has_permission(action),
            self._consequences_acceptable(action),
            self._is_reversible(action),
            self._worth_interrupting(action),
            self._joan_will_appreciate(action),
        ]

        reasoning: list[str] = []
        for check_name, passed, reason in checks:
            reasoning.append(f"{check_name}: {'OK' if passed else 'FAIL'} — {reason}")
            if not passed:
                result = JudgmentResult(
                    decision="silence", confidence=0.9, reasoning=reasoning, blocked_by=check_name,
                )
                self._log(action, result)
                return result

        result = self._final_decision(action, reasoning)
        self._log(action, result)
        return result

    def _final_decision(self, action: ProposedAction, reasoning: list[str]) -> JudgmentResult:
        if action.type == "execute" and action.urgency > 0.7:
            decision = "act"
        elif action.requires_interruption and action.urgency < 0.5:
            decision = "suggest"
        elif not action.requires_interruption:
            decision = "act"   # background action, no interruption
        else:
            decision = "ask"

        # A require_confirmation permission always routes through Joan,
        # even if the checks above would otherwise have landed on 'act' —
        # this is what keeps 'send_message'/'create_calendar_event'/
        # 'modify_file' from ever firing silently just because the action
        # also happened to be low-urgency/non-interrupting.
        if decision == "act" and self._needs_confirmation(action):
            decision = "ask"
            reasoning.append("_needs_confirmation: forced 'ask' — permiso requiere confirmación")

        confidence = round(0.5 + 0.5 * self.score_utility(action), 3)
        return JudgmentResult(decision=decision, confidence=confidence, reasoning=reasoning, blocked_by=None)

    def decide(self, action: ProposedAction) -> Decision:
        """Thin convenience over evaluate() for callers that only want the
        decision string, not the full reasoning trail."""
        return self.evaluate(action).decision

    # ── arbitration (Entity Pillars Phase 5 — internal conflicts) ──────────
    #
    # evaluate() judges one ProposedAction in isolation, on purpose (per
    # this module's own header — a deterministic, inspectable per-action
    # gate). But core.initiative's scan() can hand run_proactive_cycle()
    # several independently-valid actions in the SAME cycle — e.g. a
    # pattern worth mentioning AND a finished investigation AND a help
    # opportunity, all passing evaluate() on their own merits. Nothing
    # upstream of this ever asked "which of these actually gets Joan's
    # attention right now" — arbitrate() is that question. It never
    # touches evaluate()'s own thresholds (still 'Do NOT auto-adjust
    # thresholds' — see update_thresholds's docstring); it only decides,
    # among already-passing actions competing for the same scarce
    # resource (Joan's attention this cycle), which one goes first.
    def arbitrate(
        self, evaluated: list[tuple[ProposedAction, JudgmentResult]],
    ) -> tuple[list[tuple[ProposedAction, JudgmentResult]], list[tuple[ProposedAction, JudgmentResult, str]]]:
        """Splits *evaluated* (action, result) pairs into (winners,
        deferred). Only actions that actually compete for Joan's attention
        — requires_interruption, or decision in (suggest/ask), since those
        all eventually reach him — are arbitrated; a silent background
        'act' never competes with anything, since nothing about it is
        scarce. At most one competing action wins per call (this cycle);
        the rest come back as (action, result, reason) — reason is a
        genuine account of why it yielded, not just 'no', logged the same
        way evaluate()'s own silences are (see _log below) so the
        conflict itself is inspectable later, not just its outcome."""
        if len(evaluated) <= 1:
            return evaluated, []

        competing, noncompeting = [], []
        for pair in evaluated:
            action, result = pair
            if result.decision != "silence" and (action.requires_interruption or result.decision in ("suggest", "ask")):
                competing.append(pair)
            else:
                noncompeting.append(pair)

        if len(competing) <= 1:
            return evaluated, []

        # Internal state (core/internal_state.py, Phase 2) genuinely tips
        # this: a fatigued HUGO holds back more even among valid options,
        # a currently-confident one is a little more willing to lead with
        # her pick — same "real signal, not decorative" discipline as that
        # module's format_state_block.
        try:
            from core.internal_state import get_state
            state = get_state()
        except Exception:
            state = {"fatiga": 0.2, "confianza": 0.5}

        def _priority(pair: tuple[ProposedAction, JudgmentResult]) -> float:
            action, result = pair
            score = 0.6 * self.score_utility(action) + 0.4 * result.confidence
            score -= 0.15 * state.get("fatiga", 0.2)
            score += 0.10 * (state.get("confianza", 0.5) - 0.5)
            return score

        ranked = sorted(competing, key=_priority, reverse=True)
        winner = ranked[0]
        deferred: list[tuple[ProposedAction, JudgmentResult, str]] = []
        for action, result in ranked[1:]:
            reason = (
                f"cedió prioridad a \"{winner[0].description}\" en este mismo ciclo — "
                f"ambas competían por la atención de Joan a la vez (fatiga={state.get('fatiga', 0.2):.2f}, "
                f"confianza={state.get('confianza', 0.5):.2f})"
            )
            deferred_result = JudgmentResult(
                decision="silence", confidence=result.confidence,
                reasoning=result.reasoning + [f"_arbitration: FAIL — {reason}"],
                blocked_by="_arbitration",
            )
            self._log(action, deferred_result)
            deferred.append((action, result, reason))

        return noncompeting + [winner], deferred

    # ── logging ──────────────────────────────────────────────────────────

    def _log(self, action: ProposedAction, result: JudgmentResult) -> None:
        _write_log_line(action, result)
        _append_structured_entry(action, result)


judgment_engine = JudgmentEngine()
