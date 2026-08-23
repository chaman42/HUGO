"""Text-to-speech: mute state, the serialized TTS worker queue, the Kokoro
engine (primary) with a macOS `say` fallback, the optional XTTS v2
voice-cloning engine (TTS_ENGINE=xtts in .env — see core/tts_xtts.py and
_speak_xtts_blocking below, which falls back to Kokoro on failure), and mic
auto-unmute after speaking. Natural time-of-day phrasing lives in
core.voice_time."""
import os
import queue
import re
import subprocess
import logging
import tempfile
import threading
import time

from core.voice_time import _naturalize_times

logger = logging.getLogger(__name__)

# Seconds to ignore the wake word after TTS finishes (covers mic reverb + queue lag).
COOLDOWN_SECONDS = 2.0

# Matches the natural-speech-pause markers ('…' or '—') that personality system
# prompts (core/commands.py) are instructed to insert at breath points in longer
# responses. Kokoro splits on this and inserts real silence between segments;
# `say` gets the markers translated into an SSML-style [[slnc]] tag instead.
_PAUSE_RE = re.compile(r"\s*[…—]\s*")

# Silence inserted between Kokoro segments at a pause marker, and passed to
# macOS `say` via [[slnc <ms>]] — within the 200-300ms "natural breath" range.
PAUSE_SILENCE_SECONDS = 0.25
PAUSE_SILENCE_MS      = int(PAUSE_SILENCE_SECONDS * 1000)

# If Kokoro audio generation takes longer than this, stop pulling further
# chunks from the pipeline (see _speak_kokoro_blocking's per-chunk watchdog
# check) — whatever already streamed to the output device keeps playing,
# the reply just ends early rather than generating indefinitely. Chunks
# already streamed are never discarded/replayed via `say` (that fallback
# only fires if NOTHING was ever actually played — see that function).
KOKORO_MAX_GEN_SECS = 4.0

# Kokoro's own model output rate — fixed, not configurable. macOS output
# devices are commonly 48000Hz-native (built-in speakers, AirPods over
# A2DP); streaming raw 24000Hz straight into sd.OutputStream forces
# CoreAudio to resample it live, in the low-latency real-time path, which
# measurably distorts the audio (confirmed via A/B test: the same audio
# resampled to the device's native rate in software and streamed at THAT
# rate played back clean, while the raw 24000Hz stream did not — the
# distortion reproduces even in a bare, app-free script, so it's a
# property of this live-resample path, not app/Bluetooth-specific). See
# _speak_kokoro_blocking's own resampling of every chunk before writing.
KOKORO_NATIVE_SAMPLERATE = 24000

# Set True locally to also write everything Kokoro streams to a debug WAV
# file (see _speak_kokoro_blocking) for offline inspection — streaming
# playback itself never depends on this; off by default so normal
# operation never pays the extra concatenate+write cost this reintroduces.
_KOKORO_DEBUG_WAV = False

# ---------------------------------------------------------------------------
# Kokoro voice assignment — one shared Spanish pipeline (lang_code='e').
#
# LIRA: ef_dora — the only native Spanish female voice in Kokoro-82M.
#       Warmer and more natural than the JARVIS/FRIDAY voices this app used
#       to also support (removed 2026-08-10 — LIRA is the only personality
#       now). Fallback: macOS "Monica" (Spain Spanish female).
# ---------------------------------------------------------------------------

KOKORO_VOICE_LIRA     = "ef_dora"
KOKORO_FALLBACK_LIRA   = "Monica"                    # macOS Spain Spanish female

# ---------------------------------------------------------------------------
# TTS queue and worker
# ---------------------------------------------------------------------------

_lock     = threading.Lock()
_spoke_at: float = 0.0
_tts_proc: subprocess.Popen | None = None   # tracks the active say/afplay subprocess
_tts_stream = None   # sounddevice.OutputStream | None — tracks Kokoro's active streaming playback (mutually exclusive with _tts_proc)
_tts_stop_event: threading.Event | None = None   # signals Kokoro's producer/consumer threads (see _speak_kokoro_blocking) to stop

_tts_queue: queue.Queue = queue.Queue()
_tts_busy  = threading.Event()   # set while a TTS job is actively playing

# ---------------------------------------------------------------------------
# TTS mute — voice-output-only mute, independent of listener.py's mic mute.
# When muted, LIRA still listens, still processes, still replies in chat —
# she just doesn't speak the reply out loud. Plain module-level bool, no
# lock, mirroring listener.py's _muted (simple flag flip, GIL-safe enough).
# ---------------------------------------------------------------------------
_tts_muted = False


def set_tts_muted(muted: bool) -> None:
    global _tts_muted
    _tts_muted = muted


def is_tts_muted() -> bool:
    return _tts_muted


# ---------------------------------------------------------------------------
# Ducking — interrupt-feature infrastructure, step 1 (see
# ~/.claude memory project_interrupt_feature.md for the full design/status).
# A live, read-every-chunk gain multiplier applied to TTS playback so
# core/listener.py can quiet her down the instant it detects possible
# interrupt speech, without stopping playback outright — "duck, don't
# decide instantly" per Joan's own design call. Plain module-level float,
# same GIL-safe-enough reasoning as _tts_muted above: one writer
# (listener.py's audio callback thread) and one reader (whichever TTS
# engine's playback consumer is currently running), no read-modify-write
# race since it's always a full replacement, not an increment.
#
# Only Kokoro and XTTS actually read this (both stream through their own
# sounddevice.OutputStream, sample-by-sample) — core.voice._speak_say_blocking
# runs macOS `say` as a black-box subprocess with no per-sample hook, so it
# can't be smoothly ducked with this mechanism. Not a bug to fix here: `say`
# is this app's fallback engine, not the primary path, and the alternative
# (piping `say`'s output through our own stream instead of letting it play
# directly) is real added scope for a fallback path — noted as a known gap,
# not solved by this step.
_duck_gain = 1.0


