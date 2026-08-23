# ═══════════════════════════════════════════════════════════════════════════
# SPONTANEITY — Proactive Intelligence Phase 5. Everything through Phase 4
# answers "is this worth doing" (InitiativeEngine/JudgmentEngine/ActionEngine).
# This phase answers a narrower, higher-bar question: "is this worth doing
# specifically because it will genuinely, pleasantly surprise Joan" — a
# small subset of proactive behavior, deliberately rare (see
# SPONTANEITY_LIMITS), that exists purely to feel good, not to be useful in
# the ordinary sense Phase 4 already covers.
#
# SpontaneityEngine sits ON TOP of the existing pipeline, not beside it:
# every candidate it proposes still goes through JudgmentEngine (spec:
# "must_pass_judgment: True, non-negotiable") before anything reaches Joan,
# and delivery reuses core.initiative's queue/delivery machinery rather than
# inventing a second one — see run_spontaneity_cycle() and
# core.initiative._deliver_pending_initiative's 'source'-aware phrasing
# branch below.
#
# Same no-LLM, deterministic-heuristic discipline as Phase 2-4 for
# candidate generation and scoring — the one exception is _generate_discovery,
# which makes a real core.tools_search.search_web() call (network, not an
# LLM), same as InitiativeEngine's own use of that function.
# ═══════════════════════════════════════════════════════════════════════════
import datetime
import json
import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass, field

from core.judgment import ProposedAction, judgment_engine

logger = logging.getLogger(__name__)

LOG_PATH        = "data/spontaneity_log.json"
EPISODES_PATH   = "data/episodes.json"
USER_MODEL_PATH = "data/user_model.json"

MINIMUM_THRESHOLD = 0.72   # spec: "intentionally high — most candidates are discarded"
MIN_HISTORY_FOR_RECALIBRATION = 10   # spec: "After 10+ outcomes per type, scoring weights auto-adjust"
REACTION_CAPTURE_TTL_SECONDS = 600   # how long a pending reaction slot stays open for the next turn

SPONTANEITY_LIMITS = {
    "max_per_day":          2,
    "min_hours_between":    4,
    "max_per_week":         6,
    "cooldown_after_unwanted": 48,   # hours
}

# Documentation + the single source of truth for which method enforces each
# rule — every value here is True/non-negotiable, so this dict is never
# branched on directly; it's a checklist cross-referenced in comments at
# each enforcement point (can_trigger, select_best, run_spontaneity_cycle).
SPONTANEITY_RULES = {
    "must_pass_judgment":                 True,   # enforced: run_spontaneity_cycle always calls judgment_engine.evaluate
    "must_be_reversible_if_unsolicited":  True,   # enforced: select_best() drops any non-reversible candidate outright
    "never_interrupt_focused_state":      True,   # enforced: select_best() drops interruption_required candidates when joan_state=='focused', no urgency override
    "never_share_personal_data_spontaneously": True,   # enforced: select_best() drops any candidate needing a sensitive permission; generators never request one
    "never_repeat_unwanted_type":         True,   # enforced: select_best() excludes any type with an 'unwanted' outcome in history
    "max_consecutive_same_type":          2,      # enforced: select_best() excludes a type that was the last N delivered types
}

_SENSITIVE_PERMISSIONS = {"share_personal_information", "delete_data", "external_payment"}

_log_lock = threading.Lock()

# 'The next user turn is a reaction to a spontaneous delivery' — same
# module-level "one pending slot" pattern as core.intent._pending_action.
# Only one spontaneous delivery is ever outstanding at a time (spontaneity
# is capped at 2/day with a 4h gap, so overlap is structurally impossible).
_pending_reaction: dict | None = None
_pending_reaction_lock = threading.Lock()


def _now() -> datetime.datetime:
    return datetime.datetime.now()


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


