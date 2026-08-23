"""XTTS v2 (Coqui TTS) — voice-cloning text-to-speech engine. Enabled via
TTS_ENGINE=xtts in .env (see core/commands.py's _say_for()); this module is
only ever imported when that's set, so the heavy TTS/torch model-loading
machinery never touches the default (Kokoro) path.

Clones the voice in data/voice_reference.wav — supplied by Joan; this
module has no opinion on and does nothing to determine whose voice that is
— via XTTS's own conditioning-latents mechanism (a few seconds of reference
audio → a fixed speaker embedding + GPT conditioning tensor, computed once
and cached, not re-derived per utterance).

Runs on CPU. MPS was tested and rejected: torch 2.2.2's MPS backend has no
aten::_fft_r2c / ComplexFloat support, which XTTS's mel-spectrogram step
needs (raises NotImplementedError even with PYTORCH_ENABLE_MPS_FALLBACK=1).
There's no CUDA on this Mac either.

Streaming: uses the low-level Xtts model's own inference_stream() generator
(not the high-level TTS.api.TTS.tts_to_file()/tts(), which are fully
blocking) so playback can start on the first generated chunk instead of
waiting for the whole utterance to finish. This requires
transformers==4.36.2 — see requirements.txt's own comment on that pin;
newer transformers breaks XTTS's GPT2InferenceModel/stream_generator.py in
this (unmaintained, archived-upstream) TTS==0.22.0 release. Confirmed:
first-chunk latency is still ~15-17s on this CPU (XTTS is a much heavier
model than Kokoro-82M) — streaming means Joan hears the reply sooner than
waiting for the full generation, not that it becomes fast.

Bilingual: language is detected per utterance (a lightweight stopword
heuristic below) and passed explicitly to inference_stream — XTTS has no
auto-detect of its own.
"""
import logging
import os
import re
import threading
import time

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VOICE_REFERENCE_PATH = os.path.join(_REPO_ROOT, "data", "voice_reference.wav")

XTTS_MODEL_NAME        = "tts_models/multilingual/multi-dataset/xtts_v2"
XTTS_SAMPLE_RATE       = 24000  # XTTS v2's native output rate
XTTS_STREAM_CHUNK_SIZE = 20     # frames per streamed chunk — library default

# Coqui's own Terms-of-Service gate on the XTTS v2 checkpoint download (its
# non-commercial CPML license) — set before the model is ever loaded so a
# first-time download never blocks on manage.py's interactive input()
# prompt. Also set in .env for the same reason on a machine that hasn't
# cached the model yet when this module is imported in some other context.
os.environ.setdefault("COQUI_TOS_AGREED", "1")

_xtts_model     = None
_xtts_lock      = threading.Lock()
_xtts_available = None   # None = untested, True/False after first attempt

_latents_lock         = threading.Lock()
_cached_latents       = None   # (gpt_cond_latent, speaker_embedding)
_cached_latents_mtime = None   # VOICE_REFERENCE_PATH's mtime when cached

# Idle-unload — releases the ~1.8GB resident model after
# _XTTS_IDLE_UNLOAD_SECONDS of no real use (see speak_streaming()'s own
# _touch_xtts_last_used() call and _idle_unload_loop() below), same
# "load on demand, unload after use" discipline applied to Ollama's own
# llama-server elsewhere (see core/ollama_control.py) after a real
# incident on this machine — XTTS itself doesn't burn CPU while idle the
# way llama-server did, but there's no reason to hold ~1.8GB resident
# indefinitely once XTTS_ENGINE goes unused for a while either. 5 minutes
# matches Ollama's own default OLLAMA_KEEP_ALIVE, for a consistent policy
# across both engines rather than picking an arbitrary different number.
_XTTS_IDLE_UNLOAD_SECONDS = 300
_XTTS_IDLE_CHECK_INTERVAL = 60

_xtts_last_used_lock  = threading.Lock()
_xtts_last_used: float = 0.0

# Whether XTTS is the engine actually selected at startup (TTS_ENGINE=xtts
# in .env) — only the currently-selected engine is kept permanently
# resident (mirrors core.voice's always-on Kokoro singleton). When XTTS is
# only ever reached as core.voice._speak_xtts_blocking's on-demand fallback
# path (default engine is kokoro), the idle-unload discipline below still
# applies so a one-off XTTS use doesn't leave ~1.8GB resident indefinitely.
_XTTS_IS_SELECTED_ENGINE = os.getenv("TTS_ENGINE", "kokoro").strip().lower() == "xtts"


