# ═══════════════════════════════════════════════════════════════════════════
# LISTENER — main audio stream loop only. Wake-word variant lists/fuzzy
# matching now live in core/wake_word.py; VAD/silence-detection/RMS-gate
# helpers now live in core/vad.py (pure refactor, no behavior change).
# ═══════════════════════════════════════════════════════════════════════════
import collections
import json
import math
import os
import queue
import tempfile
import threading
import time
import logging
from concurrent.futures import ThreadPoolExecutor

import sounddevice as sd
import numpy as np
import soundfile as sf
import vosk

from core.wake_word import (
    _EN_CONF_THRESHOLD,
    _TRANSCRIPT_CONF_THRESHOLD,
    _EN_WAKE_GRAMMAR,
    _scan_result,
    _overall_confidence,
    normalize_wake_word_text,
)
from core.vad import (
    _RMS_WINDOW_SIZE,
    _MIN_SEGMENT_SECS,
    _COMMAND_WINDOW_SECS,
    _CONV_BUFFER_SECS,
    _CONV_MAX_WINDOW_SECS,
    _DUCK_TRIGGER_WINDOW,
    _DUCK_RELEASE_WINDOW,
    _DUCK_GAIN,
    _SILERO_SPEECH_THRESHOLD,
    compute_rms,
    update_rms_gate,
    silence_tracker_update,
    duck_gate_update,
    SileroSpeechBuffer,
    silero_silence_tracker_update,
)

logger = logging.getLogger(__name__)

VOSK_MODEL_ES_PATH = "data/modelos/vosk-model-es-0.42"
VOSK_MODEL_EN_PATH = "data/modelos/vosk-model-small-en-us-0.15"
SAMPLERATE         = 16000

# Mandatory cooldown between any two wake word triggers (seconds)
_WAKE_COOLDOWN_SECS = 3.0

# Interrupt feature, step 2 — how much ducked audio to accumulate before
# running the identity check (core.speaker.check_interrupt_speaker).
# speaker.MIN_DURATION (1.5s) is the floor ECAPA needs for a trustworthy
# embedding at all; this adds real margin on top rather than running the
# check at the bare minimum.
_INTERRUPT_CHECK_DURATION_SECS = 2.0


# ---------------------------------------------------------------------------
# Conversational intelligence — Phase 1: post-response context window.
#
# When Hugo actually speaks (core.commands._say_for calls note_response()
# on every reply), the next _CONTEXT_WINDOW_SECS seconds of speech are
# eligible to be treated as a continuation of the exchange without the wake
# word being said again (e.g. "Ahora mismo no puedo" right after a reply).
# core.social_reasoning.should_continue() still gates each one — this window
# only controls whether the wake word is REQUIRED, not whether Hugo responds.
#
# Every dispatch this module triggers — wake word or context-window
# continuation alike — flows into core.commands.dispatch_command(), which
# runs it through TWO social reasoning gates in sequence before generating a
# reply: Phase 1 (is_addressed/should_continue, above) filters "was Hugo's
# name actually meant for her", then Phase 2
# (core.social_reasoning.should_intervene) makes the broader "does it
# actually make sense to speak right now" call — same INTERVENIR/SILENCIO
# reasoning core.background_loops uses for its periodic proactive tick, so a
# spoken reply and an unprompted comment are held to the same bar. Nothing
# in this file needs to know about Phase 2 directly; it's entirely inside
# core/commands.py's _dispatch_command_impl.
# ---------------------------------------------------------------------------

_CONTEXT_WINDOW_SECS = 60.0

_last_response_mono: float = 0.0
_last_response_personality: str | None = None
_response_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Conversational intelligence — Phase 3: proactive contextual intervention
# WITHOUT a wake word at all — Hugo commenting unprompted on what she just
# overheard, like a person present in the room.
#
# Every finalized Vosk segment, in either listen mode and regardless of
# whether a wake word fired, is appended below to a rolling
# _PASSIVE_BUFFER_SECS-second buffer of plain text — never sent to any LLM,
# just held locally (this is the "cheap: local Vosk only" half of the spec).
# Every _PASSIVE_CHECK_INTERVAL_SECS, if that buffer picked up any speech,
# its full text is handed off (on a background thread, so the audio loop
# never blocks on it) to core.commands.maybe_ambient_intervention(), which
# runs the Phase 2 social reasoning gate (core.social_reasoning.
# should_intervene — same INTERVENIR/SILENCIO judgment used for wake-word
# replies and core.background_loops' periodic proactive tick) against the
# overheard text and, only on INTERVENIR, a single local Ollama call to
# produce (or skip) one brief in-character observation. All rate limiting
# (max one per 10 minutes), the TTS-cooldown/test-mode guards, and the
# actual Ollama call live in that function — this file only owns the buffer
# and the 30-second tick.
# ---------------------------------------------------------------------------

_PASSIVE_BUFFER_SECS         = 60.0   # how much overheard speech stays in context
_PASSIVE_CHECK_INTERVAL_SECS = 30.0   # how often the buffer is handed off for a verdict


def note_response(personality: str) -> None:
    """Called by core.commands._say_for every time Hugo actually speaks."""
    global _last_response_mono, _last_response_personality
    with _response_lock:
        _last_response_mono = time.monotonic()
        _last_response_personality = personality


def _context_window_active() -> tuple[bool, str | None]:
    """Returns (active, personality) — personality is None unless active."""
    with _response_lock:
        if _last_response_personality is None:
            return False, None
        active = (time.monotonic() - _last_response_mono) <= _CONTEXT_WINDOW_SECS
        return active, (_last_response_personality if active else None)


# ---------------------------------------------------------------------------
# Voice enrollment — Phase 4. Cross-thread trigger only: core/commands.py's
# dispatch thread calls request_voice_enrollment() when Joan asks to (re-)
# enroll her voice (see _VOICE_ENROLL_RE in that module); listen()'s own
# audio thread picks up the request on its next loop iteration and runs the
# actual multi-sample recording state machine (see the `_enroll_active`
# branch inside listen() — it needs to be that thread since it's the only
# one reading raw mic audio). ENROLL_TARGET_SAMPLES samples are recorded (3-5
# per spec; using the max so the resulting fingerprint has as much material
# as practical), each ended by the same VAD silence-cutoff wake-word command
# collection already uses, then handed to core.commands.finish_voice_
# enrollment() to actually build and save the fingerprint.
# ---------------------------------------------------------------------------

ENROLL_TARGET_SAMPLES  = 5      # spec: 3-5 sentences
_ENROLL_SAMPLE_MAX_SECS = 8.0   # hard cap per sample, in case VAD never sees silence

_enroll_state_lock  = threading.Lock()
_enroll_requested   = False   # set by request_voice_enrollment(), cleared once listen() picks it up
_enroll_in_progress = False   # True for the full duration of the recording state machine


def request_voice_enrollment() -> bool:
    """Called from core/commands.py's dispatch thread. Returns True if a new
    enrollment was queued, False if one is already requested/running (the
    caller should tell Joan to wait for the current one to finish)."""
    global _enroll_requested
    with _enroll_state_lock:
        if _enroll_requested or _enroll_in_progress:
            return False
        _enroll_requested = True
    return True


def is_enrollment_active() -> bool:
    with _enroll_state_lock:
        return _enroll_requested or _enroll_in_progress


