"""Port-freeing and stale-process cleanup used by the launcher on every
startup and before each jarvis.py (re)start, so a fresh launch never fails
with "port already in use" and orphaned processes from a crashed session
never linger.
"""
import os
import time

import psutil

from core.launcher_app import logger


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


def _cmdline_runs_script(cmdline: list[str], script_name: str) -> bool:
    """True only if `script_name` is an actual argv entry (the script being
    executed), not merely text that appears somewhere inside a larger
    argument.

    A plain substring check here is dangerous: it would match a shell
    wrapper whose -c script happens to mention the filename (e.g. `zsh -c
    "cd X && python launcher.py"` — one argv token containing the text but
    not running the script directly), an editor with the file open, or a
    grep for it — and kill an unrelated, innocent process.
    """
    return any(os.path.basename(arg) == script_name for arg in cmdline)


def _kill_stale_jarvis() -> None:
    """Kill any lingering jarvis.py processes (zombie or orphan)."""
    for proc in psutil.process_iter(["pid", "name", "cmdline", "status"]):
        try:
            cmdline = proc.cmdline()
            if _cmdline_runs_script(cmdline, "jarvis.py") and proc.pid != os.getpid():
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
    this is what prevents the classic "port 8079 already in use" error when
    Electron spawns a new launcher.py while an old one (e.g. from a previous
    crashed session, or a manually-run `python launcher.py`) is still alive.
    """
    for proc in psutil.process_iter(["pid", "name", "cmdline", "status"]):
        try:
            if proc.pid == os.getpid():
                continue
            cmdline = proc.cmdline()
            if _cmdline_runs_script(cmdline, "launcher.py"):
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
