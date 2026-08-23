"""
JarvisLite — main entry point.

Run from the project root:
    python jarvis.py

Requires a .env file at the project root with:
    ANTHROPIC_API_KEY=sk-ant-...
"""
import importlib
import logging
import os
import sys
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Bug fix ("ollama: no such file or directory" — see logs/code_engine.log):
# jarvis.py is spawned by launcher.py, itself spawned by Electron/launchd,
# which hands it the bare macOS default PATH (/usr/bin:/bin:/usr/sbin:/sbin)
# — confirmed via `ps eww` on the live process. Every `subprocess.run(["ollama",
# ...])`/`Popen(["ollama", "serve"], ...)` call in this codebase (core/
# ollama_control.py, core/code_engine/__init__.py's LLMRouter) resolves that
# bare command name against THIS process's own PATH, which never included
# wherever Homebrew actually installed it (/usr/local/bin on Intel,
# /opt/homebrew/bin on Apple Silicon) — every one of those calls was silently
# failing, in production, the whole time; only ever "worked" when tested from
# an interactive shell that already had a full PATH. Same category of bug
# already fixed once for scripts/rebuild_app.sh's own npm/node lookup — see
# that script's own comment. Fixed here, once, for the whole process (every
# subprocess this app spawns inherits this), rather than patching each
# individual `subprocess` call site across ollama_control.py/code_engine/.
os.environ["PATH"] = "/usr/local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:" + os.environ.get("PATH", "")

# ---------------------------------------------------------------------------
# Logging — must be set up before any core imports so all modules inherit it.
# ---------------------------------------------------------------------------

os.makedirs("logs", exist_ok=True)

from core.logging_setup import _setup_logging, _log_audio_devices

_setup_logging()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global exception handler — log every uncaught exception, never crash.
# ---------------------------------------------------------------------------

def _handle_exception(exc_type, exc_value, exc_tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))


sys.excepthook = _handle_exception

# SIGTERM (e.g. kill <pid>) should behave the same as Ctrl-C so the
# non-daemon listener thread exits and the process shuts down cleanly.
import signal as _signal

def _handle_sigterm(signum, frame):
    raise KeyboardInterrupt

_signal.signal(_signal.SIGTERM, _handle_sigterm)

# ---------------------------------------------------------------------------
# Core module imports
# ---------------------------------------------------------------------------

import core.listener as listener_mod
import core.speaker as speaker_mod
import core.commands as commands_mod
import core.voice as voice_mod
import core.server as server_mod
# Import-time side effect: starts its own pre-warm background thread (see
# core/embeddings.py's own module comment) — imported here, not left to
# lazy-import on first use inside memory_select.py, so the ~25s model-load
# cost happens during the ~16s startup window instead of on some unlucky
# first conversation turn.
import core.embeddings as embeddings_mod


# ---------------------------------------------------------------------------
# Microphone permission check (macOS / AVFoundation via pyobjc)
# ---------------------------------------------------------------------------

def _check_mic_permission() -> bool:
    """
    On macOS, verify microphone permission via AVFoundation (pyobjc).
    Returns True if access is granted or indeterminate (OS will prompt).
    Returns False if explicitly denied or restricted, and logs clear instructions.
    Safe no-op on non-macOS or when pyobjc is not installed.
    """
    try:
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio
    except ImportError:
        return True  # pyobjc not available — assume access is granted

    try:
        # AVAuthorizationStatus: 0=notDetermined, 1=restricted, 2=denied, 3=authorized
        status = AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio)

        if status == 3:  # authorized
            logger.info("Microphone access: authorized.")
            return True

        if status == 2:  # denied
            logger.error(
                "Microphone access DENIED. "
                "Jarvis cannot listen without microphone access. "
                "Fix: System Preferences → Privacy & Security → Microphone → "
                "enable Terminal (or the app running Jarvis), then restart."
            )
            # Open System Preferences automatically
            try:
                import subprocess as _sp
                _sp.Popen(
                    ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"],
                    stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                )
            except Exception:
                pass
            # Notify frontend
            try:
                server_mod.socketio.emit("mic_status", {"status": "denied"})
            except Exception:
                pass
            return False

        if status == 1:  # restricted (MDM / parental controls)
            logger.warning(
                "Microphone access restricted by system policy (MDM or parental controls). "
                "Jarvis may not be able to hear you."
            )
            return False

        # status == 0: notDetermined — sounddevice will trigger the OS permission prompt
        logger.info("Microphone permission not yet determined — the OS will prompt on first use.")
        return True

    except Exception as exc:
        logger.warning("Could not check microphone permission: %s", exc)
        return True

# ---------------------------------------------------------------------------
# Listener thread management
# ---------------------------------------------------------------------------

_stop_event = threading.Event()
_listener_thread: threading.Thread | None = None
_listener_lock = threading.Lock()


