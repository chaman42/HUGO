# ═══════════════════════════════════════════════════════════════════════════
# VAD — voice-activity-detection helpers used by core/listener.py's audio
# loop: RMS computation, the rolling energy gate, and the silence-duration
# tracker that triggers early command-window cutoff. Pure, stateless
# functions — the caller (listener.py) owns and passes in all mutable state
# (the rolling deque, the running silence-since timestamp), so this module
# never changes any threshold, timing, or control-flow behavior versus the
# original inline code. Split out of core/listener.py (pure refactor, no
# behavior change).
#
# Silero VAD (2026-08-20) — SileroSpeechBuffer/silero_silence_tracker_update
# below are the new primary speech/silence signal, replacing the RMS-based
# update_rms_gate/silence_tracker_update pair at the three call sites in
# listener.py (idle wake-word gate, wake-word-mode silence cutoff,
# conversation-mode silence cutoff). Reasoning: RMS is pure amplitude
# thresholding, manually calibrated to one mic at one moment (see
# _RMS_THRESHOLD's own recalibration comment below) — it can't distinguish
# real speech from any other loud sound (TV, typing, a cough), and quiet
# real speech below the threshold reads as silence. Silero is a small
# (~2MB) neural model trained specifically for that distinction, and tests
# ~5ms/window on this machine — comfortable real-time headroom.
#
# The RMS functions/constants below are kept, not deleted — compute_rms
# still feeds the UI's live mic-level meter (unrelated to VAD decisions),
# and update_rms_gate/silence_tracker_update stay available as an easy
# rollback if live testing (this hasn't been tested with real speech yet,
# only synthetic noise + isolated latency benchmarks) turns up a problem
# Silero doesn't handle well on this specific mic.
#
# duck_gate_update (interrupt-ducking, further below) is UNCHANGED and
# deliberately not migrated to Silero — it's solving a different problem
# (is mic input HUGO's own voice bleeding back through the speakers vs.
# Joan's real voice, a relative-loudness comparison against her own output
# RMS) that speech/non-speech classification doesn't address.
# ═══════════════════════════════════════════════════════════════════════════
import collections
import logging
import threading

import numpy as np

logger = logging.getLogger(__name__)

# RMS energy gate — rolling average over this many chunks (raised threshold)
#
# Recalibrated 2026-08-10: the 0.05 value below was set by ear/guess ("raised
# from 0.03 to reduce background noise triggers") and turned out to be
# roughly 2.5-5x higher than this mic actually produces at comfortable
# speaking volume — Joan had to speak uncomfortably loud to ever clear it.
# Measured directly: a 6s live capture on the configured mic
# (Micrófono del MacBook Pro) at normal conversational volume, 100ms
# chunks (same chunking the app itself uses), gave RMS values ranging
# ~0.007 (inter-word pause) to ~0.019 (loudest syllable), median ~0.011.
# 0.05 sits nearly 3x above even the loudest chunk observed — normal
# speech could never have reliably tripped this gate at all.
_RMS_THRESHOLD     = 0.010   # was 0.05 — see recalibration note above; sits at the observed speech median, above the ~0.007 pause floor
_RMS_WINDOW_SIZE   = 4      # reduced from 8 → 4: 400ms gate instead of 800ms at 100ms/chunk

# Minimum Vosk segment duration (seconds)
_MIN_SEGMENT_SECS = 0.3

# Command window: max 6 seconds, with VAD early-cutoff (wake-word mode)
_COMMAND_WINDOW_SECS = 6.0
# Recalibrated 2026-08-10 alongside _RMS_THRESHOLD above, same measured data:
# 0.02 sat ABOVE the loudest observed speech chunk (~0.019) — meaning normal
# speech could be misread as silence and cut off mid-command, not just
# fail to trigger. 0.008 sits just above the observed pause floor (~0.007)
# and below typical active-speech chunks (~0.010+), so genuine silence is
# still detected without swallowing real speech.
_VAD_SILENCE_RMS     = 0.008   # was 0.02 — see recalibration note above
_VAD_SILENCE_SECS    = 0.8    # reduced from 1.5s — dispatch sooner when speech ends

# Conversation mode: longer hard-timeout, same VAD threshold
_CONV_BUFFER_SECS    = 15.0   # keep this many seconds of rolling transcript
_CONV_MAX_WINDOW_SECS = 12.0  # hard cap after wake-word detected in conv mode

