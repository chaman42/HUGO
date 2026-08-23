# SUBAGENT MANAGER — lets TaskEngine delegate a focused sub-goal to a
# short-lived, narrowly-scoped worker (data/subagents.json). One level
# deep only: a subagent has no reference back to SubagentManager itself
# and cannot spawn another subagent — see the four _run_* methods below,
# none of which call spawn()/spawn_parallel().
#
# Isolation, by construction, not by a runtime sandbox:
#   - A subagent only ever sees `subagent.goal` and `subagent.context` —
#     the four _run_* methods never import core.memory/core.personality/
#     core.session, so there is no full memory, personality, or
#     conversation history for them to reach even if they wanted to.
#   - None of the four _run_* methods write to data/memory_*.json — RESEARCH
#     and ANALYSIS/VALIDATION are read-only Ollama/web calls; CODE delegates
#     to core.code_engine.CodeEngine, which is itself hard-restricted to
#     writing under skills/ only (see that module's own _safe_path check) —
#     memory files are simply never touched by any path through here.
#
# spawn()/spawn_parallel() only ever QUEUE a subagent as 'pending' — they
# never execute one. Actual execution happens exclusively inside
# run_pending(), called from scripts/reflective_mode.py's sleep phase (or
# an explicit manual trigger) — never automatically outside of that, per
# "no subagents outside sleep cycles without explicit Joan request".
#
# This is the only asyncio use in this codebase (everything else is
# threading-based Flask/subprocess code) — deliberately self-contained:
# run_pending() spins up its own event loop for the duration of one call
# via asyncio.run(), rather than assuming a loop is already running
# somewhere else in the process.
import asyncio
import dataclasses
import datetime
import json
import logging
import os
import threading
import uuid
from enum import Enum

logger = logging.getLogger(__name__)

SUBAGENTS_PATH = "data/subagents.json"
MAX_PARALLEL_DEFAULT     = 3   # never more than this many subagents running at once — protects the Mac
DEFAULT_TIMEOUT_SECONDS  = 120

VALID_SUBAGENT_STATUSES = ("pending", "running", "completed", "failed", "timeout", "cancelled")


class SubagentType(Enum):
    RESEARCH   = "research"     # web search + summarize findings
    CODE       = "code"         # delegates to CodeEngine
    ANALYSIS   = "analysis"     # analyze data, results, or existing code
    VALIDATION = "validation"   # verify a result meets requirements


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


@dataclasses.dataclass
class Subagent:
    id: str
    type: SubagentType
    goal: str
    context: dict
    status: str
    result: str | None
    created_at: str
    completed_at: str | None
    parent_task_id: str
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["type"] = self.type.value
        return d

    @staticmethod
    def from_dict(d: dict) -> "Subagent":
        d = dict(d)
        d["type"] = SubagentType(d["type"])
        return Subagent(**d)


class SubagentManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    # ── persistence ──────────────────────────────────────────────────────

    def _load(self) -> dict:
        try:
            with open(SUBAGENTS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("active", [])
        data.setdefault("completed", [])
        data.setdefault("max_parallel", MAX_PARALLEL_DEFAULT)
        return data

    def _save_locked(self, data: dict) -> None:
        """Caller must hold self._lock."""
        os.makedirs(os.path.dirname(SUBAGENTS_PATH) or ".", exist_ok=True)
        with open(SUBAGENTS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ── spawning (queue only — never executes inline) ──────────────────

    def spawn(self, type, goal: str, context: dict, parent_task_id: str,
              timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> str:
        if isinstance(type, str):
            type = SubagentType(type)
        subagent = Subagent(
            id=f"sub_{uuid.uuid4().hex[:10]}",
            type=type, goal=goal, context=dict(context or {}),
            status="pending", result=None,
            created_at=_now_iso(), completed_at=None,
            parent_task_id=parent_task_id, timeout_seconds=timeout_seconds,
        )
        with self._lock:
            data = self._load()
            data["active"].append(subagent.to_dict())
            self._save_locked(data)
        return subagent.id

    def spawn_parallel(self, subagents: list) -> list:
        """Each item: {"type": SubagentType|str, "goal": str,
        "context": dict, "parent_task_id": str, "timeout_seconds": int?}.
        Still just queues them — see run_pending() for actual execution."""
        return [
            self.spawn(
                s["type"], s["goal"], s.get("context", {}), s["parent_task_id"],
                s.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
            )
            for s in subagents
        ]

    # ── queries ──────────────────────────────────────────────────────────

    def get_active(self) -> list:
        with self._lock:
            return list(self._load()["active"])

    def get_result(self, subagent_id: str) -> dict:
        with self._lock:
            data = self._load()
        for bucket in ("active", "completed"):
            for s in data[bucket]:
                if s.get("id") == subagent_id:
                    return s
        return {}

    def get_all_for_task(self, task_id: str) -> list:
        with self._lock:
            data = self._load()
        return [
            s for bucket in ("active", "completed") for s in data[bucket]
            if s.get("parent_task_id") == task_id
        ]

    def cancel(self, subagent_id: str) -> bool:
        with self._lock:
            data = self._load()
            entry = next((s for s in data["active"] if s.get("id") == subagent_id), None)
            if entry is None or entry.get("status") not in ("pending", "running"):
                return False
            entry["status"]       = "cancelled"
            entry["completed_at"] = _now_iso()
            data["active"].remove(entry)
            data["completed"].append(entry)
            self._save_locked(data)
            return True

    # ── the four subagent kinds ──────────────────────────────────────────
    # None of these call self.spawn()/self.spawn_parallel() — one level
    # deep only. None import core.memory/core.personality/core.session.
    # All sync/blocking calls go through loop.run_in_executor() so they
    # don't block the event loop the other concurrent subagents share.

    async def _run_research(self, subagent: Subagent) -> str:
        loop = asyncio.get_event_loop()
        from core import tools_search

        results = await loop.run_in_executor(None, tools_search.search_web, subagent.goal)
        formatted = tools_search.format_search_results(results)
        if not formatted:
            return "Sin resultados de búsqueda."

        from core.sleep_llm import _ollama_generate
        summary = await loop.run_in_executor(
            None, _ollama_generate,
            "Eres un asistente que resume resultados de búsqueda web de forma concisa, en español.",
            f"Objetivo: {subagent.goal}\n\nResultados:\n{formatted}\n\nResume los hallazgos relevantes.",
            400,
        )
        return summary or formatted[:800]

    async def _run_code(self, subagent: Subagent) -> str:
        """context must carry either {"catalog_id": ...} (create_module)
        or {"module_name": ..., "change": ...} (update_module)."""
        loop = asyncio.get_event_loop()
        from core.code_engine import code_engine
        ctx = subagent.context

        if "module_name" in ctx and "change" in ctx:
            ok = await loop.run_in_executor(None, code_engine.update_module, ctx["module_name"], ctx["change"])
            return f"update_module({ctx['module_name']}) -> {'ok' if ok else 'failed'}"
        if "catalog_id" in ctx:
            ok = await loop.run_in_executor(None, code_engine.create_module, ctx["catalog_id"])
            return f"create_module({ctx['catalog_id']}) -> {'ok' if ok else 'failed'}"
        return "FAIL: context must include catalog_id, or module_name + change"

    async def _run_analysis(self, subagent: Subagent) -> str:
        loop = asyncio.get_event_loop()
        from core.sleep_llm import _ollama_generate

        context_text = json.dumps(subagent.context, ensure_ascii=False, indent=2)[:2000]
        result = await loop.run_in_executor(
            None, _ollama_generate,
            "Eres un asistente que analiza datos, resultados o código existente y produce un "
            "análisis estructurado, en español.",
            f"Objetivo del análisis: {subagent.goal}\n\nDatos a analizar:\n{context_text}",
            500,
        )
        return result or "Sin análisis disponible."

    async def _run_validation(self, subagent: Subagent) -> str:
        loop = asyncio.get_event_loop()
        from core.sleep_llm import _ollama_generate

        context_text = json.dumps(subagent.context, ensure_ascii=False, indent=2)[:2000]
        prompt = (
            f"Requisito a verificar: {subagent.goal}\n\n"
            f"Resultado a evaluar:\n{context_text}\n\n"
            "Responde EXACTAMENTE en este formato:\nRESULTADO: PASS o FAIL\nRAZÓN: <una frase>"
        )
        result = await loop.run_in_executor(
            None, _ollama_generate,
            "Eres un validador estricto que comprueba si un resultado cumple un requisito, en español.",
            prompt, 150,
        )
        return result or "RESULTADO: FAIL\nRAZÓN: sin respuesta del validador"

    # ── execution — only ever called from run_pending() below ───────────

    async def _execute_subagent(self, subagent: Subagent) -> None:
        runners = {
            SubagentType.RESEARCH:   self._run_research,
            SubagentType.CODE:       self._run_code,
            SubagentType.ANALYSIS:   self._run_analysis,
            SubagentType.VALIDATION: self._run_validation,
        }
        self._set_status(subagent.id, "running")
        try:
            result = await asyncio.wait_for(runners[subagent.type](subagent), timeout=subagent.timeout_seconds)
            self._finish(subagent.id, "completed", result)
        except asyncio.TimeoutError:
            logger.warning("Subagent %s (%s) timed out after %ds", subagent.id, subagent.type.value, subagent.timeout_seconds)
            self._finish(subagent.id, "timeout", None)
        except Exception as e:
            # A failed/timed-out subagent must never crash the parent task —
            # TaskEngine (via resolve_pending_subagent_steps) just sees a
            # terminal, non-'completed' status and decides what to do next.
            logger.error("Subagent %s (%s) failed: %s", subagent.id, subagent.type.value, e, exc_info=True)
            self._finish(subagent.id, "failed", str(e))

    def _set_status(self, subagent_id: str, status: str) -> None:
        with self._lock:
            data = self._load()
            for s in data["active"]:
                if s.get("id") == subagent_id:
                    s["status"] = status
            self._save_locked(data)

    def _finish(self, subagent_id: str, status: str, result) -> None:
        with self._lock:
            data = self._load()
            entry = next((s for s in data["active"] if s.get("id") == subagent_id), None)
            if entry is None:
                return
            entry["status"]       = status
            entry["result"]       = result
            entry["completed_at"] = _now_iso()
            data["active"].remove(entry)
            data["completed"].append(entry)
            self._save_locked(data)

    async def _run_pending_async(self) -> int:
        with self._lock:
            data = self._load()
            max_parallel = data.get("max_parallel", MAX_PARALLEL_DEFAULT)
            pending = [Subagent.from_dict(s) for s in data["active"] if s.get("status") == "pending"]
        if not pending:
            return 0

        semaphore = asyncio.Semaphore(max(1, max_parallel))

        async def _bounded(sub):
            async with semaphore:
                await self._execute_subagent(sub)

        await asyncio.gather(*(_bounded(s) for s in pending), return_exceptions=True)
        return len(pending)

    def run_pending(self) -> int:
        """Sync entry point — called from scripts/reflective_mode.py's
        sleep phase. Executes every currently-'pending' subagent, at most
        max_parallel at a time, each individually timeout-bounded. Returns
        how many were processed. Never raises — a subagent failure is
        caught per-subagent inside _execute_subagent, not here.

        Self-contained Ollama lifecycle (ensure-before/kill-after), same
        discipline as _run_sleep()/SkillForge.forge_from_task() — not just
        relying on whichever caller happens to invoke this to remember to
        wrap it (a real prior incident, llama-server pinned at 300%+ CPU
        from orphaned processes, is exactly what this guards against). A
        no-op call (nothing pending) skips the daemon dance entirely."""
        with self._lock:
            has_pending = any(s.get("status") == "pending" for s in self._load()["active"])
        if not has_pending:
            return 0

        import core.ollama_control as ollama_control
        ollama_control.ensure_ollama_daemon_running()
        try:
            return asyncio.run(self._run_pending_async())
        finally:
            ollama_control.kill_llama_server()


subagent_manager = SubagentManager()
