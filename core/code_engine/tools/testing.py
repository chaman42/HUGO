# TESTING — run a project's own test suite (gated by 'run_tests', True by
# default per spec — no writes, no shell, just running the project's
# existing test command) and, separately, suggest_fix() for a failure.
#
# suggest_fix() reuses core.code_engine.LLMRouter (DeepSeek primary,
# Ollama qwen2.5-coder fallback) rather than the general conversational
# model — this is squarely code-generation territory, the same thing
# LLMRouter already exists for. It ONLY ever returns text; it never calls
# Editor itself and never writes anything. The "suggest_fix ->
# Editor.replace_block -> run_file" loop described for Phase 2 is
# something a CALLER drives step by step (Joan, or a future Orchestrator —
# explicitly Phase 3, not built here) by invoking these three tools in
# sequence; nothing in this file loops or applies a fix automatically.
import logging
import os
import subprocess

from core.code_engine.tool_base import CodeEngineTool
from core.code_engine.permissions import check_permission

logger = logging.getLogger("code_engine")

_OUTPUT_CAP = 5000

# Deliberately NOT core.code_engine._CODE_CONTEXT — that system prompt is
# specifically about generating a skills/ HugoSkill module file, which
# would bias suggest_fix() toward HUGO's own module shape instead of
# whatever arbitrary project's own conventions actually apply.
_FIX_CONTEXT = (
    "Eres un asistente que analiza fallos de pruebas de software y sugiere "
    "correcciones concretas, en español. No conoces el resto del proyecto "
    "más allá de lo que se te da — no asumas ningún framework o estructura "
    "que no esté explícito en la salida del fallo o el código proporcionado."
)


class Testing(CodeEngineTool):
    name = "testing"
    description = "Ejecuta la suite de pruebas de un proyecto y sugiere correcciones para fallos (solo texto, no aplica nada)."
    version = "1.0"

    def ping(self) -> bool:
        return True

    def _pytest_binary(self, path: str) -> str:
        """Prefers the project's OWN venv's pytest (venv/.venv/env/bin/pytest)
        over a bare 'pytest' — a bare subprocess.run() doesn't inherit any
        shell's activated venv, and most Python projects only have pytest
        installed inside their own venv, not globally. Falls back to the
        bare command, which only works if pytest happens to be on this
        process's own PATH."""
        for name in ("venv", ".venv", "env"):
            candidate = os.path.join(path, name, "bin", "pytest")
            if os.path.isfile(candidate):
                return candidate
        return "pytest"

    def _detect_test_command(self, path: str) -> list | None:
        if os.path.isfile(os.path.join(path, "pytest.ini")) or os.path.isfile(os.path.join(path, "conftest.py")):
            return [self._pytest_binary(path)]
        if os.path.isdir(os.path.join(path, "tests")):
            return [self._pytest_binary(path)]
        if os.path.isfile(os.path.join(path, "package.json")):
            return ["npm", "test"]
        return None

    def _run(self, cmd: list, path: str, timeout: int) -> dict:
        try:
            result = subprocess.run(cmd, cwd=path, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"timed out after {timeout}s"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout[-_OUTPUT_CAP:],
            "stderr": result.stderr[-_OUTPUT_CAP:],
        }

    def run_all(self, path: str) -> dict:
        allowed, reason = check_permission("run_tests", path)
        if not allowed:
            return {"ok": False, "error": reason}
        cmd = self._detect_test_command(path)
        if cmd is None:
            return {"ok": False, "error": "no se detectó un framework de pruebas (pytest/npm test)"}
        return self._run(cmd, path, timeout=300)

    def run_file(self, path: str, file: str) -> dict:
        allowed, reason = check_permission("run_tests", path)
        if not allowed:
            return {"ok": False, "error": reason}
        cmd = self._detect_test_command(path) or [self._pytest_binary(path)]
        return self._run(cmd + [file], path, timeout=120)

    def run_test(self, path: str, test: str) -> dict:
        """Runs one specific test by name (pytest -k <name>)."""
        allowed, reason = check_permission("run_tests", path)
        if not allowed:
            return {"ok": False, "error": reason}
        return self._run([self._pytest_binary(path), "-k", test], path, timeout=120)

    def suggest_fix(self, failure_output: str, file_content: str = "") -> str:
        """One LLMRouter call proposing a fix — returns raw text (an
        explanation plus a corrected code block), never applied here. The
        caller (not this method) decides whether/how to feed that into
        Editor.replace_block()."""
        import core.ollama_control as ollama_control
        ollama_control.ensure_ollama_daemon_running()
        try:
            from core.code_engine import LLMRouter
            prompt = (
                f"Salida de la prueba fallida:\n{failure_output[:3000]}\n\n"
                + (f"Contenido actual del archivo relevante:\n{file_content[:3000]}\n\n" if file_content else "")
                + "Analiza el fallo y sugiere una corrección concreta: una breve explicación "
                  "de la causa, seguida del bloque de código corregido."
            )
            return LLMRouter().generate_code(prompt, _FIX_CONTEXT) or ""
        except Exception:
            logger.error("Testing.suggest_fix failed", exc_info=True)
            return ""
        finally:
            ollama_control.kill_llama_server()
