# TASK ENGINE — persistent, multi-session task tracking (data/tasks.json).
# Inspired by the Hermes Agent architecture: a goal broken into ordered
# steps, advanced one step at a time, surviving across restarts/sleep
# cycles/conversations until it's completed, blocked, or failed.
#
# Distinct from core/investigations.py: investigations are open-ended
# QUESTIONS LIRA reasons about autonomously during sleep (Incubación),
# accumulating hypotheses with no fixed step list. Tasks are discrete,
# step-based GOALS with an explicit completion criterion, created by Joan
# (or LIRA) and advanced one step at a time — a project-tracking
# primitive, not a research one. The two files are intentionally separate
# and never merged; nothing here reads or writes data/investigations.json.
#
# advance_task() is deliberately a bookkeeping primitive, not an execution
# engine: it marks the current step done (with a caller-supplied result,
# or a generic placeholder) and moves the pointer to the next step. It
# does not call an LLM and does not decide HOW to do a step's work — that
# still belongs to Subagent components this file doesn't implement. Until
# those exist, "advancing" a task during sleep just records that progress
# happened; a human (or a future executor) supplies the real step content
# via the `result` argument / POST /api/tasks.
#
# core.skill_forge IS wired in now, at both places a task can reach
# 'completed' (advance_task()'s own natural last-step completion, and
# complete_task()'s manual override) — see _forge_skill_if_applicable().
# It turns a completed task's steps into reusable procedural knowledge
# (data/procedural_skills.json), completely separate from the runnable
# skills/ modules core.module_manager tracks. create_task() also queries
# it for relevant past knowledge before a new task even starts.
#
# Touches nothing under personality/memory/conversation. The other systems
# this imports are core.notifications — an existing, producer-agnostic
# queue (core.notifications._deliver_pending_notifications is already
# called at the top of every dispatch_command(), independent of who
# created the notification), which is how a completed/blocked task or a
# sleep-time advance reaches Joan in the next conversation without this
# file touching commands.py/session.py/personality.py at all — and
# core.skill_forge itself (lazy-imported inside methods, to avoid a
# circular top-level import since skill_forge reaches back into this file
# too).
import datetime
import json
import logging
import os
import re
import threading

from core import notifications as notifications_mod

logger = logging.getLogger(__name__)

TASKS_PATH = "data/tasks.json"

VALID_TASK_STATUSES = ("pending", "in_progress", "blocked", "completed", "failed")


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