# Interrupt-ducking trigger (interrupt feature, step 1 — see ~/.claude memory
# project_interrupt_feature.md for the full design/status).
#
# A FIXED absolute RMS threshold breaks the moment playback volume changes
# — self-bleed scales with how loud she's actually playing, so a number
# calibrated at one volume is wrong at another. Instead the trigger is
# RELATIVE: mic RMS is compared against a fraction of her own CURRENT
# output signal's RMS (core.voice.get_self_output_rms(), updated live by
# whichever engine's playback consumer is running) — both sides scale
# together as volume changes, so this doesn't need re-tuning per volume
# level the way an absolute number would.
#
# Still not independently measured end-to-end (that needs a real mic+
# speakers-on test this codebase hasn't run yet — see the memory doc), but
# both ingredients below ARE genuinely measured, just not together in one
# live test:
#   - Real conversational speech into this exact mic: RMS ~0.007 (pause) to
#     ~0.019 (loudest syllable), median ~0.011 (see _RMS_THRESHOLD's own
#     recalibration note above).
#   - Kokoro's own generated signal (measured directly from real synthesized
#     PCM, no playback needed — a real phrase's per-100ms-chunk RMS: mean
#     ~0.041, median ~0.039, max ~0.098).
# If near-field speaker-to-mic self-bleed rivals normal speech-at-mic
# loudness (plausible — the reason real products need dedicated echo
# cancellation, see the earlier fixed-threshold version's own reasoning),
# the implied bleed ratio is roughly 0.011-0.041 (mic) / 0.039-0.041
# (output) ≈ 0.27-0.48. _DUCK_BLEED_RATIO sits with real margin above that
# range. _DUCK_TRIGGER_FLOOR_RMS covers the case her output RMS is near
# zero (silence gaps between segments) — without a floor, ratio*0 would
# make ANY mic noise "trigger", which is backwards; the floor uses the same
# reasoning the old fixed threshold did (margin above the measured
# loudest-syllable ceiling).
#
# Deliberately biased toward triggering TOO easily rather than missing a
# real interruption: unlike the eventual accept/reject decision for an
# actual interrupt (biased the OTHER way — false-reject over false-accept,
# see the memory doc), ducking itself is cheap and reversible, so the cost
# of an unnecessary duck is low. Re-measure for real (mic live, speakers ON,
# normal listening volume) before trusting either number here, and before
# building step 2 on top of this — these are reasoned placeholders, not
# calibrated constants.
_DUCK_BLEED_RATIO      = 0.65   # trigger when mic RMS exceeds this fraction of her own current output RMS
_DUCK_TRIGGER_FLOOR_RMS = 0.03   # absolute floor for when her output RMS is ~0 (silence gaps)
_DUCK_TRIGGER_WINDOW = 3      # rolling chunks (300ms @ 100ms/chunk) before triggering — avoids flickering on one noisy chunk
_DUCK_RELEASE_WINDOW = 5      # rolling chunks (500ms) of quiet before un-ducking — slightly longer than trigger so it doesn't chatter right at the threshold
_DUCK_GAIN           = 0.15   # "quieter, not silent" — she's still audibly speaking, just ducked, per the "duck, don't decide instantly" design


def compute_rms(data_int) -> float:
    """RMS energy (0.0-1.0 range) of one int16 audio chunk."""
    return float(np.sqrt(np.mean(data_int.astype(np.float32) ** 2))) / 32768.0


def compute_rms_float(data_float) -> float:
    """RMS energy (0.0-1.0 range) of one already-normalized float32 chunk
    (samples in [-1, 1], e.g. Kokoro/XTTS's own generated audio) — same
    computation as compute_rms but without the /32768.0 int16-range
    conversion, since these samples are already in that same normalized
    range. Used by core.voice's self-output RMS tracking (interrupt
    feature, step 1) to measure how loud HUGO is currently playing,
    independent of the mic-side int16 path."""
    return float(np.sqrt(np.mean(data_float.astype(np.float32) ** 2)))


