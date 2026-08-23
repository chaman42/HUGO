# ═══════════════════════════════════════════════════════════════════════════
# OLLAMA CONTROL — shared process-management helpers for keeping Ollama's
# resource footprint bounded: confirm the `ollama serve` daemon is up before
# a sleep session needs it, and kill the `llama-server` model-serving
# subprocess once sleep is done with it, rather than leaving it resident
# (and, per a real incident on this machine, burning 300%+ CPU) indefinitely.
#
# Two distinct processes are in play here, and this module only ever touches
# one of them:
#   - `ollama serve` — the long-running daemon (HTTP API on :11434). Meant to
#     stay up as an always-on service; never killed by this module.
#   - `llama-server` — a child process the DAEMON spawns on demand to load a
#     specific model into memory and serve it. This is what actually
#     consumes the CPU/RAM while a model is resident, and what Ollama's own
#     OLLAMA_KEEP_ALIVE (default 5 min idle) is supposed to unload
#     automatically — see this module's own docstring on why relying on
#     that default alone wasn't enough in practice (scripts/reflective_mode.py
#     and core.sleep_control call the functions here explicitly instead).
#
# Used by: scripts/reflective_mode.py (kills llama-server after every sleep
# session/continuous run), jarvis.py's shutdown handler, and
# scripts/ollama_guard.py (the periodic launchd safety net).
# ═══════════════════════════════════════════════════════════════════════════
import logging
import os
import subprocess
import time
import urllib.request

logger = logging.getLogger(__name__)

OLLAMA_HOST      = "http://localhost:11434"
OLLAMA_TAGS_URL  = f"{OLLAMA_HOST}/api/tags"
LLAMA_SERVER_PATTERN = "llama-server"   # pgrep/pkill -f pattern — matches ollama's own spawned subprocess only

# Design Studio autopilot has no discrete OS process for scripts/ollama_guard.py
# to pgrep for (unlike the Sleep System's scripts/reflective_mode.py) — it's
# just Flask request handlers inside the main jarvis.py process, one HTTP
# call per zone. This lock file is the equivalent signal: written once by
# POST /api/designs/autopilot-start before the zone loop begins, removed
# once by POST /api/designs/autopilot-stop after it ends, so the guard can
# recognize an in-flight run as a legitimate reason for llama-server to stay
# resident instead of killing it mid-generation (see is_autopilot_running's
# own docstring for the staleness handling this needs that a pgrep check
# doesn't).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REFLECTIVE_MODE_PATTERN = os.path.join(_REPO_ROOT, "scripts", "reflective_mode.py")
AUTOPILOT_LOCK_PATH = os.path.join(_REPO_ROOT, "data", "autopilot_running.lock")
AUTOPILOT_LOCK_MAX_AGE_SECONDS = 30 * 60   # generous crash-recovery ceiling — comfortably above any real run

# Same lock-file shape as the autopilot one above, for the same reason:
# core.code_engine.tools.orchestrator.ToolOrchestrator.execute_goal() makes
# many separate LLM calls in sequence (Planner.create_plan(), one per
# executed step, Debugger diagnoses, CodeReviewer checks, ...), and every
# one of those _llm_call() helpers across code_engine/tools/*.py
# individually does ensure-before/kill-after around ITSELF. Confirmed by
# direct measurement: generating 2 characters took ~45s, almost entirely
# cold model-load (qwen2.5-coder, 4.7GB, CPU-only) — killing and reloading
# that between every single call in one cycle turns what should be "45s
# once + N x (fast) generation" into "N x 45s+", making a multi-step goal
# take many times longer than necessary. kill_llama_server() itself checks
# this lock (see its own docstring) so no individual call site needs to —
# same as is_autopilot_running() being checked by callers today, just
# folded into the shared kill function instead, since Code Engine already
# has 6+ independent call sites where "remember to check" is exactly the
# kind of thing that's easy to miss at a new one.
CODE_ENGINE_CYCLE_LOCK_PATH = os.path.join(_REPO_ROOT, "data", "code_engine_cycle.lock")
CODE_ENGINE_CYCLE_LOCK_MAX_AGE_SECONDS = 60 * 60   # generous — a slow multi-step goal on CPU-only Ollama can genuinely run long


def is_ollama_daemon_reachable() -> bool:
    """True if the `ollama serve` daemon is up and answering — same check
    as core.sleep_llm._ollama_available() (duplicated rather than imported,
    same dependency-isolation reasoning as the rest of the Sleep System:
    this module needs to stay import-light enough for scripts/ollama_guard.py
    to use standalone)."""
    try:
        req = urllib.request.Request(OLLAMA_TAGS_URL, method="GET")
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False