def _run_listener(stop_event):
    try:
        import core.listener as l
        l.listen(stop_event)
    except Exception:
        logger.exception("Listener thread crashed")


def start_listener():
    global _listener_thread, _stop_event
    with _listener_lock:
        _stop_event = threading.Event()
        _listener_thread = threading.Thread(
            target=_run_listener,
            args=(_stop_event,),
            daemon=False,
            name="listener",
        )
        _listener_thread.start()
        logger.info("Listener thread started.")


def restart_listener():
    global _stop_event, _listener_thread
    with _listener_lock:
        logger.info("Restarting listener...")
        _stop_event.set()
        if _listener_thread and _listener_thread.is_alive():
            _listener_thread.join(timeout=5)
    start_listener()


# ---------------------------------------------------------------------------
# Hot-reload file watcher
# ---------------------------------------------------------------------------

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler

    _MODULE_MAP = {
        "listener": "core.listener",
        "speaker": "core.speaker",
        "commands": "core.commands",
        "voice": "core.voice",
        # Bug fix: tools.py was missing from this map, so editing it never
        # triggered a hot-reload. commands.py (which IS hot-reloaded) holds a
        # reference to the tools module via `from core import tools` — when
        # commands.py picked up new code calling a function that only exists
        # in the newer tools.py, the still-stale in-memory core.tools module
        # didn't have it yet, raising "module 'core.tools' has no attribute
        # ..." on every request until a full restart.
        "tools": "core.tools",
        # core/commands.py was split into memory.py/personality.py/intent.py
        # (pure refactor) — each needs its own entry here for the same
        # reason tools.py does above, or editing one of them during
        # development would silently require a full restart to take effect.
        "memory": "core.memory",
        "personality": "core.personality",
        "intent": "core.intent",
    }

    class _CoreReloader(FileSystemEventHandler):
        def __init__(self):
            self._last_reload: dict[str, float] = {}

        def on_modified(self, event):
            if event.is_directory or not event.src_path.endswith(".py"):
                return

            src = event.src_path
            now = time.time()
            # Debounce: ignore duplicate events within 1 s
            if now - self._last_reload.get(src, 0) < 1.0:
                return
            self._last_reload[src] = now

            stem = Path(src).stem
            module_name = _MODULE_MAP.get(stem)
            if not module_name:
                return

            logger.info("Change detected in %s — reloading %s", src, module_name)
            try:
                mod = importlib.import_module(module_name)
                importlib.reload(mod)
                logger.info("Reloaded %s successfully.", module_name)
                if stem == "listener":
                    restart_listener()
            except Exception:
                logger.exception("Failed to reload %s", module_name)

    _WATCHDOG_AVAILABLE = True

