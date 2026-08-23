# CODE SEARCH — text/regex/symbol search over a project, gated by the
# 'read' permission on every call (see core.code_engine.permissions).
# Uses ripgrep (`rg`) if a REAL system binary is on PATH — shutil.which()
# here runs against subprocess's actual environment, not this shell
# session's own aliases, so it correctly returns None (and falls back to
# the pure-Python re implementation below) on a machine that doesn't have
# ripgrep installed, which is the common case this was verified against.
import difflib
import logging
import os
import re
import shutil
import subprocess

from core.code_engine.tool_base import CodeEngineTool
from core.code_engine.permissions import check_permission

logger = logging.getLogger("code_engine")

_RG_PATH = shutil.which("rg")
_IGNORE_DIRS = {".git", "node_modules", "__pycache__", "venv", ".venv", "dist", "build"}


class CodeSearch(CodeEngineTool):
    name = "code_search"
    description = "Búsqueda de texto/regex/símbolos en un proyecto (ripgrep si está disponible, si no re de Python)."
    version = "1.0"

    def ping(self) -> bool:
        return True

    def _allowed(self, path: str) -> bool:
        allowed, _ = check_permission("read", path)
        return allowed

    # ── backends ─────────────────────────────────────────────────────────

    def _rg_search(self, path: str, pattern: str, fixed: bool) -> list | None:
        args = [_RG_PATH, "--line-number", "--no-heading", "--with-filename"]
        if fixed:
            args.append("--fixed-strings")
        args += ["--", pattern, path]
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=30)
        except Exception:
            return None
        if result.returncode not in (0, 1):   # 1 == "no matches", still a valid run
            return None
        matches = []
        for line in result.stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) != 3:
                continue
            file_, lineno, content = parts
            try:
                matches.append({"file": file_, "line": int(lineno), "content": content})
            except ValueError:
                continue
        return matches

    def _py_search(self, path: str, pattern: str, is_regex: bool) -> list:
        matches = []
        regex = re.compile(pattern) if is_regex else None
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS and not d.startswith(".")]
            for fname in files:
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                        lines = f.readlines()
                except OSError:
                    continue
                for i, line in enumerate(lines):
                    hit = regex.search(line) if is_regex else (pattern in line)
                    if not hit:
                        continue
                    matches.append({
                        "file": fpath, "line": i + 1, "content": line.rstrip("\n"),
                        "context_before": lines[i - 1].rstrip("\n") if i >= 1 else "",
                        "context_after":  lines[i + 1].rstrip("\n") if i + 1 < len(lines) else "",
                    })
        return matches

    def _enrich_context(self, matches: list) -> list:
        """rg's own output has no surrounding lines — add them, same shape
        as the pure-Python backend's results."""
        cache: dict = {}
        for m in matches:
            fpath = m["file"]
            if fpath not in cache:
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                        cache[fpath] = f.readlines()
                except OSError:
                    cache[fpath] = []
            lines = cache[fpath]
            i = m["line"] - 1
            m["context_before"] = lines[i - 1].rstrip("\n") if i >= 1 else ""
            m["context_after"]  = lines[i + 1].rstrip("\n") if i + 1 < len(lines) else ""
        return matches

    # ── public API ───────────────────────────────────────────────────────

    def search_text(self, path: str, query: str) -> list:
        if not self._allowed(path):
            return []
        if _RG_PATH:
            result = self._rg_search(path, query, fixed=True)
            if result is not None:
                return self._enrich_context(result)
        return self._py_search(path, query, is_regex=False)

    def search_regex(self, path: str, pattern: str) -> list:
        if not self._allowed(path):
            return []
        if _RG_PATH:
            result = self._rg_search(path, pattern, fixed=False)
            if result is not None:
                return self._enrich_context(result)
        return self._py_search(path, pattern, is_regex=True)

    def find_function(self, path: str, name: str) -> list:
        return self.search_regex(path, rf"\bdef\s+{re.escape(name)}\s*\(")

    def find_class(self, path: str, name: str) -> list:
        return self.search_regex(path, rf"\bclass\s+{re.escape(name)}\b")

    def find_references(self, path: str, symbol: str) -> list:
        return self.search_regex(path, rf"\b{re.escape(symbol)}\b")

    def find_imports(self, path: str, module: str) -> list:
        return self.search_regex(path, rf"^\s*(?:from\s+{re.escape(module)}\b|import\s+{re.escape(module)}\b)")

    def compare_files(self, path_a: str, path_b: str) -> str:
        if not (self._allowed(path_a) and self._allowed(path_b)):
            return ""
        try:
            with open(path_a, "r", encoding="utf-8", errors="ignore") as f:
                a = f.readlines()
            with open(path_b, "r", encoding="utf-8", errors="ignore") as f:
                b = f.readlines()
        except OSError:
            return ""
        return "".join(difflib.unified_diff(a, b, fromfile=path_a, tofile=path_b))
