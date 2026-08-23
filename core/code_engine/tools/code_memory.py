# CODE MEMORY — cross-project/cross-session learning, persisted to
# data/code_engine_memory.json. Completely separate from
# data/memory_hugo.json (HUGO's own conversational memory) — different
# file, different purpose, this module never touches that one.
#
# Auto-triggers (wired at their own call sites, not here):
#   ProjectAnalyzer.analyze()        -> remember_project()   (project_analyzer.py)
#   Debugger.verify_fix() success    -> remember_solution()  (debugger.py)
#   Orchestrator.execute_goal() start -> recall_project() + recall_preferences() (orchestrator.py)
#   Orchestrator.execute_goal() end  -> remember_decision() + preference detection (orchestrator.py)
import datetime
import difflib
import json
import logging
import os
import threading

from core.code_engine.tool_base import CodeEngineTool

logger = logging.getLogger("code_engine")

MEMORY_PATH = "data/code_engine_memory.json"

_PREFERENCE_CATEGORIES = ("style", "patterns", "avoided")

_SIMILAR_ERROR_THRESHOLD = 0.6

_DEFAULT_MEMORY = {
    "projects": {},
    "global": {
        "joan_preferences": {"style": [], "patterns": [], "avoided": []},
        "solutions_library": [],
    },
}


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


class CodeMemory(CodeEngineTool):
    name = "code_memory"
    description = "Memoria de Code Engine entre proyectos/sesiones: arquitectura, decisiones, soluciones y preferencias de Joan."
    version = "1.0"

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def ping(self) -> bool:
        return True

    # ── persistence ──────────────────────────────────────────────────────

    def _load(self) -> dict:
        try:
            with open(MEMORY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return json.loads(json.dumps(_DEFAULT_MEMORY))
        if not isinstance(data, dict):
            return json.loads(json.dumps(_DEFAULT_MEMORY))
        data.setdefault("projects", {})
        data.setdefault("global", {})
        data["global"].setdefault("joan_preferences", {})
        for cat in _PREFERENCE_CATEGORIES:
            data["global"]["joan_preferences"].setdefault(cat, [])
        data["global"].setdefault("solutions_library", [])
        return data

    def _save_locked(self, data: dict) -> None:
        os.makedirs(os.path.dirname(MEMORY_PATH) or ".", exist_ok=True)
        with open(MEMORY_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _project_entry(self, data: dict, project_path: str) -> dict:
        return data["projects"].setdefault(project_path, {
            "last_analyzed": None, "architecture": {}, "decisions": [],
            "known_errors": [], "joan_preferences_observed": [],
        })

    # ── remember ─────────────────────────────────────────────────────────

    def remember_project(self, project_path: str, analysis: dict) -> bool:
        if not project_path or not isinstance(analysis, dict):
            return False
        with self._lock:
            data = self._load()
            entry = self._project_entry(data, project_path)
            entry["last_analyzed"] = _now_iso()
            entry["architecture"] = {
                "languages": analysis.get("languages", []),
                "frameworks": analysis.get("frameworks", []),
                "entry_points": analysis.get("entry_points", []),
                "structure": analysis.get("architecture", {}),
            }
            self._save_locked(data)
        return True

    def remember_decision(self, project_path: str, decision: str, rationale: str) -> bool:
        if not project_path or not decision:
            return False
        with self._lock:
            data = self._load()
            entry = self._project_entry(data, project_path)
            entry["decisions"].append({
                "decision": decision, "rationale": rationale, "timestamp": _now_iso(),
            })
            entry["decisions"] = entry["decisions"][-100:]   # bounded — this is a log, not an archive
            self._save_locked(data)
        return True

    def remember_solution(self, error: str, solution: str, project_path: str) -> bool:
        if not error or not solution:
            return False
        with self._lock:
            data = self._load()
            library = data["global"]["solutions_library"]

            existing = next(
                (s for s in library if difflib.SequenceMatcher(None, s["error_pattern"], error).ratio() >= _SIMILAR_ERROR_THRESHOLD),
                None,
            )
            if existing:
                existing["use_count"] = existing.get("use_count", 1) + 1
                existing["solution"] = solution   # most recent successful fix wins
            else:
                library.append({
                    "error_pattern": error, "solution": solution,
                    "language": "", "use_count": 1,
                })

            if project_path:
                entry = self._project_entry(data, project_path)
                entry["known_errors"].append({"error": error, "solution": solution, "timestamp": _now_iso()})
                entry["known_errors"] = entry["known_errors"][-100:]

            self._save_locked(data)
        return True

    def remember_preference(self, category: str, preference: str) -> bool:
        if category not in _PREFERENCE_CATEGORIES or not preference:
            return False
        with self._lock:
            data = self._load()
            bucket = data["global"]["joan_preferences"][category]
            if preference not in bucket:
                bucket.append(preference)
            self._save_locked(data)
        return True

    # ── recall ───────────────────────────────────────────────────────────

    def recall_project(self, project_path: str) -> dict | None:
        with self._lock:
            data = self._load()
        return data["projects"].get(project_path)

    def recall_similar_errors(self, error: str) -> list:
        if not error:
            return []
        with self._lock:
            data = self._load()
        scored = [
            (difflib.SequenceMatcher(None, s["error_pattern"], error).ratio(), s)
            for s in data["global"]["solutions_library"]
        ]
        scored = [(score, s) for score, s in scored if score >= _SIMILAR_ERROR_THRESHOLD]
        scored.sort(key=lambda pair: (pair[0], pair[1].get("use_count", 0)), reverse=True)
        return [s for _, s in scored[:10]]

    def recall_preferences(self) -> dict:
        with self._lock:
            data = self._load()
        return data["global"]["joan_preferences"]

    def forget_project(self, project_path: str) -> bool:
        """Joan-request-only — clears one project's memory. Never called
        automatically by any tool in this package."""
        with self._lock:
            data = self._load()
            existed = project_path in data["projects"]
            data["projects"].pop(project_path, None)
            self._save_locked(data)
        return existed