except ImportError:
    _WATCHDOG_AVAILABLE = False
    logger.warning(
        "watchdog not installed — hot reload disabled. "
        "Install with: pip install watchdog"
    )

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os as _os
    logger.info("Starting JarvisLite...  (PID=%d, PPID=%d)", _os.getpid(), _os.getppid())
    logger.info("Session context: TERM=%s  TERM_PROGRAM=%s  DISPLAY=%s",
                _os.environ.get("TERM", "<unset>"),
                _os.environ.get("TERM_PROGRAM", "<unset>"),
                _os.environ.get("DISPLAY", "<unset>"))
    _log_audio_devices()

    # Add WebSocket log bridge BEFORE starting listener so all logs are captured
    _ws_handler = server_mod.SocketIOLogHandler()
    _ws_handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(_ws_handler)

    # Start Flask/SocketIO server
    server_mod.start()
    logger.info("Web UI available at http://localhost:8080")

    if _WATCHDOG_AVAILABLE:
        _observer = Observer()
        _observer.schedule(_CoreReloader(), path="core", recursive=False)
        _observer.start()
        logger.info("Hot-reload watcher active on core/")

    mic_ok = _check_mic_permission()
    if not mic_ok:
        logger.error(
            "Microphone access is not available — listener will not start. "
            "Grant access in System Preferences → Privacy & Security → Microphone, then restart Jarvis."
        )
        # Still mark ready so the frontend can connect and show the mic error
        server_mod.set_ready(True)
        server_mod.emit_force_reload()
        # Boot progress, stage 7/7 — no Vosk/mic/Kokoro to wait on in this
        # path, so jump straight to ready. See _signal_ready() below for the
        # normal (mic-available) path's stages 4-5-7.
        try:
            server_mod.socketio.emit("boot_progress", {"stage": "jarvis_ready", "percent": 100, "label": "Sistemas en línea."})
        except Exception:
            pass
    else:
        start_listener()

        # Signal ready only after ALL subsystems confirm they are live.
        # Runs in a background thread so it never blocks the Flask server or main loop.
        def _signal_ready():
            # Boot progress, stage 4/7 — jarvis.py's own socket server is
            # already up (server_mod.start() ran before this thread was
            # even created — see above), so a client connected earlier in
            # the boot sequence (stages 1-3, emitted by launcher.py) can
            # already receive this. See emit_boot_progress() in launcher.py
            # for the earlier stages and why percent is monotonic frontend-
            # side (launcher.py and jarvis.py emit on two independent
            # sockets, so arrival order isn't guaranteed).
            try:
                server_mod.socketio.emit("boot_progress", {"stage": "vosk_loading", "percent": 55, "label": "Cargando modelos de voz..."})
            except Exception:
                pass

            # ── Step 1: Vosk ES + EN models ──────────────────────────────────
            # models_ready is set inside listener._get_models() after both recognizers load.
            if not listener_mod.models_ready.wait(timeout=120):
                logger.error(
                    "STARTUP TIMEOUT: Vosk models did not finish loading within 120s. "
                    "Retrying listener — verify VOSK_MODEL_ES_PATH / VOSK_MODEL_EN_PATH."
                )
                restart_listener()
                # Allow another 120s after the retry.
                if not listener_mod.models_ready.wait(timeout=120):
                    logger.error(
                        "STARTUP TIMEOUT: Vosk model load retry also failed (240s total). "
                        "Jarvis will NOT be marked ready — restart manually."
                    )
                    return

            # ── Step 2: Microphone stream ─────────────────────────────────────
            # mic_ready is set inside listener.listen() once sd.InputStream opens.
            # This is the critical gate: jarvis_ready must not fire before audio capture
            # is live, otherwise the system appears ready but is actually deaf.
            if not listener_mod.mic_ready.wait(timeout=30):
                logger.error(
                    "STARTUP TIMEOUT: Mic stream did not open within 30s of models loading. "
                    "Retrying listener — check audio device and System Preferences → Microphone."
                )
                restart_listener()
                if not listener_mod.mic_ready.wait(timeout=30):
                    logger.error(
                        "STARTUP TIMEOUT: Mic stream retry also failed. "
                        "Jarvis will NOT be marked ready — fix audio access and restart."
                    )
                    return

            # ── Step 3: Kokoro TTS primary voice ─────────────────────────────
            # kokoro_ready is set in voice._prewarm_kokoro() after the first voice
            # (JARVIS/em_santa) warms up. Non-fatal if it times out — TTS still works,
            # the first utterance will just be slower.
            # Boot progress, stage 5/7.
            try:
                server_mod.socketio.emit("boot_progress", {"stage": "kokoro_prewarm", "percent": 70, "label": "Precalentando síntesis de voz..."})
            except Exception:
                pass
            if not voice_mod.kokoro_ready.wait(timeout=30):
                logger.warning(
                    "STARTUP WARNING: Kokoro TTS did not finish pre-warming within 30s. "
                    "First voice response may be slow (Kokoro still loading in background)."
                )

            # All required subsystems confirmed ready — safe to mark Jarvis as ready.
            server_mod.set_ready(True)
            server_mod.emit_force_reload()
            # Boot progress, stage 7/7 — the real 'jarvis_ready' milestone.
            # Launcher.py also detects readiness independently (polling
            # /api/ready and emitting its own 'jarvis_ready' event), but
            # this fires from the actual source of truth with zero polling
            # latency in between.
            try:
                server_mod.socketio.emit("boot_progress", {"stage": "jarvis_ready", "percent": 100, "label": "Sistemas en línea."})
            except Exception:
                pass

        threading.Thread(target=_signal_ready, daemon=True, name="ready-signal").start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down JarvisLite...")
        _stop_event.set()
        # Stop any continuous-sleep child THIS process is tracking (a clean
        # shutdown reaching this point already implies one thing: it's not
        # an abrupt SIGKILL/crash, so there's an actual chance to tell it to
        # stop rather than leaving it to become an orphan — see
        # core.sleep_control's own startup sweep for the case where this
        # step gets skipped anyway). Then release Ollama's model-serving
        # subprocess — real incident on this machine: llama-server pinned
        # at 300%+ CPU for two days from orphaned sleep processes never
        # doing this (see core/ollama_control.py's own docstring). Both
        # best-effort; neither should ever block process exit.
        try:
            import core.sleep_control as sleep_control_mod
            sleep_control_mod.stop_continuous_sleep()
        except Exception:
            logger.warning("Failed to stop continuous sleep on shutdown", exc_info=True)
        try:
            import core.ollama_control as ollama_control_mod
            ollama_control_mod.kill_llama_server()
        except Exception:
            logger.warning("Failed to kill llama-server on shutdown", exc_info=True)
        if _WATCHDOG_AVAILABLE:
            _observer.stop()
            _observer.join()
        logger.info("Goodbye.")
