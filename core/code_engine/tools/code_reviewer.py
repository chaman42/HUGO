# CODE REVIEWER — self-review before Orchestrator commits (see
# orchestrator.py's execute_goal(), which calls review_changes() right
# before its final Git.checkpoint()). Two tiers, same shape as every other
# Phase 1-3 tool that mixes fast heuristics with an LLM call:
#   - deterministic/regex, no LLM: find_security_issues, find_dead_code,
#     find_duplicates, find_missing_tests — always available, no cost, no
#     network, no flakiness.
#   - one LLMRouter call each: find_bugs, check_quality, find_regressions —
#     genuinely needs judgment a regex can't approximate.
# Every result funnels into ONE report shape (see _new_report/_bucket) so
# review_file/review_full_project/review_changes are all just different
# ways of populating the same {summary, critical, warnings, suggestions,
# quality_score, missing_tests} structure.
import difflib
import json
import logging
import os
import re

from core.code_engine.tool_base import CodeEngineTool
from core.code_engine.permissions import check_permission

logger = logging.getLogger("code_engine")

_IGNORE_DIRS = {".git", "node_modules", "__pycache__", "venv", ".venv", "dist", "build", ".next", "target"}
_REVIEWABLE_EXTENSIONS = {
    ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript", ".jsx": "JavaScript",
    ".tsx": "TypeScript", ".go": "Go", ".rs": "Rust", ".rb": "Ruby", ".java": "Java",
}

# Caps so review_full_project() on a large repo stays bounded — LLM calls
# are the expensive part, files are reviewed individually.
_MAX_FILES_PER_PROJECT_REVIEW = 25
_MAX_CODE_CHARS_PER_LLM_CALL  = 6000

_BUG_CONTEXT = (
    "Eres un revisor de código que busca errores reales (lógicos, de manejo de "
    "excepciones, de concurrencia, off-by-one, etc.), en español. Responde SOLO "
    'con JSON: {"bugs": [{"line": int|null, "description": str, "fix": str, '
    '"severity": "critical"|"warning"}]}. Si no encuentras nada, {"bugs": []}. '
    "Sin texto fuera del JSON."
)

_QUALITY_CONTEXT = (
    "Eres un revisor de código que evalúa calidad general (legibilidad, "
    "estructura, nombres, complejidad), en español. Responde SOLO con JSON: "
    '{"score": 0-10, "issues": [str], "suggestions": [str]}. Sin texto fuera del JSON.'
)

_REGRESSION_CONTEXT = (
    "Eres un revisor de código que analiza un diff en busca de POSIBLES "
    "regresiones de comportamiento respecto a la versión anterior, en español. "
    'Responde SOLO con JSON: {"regressions": [{"file": str, "description": str, '
    '"risk": "high"|"medium"|"low"}]}. Si no ves ninguna, {"regressions": []}. '
    "Sin texto fuera del JSON."
)

# (pattern, description, fix) — deterministic, no LLM. Language-agnostic
# where the syntax allows it; Python-specific ones are marked as such.
_SECURITY_PATTERNS = [
    (re.compile(r"\beval\s*\("), "uso de eval() sobre datos potencialmente no confiables", "evita eval(); usa ast.literal_eval() o un parser específico"),
    (re.compile(r"\bexec\s*\("), "uso de exec() sobre datos potencialmente no confiables", "evita exec() con entrada externa"),
    (re.compile(r"os\.system\s*\("), "os.system() con posible inyección de shell", "usa subprocess.run() con una lista de argumentos, nunca shell=True con entrada externa"),
    (re.compile(r"subprocess\.\w+\([^)]*shell\s*=\s*True"), "subprocess con shell=True — riesgo de inyección si el comando incluye entrada externa", "usa una lista de argumentos y shell=False"),
    (re.compile(r"pickle\.loads?\s*\("), "deserialización con pickle sobre datos no confiables — ejecución de código arbitrario", "usa json u otro formato de datos, no pickle, para entrada no confiable"),
    (re.compile(r"yaml\.load\s*\((?!.*Loader\s*=\s*yaml\.SafeLoader)"), "yaml.load() sin SafeLoader — puede ejecutar código arbitrario", "usa yaml.safe_load()"),
    (re.compile(r"except\s*:\s*(?:#.*)?$", re.MULTILINE), "except desnudo — oculta errores inesperados (incluyendo KeyboardInterrupt/SystemExit)", "captura la excepción específica esperada"),
    (re.compile(r"(?i)(password|api[_-]?key|secret|token)\s*=\s*['\"][^'\"]{4,}['\"]"), "posible credencial/secreto hardcodeado en el código", "mueve el valor a una variable de entorno (.env), nunca al código fuente"),
    (re.compile(r"\.format\(.*\)\s*.*(?:SELECT|INSERT|UPDATE|DELETE)\b", re.IGNORECASE), "posible SQL construido con formateo de string — riesgo de inyección SQL", "usa parámetros bindeados (placeholders), nunca interpolación de string"),
    (re.compile(r"f['\"].*(?:SELECT|INSERT|UPDATE|DELETE)\b.*\{", re.IGNORECASE), "posible SQL construido con f-string — riesgo de inyección SQL", "usa parámetros bindeados (placeholders), nunca interpolación de string"),
]

