#!/usr/bin/env python3
"""Periodic safety net — invoked every 10 minutes by the
com.joan.hugo.ollama-guard LaunchAgent. Independent of jarvis.py and
scripts/reflective_mode.py's own cleanup (core/ollama_control.py's
kill_llama_server() calls in both of those); this exists specifically to
catch the case those miss: a crash, SIGKILL, force-quit, or any other exit
path that skips normal cleanup, leaving llama-server resident and burning
CPU with nothing left alive to ever release it.

Real incident this fixes: llama-server pinned at 300%+ CPU continuously for
over two days on this machine, driven by orphaned copies of
scripts/reflective_mode.py --continuous that outlived several jarvis.py
restarts (see core/sleep_control.py's own startup-sweep for the other half
of that fix — this script is the belt to that suspenders, catching drift
between jarvis.py launches rather than only at the next one).

Recurrence of that same incident (2026-08-19): core/sleep_control.py's
sweep only runs once, at jarvis.py's own startup — while HUGO stays
closed, nothing ever re-triggers it, so orphans from the last session just
sit there. This script's own _any_sleep_process_alive() check couldn't
tell those apart from a genuine session, so it kept leaving llama-server
alone. Fixed by having main() call
core.ollama_control.kill_orphaned_continuous_sleep_processes() first —
same ppid-based orphan check as core/sleep_control.py's sweep, just
runnable on this script's own 10-minute timer instead of only at the next
jarvis.py launch.

Logic: if llama-server is running AND no sleep-related process
(scripts/reflective_mode.py, --continuous or one-shot) is actually alive
right now, AND no Design Studio autopilot run is in flight
(core.ollama_control.is_autopilot_running — see its own docstring), AND no
Code Engine orchestration cycle is in flight
(core.ollama_control.is_code_engine_cycle_running — same lock-file shape,
kept warm across a whole ToolOrchestrator.execute_goal() cycle rather than
reloaded between every one of its many individual LLM calls), kill
llama-server. Checks the real process table for the sleep case, not just
data/sleep_budget.json's persisted 'running' flag — that flag can go stale
if whatever was supposed to update it on exit never got the chance to
(exactly the crash/SIGKILL case this script exists for). Autopilot/Code
Engine have no process table to fall back on (both are just Flask request
handlers / a background thread inside jarvis.py, not a separate process)
— see AUTOPILOT_LOCK_MAX_AGE_SECONDS/CODE_ENGINE_CYCLE_LOCK_MAX_AGE_SECONDS
for how each case's own staleness/crash-recovery is handled instead.
Note kill_llama_server() itself also checks is_code_engine_cycle_running()
directly (see its own docstring) — the check here is belt-and-suspenders
for a clear diagnostic message, not the only thing preventing it.

Real incident this ALSO fixes (in addition to the CPU-pinning one above):
before the autopilot lock existed, this script's own periodic sweep would
SIGKILL llama-server mid-zone-generation during a Design Studio autopilot
run, and core.commands.run_autopilot_zone's except clause swallowed the
resulting Ollama 500 into a fully-empty zone with no error ever surfaced to
the UI — autopilot looked like it was "designing" while actually producing
nothing.

Manual run: python3 scripts/ollama_guard.py
"""
import os
import subprocess
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT  = os.path.dirname(_SCRIPT_DIR)

# Same venv-site-packages bootstrap as scripts/reflective_mode.py — this
# script is invoked by launchd via the bare system python3.11, which has
# none of this project's packages on its own sys.path. Nothing this script
# actually calls needs a third-party package (only stdlib subprocess), but
# core.ollama_control does live under core/, hence _REPO_ROOT still needs
# to be on sys.path.
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from core import ollama_control  # noqa: E402

_REFLECTIVE_MODE_PATTERN = os.path.join(_REPO_ROOT, "scripts", "reflective_mode.py")


def _any_sleep_process_alive() -> bool:
    """True if any scripts/reflective_mode.py invocation (continuous or
    one-shot) is currently running — checked against the real process
    table rather than data/sleep_budget.json's persisted state, which can
    go stale exactly when this script needs to know the truth (a crash
    that skipped normal cleanup)."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", _REFLECTIVE_MODE_PATTERN],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except Exception:
        # Unknown state — err toward NOT killing a possibly-legitimate
        # in-flight session rather than risk cutting one off mid-phase.
        return True


def main() -> int:
    if not ollama_control.is_llama_server_running():
        print("llama-server not running — nothing to do.")
        return 0

    killed = ollama_control.kill_orphaned_continuous_sleep_processes()
    if killed:
        print(f"killed {killed} orphaned continuous-sleep process(es) — reassessing.")

    if _any_sleep_process_alive():
        print("llama-server running and a sleep process is active — leaving it alone.")
        return 0

    if ollama_control.is_autopilot_running():
        print("llama-server running and a Design Studio autopilot run is active — leaving it alone.")
        return 0

    if ollama_control.is_code_engine_cycle_running():
        print("llama-server running and a Code Engine orchestration cycle is active — leaving it alone.")
        return 0

    killed = ollama_control.kill_llama_server()
    print(f"llama-server was running with no sleep process alive — killed={killed}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
