"""Port-freeing and stale-process cleanup used by the launcher on every
startup and before each jarvis.py (re)start, so a fresh launch never fails
with "port already in use" and orphaned processes from a crashed session
never linger.
"""
import os
import time

import psutil

from core.launcher_app import logger

# This repo's own root — see _cmdline_runs_script's own docstring for why
# matching only stops here now: HUGO and LIRA both run a script literally
# named jarvis.py/launcher.py (HUGO was forked from LIRA), and now run
# side by side on the same machine on different ports (see
# core.process_manager._JARVIS_PORT's own comment on the +100 offset) — a
# basename-only match here used to kill the OTHER app's process on every
# single HUGO startup.
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _free_port(port: int) -> None:
    """Kill any process LISTEN-ing on the given port."""
    killed = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            for conn in proc.net_connections(kind="inet"):
                if conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
                    logger.info("Killing PID %d (%s) blocking port %d", proc.pid, proc.name(), port)
                    proc.kill()
                    proc.wait(timeout=5)
                    killed.append(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    if killed:
        time.sleep(0.5)  # brief pause after kill


def _cmdline_runs_script(cmdline: list[str], script_name: str, proc: "psutil.Process | None" = None) -> bool:
    """True only if `script_name` is an actual argv entry (the script being
    executed) AND it resolves to a path inside THIS repo (_PROJECT_ROOT) —
    not merely text that appears somewhere inside a larger argument, and
    not some other project's same-named script.

    A plain substring check here is dangerous: it would match a shell
    wrapper whose -c script happens to mention the filename (e.g. `zsh -c
    "cd X && python launcher.py"` — one argv token containing the text but
    not running the script directly), an editor with the file open, or a
    grep for it — and kill an unrelated, innocent process.

    The path check matters just as much: HUGO and LIRA both run a script
    literally named jarvis.py/launcher.py (this app was forked from LIRA)
    and are meant to run side by side — without it, HUGO's own startup
    cleanup would kill LIRA's process (and vice versa) purely on filename.
    `proc` (when given) resolves a relative argv entry (the common case —
    `python jarvis.py` launched with cwd already at the project root)
    against THAT process's own cwd, not ours.
    """
    for arg in cmdline:
        if os.path.basename(arg) != script_name:
            continue
        try:
            abs_path = arg if os.path.isabs(arg) else os.path.join(proc.cwd() if proc else os.getcwd(), arg)
            abs_path = os.path.realpath(abs_path)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            abs_path = os.path.realpath(arg)
        if abs_path == os.path.join(_PROJECT_ROOT, script_name) or abs_path.startswith(_PROJECT_ROOT + os.sep):
            return True
    return False


def _kill_stale_jarvis() -> None:
    """Kill any lingering jarvis.py processes (zombie or orphan) — this
    repo's own jarvis.py only, see _cmdline_runs_script's own docstring."""
    for proc in psutil.process_iter(["pid", "name", "cmdline", "status"]):
        try:
            cmdline = proc.cmdline()
            if _cmdline_runs_script(cmdline, "jarvis.py", proc) and proc.pid != os.getpid():
                logger.info(
                    "Killing stale jarvis process PID %d (status: %s)", proc.pid, proc.status()
                )
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except psutil.TimeoutExpired:
                    pass
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass


def _kill_stale_launcher() -> None:
    """Kill any OTHER launcher.py processes still running (never self).

    Called once at startup (see __main__) so a fresh launch always wins —
    this is what prevents the classic "port 8179 already in use" error when
    Electron spawns a new launcher.py while an old one (e.g. from a previous
    crashed session, or a manually-run `python launcher.py`) is still alive.
    This repo's own launcher.py only, see _cmdline_runs_script's own
    docstring — never LIRA's.
    """
    for proc in psutil.process_iter(["pid", "name", "cmdline", "status"]):
        try:
            if proc.pid == os.getpid():
                continue
            cmdline = proc.cmdline()
            if _cmdline_runs_script(cmdline, "launcher.py", proc):
                logger.info(
                    "Killing stale launcher process PID %d (status: %s)", proc.pid, proc.status()
                )
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except psutil.TimeoutExpired:
                    pass
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