_DEF_RE = re.compile(r"^\s*(?:def|class)\s+(\w+)", re.MULTILINE)


def _extract_json(raw: str) -> dict | None:
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    try:
        data = json.loads(m.group(0) if m else raw)
    except (json.JSONDecodeError, AttributeError):
        return None
    return data if isinstance(data, dict) else None


def _llm_call(prompt: str, context: str) -> str:
    try:
        from core.code_engine import LLMRouter
        import core.ollama_control as ollama_control
        ollama_control.ensure_ollama_daemon_running()
        try:
            return LLMRouter().generate_code(prompt, context) or ""
        finally:
            ollama_control.kill_llama_server()
    except Exception:
        logger.error("CodeReviewer: LLM call failed", exc_info=True)
        return ""


def _new_report() -> dict:
    return {
        "summary": "", "critical": [], "warnings": [], "suggestions": [],
        "quality_score": None, "missing_tests": [],
    }


def _finalize_summary(report: dict) -> dict:
    n_crit, n_warn, n_sugg = len(report["critical"]), len(report["warnings"]), len(report["suggestions"])
    total = n_crit + n_warn + n_sugg
    if total == 0:
        report["summary"] = "Sin problemas encontrados."
    else:
        parts = []
        if n_crit:
            parts.append(f"{n_crit} crítico(s)")
        if n_warn:
            parts.append(f"{n_warn} advertencia(s)")
        if n_sugg:
            parts.append(f"{n_sugg} sugerencia(s)")
        report["summary"] = f"{total} problema(s) encontrado(s) — " + ", ".join(parts)
    if report["missing_tests"]:
        report["summary"] += f" — {len(report['missing_tests'])} función(es) sin test aparente"
    return report


