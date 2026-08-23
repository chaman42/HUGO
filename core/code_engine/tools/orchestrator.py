# TOOL ORCHESTRATOR — the one place in this package that chains multiple
# tools together automatically: plan -> checkpoint -> execute each step ->
# debug/retry/replan on failure -> final test -> commit -> escalate to
# Joan if it gets stuck. This is genuinely autonomous — it decides which
# tool method to call and with what arguments via an LLM, not a human
# reviewing each step.
#
# The safety argument this whole file rests on: Orchestrator has NO
# elevated access. Every tool method it calls (via _execute_step's
# getattr(tool, method)(**args)) still runs that tool's own
# check_permission() call internally — FileSystem/Editor/Git/Shell/
# DependencyManager/Testing/Debugger/CheckpointManager all already refuse
# on their own if the operation or path isn't allowed. If the LLM
# proposes calling file_system.delete() while 'delete' is False (the
# default), that call still just returns False — nothing here bypasses
# or elevates past what a direct caller could already do. This file adds
# planning/retry/escalation logic on TOP of that existing gate, never
# around it.
#
# Safety addition beyond the literal spec: MAX_TOTAL_STEP_ATTEMPTS caps
# the WHOLE goal's total step-execution attempts, not just each
# individual step's. _handle_failure()'s "replan" path effectively
# resets a step's own per-step attempt counter (replan() replaces the
# failed step with new step objects) — without a goal-wide cap, a
# persistently-failing goal could retry/replan forever. This bounds
# execute_goal() to always terminate.
import datetime
import inspect
import json
import logging
import re
import time

from core.code_engine.tool_base import CodeEngineTool
from core.code_engine.permissions import check_permission

logger = logging.getLogger("code_engine")

MAX_STEP_ATTEMPTS = 3          # per-step escalation threshold (core.subagent/task_engine use the same "3 strikes" convention)
MAX_TOTAL_STEP_ATTEMPTS = 20   # goal-wide hard cap — guarantees execute_goal() always terminates
# Wall-clock cap, independent of MAX_TOTAL_STEP_ATTEMPTS — that bounds
# RETRY COUNT, not elapsed time, and on CPU-only Ollama a single LLM call
# can itself legitimately take several minutes (measured directly: a real
# plan-generation call took 388s; some step-execution calls took 700s+
# before completing). A handful of slow-but-successful calls could
# together run for a very long time without ever tripping the attempt
# cap. 45 minutes is generous enough for a genuinely slow multi-step goal
# on this hardware while still guaranteeing the cycle eventually gives up
# and reports back rather than running indefinitely.
MAX_GOAL_WALL_CLOCK_SECONDS = 45 * 60

_STEP_ACTION_CONTEXT = (
    "Eres un asistente que traduce un paso de un plan en UNA llamada concreta a una "
    "herramienta, en español. Responde SOLO con un JSON: "
    '{"method": "<nombre_del_método>", "args": {...}}. Usa ÚNICAMENTE métodos que '
    "existan en la lista dada, con los argumentos que necesiten. Sin texto fuera del JSON."
)


def _extract_json(raw: str) -> dict | None:
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    try:
        data = json.loads(m.group(0) if m else raw)
    except (json.JSONDecodeError, AttributeError):
        return None
    return data if isinstance(data, dict) else None


def _llm_call(prompt: str) -> str:
    return _llm_call_with_context(prompt, _STEP_ACTION_CONTEXT)


def _llm_call_with_context(prompt: str, context: str) -> str:
    try:
        from core.code_engine import LLMRouter
        import core.ollama_control as ollama_control
        ollama_control.ensure_ollama_daemon_running()
        try:
            return LLMRouter().generate_code(prompt, context) or ""
        finally:
            ollama_control.kill_llama_server()
    except Exception:
        logger.error("Orchestrator: LLM call failed", exc_info=True)
        return ""