def update_rms_gate(rolling: "collections.deque[float]", rms: float,
                     threshold: float = _RMS_THRESHOLD) -> bool:
    """Append `rms` to the rolling window (mutated in place — same deque
    object the caller holds, so behavior is identical to the original
    inline `_rms_rolling.append(rms)` + average) and return whether the
    rolling average clears `threshold` — i.e. whether the idle path should
    keep processing this chunk (wake-word scan) instead of skipping it as
    background noise."""
    rolling.append(rms)
    avg = sum(rolling) / max(len(rolling), 1)
    return avg > threshold


def silence_tracker_update(
    rms: float,
    silence_since: float | None,
    now: float,
    rms_threshold: float = _VAD_SILENCE_RMS,
    silence_secs: float = _VAD_SILENCE_SECS,
) -> tuple[bool, float | None]:
    """One VAD early-cutoff tick, shared by wake-word and conversation mode's
    identical silence-tracking logic. Returns (exceeded, new_silence_since):

      - exceeded=True means sustained silence has lasted >= silence_secs —
        the caller should flush its collected text and dispatch.
      - new_silence_since is the value the caller should store back into its
        own `_silence_since`/`_conv_silence` nonlocal for the next tick.

    Purely functional — no side effects beyond the returned values, so the
    caller's collected-text/dispatch-triggering logic (which differs between
    wake-word and conversation mode) stays exactly where it was.
    """
    if rms < rms_threshold:
        if silence_since is None:
            return False, now
        if now - silence_since >= silence_secs:
            return True, silence_since
        return False, silence_since
    return False, None


def duck_gate_update(rolling: "collections.deque[float]", rms: float,
                      currently_ducked: bool, self_output_rms: float,
                      bleed_ratio: float = _DUCK_BLEED_RATIO,
                      trigger_floor: float = _DUCK_TRIGGER_FLOOR_RMS,
                      trigger_window: int = _DUCK_TRIGGER_WINDOW,
                      release_window: int = _DUCK_RELEASE_WINDOW) -> bool:
    """One interrupt-ducking tick (step 1 — see _DUCK_BLEED_RATIO's own
    comment for the design/reasoning behind this). `rolling` is a deque the
    caller owns (maxlen=max(trigger_window, release_window), mutated in
    place same as update_rms_gate above); returns whether the caller should
    be ducked (voice.set_duck_gain) after this chunk.

    `self_output_rms` (core.voice.get_self_output_rms(), her own current
    playback loudness) sets the trigger level ADAPTIVELY — max(trigger_floor,
    bleed_ratio * self_output_rms) — rather than a fixed RMS number, so this
    keeps working as playback volume changes instead of needing re-tuning
    per volume level. Recomputed fresh each call since her output RMS
    varies segment to segment (louder syllables, quieter pauses); using the
    single latest value as a stand-in for the whole rolling window is a
    reasonable approximation at 100ms/chunk granularity, not worth a
    windowed average of its own.

    Hysteresis: needs `trigger_window` consecutive above-threshold chunks to
    duck, but `release_window` consecutive below-threshold chunks to
    un-duck — release is deliberately the longer window so a real interrupt
    attempt doesn't get chattered on by a brief dip mid-sentence (see step
    2/3, not built yet, for turning a sustained duck into an actual stop)."""
    trigger_rms = max(trigger_floor, bleed_ratio * self_output_rms)
    rolling.append(rms)
    window = trigger_window if not currently_ducked else release_window
    recent = list(rolling)[-window:]
    if len(recent) < window:
        return currently_ducked   # not enough history yet — hold current state
    above = all(r >= trigger_rms for r in recent)
    below = all(r < trigger_rms for r in recent)
    if not currently_ducked and above:
        return True
    if currently_ducked and below:
        return False
    return currently_ducked


# ---------------------------------------------------------------------------
# Silero VAD — see this module's own top comment for why. Silero's model
# requires an EXACT window of 512 samples at 16kHz (32ms) per call, not an
# arbitrary chunk size — listener.py's audio loop reads 100ms (1600-sample)
# chunks, which isn't a multiple of 512 (1600 = 3*512 + 64 remainder), so
# SileroSpeechBuffer below carries the leftover samples across calls rather
# than requiring listener.py to change its own chunking.
# ---------------------------------------------------------------------------

_SILERO_SAMPLE_RATE    = 16000
_SILERO_WINDOW_SAMPLES = 512      # fixed by the model itself, not a tunable
_SILERO_SPEECH_THRESHOLD = 0.5    # Silero's own documented default cutoff

