"""Text-to-speech: mute state, the serialized TTS worker queue, macOS `say`
(the only TTS engine — Kokoro and XTTS were removed), and mic auto-unmute
after speaking. Natural time-of-day phrasing lives in core.voice_time."""
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
# responses. `say` gets the markers translated into an SSML-style [[slnc]] tag.
_PAUSE_RE = re.compile(r"\s*[…—]\s*")

# Silence passed to macOS `say` via [[slnc <ms>]] — within the 200-300ms
# "natural breath" range.
PAUSE_SILENCE_SECONDS = 0.25
PAUSE_SILENCE_MS      = int(PAUSE_SILENCE_SECONDS * 1000)

# ---------------------------------------------------------------------------
# TTS queue and worker
# ---------------------------------------------------------------------------

_lock     = threading.Lock()
_spoke_at: float = 0.0
_tts_proc: subprocess.Popen | None = None   # tracks the active say/afplay subprocess

_tts_queue: queue.Queue = queue.Queue()
_tts_busy  = threading.Event()   # set while a TTS job is actively playing

# ---------------------------------------------------------------------------
# TTS mute — voice-output-only mute, independent of listener.py's mic mute.
# When muted, HUGO still listens, still processes, still replies in chat —
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
# Currently dormant: `say` (the only TTS engine left — Kokoro/XTTS were
# both streaming engines that read this per-chunk, and both were removed)
# runs as a black-box subprocess with no per-sample hook, so it can't be
# smoothly ducked with this mechanism. Left in place since core/listener.py
# still calls set_duck_gain()/get_self_output_rms() unconditionally as part
# of the interrupt-ducking loop — harmless no-op writes/reads until some
# future engine streams sample-by-sample again.
_duck_gain = 1.0


def set_duck_gain(gain: float) -> None:
    global _duck_gain
    _duck_gain = max(0.0, min(1.0, gain))


def get_duck_gain() -> float:
    return _duck_gain


# Live self-output RMS — the actual current TTS chunk's own loudness,
# updated by a streaming engine's playback consumer on every chunk write
# (currently nothing does — see _duck_gain's comment above), read by
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
# Internal helpers
# ---------------------------------------------------------------------------

def in_cooldown() -> bool:
    """Return True while TTS is queued/playing or within COOLDOWN_SECONDS of finishing."""
    if not _tts_queue.empty() or _tts_busy.is_set():
        return True
    with _lock:
        return time.monotonic() - _spoke_at < COOLDOWN_SECONDS


def _kill_active() -> None:
    """Kill any in-progress TTS output — the say/afplay subprocess
    (_tts_proc), the only active-playback mechanism this module has now
    (Kokoro/XTTS's streaming sounddevice.OutputStream path was removed
    along with those engines). Must be called with _lock held."""
    global _tts_proc
    if _tts_proc is not None and _tts_proc.poll() is None:
        _tts_proc.kill()
        try:
            _tts_proc.wait(timeout=5)  # process already sent SIGKILL; 5s is generous
        except subprocess.TimeoutExpired:
            logger.warning("[TTS] Killed TTS process did not reap within 5s")


def stop_speaking() -> None:
    """Interrupt feature, step 3 — the actual "cut her off" action. Kills
    whatever's currently playing (the `say` subprocess — see _kill_active)
    AND drains any already-queued replies, so an accepted interruption
    doesn't just get silently overridden a moment later by the next queued
    line finishing this one off. Safe to call even when nothing is
    playing/queued — no-op. Never raises."""
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


# ---------------------------------------------------------------------------
# macOS `say` — PCM synthesis (no local playback side effect, just bytes)
# ---------------------------------------------------------------------------

def synthesize_pcm48_say(text: str, voice: str | None = None, rate: int = 175) -> bytes | None:
    """Synthesizes `text` via macOS `say`, straight to raw 48kHz/16-bit/
    stereo PCM bytes. `voice=None` (the default) omits `say`'s -v flag
    entirely, so it uses whatever voice is set as this Mac's system default
    (System Settings -> Accessibility -> Spoken Content) — chosen
    deliberately over hardcoding a specific voice name, per Joan's own
    2026-08-10 test that the system default (a Siri voice) sounded better
    than the old Kokoro fallback voice. No model to load — no warm-up cost
    at all, just `say`'s own subprocess startup latency. Used by
    core/discord_voice.py's speak(). Blocking (spawns
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


# ---------------------------------------------------------------------------
# System TTS via macOS `say` — the only TTS engine (Kokoro/XTTS removed).
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
    """Enqueue say TTS. Returns immediately."""
    if is_tts_muted():
        logger.debug("[TTS] muted — skipping playback")
        return
    _pre_speak(text, cmd_start)
    _tts_queue.put((
        _speak_say_blocking, (text,),
        {"voice": voice, "rate": rate, "cmd_start": cmd_start, "llm_done_mono": llm_done_mono},
    ))