def _touch_xtts_last_used() -> None:
    global _xtts_last_used
    with _xtts_last_used_lock:
        _xtts_last_used = time.monotonic()


def _get_xtts():
    """Lazy singleton — same pattern as core.voice._get_kokoro(). Downloads
    the ~1.8GB XTTS v2 checkpoint on first use if not already cached
    locally (~/Library/Application Support/tts on macOS). _xtts_available
    is reset to None (not left permanently False) by _unload_xtts() below,
    so a transient earlier failure doesn't block every future attempt for
    the rest of the process's life."""
    global _xtts_model, _xtts_available
    with _xtts_lock:
        if _xtts_available is False:
            return None
        if _xtts_model is None:
            try:
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    from TTS.api import TTS as TTSApi
                    api = TTSApi(model_name=XTTS_MODEL_NAME, progress_bar=False, gpu=False)
                _xtts_model = api.synthesizer.tts_model
                _xtts_available = True
                logger.info("XTTS v2 model loaded (CPU).")
                _touch_xtts_last_used()   # starts the idle clock fresh from the moment it became available
            except Exception as e:
                _xtts_available = False
                logger.warning("XTTS v2 unavailable: %s — will fall back to Kokoro.", e)
        return _xtts_model


def _unload_xtts() -> None:
    """Frees the resident model + cached conditioning latents — called by
    _idle_unload_loop() below after _XTTS_IDLE_UNLOAD_SECONDS of no real
    use. The next speak_streaming() call transparently reloads from
    scratch (same cold-start cost as the very first call ever)."""
    global _xtts_model, _xtts_available, _cached_latents, _cached_latents_mtime
    with _xtts_lock:
        if _xtts_model is None:
            return
        _xtts_model = None
        _xtts_available = None
    with _latents_lock:
        _cached_latents = None
        _cached_latents_mtime = None
    logger.info(
        "XTTS v2 idle for %ds — model unloaded to free memory (reloads on next use).",
        _XTTS_IDLE_UNLOAD_SECONDS,
    )


def _idle_unload_loop() -> None:
    while True:
        time.sleep(_XTTS_IDLE_CHECK_INTERVAL)
        with _xtts_lock:
            loaded = _xtts_model is not None
        if not loaded:
            continue
        with _xtts_last_used_lock:
            idle_for = time.monotonic() - _xtts_last_used
        if idle_for >= _XTTS_IDLE_UNLOAD_SECONDS:
            _unload_xtts()


if not _XTTS_IS_SELECTED_ENGINE:
    # Only guard memory for the on-demand-fallback case. When XTTS is the
    # actively selected engine (TTS_ENGINE=xtts) it's meant to stay resident
    # permanently, same as Kokoro's own always-on singleton.
    threading.Thread(target=_idle_unload_loop, daemon=True, name="xtts-idle-unload").start()


def _get_conditioning_latents():
    """Computes (and caches) the voice-clone conditioning latents from
    data/voice_reference.wav. Recomputed if the file's mtime changes, so
    swapping in a different reference recording takes effect on the next
    call rather than needing a process restart."""
    global _cached_latents, _cached_latents_mtime
    if not os.path.exists(VOICE_REFERENCE_PATH):
        raise RuntimeError(f"Missing voice reference file: {VOICE_REFERENCE_PATH}")

    mtime = os.path.getmtime(VOICE_REFERENCE_PATH)
    with _latents_lock:
        if _cached_latents is not None and _cached_latents_mtime == mtime:
            return _cached_latents
        model = _get_xtts()
        if model is None:
            raise RuntimeError("XTTS model not available")
        gpt_cond_latent, speaker_embedding = model.get_conditioning_latents(
            audio_path=[VOICE_REFERENCE_PATH],
        )
        _cached_latents = (gpt_cond_latent, speaker_embedding)
        _cached_latents_mtime = mtime
        return _cached_latents