class TaskEngine:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    # ── persistence ──────────────────────────────────────────────────────

    def _load(self) -> dict:
        try:
            with open(TASKS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {"tasks": []}
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            return {"tasks": []}
        return data

    def _save_locked(self, data: dict) -> None:
        """Caller must hold self._lock."""
        os.makedirs(os.path.dirname(TASKS_PATH) or ".", exist_ok=True)
        with open(TASKS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _next_id_locked(self, data: dict) -> str:
        nums = []
        for t in data["tasks"]:
            m = re.match(r"task_(\d+)$", str(t.get("id", "")))
            if m:
                nums.append(int(m.group(1)))
        return f"task_{(max(nums) + 1) if nums else 1:03d}"

    def _find_locked(self, data: dict, task_id: str) -> dict | None:
        return next((t for t in data["tasks"] if t.get("id") == task_id), None)

    # ── lifecycle ────────────────────────────────────────────────────────

    def create_task(self, goal: str, steps: list, priority: int = 1, created_by: str = "joan") -> str:
        """`steps` may be a list of plain description strings, or already-
        shaped {"description": ...} dicts (e.g. round-tripped from the
        API) — either way every step starts 'pending' except the first,
        which starts 'in_progress' the moment advance_task() first touches
        it (see there)."""
        step_dicts = []
        for i, s in enumerate(steps, start=1):
            if isinstance(s, dict):
                step_dicts.append({
                    "id":          s.get("id", i),
                    "description": s.get("description", ""),
                    "status":      s.get("status", "pending"),
                    "result":      s.get("result"),
                })
            else:
                step_dicts.append({"id": i, "description": str(s), "status": "pending", "result": None})

        # Procedural-knowledge lookup — best-effort, pure tag-overlap
        # matching (see core.skill_forge.SkillForge.find_relevant_skills),
        # no LLM call, so it's safe on this synchronous path. Stashed into
        # context_snapshot so anything that later works this task (a human,
        # or core.code_engine.CodeEngine — see its own prompt builders) can
        # see what LIRA already learned from similar past tasks.
        relevant_skills = []
        try:
            from core.skill_forge import skill_forge, _simple_tags
            relevant_skills = skill_forge.find_relevant_skills(goal, _simple_tags(goal))
        except Exception:
            logger.error("TaskEngine: find_relevant_skills failed for goal=%r", goal, exc_info=True)

        with self._lock:
            data = self._load()
            task_id = self._next_id_locked(data)
            now = _now_iso()
            task = {
                "id":               task_id,
                "goal":             goal,
                "status":           "pending",
                "priority":         priority,
                "created_at":       now,
                "updated_at":       now,
                "steps":            step_dicts,
                "current_step":     step_dicts[0]["id"] if step_dicts else None,
                "context_snapshot": {"relevant_skills": relevant_skills} if relevant_skills else {},
                "created_by":       created_by,
                "blocked_reason":   None,
            }
            data["tasks"].append(task)
            self._save_locked(data)
        return task_id

    def advance_task(self, task_id: str, result: str | None = None) -> dict:
        """Marks the current step 'completed' (storing `result`, or a
        generic placeholder if none given) and moves to the next pending
        step — or completes the task if that was the last one. Returns a
        summary dict; never raises."""
        with self._lock:
            data = self._load()
            task = self._find_locked(data, task_id)
            if task is None:
                return {"ok": False, "error": "task not found"}
            if task["status"] in ("completed", "failed"):
                return {"ok": False, "error": f"task already {task['status']}"}

            steps = task["steps"]
            idx = next((i for i, s in enumerate(steps) if s["id"] == task["current_step"]), None)
            if idx is None:
                return {"ok": False, "error": "no current step to advance"}

            step = steps[idx]
            step["status"] = "completed"
            step["result"] = result if result is not None else (step.get("result") or "Completado.")

            task_completed = idx + 1 >= len(steps)
            if task_completed:
                task["status"]       = "completed"
                task["current_step"] = None
            else:
                next_step = steps[idx + 1]
                next_step["status"]  = "in_progress"
                task["status"]       = "in_progress"
                task["current_step"] = next_step["id"]

            task["blocked_reason"] = None
            task["updated_at"]     = _now_iso()
            goal                   = task["goal"]
            steps_completed        = idx + 1
            total_steps             = len(steps)
            self._save_locked(data)

        if task_completed:
            notifications_mod.create_notification(
                "task", f"Tarea completada: {goal}", f"Tarea completada: {goal}.",
            )
            self._forge_skill_if_applicable(task_id)

        return {
            "ok":              True,
            "task_id":         task_id,
            "status":          "completed" if task_completed else "in_progress",
            "step_completed":  step["description"],
            "steps_completed": steps_completed,
            "total_steps":     total_steps,
            "current_step":    None if task_completed else steps[idx + 1]["id"],
            "task_completed":  task_completed,
        }

    def block_task(self, task_id: str, reason: str) -> bool:
        with self._lock:
            data = self._load()
            task = self._find_locked(data, task_id)
            if task is None:
                return False
            task["status"]         = "blocked"
            task["blocked_reason"] = reason
            task["updated_at"]     = _now_iso()
            goal, current_step     = task["goal"], task.get("current_step")
            self._save_locked(data)

        notifications_mod.create_notification(
            "task", f"Tarea bloqueada: {goal}",
            f"{goal}: bloqueado en paso {current_step}, necesito tu input.",
        )
        return True

    def complete_task(self, task_id: str) -> bool:
        """Manual override — marks the task (and any still-open steps)
        done outright, regardless of how many steps remain. Distinct from
        advance_task()'s own natural completion when the last step
        finishes; both end up in the same 'completed' state and fire the
        same notification."""
        with self._lock:
            data = self._load()
            task = self._find_locked(data, task_id)
            if task is None:
                return False
            for s in task["steps"]:
                if s["status"] != "completed":
                    s["status"] = "completed"
                    if s.get("result") is None:
                        s["result"] = "Completado."
            task["status"]         = "completed"
            task["current_step"]   = None
            task["blocked_reason"] = None
            task["updated_at"]     = _now_iso()
            goal = task["goal"]
            self._save_locked(data)

        notifications_mod.create_notification(
            "task", f"Tarea completada: {goal}", f"Tarea completada: {goal}.",
        )
        self._forge_skill_if_applicable(task_id)
        return True

    def _forge_skill_if_applicable(self, task_id: str) -> None:
        """Called automatically whenever a task reaches 'completed' — both
        here (the explicit override) and from advance_task()'s own natural
        last-step completion, since that's how most real completions
        actually happen (e.g. during a sleep cycle — see
        advance_during_sleep()). Best-effort; a SkillForge failure must
        never break task completion itself."""
        try:
            from core.skill_forge import skill_forge
            skill_id = skill_forge.forge_from_task(task_id)
            if skill_id:
                logger.info(f"Skill forged: {skill_id}")
        except Exception:
            logger.error("TaskEngine: forge_from_task failed for %s", task_id, exc_info=True)

    def fail_task(self, task_id: str, reason: str) -> bool:
        """Also backs POST /api/tasks/<id>/cancel — a cancellation is just
        a fail with a caller-supplied reason. `reason` is stored in
        blocked_reason (the schema has no separate failed-reason field;
        it's repurposed here as "why this task isn't running", which
        applies just as well to failed as to blocked)."""
        with self._lock:
            data = self._load()
            task = self._find_locked(data, task_id)
            if task is None:
                return False
            task["status"]         = "failed"
            task["blocked_reason"] = reason
            task["updated_at"]     = _now_iso()
            self._save_locked(data)
        return True

    # ── subagent delegation ──────────────────────────────────────────────
    # Deliberate and explicit — nothing in advance_task()/advance_during_sleep()
    # decides on its own that a step "needs" subagents; per core.subagent's
    # own "no subagents outside sleep cycles without explicit request" rule,
    # a caller (Joan, or a future planning step LIRA runs with Joan's
    # go-ahead) has to call spawn_subagents_for_step() itself.

    def spawn_subagents_for_step(self, task_id: str, subagent_specs: list) -> list:
        """Queues `subagent_specs` (see core.subagent.SubagentManager.
        spawn_parallel's own docstring for the shape) against the task's
        CURRENT step, and marks that step as waiting rather than
        completing it — resolve_pending_subagent_steps() (called from
        scripts/reflective_mode.py's sleep phase, after
        core.subagent.subagent_manager.run_pending() has had a chance to
        actually execute them) picks the step back up once every
        referenced subagent reaches a terminal state. Returns the spawned
        subagent ids, or [] if the task/step doesn't exist."""
        with self._lock:
            data = self._load()
            task = self._find_locked(data, task_id)
            if task is None:
                return []
            idx = next((i for i, s in enumerate(task["steps"]) if s["id"] == task.get("current_step")), None)
            if idx is None:
                return []

        from core.subagent import subagent_manager
        specs = [{**s, "parent_task_id": task_id} for s in subagent_specs]
        ids = subagent_manager.spawn_parallel(specs)

        with self._lock:
            data = self._load()
            task = self._find_locked(data, task_id)
            if task is None:
                return ids
            idx = next((i for i, s in enumerate(task["steps"]) if s["id"] == task.get("current_step")), None)
            if idx is not None:
                task["steps"][idx]["result"] = json.dumps({"pending_subagent_ids": ids})
                task["steps"][idx]["status"] = "in_progress"
                task["status"]     = "in_progress"
                task["updated_at"] = _now_iso()
                self._save_locked(data)
        return ids

    def resolve_pending_subagent_steps(self) -> int:
        """Called from scripts/reflective_mode.py's sleep phase, after
        subagent_manager.run_pending() runs. Finds every current step
        whose result marks it waiting on subagent ids; once ALL of them
        have reached a terminal state (completed/failed/timeout/cancelled
        — a failed or timed-out subagent does NOT block this, it's just
        folded into the summary), calls advance_task() with an aggregated
        summary of their results so the task actually moves forward — that
        summary becomes this step's real result and is what the next
        step's prompt/context sees. A step still waiting on at least one
        'pending'/'running' subagent is left alone for the next cycle.
        Returns how many steps were resolved."""
        from core.subagent import subagent_manager

        with self._lock:
            tasks_snapshot = [dict(t) for t in self._load()["tasks"]]

        resolved = 0
        for task in tasks_snapshot:
            if task.get("status") not in ("pending", "in_progress"):
                continue
            idx = next((i for i, s in enumerate(task["steps"]) if s["id"] == task.get("current_step")), None)
            if idx is None:
                continue
            step = task["steps"][idx]
            try:
                marker = json.loads(step.get("result") or "")
            except (json.JSONDecodeError, TypeError):
                marker = None
            if not isinstance(marker, dict) or "pending_subagent_ids" not in marker:
                continue

            sub_results = [subagent_manager.get_result(sid) for sid in marker["pending_subagent_ids"]]
            if any((not r) or r.get("status") in ("pending", "running") for r in sub_results):
                continue   # still waiting on at least one

            summary = "; ".join(
                f"{r.get('type')}: {r.get('result') or ('[' + r.get('status', '') + ']')}"
                for r in sub_results if r
            )
            self.advance_task(task["id"], result=summary or "Subagentes completados sin resultado.")
            resolved += 1
        return resolved

    # ── queries ──────────────────────────────────────────────────────────

    def get_all_tasks(self) -> list:
        """Every task regardless of status — backs GET /api/tasks. Not in
        the original method list, but needed to implement that endpoint
        (get_pending_tasks() below deliberately excludes completed/failed/
        blocked tasks, which the API still needs to show)."""
        with self._lock:
            return list(self._load()["tasks"])

    def get_pending_tasks(self) -> list:
        """Tasks still actionable by advance_task() — 'pending' or
        'in_progress' — sorted by priority (lower number = higher
        priority, same convention as core.module_manager's manifests).
        Excludes 'blocked' (needs Joan, not the engine) and terminal
        statuses."""
        tasks = [t for t in self.get_all_tasks() if t.get("status") in ("pending", "in_progress")]
        tasks.sort(key=lambda t: t.get("priority", 999))
        return tasks

    def get_task_status(self, task_id: str) -> dict:
        with self._lock:
            data = self._load()
        task = self._find_locked(data, task_id)
        return dict(task) if task else {}

    # ── sleep / wakeup integration ──────────────────────────────────────

    def advance_during_sleep(self) -> dict:
        """Called once per sleep cycle from scripts/reflective_mode.py.
        Advances the single highest-priority actionable task by exactly
        one step — independent of the Sleep System's own token budget, so
        it still runs even on a cycle where the 8-phase session itself was
        skipped. Queues a proactive notification either way (partial
        progress or full completion) so LIRA can mention it next time Joan
        talks to her, without this file touching the conversation layer
        directly."""
        pending = self.get_pending_tasks()
        if not pending:
            return {"ok": False, "reason": "no actionable tasks"}

        task   = pending[0]   # already priority-sorted
        result = self.advance_task(task["id"], result="Avance automático durante el sueño.")
        if result.get("ok") and not result.get("task_completed"):
            notifications_mod.create_notification(
                "task", f"Avance en tarea: {task['goal']}",
                f"Avancé en '{task['goal']}' — completé el paso "
                f"{result['steps_completed']} de {result['total_steps']}.",
            )
        return result

    def resume_on_wakeup(self) -> None:
        """Called once at server start (see core.server.start()). Logs
        every in-progress task's state — purely diagnostic; the "Avancé en
        X..." summary Joan actually hears comes from the notification
        advance_during_sleep() already queued while sleeping (see above),
        delivered the same way on her next real message regardless of
        whether the server restarted in between."""
        tasks = [t for t in self.get_all_tasks() if t.get("status") == "in_progress"]
        if not tasks:
            logger.info("[TASKS] resume_on_wakeup — no in-progress tasks")
            return
        for t in tasks:
            logger.info(
                "[TASKS] resume_on_wakeup — %s (%r) at step %s/%s",
                t.get("id"), t.get("goal"), t.get("current_step"), len(t.get("steps", [])),
            )


task_engine = TaskEngine()