def set_duck_gain(gain: float) -> None:
    global _duck_gain
    _duck_gain = max(0.0, min(1.0, gain))


def get_duck_gain() -> float:
    return _duck_gain


# Live self-output RMS — the actual current TTS chunk's own loudness,
# updated by Kokoro/XTTS's playback consumer on every chunk write, read by
# core.listener's duck-trigger check (core.vad.duck_gate_update) so the
# trigger can compare "how loud is the mic right now" against "how loud is
# she outputting right now" as a RATIO instead of a fixed absolute RMS
# number. A fixed threshold breaks the moment playback volume changes (self-
# bleed scales with it); a ratio holds up across volume levels because both
# sides scale together. Same single-writer/single-reader GIL-safety
# reasoning as _duck_gain above. Resets to 0.0 whenever nothing is actually
# playing (silence between segments, or no TTS active at all) so the ratio
# check naturally can't fire from mic noise alone while she's silent.
_self_output_rms = 0.0


def set_self_output_rms(rms: float) -> None:
    global _self_output_rms
    _self_output_rms = max(0.0, rms)


def get_self_output_rms() -> float:
    return _self_output_rms


# ---------------------------------------------------------------------------
# TTS engine — 'kokoro' (default) or 'xtts', live-switchable via
# POST /api/set_tts_engine (see core/routes_control.py) without a jarvis.py
# restart — core.commands._say_for() reads get_tts_engine() fresh on every
# turn rather than a frozen constant, so a switch applies to the very next
# reply. Persisted to .env so it also survives a real restart.
#
# load_dotenv() is called explicitly here (not assumed already done by
# whatever imported this module first) because core.commands imports
# core.voice BEFORE core.groq_config — the module that actually calls
# load_dotenv() elsewhere in this codebase — so relying on import order
# would risk reading this env var before .env is even loaded. Same
# defensive pattern as core.sleep_state/core.reflective/core.tools.
#
# override=True (bug fix — TTS engine appeared to "reset on restart"):
# jarvis.py is spawned by launcher.py (core.process_manager._start_jarvis,
# a long-running process), which inherits whatever launcher.py's own
# in-memory os.environ holds at that moment — including a load_dotenv()
# call from ANY module launcher.py itself imports, made once, however
# long ago, whenever launcher.py started. If .env's TTS_ENGINE has been
# changed on disk since then (exactly what POST /api/set_tts_engine does
# — see _persist_tts_engine_to_env below), the default override=False
# means jarvis.py's own load_dotenv() call here silently keeps that STALE
# inherited value instead of the current file content — confirmed by
# reproducing it directly: `TTS_ENGINE=kokoro python3 -c "import core.voice..."`
# ignored a .env containing TTS_ENGINE=say without this flag. TTS_ENGINE
# is entirely owned by this app's own .env (nothing legitimate sets it as
# a real exported secret the way GROQ_API_KEY etc. might be), so the file
# should always be authoritative on a fresh process start.
# ---------------------------------------------------------------------------
from dotenv import load_dotenv

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV_PATH  = os.path.join(_REPO_ROOT, ".env")
load_dotenv(_ENV_PATH, override=True)

_tts_engine_lock = threading.Lock()
_tts_engine      = os.getenv("TTS_ENGINE", "kokoro").strip().lower()


def get_tts_engine() -> str:
    with _tts_engine_lock:
        return _tts_engine


