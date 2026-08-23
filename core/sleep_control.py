# ═══════════════════════════════════════════════════════════════════════════
# SLEEP CONTROL — continuous-sleep subprocess lifecycle (start/stop/status)
# and the idle-trigger background loop. Split out of core/commands.py (pure
# refactor, no behavior change).
#
# Sleep System — "while jarvis.py is open" auto-trigger half. After
# _SLEEP_IDLE_SECONDS (20 min) of no user interaction, spawns
# scripts/reflective_mode.py --continuous as a genuine CHILD PROCESS (see
# _start_continuous_sleep) rather than calling core.sleep once in a
# background thread — continuous sleep now runs cycle after cycle forever
# (see core.sleep.run_continuous_sleep) until this process SIGTERMs it,
# which only real process isolation makes safe: a hung local-model call in
# the sleep process can never block this process's own audio/event loop,
# and a single terminate() unconditionally stops it regardless of what
# phase it's mid-call on.
#
# _continuous_sleep_proc is the ONLY authority in this app on "is HUGO
# sleeping right now" — core/server.py's status endpoint calls
# is_continuous_sleep_running() rather than re-deriving this itself.
#
# The manually-triggered path ("Iniciar Sueño" in Ajustes) goes through
# core/server.py's POST /api/sleep/start, which also calls
# _start_continuous_sleep() (trigger='manual') — same subprocess mechanism,
# just a different trigger label recorded in data/sleep_budget.json's
# 'continuous' state for display.
# ═══════════════════════════════════════════════════════════════════════════
import logging
import os
import subprocess
import sys
import threading
import time

from core import memory

logger = logging.getLogger(__name__)

_SLEEP_IDLE_SECONDS = 20 * 60   # trigger threshold: 20 min with no user interaction, per spec
_SLEEP_POLL_SECONDS  = 60       # how often this thread checks the idle clock / reaps a finished subprocess

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_continuous_sleep_lock: threading.Lock          = threading.Lock()
_continuous_sleep_proc: subprocess.Popen | None = None

# Set by notify_user_interaction() the instant it actually interrupts a
# running sleep — read-and-cleared exactly once by
# core.personalities.base._build_system_prompt() so HUGO's very next reply
# can acknowledge just having woken up, in character, without this leaking
# into any later reply.
_just_woke_from_sleep: bool = False


def is_continuous_sleep_running() -> bool:
    """True if a continuous-sleep child process is currently alive. Single
    source of truth for "is HUGO sleeping right now" in this process."""
    with _continuous_sleep_lock:
        proc = _continuous_sleep_proc
    return proc is not None and proc.poll() is None


def _is_continuous_sleep_process_alive() -> bool:
    """True if any scripts/reflective_mode.py --continuous process is alive
    right now, per the real process table — not just this process's own
    _continuous_sleep_proc handle. That handle always starts as None on a
    fresh jarvis.py launch, even though a previously-spawned child can
    easily outlive the parent that spawned it (crash, force-quit, restart
    that skipped cleanup). _kill_stale_continuous_sleep_processes() is
    supposed to sweep those away once at import time, but a race between
    that sweep and this check — or any other gap — used to mean
    _start_continuous_sleep() would spawn a second one anyway, since its
    only check was the in-memory handle. This is the real incident
    documented in scripts/ollama_guard.py's own docstring: orphans
    outliving 'several jarvis.py restarts', each one keeping llama-server
    pinned. Checking the process table directly here, right before
    spawning, closes that gap regardless of why the startup sweep missed
    it."""
    try:
        pattern = os.path.join(_REPO_ROOT, "scripts", "reflective_mode.py") + " --continuous"
        result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5)
        return result.returncode == 0 and bool(result.stdout.strip())
    except Exception:
        # Unknown state — err toward NOT spawning a possible duplicate.
        return True