# ═══════════════════════════════════════════════════════════════════════════
# DATA TYPES
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class SpontaneousCandidate:
    id:                    str
    description:           str
    action:                ProposedAction
    utility_score:         float
    relevance_score:       float
    surprise_value:        float
    reversible:            bool
    interruption_required: bool
    minimum_threshold:     float = MINIMUM_THRESHOLD
    # Not in the spec's literal dataclass, but required for outcome_weights/
    # never_repeat_unwanted_type/max_consecutive_same_type to mean anything
    # at all — those are explicitly per-type rules (see spontaneity_log.json's
    # own 'outcome_weights' keyed by discovery/optimization/preparation/
    # observation/small_gesture). Every _generate_* below sets it to its own
    # generator name.
    type: str = ""


# ═══════════════════════════════════════════════════════════════════════════
# LOG — data/spontaneity_log.json. today_count/week_count auto-roll when the
# stored date/week no longer matches now, rather than needing a separate
# midnight/Monday cron job.
# ═══════════════════════════════════════════════════════════════════════════

_DEFAULT_LOG = {
    "today_count": 0, "week_count": 0, "last_triggered": None, "last_unwanted": None,
    "cooldown_active": False, "manually_disabled_until": None, "history": [],
    "outcome_weights": {"discovery": 0.8, "optimization": 0.9, "preparation": 1.1, "observation": 0.7, "small_gesture": 1.0},
}


def _load_log() -> dict:
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = json.loads(json.dumps(_DEFAULT_LOG))
    if not isinstance(data, dict):
        data = json.loads(json.dumps(_DEFAULT_LOG))
    for key, default in _DEFAULT_LOG.items():
        data.setdefault(key, default if not isinstance(default, (dict, list)) else json.loads(json.dumps(default)))

    now = _now()
    last_triggered = data.get("last_triggered")
    if last_triggered:
        try:
            last_dt = datetime.datetime.fromisoformat(last_triggered)
            if last_dt.date() != now.date():
                data["today_count"] = 0
            if last_dt.isocalendar()[:2] != now.isocalendar()[:2]:
                data["week_count"] = 0
        except ValueError:
            pass

    last_unwanted = data.get("last_unwanted")
    if data.get("cooldown_active") and last_unwanted:
        try:
            elapsed_h = (now - datetime.datetime.fromisoformat(last_unwanted)).total_seconds() / 3600
            if elapsed_h >= SPONTANEITY_LIMITS["cooldown_after_unwanted"]:
                data["cooldown_active"] = False
        except ValueError:
            data["cooldown_active"] = False

    disabled_until = data.get("manually_disabled_until")
    if disabled_until:
        try:
            if now >= datetime.datetime.fromisoformat(disabled_until):
                data["manually_disabled_until"] = None
        except ValueError:
            data["manually_disabled_until"] = None

    return data