class ToolOrchestrator(CodeEngineTool):
    name = "orchestrator"
    description = "Ejecuta un objetivo de forma autónoma: planifica, ejecuta, depura y confirma — dentro del sistema de permisos existente."
    version = "1.0"

    def ping(self) -> bool:
        return True

    # ── tool selection / step execution ─────────────────────────────────

    def _select_tool(self, step: dict):
        from core.code_engine.tool_manager import tool_manager
        name = (step or {}).get("tool")
        return tool_manager.get_tool(name) if name else None

    def _describe_tool_methods(self, tool) -> str:
        """Public methods only, with signatures — fed to the LLM so it
        only ever proposes calls that actually exist on this tool."""
        lines = []
        for attr_name in dir(tool):
            if attr_name.startswith("_"):
                continue
            attr = getattr(tool, attr_name)
            if not callable(attr):
                continue
            try:
                sig = str(inspect.signature(attr))
            except (TypeError, ValueError):
                sig = "(...)"
            lines.append(f"{attr_name}{sig}")
        return "\n".join(lines)

    def _execute_step(self, step: dict, project_path: str, context: dict) -> dict:
        tool = self._select_tool(step)
        if tool is None:
            return {"ok": False, "error": f"unknown tool: {step.get('tool')!r}"}

        prompt = (
            f"Paso a ejecutar: {step.get('description')}\n"
            f"Herramienta: {step.get('tool')}\n"
            f"Métodos disponibles en esta herramienta:\n{self._describe_tool_methods(tool)}\n\n"
            f"Ruta del proyecto: {project_path}\n"
            f"Contexto adicional: {json.dumps(context or {}, ensure_ascii=False)[:1500]}\n\n"
            "Elige el método más adecuado y sus argumentos (usa la ruta del proyecto "
            "para el argumento de ruta/path donde el método lo requiera)."
        )
        action = _extract_json(_llm_call(prompt))
        if not action or "method" not in action:
            return {"ok": False, "error": "could not determine a concrete action for this step"}

        method_name = action["method"]
        args = action.get("args") or {}
        if method_name.startswith("_") or not hasattr(tool, method_name):
            return {"ok": False, "error": f"tool {step.get('tool')!r} has no public method {method_name!r}"}
        if not isinstance(args, dict):
            return {"ok": False, "error": "action 'args' must be an object"}

        method = getattr(tool, method_name)
        try:
            result = method(**args)
        except TypeError as e:
            return {"ok": False, "error": f"bad arguments for {method_name}(): {e}"}
        except Exception as e:
            logger.error("Orchestrator._execute_step: %s.%s failed", step.get("tool"), method_name, exc_info=True)
            return {"ok": False, "error": str(e)}

        # Normalize whatever the underlying tool returned into {ok, ...}
        # — most Phase 1/2/3 tools already return {ok: bool, ...} or a
        # plain bool; a handful (analyze(), detect(), etc.) return a bare
        # dict/list with no 'ok' key, which counts as success unless it
        # explicitly carries an 'error' key (the convention every
        # permission-denied path in this package already uses).
        if isinstance(result, dict):
            if "ok" in result:
                return result
            if "error" in result:
                return {"ok": False, "error": result["error"], "result": result}
            return {"ok": True, "result": result}
        if isinstance(result, bool):
            return {"ok": result}
        return {"ok": True, "result": result}

    # ── failure handling ─────────────────────────────────────────────────

    def _should_escalate(self, failure_count: int, error) -> bool:
        if failure_count >= MAX_STEP_ATTEMPTS:
            return True
        text = str(error).lower()
        permission_markers = ("not in allowed_project_paths", "is disabled in", "permission", "no venv found")
        scope_markers = ("blocked a write outside", "outside skills", "unknown tool", "no public method")
        return any(m in text for m in permission_markers) or any(m in text for m in scope_markers)

    def _handle_failure(self, step: dict, error, plan: dict) -> dict:
        """Decides retry / replan / escalate. Returns
        {"action": "retry"|"replan"|"escalate", ...}. `error` is whatever
        _execute_step() returned (a dict, typically carrying 'error')."""
        attempts = step.get("_attempts", 0) + 1
        step["_attempts"] = attempts
        error_text = error.get("error") if isinstance(error, dict) else str(error)

        if self._should_escalate(attempts, error_text):
            return {"action": "escalate", "reason": error_text, "attempts": attempts}

        # A debug pass before blindly retrying, if this looks like a
        # code-level failure a diagnosis could actually inform (not a
        # permission/environment issue, which _should_escalate already
        # caught above).
        try:
            from core.code_engine.tool_manager import tool_manager
            debugger = tool_manager.get_tool("debugger")
            if debugger and error_text:
                # attempt=attempts: Debugger.analyze_traceback() consults
                # DocsBrowser.research_error() automatically once this is
                # >= 2 (Phase 4) — see that method's own docstring.
                diagnosis = debugger.analyze_traceback(error_text, attempt=attempts)
                if diagnosis.get("suggested_fix"):
                    return {"action": "retry", "diagnosis": diagnosis, "attempts": attempts}
        except Exception:
            logger.error("Orchestrator._handle_failure: debug pass failed", exc_info=True)

        return {"action": "replan", "attempts": attempts}

    # ── Phase 4: self-review, preference learning ────────────────────────

    def _self_review_and_fix(self, project_path: str, goal: str, starting_hash: str) -> dict:
        """Runs right before the final commit (see execute_goal()).
        Critical issues get one best-effort fix attempt each (routed
        through _execute_step's same LLM-driven tool-call translation
        used for plan steps, tool='editor') and are re-checked; whatever
        is STILL critical after that is escalated to Joan via the usual
        notification queue but does NOT block the commit — Phase 3's
        whole design already treats 'notify and keep going' as safer than
        'block forever' for an autonomous cycle, see _escalate()'s own
        callers. Warnings/suggestions are never auto-fixed, just carried
        into the final summary. No-ops (returns {}) if code_reviewer or
        the starting checkpoint hash aren't available."""
        from core.code_engine.tool_manager import tool_manager
        reviewer = tool_manager.get_tool("code_reviewer")
        if reviewer is None or not starting_hash:
            return {}

        report = reviewer.review_changes(project_path, starting_hash)
        critical = report.get("critical") or []
        if not critical:
            return report

        for issue in critical[:5]:
            file_ = issue.get("file")
            if not file_:
                continue
            step = {
                "description": f"Corrige este problema crítico en {file_}: {issue.get('description', '')} — {issue.get('fix', '')}",
                "tool": "editor",
            }
            try:
                self._execute_step(step, project_path, {"goal": goal, "review_issue": issue})
            except Exception:
                logger.warning("Orchestrator._self_review_and_fix: fix attempt failed for %s", file_, exc_info=True)

        rechecked = reviewer.review_changes(project_path, starting_hash)
        if rechecked.get("critical"):
            self._escalate(
                goal, None,
                f"la auto-revisión encontró {len(rechecked['critical'])} problema(s) crítico(s) "
                "que no se pudieron corregir automáticamente — revisa antes de confiar en este cambio",
            )
        return rechecked

    def _detect_and_save_preferences(self, project_path: str, starting_hash: str) -> None:
        """Phase 4 preference detection — one Ollama call over the diff
        since `starting_hash`, per spec's exact prompt. Only 'alta'
        (high) confidence inferences are saved (CodeMemory.
        remember_preference) — a guess the model itself isn't sure about
        has no business shaping how LIRA edits code next time. Best-effort
        and entirely non-fatal: any failure here just means no new
        preference was learned this session, never affects the goal's
        own success/failure."""
        if not starting_hash:
            return
        try:
            from core.code_engine.tool_manager import tool_manager
            git = tool_manager.get_tool("git")
            code_memory = tool_manager.get_tool("code_memory")
            if git is None or code_memory is None:
                return
            diff_text = git.diff(project_path, ref=starting_hash)
            if not diff_text.strip():
                return

            prompt = f"Diff:\n{diff_text[:4000]}\n\n¿Qué preferencias de estilo de código se pueden inferir de estos cambios?"
            context = (
                "Eres un asistente que infiere preferencias de estilo de código de Joan a partir "
                "de un diff, en español. Responde SOLO con JSON: "
                '{"preferences": [{"category": "style"|"patterns"|"avoided", "text": str, '
                '"confidence": "alta"|"media"|"baja"}]}. Si no hay nada claro, {"preferences": []}. '
                "Sin texto fuera del JSON."
            )
            raw = _llm_call_with_context(prompt, context)
            parsed = _extract_json(raw) or {}
            for pref in parsed.get("preferences") or []:
                if not isinstance(pref, dict) or pref.get("confidence") != "alta":
                    continue
                category = pref.get("category")
                text = pref.get("text")
                if category in ("style", "patterns", "avoided") and text:
                    code_memory.remember_preference(category, str(text))
        except Exception:
            logger.warning("Orchestrator._detect_and_save_preferences failed", exc_info=True)

    def _escalate(self, goal: str, plan_id: str, reason: str) -> None:
        """Stops all work and notifies Joan — same producer-agnostic
        core.notifications queue TaskEngine/SkillForge/CodeEngine already
        use, delivered on her next real conversation turn without this
        file touching commands.py/session.py/personality.py at all."""
        try:
            from core import notifications as notifications_mod
            notifications_mod.create_notification(
                "code_engine",
                f"Bloqueada en '{goal}'",
                f"Bloqueada en '{goal}' — {reason}. ¿Cómo procedo?",
            )
        except Exception:
            logger.error("Orchestrator._escalate: notification failed", exc_info=True)
        logger.info("[ORCHESTRATOR] escalated — goal=%r plan=%s reason=%s", goal, plan_id, reason)

    # ── the full cycle ───────────────────────────────────────────────────

    def execute_goal(self, goal: str, project_path: str) -> dict:
        """Thin wrapper — see _execute_goal_impl() for the real cycle. Just
        adds mark/clear-code-engine-cycle around the whole (possibly
        many-LLM-call) run, same ensure-before/kill-after-once-not-per-call
        shape as core.code_engine.CodeEngine.create_module()'s own wrapper.
        See core.ollama_control.mark_code_engine_cycle_running()'s own
        docstring for why this matters: without it, every individual
        _llm_call() across Planner/Debugger/CodeReviewer/this file's own
        _execute_step() kills and reloads the model between every single
        call in the cycle — confirmed by direct measurement to cost ~45s
        of pure cold-load time PER call on this hardware."""
        import core.ollama_control as ollama_control
        # Do NOT let Orchestrator bypass the permission system — checked
        # here up front (in addition to every individual tool call below
        # already checking it independently), and BEFORE marking the cycle
        # running — a denied goal never touches Ollama at all.
        allowed, reason = check_permission("read", project_path)
        if not allowed:
            return {"success": False, "summary": reason, "steps_completed": 0, "time": 0}

        ollama_control.mark_code_engine_cycle_running()
        try:
            return self._execute_goal_impl(goal, project_path)
        finally:
            ollama_control.clear_code_engine_cycle_running()

    def _execute_goal_impl(self, goal: str, project_path: str) -> dict:
        started = time.monotonic()

        from core.code_engine.tool_manager import tool_manager
        analyzer     = tool_manager.get_tool("project_analyzer")
        planner      = tool_manager.get_tool("planner")
        checkpointer = tool_manager.get_tool("checkpoint_manager")
        testing      = tool_manager.get_tool("testing")
        git          = tool_manager.get_tool("git")
        code_memory  = tool_manager.get_tool("code_memory")

        if planner is None:
            return {"success": False, "summary": "planner tool unavailable", "steps_completed": 0, "time": 0}

        project_context = analyzer.analyze(project_path) if analyzer else {}
        if isinstance(project_context, dict) and project_context.get("error"):
            return {"success": False, "summary": project_context["error"], "steps_completed": 0, "time": 0}

        # Phase 4: known context from past sessions (this project's own
        # history, and Joan's coding preferences observed across ALL
        # projects) folded into what Planner sees when it builds the plan —
        # never overrides project_context's own live-analyzed keys, just
        # adds to them.
        if code_memory:
            remembered = code_memory.recall_project(project_path)
            if remembered:
                project_context = {**(project_context or {}), "known_project_memory": remembered}
            preferences = code_memory.recall_preferences()
            if any(preferences.values()):
                project_context = {**(project_context or {}), "joan_preferences": preferences}

        plan = planner.create_plan(goal, {**(project_context or {}), "path": project_path})
        if not plan or plan.get("error"):
            return {"success": False, "summary": (plan or {}).get("error", "no se pudo generar un plan"), "steps_completed": 0, "time": 0}
        plan_id = planner.save_plan(plan)

        starting_hash = ""
        if checkpointer:
            snapshot = checkpointer.auto_checkpoint(project_path, f"plan {plan_id}: {goal}")
            starting_hash = snapshot.get("hash", "") if isinstance(snapshot, dict) else ""

        steps_completed = 0
        total_attempts = 0
        step_extra_context: dict = {}

        while True:
            step = planner.next_step(plan)
            if step is None:
                break   # every step done, or nothing left runnable

            total_attempts += 1
            if total_attempts > MAX_TOTAL_STEP_ATTEMPTS:
                plan = planner.mark_step_failed(plan, step["id"], "exceeded total attempt budget for this goal")
                planner.save_plan(plan)
                self._escalate(goal, plan_id, "se alcanzó el límite de intentos totales para este objetivo")
                return {
                    "success": False, "summary": "Bloqueada — límite de intentos totales alcanzado",
                    "steps_completed": steps_completed, "time": round(time.monotonic() - started, 1),
                    "plan_id": plan_id, "escalated": True,
                }
            if time.monotonic() - started > MAX_GOAL_WALL_CLOCK_SECONDS:
                plan = planner.mark_step_failed(plan, step["id"], "exceeded wall-clock time budget for this goal")
                planner.save_plan(plan)
                self._escalate(goal, plan_id, "se alcanzó el límite de tiempo para este objetivo")
                return {
                    "success": False, "summary": "Bloqueada — límite de tiempo alcanzado",
                    "steps_completed": steps_completed, "time": round(time.monotonic() - started, 1),
                    "plan_id": plan_id, "escalated": True,
                }

            ctx = {"goal": goal, "plan_id": plan_id}
            if step["id"] in step_extra_context:
                ctx["previous_failure"] = step_extra_context[step["id"]]

            result = self._execute_step(step, project_path, ctx)
            if result.get("ok"):
                plan = planner.mark_step_done(plan, step["id"], result)
                steps_completed += 1
                planner.save_plan(plan)
                continue

            decision = self._handle_failure(step, result, plan)
            if decision["action"] == "escalate":
                plan = planner.mark_step_failed(plan, step["id"], decision.get("reason", "unknown"))
                planner.save_plan(plan)
                self._escalate(goal, plan_id, decision.get("reason", "fallo desconocido"))
                return {
                    "success": False, "summary": f"Bloqueada — {decision.get('reason')}",
                    "steps_completed": steps_completed, "time": round(time.monotonic() - started, 1),
                    "plan_id": plan_id, "escalated": True,
                }
            if decision["action"] == "replan":
                plan = planner.replan(plan, {"step": step.get("description"), "error": result.get("error")})
                planner.save_plan(plan)
                continue
            # "retry" — step stays 'pending', loop picks it back up; feed
            # the diagnosis forward so the retry is actually informed.
            step_extra_context[step["id"]] = {"error": result.get("error"), "diagnosis": decision.get("diagnosis")}
            continue

        # Defense in depth on top of Planner.replan()'s own fallback
        # (which now always keeps at least the original failed/pending
        # steps rather than ever producing an empty list — see that
        # method): a plan that ends with literally zero steps means
        # nothing was ever attempted, not that everything succeeded.
        # Confirmed via testing this combination was reachable before the
        # replan() fix and produced a false "success".
        if not plan.get("steps"):
            self._escalate(goal, plan_id, "el plan quedó sin pasos — no se pudo generar ni recuperar un plan utilizable")
            return {
                "success": False, "summary": "Bloqueada — el plan quedó vacío",
                "steps_completed": steps_completed, "time": round(time.monotonic() - started, 1),
                "plan_id": plan_id, "escalated": True,
            }

        test_summary = testing.run_all(project_path) if testing else {}

        # Phase 4 — self-review BEFORE the final commit (per spec: fix
        # critical issues first, commit still happens either way — see
        # _self_review_and_fix's own docstring for why this doesn't block).
        review_report = self._self_review_and_fix(project_path, goal, starting_hash)

        final_hash = git.checkpoint(project_path, f"goal: {goal}") if git else ""

        # Phase 4 — record what was decided and why, and (best-effort)
        # infer any new Joan coding-style preferences from this session's
        # diff. Both no-op cleanly if code_memory/starting_hash aren't
        # available.
        if code_memory:
            rationale = f"Completado en {steps_completed} paso(s)."
            if review_report.get("summary"):
                rationale += f" Auto-revisión: {review_report['summary']}"
            code_memory.remember_decision(project_path, goal, rationale)
        self._detect_and_save_preferences(project_path, starting_hash)

        plan["status"] = "completed"
        plan["last_review"] = review_report or None   # GET /api/code-engine/review/<plan_id> reads this
        planner.save_plan(plan)

        return {
            "success": True,
            "summary": f"Completado — {steps_completed} paso(s).",
            "steps_completed": steps_completed,
            "time": round(time.monotonic() - started, 1),
            "plan_id": plan_id,
            "tests": test_summary,
            "final_checkpoint": final_hash or None,
            "review": review_report or None,
        }
