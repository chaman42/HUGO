# EDITOR — line/block/text edits, gated by the 'write' permission on
# every mutating call. _backup() runs before ANY edit — unconditionally,
# not best-effort-skippable — copying the file's current content to
# data/code_engine_backups/<filename>.<timestamp> and pruning to the 10
# most recent backups per file.
import datetime
import difflib
import logging
import os
import re
import subprocess

from core.code_engine.tool_base import CodeEngineTool
from core.code_engine.permissions import check_permission

logger = logging.getLogger("code_engine")

BACKUPS_DIR = "data/code_engine_backups"
MAX_BACKUPS_PER_FILE = 10


class Editor(CodeEngineTool):
    name = "editor"
    description = "Edición de archivos con backup automático antes de cada cambio."
    version = "1.0"

    def ping(self) -> bool:
        return True

    def _check(self, operation: str, path: str) -> bool:
        allowed, reason = check_permission(operation, path)
        if not allowed:
            logger.warning("Editor: denied %s on %r (%s)", operation, path, reason)
        return allowed

    def _backup(self, path: str) -> str | None:
        """Always called before any edit below. No-op (returns None) for a
        path that doesn't exist yet — nothing to back up for a brand-new
        file."""
        if not os.path.isfile(path):
            return None
        try:
            os.makedirs(BACKUPS_DIR, exist_ok=True)
            basename = os.path.basename(path)
            timestamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S%f")
            backup_path = os.path.join(BACKUPS_DIR, f"{basename}.{timestamp}")
            with open(path, "r", encoding="utf-8", errors="ignore") as src:
                content = src.read()
            with open(backup_path, "w", encoding="utf-8") as dst:
                dst.write(content)
        except OSError:
            logger.error("Editor._backup(%r) failed", path, exc_info=True)
            return None

        prefix = f"{basename}."
        try:
            existing = sorted(f for f in os.listdir(BACKUPS_DIR) if f.startswith(prefix))
        except OSError:
            existing = []
        while len(existing) > MAX_BACKUPS_PER_FILE:
            oldest = existing.pop(0)
            try:
                os.remove(os.path.join(BACKUPS_DIR, oldest))
            except OSError:
                pass
        return backup_path

    def show_diff(self, path: str, new_content: str) -> str:
        """Read-only preview — no backup, no write. Call this BEFORE an
        edit to see what would change."""
        if not self._check("read", path):
            return ""
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                old_lines = f.readlines()
        except OSError:
            old_lines = []
        new_lines = new_content.splitlines(keepends=True)
        return "".join(difflib.unified_diff(old_lines, new_lines, fromfile=path, tofile=path + " (nuevo)"))

    def insert(self, path: str, line: int, content: str) -> bool:
        if not self._check("write", path):
            return False
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except OSError:
            lines = []
        self._backup(path)
        idx = max(0, min(line, len(lines)))
        insert_text = content if content.endswith("\n") else content + "\n"
        lines[idx:idx] = [insert_text]
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            return True
        except OSError:
            return False

    def replace_block(self, path: str, start: int, end: int, content: str) -> bool:
        """1-indexed, inclusive [start, end] — matches show_diff/search
        results' own line numbering."""
        if not self._check("write", path):
            return False
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except OSError:
            return False
        self._backup(path)
        start_idx = max(0, start - 1)
        end_idx = max(start_idx, end)
        new_lines = content.splitlines(keepends=True)
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] += "\n"
        lines[start_idx:end_idx] = new_lines
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            return True
        except OSError:
            return False

    def replace_text(self, path: str, old: str, new: str, all: bool = False) -> bool:
        if not self._check("write", path):
            return False
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except OSError:
            return False
        if old not in content:
            return False
        self._backup(path)
        updated = content.replace(old, new) if all else content.replace(old, new, 1)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(updated)
            return True
        except OSError:
            return False

    def delete_block(self, path: str, start: int, end: int) -> bool:
        return self.replace_block(path, start, end, "")

    def apply_patch(self, path: str, patch: str) -> bool:
        """Applies a unified diff via the system `patch` command, run from
        the file's own directory so the diff's path headers only ever
        touch this one file."""
        if not self._check("write", path):
            return False
        self._backup(path)
        try:
            result = subprocess.run(
                ["patch", "-p0", os.path.basename(path)],
                input=patch, capture_output=True, text=True, timeout=15,
                cwd=os.path.dirname(os.path.abspath(path)),
            )
            return result.returncode == 0
        except Exception:
            logger.error("Editor.apply_patch(%r) failed", path, exc_info=True)
            return False

    def rename_symbol(self, path: str, old_name: str, new_name: str) -> bool:
        """Whole-word text replace — a simple, dependency-free rename
        within one file, not an AST-aware cross-file refactor."""
        if not self._check("write", path):
            return False
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except OSError:
            return False
        pattern = re.compile(rf"\b{re.escape(old_name)}\b")
        if not pattern.search(content):
            return False
        self._backup(path)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(pattern.sub(new_name, content))
            return True
        except OSError:
            return False