# ---------------------------------------------------------------------------
# Mute state
#
# _muted (UI-controlled mute button) is persisted to data/mic_mute.json —
# same pattern as _listen_mode/_MODE_CONFIG_PATH just below — so toggling
# the mic off survives a jarvis.py restart instead of silently coming back
# online every relaunch (bug: Joan mutes, restarts the app, mic is back on
# with no indication anything changed). _auto_muted (TTS-echo prevention)
# is deliberately NOT persisted — it's a transient per-turn state that
# should always start False on a fresh process.
# ---------------------------------------------------------------------------

_MUTE_CONFIG_PATH = "data/mic_mute.json"


def _load_muted() -> bool:
    try:
        with open(_MUTE_CONFIG_PATH, "r", encoding="utf-8") as f:
            return bool(json.load(f).get("muted", False))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False


def _save_muted(muted: bool) -> None:
    os.makedirs(os.path.dirname(_MUTE_CONFIG_PATH) or ".", exist_ok=True)
    try:
        with open(_MUTE_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"muted": muted}, f)
    except OSError as e:
        logger.warning("Could not persist mic mute state: %s", e)


_muted      = _load_muted()   # UI-controlled (mute button) — restored from disk
_auto_muted = False           # Auto-set during TTS playback to prevent echo


def set_muted(muted: bool) -> None:
    global _muted
    _muted = muted
    _save_muted(muted)
    # User-initiated mute now stops the physical PortAudio stream (not just a
    # flag-gated skip) so macOS drops the orange mic indicator entirely.
    if muted:
        _stop_mic_stream()
    else:
        _start_mic_stream()


def set_auto_muted(muted: bool) -> None:
    global _auto_muted
    _auto_muted = muted


def is_muted() -> bool:
    return _muted


def is_auto_muted() -> bool:
    return _auto_muted


# ---------------------------------------------------------------------------
# Mic stream control — stop/start the PortAudio stream independently of the
# listen loop so the macOS orange mic indicator dot can be removed on demand.
#
# _stop_mic_stream() clears _mic_streaming; the listen loop detects this on
# each iteration, calls stream.stop() (CoreAudio input unit stops → orange
# dot disappears), and blocks until _start_mic_stream() sets the event again
# (at which point the loop also resets the Vosk recognizers so no stale
# partial state survives the pause).
#
# Used by set_muted() (wired to the /api/mute and /api/unmute endpoints and
# their 'mute_state' socket event) and by mic_stop()/mic_start() (wired to
# the /api/mic_stop and /api/mic_start endpoints used by the Electron Tray).
# ---------------------------------------------------------------------------

_mic_streaming     = threading.Event()
if not _muted:   # honour a persisted mute (see _load_muted above) from the very first stream open
    _mic_streaming.set()
_reset_recognizers = threading.Event()   # set on restart: Reset() Vosk state


def _stop_mic_stream() -> None:
    """Stop the sounddevice input stream entirely — orange mic dot disappears."""
    _mic_streaming.clear()


def _start_mic_stream() -> None:
    """Restart the sounddevice input stream, re-initializing Vosk recognizers."""
    _reset_recognizers.set()
    _mic_streaming.set()


def mic_stop() -> None:
    """Pause the PortAudio capture stream — orange mic dot disappears."""
    _stop_mic_stream()


def mic_start() -> None:
    """Resume the PortAudio capture stream after a mic_stop() call."""
    _start_mic_stream()


# ---------------------------------------------------------------------------
# Listen mode — 'wake_word' (default) or 'conversation'
#
# Part 2: persisted to data/mode_config.json so the chosen mode survives
# jarvis.py restarts.  set_listen_mode() is called by both the voice-command
# handler in commands.py and the /api/mode HTTP endpoint in server.py.
# When the mode changes a 'mode_change' SocketIO event is emitted so the
# HUD button updates in real time.
# ---------------------------------------------------------------------------

_MODE_CONFIG_PATH = "data/mode_config.json"
_listen_mode: str = "wake_word"   # 'wake_word' | 'conversation'
_mode_lock    = threading.Lock()


def _load_listen_mode() -> str:
    try:
        with open(_MODE_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("mode", "wake_word")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return "wake_word"


def _save_listen_mode(mode: str) -> None:
    os.makedirs(os.path.dirname(_MODE_CONFIG_PATH) or ".", exist_ok=True)
    try:
        with open(_MODE_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"mode": mode}, f)
    except OSError as e:
        logger.warning("Could not persist mode config: %s", e)


def get_listen_mode() -> str:
    """Thread-safe read of the current listen mode."""
    with _mode_lock:
        return _listen_mode


def set_listen_mode(mode: str) -> None:
    """Switch listen mode ('wake_word' | 'conversation').

    Thread-safe.  Persists to disk and emits a 'mode_change' SocketIO event.
    """
    global _listen_mode
    if mode not in ("wake_word", "conversation"):
        logger.warning("Unknown listen mode %r — ignoring", mode)
        return
    with _mode_lock:
        if _listen_mode == mode:
            return
        _listen_mode = mode
    _save_listen_mode(mode)
    logger.info("Listen mode → %s", mode)
    try:
        import core.server as _srv
        _srv.socketio.emit("mode_change", {"mode": mode})
    except Exception:
        pass


# Load persisted mode at module import (before the listen loop starts)
_listen_mode = _load_listen_mode()


# ---------------------------------------------------------------------------
# Vosk model singletons
# ---------------------------------------------------------------------------

_model_es      = None
_model_en      = None
_models_lock   = threading.Lock()
_en_available  = None

models_ready = threading.Event()
# Set inside listen() once sd.InputStream opens; _signal_ready() waits on this
# before marking the system ready — prevents jarvis_ready from firing while the
# mic pipeline is still initializing.
mic_ready = threading.Event()


def _get_models():
    global _model_es, _model_en, _en_available
    with _models_lock:
        if _model_es is None:
            vosk.SetLogLevel(-1)
            try:
                _model_es = vosk.Model(VOSK_MODEL_ES_PATH)
                logger.info("Vosk ES model loaded.")
            except Exception as e:
                logger.critical(
                    "Failed to load Spanish Vosk model from '%s': %s — "
                    "listener cannot start without this model.",
                    VOSK_MODEL_ES_PATH, e,
                )
                raise
        if _en_available is None:
            try:
                _model_en     = vosk.Model(VOSK_MODEL_EN_PATH)
                _en_available = True
                logger.info("Vosk EN wake-word model loaded (dual-recognizer active).")
            except Exception as e:
                _en_available = False
                # Bug fix (Bug 7): raised to WARNING so it surfaces in the activity
                # log.  A socket emit is deferred to the listen loop where the
                # server is guaranteed to be running.
                logger.warning(
                    "Vosk EN model unavailable (%s) — English-accented 'HUGO' detection may be unreliable.", e
                )
    models_ready.set()
    return _model_es, _model_en if _en_available else None


# ---------------------------------------------------------------------------
# Audio buffer
# ---------------------------------------------------------------------------

class CircularBuffer:
    def __init__(self, max_seconds):
        self.max_frames = int(max_seconds * SAMPLERATE)
        # deque with maxlen gives O(1) append + automatic eviction of old samples.
        # np.roll() was O(n) and allocated a new array on every 100ms chunk.
        self.buffer: collections.deque = collections.deque(maxlen=self.max_frames)

    def update(self, new_data):
        # extend() appends each sample; old samples auto-evicted via maxlen.
        self.buffer.extend(new_data)

    def get_audio(self):
        return np.array(self.buffer, dtype=np.int16).tobytes()