def _start_continuous_sleep(trigger: str) -> bool:
    """Spawns scripts/reflective_mode.py --continuous [--manual] as a child
    process, unless one is already running. Returns whether a new one was
    actually started. Fire-and-forget — the child runs indefinitely, so
    this never blocks waiting on it (see notify_user_interaction() /
    stop_continuous_sleep() for how it's told to stop)."""
    global _continuous_sleep_proc
    with _continuous_sleep_lock:
        if _continuous_sleep_proc is not None and _continuous_sleep_proc.poll() is None:
            return False
        if _is_continuous_sleep_process_alive():
            logger.warning(
                "[SLEEP] a continuous-sleep process is already alive but untracked by this "
                "process — not spawning a duplicate (see _is_continuous_sleep_process_alive)."
            )
            return False
        script = os.path.join(_REPO_ROOT, "scripts", "reflective_mode.py")
        args   = [sys.executable, script, "--continuous"]
        if trigger == "manual":
            args.append("--manual")
        try:
            log_path = os.path.join(_REPO_ROOT, "logs", "sleep_continuous_stdout.log")
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            log_file = open(log_path, "a", encoding="utf-8")
            _continuous_sleep_proc = subprocess.Popen(
                args, cwd=_REPO_ROOT, stdout=log_file, stderr=log_file,
            )
            logger.info(
                "[SLEEP] continuous sleep started — trigger=%s pid=%s",
                trigger, _continuous_sleep_proc.pid,
            )
            return True
        except Exception:
            logger.warning("[SLEEP] failed to start continuous sleep subprocess", exc_info=True)
            _continuous_sleep_proc = None
            return False


def notify_user_interaction() -> None:
    """Called at every point 'the user is interacting right now' is
    detected: core/commands.py's dispatch_command() top (covers text input,
    and voice once its transcript is ready) and core/listener.py's
    wake-word-detected moments (an earlier, voice-only signal — stops sleep
    the instant Joan starts speaking, not just once the whole utterance is
    transcribed).

    Cheap no-op when nothing is sleeping (the overwhelming common case), so
    safe to call from a hot audio-processing path. Records WHY sleep is
    about to stop directly in data/sleep_budget.json's continuous state
    BEFORE signaling — see core.sleep.run_continuous_sleep's own docstring
    on why the child preserves rather than overwrites this on exit."""
    global _just_woke_from_sleep
    with _continuous_sleep_lock:
        proc = _continuous_sleep_proc
        if proc is None or proc.poll() is not None:
            return
        try:
            import core.sleep as sleep_mod
            state = sleep_mod.load_continuous_state()
            state["last_wake"]   = memory._now_iso()
            state["stop_reason"] = "interaction"
            sleep_mod.save_continuous_state(state)
        except Exception:
            logger.warning("[SLEEP] failed to record wake state before signaling", exc_info=True)
        try:
            proc.terminate()   # SIGTERM — see scripts/reflective_mode.py's --continuous handler
        except Exception:
            logger.warning("[SLEEP] failed to signal continuous sleep subprocess", exc_info=True)
        _just_woke_from_sleep = True
    logger.info("[SLEEP] interaction detected — signaling continuous sleep to stop")


def stop_continuous_sleep() -> bool:
    """Manual 'Detener Sueño' path — POST /api/sleep/stop. Returns whether
    anything was actually stopped."""
    with _continuous_sleep_lock:
        proc = _continuous_sleep_proc
        if proc is None or proc.poll() is not None:
            return False
        try:
            import core.sleep as sleep_mod
            state = sleep_mod.load_continuous_state()
            state["stop_reason"] = "manual_stop"
            sleep_mod.save_continuous_state(state)
        except Exception:
            logger.warning("[SLEEP] failed to record manual-stop state before signaling", exc_info=True)
        try:
            proc.terminate()
        except Exception:
            logger.warning("[SLEEP] failed to signal continuous sleep subprocess", exc_info=True)
    logger.info("[SLEEP] manual stop requested")
    return True


def _sleep_loop() -> None:
    """Background thread — idle-triggers continuous sleep (spawns the
    subprocess, see _start_continuous_sleep) and reaps the handle once the
    child has exited on its own (normal SIGTERM-triggered stop, or an
    unhandled crash) so the NEXT idle period can start a fresh one. Also
    refreshes the in-memory instructions cache each tick — a continuous
    cycle's own Phase 6 (Autocrítica) may have just written new notes to
    memory_instructions.json, and this is cheap enough to just always do
    rather than trying to detect exactly when that happened (see
    memory.reload_instructions()'s own docstring). Also refreshes the
    in-memory feature-flags cache each tick, same reasoning — the sleep
    subprocess toggles 'proactividad' off/on directly on disk around its
    own run (see memory.reload_feature_flags()'s own docstring). Same
    daemon-thread + hot-reload dedup pattern as every other background
    thread in this app (see the threading.enumerate() guard at the
    bottom)."""
    import core.commands as commands
    from core import background_loops

    global _continuous_sleep_proc
    while True:
        time.sleep(_SLEEP_POLL_SECONDS)
        try:
            with _continuous_sleep_lock:
                proc = _continuous_sleep_proc
                if proc is not None and proc.poll() is not None:
                    _continuous_sleep_proc = None   # reap — free to start a new one below

            memory.reload_instructions()
            # Picks up 'proactividad' being toggled off/on directly on disk
            # by scripts/reflective_mode.py's --continuous handler (a
            # separate OS process) while a sleep session is running/ending —
            # see reload_feature_flags()'s own docstring.
            memory.reload_feature_flags()

            if background_loops._proactive_blocked():
                continue
            if time.monotonic() - commands._last_interaction_mono < _SLEEP_IDLE_SECONDS:
                continue
            _start_continuous_sleep(trigger="idle")
        except Exception:
            logger.warning("Sleep System check failed (non-critical)", exc_info=True)