def ensure_ollama_daemon_running() -> None:
    """Starts `ollama serve` (detached, own session) if the daemon isn't
    already reachable. Never raises — a sleep phase that needs Ollama and
    finds it unreachable just falls through to the existing Groq fallback
    (see core.sleep_llm._groq_call), same as if this function didn't exist
    at all; this is a best-effort convenience, not a hard dependency."""
    if is_ollama_daemon_reachable():
        return
    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        logger.info("[OLLAMA] daemon was not reachable — started `ollama serve`.")
    except Exception:
        logger.debug("[OLLAMA] failed to start `ollama serve`", exc_info=True)


def is_llama_server_running() -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-f", LLAMA_SERVER_PATTERN],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except Exception:
        return False


def kill_llama_server() -> bool:
    """Kills Ollama's model-serving subprocess ONLY — never the `ollama
    serve` daemon itself, which stays up as the always-on service and will
    transparently spawn a fresh llama-server the next time a request
    actually needs one. Safe to call liberally (a no-op if nothing is
    running). Returns whether anything was actually killed. Never raises.

    Defers (no-op, returns False) while is_code_engine_cycle_running() —
    every individual _llm_call() across core/code_engine/tools/*.py calls
    this in its own `finally` block, and without this check each one would
    tear the model down again the instant its single call finished, right
    before the NEXT call in the same Orchestrator cycle needed it back."""
    if is_code_engine_cycle_running():
        return False
    if not is_llama_server_running():
        return False
    try:
        subprocess.run(["pkill", "-f", LLAMA_SERVER_PATTERN], capture_output=True, timeout=5)
        logger.info("[OLLAMA] killed llama-server — sleep session ended, no reason to keep the model resident.")
        return True
    except Exception:
        logger.debug("[OLLAMA] failed to kill llama-server", exc_info=True)
        return False


def mark_autopilot_running() -> None:
    """Called once by POST /api/designs/autopilot-start, before the zone
    loop begins. Best-effort — a failure to write this file just means
    autopilot risks losing llama-server to scripts/ollama_guard.py mid-run,
    no worse than before this lock existed. Never raises."""
    try:
        os.makedirs(os.path.dirname(AUTOPILOT_LOCK_PATH), exist_ok=True)
        with open(AUTOPILOT_LOCK_PATH, "w") as f:
            f.write(str(time.time()))
    except Exception:
        logger.debug("[OLLAMA] failed to write autopilot lock", exc_info=True)


def clear_autopilot_running() -> None:
    """Called once by POST /api/designs/autopilot-stop, after the zone loop
    ends. Best-effort, never raises — if this doesn't run (e.g. the tab was
    closed mid-run), AUTOPILOT_LOCK_MAX_AGE_SECONDS is what eventually lets
    scripts/ollama_guard.py reclaim llama-server instead of the lock
    blocking it forever."""
    try:
        os.remove(AUTOPILOT_LOCK_PATH)
    except FileNotFoundError:
        pass
    except Exception:
        logger.debug("[OLLAMA] failed to remove autopilot lock", exc_info=True)


def is_autopilot_running() -> bool:
    """True if a Design Studio autopilot run is currently in flight — checked
    by scripts/ollama_guard.py alongside its existing reflective_mode.py
    process check before killing llama-server (real incident this fixes:
    the guard's 10-minute sweep killing llama-server mid-zone-generation,
    autopilot silently falling back to an empty zone with no error surfaced
    anywhere). A lock file has no process-table equivalent to double-check
    against, so age is the next best signal — older than
    AUTOPILOT_LOCK_MAX_AGE_SECONDS is treated as stale (a crash or force-quit
    that skipped autopilot-stop) rather than trusted forever. Never raises."""
    try:
        age = time.time() - os.path.getmtime(AUTOPILOT_LOCK_PATH)
        return age < AUTOPILOT_LOCK_MAX_AGE_SECONDS
    except Exception:
        return False


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


def _get_parent_pid(pid: int) -> int | None:
    try:
        result = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        return int(result.stdout.strip())
    except (ValueError, OSError):
        return None