class CodeReviewer(CodeEngineTool):
    name = "code_reviewer"
    description = "Revisa cambios/archivos/proyectos: bugs, seguridad, código muerto, duplicados, tests faltantes, calidad."
    version = "1.0"

    def ping(self) -> bool:
        return True

    def _allowed(self, path: str) -> bool:
        allowed, _ = check_permission("read", path)
        return allowed

    def _language_for(self, file_path: str) -> str:
        return _REVIEWABLE_EXTENSIONS.get(os.path.splitext(file_path)[1].lower(), "")

    def _walk_reviewable(self, path: str, max_files: int):
        count = 0
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS and not d.startswith(".")]
            for f in files:
                if os.path.splitext(f)[1].lower() not in _REVIEWABLE_EXTENSIONS:
                    continue
                yield os.path.join(root, f)
                count += 1
                if count >= max_files:
                    return

    # ── heuristic (no LLM) ───────────────────────────────────────────────

    def find_security_issues(self, code: str, language: str) -> list:
        issues = []
        lines = code.splitlines()
        for i, line in enumerate(lines, start=1):
            for pattern, description, fix in _SECURITY_PATTERNS:
                if pattern.search(line):
                    issues.append({"type": "security", "line": i, "description": description, "fix": fix})
        return issues

    def find_dead_code(self, project_path: str) -> list:
        """Heuristic — a def/class whose name is referenced nowhere except
        its own definition line is flagged as likely-dead. False positives
        are expected (dynamic dispatch, external callers, __all__ exports,
        dunder methods) — this is a lead for Joan to check, not a
        guarantee."""
        allowed, reason = check_permission("read", project_path)
        if not allowed:
            return [{"error": reason}]

        from core.code_engine.tool_manager import tool_manager
        analyzer = tool_manager.get_tool("project_analyzer")
        search = tool_manager.get_tool("code_search")
        if analyzer is None or search is None:
            return []

        symbols = analyzer.index_symbols(project_path)
        results = []
        for name, rel_file in symbols.items():
            if name.startswith("__") and name.endswith("__"):
                continue   # dunder methods — always "referenced" implicitly
            refs = search.find_references(project_path, name)
            if len(refs) <= 1:
                results.append({
                    "type": "dead_code", "file": rel_file, "symbol": name,
                    "description": f"'{name}' no parece referenciado en ningún otro lugar del proyecto",
                    "fix": "confirma que no se usa dinámicamente antes de eliminarlo",
                })
        return results

    def find_duplicates(self, project_path: str) -> list:
        """Pairwise difflib similarity over reviewable files under a size/
        count cap — O(n^2), fine for a project-scoped, occasionally-run
        tool. Flags pairs above _DUPLICATE_THRESHOLD similarity."""
        allowed, reason = check_permission("read", project_path)
        if not allowed:
            return [{"error": reason}]

        threshold = 0.85
        files = list(self._walk_reviewable(project_path, max_files=100))
        contents = {}
        for f in files:
            try:
                with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                    text = fh.read()
                if len(text) > 200:   # skip trivially small files — noisy, not meaningful duplicates
                    contents[f] = text
            except OSError:
                continue

        results = []
        items = list(contents.items())
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                file_a, text_a = items[i]
                file_b, text_b = items[j]
                ratio = difflib.SequenceMatcher(None, text_a, text_b).quick_ratio()
                if ratio >= threshold:
                    results.append({
                        "type": "duplicate",
                        "file_a": os.path.relpath(file_a, project_path),
                        "file_b": os.path.relpath(file_b, project_path),
                        "similarity": round(ratio, 2),
                        "description": f"contenido muy similar ({ratio:.0%})",
                        "fix": "considera extraer la lógica compartida a un módulo/función común",
                    })
        return results

    def find_missing_tests(self, project_path: str) -> list:
        """Heuristic — a public function name (no leading underscore) that
        never appears in any file whose path contains 'test' is flagged.
        Only functions, not classes (classes are commonly tested via their
        methods' own names, which would double-count)."""
        allowed, reason = check_permission("read", project_path)
        if not allowed:
            return []

        from core.code_engine.tool_manager import tool_manager
        search = tool_manager.get_tool("code_search")
        if search is None:
            return []

        missing = []
        for f in self._walk_reviewable(project_path, max_files=_MAX_FILES_PER_PROJECT_REVIEW):
            if "test" in os.path.basename(f).lower():
                continue
            try:
                with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                    content = fh.read()
            except OSError:
                continue
            for m in re.finditer(r"^\s*def\s+([a-zA-Z][\w]*)\s*\(", content, re.MULTILINE):
                name = m.group(1)
                if name.startswith("_"):
                    continue
                refs = search.find_references(project_path, name)
                if not any("test" in os.path.basename(r["file"]).lower() for r in refs):
                    missing.append(name)
        return sorted(set(missing))

    # ── LLM-assisted ─────────────────────────────────────────────────────

    def find_bugs(self, code: str, language: str) -> list:
        prompt = f"Lenguaje: {language}\n\nCódigo:\n{code[:_MAX_CODE_CHARS_PER_LLM_CALL]}\n\nBusca errores reales."
        parsed = _extract_json(_llm_call(prompt, _BUG_CONTEXT))
        bugs = (parsed or {}).get("bugs")
        return bugs if isinstance(bugs, list) else []

    def check_quality(self, code: str, language: str) -> dict:
        prompt = f"Lenguaje: {language}\n\nCódigo:\n{code[:_MAX_CODE_CHARS_PER_LLM_CALL]}\n\nEvalúa la calidad."
        parsed = _extract_json(_llm_call(prompt, _QUALITY_CONTEXT))
        if not parsed:
            return {"score": None, "issues": [], "suggestions": []}
        score = parsed.get("score")
        try:
            score = max(0.0, min(10.0, float(score)))
        except (TypeError, ValueError):
            score = None
        return {
            "score": score,
            "issues": [str(x) for x in (parsed.get("issues") or [])],
            "suggestions": [str(x) for x in (parsed.get("suggestions") or [])],
        }

    def find_regressions(self, project_path: str, baseline_hash: str) -> list:
        allowed, reason = check_permission("read", project_path)
        if not allowed:
            return [{"error": reason}]
        from core.code_engine.tool_manager import tool_manager
        git = tool_manager.get_tool("git")
        if git is None:
            return []
        diff_text = git.diff(project_path, ref=baseline_hash)
        if not diff_text.strip():
            return []
        prompt = f"Diff:\n{diff_text[:_MAX_CODE_CHARS_PER_LLM_CALL]}\n\nAnaliza posibles regresiones."
        parsed = _extract_json(_llm_call(prompt, _REGRESSION_CONTEXT))
        regressions = (parsed or {}).get("regressions")
        return regressions if isinstance(regressions, list) else []

    # ── per-file / per-project / per-change review ──────────────────────

    def review_file(self, file_path: str) -> dict:
        allowed, reason = check_permission("read", file_path)
        if not allowed:
            report = _new_report()
            report["summary"] = reason
            return report
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                code = f.read()
        except OSError as e:
            report = _new_report()
            report["summary"] = str(e)
            return report

        language = self._language_for(file_path)
        report = _new_report()

        for issue in self.find_security_issues(code, language):
            report["critical"].append({**issue, "file": file_path})

        for bug in self.find_bugs(code, language):
            bucket = "critical" if bug.get("severity") == "critical" else "warnings"
            report[bucket].append({
                "type": "bug", "file": file_path, "line": bug.get("line"),
                "description": bug.get("description", ""), "fix": bug.get("fix", ""),
            })

        quality = self.check_quality(code, language)
        report["quality_score"] = quality["score"]
        for issue in quality["issues"]:
            report["warnings"].append({"type": "quality", "file": file_path, "line": None, "description": issue, "fix": ""})
        for suggestion in quality["suggestions"]:
            report["suggestions"].append({"type": "quality", "file": file_path, "line": None, "description": suggestion, "fix": ""})

        return _finalize_summary(report)

    def review_full_project(self, project_path: str) -> dict:
        allowed, reason = check_permission("read", project_path)
        if not allowed:
            report = _new_report()
            report["summary"] = reason
            return report

        report = _new_report()
        scores = []
        for f in self._walk_reviewable(project_path, max_files=_MAX_FILES_PER_PROJECT_REVIEW):
            file_report = self.review_file(f)
            report["critical"].extend(file_report["critical"])
            report["warnings"].extend(file_report["warnings"])
            report["suggestions"].extend(file_report["suggestions"])
            if file_report["quality_score"] is not None:
                scores.append(file_report["quality_score"])

        report["quality_score"] = round(sum(scores) / len(scores), 1) if scores else None
        report["missing_tests"] = self.find_missing_tests(project_path)

        for dup in self.find_duplicates(project_path):
            report["suggestions"].append({
                "type": "duplicate", "file": dup.get("file_a"), "line": None,
                "description": f"{dup['description']} con {dup.get('file_b')}", "fix": dup.get("fix", ""),
            })
        for dead in self.find_dead_code(project_path):
            if "error" in dead:
                continue
            report["suggestions"].append({
                "type": "dead_code", "file": dead.get("file"), "line": None,
                "description": dead.get("description", ""), "fix": dead.get("fix", ""),
            })

        return _finalize_summary(report)

    def review_changes(self, project_path: str, since_checkpoint: str) -> dict:
        """Reviews only the files that changed since `since_checkpoint` —
        the auto-review Orchestrator runs before its final commit (see
        orchestrator.py). Falls back to review_full_project() if the diff
        can't be parsed for a file list (e.g. bad hash)."""
        allowed, reason = check_permission("read", project_path)
        if not allowed:
            report = _new_report()
            report["summary"] = reason
            return report

        from core.code_engine.tool_manager import tool_manager
        git = tool_manager.get_tool("git")
        if git is None:
            report = _new_report()
            report["summary"] = "git tool unavailable"
            return report

        diff_text = git.diff(project_path, ref=since_checkpoint)
        changed_files = sorted(set(re.findall(r"^\+\+\+ b/(.+)$", diff_text, re.MULTILINE)))
        if not changed_files:
            report = _new_report()
            report["summary"] = "Sin cambios detectados desde ese checkpoint."
            return report

        report = _new_report()
        scores = []
        reviewed_dirs = set()
        for rel in changed_files:
            full = os.path.join(project_path, rel)
            if os.path.splitext(full)[1].lower() not in _REVIEWABLE_EXTENSIONS or not os.path.isfile(full):
                continue
            file_report = self.review_file(full)
            report["critical"].extend(file_report["critical"])
            report["warnings"].extend(file_report["warnings"])
            report["suggestions"].extend(file_report["suggestions"])
            if file_report["quality_score"] is not None:
                scores.append(file_report["quality_score"])
            reviewed_dirs.add(os.path.dirname(full))

        report["quality_score"] = round(sum(scores) / len(scores), 1) if scores else None
        report["missing_tests"] = self.find_missing_tests(project_path)
        return _finalize_summary(report)

    def generate_report(self, results: dict) -> str:
        """Human-readable Spanish summary for Joan — the text form of a
        review report dict (from any of the three review_* methods)."""
        results = results or {}
        lines = [results.get("summary", "Sin resumen.")]
        if results.get("quality_score") is not None:
            lines.append(f"Puntuación de calidad: {results['quality_score']}/10")
        for label, key in (("Críticos", "critical"), ("Advertencias", "warnings"), ("Sugerencias", "suggestions")):
            items = results.get(key) or []
            if not items:
                continue
            lines.append(f"\n{label}:")
            for item in items[:10]:
                loc = f"{item.get('file', '?')}"
                if item.get("line"):
                    loc += f":{item['line']}"
                lines.append(f"  - [{item.get('type', '?')}] {loc} — {item.get('description', '')}")
        if results.get("missing_tests"):
            lines.append(f"\nSin test aparente: {', '.join(results['missing_tests'][:10])}")
        return "\n".join(lines)