def _get_parent_pid(pid: int) -> int | None:
    try:
        result = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        return int(result.stdout.strip())
    except (ValueError, OSError):
        return None


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists, just not ours to signal — still alive
    except OSError:
        return False
    return True


def _is_jarvis_process(pid: int) -> bool:
    try:
        result = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        return "jarvis.py" in result.stdout
    except OSError:
        return False


def _kill_stale_continuous_sleep_processes() -> None:
    """Called once, at module import time. Originally assumed ANY
    'reflective_mode.py --continuous' process found at this moment must be
    an orphan, since _continuous_sleep_proc always starts as None on a
    fresh process. That's true for the process calling this — but this
    module gets imported (via core.commands/core.server) by ANY script
    that touches those modules, not just a genuine jarvis.py restart —
    including a short-lived debug/test invocation run while the real
    jarvis.py is still alive and legitimately using its own continuous-
    sleep child. The original blanket `pkill -f` had no way to tell those
    apart, so it killed the live app's healthy child out from under it —
    which then just respawns a new one on its next idle check. Repeated
    over many such invocations in one debugging session, THAT pattern —
    not a genuine app bug — is what looked like runaway orphan
    accumulation (confirmed: this is exactly what happened during this
    session's own Code Engine testing).

    Fixed by checking each candidate's PARENT pid before killing it: if
    the parent is a still-alive `jarvis.py` process, this child is
    legitimately owned by a currently-running instance — leave it alone.
    Only children whose parent is dead or reparented (PPID 1/launchd,
    the previous jarvis.py no longer existing) count as genuinely
    orphaned.

    Real incident this guards against: three orphans accumulated across
    restarts over two days, each looping forever, together pinning
    Ollama's llama-server at 300%+ CPU continuously — see
    core/ollama_control.py's module docstring for the other half of that
    fix (killing llama-server once sleep genuinely stops). Best-effort — a
    failure here just means a genuine orphan keeps running until the next
    restart or scripts/ollama_guard.py's own periodic sweep catches it."""
    script_pattern = os.path.join(_REPO_ROOT, "scripts", "reflective_mode.py") + " --continuous"
    try:
        result = subprocess.run(["pgrep", "-f", script_pattern], capture_output=True, text=True, timeout=5)
        candidate_pids = [int(p) for p in result.stdout.split() if p.strip()]
        if not candidate_pids:
            return

        my_pid = os.getpid()
        truly_orphaned = []
        for pid in candidate_pids:
            if pid == my_pid:
                continue
            ppid = _get_parent_pid(pid)
            if ppid and _is_pid_alive(ppid) and _is_jarvis_process(ppid):
                continue   # legitimately owned by a currently-running jarvis.py — not orphaned
            truly_orphaned.append(pid)

        if not truly_orphaned:
            return
        logger.warning(
            "[SLEEP] found %d orphaned continuous-sleep process(es) from a previous session (pid(s) %s) — killing.",
            len(truly_orphaned), ", ".join(str(p) for p in truly_orphaned),
        )
        for pid in truly_orphaned:
            try:
                os.kill(pid, 15)   # SIGTERM — same graceful-shutdown signal stop_continuous_sleep() sends
            except OSError:
                pass
    except Exception:
        logger.warning("[SLEEP] failed to sweep stale continuous-sleep processes at startup", exc_info=True)


_kill_stale_continuous_sleep_processes()

# Same reload-dedup reasoning as core/background_loops.py's own threads —
# for the Sleep System's own idle-trigger thread, entirely separate from
# reflective-loop, own budget/trigger/state.
if not any(t.name == "sleep-loop" for t in threading.enumerate()):
    threading.Thread(target=_sleep_loop, daemon=True, name="sleep-loop").start()
