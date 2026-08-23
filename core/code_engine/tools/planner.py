# PLANNER — decomposes a goal into an ordered, dependency-aware plan via
# one LLMRouter call, persisted to data/code_engine_plans.json so it
# survives across sleep cycles/restarts. create_plan() REQUIRES a path in
# project_context and refuses (returns {"error": ...}, creates nothing)
# if that path isn't in allowed_project_paths — Planner never produces an
# unscoped plan, even if a caller simply omits the path.
import datetime
import json
import logging
import os
import re
import threading

from core.code_engine.tool_base import CodeEngineTool
from core.code_engine.permissions import check_permission

logger = logging.getLogger("code_engine")

PLANS_PATH = "data/code_engine_plans.json"

_PLAN_CONTEXT = (
    "Eres un asistente que descompone un objetivo de programación en pasos "
    "concretos y ordenados, en español. Cada paso debe usar UNA de estas "
    "herramientas: project_analyzer, file_system, code_search, editor, git, "
    "shell, dependency_manager, testing, debugger. Responde SOLO con un JSON: "
    '{"steps": [{"description": str, "tool": str, "depends_on": [ids de pasos previos, enteros]}], '
    '"estimated_complexity": "baja"|"media"|"alta"}. Los pasos empiezan en id 1 '
    "(no incluyas 'id' en el JSON, se numeran en orden). Sin texto fuera del JSON."
)


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


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
    try:
        from core.code_engine import LLMRouter
        import core.ollama_control as ollama_control
        ollama_control.ensure_ollama_daemon_running()
        try:
            return LLMRouter().generate_code(prompt, _PLAN_CONTEXT) or ""
        finally:
            ollama_control.kill_llama_server()
    except Exception:
        logger.error("Planner: LLM call failed", exc_info=True)
        return ""


