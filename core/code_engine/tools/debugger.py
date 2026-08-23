# DEBUGGER — analyzes tracebacks/logs (regex extraction + one LLMRouter
# call for root_cause/suggested_fix), locates the actual source of an
# error via CodeSearch, and adds/removes temporary debug logging. Every
# temp-logging line add_temp_logging() inserts is tagged '# HUGO_DEBUG'
# so remove_temp_logging() can find and strip exactly (and only) what it
# added — never touches a print()/logger call that was already there.
# Read-only for analysis; every mutation (temp logging insert/remove)
# goes through Editor, so it still gets Editor's own automatic backup.
import logging
import os
import re

from core.code_engine.tool_base import CodeEngineTool
from core.code_engine.permissions import check_permission

logger = logging.getLogger("code_engine")

_TRACEBACK_FILE_LINE_RE = re.compile(r'File "([^"]+)", line (\d+)')
_PYTHON_ERROR_RE = re.compile(r'^([A-Za-z_][\w.]*(?:Error|Exception|Warning)):\s*(.*)$', re.MULTILINE)

_DEBUG_TAG = "# HUGO_DEBUG"

_DEBUG_CONTEXT = (
    "Eres un asistente que diagnostica errores de software a partir de tracebacks y "
    "logs, en español. Responde EXACTAMENTE en este formato, dos líneas:\n"
    "CAUSA: <causa raíz en una frase>\n"
    "SUGERENCIA: <corrección concreta sugerida>"
)


def _split_cause_fix(raw: str) -> tuple:
    cause, fix = "", ""
    m = re.search(r"CAUSA:\s*(.+)", raw or "")
    if m:
        cause = m.group(1).strip()
    m2 = re.search(r"SUGERENCIA:\s*(.+)", raw or "", re.DOTALL)
    if m2:
        fix = m2.group(1).strip()
    return cause, (fix or (raw or "").strip())


def _llm_call(prompt: str) -> str:
    """Shared ensure/kill-wrapped LLMRouter call — same discipline as
    every other Ollama usage in this package."""
    try:
        from core.code_engine import LLMRouter
        import core.ollama_control as ollama_control
        ollama_control.ensure_ollama_daemon_running()
        try:
            return LLMRouter().generate_code(prompt, _DEBUG_CONTEXT) or ""
        finally:
            ollama_control.kill_llama_server()
    except Exception:
        logger.error("Debugger: LLM call failed", exc_info=True)
        return ""