def kill_orphaned_continuous_sleep_processes() -> int:
    """Kills scripts/reflective_mode.py --continuous processes whose parent
    jarvis.py has died — same orphan definition as core.sleep_control's own
    _kill_stale_continuous_sleep_processes(), duplicated here (same
    import-light reasoning as this module's other _is_pid_alive/
    _is_jarvis_process helpers) so scripts/ollama_guard.py can run this on
    its own 10-minute launchd timer instead of only at the next jarvis.py
    launch.

    Real incident this fixes: core.sleep_control's sweep only runs once, at
    jarvis.py's own startup — while HUGO stays closed, nothing ever
    triggers it again. Orphaned continuous-sleep children (parent jarvis.py
    already dead) accumulated for over a day, and each one made
    scripts/ollama_guard.py's own _any_sleep_process_alive() check treat
    them as a legitimate in-flight session, leaving llama-server resident
    and busy the entire time even with the app closed.

    Returns the number of orphans killed. Never raises."""
    script_pattern = REFLECTIVE_MODE_PATTERN + " --continuous"
    try:
        result = subprocess.run(["pgrep", "-f", script_pattern], capture_output=True, text=True, timeout=5)
        candidate_pids = [int(p) for p in result.stdout.split() if p.strip()]
        if not candidate_pids:
            return 0

        orphans = []
        for pid in candidate_pids:
            ppid = _get_parent_pid(pid)
            if ppid and _is_pid_alive(ppid) and _is_jarvis_process(ppid):
                continue   # legitimately owned by a currently-running jarvis.py — not orphaned
            orphans.append(pid)

        for pid in orphans:
            try:
                os.kill(pid, 15)   # SIGTERM — same graceful-shutdown signal stop_continuous_sleep() sends
            except OSError:
                pass
        if orphans:
            logger.info("[OLLAMA] killed %d orphaned continuous-sleep process(es): %s", len(orphans), orphans)
        return len(orphans)
    except Exception:
        logger.debug("[OLLAMA] failed to sweep orphaned continuous-sleep processes", exc_info=True)
        return 0


def mark_code_engine_cycle_running() -> None:
    """Called once by ToolOrchestrator.execute_goal(), right after its
    permission check passes and before the first LLM call of the cycle.
    Writes THIS process's own pid (not just a timestamp) — see
    is_code_engine_cycle_running()'s own docstring for why: same class of
    bug as the one fixed in core.sleep_control's continuous-sleep sweep
    earlier this session (age-only staleness can't tell 'still genuinely
    running' apart from 'the owning process died and left this behind').
    Best-effort — a failure to write this just means the cycle loses the
    keep-warm optimization (back to killing/reloading between every call,
    the pre-fix behavior), never a hard failure. Never raises."""
    try:
        os.makedirs(os.path.dirname(CODE_ENGINE_CYCLE_LOCK_PATH), exist_ok=True)
        with open(CODE_ENGINE_CYCLE_LOCK_PATH, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        logger.debug("[OLLAMA] failed to write code_engine cycle lock", exc_info=True)


def clear_code_engine_cycle_running() -> None:
    """Called once by ToolOrchestrator.execute_goal(), in a `finally` so it
    always runs (success, failure, or escalation) — removes the lock so
    kill_llama_server() resumes killing normally, then immediately calls it
    once to actually free the now-idle model (the cycle keeping it warm
    THROUGHOUT is the whole point; leaving it resident forever afterward
    would just reintroduce the original CPU-pinning problem this package's
    ensure/kill discipline exists to prevent)."""
    try:
        os.remove(CODE_ENGINE_CYCLE_LOCK_PATH)
    except FileNotFoundError:
        pass
    except Exception:
        logger.debug("[OLLAMA] failed to remove code_engine cycle lock", exc_info=True)
    kill_llama_server()


def is_code_engine_cycle_running() -> bool:
    """True if a Code Engine Orchestrator cycle is currently in flight.

    Checks the OWNING PROCESS's actual liveness first (is the pid in the
    lock file still alive, and is it genuinely a jarvis.py process — not
    some unrelated process that happened to reuse the pid after jarvis.py
    exited) — a jarvis.py crash/restart mid-cycle leaves this lock behind
    with no thread left to ever clear it, and without this check it would
    incorrectly report "running" for up to
    CODE_ENGINE_CYCLE_LOCK_MAX_AGE_SECONDS (an hour) even though nothing
    is. Same bug class, same fix shape as core.sleep_control's continuous-
    sleep orphan sweep earlier this session (age-only staleness check vs.
    actually verifying the owning process). Falls back to the pure-age
    check only if the lock's content isn't a parseable pid (defensive,
    shouldn't happen given mark_code_engine_cycle_running() always writes
    one). Never raises."""
    try:
        with open(CODE_ENGINE_CYCLE_LOCK_PATH, "r") as f:
            content = f.read().strip()
        age = time.time() - os.path.getmtime(CODE_ENGINE_CYCLE_LOCK_PATH)
        if age >= CODE_ENGINE_CYCLE_LOCK_MAX_AGE_SECONDS:
            return False
        try:
            pid = int(content)
        except ValueError:
            return True   # pre-fix lock format (timestamp, not pid) — fall back to age-only
        return _is_pid_alive(pid) and _is_jarvis_process(pid)
    except Exception:
        return False