_silero_model = None
_silero_lock  = threading.Lock()


def _get_silero_model():
    """Lazy singleton, same pattern as core/embeddings.py's _get_model() —
    loading costs a few seconds, so this happens once per process, not per
    call. Thread-safe: listener.py's audio loop and the pre-warm thread
    below could both reach this on process start."""
    global _silero_model
    if _silero_model is None:
        with _silero_lock:
            if _silero_model is None:
                from silero_vad import load_silero_vad
                _silero_model = load_silero_vad()
    return _silero_model


class SileroSpeechBuffer:
    """Owns the leftover-sample carry between calls (same 'caller owns
    mutable state' contract as the rolling deques elsewhere in this
    module — listener.py instantiates one of these per audio stream
    context, same as it owns _rms_rolling). process() takes one arbitrary-
    length int16 chunk and returns a single aggregated speech probability
    for it (max across every complete 512-sample window formed from
    leftover+new samples — max rather than mean so a chunk containing even
    a brief real speech onset isn't diluted by mostly-silence in the same
    100ms window, which matters most for the silence-cutoff timer: better
    to slightly delay a cutoff than clip the start of the next word).

    Returns 0.0 (not an error) if the model fails to load or inference
    raises — never raises, so a live import/runtime problem degrades to
    'never detects speech via Silero' rather than crashing the audio loop.
    Callers should already have the RMS-based path available as a fallback
    (see this module's top comment) if that degradation is ever observed
    in practice.
    """

    def __init__(self):
        self._leftover = np.array([], dtype=np.float32)

    def process(self, data_int) -> float:
        try:
            model = _get_silero_model()
        except Exception as e:
            logger.debug("[VAD] Silero model unavailable: %s", e)
            return 0.0

        floats = data_int.astype(np.float32) / 32768.0
        buf = np.concatenate([self._leftover, floats])

        n_windows = len(buf) // _SILERO_WINDOW_SAMPLES
        if n_windows == 0:
            self._leftover = buf
            return 0.0

        usable = n_windows * _SILERO_WINDOW_SAMPLES
        self._leftover = buf[usable:]

        import torch
        best = 0.0
        try:
            with torch.no_grad():
                for i in range(n_windows):
                    window = buf[i * _SILERO_WINDOW_SAMPLES:(i + 1) * _SILERO_WINDOW_SAMPLES]
                    prob = model(torch.from_numpy(window), _SILERO_SAMPLE_RATE).item()
                    if prob > best:
                        best = prob
        except Exception as e:
            logger.debug("[VAD] Silero inference failed: %s", e)
            return 0.0
        return best


def silero_silence_tracker_update(
    is_speech: bool,
    silence_since: float | None,
    now: float,
    silence_secs: float = _VAD_SILENCE_SECS,
) -> tuple[bool, float | None]:
    """Same contract/return shape as silence_tracker_update above (drop-in
    replacement at the three listener.py call sites), but driven by
    Silero's speech/no-speech decision instead of an RMS threshold
    comparison — the caller passes `prob >= _SILERO_SPEECH_THRESHOLD`
    (SileroSpeechBuffer.process()'s return value already thresholded) as
    is_speech."""
    if not is_speech:
        if silence_since is None:
            return False, now
        if now - silence_since >= silence_secs:
            return True, silence_since
        return False, silence_since
    return False, None


def _prewarm_silero() -> None:
    """Same reasoning as core/embeddings.py's _prewarm(): a lazy first
    load+inference costs real time (model load ~5s, confirmed 2026-08-20)
    that would otherwise land on whichever audio chunk happens to be first
    after process start. Runs one real inference call (a silent window),
    not just model construction — see embeddings.py's own comment on why
    that distinction matters (object construction alone left the first
    real call still slow)."""
    try:
        buf = SileroSpeechBuffer()
        buf.process(np.zeros(_SILERO_WINDOW_SAMPLES, dtype=np.int16))
        logger.debug("[VAD] Silero pre-warm complete.")
    except Exception as e:
        logger.debug("[VAD] Silero pre-warm failed: %s", e)


threading.Thread(target=_prewarm_silero, daemon=True, name="silero-vad-prewarm").start()