class Debugger(CodeEngineTool):
    name = "debugger"
    description = "Analiza tracebacks/logs, localiza el origen de errores y añade/quita logging temporal."
    version = "1.0"

    def ping(self) -> bool:
        return True

    def analyze_traceback(self, traceback: str, attempt: int = 1) -> dict:
        """`attempt`: which try this is at diagnosing the SAME failure (a
        caller like Orchestrator._handle_failure already tracks this per
        step — see step['_attempts']). At attempt >= 2 (i.e. the LLM
        diagnosis has already been tried twice without resolving it),
        automatically consults DocsBrowser.research_error() before
        returning — known solutions from the web, folded into
        'suggested_fix' if the LLM's own guess came back empty, always
        exposed separately under 'docs_research'. Requires the 'internet'
        permission; silently skipped (docs_research: None) if that's off,
        same as every other DocsBrowser caller."""
        file_, line = None, None
        for m in _TRACEBACK_FILE_LINE_RE.finditer(traceback or ""):
            file_, line = m.group(1), int(m.group(2))   # last match = innermost frame, closest to the actual fault
        error_type, message = None, ""
        m = _PYTHON_ERROR_RE.search(traceback or "")
        if m:
            error_type, message = m.group(1), m.group(2)

        prompt = f"Traceback:\n{(traceback or '')[:3000]}\n\nDiagnostica este error."
        root_cause, suggested_fix = _split_cause_fix(_llm_call(prompt))

        docs_research = None
        if attempt >= 2:
            try:
                from core.code_engine.tool_manager import tool_manager
                docs = tool_manager.get_tool("docs_browser")
                if docs:
                    docs_research = docs.research_error(message or (traceback or "")[:300], error_type or "python")
                    if not suggested_fix and docs_research.get("solutions"):
                        suggested_fix = docs_research["solutions"][0]
            except Exception:
                logger.warning("Debugger: docs research pass failed", exc_info=True)

        return {
            "error_type": error_type, "message": message,
            "file": file_, "line": line,
            "root_cause": root_cause, "suggested_fix": suggested_fix,
            "docs_research": docs_research,
        }

    def analyze_logs(self, log_path: str, context: str = None) -> dict:
        allowed, reason = check_permission("read", log_path)
        if not allowed:
            return {"error": reason}
        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()[-8000:]
        except OSError as e:
            return {"error": str(e)}

        prompt = (
            (f"Contexto — qué intentaba hacer HUGO: {context}\n\n" if context else "")
            + f"Log:\n{content}\n\nResume qué salió mal y por qué, en 2-3 frases."
        )
        summary = _llm_call(prompt)
        return {"log_path": log_path, "summary": summary, "excerpt": content[-2000:]}

    def find_error_origin(self, traceback: str, project_path: str) -> dict:
        """Cross-checks analyze_traceback()'s extracted file against the
        actual project via CodeSearch — the traceback's own path might be
        absolute, relative to some other cwd, or inside a venv, not
        necessarily the project-relative path a caller expects."""
        info = self.analyze_traceback(traceback)
        file_ = info.get("file")
        if not file_:
            return {**info, "confirmed_in_project": False, "resolved_file": None}

        from core.code_engine.tool_manager import tool_manager
        search = tool_manager.get_tool("code_search")
        basename = os.path.basename(file_)
        matches = search.search_text(project_path, basename) if search else []
        resolved = next((m["file"] for m in matches if os.path.basename(m["file"]) == basename), None)
        return {**info, "confirmed_in_project": resolved is not None, "resolved_file": resolved}

    def reproduce(self, failure: dict, project_path: str) -> dict:
        """Best-effort minimal reproduction — re-runs whatever the
        `failure` dict references (a 'test'/'file' pair for Testing, or a
        'command' for Shell), isolated from the rest of a plan."""
        allowed, reason = check_permission("read", project_path)
        if not allowed:
            return {"ok": False, "error": reason}

        from core.code_engine.tool_manager import tool_manager
        failure = failure or {}
        if failure.get("test"):
            testing = tool_manager.get_tool("testing")
            return testing.run_test(project_path, failure["test"]) if testing else {"ok": False, "error": "testing tool unavailable"}
        if failure.get("file"):
            testing = tool_manager.get_tool("testing")
            return testing.run_file(project_path, failure["file"]) if testing else {"ok": False, "error": "testing tool unavailable"}
        if failure.get("command"):
            shell = tool_manager.get_tool("shell")
            return shell.run(failure["command"], project_path) if shell else {"ok": False, "error": "shell tool unavailable"}
        return {"ok": False, "error": "failure dict has no 'test'/'file'/'command' to reproduce"}

    def add_temp_logging(self, file: str, lines: list) -> bool:
        """Inserts one print(), tagged '# HUGO_DEBUG', right before each
        given 1-indexed line — via Editor.insert(), so every insertion
        still gets Editor's own automatic backup. Inserts bottom-up so
        earlier insertions don't shift the line numbers of ones still to
        come."""
        from core.code_engine.tool_manager import tool_manager
        editor = tool_manager.get_tool("editor")
        if editor is None:
            return False
        ok_all = True
        for line in sorted(set(int(l) for l in lines), reverse=True):
            snippet = f'print(f"{_DEBUG_TAG} line {line}: {{locals()}}")  {_DEBUG_TAG}'
            if not editor.insert(file, max(0, line - 1), snippet):
                ok_all = False
        return ok_all

    def remove_temp_logging(self, file: str) -> bool:
        """Strips every line containing the '# HUGO_DEBUG' tag — nothing
        else. Still goes through Editor's own backup first."""
        allowed, reason = check_permission("write", file)
        if not allowed:
            logger.warning("Debugger: denied remove_temp_logging on %r (%s)", file, reason)
            return False
        try:
            with open(file, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except OSError:
            return False
        kept = [line for line in lines if _DEBUG_TAG not in line]
        if len(kept) == len(lines):
            return True   # nothing tagged — not an error, just nothing to do

        from core.code_engine.tool_manager import tool_manager
        editor = tool_manager.get_tool("editor")
        if editor is not None:
            editor._backup(file)   # same backup discipline as every other Editor-driven mutation
        try:
            with open(file, "w", encoding="utf-8") as f:
                f.writelines(kept)
            return True
        except OSError:
            return False

    def verify_fix(self, failure: dict, project_path: str, error: str = None, solution: str = None) -> bool:
        """Re-runs the specific failing test/command after a fix. `error`/
        `solution` are optional — when both are given AND the fix
        verifies, automatically saves the pair to CodeMemory
        (remember_solution) so future debugging sessions (this project or
        another) can recall it via recall_similar_errors(). Omitted by a
        caller that doesn't have clean error/solution text (e.g. a bare
        reproduce()-only check) — verification itself is unaffected
        either way."""
        ok = bool(self.reproduce(failure, project_path).get("ok"))
        if ok and error and solution:
            try:
                from core.code_engine.tool_manager import tool_manager
                code_memory = tool_manager.get_tool("code_memory")
                if code_memory:
                    code_memory.remember_solution(error, solution, project_path)
            except Exception:
                logger.warning("Debugger: remember_solution failed", exc_info=True)
        return ok
