# GIT — every method runs `git -C <path> ...`, so it's structurally
# impossible for a call to operate on any repo other than the one at
# `path` — combined with the permission check, that path also has to be
# inside allowed_project_paths. No `push` method — the permissions schema
# defines a 'git_push' flag but nothing here implements pushing yet (out
# of scope for Phase 1; the flag exists for a future phase to check).
#
# `path` scoping when it's a SUBDIRECTORY of a larger repo (e.g. skills/
# inside JarvisLite's own repo, not its own git root): `git -C path ...`
# only changes git's CURRENT DIRECTORY, not what it operates on — a bare
# `git status`/`git add -A`/`git reset --hard` still sees (and, for the
# mutating ones, touches) the WHOLE enclosing repo, not just `path`.
# Every mutating/reporting method below is explicitly scoped with a `--
# .` pathspec (or, for rollback, `checkout <hash> -- .` instead of
# `reset --hard`) so a call against skills/ can never stage, stash, or
# discard changes anywhere else in the repo. `branches()` and `revert()`
# are the two exceptions: branches are inherently repo-wide (no
# per-directory concept), and revert operates on a specific commit object,
# not a path — reverting a commit that happened to touch files outside
# `path` will still touch them, which is correct git semantics for
# reverting a commit as a unit, not a scoping gap.
import logging
import subprocess

from core.code_engine.tool_base import CodeEngineTool
from core.code_engine.permissions import check_permission

logger = logging.getLogger("code_engine")


class Git(CodeEngineTool):
    name = "git"
    description = "Operaciones git de lectura/escritura, siempre acotadas al project path dado."
    version = "1.0"

    def ping(self) -> bool:
        try:
            result = subprocess.run(["git", "--version"], capture_output=True, timeout=5)
            return result.returncode == 0
        except Exception:
            return False

    def _check(self, operation: str, path: str) -> bool:
        allowed, reason = check_permission(operation, path)
        if not allowed:
            logger.warning("Git: denied %s on %r (%s)", operation, path, reason)
        return allowed

    def _run(self, path: str, args: list, timeout: int = 20):
        try:
            return subprocess.run(["git", "-C", path] + args, capture_output=True, text=True, timeout=timeout)
        except Exception:
            logger.error("Git._run(%r, %r) failed", path, args, exc_info=True)
            return None

    def status(self, path: str) -> str:
        if not self._check("read", path):
            return ""
        result = self._run(path, ["status", "--porcelain", "--", "."])
        return result.stdout if result else ""

    def diff(self, path: str, file: str = None, ref: str = None) -> str:
        """`ref`, added for CheckpointManager.diff_from_checkpoint(): diffs
        the working tree against a specific commit instead of the index/
        HEAD. Backward compatible — omitted, behavior is unchanged."""
        if not self._check("read", path):
            return ""
        args = ["diff"] + ([ref] if ref else []) + ["--", (file or ".")]
        result = self._run(path, args)
        return result.stdout if result else ""

    def log(self, path: str, limit: int = 10) -> list:
        """Only commits that touched something under `path` — same `--
        .` scoping as status()/diff(), not the whole repo's history."""
        if not self._check("read", path):
            return []
        result = self._run(path, ["log", f"-{limit}", "--format=%H|%an|%ad|%s", "--date=iso", "--", "."])
        if not result or result.returncode != 0:
            return []
        entries = []
        for line in result.stdout.splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                entries.append({"hash": parts[0], "author": parts[1], "date": parts[2], "message": parts[3]})
        return entries

    def log_matching(self, path: str, grep: str, limit: int = 50) -> list:
        """Like log(), but matches by commit MESSAGE (git log --grep)
        instead of by path diff. Necessary for finding --allow-empty
        commits (checkpoints) — by definition they touch no files, so
        log()'s `-- .` pathspec scoping makes them invisible even though
        they genuinely exist (confirmed: list_checkpoints() returned
        nothing for a checkpoint created moments earlier, until this
        fixed it). Repo-wide within the enclosing repo, same limitation as
        branches() — an empty commit has no path affinity to scope by."""
        if not self._check("read", path):
            return []
        result = self._run(path, ["log", f"-{limit}", "--format=%H|%an|%ad|%s", "--date=iso", f"--grep={grep}"])
        if not result or result.returncode != 0:
            return []
        entries = []
        for line in result.stdout.splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                entries.append({"hash": parts[0], "author": parts[1], "date": parts[2], "message": parts[3]})
        return entries

    def branches(self, path: str) -> list:
        """Repo-wide by nature — a branch isn't scoped to a subdirectory."""
        if not self._check("read", path):
            return []
        result = self._run(path, ["branch", "--format=%(refname:short)"])
        return result.stdout.split() if result and result.returncode == 0 else []

    def commit(self, path: str, message: str) -> bool:
        if not self._check("write", path):
            return False
        added = self._run(path, ["add", "--", "."])
        if added is None or added.returncode != 0:
            return False
        result = self._run(path, ["commit", "-m", message])
        return result is not None and result.returncode == 0

    def stash(self, path: str) -> bool:
        """`git stash push -- .`, not bare `git stash` — the latter
        stashes the WHOLE working tree, which would sweep in unrelated
        dirty files elsewhere in the repo when `path` is a subdirectory."""
        if not self._check("write", path):
            return False
        result = self._run(path, ["stash", "push", "--", "."])
        return result is not None and result.returncode == 0

    def stash_pop(self, path: str) -> bool:
        """No pathspec on `pop` (git doesn't support one) — safe anyway,
        since stash() above only ever pushed a path-scoped stash entry to
        begin with, so popping it back only ever restores that."""
        if not self._check("write", path):
            return False
        result = self._run(path, ["stash", "pop"])
        return result is not None and result.returncode == 0

    def revert(self, path: str, commit_hash: str) -> bool:
        if not self._check("write", path):
            return False
        result = self._run(path, ["revert", "--no-edit", commit_hash])
        return result is not None and result.returncode == 0

    def checkpoint(self, path: str, label: str, reason: str = "") -> str:
        """git add -- . && git commit --allow-empty -m "checkpoint:
        {label}" -> the new commit's hash, or "" on failure. --allow-empty
        so a checkpoint always succeeds even if nothing changed under
        `path` since the last one. `reason`, added for CheckpointManager.
        create(): appended as the commit body (backward compatible —
        omitted, the message is exactly what it always was)."""
        if not self._check("write", path):
            return ""
        added = self._run(path, ["add", "--", "."])
        if added is None:
            return ""
        message = f"checkpoint: {label}" + (f"\n\n{reason}" if reason else "")
        result = self._run(path, ["commit", "--allow-empty", "-m", message])
        if result is None or result.returncode != 0:
            return ""
        rev = self._run(path, ["rev-parse", "HEAD"])
        return rev.stdout.strip() if rev and rev.returncode == 0 else ""

    def rollback_to_checkpoint(self, path: str, commit_hash: str) -> bool:
        """git checkout <commit_hash> -- . — restores every file under
        `path` to its state at that commit. Deliberately NOT `git reset
        --hard`: a hard reset moves HEAD and rewrites the ENTIRE working
        tree to match the target commit, which would silently discard
        uncommitted changes (and detach from commits made) anywhere else
        in the repo when `path` is a subdirectory, not a repo root. A
        scoped checkout achieves the same practical goal — undo bad
        changes under this path — without touching anything outside it or
        moving HEAD backward; the checkpoint commit itself stays reachable
        in history either way."""
        if not self._check("write", path):
            return False
        result = self._run(path, ["checkout", commit_hash, "--", "."])
        return result is not None and result.returncode == 0
