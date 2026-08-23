# ═══════════════════════════════════════════════════════════════════════════
# ACTION ENGINE — Proactive Intelligence Phase 4, the execution half. Only
# ever called for a ProposedAction whose JudgmentEngine verdict is "act"
# (see core/initiative.py's run_proactive_cycle — the "suggest"/"ask"
# branches never reach this file at all; they go straight into
# data/initiative_queue.json for the next conversation to surface, matched
# to the right phrasing there, not here).
#
# Same dependency-light, no-LLM discipline as core/judgment.py and
# core/situation.py — every tool handler below calls a real, already-
# existing function in this codebase (memory_select, tools_search,
# task_engine, reminders, notifications, tools_calendar); nothing here
# invents a new side channel. Two tool names in ACTION_TOOL_MAP notably
# don't map 1:1 onto a module of the same name — see _TOOL_HANDLERS' own
# comments for "discord_bridge" and "subagent_manager".
# ═══════════════════════════════════════════════════════════════════════════
import datetime
import json
import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass, field

from core.judgment import ProposedAction, JudgmentResult

logger = logging.getLogger(__name__)

ACTION_LOG_PATH   = "logs/action_engine.log"
ACTION_STORE_PATH = "data/action_engine_log.json"
MAX_LOGGED_RESULTS = 200

_log_lock = threading.Lock()

# Kind -> ordered tool preference list. select_tools() reads this after
# _classify_action() picks the kind; plan() turns tools[0] into the primary
# step and tools[1] (if any) into its fallback.
ACTION_TOOL_MAP = {
    "prepare_information": ["memory_context", "web_search"],
    "create_reminder":     ["task_engine"],
    "send_notification":   ["discord_bridge"],
    "run_background_task": ["task_engine", "subagent_manager"],
    "calendar_check":      ["calendar_skill"],
}

_ESTIMATED_DURATION = {
    "prepare_information": 5,
    "create_reminder":     2,
    "send_notification":   2,
    "run_background_task": 30,
    "calendar_check":      3,
}


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


# ═══════════════════════════════════════════════════════════════════════════
# DATA TYPES
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ActionPlan:
    action:             ProposedAction
    steps:              list[dict]     # [{"tool": str, "params": dict, "depends_on": None}]
    fallbacks:          dict           # tool -> fallback_tool
    estimated_duration: int            # seconds
    background:         bool           # runs without interrupting Joan


@dataclass
class ActionResult:
    action_id:    str
    success:      bool
    status:       str = "completed"   # completed | running | failed
    output:       str | None = None
    error:        str | None = None
    tool_used:    str | None = None
    completed_at: str = field(default_factory=_now_iso)


# ═══════════════════════════════════════════════════════════════════════════
# ACTION CLASSIFICATION — which ACTION_TOOL_MAP kind a ProposedAction maps
# to. Keyword-based on description/trigger, same coarse-but-inspectable
# style as core/intent.py's own regex classification — deliberately no LLM
# call in the execution path that follows a JudgmentEngine approval.
# ═══════════════════════════════════════════════════════════════════════════

_CALENDAR_RE   = re.compile(r"\b(calendari|evento|agenda|reuni[oó]n)\b", re.IGNORECASE)
_REMINDER_RE   = re.compile(r"\brecordatori|recu[eé]rda|no\s+olvid", re.IGNORECASE)
_NOTIFY_RE     = re.compile(r"\bcompartir|investigaci[oó]n\s+complet|curiosidad|reflexi[oó]n\s+pendiente", re.IGNORECASE)
_BACKGROUND_RE = re.compile(r"\btarea\s+bloqueada|m[oó]dulo|error\b", re.IGNORECASE)
# detect_help_opportunities' own "Posible error de módulo: ..." / "Tarea
# bloqueada: ..." descriptions (core/initiative.py) are already reports
# ABOUT an existing problem, not a new unit of work — but they contain the
# same "módulo"/"tarea bloqueada" keywords _BACKGROUND_RE matches on, so
# without this exclusion every one of them got reclassified as
# run_background_task and spawned a brand-new task_engine task whose goal
# was the report text itself (observed 2026-08-13: 25 of 27 entries in
# data/tasks.json were these, task_003 onward). Excluded here so they fall
# through to the prepare_information default instead — informational only,
# creates nothing.
_SELF_REPORT_RE = re.compile(r"^(Posible error de m[oó]dulo|Tarea bloqueada):", re.IGNORECASE)