def _prewarm() -> None:
    """Background pre-warm — mirrors core.voice._prewarm_kokoro(): loads
    the model, computes conditioning latents, and runs one near-silent
    inference_stream() chunk once at startup so every kernel PyTorch needs
    is already initialized before the first real utterance. Silent no-op if
    XTTS or the reference file aren't available yet — speak time falls back
    to Kokoro regardless (see core.voice._speak_xtts_blocking)."""
    try:
        model = _get_xtts()
        if model is None:
            return
        gpt_cond_latent, speaker_embedding = _get_conditioning_latents()
        for _ in model.inference_stream(
            "...", "es", gpt_cond_latent, speaker_embedding,
            stream_chunk_size=XTTS_STREAM_CHUNK_SIZE,
        ):
            break  # one chunk is enough to force kernel init
        logger.info("XTTS warm — listo")
    except Exception as e:
        logger.debug("XTTS pre-warm skipped: %s", e)


threading.Thread(target=_prewarm, daemon=True, name="xtts-prewarm").start()


# ---------------------------------------------------------------------------
# Bilingual detection — Spanish/English, automatic per utterance. XTTS has no
# built-in language auto-detect; this is a lightweight stopword heuristic
# (no extra ML dependency, no network call) since the only two languages
# LIRA ever actually needs are Spanish and English.
# ---------------------------------------------------------------------------

_EN_STOPWORDS = frozenset({
    "the", "is", "are", "you", "your", "and", "of", "to", "in", "it", "that",
    "this", "with", "for", "on", "was", "have", "has", "what", "how", "when",
    "where", "why", "will", "would", "can", "could", "should", "there",
    "here", "yes", "no", "hello", "hi", "thanks", "please", "not", "do",
    "does", "did", "i", "we", "they", "he", "she",
})
_ES_STOPWORDS = frozenset({
    "el", "la", "los", "las", "de", "que", "y", "en", "un", "una", "es",
    "por", "para", "con", "no", "sí", "tú", "usted", "cómo", "cuándo",
    "dónde", "qué", "porque", "está", "están", "hola", "gracias",
    "del", "al", "se", "su", "sus", "más", "pero", "también",
})

_WORD_RE = re.compile(r"[a-záéíóúñü']+")


def detect_language(text: str) -> str:
    """Returns 'en' or 'es'. Defaults to 'es' (this is a Spanish-first
    assistant) on a tie or when no recognizable stopword appears at all."""
    words = _WORD_RE.findall(text.lower())
    en_score = sum(1 for w in words if w in _EN_STOPWORDS)
    es_score = sum(1 for w in words if w in _ES_STOPWORDS)
    return "en" if en_score > es_score else "es"


# ---------------------------------------------------------------------------
# Streaming synthesis + playback
# ---------------------------------------------------------------------------