def _save_log_locked(data: dict) -> None:
    os.makedirs(os.path.dirname(LOG_PATH) or ".", exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_log_snapshot() -> dict:
    return _load_log()


def get_status() -> dict:
    log = _load_log()
    next_available = None
    if log.get("last_triggered"):
        try:
            last_dt = datetime.datetime.fromisoformat(log["last_triggered"])
            next_available = (last_dt + datetime.timedelta(hours=SPONTANEITY_LIMITS["min_hours_between"])).isoformat(timespec="seconds")
        except ValueError:
            pass
    return {
        "today_count":     log.get("today_count", 0),
        "week_count":      log.get("week_count", 0),
        "cooldown_active": log.get("cooldown_active", False),
        "disabled":        bool(log.get("manually_disabled_until")),
        "next_available":  next_available,
        "can_trigger_now": SpontaneityEngine().can_trigger(),
    }


def disable_temporarily(hours: float = 24.0) -> dict:
    """POST /api/spontaneity/disable — Joan explicitly pausing spontaneity
    for a while. Not a permanent off switch (that's the 'proactividad'
    feature flag, which already gates every other proactive path) — just a
    cooldown Joan controls directly, same shape as cooldown_active but
    caller-initiated instead of outcome-triggered."""
    with _log_lock:
        data = _load_log()
        data["manually_disabled_until"] = (_now() + datetime.timedelta(hours=hours)).isoformat(timespec="seconds")
        _save_log_locked(data)
        return data


def _load_episodes_raw() -> list[dict]:
    try:
        with open(EPISODES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _load_user_model() -> dict:
    try:
        with open(USER_MODEL_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


# ═══════════════════════════════════════════════════════════════════════════
# SPONTANEITY ENGINE
# ═══════════════════════════════════════════════════════════════════════════

class SpontaneityEngine:

    # ── frequency gate ──────────────────────────────────────────────────

    def can_trigger(self) -> bool:
        log = _load_log()
        if log.get("manually_disabled_until"):
            return False
        if log.get("cooldown_active"):
            return False
        if log.get("today_count", 0) >= SPONTANEITY_LIMITS["max_per_day"]:
            return False
        if log.get("week_count", 0) >= SPONTANEITY_LIMITS["max_per_week"]:
            return False
        last_triggered = log.get("last_triggered")
        if last_triggered:
            try:
                hours_since = (_now() - datetime.datetime.fromisoformat(last_triggered)).total_seconds() / 3600
                if hours_since < SPONTANEITY_LIMITS["min_hours_between"]:
                    return False
            except ValueError:
                pass
        return True

    # ── candidate generators ────────────────────────────────────────────
    # Each returns one candidate or None — "NOT forced" (spec, on
    # _generate_observation, but the same spirit applies to every
    # generator here): silence is the correct, common outcome, not a
    # fallback.

    def _generate_discovery(self) -> SpontaneousCandidate | None:
        """Web search during sleep around one of Joan's known interests
        (user_model.json's current_focus, falling back to the most recent
        episode topic) — a candidate only when something comes back at
        all; 'remarkable' isn't independently judged beyond that (no LLM
        call here — see module docstring), so this leans conservative on
        purpose via a below-average utility_score rather than overclaiming
        how interesting the result actually is."""
        model = _load_user_model()
        topic = next(iter(model.get("current_focus") or []), None)
        if not topic:
            episodes = _load_episodes_raw()
            topic = episodes[-1].get("topic") if episodes else None
        if not topic:
            return None
        try:
            from core import tools_search
            results = tools_search.search_web(str(topic))
        except Exception:
            logger.debug("_generate_discovery: search failed (non-critical)", exc_info=True)
            return None
        if not results:
            return None
        headline = results[0].get("title") or results[0].get("snippet") or ""
        if not headline:
            return None
        description = f"Encontré algo sobre {topic} que creo que te va a interesar."
        action = ProposedAction(
            description=description, type="inform",
            trigger=f"implicit: spontaneity discovery — {topic}",
            urgency=0.15, reversible=True, requires_interruption=False,
            estimated_value=0.5, context={"headline": headline, "topic": topic},
        )
        return SpontaneousCandidate(
            id=f"spont_{uuid.uuid4().hex[:10]}", description=description, action=action,
            utility_score=0.5, relevance_score=0.6, surprise_value=0.75,
            reversible=True, interruption_required=False, type="discovery",
        )

    def _generate_optimization(self) -> SpontaneousCandidate | None:
        """A recurring manual pattern — reuses core.situation's own
        pattern-mining (data/situation.json's 'patterns', already requiring
        >=5 observations — see core.situation.MIN_OBSERVATIONS_FOR_PATTERN)
        as the proxy for 'Joan keeps doing X manually', rather than
        tracking manual-task repetition separately."""
        try:
            from core.situation import situation_engine
            data = situation_engine._load()
        except Exception:
            return None
        patterns = [p for p in data.get("patterns", []) if p.get("observations", 0) >= 5]
        if not patterns:
            return None
        p = max(patterns, key=lambda x: x.get("observations", 0))
        description = f"¿Quieres que automatice esto? Lo he visto {p['observations']} veces."
        action = ProposedAction(
            description=description, type="suggest",
            trigger=f"implicit: spontaneity optimization — patrón {p['id']}",
            urgency=0.2, reversible=True, requires_interruption=False,
            estimated_value=0.6, context={"pattern": p["description"]},
        )
        return SpontaneousCandidate(
            id=f"spont_{uuid.uuid4().hex[:10]}", description=description, action=action,
            utility_score=0.65, relevance_score=0.7, surprise_value=0.4,
            reversible=True, interruption_required=False, type="optimization",
        )

    def _generate_preparation(self) -> SpontaneousCandidate | None:
        """Reuses core.initiative.InitiativeEngine's own predict_needs()/
        predict_next_actions() detectors (Phase 4) rather than
        re-implementing sequence/routine matching — the difference here is
        framing (a short, delight-oriented line + a much higher score
        floor via score_candidate's threshold) and delivery path
        (spontaneity's own frequency cap), not detection logic."""
        try:
            from core.initiative import initiative_engine
            candidates = initiative_engine.predict_needs() or initiative_engine.predict_next_actions()
        except Exception:
            return None
        if not candidates:
            return None
        base_action = candidates[0]
        description = "Ya te dejé esto preparado."
        action = ProposedAction(
            description=description, type="inform",
            trigger=f"implicit: spontaneity preparation — {base_action.trigger}",
            urgency=0.15, reversible=True, requires_interruption=False,
            estimated_value=base_action.estimated_value, context=base_action.context,
        )
        return SpontaneousCandidate(
            id=f"spont_{uuid.uuid4().hex[:10]}", description=description, action=action,
            utility_score=0.7, relevance_score=0.7, surprise_value=0.5,
            reversible=True, interruption_required=False, type="preparation",
        )

    def _generate_observation(self) -> SpontaneousCandidate | None:
        """Only a genuinely fresh, unresolved anomaly (core.situation's own
        Phase 2 detect_anomalies) counts — this is the one generator most
        at risk of feeling nagging if forced, so it only fires on
        something situation.json hasn't already surfaced elsewhere."""
        try:
            from core.situation import situation_engine
            anomalies = situation_engine.detect_anomalies()
        except Exception:
            return None
        if not anomalies:
            return None
        a = anomalies[0]
        description = f"Por si te sirve: {a['description']}"
        action = ProposedAction(
            description=description, type="inform",
            trigger="implicit: spontaneity observation — anomalía en situation.json",
            urgency=0.15, reversible=True, requires_interruption=False,
            estimated_value=0.4, context={"anomaly": a["description"]},
        )
        return SpontaneousCandidate(
            id=f"spont_{uuid.uuid4().hex[:10]}", description=description, action=action,
            utility_score=0.45, relevance_score=0.5, surprise_value=0.6,
            reversible=True, interruption_required=False, type="observation",
        )

    def _generate_small_gesture(self) -> SpontaneousCandidate | None:
        """An old (14-45 day), high-importance episode whose topic hasn't
        come up again since — 'must be specific to Joan, never generic'
        (spec), so this only ever surfaces real stored episode content,
        never a templated line with no actual memory behind it."""
        now = datetime.date.today()
        episodes = _load_episodes_raw()
        recent_topics = " ".join(e.get("topic", "") for e in episodes[-5:]).lower()
        for e in episodes:
            try:
                e_date = datetime.date.fromisoformat(str(e.get("date", ""))[:10])
            except ValueError:
                continue
            age_days = (now - e_date).days
            if not (14 <= age_days <= 45):
                continue
            if e.get("importance", 1) < 4:
                continue
            topic = e.get("topic", "")
            if topic and topic.lower() in recent_topics:
                continue   # already resurfaced naturally — not a surprise anymore
            description = f"Me acordé de esto: {e.get('summary', topic)}."
            action = ProposedAction(
                description=description, type="inform",
                trigger=f"implicit: spontaneity small_gesture — episodio del {e['date']}",
                urgency=0.1, reversible=True, requires_interruption=False,
                estimated_value=0.45, context={"episode_topic": topic},
            )
            return SpontaneousCandidate(
                id=f"spont_{uuid.uuid4().hex[:10]}", description=description, action=action,
                utility_score=0.4, relevance_score=0.85, surprise_value=0.8,
                reversible=True, interruption_required=False, type="small_gesture",
            )
        return None

    def generate_candidates(self) -> list[SpontaneousCandidate]:
        candidates = []
        for generator in (
            self._generate_discovery, self._generate_optimization, self._generate_preparation,
            self._generate_observation, self._generate_small_gesture,
        ):
            try:
                c = generator()
            except Exception:
                logger.warning("Spontaneity generator %s failed", generator.__name__, exc_info=True)
                c = None
            if c is not None:
                candidates.append(c)
        return candidates

    # ── scoring ──────────────────────────────────────────────────────────

    def _past_outcome_score(self, c: SpontaneousCandidate) -> float:
        """Maps the persisted, slowly-recalibrated outcome_weights[type]
        (centered near 1.0, roughly 0.6-1.4 in practice — see
        record_outcome()) into the 0..1 'similar_appreciated' scale
        score_candidate()'s own formula expects. weight=1.0 (neutral prior,
        no history yet) -> 0.5, the exact midpoint that leaves
        (0.8 + x*0.4) at 1.0 — no bonus, no penalty, until real outcomes
        exist."""
        log = _load_log()
        weight = log.get("outcome_weights", {}).get(c.type, 1.0)
        return _clamp01((weight - 0.6) / 0.8)

    def score_candidate(self, c: SpontaneousCandidate) -> float:
        base = (
            c.utility_score * 0.35 +
            c.relevance_score * 0.35 +
            c.surprise_value * 0.20 +
            (1.0 if c.reversible else 0.3) * 0.10
        )
        if c.interruption_required:
            base *= 0.7
        if not c.reversible:
            base *= 0.6
        similar_appreciated = self._past_outcome_score(c)
        base *= (0.8 + similar_appreciated * 0.4)
        return round(_clamp01(base), 4)

    # ── selection (hard rules enforced here — see SPONTANEITY_RULES) ────

    def _recent_delivered_types(self, n: int = SPONTANEITY_RULES["max_consecutive_same_type"]) -> list[str]:
        log = _load_log()
        resolved = [h for h in log.get("history", []) if h.get("candidate_type")]
        return [h["candidate_type"] for h in resolved[-n:]]

    def _is_type_unwanted(self, type_: str) -> bool:
        log = _load_log()
        return any(h.get("candidate_type") == type_ and h.get("joan_reaction") == "unwanted" for h in log.get("history", []))

    def select_best(self, candidates: list[SpontaneousCandidate]) -> SpontaneousCandidate | None:
        try:
            from core.situation import situation_engine
            joan_state = situation_engine.get_current_situation().get("joan_state")
        except Exception:
            joan_state = "unknown"

        recent_types = self._recent_delivered_types()
        blocked_by_streak = {recent_types[0]} if len(recent_types) == SPONTANEITY_RULES["max_consecutive_same_type"] and len(set(recent_types)) == 1 else set()

        survivors = []
        for c in candidates:
            if not c.reversible:
                continue   # must_be_reversible_if_unsolicited — non-negotiable
            if c.interruption_required and joan_state == "focused":
                continue   # never_interrupt_focused_state — no urgency override, unlike JudgmentEngine's own soft threshold
            if c.action.permissions_needed and (_SENSITIVE_PERMISSIONS & set(c.action.permissions_needed)):
                continue   # never_share_personal_data_spontaneously
            if self._is_type_unwanted(c.type):
                continue   # never_repeat_unwanted_type
            if c.type in blocked_by_streak:
                continue   # max_consecutive_same_type
            score = self.score_candidate(c)
            if score < c.minimum_threshold:
                continue
            survivors.append((score, c))

        if not survivors:
            return None
        survivors.sort(key=lambda t: t[0], reverse=True)
        return survivors[0][1]

    # ── main entry point ────────────────────────────────────────────────

    def consider(self) -> SpontaneousCandidate | None:
        """Returns the single best SpontaneousCandidate this cycle, or
        None. NOTE: returns the candidate wrapper (not a bare
        ProposedAction, despite the class-interface sketch's type hint) —
        callers need candidate.id for record_outcome() and candidate.type
        for the frequency/rule bookkeeping below, neither of which a bare
        ProposedAction carries. See run_spontaneity_cycle() for the actual
        integration, which mirrors the spec's own pseudocode
        ('judgment_engine.evaluate(candidate.action)')."""
        if not self.can_trigger():
            return None
        candidates = self.generate_candidates()
        return self.select_best(candidates)

    # ── outcome learning ─────────────────────────────────────────────────

    def _record_pending(self, candidate: SpontaneousCandidate, score: float, context_label: str) -> None:
        """Called by run_spontaneity_cycle() right after a candidate is
        actually queued for delivery — appends a history entry with
        joan_reaction=None (pending) and bumps today_count/week_count/
        last_triggered. record_outcome() later fills in the reaction once
        Joan's next turn is classified (see core.commands' reaction-capture
        hook)."""
        with _log_lock:
            log = _load_log()
            log["today_count"] = log.get("today_count", 0) + 1
            log["week_count"] = log.get("week_count", 0) + 1
            log["last_triggered"] = _now_iso()
            log["history"].append({
                "candidate_id":   candidate.id,
                "candidate_type": candidate.type,
                "joan_reaction":  None,
                "context":        context_label,
                "score_at_time":  score,
                "delivered_at":   _now_iso(),
            })
            _save_log_locked(log)

    def record_outcome(self, candidate_id: str, joan_reaction: str) -> None:
        """joan_reaction: 'appreciated' | 'neutral' | 'unwanted'."""
        if joan_reaction not in ("appreciated", "neutral", "unwanted"):
            logger.warning("record_outcome: unknown reaction %r", joan_reaction)
            return
        with _log_lock:
            log = _load_log()
            entry = next((h for h in log["history"] if h.get("candidate_id") == candidate_id), None)
            if entry is None:
                logger.debug("record_outcome: no matching pending entry for %r", candidate_id)
                return
            entry["joan_reaction"] = joan_reaction
            type_ = entry.get("candidate_type", "")

            if joan_reaction == "unwanted":
                log["last_unwanted"] = _now_iso()
                log["cooldown_active"] = True

            # Recalibrate outcome_weights[type] only once >=10 RESOLVED
            # outcomes of that type exist — spec: avoids wild swings from
            # one or two reactions.
            resolved = [h for h in log["history"] if h.get("candidate_type") == type_ and h.get("joan_reaction")]
            if len(resolved) >= MIN_HISTORY_FOR_RECALIBRATION:
                score_map = {"appreciated": 1.0, "neutral": 0.5, "unwanted": 0.0}
                avg = sum(score_map[h["joan_reaction"]] for h in resolved) / len(resolved)
                log["outcome_weights"][type_] = round(0.6 + 0.8 * avg, 3)

            _save_log_locked(log)


spontaneity_engine = SpontaneityEngine()


# ═══════════════════════════════════════════════════════════════════════════
# ORCHESTRATION — the spec's own integration pseudocode:
#   candidate = spontaneity_engine.consider()
#   if candidate:
#       judgment = judgment_engine.evaluate(candidate.action)
#       if judgment.decision in ["act", "suggest"]:
#           initiative_queue.add(candidate)
# 'ask'/'silence' verdicts drop the candidate — a spontaneous gesture that
# JudgmentEngine thinks needs to ask permission first, or shouldn't happen
# at all, isn't spontaneous anymore; it's just Phase 4 initiative, which
# already has its own path for that.
# ═══════════════════════════════════════════════════════════════════════════

def run_spontaneity_cycle(*, context_label: str = "") -> dict:
    """Called from scripts/reflective_mode.py's sleep cycle and from
    core.background_loops's 30-min initiative tick (see each call site's
    own comment for why those two, not a dedicated third loop — spec:
    'called during sleep and at conversation pauses', and the 30-min tick
    already only fires during idle-but-active windows, a natural pause).
    Never raises."""
    try:
        candidate = spontaneity_engine.consider()
    except Exception:
        logger.warning("run_spontaneity_cycle: consider() failed", exc_info=True)
        return {"triggered": False, "reason": "error"}

    if candidate is None:
        return {"triggered": False, "reason": "no candidate passed can_trigger/select_best"}

    try:
        judgment = judgment_engine.evaluate(candidate.action)
    except Exception:
        logger.warning("run_spontaneity_cycle: judgment evaluation failed", exc_info=True)
        return {"triggered": False, "reason": "judgment error"}

    if judgment.decision not in ("act", "suggest"):
        logger.info("[SPONTANEITY] candidate %s dropped — judgment=%s", candidate.id, judgment.decision)
        return {"triggered": False, "reason": f"judgment={judgment.decision}"}

    score = spontaneity_engine.score_candidate(candidate)
    spontaneity_engine._record_pending(candidate, score, context_label)

    from core.initiative import enqueue
    enqueue({
        "id":          candidate.id,
        "type":        judgment.decision,   # 'act' -> spoken as an inform-style note; 'suggest' -> as-is
        "description": candidate.description,
        "source":      "spontaneity",       # candidate.description is still a raw fact, not a
                                             # spoken line — core.initiative._phrase_entry rephrases
                                             # every queue entry through HUGO's voice regardless of source
        "candidate_type": candidate.type,
        "created_at":  _now_iso(),
        "expires_at":  None,
        "delivered":   False,
    })
    logger.info("[SPONTANEITY] queued candidate %s (%s, score=%.2f)", candidate.id, candidate.type, score)
    return {"triggered": True, "candidate_id": candidate.id, "type": candidate.type, "score": score}


# ═══════════════════════════════════════════════════════════════════════════
# REACTION CAPTURE — "HUGO infers Joan's reaction from: explicit phrases,
# implicit continued engagement vs topic change vs silence, emotional
# tone." Hooked from core.commands.dispatch_command: when
# core.initiative._deliver_pending_initiative() delivers an entry with
# source=='spontaneity', it calls mark_awaiting_reaction() below; the VERY
# NEXT user turn (within REACTION_CAPTURE_TTL_SECONDS) is classified and
# fed to record_outcome() — same "one message, one look" pattern as
# core.intent._pending_action's own TTL-guarded slot.
# ═══════════════════════════════════════════════════════════════════════════

_APPRECIATED_RE = re.compile(
    r"\bgracias\b|\bjusto\s+lo\s+que\s+necesitaba\b|\bqu[eé]\s+bien\b|\bperfecto\b|\bostia\b.*\bgracias\b|"
    r"\bme\s+encanta\b|\bgenial\b|\bbuena\s+idea\b",
    re.IGNORECASE,
)
_UNWANTED_RE = re.compile(
    r"\bno\s+hac[ií]a\s+falta\b|\bno\s+me\s+interesa\b|\bno\s+quiero\b|\bd[eé]jalo\b|\bpara\b\s*$|"
    r"\bno\s+hagas\s+eso\b|\bmol[eé]stame\b|\bno\s+lo\s+hagas\b",
    re.IGNORECASE,
)


def _classify_reaction(transcript: str) -> str:
    text = (transcript or "").strip()
    if not text:
        return "neutral"   # silence — see docstring below
    if _UNWANTED_RE.search(text):
        return "unwanted"
    if _APPRECIATED_RE.search(text):
        return "appreciated"
    return "neutral"   # continued engagement without an explicit signal either way


def mark_awaiting_reaction(candidate_id: str) -> None:
    global _pending_reaction
    with _pending_reaction_lock:
        _pending_reaction = {"candidate_id": candidate_id, "at": _now()}


def maybe_capture_reaction(transcript: str) -> None:
    """Called at the top of core.commands.dispatch_command, right where
    the queue-delivery hook already lives — if a spontaneous delivery is
    awaiting a reaction and this turn arrived within the TTL, classify it
    and record the outcome. A message that arrives too late (TTL expired,
    or Joan just went silent for a while) is left unclassified rather than
    guessed — record_outcome() is simply never called for it, so that
    history entry stays 'joan_reaction: null' forever (excluded from
    recalibration averages, same as any other unresolved entry)."""
    global _pending_reaction
    with _pending_reaction_lock:
        pending = _pending_reaction
        _pending_reaction = None
    if pending is None:
        return
    if (_now() - pending["at"]).total_seconds() > REACTION_CAPTURE_TTL_SECONDS:
        return
    reaction = _classify_reaction(transcript)
    try:
        spontaneity_engine.record_outcome(pending["candidate_id"], reaction)
    except Exception:
        logger.warning("maybe_capture_reaction: record_outcome failed", exc_info=True)


def get_recent_history(limit: int = 50) -> list[dict]:
    log = _load_log()
    return log.get("history", [])[-limit:][::-1]