# Same 'explicit:'/'implicit:' trigger-prefix convention as
# core.judgment.ProposedAction/JudgmentEngine._request_provenance — read
# again here (not imported from there) since it's a plain text convention,
# not shared state, and action_engine has no other reason to depend on
# JudgmentEngine internals.
_EXPLICIT_TRIGGER_RE = re.compile(r"^\s*explicit\s*:", re.IGNORECASE)


def _is_explicit(action: ProposedAction) -> bool:
    return bool(_EXPLICIT_TRIGGER_RE.match(action.trigger or ""))


def _classify_action(action: ProposedAction) -> str:
    text = f"{action.description} {action.trigger}"
    if _CALENDAR_RE.search(text):
        return "calendar_check"
    if _REMINDER_RE.search(text):
        return "create_reminder"
    if _NOTIFY_RE.search(text):
        return "send_notification"
    if action.type == "execute" or (_BACKGROUND_RE.search(text) and not _SELF_REPORT_RE.match(action.description or "")):
        return "run_background_task"
    return "prepare_information"


class ActionEngine:

    # ── planning ─────────────────────────────────────────────────────────

    def select_tools(self, action: ProposedAction) -> list[str]:
        kind = _classify_action(action)
        return list(ACTION_TOOL_MAP.get(kind, ["memory_context"]))

    def plan(self, action: ProposedAction) -> ActionPlan:
        kind  = _classify_action(action)
        tools = self.select_tools(action)
        primary = tools[0] if tools else "memory_context"
        fallbacks = {primary: tools[1]} if len(tools) > 1 else {}
        steps = [{"tool": primary, "params": {"action": action}, "depends_on": None}]
        return ActionPlan(
            action=action,
            steps=steps,
            fallbacks=fallbacks,
            estimated_duration=_ESTIMATED_DURATION.get(kind, 5),
            background=not action.requires_interruption,
        )

    # ── tool execution ───────────────────────────────────────────────────

    def _run_step(self, step: dict) -> ActionResult:
        tool = step["tool"]
        action: ProposedAction = step["params"]["action"]
        handler = _TOOL_HANDLERS.get(tool)
        if handler is None:
            return ActionResult(action_id="", success=False, status="failed",
                                 error=f"unknown tool: {tool!r}", tool_used=tool)
        try:
            return handler(action)
        except Exception as e:
            logger.warning("Action tool %r failed", tool, exc_info=True)
            return ActionResult(action_id="", success=False, status="failed", error=str(e), tool_used=tool)

    def run_background(self, action: ProposedAction) -> str:
        """Spawns the plan's steps on a daemon thread and returns
        immediately with an action_id — the caller (execute()) doesn't
        wait for completion; the thread logs its own final ActionResult
        when done, and enqueues an 'inform' entry into
        data/initiative_queue.json so Joan hears about it at the next
        natural pause (see communicate_result())."""
        action_id = f"action_{uuid.uuid4().hex[:10]}"

        def _worker():
            plan = self.plan(action)
            result = None
            for step in plan.steps:
                result = self._run_step(step)
                if not result.success:
                    fallback = plan.fallbacks.get(step["tool"])
                    if fallback:
                        result = self._run_step({**step, "tool": fallback})
                if not result.success:
                    result = self.handle_failure(action, result.error or "unknown error")
                    break
            result.action_id = action_id
            if result.status != "failed":
                result.status = "completed"
                self.verify_result(result)
            self._log(action, None, result)
            self.communicate_result(result, None, background=True, action=action)

        threading.Thread(target=_worker, daemon=True, name=f"action-{action_id}").start()
        return action_id

    # ── main entry point ────────────────────────────────────────────────

    def execute(self, action: ProposedAction, judgment: JudgmentResult) -> ActionResult:
        plan = self.plan(action)
        action_id = f"action_{uuid.uuid4().hex[:10]}"

        if plan.background:
            bg_id = self.run_background(action)
            result = ActionResult(action_id=bg_id, success=True, status="running",
                                   output="ejecutando en segundo plano", tool_used=plan.steps[0]["tool"])
            self._log(action, judgment, result)
            return result

        result = None
        for step in plan.steps:
            result = self._run_step(step)
            if not result.success:
                fallback = plan.fallbacks.get(step["tool"])
                if fallback:
                    result = self._run_step({**step, "tool": fallback})
            if not result.success:
                result = self.handle_failure(action, result.error or "unknown error")
                result.action_id = action_id
                self.communicate_result(result, judgment, background=False, action=action)
                self._log(action, judgment, result)
                return result

        result.action_id = action_id
        if not self.verify_result(result):
            result.success = False
            result.status  = "failed"
            result.error   = (result.error or "") + " (resultado no verificado)"
        self.communicate_result(result, judgment, background=False, action=action)
        self._log(action, judgment, result)
        return result

    def verify_result(self, result: ActionResult) -> bool:
        """Light sanity check, not a re-execution — a successful tool call
        that produced no output at all (for a tool that's supposed to
        produce one) is treated as unverified. task_engine/subagent_manager
        calls succeed by their side effect (a task/subagent record was
        created), so an empty output there is expected, not suspicious."""
        if not result.success:
            return False
        if result.tool_used in ("task_engine", "subagent_manager"):
            return True
        return bool(result.output)

    def handle_failure(self, action: ProposedAction, error: str) -> ActionResult:
        logger.info("Action failed (%s): %s", action.description, error)
        return ActionResult(action_id="", success=False, status="failed", error=error)

    def communicate_result(
        self, result: ActionResult, judgment: JudgmentResult | None, *,
        background: bool, action: ProposedAction | None = None,
    ) -> None:
        """How HUGO tells Joan about an 'act' outcome — the only decision
        that ever reaches here (suggest/ask are queued directly by
        core.initiative.run_proactive_cycle, never executed). A failed
        proactive (implicit) action stays completely silent, per spec —
        only a failure on an EXPLICITLY requested action gets surfaced,
        and even then only as a queued/spoken note, never a long
        explanation of why it failed."""
        if not result.success:
            if action is not None and _is_explicit(action):
                if background:
                    from core.initiative import enqueue
                    enqueue({
                        "id": f"init_{uuid.uuid4().hex[:10]}", "type": "inform",
                        "description": f"No pude completar: {action.description}.",
                        "created_at": _now_iso(), "expires_at": None, "delivered": False,
                    })
                else:
                    try:
                        import core.background_loops as background_loops
                        import core.personality as personality_mod
                        from core import response as response_mod
                        with personality_mod._personality_lock:
                            personality = personality_mod._personality
                        # Phrased naturally (see feedback_no_hardcoded_replies
                        # memory) — this used to be a fixed f-string spoken
                        # verbatim every time.
                        spoken = response_mod._format_response(
                            f"No pude completar: {action.description}.",
                            personality=personality,
                        )
                        background_loops._speak_unprompted(personality, spoken)
                    except Exception:
                        logger.debug("communicate_result: failure speak failed (non-critical)", exc_info=True)
            return   # silent failure otherwise — per spec
        if background:
            # Short completion notice only ("ya tengo listo lo que preparé
            # sobre X") — matches the spec's own example ('Por cierto, ya
            # tengo el resumen listo', not the resumen itself dumped
            # verbatim). The actual prepared content (result.output) rides
            # along as 'detail' — inspectable via GET /api/initiative/queue
            # and GET /api/action-engine/log, never spoken outright.
            from core.initiative import enqueue
            short_desc = action.description if action is not None else "una tarea"
            enqueue({
                "id":          f"init_{uuid.uuid4().hex[:10]}",
                "type":        "inform",
                "description": f"ya tengo listo lo que preparé sobre: {short_desc}",
                "detail":      result.output,
                "created_at":  _now_iso(),
                "expires_at":  None,
                "delivered":   False,
            })
        else:
            # Foreground 'act' only happens for high-urgency, already-
            # judgment-approved actions (see JudgmentEngine._final_decision:
            # action.type=='execute' and urgency>0.7) — one short sentence,
            # spoken immediately, same channel as core.background_loops'
            # other unprompted speech.
            try:
                import core.background_loops as background_loops
                import core.personality as personality_mod
                from core import response as response_mod
                with personality_mod._personality_lock:
                    personality = personality_mod._personality
                # Phrased naturally (see feedback_no_hardcoded_replies
                # memory) — result.output is frequently raw data (an id, a
                # joined list, a flat action.description) rather than
                # already-natural prose, and the "Hecho." fallback used to
                # be spoken as a fixed string verbatim.
                spoken = response_mod._format_response(result.output or "Hecho.", personality=personality)
                background_loops._speak_unprompted(personality, spoken)
            except Exception:
                logger.debug("communicate_result: foreground speak failed (non-critical)", exc_info=True)

    # ── logging ──────────────────────────────────────────────────────────

    def _log(self, action: ProposedAction, judgment: JudgmentResult | None, result: ActionResult) -> None:
        try:
            os.makedirs(os.path.dirname(ACTION_LOG_PATH) or ".", exist_ok=True)
            with open(ACTION_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"[{_now_iso()}] {result.status.upper()} — \"{action.description}\" (tool={result.tool_used})\n")
                if result.error:
                    f.write(f"  error: {result.error}\n")
        except Exception:
            logger.warning("Failed to write logs/action_engine.log", exc_info=True)

        entry = {
            "at":          _now_iso(),
            "action_id":   result.action_id,
            "description": action.description,
            "status":      result.status,
            "success":     result.success,
            "tool_used":   result.tool_used,
            "output":      result.output,
            "error":       result.error,
        }
        try:
            with _log_lock:
                try:
                    with open(ACTION_STORE_PATH, "r", encoding="utf-8") as f:
                        entries = json.load(f)
                except (FileNotFoundError, json.JSONDecodeError, OSError):
                    entries = []
                if not isinstance(entries, list):
                    entries = []
                entries.append(entry)
                entries = entries[-MAX_LOGGED_RESULTS:]
                os.makedirs(os.path.dirname(ACTION_STORE_PATH) or ".", exist_ok=True)
                with open(ACTION_STORE_PATH, "w", encoding="utf-8") as f:
                    json.dump(entries, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.warning("Failed to write data/action_engine_log.json", exc_info=True)


def get_recent_results(limit: int = 50) -> list[dict]:
    try:
        with open(ACTION_STORE_PATH, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    if not isinstance(entries, list):
        return []
    return entries[-limit:][::-1]


# ═══════════════════════════════════════════════════════════════════════════
# TOOL HANDLERS — one per ACTION_TOOL_MAP entry. Each takes the
# ProposedAction and returns an ActionResult; action_id/status are filled
# in by the caller (run_background/execute), not here.
# ═══════════════════════════════════════════════════════════════════════════

def _tool_memory_context(action: ProposedAction) -> ActionResult:
    """'prepare_information' primary tool — pulls relevant facts (Layer 1/2,
    same pool core.personalities.base uses per-turn) for the action's own
    description, so the prepared text is ready before Joan asks. Empty
    result is treated as a soft failure so plan()'s web_search fallback
    gets a chance instead."""
    from core import memory_select
    pool = memory_select._load_shared_facts()
    try:
        import core.personality as personality_mod
        with personality_mod._personality_lock:
            personality = personality_mod._personality
        pool += memory_select._load_personality_facts(personality)
    except Exception:
        pass
    facts = memory_select._select_relevant_facts(action.description, pool)
    block = memory_select._format_relevant_facts_block(facts)
    return ActionResult(action_id="", success=bool(block), output=block or None, tool_used="memory_context")


def _tool_web_search(action: ProposedAction) -> ActionResult:
    from core import tools_search
    results = tools_search.search_web(action.description)
    if not results:
        return ActionResult(action_id="", success=False, error="sin resultados de búsqueda", tool_used="web_search")
    return ActionResult(action_id="", success=True, output=tools_search.format_search_results(results), tool_used="web_search")


def _tool_task_engine(action: ProposedAction) -> ActionResult:
    """Serves BOTH 'create_reminder' and 'run_background_task' kinds
    (see ACTION_TOOL_MAP) — the two are told apart by _classify_action's
    kind, re-derived here since the tool name alone doesn't carry it."""
    kind = _classify_action(action)
    if kind == "create_reminder":
        from core import reminders as reminders_mod
        import core.personality as personality_mod
        with personality_mod._personality_lock:
            personality = personality_mod._personality
        # trigger_type='session' — delivered at the next real interaction
        # via reminders._deliver_session_reminders, the exact "next natural
        # conversation pause" semantics this action wants.
        reminders_mod._add_reminder(action.description, personality, "session", None)
        return ActionResult(action_id="", success=True, output=action.description, tool_used="task_engine")

    from core.task_engine import task_engine
    task_id = task_engine.create_task(
        goal=action.description, steps=[action.description], priority=2, created_by="hugo",
    )
    return ActionResult(action_id="", success=True, output=task_id, tool_used="task_engine")


def _tool_subagent_manager(action: ProposedAction) -> ActionResult:
    """Fallback for 'run_background_task' if task_engine's own create_task
    somehow fails — queues the same goal as a one-off task the subagent
    manager's next run_pending() pass will pick up (see
    core.subagent.SubagentManager), rather than duplicating its dispatch
    logic here."""
    from core.task_engine import task_engine
    task_id = task_engine.create_task(
        goal=action.description, steps=[action.description], priority=3, created_by="hugo",
    )
    return ActionResult(action_id="", success=True, output=task_id, tool_used="subagent_manager")


def _tool_discord_bridge(action: ProposedAction) -> ActionResult:
    """ACTION_TOOL_MAP names this 'discord_bridge', but core/discord_bridge.py
    is a reactive DM chat-bot (generate_reply per incoming message) with no
    synchronous outbound-push API safe to call from a background thread —
    there's no live discord.Client instance to send through here. The real
    'tell Joan something proactively, she'll see it next time she looks'
    primitive that already exists in this codebase is
    core.notifications.create_notification (GET /api/notifications +
    voice delivery via _deliver_pending_notifications), so that's what this
    handler actually calls."""
    from core import notifications
    notifications.create_notification("initiative", action.description, action.description)
    return ActionResult(action_id="", success=True, output=action.description, tool_used="discord_bridge")


def _tool_calendar_skill(action: ProposedAction) -> ActionResult:
    from core import tools_calendar
    events = tools_calendar.get_today_events()
    if not events:
        return ActionResult(action_id="", success=True, output="Sin eventos hoy.", tool_used="calendar_skill")
    lines = [f"{e.get('time', '')} — {e.get('title', '')}" for e in events]
    return ActionResult(action_id="", success=True, output="; ".join(lines), tool_used="calendar_skill")


_TOOL_HANDLERS = {
    "memory_context":     _tool_memory_context,
    "web_search":         _tool_web_search,
    "task_engine":        _tool_task_engine,
    "subagent_manager":   _tool_subagent_manager,
    "discord_bridge":     _tool_discord_bridge,
    "calendar_skill":     _tool_calendar_skill,
}


action_engine = ActionEngine()
