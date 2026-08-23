# FILE SYSTEM — every operation is gated by _check_permission() (see
# core.code_engine.permissions), which refuses anything outside
# data/code_engine_permissions.json's allowed_project_paths — empty by
# default, so nothing is accessible until Joan adds a path there herself.
import logging
import os
import shutil

from core.code_engine.tool_base import CodeEngineTool
from core.code_engine.permissions import check_permission

logger = logging.getLogger("code_engine")

_IGNORE_TREE_ENTRIES = {".git", "node_modules", "__pycache__", "venv", ".venv"}


class FileSystem(CodeEngineTool):
    name = "file_system"
    description = "Lectura/escritura/gestión de archivos, restringido a allowed_project_paths."
    version = "1.0"

    def ping(self) -> bool:
        return True

    def _check_permission(self, operation: str, path: str) -> bool:
        allowed, reason = check_permission(operation, path)
        if not allowed:
            logger.warning("FileSystem: denied %s on %r (%s)", operation, path, reason)
        return allowed

    def exists(self, path: str) -> bool:
        if not self._check_permission("read", path):
            return False
        return os.path.exists(path)

    def read(self, path: str) -> str:
        if not self._check_permission("read", path):
            return ""
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
        except OSError:
            return ""

    def write(self, path: str, content: str) -> bool:
        if not self._check_permission("write", path):
            return False
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return True
        except OSError:
            logger.error("FileSystem.write(%r) failed", path, exc_info=True)
            return False

    def list_dir(self, path: str, recursive: bool = False) -> list:
        if not self._check_permission("read", path):
            return []
        if not recursive:
            try:
                return sorted(os.listdir(path))
            except OSError:
                return []
        results = []
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in _IGNORE_TREE_ENTRIES]
            for name in dirs + files:
                results.append(os.path.relpath(os.path.join(root, name), path))
        return sorted(results)

    def create_dir(self, path: str) -> bool:
        if not self._check_permission("write", path):
            return False
        try:
            os.makedirs(path, exist_ok=True)
            return True
        except OSError:
            return False

    def delete(self, path: str) -> bool:
        if not self._check_permission("delete", path):
            return False
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            return True
        except OSError:
            logger.error("FileSystem.delete(%r) failed", path, exc_info=True)
            return False

    def move(self, src: str, dst: str) -> bool:
        if not (self._check_permission("write", src) and self._check_permission("write", dst)):
            return False
        try:
            shutil.move(src, dst)
            return True
        except OSError:
            return False

    def copy(self, src: str, dst: str) -> bool:
        if not (self._check_permission("read", src) and self._check_permission("write", dst)):
            return False
        try:
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
            return True
        except OSError:
            return False

    def get_tree(self, path: str, max_depth: int = 4) -> str:
        if not self._check_permission("read", path):
            return ""
        lines = [os.path.basename(path.rstrip(os.sep)) or path]

        def _walk(current: str, depth: int, prefix: str) -> None:
            if depth > max_depth:
                return
            try:
                entries = sorted(
                    e for e in os.listdir(current)
                    if not e.startswith(".") and e not in _IGNORE_TREE_ENTRIES
                )
            except OSError:
                return
            for i, entry in enumerate(entries):
                full = os.path.join(current, entry)
                is_last = i == len(entries) - 1
                connector = "└── " if is_last else "├── "
                lines.append(f"{prefix}{connector}{entry}{'/' if os.path.isdir(full) else ''}")
                # Don't descend into symlinked directories — os.path.isdir()
                # follows symlinks to answer "is this a directory", but
                # recursing into one could walk straight out of the
                # allowed path this whole call was gated on (same class of
                # bug os.walk()'s own followlinks=False default prevents).
                if os.path.isdir(full) and not os.path.islink(full):
                    _walk(full, depth + 1, prefix + ("    " if is_last else "│   "))

        _walk(path, 1, "")
        return "\n".join(lines)
