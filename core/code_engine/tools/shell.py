# SHELL — arbitrary command execution, gated by the 'shell' permission
# (False by default — see core/code_engine/permissions.py; Joan must
# explicitly flip it to true in data/code_engine_permissions.json before
# ANY command runs, on top of `cwd` already needing to be in
# allowed_project_paths). This is the single most dangerous tool in the
# Code Engine toolkit: once 'shell' is true for an allowed path, it's
# equivalent to a full terminal scoped to that path. Every attempt —
# allowed or denied — is logged to logs/code_engine_shell.log for audit,
# not just successes.
import datetime
import logging
import os
import subprocess

from core.code_engine.tool_base import CodeEngineTool
from core.code_engine.permissions import check_permission

logger = logging.getLogger("code_engine")

SHELL_LOG_PATH = "logs/code_engine_shell.log"
DEFAULT_TIMEOUT_SECONDS = 60


def _log_shell(status: str, cwd: str, command: str, detail: str = "") -> None:
    try:
        os.makedirs(os.path.dirname(SHELL_LOG_PATH) or ".", exist_ok=True)
        with open(SHELL_LOG_PATH, "a", encoding="utf-8") as f:
            timestamp = datetime.datetime.now().isoformat(timespec="seconds")
            f.write(f"{timestamp} [{status}] cwd={cwd!r} cmd={command!r} {detail}\n")
    except OSError:
        pass


class Shell(CodeEngineTool):
    name = "shell"
    description = "Ejecuta comandos de shell dentro de un project path permitido — requiere el permiso 'shell' activado explícitamente."
    version = "1.0"

    def ping(self) -> bool:
        return True

    def run(self, command: str, cwd: str, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> dict:
        allowed, reason = check_permission("shell", cwd)
        if not allowed:
            logger.warning("Shell: denied run() in %r (%s)", cwd, reason)
            _log_shell("DENIED", cwd, command, reason)
            return {"ok": False, "error": reason, "stdout": "", "stderr": "", "returncode": None}

        # Checkpoint before EVERY command, not just ones detected as
        # destructive — Shell can't know in advance whether an arbitrary
        # command will modify files, so the conservative default is to
        # always snapshot first.
        try:
            from core.code_engine.tools.checkpoint_manager import CheckpointManager
            CheckpointManager().auto_checkpoint(cwd, f"shell: {command[:60]}")
        except Exception:
            logger.warning("Shell: auto_checkpoint failed (continuing anyway)", exc_info=True)

        try:
            result = subprocess.run(
                command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            )
            status = "OK" if result.returncode == 0 else "FAILED"
            _log_shell(status, cwd, command, f"exit={result.returncode}")
            return {
                "ok": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
            }
        except subprocess.TimeoutExpired:
            _log_shell("TIMEOUT", cwd, command, f"{timeout}s")
            return {"ok": False, "error": f"timed out after {timeout}s", "stdout": "", "stderr": "", "returncode": None}
        except Exception as e:
            logger.error("Shell.run(%r) failed", command, exc_info=True)
            _log_shell("ERROR", cwd, command, str(e))
            return {"ok": False, "error": str(e), "stdout": "", "stderr": "", "returncode": None}
