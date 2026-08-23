# CHECKPOINT MANAGER — git-commit-backed safety snapshots. Wraps
# core.code_engine.tools.git.Git rather than reimplementing git calls;
# checkpoint metadata (label/reason) lives in the commit message itself
# ("checkpoint: {label}\n\n{reason}") — no separate JSON ledger to keep in
# sync with git's own history, which is already the single source of
# truth and already correctly scoped to `path`, not the enclosing repo
# (see git.py's own module comment).
import logging

from core.code_engine.tool_base import CodeEngineTool

logger = logging.getLogger("code_engine")

# rollback() refuses (returns False) past this many changed lines unless
# called again with confirm=True — "asks for Joan confirmation" for a
# synchronous method translates to: pause here, require an explicit
# second call to actually proceed, same shape as this codebase's other
# propose-then-confirm flows (core.intent._pending_action).
_ROLLBACK_CONFIRM_LINE_THRESHOLD = 50


class CheckpointManager(CodeEngineTool):
    name = "checkpoint_manager"
    description = "Snapshots git con metadatos, listado, rollback (con confirmación si es grande) y diff — envuelve el tool Git."
    version = "1.0"

    def ping(self) -> bool:
        return True

    def _git(self):
        from core.code_engine.tool_manager import tool_manager
        return tool_manager.get_tool("git")

    def create(self, project_path: str, label: str, reason: str = "") -> dict:
        git = self._git()
        if git is None:
            return {"error": "git tool unavailable"}
        commit_hash = git.checkpoint(project_path, label, reason)
        if not commit_hash:
            return {"error": "checkpoint commit failed (permission denied or git error)"}
        log = git.log(project_path, limit=1)
        timestamp = log[0]["date"] if log else ""
        return {"hash": commit_hash, "label": label, "reason": reason, "timestamp": timestamp}

    def list_checkpoints(self, project_path: str) -> list:
        """Uses Git.log_matching() (message-based), not log() (path-diff-
        based) — checkpoint commits are --allow-empty and so have no file
        diff for a pathspec to match against; see that method's own
        docstring."""
        git = self._git()
        if git is None:
            return []
        entries = []
        for commit in git.log_matching(project_path, "^checkpoint:", limit=50):
            if commit["message"].startswith("checkpoint:"):
                entries.append({
                    "hash": commit["hash"],
                    "label": commit["message"][len("checkpoint:"):].strip(),
                    "timestamp": commit["date"],
                })
        return entries

    def diff_from_checkpoint(self, project_path: str, checkpoint_hash: str) -> str:
        git = self._git()
        if git is None:
            return ""
        return git.diff(project_path, ref=checkpoint_hash)

    def rollback(self, project_path: str, checkpoint_hash: str, confirm: bool = False) -> bool:
        """git checkout <hash> -- . (via Git.rollback_to_checkpoint — the
        already-scoped, non-destructive-to-siblings version, not `reset
        --hard`). Refuses outright if the diff since `checkpoint_hash`
        touches more than 50 lines, unless called again with
        confirm=True."""
        git = self._git()
        if git is None:
            return False

        diff_text = self.diff_from_checkpoint(project_path, checkpoint_hash)
        lines_changed = sum(
            1 for line in diff_text.splitlines()
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        )
        if lines_changed > _ROLLBACK_CONFIRM_LINE_THRESHOLD and not confirm:
            logger.warning(
                "CheckpointManager: rollback to %s in %r touches %d lines — refusing without confirm=True",
                checkpoint_hash, project_path, lines_changed,
            )
            return False

        return git.rollback_to_checkpoint(project_path, checkpoint_hash)

    def auto_checkpoint(self, project_path: str, context: str) -> dict:
        """Called automatically before a destructive/mutating operation —
        see DependencyManager.install() and Shell.run() (Editor has no
        multi-file method to hook this to; ToolOrchestrator.execute_goal()
        calls this once before starting a plan instead — see that
        module)."""
        return self.create(project_path, f"auto: before {context}", reason=f"Snapshot automático antes de: {context}")