class Planner(CodeEngineTool):
    name = "planner"
    description = "Descompone objetivos en planes de pasos ordenados (LLMRouter), persistidos entre sesiones."
    version = "1.0"

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def ping(self) -> bool:
        return True

    # ── persistence ──────────────────────────────────────────────────────

    def _load(self) -> dict:
        try:
            with open(PLANS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {"plans": {}}
        if not isinstance(data, dict) or not isinstance(data.get("plans"), dict):
            return {"plans": {}}
        return data

    def _save_locked(self, data: dict) -> None:
        os.makedirs(os.path.dirname(PLANS_PATH) or ".", exist_ok=True)
        with open(PLANS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ── plan creation ────────────────────────────────────────────────────

    def create_plan(self, goal: str, project_context: dict) -> dict:
        project_context = project_context or {}
        project_path = project_context.get("path") or project_context.get("project_path")
        if not project_path:
            return {"error": "project_context must include 'path' — Planner never creates unscoped plans"}
        allowed, reason = check_permission("read", project_path)
        if not allowed:
            return {"error": reason}

        context_text = json.dumps(project_context, ensure_ascii=False)[:2000]
        prompt = f"Objetivo: {goal}\n\nContexto del proyecto:\n{context_text}\n\nGenera el plan."
        parsed = _extract_json(_llm_call(prompt))

        steps = []
        for i, s in enumerate((parsed or {}).get("steps") or [], start=1):
            if not isinstance(s, dict):
                continue
            steps.append({
                "id": i,
                "description": str(s.get("description", ""))[:300],
                "tool": (str(s.get("tool")).strip() or None) if s.get("tool") else None,
                "depends_on": sorted({d for d in (s.get("depends_on") or []) if isinstance(d, int) and 0 < d < i}),
                "status": "pending",
                "result": None,
            })
        if not steps:
            # LLM failed/returned nothing usable — one generic step rather
            # than a plan with zero steps (next_step() would just return
            # None forever).
            steps = [{"id": 1, "description": goal, "tool": None, "depends_on": [], "status": "pending", "result": None}]

        return {
            "id": None,
            "goal": goal,
            "project_path": project_path,
            "steps": steps,
            "estimated_complexity": (parsed or {}).get("estimated_complexity", "media"),
            "status": "active",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }

    # ── plan progression ─────────────────────────────────────────────────

    def next_step(self, plan: dict) -> dict | None:
        """First 'pending' step whose dependencies are all 'completed' —
        None if nothing is runnable right now (either every step is done,
        or the only remaining ones are blocked on a dependency that
        failed)."""
        done_ids = {s["id"] for s in plan.get("steps", []) if s.get("status") == "completed"}
        for step in plan.get("steps", []):
            if step.get("status") != "pending":
                continue
            if all(d in done_ids for d in (step.get("depends_on") or [])):
                return step
        return None

    def mark_step_done(self, plan: dict, step_id: int, result: dict) -> dict:
        for step in plan.get("steps", []):
            if step["id"] == step_id:
                step["status"] = "completed"
                step["result"] = result
        plan["updated_at"] = _now_iso()
        if plan.get("steps") and all(s.get("status") == "completed" for s in plan["steps"]):
            plan["status"] = "completed"
        return plan

    def mark_step_failed(self, plan: dict, step_id: int, error: str) -> dict:
        for step in plan.get("steps", []):
            if step["id"] == step_id:
                step["status"] = "failed"
                step["result"] = {"error": error}
        plan["status"] = "blocked"
        plan["updated_at"] = _now_iso()
        return plan

    def replan(self, plan: dict, failure_context: dict) -> dict:
        """One more LLMRouter call: given what's already completed and
        what went wrong, generates REVISED remaining steps — completed
        steps are kept as-is, pending/failed ones are replaced entirely."""
        completed = [s for s in plan.get("steps", []) if s.get("status") == "completed"]
        not_completed = [s for s in plan.get("steps", []) if s.get("status") != "completed"]
        prompt = (
            f"Plan original para: {plan.get('goal')}\n"
            f"Pasos ya completados: {[s['description'] for s in completed]}\n"
            f"Fallo encontrado: {json.dumps(failure_context or {}, ensure_ascii=False)[:1000]}\n\n"
            "Genera los pasos RESTANTES revisados para lograr el objetivo, evitando el "
            "enfoque que falló."
        )
        parsed = _extract_json(_llm_call(prompt))

        next_id = max((s["id"] for s in plan.get("steps", [])), default=0) + 1
        new_steps = []
        for s in (parsed or {}).get("steps") or []:
            if not isinstance(s, dict):
                continue
            new_steps.append({
                "id": next_id,
                "description": str(s.get("description", ""))[:300],
                "tool": (str(s.get("tool")).strip() or None) if s.get("tool") else None,
                "depends_on": [d for d in (s.get("depends_on") or []) if isinstance(d, int)],
                "status": "pending",
                "result": None,
            })
            next_id += 1

        if not new_steps:
            # LLM failed/returned nothing usable — put the original
            # failed/pending steps back as 'pending' rather than silently
            # dropping them. Confirmed by testing this matters: without
            # it, a plan could end up with ZERO steps, next_step() then
            # returns None immediately, and execute_goal()'s loop exits
            # thinking everything succeeded — steps_completed=0 falsely
            # reported as success.
            for s in not_completed:
                s["status"] = "pending"
                s["result"] = None
            new_steps = not_completed

        plan["steps"] = completed + new_steps
        plan["status"] = "active"
        plan["updated_at"] = _now_iso()
        return plan

    # ── persistence (public) ────────────────────────────────────────────

    def save_plan(self, plan: dict) -> str:
        with self._lock:
            data = self._load()
            plan_id = plan.get("id")
            if not plan_id:
                n = len(data["plans"]) + 1
                plan_id = f"plan_{n:03d}"
                while plan_id in data["plans"]:
                    n += 1
                    plan_id = f"plan_{n:03d}"
            plan["id"] = plan_id
            data["plans"][plan_id] = plan
            self._save_locked(data)
        return plan_id

    def load_plan(self, plan_id: str) -> dict:
        with self._lock:
            data = self._load()
        return data["plans"].get(plan_id, {})

    def get_all_plans(self) -> list:
        with self._lock:
            data = self._load()
        return list(data["plans"].values())

    def cancel_plan(self, plan_id: str) -> bool:
        with self._lock:
            data = self._load()
            plan = data["plans"].get(plan_id)
            if plan is None:
                return False
            plan["status"] = "cancelled"
            plan["updated_at"] = _now_iso()
            self._save_locked(data)
        return True