# ---------------------------------------------------------------------------
# Main listen loop
# ---------------------------------------------------------------------------

def listen(stop_event):
    """Main listen loop. Runs until stop_event is set."""
    import core.commands as commands
    global _enroll_requested, _enroll_in_progress

    audio_q         = queue.Queue()
    model_es, model_en = _get_models()

    rec_es = vosk.KaldiRecognizer(model_es, SAMPLERATE)
    rec_es.SetWords(True)

    rec_en = (
        vosk.KaldiRecognizer(model_en, SAMPLERATE, _EN_WAKE_GRAMMAR)
        if model_en else None
    )

    circular_buffer = CircularBuffer(max_seconds=5)

    # Thread pool for parallel ES + EN recognition per audio chunk
    _recog_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="recog")

    def _audio_callback(indata, frames, time_info, status):
        if status:
            logger.warning("Audio stream status: %s", status)
        audio_q.put(indata.copy())

    # Bug fix (Bug 7): emit a user-visible warning if the EN model is missing.
    # Done here (not in _get_models) because the SocketIO server is guaranteed
    # to be running by the time listen() is called.
    if not _en_available:
        logger.warning(
            "English wake-word model is not available. "
            "English-accented 'HUGO' detection relies on the Spanish recognizer only and may be less reliable."
        )
        try:
            import core.server as _srv_warn
            _srv_warn.socketio.emit("log", {
                "type": "system",
                "message": (
                    "Aviso: modelo EN no disponible — "
                    "la detección de 'HUGO' en inglés puede ser menos fiable."
                ),
            })
        except Exception:
            pass

    logger.info("Jarvis is listening...")

    # MIC_NAME from .env — empty/unset means auto-detect the system default input.
    _TARGET = os.environ.get("MIC_NAME", "").strip() or None
    device  = None
    if _TARGET:
        for idx, info in enumerate(sd.query_devices()):
            if _TARGET in info["name"] and info["max_input_channels"] > 0:
                device = idx
                break
        if device is not None:
            logger.info("Audio device: %d — %s", device, _TARGET)
        else:
            logger.warning("Device '%s' not found — using system default.", _TARGET)
    else:
        logger.info("MIC_NAME not set — using system default microphone.")

    try:
        stream = sd.InputStream(
            samplerate=SAMPLERATE, channels=1, dtype="int16",
            blocksize=1600,  # 100ms/chunk (halved from 200ms) — doubles wake check frequency
            device=device, callback=_audio_callback,
        )
    except Exception as e:
        logger.warning("Could not open device %s (%s) — falling back to default.", device, e)
        stream = sd.InputStream(
            samplerate=SAMPLERATE, channels=1, dtype="int16",
            blocksize=1600,  # 100ms/chunk (halved from 200ms) — doubles wake check frequency
            device=None, callback=_audio_callback,
        )

    import core.server as server_mod
    import core.memory as memory
    import core.voice as voice

    # RMS level tracking
    _last_level_emit = time.monotonic()
    _rms_window_emit: list[float] = []

    # Rolling RMS energy gate — kept for compute_rms's level-meter use and
    # as an inert rollback path (see core.vad's own top comment); no longer
    # the live VAD decision signal.
    _rms_rolling: collections.deque = collections.deque(maxlen=_RMS_WINDOW_SIZE)

    # Silero VAD (2026-08-20) — the live speech/silence signal, replacing
    # the RMS gate/silence-tracker at every call site below. One buffer per
    # stream context, same "caller owns mutable state" reasoning as
    # _rms_rolling above (see SileroSpeechBuffer's own docstring for why it
    # needs to own leftover-sample state across calls).
    _silero_buffer = SileroSpeechBuffer()

    # Interrupt-ducking state (step 1 — see core.vad.duck_gate_update's own
    # docstring). Only ever touched while is_auto_muted() and the
    # 'interrupt_ducking_enabled' feature flag are both true; harmless,
    # unused state the rest of the time.
    _duck_rolling: collections.deque = collections.deque(maxlen=max(_DUCK_TRIGGER_WINDOW, _DUCK_RELEASE_WINDOW))
    _ducked = False
    _was_auto_muted = False   # edge-detects a fresh TTS turn starting, to reset stale duck state from the previous one

    # Interrupt-decision state (step 2 — see core.speaker.check_interrupt_speaker's
    # own docstring). Raw audio accumulates here ONLY while ducked (a
    # candidate interruption); once it reaches _INTERRUPT_CHECK_DURATION_SECS
    # the identity check runs on a background thread (never inline — ECAPA
    # embedding isn't cheap enough to run in the audio callback's own
    # real-time budget) and _interrupt_decided latches so it only fires once
    # per ducked episode, not every chunk after the threshold.
    _interrupt_buffer: list = []
    _interrupt_decided = False

    # STT chunk-processing latency diagnostic (2026-08-10) — each chunk is
    # 100ms of audio (blocksize=1600 @ 16kHz); if AcceptWaveform (both
    # recognizers, run in parallel via _recog_pool) takes meaningfully
    # longer than 100ms on average, Vosk is falling behind real-time and
    # adding genuine extra latency on top of however long Joan actually
    # spoke — logged once per _STT_CHUNK_LOG_EVERY chunks (~5s of audio)
    # rather than every single chunk, to keep this from flooding the log.
    _stt_chunk_times: list[float] = []
    _STT_CHUNK_LOG_EVERY = 50

    # ── Wake-word mode state ─────────────────────────────────────────────────
    _collecting          = False
    _collect_start       = 0.0
    _collect_personality: str | None = None
    _collected_parts: list[str]      = []
    _silence_since: float | None     = None   # for VAD early cutoff
    # True when this collection window was opened by the post-response
    # context window (no wake word said) rather than an explicit wake word —
    # threaded through to commands.dispatch_command() so the right social
    # reasoning check (is_addressed vs. should_continue) is applied.
    _collect_is_continuation: bool   = False

    # ── Conversation mode state ──────────────────────────────────────────────
    # Part 2: rolling buffer stores (timestamp, text) pairs for the last
    # _CONV_BUFFER_SECS seconds.  When a wake word is detected in a segment,
    # the buffer contents become the conversation context for the dispatch call.
    _conv_buffer: collections.deque = collections.deque()  # (float, str)
    _conv_collecting     = False   # True after wake word detected in conv mode
    _conv_collect_start  = 0.0
    _conv_personality: str | None = None
    _conv_parts: list[str]        = []
    _conv_silence: float | None   = None

    # ── Passive ambient buffer (Phase 3) ─────────────────────────────────────
    # Rolling (timestamp, text) log of every finalized segment heard, in
    # either listen mode, wake word or not — see the module comment above
    # _PASSIVE_BUFFER_SECS for the full flow.
    _passive_buffer: collections.deque = collections.deque()  # (float, str)
    _last_passive_check = time.monotonic()

    # ── Voice enrollment (Phase 4) ────────────────────────────────────────────
    # See the module comment above request_voice_enrollment() for the full
    # cross-thread trigger flow. _enroll_awaiting_prompt is True between
    # (a) the enrollment starting or a sample finishing and (b) the
    # resulting TTS prompt actually finishing — audio is NOT accumulated
    # during that window, so HUGO's own prompt voice never contaminates the
    # next sample.
    _enroll_active           = False
    _enroll_awaiting_prompt  = False
    _enroll_parts: list      = []   # raw int16 chunks for the sample in progress
    _enroll_collect_start    = 0.0
    _enroll_silence: float | None = None
    _enroll_samples_done     = 0
    _enroll_paths: list[str] = []

    # Dispatch guard — prevents overlap while a command is being processed
    _dispatch_in_progress = threading.Event()

    # Wake-word rate limiter
    _last_wake_time: float = 0.0

    # Per-iteration JSON result caches — updated each time AcceptWaveform fires
    # (i.e., when a new finalized segment arrives). All reads within an iteration
    # use the cached dict to avoid re-parsing the same Vosk result string.
    _es_result_json: dict = {"text": "", "result": []}
    _en_result_json: dict = {"text": "", "result": []}

    def _run_dispatch(text: str, personality: str, context: str | None = None,
                      is_continuation: bool = False, audio_snapshot: bytes | None = None) -> None:
        """Run dispatch in background — mutes mic immediately, unmutes after TTS.

        Args:
            context: Optional rolling transcript context from conversation mode.
                     Passed through to commands.dispatch_command().
            is_continuation: True when this text was captured without a wake
                     word, inside the post-response context window — tells
                     commands.dispatch_command() to gate with
                     social_reasoning.should_continue() instead of
                     is_addressed().
            audio_snapshot: Phase 4/5 — raw int16 PCM bytes of this
                     utterance (circular_buffer.get_audio(), captured by the
                     caller right as the command window closed — the buffer
                     itself keeps rolling, so it must be snapshotted before
                     this thread starts). Written to a wav file here (off
                     the audio thread) and passed to dispatch_command() as
                     audio_path for the multi-factor speaker identification
                     gate in core/commands.py. None for conversation-mode
                     dispatch (no speaker check there yet) or if the write
                     fails — identification just falls back to "no signal"
                     in that case, never blocks the reply.
        """
        set_auto_muted(True)
        audio_path = None
        if audio_snapshot:
            try:
                os.makedirs("data/tmp", exist_ok=True)
                audio_path = "data/tmp/speaker_sample.wav"
                pcm = np.frombuffer(audio_snapshot, dtype=np.int16)
                sf.write(audio_path, pcm, SAMPLERATE, subtype="PCM_16")
            except Exception:
                logger.debug("Speaker sample snapshot failed", exc_info=True)
                audio_path = None
        try:
            commands.dispatch_command(
                text, context=context, voice_gated=True, is_continuation=is_continuation,
                audio_path=audio_path,
            )
        except Exception:
            logger.exception("Error during dispatch")
        finally:
            _dispatch_in_progress.clear()
            # Auto-unmute is normally handled by voice._schedule_auto_unmute,
            # which fires ~1.5s after actual TTS playback finishes. But
            # dispatch_command() only *queues* TTS (core.voice.speak_* puts a
            # job on _tts_queue and returns immediately) — it does not wait
            # for playback — so this point is reached while the reply is
            # still being spoken. Only clear the mute here as a fallback for
            # the case TTS never fired at all (empty reply / exception before
            # speak); otherwise let the real playback-driven unmute do it, or
            # HUGO hears and reacts to her own voice.
            if not voice.tts_pending():
                set_auto_muted(False)

    def _do_dispatch(full_text: str, personality: str,
                     context: str | None = None) -> None:
        """Emit partial clear, mark in-progress, spawn dispatch thread."""
        nonlocal _collecting, _collected_parts, _collect_personality, _silence_since
        nonlocal _collect_is_continuation
        # Corrects a mis-heard "Lyra"/"Leera"/etc. anywhere in the text, not
        # just the bare-wake-word case personality.py's _WAKE_ONLY_RE
        # already handled — see normalize_wake_word_text's own docstring.
        full_text = normalize_wake_word_text(full_text)
        is_continuation       = _collect_is_continuation
        _collecting          = False
        _collected_parts     = []
        _collect_personality = None
        _silence_since       = None
        _collect_is_continuation = False
        # Phase 4/5: snapshot the last ~5s of raw audio NOW, before this
        # command's utterance ages out of the rolling circular_buffer.
        audio_snapshot = circular_buffer.get_audio()
        rec_es.Reset()
        if rec_en:
            rec_en.Reset()
        # Clear partial transcript display
        try:
            server_mod.emit_partial_transcript("")
        except Exception:
            pass
        if full_text and not _dispatch_in_progress.is_set():
            # STT+VAD phase, end to end as Joan actually experiences it:
            # from the moment the wake word was detected (_collect_start)
            # to the moment the app decided the utterance is complete
            # (this line) — includes however long Joan spoke PLUS the
            # fixed _VAD_SILENCE_SECS confirmation wait after she stopped.
            # Not the same thing as raw Vosk compute speed (see the
            # separate stt_chunk_avg diagnostic above) — a long value here
            # is often just "Joan spoke for a while", not necessarily a
            # processing bottleneck; use both numbers together to tell
            # which one it actually is.
            _stt_vad_elapsed = time.monotonic() - _collect_start if _collect_start else None
            logger.info(
                "[LATENCY] command_window_closed  text=%r  stt_vad_elapsed=%s",
                full_text, f"{_stt_vad_elapsed:.3f}s" if _stt_vad_elapsed is not None else "n/a",
            )
            # Show the final transcript in the chat log as a user message —
            # same as typed input — before dispatch_command() runs, so the
            # user's turn always appears ahead of the assistant's reply.
            server_mod.emit_user_transcript(full_text)
            _dispatch_in_progress.set()
            # A real command directed at her — never let this linger in the
            # ambient buffer to be re-processed as "overheard chatter" by
            # maybe_ambient_intervention a tick or two later (see the
            # _passive_buffer module comment: it collects wake-word speech
            # too, so without this a just-answered request could get an
            # unwanted proactive "comment" tacked onto it).
            _passive_buffer.clear()
            threading.Thread(
                target=_run_dispatch,
                args=(full_text, personality),
                kwargs={
                    "context": context, "is_continuation": is_continuation,
                    "audio_snapshot": audio_snapshot,
                },
                daemon=True,
                name="cmd-dispatch",
            ).start()
        try:
            from core.server import emit_status
            emit_status("listening")
        except Exception:
            pass

    def _do_conv_dispatch(full_text: str, personality: str,
                          context: str | None = None) -> None:
        """Dispatch a conversation-mode command and reset conv state."""
        nonlocal _conv_collecting, _conv_collect_start, _conv_personality
        nonlocal _conv_parts, _conv_silence
        # Same normalization as _do_dispatch above — see its comment.
        full_text = normalize_wake_word_text(full_text)
        _conv_collecting    = False
        _conv_collect_start = 0.0
        _conv_personality   = None
        _conv_parts         = []
        _conv_silence       = None
        _conv_buffer.clear()
        audio_snapshot = circular_buffer.get_audio()
        rec_es.Reset()
        if rec_en:
            rec_en.Reset()
        try:
            server_mod.emit_partial_transcript("")
        except Exception:
            pass
        if full_text and not _dispatch_in_progress.is_set():
            logger.info("[CONV] dispatch  text=%r  context_len=%d",
                        full_text, len(context or ""))
            # Show the final transcript in the chat log as a user message —
            # same as typed input — before dispatch_command() runs.
            server_mod.emit_user_transcript(full_text)
            _dispatch_in_progress.set()
            # Same reasoning as the wake-word path's _do_dispatch above —
            # keep a just-answered request out of the ambient buffer.
            _passive_buffer.clear()
            threading.Thread(
                target=_run_dispatch,
                args=(full_text, personality),
                kwargs={"context": context, "audio_snapshot": audio_snapshot},
                daemon=True,
                name="conv-dispatch",
            ).start()
        try:
            from core.server import emit_status
            emit_status("listening")
        except Exception:
            pass

    def _check_interrupt_candidate(chunks: list) -> None:
        """Interrupt feature, steps 2+3 — background-thread target (never
        call inline from the audio callback: ECAPA embedding isn't cheap
        enough for the callback's real-time budget). Writes the
        accumulated ducked-audio chunks to a temp WAV, runs
        core.speaker.check_interrupt_speaker() on it, and — on an
        accepted result — actually stops her (core.voice.stop_speaking())
        and immediately restores full listening (set_auto_muted(False),
        bypassing the normal 1.5s post-speech echo-decay delay — see
        core.voice._schedule_auto_unmute — since the identity check has
        already confirmed real speech is happening right now, not echo
        settling). Deliberately does NOT retroactively feed the ~2s of
        already-captured candidate audio into Vosk — whatever was said
        DURING the duck window is lost; Joan just continues naturally
        after the interrupt, same as interrupting a person. Simpler and
        lower-risk than threading that audio into the wake-word/
        conversation state machine for a first pass."""
        import core.speaker as speaker_mod
        import core.voice as voice_mod
        tmp_path = None
        try:
            pcm = np.concatenate(chunks)
            fd, tmp_path = tempfile.mkstemp(suffix="_interrupt.wav")
            os.close(fd)
            sf.write(tmp_path, pcm, SAMPLERATE, subtype="PCM_16")
            accepted = speaker_mod.check_interrupt_speaker(tmp_path)
            logger.info("[IDENTITY] Interrupt candidate resolved: %s", "ACCEPTED — stopping" if accepted else "rejected")
            if accepted:
                voice_mod.stop_speaking()
                set_auto_muted(False)
        except Exception:
            logger.warning("_check_interrupt_candidate failed (non-critical)", exc_info=True)
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    try:
        with stream:
            # Mic stream is now open and actively capturing — signal startup health check.
            # This must happen before emit_mic_active() so _signal_ready() in jarvis.py
            # unblocks only after the audio pipeline is genuinely live.
            mic_ready.set()
            server_mod.emit_mic_active()
            while not stop_event.is_set():
                # ── Mic stream pause/resume (for macOS orange-dot control) ───
                # _stop_mic_stream() clears _mic_streaming; we pause PortAudio here
                # so CoreAudio releases the input unit and the orange dot disappears.
                # _start_mic_stream() sets the event to resume.
                if not _mic_streaming.is_set():
                    if stream.active:
                        stream.stop()
                        server_mod.emit_mic_inactive()
                        server_mod.emit_mic_level(0.0)
                        # Drain stale audio chunks so they don't replay after resume
                        while not audio_q.empty():
                            try:
                                audio_q.get_nowait()
                            except queue.Empty:
                                break
                    # Block until _start_mic_stream() fires; honour stop_event every second
                    _mic_streaming.wait(timeout=1.0)
                    if _mic_streaming.is_set() and not stream.active:
                        stream.start()
                        server_mod.emit_mic_active()
                        # Re-initialize Vosk recognizers — discards any partial
                        # state accumulated before the stream was stopped.
                        if _reset_recognizers.is_set():
                            _reset_recognizers.clear()
                            rec_es.Reset()
                            if rec_en:
                                rec_en.Reset()
                    continue

                # ── Get audio chunk ──────────────────────────────────────────
                try:
                    data = audio_q.get(timeout=0.1)
                except queue.Empty:
                    now = time.monotonic()
                    if now - _last_level_emit >= 0.5:
                        _last_level_emit = now
                        server_mod.emit_mic_level(0.0)
                    # Check collection deadlines even when no audio arrives
                    if _collecting and now >= _collect_start + _COMMAND_WINDOW_SECS:
                        full_text   = " ".join(_collected_parts).strip()
                        personality = _collect_personality
                        _do_dispatch(full_text, personality)
                    elif _conv_collecting and now >= _conv_collect_start + _CONV_MAX_WINDOW_SECS:
                        full_text = " ".join(_conv_parts).strip()
                        ctx = " ".join(t for _, t in _conv_buffer)
                        _do_conv_dispatch(full_text, _conv_personality, context=ctx or None)
                    continue

                # Drop stale chunks if queue is building up (keep real-time)
                while audio_q.qsize() > 2:
                    try:
                        audio_q.get_nowait()
                    except queue.Empty:
                        break

                data_int = data[:, 0]

                # ── RMS for level emission ───────────────────────────────────
                rms = compute_rms(data_int)
                _rms_window_emit.append(rms)

                # ── Silero speech probability — the live VAD signal (see
                # core.vad's own top comment) — fed to every silence-
                # tracker/gate call site below in place of the raw rms value.
                _silero_prob = _silero_buffer.process(data_int)
                _is_speech = _silero_prob >= _SILERO_SPEECH_THRESHOLD

                now = time.monotonic()
                if now - _last_level_emit >= 0.5:
                    _last_level_emit = now
                    peak = max(_rms_window_emit) if _rms_window_emit else 0.0
                    _rms_window_emit.clear()
                    log_level = max(0.0, min(1.0, (math.log10(peak + 1e-9) + 3) / 3)) if peak > 0 else 0.0
                    server_mod.emit_mic_level(log_level)

                # ── User mute — always a full skip, no exceptions ────────────
                if is_muted():
                    continue

                # ── Auto-mute during TTS — full skip UNLESS interrupt ducking
                #    is on, in which case this chunk still gets scored for a
                #    duck-trigger (step 1) and, while ducked, accumulated
                #    toward an identity check that can actually stop her
                #    (steps 2+3 — see _check_interrupt_candidate's own
                #    docstring). The CONTINUE below still fires either way
                #    this session — wake-word/STT stays fully skipped
                #    during her own TTS regardless; a confirmed interrupt
                #    ends her TTS turn entirely (is_auto_muted() goes False
                #    from within _check_interrupt_candidate), so normal
                #    listening picks back up on its own next chunk rather
                #    than needing special handling here.
                auto_muted = is_auto_muted()
                if auto_muted:
                    if memory.is_feature_enabled("interrupt_ducking_enabled"):
                        if not _was_auto_muted:
                            # Fresh TTS turn starting — clear any stale
                            # ducked/rolling-window state left over from the
                            # previous turn (see _was_auto_muted's own
                            # comment) so every utterance starts un-ducked.
                            _duck_rolling.clear()
                            _ducked = False
                            voice.set_duck_gain(1.0)
                            _interrupt_buffer = []
                            _interrupt_decided = False
                        was_ducked = _ducked
                        _ducked = duck_gate_update(_duck_rolling, rms, _ducked, voice.get_self_output_rms())
                        voice.set_duck_gain(_DUCK_GAIN if _ducked else 1.0)

                        if _ducked:
                            _interrupt_buffer.append(data_int.copy())
                            if not _interrupt_decided:
                                total_secs = sum(len(c) for c in _interrupt_buffer) / SAMPLERATE
                                if total_secs >= _INTERRUPT_CHECK_DURATION_SECS:
                                    _interrupt_decided = True
                                    threading.Thread(
                                        target=_check_interrupt_candidate,
                                        args=(list(_interrupt_buffer),),
                                        daemon=True, name="interrupt-check",
                                    ).start()
                        elif was_ducked:
                            # Released — false alarm, or genuine speech that
                            # never reached the check duration before going
                            # quiet again. Either way, clear for whatever
                            # duck episode (if any) comes next in this turn.
                            _interrupt_buffer = []
                            _interrupt_decided = False
                    _was_auto_muted = True
                    continue
                _was_auto_muted = False

                # ════════════════════════════════════════════════════════════
                # VOICE ENROLLMENT (Phase 4) — takes over the loop entirely
                # while active, same "own branch + continue" shape as
                # conversation mode / wake-word collection below.
                # ════════════════════════════════════════════════════════════
                if not _enroll_active:
                    with _enroll_state_lock:
                        _should_start = _enroll_requested and not _enroll_in_progress
                        if _should_start:
                            _enroll_requested   = False
                            _enroll_in_progress = True
                    if _should_start:
                        _enroll_active          = True
                        _enroll_awaiting_prompt = True   # wait out the intro line commands.py already spoke
                        _enroll_parts           = []
                        _enroll_collect_start   = now
                        _enroll_silence         = None
                        _enroll_samples_done    = 0
                        _enroll_paths           = []
                        logger.info("[IDENTITY] Voice enrollment started (target=%d samples)",
                                    ENROLL_TARGET_SAMPLES)

                if _enroll_active:
                    import core.voice as _voice_enroll
                    if _enroll_awaiting_prompt:
                        if not _voice_enroll.in_cooldown():
                            _enroll_awaiting_prompt = False
                            _enroll_collect_start   = now
                            _enroll_silence         = None
                        continue

                    _enroll_parts.append(data_int.copy())
                    _e_exceeded, _enroll_silence = silero_silence_tracker_update(_is_speech, _enroll_silence, now)
                    _e_deadline = now >= _enroll_collect_start + _ENROLL_SAMPLE_MAX_SECS

                    if _e_exceeded or _e_deadline:
                        sample_pcm = (
                            np.concatenate(_enroll_parts) if _enroll_parts
                            else np.array([], dtype=np.int16)
                        )
                        _enroll_parts = []
                        duration_secs = len(sample_pcm) / SAMPLERATE

                        import core.speaker as speaker_mod
                        if duration_secs >= speaker_mod.MIN_DURATION:
                            sample_idx  = len(_enroll_paths) + 1
                            sample_path = f"data/memoria_voz/enroll_{sample_idx}.wav"
                            try:
                                os.makedirs(os.path.dirname(sample_path), exist_ok=True)
                                sf.write(sample_path, sample_pcm, SAMPLERATE, subtype="PCM_16")
                                _enroll_paths.append(sample_path)
                                _enroll_samples_done += 1
                                logger.info(
                                    "[IDENTITY] Enrollment sample %d/%d captured (%.1fs)",
                                    _enroll_samples_done, ENROLL_TARGET_SAMPLES, duration_secs,
                                )
                            except Exception:
                                logger.warning("Enrollment: failed to write sample", exc_info=True)
                        else:
                            logger.debug(
                                "Enrollment: sample too short (%.2fs) — asking again",
                                duration_secs,
                            )

                        rec_es.Reset()
                        if rec_en:
                            rec_en.Reset()

                        if _enroll_samples_done >= ENROLL_TARGET_SAMPLES:
                            paths_snapshot = list(_enroll_paths)
                            threading.Thread(
                                target=commands.finish_voice_enrollment,
                                args=(paths_snapshot,),
                                daemon=True, name="voice-enroll-finish",
                            ).start()
                            _enroll_active = False
                            with _enroll_state_lock:
                                _enroll_in_progress = False
                        else:
                            threading.Thread(
                                target=commands.prompt_next_enrollment_sample,
                                args=(_enroll_samples_done, ENROLL_TARGET_SAMPLES),
                                daemon=True, name="voice-enroll-prompt",
                            ).start()
                            _enroll_awaiting_prompt = True
                    continue

                # ── Feed recognizers in parallel ─────────────────────────────
                data_bytes = data_int.tobytes()
                circular_buffer.update(data_int)

                def _feed_es(_b=data_bytes):
                    return rec_es.AcceptWaveform(_b)

                def _feed_en(_b=data_bytes):
                    if rec_en is None:
                        return False
                    return rec_en.AcceptWaveform(_b)

                _stt_chunk_t0 = time.monotonic()
                fut_es = _recog_pool.submit(_feed_es)
                fut_en = _recog_pool.submit(_feed_en)
                es_fired = fut_es.result()
                en_fired = fut_en.result()
                _stt_chunk_times.append(time.monotonic() - _stt_chunk_t0)
                if len(_stt_chunk_times) >= _STT_CHUNK_LOG_EVERY:
                    _avg_ms = 1000 * sum(_stt_chunk_times) / len(_stt_chunk_times)
                    _max_ms = 1000 * max(_stt_chunk_times)
                    logger.info(
                        "[LATENCY] stt_chunk avg=%.1fms max=%.1fms over %d chunks (chunk_audio=100ms, real-time budget)",
                        _avg_ms, _max_ms, len(_stt_chunk_times),
                    )
                    _stt_chunk_times.clear()

                # Update JSON caches when AcceptWaveform produces a new finalized segment.
                # All code below reuses these dicts instead of re-calling json.loads().
                if es_fired:
                    _es_result_json = json.loads(rec_es.Result())
                if en_fired and rec_en is not None:
                    _en_result_json = json.loads(rec_en.Result())

                # ── Passive ambient buffer (Phase 3) ─────────────────────────
                # Every finalized segment goes in, regardless of listen mode
                # or wake-word/collecting state — this is the "hears
                # everything" half of the spec. Never touches the LLM here;
                # only the periodic handoff below does, and only past the
                # Phase 2 social reasoning gate.
                if es_fired:
                    _ambient_seg = _es_result_json.get("text", "").strip()
                    if _ambient_seg:
                        _passive_buffer.append((now, _ambient_seg))
                _ambient_cutoff = now - _PASSIVE_BUFFER_SECS
                while _passive_buffer and _passive_buffer[0][0] < _ambient_cutoff:
                    _passive_buffer.popleft()

                if now - _last_passive_check >= _PASSIVE_CHECK_INTERVAL_SECS:
                    _last_passive_check = now
                    # Only hand off when there was actual speech activity, and
                    # never while a real command is being collected/dispatched
                    # — the ambient check is strictly a background comment,
                    # not a substitute for an in-flight exchange.
                    if (_passive_buffer and not _collecting and not _conv_collecting
                            and not _dispatch_in_progress.is_set()):
                        _ambient_text = " ".join(t for _, t in _passive_buffer)
                        threading.Thread(
                            target=commands.maybe_ambient_intervention,
                            args=(_ambient_text,),
                            daemon=True,
                            name="ambient-check",
                        ).start()

                # ── Read current listen mode ─────────────────────────────────
                current_mode = get_listen_mode()

                # ════════════════════════════════════════════════════════════
                # CONVERSATION MODE
                # Part 2: streams continuously; triggers a response when the
                # active personality's name appears in a completed segment.
                # A rolling context buffer (last _CONV_BUFFER_SECS seconds) is
                # passed to dispatch so the LLM has conversation awareness.
                # ════════════════════════════════════════════════════════════
                if current_mode == "conversation":
                    # Prune rolling buffer to keep only recent transcript
                    cutoff = now - _CONV_BUFFER_SECS
                    while _conv_buffer and _conv_buffer[0][0] < cutoff:
                        _conv_buffer.popleft()

                    if _conv_collecting:
                        # ── Collecting response after wake word detected ──────
                        if es_fired:
                            seg = _es_result_json.get("text", "").strip()
                            if seg:
                                _conv_parts.append(seg)

                        # Real-time partial transcript
                        try:
                            partial = json.loads(rec_es.PartialResult()).get("partial", "").strip()
                            if partial:
                                server_mod.emit_partial_transcript(partial)
                        except Exception:
                            pass

                        # VAD: dispatch after sustained silence
                        _conv_exceeded, _conv_silence = silero_silence_tracker_update(_is_speech, _conv_silence, now)
                        if _conv_exceeded:
                            # Flush any pending partial
                            try:
                                partial = json.loads(rec_es.PartialResult()).get("partial", "").strip()
                                if partial:
                                    _conv_parts.append(partial)
                            except Exception:
                                pass
                            full_text = " ".join(_conv_parts).strip()
                            ctx = " ".join(t for _, t in _conv_buffer) or None
                            logger.info("[CONV] VAD dispatch after %.1fs silence",
                                        now - _conv_silence)
                            _do_conv_dispatch(full_text, _conv_personality, context=ctx)

                        # Hard deadline guard
                        if now >= _conv_collect_start + _CONV_MAX_WINDOW_SECS:
                            full_text = " ".join(_conv_parts).strip()
                            ctx = " ".join(t for _, t in _conv_buffer) or None
                            _do_conv_dispatch(full_text, _conv_personality, context=ctx)

                    else:
                        # ── Idle: accumulate rolling buffer + watch for name ──
                        if es_fired:
                            seg_json = _es_result_json
                            seg_text = seg_json.get("text", "").strip()

                            # Add completed segment to the rolling context buffer
                            if seg_text:
                                _conv_buffer.append((now, seg_text))

                            # Check if the active personality's name appears
                            import core.voice as _voice_mod
                            if _voice_mod.in_cooldown():
                                pass   # TTS still playing — skip wake detection
                            elif _dispatch_in_progress.is_set():
                                pass   # previous command still processing
                            else:
                                wake = _scan_result(seg_json)
                                if wake:
                                    # Inter-wake cooldown applies in conv mode too
                                    if now - _last_wake_time >= _WAKE_COOLDOWN_SECS:
                                        _last_wake_time = now
                                        logger.info(
                                            "[CONV] Wake word '%s' detected — collecting response (VAD active)",
                                            wake,
                                        )
                                        _conv_collecting    = True
                                        _conv_collect_start = now
                                        _conv_personality   = wake
                                        _conv_parts         = []
                                        _conv_silence       = None
                                        try:
                                            commands.notify_user_interaction()
                                        except Exception:
                                            pass
                                        try:
                                            from core.server import emit_status
                                            emit_status("processing")
                                        except Exception:
                                            pass

                        # Show partial transcript and check for wake word in partial
                        try:
                            partial_text = json.loads(rec_es.PartialResult()).get("partial", "").strip()
                            if partial_text:
                                server_mod.emit_partial_transcript(partial_text)
                        except Exception:
                            partial_text = ""

                        # Partial wake detection for conversation mode — same logic as
                        # wake-word mode: open the collection window immediately
                        if partial_text and not _conv_collecting:
                            import core.voice as _voice_mod_cp
                            if not _voice_mod_cp.in_cooldown() and not _dispatch_in_progress.is_set():
                                partial_wake = _scan_result({"text": partial_text, "result": []})
                                if partial_wake:
                                    wake_now_p = time.monotonic()
                                    if wake_now_p - _last_wake_time >= _WAKE_COOLDOWN_SECS:
                                        _last_wake_time     = wake_now_p
                                        logger.info(
                                            "[LATENCY] wake_detect source=partial mode=conversation "
                                            "personality=%s partial=%r",
                                            partial_wake, partial_text[:50],
                                        )
                                        _conv_collecting    = True
                                        _conv_collect_start = wake_now_p
                                        _conv_personality   = partial_wake
                                        _conv_parts         = []
                                        _conv_silence       = None
                                        try:
                                            commands.notify_user_interaction()
                                        except Exception:
                                            pass
                                        try:
                                            from core.server import emit_status
                                            emit_status("processing")
                                        except Exception:
                                            pass

                    continue  # skip wake-word mode branches below

                # ════════════════════════════════════════════════════════════
                # WAKE-WORD MODE — command collection window
                # ════════════════════════════════════════════════════════════
                if _collecting:
                    # Accumulate full segments
                    if es_fired:
                        seg_text = _es_result_json.get("text", "").strip()
                        if seg_text:
                            _collected_parts.append(seg_text)
                        # Bug fix (Bug 5 — VAD timer reset):
                        # Removed: _silence_since = None
                        # The old code reset the silence timer every time a segment
                        # completed, which prevented VAD from triggering mid-thought
                        # pauses (user could wait indefinitely without the 6 s hard
                        # deadline firing).  The RMS-based else-branch below correctly
                        # resets the timer when audio energy is above threshold, so
                        # the explicit reset here is unnecessary and harmful.

                    # Real-time partial transcript display
                    try:
                        partial = json.loads(rec_es.PartialResult()).get("partial", "").strip()
                        if partial:
                            server_mod.emit_partial_transcript(partial)
                    except Exception:
                        pass

                    # VAD early cutoff — stop waiting if user stopped talking
                    _wake_exceeded, _silence_since = silero_silence_tracker_update(_is_speech, _silence_since, now)
                    if _wake_exceeded:
                        # Flush any pending partial result into collected parts
                        try:
                            partial = json.loads(rec_es.PartialResult()).get("partial", "").strip()
                            if partial:
                                _collected_parts.append(partial)
                        except Exception:
                            pass
                        full_text   = " ".join(_collected_parts).strip()
                        personality = _collect_personality
                        logger.info("[VAD] early dispatch after %.1fs silence, text=%r",
                                    now - _silence_since, full_text)
                        _do_dispatch(full_text, personality)
                        continue

                    # Hard deadline
                    if now >= _collect_start + _COMMAND_WINDOW_SECS:
                        full_text   = " ".join(_collected_parts).strip()
                        personality = _collect_personality
                        _do_dispatch(full_text, personality)
                    continue

                # ── Silero speech gate (idle path only) ──────────────────────
                # Replaces the old rolling-RMS-average gate — same purpose
                # (skip wasted Vosk/wake-word work on background noise), but
                # driven by actual speech detection instead of raw loudness.
                # _rms_rolling itself is still updated (see below) purely as
                # a rollback path — update_rms_gate is never consulted now.
                update_rms_gate(_rms_rolling, rms)
                if not _is_speech:
                    continue

                # Real audio energy above ambient — signal potential user
                # interaction now, BEFORE wake-word confirmation. Without
                # this, a CPU-heavy background sleep session (see
                # commands.notify_user_interaction's own docstring) only
                # stops once the wake word is actually recognized — but a
                # sleep-driven Ollama burst can raise the mic's noise floor
                # enough that a normal-volume "Hugo" never clears the
                # wake-word confidence threshold, so sleep never gets the
                # stop signal and the noise never lets up (bug reported
                # 2026-08-19: "have to speak too loud" traced to exactly
                # this — fan noise from a live background sleep cycle
                # sitting almost as loud as normal speech at the mic).
                # notify_user_interaction() is a cheap no-op when nothing
                # is sleeping, so calling it on every gate-open chunk (not
                # just confirmed wake words) is safe from a hot audio path.
                try:
                    commands.notify_user_interaction()
                except Exception:
                    pass

                if not es_fired:
                    # Show partial transcript and check for wake word in the partial.
                    # Acting on a partial is the main latency win: we don't wait for Vosk
                    # to finalize the segment — as soon as the wake word is visible in the
                    # intermediate result the command window opens immediately.
                    try:
                        partial_text = json.loads(rec_es.PartialResult()).get("partial", "").strip()
                        if partial_text:
                            server_mod.emit_partial_transcript(partial_text)
                    except Exception:
                        partial_text = ""

                    if partial_text:
                        import core.voice as _voice_mod_p
                        if not _voice_mod_p.in_cooldown() and not _dispatch_in_progress.is_set():
                            # Use text-only scan (no per-word confidence in partials)
                            partial_wake = _scan_result({"text": partial_text, "result": []})
                            if partial_wake:
                                wake_now = time.monotonic()
                                if wake_now - _last_wake_time >= _WAKE_COOLDOWN_SECS:
                                    _last_wake_time = wake_now
                                    logger.info(
                                        "[LATENCY] wake_detect source=partial personality=%s "
                                        "partial=%r window_open_immediately=True",
                                        partial_wake, partial_text[:50],
                                    )
                                    logger.info(
                                        "Wake word '%s' detected in PARTIAL — command window "
                                        "open immediately, collecting for up to %.1fs (VAD active)...",
                                        partial_wake, _COMMAND_WINDOW_SECS,
                                    )
                                    _collecting          = True
                                    _collect_start       = wake_now
                                    _collect_personality = partial_wake
                                    # Start empty: when this segment finalises (es_fired)
                                    # the collecting path adds the full text including
                                    # any post-wake words spoken in the same breath.
                                    _collected_parts     = []
                                    _silence_since       = None
                                    try:
                                        commands.notify_user_interaction()
                                    except Exception:
                                        pass
                                    try:
                                        from core.server import emit_status
                                        emit_status("processing")
                                    except Exception:
                                        pass
                    continue

                es_json = _es_result_json

                # ── Minimum segment duration check ───────────────────────────
                seg_words = es_json.get("result", [])
                if seg_words:
                    seg_end = max(w.get("end", 0.0) for w in seg_words)
                    if seg_end < _MIN_SEGMENT_SECS:
                        logger.debug("Segment skipped (too short %.2fs): %r",
                                     seg_end, es_json.get("text", ""))
                        continue

                # ── Overall transcript confidence filter ─────────────────────
                overall_conf = _overall_confidence(es_json)
                if overall_conf < _TRANSCRIPT_CONF_THRESHOLD:
                    logger.debug("Transcript skipped (low confidence=%.2f): %r",
                                 overall_conf, es_json.get("text", ""))
                    continue

                # ── English recognizer result ────────────────────────────────
                en_json = {"text": "", "result": []}
                if rec_en:
                    if en_fired:
                        en_json = _en_result_json
                    else:
                        partial_text = json.loads(rec_en.PartialResult()).get("partial", "")
                        en_json = {"text": partial_text, "result": []}

                # ── Wake word detection ──────────────────────────────────────
                # EN: stricter threshold (_EN_CONF_THRESHOLD=0.85) than the
                # Spanish recognizer, since "hugo" is more prone to false
                # positives against ordinary English words at a lower bar.
                wake = _scan_result(es_json) or _scan_result(
                    en_json,
                    conf_threshold=_EN_CONF_THRESHOLD,
                )
                if not wake:
                    # ── Post-response context window ─────────────────────────
                    # No wake word here, but if Hugo just responded within the
                    # last _CONTEXT_WINDOW_SECS, this finalized segment may be a
                    # continuation of that exchange (e.g. "Ahora mismo no
                    # puedo"). Open a normal collection window using the last
                    # personality — social_reasoning.should_continue() still
                    # gates the actual response inside dispatch_command().
                    ctx_active, ctx_personality = _context_window_active()
                    if ctx_active:
                        cont_text = es_json.get("text", "").strip()
                        if cont_text:
                            import core.voice as voice_cw
                            if not voice_cw.in_cooldown() and not _dispatch_in_progress.is_set():
                                logger.info(
                                    "[CONTEXT] Continuation window active (%.1fs since last "
                                    "response) — text=%r",
                                    time.monotonic() - _last_response_mono, cont_text[:50],
                                )
                                _collecting              = True
                                _collect_start           = now
                                _collect_personality      = ctx_personality
                                _collected_parts         = [cont_text]
                                _silence_since            = None
                                _collect_is_continuation = True
                                try:
                                    commands.notify_user_interaction()
                                except Exception:
                                    pass
                                try:
                                    from core.server import emit_status
                                    emit_status("processing")
                                except Exception:
                                    pass
                    continue

                import core.voice as voice
                if voice.in_cooldown():
                    logger.debug("Wake word ignored — TTS cooldown active")
                    continue

                # Bug fix (Bug 4 — dispatch guard race):
                # Changed from DEBUG to INFO so the user can see their wake word
                # was received even when a previous command is still running.
                # A brief partial-transcript hint provides visual feedback.
                if _dispatch_in_progress.is_set():
                    logger.info(
                        "Wake word '%s' detected but previous dispatch still in progress — ignored",
                        wake,
                    )
                    try:
                        server_mod.emit_partial_transcript("…procesando…")
                    except Exception:
                        pass
                    continue

                # ── Inter-wake cooldown ──────────────────────────────────────
                wake_now = time.monotonic()
                if wake_now - _last_wake_time < _WAKE_COOLDOWN_SECS:
                    logger.debug("Wake word ignored — inter-wake cooldown (%.1fs remaining)",
                                 _WAKE_COOLDOWN_SECS - (wake_now - _last_wake_time))
                    continue
                _last_wake_time = wake_now

                # ── Enter post-wake confirmation window ──────────────────────
                text = es_json.get("text", "").strip()
                logger.info(
                    "[LATENCY] wake_detect source=final personality=%s text=%r",
                    wake, text[:50],
                )
                logger.info(
                    "Wake word '%s' detected in final result — collecting command for "
                    "up to %.1fs (VAD active)...", wake, _COMMAND_WINDOW_SECS,
                )
                _collecting          = True
                _collect_start       = wake_now
                _collect_personality = wake
                # Full segment text already includes any words spoken after the wake word
                # in the same breath — keep them as the start of the command buffer.
                _collected_parts     = [text] if text else []
                _silence_since       = None

                try:
                    commands.notify_user_interaction()
                except Exception:
                    pass
                try:
                    from core.server import emit_status
                    emit_status("processing")
                except Exception:
                    pass

    finally:
        _recog_pool.shutdown(wait=False)
        server_mod.emit_mic_inactive()
        server_mod.emit_mic_level(0.0)
