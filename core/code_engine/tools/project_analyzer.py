# PROJECT ANALYZER — heuristic project mapping: language/framework/entry-
# point detection by file extension and dependency-file content, not a
# real language-server/AST-based understanding. Read-only — every method
# is gated by the 'read' permission (see core.code_engine.permissions).
import os
import re

from core.code_engine.tool_base import CodeEngineTool
from core.code_engine.permissions import check_permission

_LANGUAGE_EXTENSIONS = {
    ".py": "Python", ".js": "JavaScript", ".mjs": "JavaScript", ".ts": "TypeScript",
    ".tsx": "TypeScript", ".jsx": "JavaScript", ".java": "Java", ".go": "Go",
    ".rs": "Rust", ".rb": "Ruby", ".php": "PHP", ".c": "C", ".h": "C",
    ".cpp": "C++", ".hpp": "C++", ".cs": "C#", ".swift": "Swift", ".kt": "Kotlin",
    ".m": "Objective-C", ".sh": "Shell", ".html": "HTML", ".css": "CSS", ".sql": "SQL",
}

# dependency-file basename -> its own "native" language, just for context
_DEPENDENCY_FILES = (
    "package.json", "requirements.txt", "Pipfile", "pyproject.toml",
    "Cargo.toml", "go.mod", "Gemfile", "composer.json", "pom.xml", "build.gradle",
)

_FRAMEWORK_MARKERS = {
    "flask": "Flask", "django": "Django", "fastapi": "FastAPI",
    "\"react\"": "React", "\"vue\"": "Vue", "\"next\"": "Next.js",
    "\"express\"": "Express", "@angular/core": "Angular", "\"svelte\"": "Svelte",
    "rails": "Ruby on Rails", "spring-boot": "Spring Boot",
}

_TEST_FRAMEWORK_MARKERS = {
    "pytest": "pytest", "unittest": "unittest", "jest": "Jest", "mocha": "Mocha",
}

_ENTRY_POINT_NAMES = (
    "main.py", "app.py", "manage.py", "wsgi.py", "asgi.py",
    "index.js", "server.js", "main.js", "index.ts", "main.ts", "main.go", "main.rs",
)

_IGNORE_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build", ".next", "target"}

_IMPORT_RE = re.compile(r"^\s*(?:from\s+(\S+)\s+import|import\s+(\S+))", re.MULTILINE)
_DEF_RE    = re.compile(r"^\s*(?:def|class)\s+(\w+)", re.MULTILINE)


class ProjectAnalyzer(CodeEngineTool):
    name = "project_analyzer"
    description = "Detecta lenguajes, frameworks, puntos de entrada y estructura de un proyecto."
    version = "1.0"

    def ping(self) -> bool:
        return True

    def _allowed(self, path: str) -> bool:
        allowed, _ = check_permission("read", path)
        return allowed

    def _walk_files(self, path: str, max_files: int = 5000):
        count = 0
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS and not d.startswith(".")]
            for f in files:
                yield os.path.join(root, f)
                count += 1
                if count >= max_files:
                    return

    def detect_languages(self, path: str) -> list:
        if not self._allowed(path):
            return []
        counts: dict = {}
        for f in self._walk_files(path):
            lang = _LANGUAGE_EXTENSIONS.get(os.path.splitext(f)[1].lower())
            if lang:
                counts[lang] = counts.get(lang, 0) + 1
        return sorted(counts, key=counts.get, reverse=True)

    def detect_frameworks(self, path: str) -> list:
        if not self._allowed(path):
            return []
        found = set()
        for fname in _DEPENDENCY_FILES:
            fpath = os.path.join(path, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read().lower()
            except OSError:
                continue
            for marker, framework in _FRAMEWORK_MARKERS.items():
                if marker in content:
                    found.add(framework)
        return sorted(found)

    def detect_entry_points(self, path: str) -> list:
        if not self._allowed(path):
            return []
        return [
            os.path.relpath(f, path) for f in self._walk_files(path)
            if os.path.basename(f) in _ENTRY_POINT_NAMES
        ]

    def map_imports(self, path: str) -> dict:
        if not self._allowed(path):
            return {}
        imports_map = {}
        for f in self._walk_files(path):
            if not f.endswith(".py"):
                continue
            try:
                with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                    content = fh.read()
            except OSError:
                continue
            mods = [m.group(1) or m.group(2) for m in _IMPORT_RE.finditer(content)]
            if mods:
                imports_map[os.path.relpath(f, path)] = mods
        return imports_map

    def index_symbols(self, path: str) -> dict:
        if not self._allowed(path):
            return {}
        symbols = {}
        for f in self._walk_files(path):
            if not f.endswith(".py"):
                continue
            try:
                with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                    content = fh.read()
            except OSError:
                continue
            rel = os.path.relpath(f, path)
            for m in _DEF_RE.finditer(content):
                symbols[m.group(1)] = rel
        return symbols

    def analyze(self, project_path: str) -> dict:
        allowed, reason = check_permission("read", project_path)
        if not allowed:
            return {"error": reason}
        if not os.path.isdir(project_path):
            return {"error": f"not a directory: {project_path}"}

        has_tests = False
        test_framework = None
        for f in self._walk_files(project_path):
            basename = os.path.basename(f).lower()
            if "test" in basename or basename.startswith("spec"):
                has_tests = True
            try:
                with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                    head = fh.read(500).lower()
            except OSError:
                head = ""
            for marker, name in _TEST_FRAMEWORK_MARKERS.items():
                if marker in head:
                    test_framework = name
                    break

        dependencies_file = next(
            (fname for fname in _DEPENDENCY_FILES if os.path.isfile(os.path.join(project_path, fname))),
            None,
        )

        readme = ""
        for candidate in ("README.md", "README.rst", "README.txt", "README"):
            fpath = os.path.join(project_path, candidate)
            if os.path.isfile(fpath):
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                        readme = f.read(2000)
                except OSError:
                    pass
                break

        directories = []
        try:
            for entry in os.listdir(project_path):
                full = os.path.join(project_path, entry)
                if os.path.isdir(full) and entry not in _IGNORE_DIRS and not entry.startswith("."):
                    directories.append(entry)
        except OSError:
            pass

        entry_points = self.detect_entry_points(project_path)
        key_files = [os.path.basename(f) for f in entry_points]
        if dependencies_file:
            key_files.append(dependencies_file)

        result = {
            "languages":         self.detect_languages(project_path),
            "frameworks":        self.detect_frameworks(project_path),
            "entry_points":      entry_points,
            "has_tests":         has_tests,
            "test_framework":    test_framework,
            "dependencies_file": dependencies_file,
            "readme":            readme,
            "architecture": {
                "directories": sorted(directories),
                "key_files":   key_files,
                "imports_map": self.map_imports(project_path),
            },
        }

        # Phase 4 auto-trigger: every successful analyze() updates
        # CodeMemory's project architecture map — best-effort, never lets
        # a memory-write failure affect the analysis result itself.
        try:
            from core.code_engine.tool_manager import tool_manager
            code_memory = tool_manager.get_tool("code_memory")
            if code_memory:
                code_memory.remember_project(project_path, result)
        except Exception:
            pass

        return result