def speak_streaming(
    segments: list[str], language: str, silence_seconds: float,
    on_first_chunk=None,
) -> None:
    """Synthesizes and plays *segments* (already split on pause markers by
    the caller — see core.voice._speak_xtts_blocking) back-to-back through
    ONE open audio output stream, writing each generated chunk the moment
    it's ready. That's what makes playback start before the full utterance
    finishes synthesizing, unlike Kokoro's own fully-buffered approach
    (core.voice._speak_kokoro_blocking collects every chunk before playing
    any of them). A short silence buffer is written between segments, same
    purpose as Kokoro's own pause handling.

    on_first_chunk: optional zero-arg callable invoked exactly once, right
    before the very first generated chunk is written to the output stream
    (i.e. the moment audio genuinely starts) — see
    core.voice._emit_tts_first_audio, which core.voice._speak_xtts_blocking
    passes in here for the chat's TTS-latency display. Never called at all
    if generation fails before producing a single chunk.

    Raises on any failure (model unavailable, missing reference, no audio
    produced) — the caller falls back to Kokoro."""
    _touch_xtts_last_used()   # real usage — resets the idle-unload clock (see _idle_unload_loop)

    import numpy as np
    import sounddevice as sd
    import torch
    # Ducking (interrupt-feature infra, step 1) — lazy import to avoid a
    # module-load-order dependency between this file and core.voice
    # (core.voice already imports this module for the xtts engine, so a
    # top-level import here would be circular). Read fresh per-chunk below
    # via get_duck_gain(), not cached here — the whole point is picking up
    # a live change mid-utterance.
    from core.voice import get_duck_gain, set_self_output_rms, register_active_stream
    from core.vad import compute_rms_float
    import threading

    # Checked FIRST, deliberately — its own missing-reference-file check is
    # a plain os.path.exists() with no lock and no model touched at all, so
    # a missing data/voice_reference.wav fails in milliseconds. Calling
    # _get_xtts() first would instead block on _xtts_lock for as long as
    # the ~1.8GB model takes to load (or however much of that the
    # background pre-warm thread — see _prewarm() — hasn't finished yet)
    # before ever getting to check the one thing that actually matters here.
    gpt_cond_latent, speaker_embedding = _get_conditioning_latents()
    model = _get_xtts()
    if model is None:
        raise RuntimeError("XTTS model not available")

    silence = np.zeros((int(XTTS_SAMPLE_RATE * silence_seconds), 1), dtype=np.float32)

    stream = sd.OutputStream(samplerate=XTTS_SAMPLE_RATE, channels=1, dtype="float32")
    stream.start()
    # Registers this stream with core.voice so core.voice.stop_speaking()
    # (interrupt feature, step 3) can actually abort it from outside — see
    # register_active_stream's own docstring for why this didn't exist
    # before: this stream used to be purely local to this function,
    # invisible to _kill_active() entirely.
    stop_event = threading.Event()
    register_active_stream(stream, stop_event)
    total_chunks = 0
    try:
        for i, segment in enumerate(segments):
            if stop_event.is_set():
                break
            for chunk in model.inference_stream(
                segment, language, gpt_cond_latent, speaker_embedding,
                stream_chunk_size=XTTS_STREAM_CHUNK_SIZE,
            ):
                if stop_event.is_set():
                    break   # interrupted (core.voice.stop_speaking) — stop pulling more generated audio
                if total_chunks == 0 and on_first_chunk is not None:
                    try:
                        on_first_chunk()
                    except Exception:
                        pass
                audio = chunk.detach().cpu().numpy() if torch.is_tensor(chunk) else np.asarray(chunk)
                frame = audio.astype(np.float32).reshape(-1, 1)
                set_self_output_rms(compute_rms_float(frame))
                gain = get_duck_gain()
                stream.write(frame * gain if gain != 1.0 else frame)
                total_chunks += 1
            if stop_event.is_set():
                break
            if i < len(segments) - 1:
                stream.write(silence)
        # An interrupt (stop_event set) that lands before any chunk played
        # is a clean stop, not a failure — must NOT raise here, since the
        # caller's except-block falls back to speaking the same reply
        # through Kokoro, which would effectively un-interrupt her right
        # back into talking again the instant she was stopped.
        if total_chunks == 0 and not stop_event.is_set():
            raise RuntimeError("XTTS generated no audio")
    finally:
        stream.stop()
        stream.close()


def synthesize_pcm(text: str, language: str = "es"):
    """Synthesizes `text` to a raw float32 mono numpy array at
    XTTS_SAMPLE_RATE — no playback side effect, same 'just return the
    audio' contract as core.voice.synthesize_pcm48/synthesize_pcm48_say
    (Kokoro/say's own no-side-effect synthesis functions), which this
    mirrors for the one engine that didn't have an equivalent yet (added
    for core.speaker's self-voice fingerprinting — see that module's own
    'ignore my own voice' section). Reuses the exact same model/
    conditioning-latents path speak_streaming() does; just collects chunks
    into an array instead of writing them to a sounddevice.OutputStream.
    Raises on failure (model unavailable, missing reference, no audio
    produced) — same as speak_streaming(), callers should catch."""
    import numpy as np
    import torch

    gpt_cond_latent, speaker_embedding = _get_conditioning_latents()
    model = _get_xtts()
    if model is None:
        raise RuntimeError("XTTS model not available")

    chunks = []
    for chunk in model.inference_stream(
        text, language, gpt_cond_latent, speaker_embedding,
        stream_chunk_size=XTTS_STREAM_CHUNK_SIZE,
    ):
        audio = chunk.detach().cpu().numpy() if torch.is_tensor(chunk) else np.asarray(chunk)
        chunks.append(audio.astype(np.float32))
    if not chunks:
        raise RuntimeError("XTTS generated no audio")
    return np.concatenate(chunks)