def _persist_tts_engine_to_env(engine: str) -> None:
    """Rewrites .env's TTS_ENGINE= line in place, preserving every other
    line untouched — appends the line if .env doesn't have one yet.
    Best-effort: set_tts_engine() already applied the in-memory switch
    before calling this, so a disk-write failure here never blocks the
    'changes apply immediately' behavior, just the restart-persistence."""
    try:
        with open(_ENV_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []

    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith("TTS_ENGINE="):
            lines[i] = f"TTS_ENGINE={engine}\n"
            found = True
            break
    if not found:
        lines.append(f"TTS_ENGINE={engine}\n")

    try:
        with open(_ENV_PATH, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception:
        logger.warning("Failed to persist TTS_ENGINE=%s to .env", engine, exc_info=True)


def set_tts_engine(engine: str) -> str:
    """Switches the live TTS engine — 'kokoro', 'xtts', or 'say' (macOS's
    native `say` command, per-personality voice/rate — see
    core.commands._say_for). Raises ValueError for anything else (same
    contract as core.memory_flags.set_feature_flag for an unknown flag
    name). Returns the normalized engine string."""
    global _tts_engine
    engine = engine.strip().lower()
    if engine not in ("kokoro", "xtts", "say"):
        raise ValueError(f"unknown TTS engine: {engine}")
    with _tts_engine_lock:
        _tts_engine = engine
    _persist_tts_engine_to_env(engine)
    return engine


def _tts_worker() -> None:
    """Background worker that serialises all TTS jobs."""
    while True:
        job = _tts_queue.get()
        _tts_busy.set()
        try:
            func, args, kwargs = job
            func(*args, **kwargs)
        except Exception:
            logger.exception("TTS worker error")
        finally:
            _tts_busy.clear()
            _tts_queue.task_done()


threading.Thread(target=_tts_worker, daemon=True, name="tts-worker").start()


# ---------------------------------------------------------------------------
# Auto-unmute — threading.Timer based so the TTS worker never sleeps.
#
# Bug fix (Bug 6 — auto-unmute stacking):
# The old implementation called time.sleep(1.5) inside _post_speak, which runs
# in the TTS worker thread.  With N queued jobs the worker blocked for
# N × 1.5 s between jobs, making the mic stay muted far longer than intended.
#
# Fix: use threading.Timer.  Each job reschedules the timer (cancelling the
# previous one), so only the LAST job's 1.5 s delay fires.  The worker thread
# never sleeps between jobs.
# ---------------------------------------------------------------------------

_unmute_timer: threading.Timer | None = None
_unmute_lock  = threading.Lock()


def _schedule_auto_unmute(delay: float = 1.5) -> None:
    """Schedule mic unmute after *delay* seconds.

    Cancels any previously scheduled unmute so that back-to-back TTS jobs
    don't stack delays — only the final job's timer fires.
    """
    global _unmute_timer

    def _do_unmute():
        if not _tts_queue.empty() or _tts_busy.is_set():
            return
        try:
            import core.listener as _listener
            _listener.set_auto_muted(False)
        except Exception as e:
            logger.debug("Auto-unmute failed: %s", e)

    with _unmute_lock:
        if _unmute_timer is not None:
            _unmute_timer.cancel()
        t = threading.Timer(delay, _do_unmute)
        t.daemon = True
        t.start()
        _unmute_timer = t


# ---------------------------------------------------------------------------
# Kokoro pipeline — single lazy singleton. Uses lang_code='e' (Spanish);
# LIRA's voice style (ef_dora) is selected per-call via the `voice` parameter.
# ---------------------------------------------------------------------------

_kokoro_pipeline       = None
_kokoro_pipeline_lock  = threading.Lock()
_kokoro_available      = None   # None = untested, True/False after first attempt
# Set after LIRA's voice finishes pre-warming; _signal_ready() waits on this
# so jarvis_ready isn't emitted while the TTS pipeline is still cold.
kokoro_ready           = threading.Event()


def _get_kokoro():
    global _kokoro_pipeline, _kokoro_available
    with _kokoro_pipeline_lock:
        if _kokoro_available is False:
            return None
        if _kokoro_pipeline is None:
            try:
                from kokoro import KPipeline
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    # lang_code='e' = Spanish (espeak-ng 'es' phonemization).
                    # All three personality voices share this single pipeline.
                    _kokoro_pipeline = KPipeline(lang_code="e", repo_id="hexgrad/Kokoro-82M")
                _kokoro_available = True
                logger.info("Kokoro TTS pipeline loaded (Spanish, lang_code='e').")
            except Exception as e:
                _kokoro_available = False
                logger.warning("Kokoro unavailable: %s — will use macOS say as fallback.", e)
        return _kokoro_pipeline


def _prewarm_kokoro() -> None:
    """Generate a silent one-word chunk for each personality voice to warm the pipeline.

    Pre-warming all three voices forces Kokoro to load voice tensors for em_santa,
    ef_dora, and af_bella into memory so the first real utterance is fast.

    Only the actively-selected engine (get_tts_engine()) is kept permanently
    resident — mirrors the same policy applied to XTTS (see
    core.tts_xtts._XTTS_IS_SELECTED_ENGINE). When TTS_ENGINE=xtts, Kokoro
    stays fully lazy (it's only ever needed as _speak_kokoro_blocking's
    on-demand fallback — see core.commands._say_for) instead of eagerly
    loading a second full model at startup.
    """
    if get_tts_engine() != "kokoro":
        kokoro_ready.set()
        return

    pipeline = _get_kokoro()
    if pipeline is None:
        # Kokoro unavailable — signal immediately so _signal_ready() doesn't wait.
        kokoro_ready.set()
        return

    import warnings
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            # Near-silent phrase — just enough for PyTorch to run every
            # kernel it'll need at real-utterance time, without wasting
            # warmup time synthesizing a full word.
            for _ in pipeline("...", voice=KOKORO_VOICE_LIRA):
                break  # one chunk is enough to force tensor load
        logger.debug("Kokoro pre-warm complete: LIRA (%s).", KOKORO_VOICE_LIRA)
    except Exception as e:
        logger.debug("Kokoro pre-warm failed for LIRA (%s): %s", KOKORO_VOICE_LIRA, e)
    kokoro_ready.set()

    logger.info("Kokoro warm — listo")


# Pre-warm LIRA's voice in a background thread so startup doesn't block.
# (Skipped internally, see _prewarm_kokoro's docstring, when XTTS is the
# actively selected engine.)
threading.Thread(target=_prewarm_kokoro, daemon=True, name="kokoro-prewarm").start()

if get_tts_engine() == "xtts":
    # Eagerly import core.tts_xtts so its own module-level pre-warm thread
    # (core.tts_xtts._prewarm) starts right now instead of waiting for the
    # first real utterance — same "warm at startup" treatment Kokoro gets
    # above, just for whichever engine is actually configured.
    try:
        from core import tts_xtts as _tts_xtts_eager  # noqa: F401
    except Exception:
        logger.debug("Eager XTTS import at startup failed — will retry lazily on first speak.", exc_info=True)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def in_cooldown() -> bool:
    """Return True while TTS is queued/playing or within COOLDOWN_SECONDS of finishing."""
    if not _tts_queue.empty() or _tts_busy.is_set():
        return True
    with _lock:
        return time.monotonic() - _spoke_at < COOLDOWN_SECONDS


def _kill_active() -> None:
    """Kill any in-progress TTS output — the say/afplay subprocess (_tts_proc)
    or Kokoro's live streaming sounddevice.OutputStream (_tts_stream), the
    two active-playback mechanisms this module has, never both at once.
    Must be called with _lock held."""
    global _tts_proc, _tts_stream, _tts_stop_event
    if _tts_proc is not None and _tts_proc.poll() is None:
        _tts_proc.kill()
        try:
            _tts_proc.wait(timeout=5)  # process already sent SIGKILL; 5s is generous
        except subprocess.TimeoutExpired:
            logger.warning("[TTS] Killed TTS process did not reap within 5s")
    if _tts_stream is not None:
        if _tts_stop_event is not None:
            # Wakes Kokoro's producer/consumer threads (see _speak_kokoro_blocking)
            # so they unwind promptly instead of the producer blocking on a full
            # queue.put() or generating audio nobody will ever play.
            _tts_stop_event.set()
        try:
            _tts_stream.abort()   # drops any buffered-but-unplayed audio immediately, unlike stop()
            _tts_stream.close()
        except Exception:
            logger.debug("[TTS] Failed to abort/close active Kokoro stream", exc_info=True)
        _tts_stream = None
        _tts_stop_event = None


def register_active_stream(stream, stop_event: threading.Event) -> None:
    """Kills whatever was previously active (see _kill_active) and
    registers `stream`/`stop_event` as the new one — the one shared
    mechanism stop_speaking() (interrupt feature, step 3) needs to abort
    playback from OUTSIDE the engine that started it, regardless of which
    engine that is. Originally Kokoro-only inline code in
    _speak_kokoro_blocking; core.tts_xtts.speak_streaming now calls this
    too, so both engines' active streams are reachable the same way — a
    real gap before this: XTTS's own OutputStream was purely local to that
    function, invisible to _kill_active() entirely, meaning an XTTS
    utterance genuinely could NOT have been interrupted before."""
    global _tts_stream, _tts_stop_event
    with _lock:
        _kill_active()
        _tts_stream = stream
        _tts_stop_event = stop_event


def stop_speaking() -> None:
    """Interrupt feature, step 3 — the actual "cut her off" action. Kills
    whatever's currently playing (Kokoro or XTTS's registered stream, or
    the `say` subprocess — see _kill_active) AND drains any already-queued
    replies, so an accepted interruption doesn't just get silently
    overridden a moment later by the next queued line finishing this one
    off. Safe to call even when nothing is playing/queued — no-op. Never
    raises."""
    with _lock:
        _kill_active()
    while True:
        try:
            _tts_queue.get_nowait()
            _tts_queue.task_done()
        except queue.Empty:
            break


def _pre_speak(text: str, cmd_start: float | None = None) -> None:
    elapsed = f" t=+{time.monotonic() - cmd_start:.3f}s" if cmd_start else ""
    logger.info("[LATENCY] tts_start%s  text=%r", elapsed, text[:60])
    try:
        from core.server import emit_status
        emit_status("speaking")
    except Exception:
        pass


def _post_speak(cmd_start: float | None = None) -> None:
    global _spoke_at
    with _lock:
        _spoke_at = time.monotonic()
    elapsed = f" t=+{time.monotonic() - cmd_start:.3f}s" if cmd_start else ""
    logger.info("[LATENCY] tts_end%s", elapsed)
    try:
        from core.server import emit_status
        emit_status("listening")
    except Exception:
        pass
    # Schedule mic unmute 1.5 s after TTS ends (non-blocking timer — fixes Bug 6).
    _schedule_auto_unmute(1.5)
    # Reset ducking state — shared by all three engines via this one hook
    # (see set_self_output_rms/set_duck_gain's own comments, interrupt
    # feature step 1) so neither lingers stale into whatever's spoken next.
    set_self_output_rms(0.0)
    set_duck_gain(1.0)


def _emit_tts_first_audio(llm_done_mono: float | None) -> None:
    """Called exactly once per turn, at the moment audio genuinely starts
    playing — see the three call sites: _speak_kokoro_blocking()'s own
    streaming write() calls (Kokoro), _speak_say_blocking() (macOS say),
    and tts_xtts.speak_streaming()'s on_first_chunk callback (XTTS's first
    streamed chunk). Computes 'TTS latency' — time from the LLM reply
    being finalized to first audio output — for the chat's timing display
    (ui/js/chat-render.js). Silent no-op if llm_done_mono wasn't passed
    through (e.g. a caller that never threaded it) — this is a UI nicety,
    never allowed to affect playback."""
    if llm_done_mono is None:
        return
    tts_latency = time.monotonic() - llm_done_mono
    logger.info("[LATENCY] tts_first_audio  tts_latency=%.3fs", tts_latency)
    try:
        from core.server import emit_response_timing
        emit_response_timing({"tts_latency": tts_latency})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Kokoro TTS — shared implementation, voice selected per personality
# ---------------------------------------------------------------------------

def _make_silence_chunk(seconds: float = PAUSE_SILENCE_SECONDS, sample_rate: int = KOKORO_NATIVE_SAMPLERATE):
    """Return a numpy silence buffer to splice between Kokoro pause segments."""
    import numpy as np
    return np.zeros(int(sample_rate * seconds), dtype=np.float32)


def synthesize_pcm48(text: str, voice: str = KOKORO_VOICE_LIRA) -> bytes | None:
    """Synthesizes `text` with Kokoro straight to raw 48kHz/16-bit/stereo PCM
    bytes — the exact format discord.py's PCMAudio source expects. Unlike
    _speak_kokoro_blocking, this has no local playback side effect at all
    (no sounddevice.OutputStream, no TTS worker queue, no mute/interrupt
    plumbing) — it just returns bytes. Used by core/discord_voice.py so
    LIRA can answer inside a Discord voice channel without touching the
    mic/speaker pipeline at all. Blocking (runs Kokoro's generator to
    completion) — call via run_in_executor from async code. Returns None
    if Kokoro is unavailable or produced no audio."""
    import warnings
    import numpy as np
    import torch
    from scipy.signal import resample_poly

    pipeline = _get_kokoro()
    if pipeline is None:
        return None

    text = _naturalize_times(text)
    segments = [s.strip() for s in _PAUSE_RE.split(text) if s.strip()] or [text]

    chunks: list = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for segment in segments:
            for _, _, audio in pipeline(segment, voice=voice):
                if audio is None:
                    continue
                arr = audio.detach().cpu().numpy() if torch.is_tensor(audio) else np.asarray(audio)
                chunks.append(arr.astype(np.float32))
    if not chunks:
        return None

    mono_24k = np.concatenate(chunks)
    mono_48k = resample_poly(mono_24k, 2, 1).astype(np.float32)   # 24kHz -> 48kHz, exact 2x — Discord's fixed PCM rate
    mono_48k = np.clip(mono_48k, -1.0, 1.0)
    pcm16 = (mono_48k * 32767.0).astype(np.int16)
    stereo = np.repeat(pcm16.reshape(-1, 1), 2, axis=1)   # mono -> stereo, both channels identical
    return stereo.tobytes()


def synthesize_pcm48_say(text: str, voice: str | None = None, rate: int = 175) -> bytes | None:
    """Synthesizes `text` via macOS `say`, straight to raw 48kHz/16-bit/
    stereo PCM bytes — same output contract as synthesize_pcm48 (Kokoro),
    just a different engine. `voice=None` (the default) omits `say`'s -v
    flag entirely, so it uses whatever voice is set as this Mac's system
    default (System Settings -> Accessibility -> Spoken Content) — chosen
    deliberately over hardcoding a specific voice name (e.g. the existing
    KOKORO_FALLBACK_LIRA="Monica" used elsewhere), per Joan's own
    2026-08-10 test that the system default (a Siri voice) sounds better
    here than Mónica. No model to load (unlike Kokoro) — no warm-up cost
    at all, just `say`'s own subprocess startup latency. Used by
    core/discord_voice.py's speak() instead of Kokoro. Blocking (spawns
    `say`, waits for it to finish writing the file) — call via
    run_in_executor from async code. Returns None on any failure."""
    import numpy as np
    import soundfile as sf
    from fractions import Fraction
    from scipy.signal import resample_poly

    text = _naturalize_times(text)
    say_text = _PAUSE_RE.sub(f" [[slnc {PAUSE_SILENCE_MS}]] ", text)

    fd, path = tempfile.mkstemp(suffix=".aiff")
    os.close(fd)
    try:
        cmd = ["say", "-r", str(rate)]
        if voice:
            cmd += ["-v", voice]
        cmd += ["-o", path, say_text]
        proc = subprocess.run(cmd, timeout=30, capture_output=True)
        if proc.returncode != 0:
            logger.warning("[TTS] say -o failed (exit %s): %s", proc.returncode, proc.stderr.decode(errors="replace"))
            return None

        data, samplerate = sf.read(path, dtype="float32", always_2d=True)
        mono = data.mean(axis=1)   # collapse whatever channel count `say` wrote to mono
        if samplerate != 48000:
            ratio = Fraction(48000, samplerate).limit_denominator(1000)
            mono = resample_poly(mono, ratio.numerator, ratio.denominator).astype(np.float32)
        mono = np.clip(mono, -1.0, 1.0)
        pcm16 = (mono * 32767.0).astype(np.int16)
        stereo = np.repeat(pcm16.reshape(-1, 1), 2, axis=1)
        return stereo.tobytes()
    except Exception:
        logger.warning("[TTS] synthesize_pcm48_say failed", exc_info=True)
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _speak_kokoro_blocking(
    text: str,
    voice: str,
    fallback_voice: str | None = None,
    cmd_start: float | None = None,
    llm_done_mono: float | None = None,
) -> None:
    """Blocking Kokoro TTS — runs inside the TTS worker thread (this call
    itself blocks the worker until the whole utterance finishes, but
    internally it's a true producer/consumer pipeline: Kokoro's pipeline
    generator runs on its own thread and a live sounddevice.OutputStream is
    fed by a second thread, so generation and playback proceed concurrently
    rather than collecting the whole reply into a list before any of it
    plays.

    Producer/consumer buffering: a producer thread drives Kokoro's pipeline
    generator and puts each chunk on a bounded queue (maxsize=3); a
    consumer thread drains that queue and writes continuously to the
    sd.OutputStream. This decouples generation speed from playback — the
    consumer never waits on the next chunk to be generated, it blocks on
    queue.get() with a short timeout, which returns fast — so playback
    never runs dry just because Kokoro is momentarily slower than
    realtime. If the queue fills up (consumer slower than the producer,
    not the normal case but possible), the producer's queue.put() blocks —
    natural backpressure.

    Buffering: the very first chunk of the whole utterance is held back
    (not enqueued) until the second one arrives, then both are enqueued
    together as one item — a single lone chunk is more likely to
    click/pop at the join with whatever follows than two concatenated
    ones, and waiting one extra ~model-step is not perceptible. Every
    chunk from the third one onward is enqueued straight through. TTS
    latency (see _emit_tts_first_audio) is measured when the CONSUMER
    plays that first item, not when the producer generates it.

    KOKORO_MAX_GEN_SECS watchdog: checked by the producer on every chunk
    arrival (the only point it regains control from Kokoro's blocking
    generator) — once exceeded, generation is cancelled (no more chunks
    pulled, no more segments started); whatever's already queued/playing
    keeps playing, the reply just ends early. Falls back to macOS `say`
    (fallback_voice) ONLY if NOTHING was ever actually played — once real
    audio has started, this never restarts the reply through a different
    voice/engine, which would be a jarring double-speak rather than a
    clean recovery.
    """
    if is_tts_muted():
        logger.debug("[TTS] muted — skipping playback (voice=%s)", voice)
        return

    text = _naturalize_times(text)
    _pre_speak(text, cmd_start)

    import fractions
    import numpy as np
    import sounddevice as sd
    import torch
    from scipy.signal import resample_poly
    from core.vad import compute_rms_float

    # Bug fix: register_active_stream() below now owns the actual
    # assignment (it has its own `global` for these), but the fallback-to-
    # `say` cleanup further down in this function still reads/clears
    # _tts_stream/_tts_stop_event directly — without this declaration,
    # that later assignment made Python treat both names as LOCAL to this
    # whole function (Python's normal scoping rule: any assignment
    # anywhere in a function makes a name local throughout it), so the
    # earlier read raised UnboundLocalError the very first time this path
    # was ever exercised for real (caught by a live stop_speaking() test).
    global _tts_stream, _tts_stop_event

    stream = None   # constructed INSIDE the try block below (see its own comment) — None here
                     # just lets the finally block check safely if construction itself failed.
    debug_chunks: list = []   # only ever populated if _KOKORO_DEBUG_WAV is True

    got_audio       = False   # True the moment the producer yields ANY non-None audio
    any_played      = False   # True once at least one item has actually reached the stream
    producer_error: Exception | None = None
    consumer_error: Exception | None = None

    # Resample every chunk from Kokoro's fixed 24000Hz to the output device's
    # own native rate BEFORE writing it, and open the stream at that native
    # rate — see KOKORO_NATIVE_SAMPLERATE's own comment for why: writing raw
    # 24000Hz straight to a 48000Hz-native device forces CoreAudio to
    # resample it live, which is what was actually causing the distortion.
    # Falls back to KOKORO_NATIVE_SAMPLERATE (no resampling) if the device
    # query fails for any reason — never lets a query hiccup block playback.
    try:
        out_samplerate = int(round(sd.query_devices(kind="output")["default_samplerate"]))
        if out_samplerate <= 0:
            raise ValueError(f"non-positive default_samplerate: {out_samplerate}")
    except Exception as e:
        logger.debug("[TTS] Could not resolve output device native samplerate (%s) — using %dHz unresampled.",
                      e, KOKORO_NATIVE_SAMPLERATE)
        out_samplerate = KOKORO_NATIVE_SAMPLERATE

    # Exact small-integer ratio (e.g. 48000/24000 -> 2/1, 44100/24000 -> 147/80)
    # for scipy.signal.resample_poly, which resamples by that exact rational
    # factor rather than an approximate/interpolated one.
    _resample_ratio = fractions.Fraction(out_samplerate, KOKORO_NATIVE_SAMPLERATE).limit_denominator(1000)
    _resample_up, _resample_down = _resample_ratio.numerator, _resample_ratio.denominator

    def _to_numpy(chunk):
        # Kokoro's pipeline yields torch tensors, not numpy arrays — the old
        # code never noticed because np.concatenate() converts tensors
        # implicitly via NumPy's array protocol, but explicit calls like
        # .astype() don't exist on a torch.Tensor at all (raises
        # AttributeError). Converts explicitly here instead of relying on
        # that implicit path, same as core.tts_xtts.speak_streaming() does.
        if torch.is_tensor(chunk):
            return chunk.detach().cpu().numpy()
        return np.asarray(chunk)

    def _frame(chunk):
        arr = _to_numpy(chunk).astype(np.float32)
        if _resample_up != _resample_down:
            arr = resample_poly(arr, _resample_up, _resample_down).astype(np.float32)
        return arr.reshape(-1, 1)

    try:
        pipeline = _get_kokoro()
        if pipeline is None:
            raise RuntimeError("Kokoro pipeline not available")

        import warnings

        # Natural speech pauses: split on '…' / '—' markers (see core/commands.py
        # personality prompts) and generate each segment separately, splicing a
        # brief silence buffer between them instead of speaking straight through.
        segments = [s.strip() for s in _PAUSE_RE.split(text) if s.strip()]
        if not segments:
            segments = [text]

        t_gen_start = time.monotonic()
        logger.info("[LATENCY] kokoro_gen_start  voice=%s  segments=%d", voice, len(segments))

        # Constructed and opened INSIDE the try block, deliberately — a real,
        # observed failure mode on this machine is the OutputStream itself
        # failing to open (PortAudio device contention with the always-on
        # mic input stream, e.g. PaErrorCode -9986). If that happened
        # outside this try/except, there'd be no fallback to `say` at all.
        stream = sd.OutputStream(samplerate=out_samplerate, channels=1, dtype="float32")
        stream.start()
        stop_event = threading.Event()
        register_active_stream(stream, stop_event)

        # Bounded to 3 chunks ahead — enough to absorb generation jitter
        # without buffering so much audio that a mid-stream cancel/kill
        # takes noticeably long to actually go silent.
        chunk_queue: queue.Queue = queue.Queue(maxsize=3)

        def _producer() -> None:
            nonlocal got_audio, producer_error
            pending_first = None   # holds chunk #1 of the whole utterance until chunk #2 arrives
            started = False        # True once anything has been enqueued as real audio
            cancelled = False
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    for i, segment in enumerate(segments):
                        if cancelled:
                            break
                        for _, _, audio in pipeline(segment, voice=voice):
                            if stop_event.is_set():
                                cancelled = True
                                break
                            if audio is None:
                                continue
                            got_audio = True

                            # Handle the chunk FIRST (buffer or enqueue it), THEN
                            # check the watchdog — checking before handling would
                            # silently drop this exact chunk's audio if it's the
                            # one that trips the timeout, since it would never get
                            # buffered or enqueued anywhere at all.
                            if pending_first is None and not started:
                                pending_first = audio   # chunk #1 — buffer, don't enqueue yet
                            elif pending_first is not None:
                                # Explicit numpy conversion before concatenating —
                                # don't rely on np.concatenate's implicit tensor
                                # handling (see _to_numpy's own comment on why
                                # that's exactly the assumption that broke
                                # .astype() above).
                                combined = np.concatenate([_to_numpy(pending_first), _to_numpy(audio)])
                                chunk_queue.put(_frame(combined))   # chunks #1+#2 together
                                pending_first = None
                                started = True
                            else:
                                chunk_queue.put(_frame(audio))   # chunk #3 onward — straight through

                            if time.monotonic() - t_gen_start > KOKORO_MAX_GEN_SECS:
                                logger.warning(
                                    "[LATENCY] Kokoro exceeded %.1fs — cancelling further generation (voice=%s)",
                                    KOKORO_MAX_GEN_SECS, voice,
                                )
                                cancelled = True
                                break

                        if cancelled:
                            break
                        if i < len(segments) - 1:
                            if pending_first is not None:
                                # Only one chunk arrived before this segment boundary —
                                # flush it now rather than holding it across a silence gap.
                                chunk_queue.put(_frame(pending_first))
                                pending_first = None
                                started = True
                            chunk_queue.put(_make_silence_chunk(sample_rate=out_samplerate).reshape(-1, 1))

                    # Utterance ended (naturally or via the watchdog) with a single
                    # chunk still held back, waiting for a second that never came —
                    # flush it now instead of silently dropping real audio.
                    if pending_first is not None:
                        chunk_queue.put(_frame(pending_first))
            except Exception as e:
                producer_error = e
            finally:
                chunk_queue.put(None)   # sentinel — always signals the consumer to stop

        def _consumer() -> None:
            nonlocal any_played, consumer_error
            first_played = False
            draining = False   # True once a stream write has failed — keep pulling
                                # items off the queue (discarding them) so the
                                # producer's queue.put() calls, including its final
                                # sentinel, never block forever on a consumer that
                                # stopped reading.
            while True:
                try:
                    item = chunk_queue.get(timeout=0.2)
                except queue.Empty:
                    continue   # producer is just slow — no underrun, the stream stays open
                if item is None:
                    break
                if draining:
                    continue
                try:
                    set_self_output_rms(compute_rms_float(item))
                    gain = _duck_gain
                    stream.write(item * gain if gain != 1.0 else item)
                except Exception as e:
                    logger.warning("[TTS] Kokoro consumer stream write failed: %s", e)
                    consumer_error = e
                    stop_event.set()   # also tells the producer to stop generating
                    draining = True
                    continue
                any_played = True
                if _KOKORO_DEBUG_WAV:
                    debug_chunks.append(item)
                if not first_played:
                    # Fires when the CONSUMER actually plays the first item, not
                    # when the producer generated it — that's the real "audio is
                    # now genuinely reaching the speaker" moment.
                    first_played = True
                    _emit_tts_first_audio(llm_done_mono)

        producer_thread = threading.Thread(target=_producer, name="kokoro-producer", daemon=True)
        consumer_thread = threading.Thread(target=_consumer, name="kokoro-consumer", daemon=True)
        producer_thread.start()
        consumer_thread.start()
        producer_thread.join()
        consumer_thread.join()

        err = producer_error or consumer_error
        if err is not None:
            raise err
        # An interrupt (core.voice.stop_speaking, step 3 of the interrupt
        # feature) that lands before any audio was generated is a clean
        # stop, not a failure — must NOT raise here, since the except-block
        # below falls back to `say`, which would effectively un-interrupt
        # her right back into speaking the same line through a different
        # voice the instant she was stopped.
        if not got_audio and not stop_event.is_set():
            raise RuntimeError("Kokoro generated no audio")

        gen_secs = time.monotonic() - t_gen_start
        logger.info("[LATENCY] kokoro_gen_end  duration=%.3fs  voice=%s", gen_secs, voice)

    except Exception as e:
        if any_played:
            # Real Kokoro audio already reached the speaker — ending here
            # (reply cut short) beats restarting the whole line through a
            # different voice on top of what was just heard. Stream
            # cleanup happens uniformly in `finally` below either way.
            logger.warning("Kokoro exception after audio had already started (voice=%s): %s", voice, e)
        else:
            logger.warning("Kokoro TTS failed (voice=%s, %s) — falling back to say.", voice, e)
            # Release the audio device BEFORE handing off to `say` — `finally`
            # below still runs after this (it's idempotent: stopping/closing
            # an already-closed stream just raises into its own swallowed
            # try/except), but say's own subprocess shouldn't start against
            # an still-open (if unused) Kokoro stream.
            if stream is not None:
                try:
                    stream.abort()
                    stream.close()
                except Exception:
                    pass
                with _lock:
                    if _tts_stream is stream:
                        _tts_stream = None
                        _tts_stop_event = None
            _speak_say_blocking(text, voice=fallback_voice, llm_done_mono=llm_done_mono)
            return

    finally:
        # Single, uniform cleanup point for every exit path above (normal
        # completion, cancelled-but-something-played, or the fallback-to-say
        # return) — stream may be None if sd.OutputStream() itself failed
        # to construct/open before ever being assigned.
        if stream is not None:
            try:
                stream.stop()   # waits for whatever's still buffered to finish playing
                stream.close()
            except Exception:
                pass
            with _lock:
                if _tts_stream is stream:
                    _tts_stream = None
                    _tts_stop_event = None
        if _KOKORO_DEBUG_WAV and debug_chunks:
            try:
                import soundfile as sf
                fd, path = tempfile.mkstemp(suffix=".wav")
                os.close(fd)
                sf.write(path, np.concatenate(debug_chunks), out_samplerate)
                logger.debug("[TTS] Kokoro debug WAV written to %s", path)
            except Exception:
                logger.debug("[TTS] Failed to write Kokoro debug WAV", exc_info=True)

    _post_speak(cmd_start)


# ---------------------------------------------------------------------------
# Public Kokoro TTS — one function per personality
# ---------------------------------------------------------------------------

def speak_kokoro_lira(text: str, cmd_start: float | None = None, llm_done_mono: float | None = None) -> None:
    """Enqueue Kokoro TTS for LIRA (ef_dora — native Spanish female, warm and natural)."""
    _tts_queue.put((
        _speak_kokoro_blocking,
        (text,),
        {
            "voice": KOKORO_VOICE_LIRA, "fallback_voice": KOKORO_FALLBACK_LIRA,
            "cmd_start": cmd_start, "llm_done_mono": llm_done_mono,
        },
    ))


# ---------------------------------------------------------------------------
# System TTS via macOS `say` — last-resort fallback only.
# Not used for any personality's primary path; only reached when Kokoro fails.
# ---------------------------------------------------------------------------

def _speak_say_blocking(text: str, voice: str | None = None,
                         rate: int = 175,
                         cmd_start: float | None = None,
                         llm_done_mono: float | None = None) -> None:
    """Blocking say TTS — runs inside the TTS worker thread."""
    global _tts_proc
    text = _naturalize_times(text)
    try:
        logger.info("[LATENCY] say_start  voice=%s", voice)
        # Translate '…' / '—' pause markers into `say`'s embedded silence command
        # so longer fallback utterances still get a natural breath, not a runon.
        say_text = _PAUSE_RE.sub(f" [[slnc {PAUSE_SILENCE_MS}]] ", text)
        cmd = ["say", "-r", str(rate)]
        if voice:
            cmd += ["-v", voice]
        cmd.append(say_text)
        with _lock:
            _kill_active()
            _tts_proc = subprocess.Popen(cmd, start_new_session=True)
        # No _emit_tts_first_audio() call here, unlike Kokoro/XTTS — Popen()
        # returning only means the `say` binary was launched, not that audio
        # is actually reaching the speaker yet. Unlike Kokoro's in-process
        # streaming writes or XTTS's on_first_chunk callback (both genuine
        # "sound is playing now" signals), `say` is a black-box subprocess
        # with real, voice-dependent synthesis startup lag before its first
        # sample — worse for higher-quality System Voices (Enhanced/Premium/
        # Siri) than the old default Mónica/Eddy. Reporting Popen()-launch
        # time as "first audio" understated that lag entirely (bug: the
        # chat's latency display showed a fast number while the actual
        # speech visibly started later). Better to show nothing for this
        # engine than a number that's confidently wrong.
        try:
            _tts_proc.wait(timeout=30)  # timeout prevents TTS queue lockup if say hangs
        except subprocess.TimeoutExpired:
            logger.warning("[TTS] say timed out after 30s — killing to unblock queue")
            _tts_proc.kill()
            _tts_proc.wait()
        logger.info("[LATENCY] say_end")
    except FileNotFoundError:
        logger.error("`say` command not found — are you on macOS?")
    except Exception:
        logger.exception("say TTS failed")
    _post_speak(cmd_start)


def speak(text: str, voice: str | None = None, rate: int = 175,
          cmd_start: float | None = None, llm_done_mono: float | None = None) -> None:
    """Enqueue say TTS — last-resort fallback. Returns immediately."""
    if is_tts_muted():
        logger.debug("[TTS] muted — skipping playback (say fallback)")
        return
    _pre_speak(text, cmd_start)
    _tts_queue.put((
        _speak_say_blocking, (text,),
        {"voice": voice, "rate": rate, "cmd_start": cmd_start, "llm_done_mono": llm_done_mono},
    ))


# ---------------------------------------------------------------------------
# XTTS v2 — optional voice-cloning engine, selected via TTS_ENGINE=xtts in
# .env (see core/commands.py's _say_for()). Falls back to this
# personality's own Kokoro voice on any failure (model/reference
# unavailable, generation exception) — same "never break TTS entirely"
# contract Kokoro itself has with macOS `say`. The actual model loading /
# streaming-synthesis implementation lives in core/tts_xtts.py, imported
# lazily below so the (heavy, TTS-package-specific) XTTS machinery is never
# even touched on the default Kokoro path.
# ---------------------------------------------------------------------------

# Kokoro voice/fallback to use if XTTS fails — LIRA is the only personality
# now (JARVIS/FRIDAY removed 2026-08-10), so no per-personality lookup is
# needed anymore.
_XTTS_KOKORO_FALLBACK = (KOKORO_VOICE_LIRA, KOKORO_FALLBACK_LIRA)


def _speak_xtts_blocking(
    text: str, personality: str,
    cmd_start: float | None = None, llm_done_mono: float | None = None,
) -> None:
    """Blocking XTTS v2 TTS — runs inside the TTS worker thread. Clones the
    voice in data/voice_reference.wav (see core.tts_xtts) and streams audio
    as it's generated, so playback starts well before the full utterance
    finishes synthesizing — see core.tts_xtts.speak_streaming's own
    docstring for why this is still much slower overall than Kokoro.

    On any failure, falls straight into _speak_kokoro_blocking (not the
    speak_kokoro*() enqueue wrappers — we're already inside the worker
    thread, see _tts_worker) for this personality's normal Kokoro voice,
    same shape as _speak_kokoro_blocking's own fallback into
    _speak_say_blocking. That call re-runs _pre_speak() a second time
    (a harmless duplicate "speaking" status emit/log line, not a real
    double-speak) since _speak_kokoro_blocking always calls it
    unconditionally at its own top."""
    if is_tts_muted():
        logger.debug("[TTS] muted — skipping playback (xtts, personality=%s)", personality)
        return

    kokoro_voice, kokoro_fallback = _XTTS_KOKORO_FALLBACK
    naturalized = _naturalize_times(text)
    _pre_speak(naturalized, cmd_start)

    try:
        from core import tts_xtts

        segments = [s.strip() for s in _PAUSE_RE.split(naturalized) if s.strip()] or [naturalized]
        language = tts_xtts.detect_language(naturalized)

        t_gen_start = time.monotonic()
        logger.info(
            "[LATENCY] xtts_gen_start  personality=%s  segments=%d  language=%s",
            personality, len(segments), language,
        )
        tts_xtts.speak_streaming(
            segments, language, silence_seconds=PAUSE_SILENCE_SECONDS,
            on_first_chunk=lambda: _emit_tts_first_audio(llm_done_mono),
        )
        logger.info(
            "[LATENCY] xtts_gen_end  duration=%.3fs  personality=%s",
            time.monotonic() - t_gen_start, personality,
        )
    except Exception as e:
        logger.warning("XTTS TTS failed (personality=%s, %s) — falling back to Kokoro.", personality, e)
        _speak_kokoro_blocking(
            text, voice=kokoro_voice, fallback_voice=kokoro_fallback,
            cmd_start=cmd_start, llm_done_mono=llm_done_mono,
        )
        return

    _post_speak(cmd_start)


def speak_xtts(
    text: str, personality: str,
    cmd_start: float | None = None, llm_done_mono: float | None = None,
) -> None:
    """Enqueue XTTS v2 TTS for *personality* — see _speak_xtts_blocking."""
    _tts_queue.put((
        _speak_xtts_blocking,
        (text,),
        {"personality": personality, "cmd_start": cmd_start, "llm_done_mono": llm_done_mono},
    ))
