import datetime
import json
import math
import os
import logging

import soundfile as sf

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Speaker verification — Phase 4/5 of the conversational intelligence system.
#
# Set SPEAKER_VERIFICATION_ENABLED = False to disable: the module then loads
# without importing SpeechBrain and every public function below returns its
# documented "no signal" default (identify_speaker -> 0.0, verify_speaker ->
# False) with zero CPU/RAM impact — same contract the module had before
# Phase 4, just now the default is on.
# ---------------------------------------------------------------------------
SPEAKER_VERIFICATION_ENABLED = True  # Phase 4 — voice fingerprinting enabled

MODEL_SAVEDIR   = "data/models/spkrec-ecapa-voxceleb"
MIN_DURATION    = 1.5   # seconds

# Legacy per-file comparison path (pre-Phase 4) — kept as a fallback for
# when Joan hasn't run voice enrollment yet, so speaker ID degrades to "no
# signal" rather than hard-locking anyone out (see identify_speaker below).
OWNER_VOICE_DIR = "data/memoria_voz"
SCORE_THRESHOLD = 0.6

# Phase 4 — enrolled voice fingerprint (embedding-based, one file per app,
# not one file per sample the way OWNER_VOICE_DIR was).
FINGERPRINT_PATH  = "data/voice_fingerprint.json"
ENROLL_MIN_SAMPLES = 3
ENROLL_MAX_SAMPLES = 5

# Confidence tiers (see identify_speaker's docstring for what each means to
# a caller) — spec values, not independently calibrated against a labeled
# voice dataset (there isn't one here); tune these two numbers if real-world
# use shows the split lands wrong.
CONFIDENCE_HIGH = 0.75   # >= this: identified as Joan, full capabilities
CONFIDENCE_LOW  = 0.40   # >= this (but < HIGH): uncertain, respond but note it
                          # <  this: unknown speaker, limited response

# Interrupt feature, step 2 (see ~/.claude memory project_interrupt_feature.md
# for the full design/status) — deliberately stricter than CONFIDENCE_HIGH.
# Joan's own explicit design call: "I would prefer false rejects over false
# accepts on interrupting" — being talked over by your dad, a stranger, or
# your own TTS echo is worse than occasionally having to repeat an
# interruption attempt. This sits well above the ordinary "good enough to
# treat as Joan for a reply" bar.
INTERRUPT_CONFIDENCE_THRESHOLD = 0.85

# Above this is_own_voice() score, an interrupt candidate is rejected as
# self-echo rather than even being checked against Joan's fingerprint —
# belt-and-suspenders alongside step 1's ducking (which already tries not
# to duck on self-bleed in the first place via the adaptive ratio) for
# whatever slips through anyway.
INTERRUPT_OWN_VOICE_REJECT_THRESHOLD = 0.5

# ---------------------------------------------------------------------------
# Pitch (F0) fingerprinting — a classical acoustic biometric fused with the
# ECAPA embedding above, added specifically because ECAPA leans heavily on
# vocal-tract/timbre character, which runs similar between close relatives
# (shared anatomy — e.g. father/son) and wasn't discriminating them well in
# practice (voice scores 0.02 apart for two different real speakers, see
# core.commands' own [IDENTITY] log history). Fundamental frequency depends
# more on vocal cord length/tension, a largely independent signal — verified
# directly against real samples here: data/voice_reference.wav (the licensed
# voice-actor reference core.tts_xtts clones from, per that module's own
# docstring — not Joan's own voice) read ~192Hz mean F0 vs. a real
# data/tmp/speaker_sample.wav snapshot at ~99.5Hz, nearly an octave apart —
# still a valid demonstration that pitch separates two different speakers
# the embedding alone saw as close, just not literally "Joan vs. Joan's
# dad" as an earlier draft of this comment assumed. Fused, not a
# replacement — see identify_speaker()'s own fusion.
_PITCH_FMIN_HZ      = 60    # librosa.note_to_hz('C2') ~ 65Hz — comfortably below any adult voice
_PITCH_FMAX_HZ      = 400   # comfortably above any adult voice's fundamental (formants run higher, not F0 itself)
_PITCH_MIN_VOICED_FRAMES = 5    # fewer than this and the estimate is too noisy to trust — treat as "no pitch signal"
_PITCH_STD_FLOOR_HZ = 12.0  # comparison tolerance floor — an enrollment with near-zero measured
                             # variance (e.g. very consistent samples, or few of them) shouldn't
                             # become unrealistically strict about a normal session-to-session wobble
_VOICE_EMBEDDING_WEIGHT = 0.75   # ECAPA cosine similarity — still the dominant signal
_VOICE_PITCH_WEIGHT     = 0.25   # F0 closeness — real but narrower (one scalar vs. a full embedding)

_verifier = None


def _trust_all_enabled() -> bool:
    """'voice_trust_all' (Ajustes -> Modo Test's expandable panel) — a
    deliberate bulk/rapid-enrollment bypass: while on, identify_speaker()
    reports every sample as a confirmed match. That confidence then flows
    into BOTH existing consumers unchanged — core.commands's Phase 4/5 gate
    (full personalization, no restrict_memory) and core.social.SocialEngine.
    identify_person()'s existing 'accepted match -> absorb_sample()' wiring
    (see that function) — so turning this on both trusts and actively
    learns from whatever's said next, without needing a second code path
    duplicating that absorption logic here. Lazy-imports core.memory (same
    reasoning as every other lazy import in this file: keep this module
    importable/cheap when speaker verification itself is disabled)."""
    try:
        from core import memory
        return memory.is_feature_enabled("voice_trust_all")
    except Exception:
        return False


def _learning_enabled() -> bool:
    """'voice_learning_enabled' — see absorb_sample()'s own docstring for
    what turning this off actually changes (identification keeps running;
    only the fingerprint stops being refined by new samples)."""
    try:
        from core import memory
        return memory.is_feature_enabled("voice_learning_enabled")
    except Exception:
        return True   # flag lookup failing should never itself block learning


def _get_verifier():
    global _verifier
    if _verifier is None:
        # Lazy import — SpeechBrain (~200 MB) is only loaded when verification
        # is enabled. Keeps startup cost at zero when the flag is False.
        from speechbrain.inference import SpeakerRecognition
        logger.info("Loading SpeechBrain speaker recognition model...")
        _verifier = SpeakerRecognition.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir=MODEL_SAVEDIR,
        )
        logger.info("Speaker model ready.")
    return _verifier


def _embed_file(file_path: str):
    """Returns this file's ECAPA speaker embedding as a flat list[float], or
    None on any failure (missing file, too short, model error). Never
    raises."""
    if not os.path.exists(file_path):
        logger.warning("_embed_file: file not found: %s", file_path)
        return None
    try:
        duration = sf.info(file_path).duration
    except Exception as e:
        logger.warning("_embed_file: could not read %s: %s", file_path, e)
        return None
    if duration < MIN_DURATION:
        logger.warning(
            "_embed_file: audio too short (%.2fs < %.2fs) — skipping: %s",
            duration, MIN_DURATION, file_path,
        )
        return None
    try:
        verifier = _get_verifier()
        signal   = verifier.load_audio(file_path)
        embedding = verifier.encode_batch(signal.unsqueeze(0), normalize=False)
        return embedding.squeeze().detach().cpu().tolist()
    except Exception:
        logger.exception("_embed_file: embedding failed for %s", file_path)
        return None


def _cosine_confidence(emb_a, emb_b) -> float:
    """Cosine similarity between two embeddings, clamped to [0, 1] — ECAPA
    embeddings for genuinely different speakers already sit close to 0 and
    genuine matches sit meaningfully higher, so a plain clamp (rather than a
    rescale of the full [-1, 1] cosine range) keeps 'clearly not a match'
    near 0 instead of inflating it to ~0.5."""
    import torch
    a = torch.tensor(emb_a, dtype=torch.float32)
    b = torch.tensor(emb_b, dtype=torch.float32)
    score = torch.nn.functional.cosine_similarity(a.unsqueeze(0), b.unsqueeze(0)).item()
    return max(0.0, min(1.0, score))


def _pitch_stats(file_path: str) -> dict | None:
    """Mean/std fundamental frequency (F0, Hz) across voiced frames — see
    the pitch-fingerprinting module comment above for why this exists
    alongside the ECAPA embedding. Returns None on any failure (missing
    file, too few voiced frames to trust, librosa error) — never raises;
    callers treat None as 'no pitch signal', same degrade-gracefully
    contract as _embed_file()."""
    try:
        import numpy as np
        import librosa
        y, sr = librosa.load(file_path, sr=None, mono=True)
        f0, _voiced_flag, _voiced_probs = librosa.pyin(y, fmin=_PITCH_FMIN_HZ, fmax=_PITCH_FMAX_HZ, sr=sr)
        voiced = f0[~np.isnan(f0)]
        if voiced.size < _PITCH_MIN_VOICED_FRAMES:
            return None
        return {"mean": float(np.mean(voiced)), "std": float(np.std(voiced))}
    except Exception:
        logger.debug("_pitch_stats failed for %s", file_path, exc_info=True)
        return None


def _pitch_confidence(sample_pitch_hz: float, fingerprint: dict) -> float | None:
    """Gaussian falloff between a new sample's mean pitch and the enrolled
    fingerprint's — None if the fingerprint predates pitch fingerprinting
    (enrolled before this feature; caller falls back to embedding-only,
    see identify_speaker). sigma floors at _PITCH_STD_FLOOR_HZ so a
    fingerprint built from very consistent (or very few) enrollment
    samples doesn't become unrealistically strict about normal
    session-to-session pitch wobble (tiredness, mic distance, mood)."""
    enrolled_mean = fingerprint.get("pitch_mean_hz")
    if enrolled_mean is None:
        return None
    sigma = max(fingerprint.get("pitch_std_hz") or 0.0, _PITCH_STD_FLOOR_HZ)
    diff = sample_pitch_hz - enrolled_mean
    return math.exp(-(diff ** 2) / (2 * sigma ** 2))


# ---------------------------------------------------------------------------
# Phase 4 — enrollment
# ---------------------------------------------------------------------------

def _load_fingerprint() -> dict | None:
    try:
        with open(FINGERPRINT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("model_data"):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return None


def enroll_speaker(sample_paths: list[str]) -> dict | None:
    """Builds Joan's voice fingerprint from ENROLL_MIN_SAMPLES-ENROLL_MAX_SAMPLES
    enrollment recordings (see core/listener.py's enrollment flow, triggered
    by core/commands.py) and writes it to FINGERPRINT_PATH as
    {enrolled_at, samples_count, model_data}, where model_data is the mean
    ECAPA embedding across every sample that embedded successfully (a single
    averaged vector generalizes better than any one sample — same reasoning
    speaker-ID systems commonly use for multi-utterance enrollment).

    Returns the saved dict, or None if fewer than ENROLL_MIN_SAMPLES samples
    embedded successfully (enrollment failed — caller should tell Joan to
    try again rather than silently saving a weak fingerprint)."""
    if not SPEAKER_VERIFICATION_ENABLED:
        return None

    embeddings = []
    pitch_means = []
    for path in sample_paths:
        emb = _embed_file(path)
        if emb is not None:
            embeddings.append(emb)
        # Pitch extraction is independent of (and best-effort relative to)
        # the embedding above — a sample that embeds fine but has too few
        # voiced frames for a trustworthy F0 estimate (or vice versa)
        # shouldn't block enrollment; pitch just ends up with fewer
        # contributing samples than the embedding, or none at all (see the
        # "at least one sample" check below).
        pitch = _pitch_stats(path)
        if pitch is not None:
            pitch_means.append(pitch["mean"])

    if len(embeddings) < ENROLL_MIN_SAMPLES:
        logger.warning(
            "enroll_speaker: only %d/%d samples embedded successfully — enrollment aborted",
            len(embeddings), len(sample_paths),
        )
        return None

    dims  = len(embeddings[0])
    mean_embedding = [
        sum(e[i] for e in embeddings) / len(embeddings) for i in range(dims)
    ]

    fingerprint = {
        "enrolled_at":   datetime.datetime.now().isoformat(),
        "samples_count": len(embeddings),
        "model_data":    mean_embedding,
    }
    # pitch_std_hz here is the spread ACROSS enrollment samples' own mean
    # pitches (how much Joan's average pitch varies session to session at
    # enrollment time) — the right thing to compare a future sample's mean
    # pitch against, not the spread of individual frames within one
    # sample. Omitted entirely (not even a 0.0) when no sample yielded a
    # usable pitch estimate, so identify_speaker's fingerprint.get(
    # "pitch_mean_hz") check correctly treats this fingerprint as
    # pitch-less rather than pretending 0Hz is a real measurement.
    if pitch_means:
        n = len(pitch_means)
        mean_pitch = sum(pitch_means) / n
        variance   = sum((p - mean_pitch) ** 2 for p in pitch_means) / n
        fingerprint["pitch_mean_hz"] = mean_pitch
        fingerprint["pitch_std_hz"]  = math.sqrt(variance)
        fingerprint["pitch_samples_count"] = n
    os.makedirs(os.path.dirname(FINGERPRINT_PATH) or ".", exist_ok=True)
    with open(FINGERPRINT_PATH, "w", encoding="utf-8") as f:
        json.dump(fingerprint, f)
    logger.info(
        "[IDENTITY] Voice fingerprint enrolled — %d samples (%d with usable pitch)",
        len(embeddings), len(pitch_means),
    )
    return fingerprint


def has_fingerprint() -> bool:
    return _load_fingerprint() is not None


def absorb_sample(file_path: str) -> bool:
    """Folds one more genuine, ALREADY-CONFIRMED-as-Joan utterance into the
    existing fingerprint via an incremental running mean — Joan's voice
    fingerprint keeps improving from real conversation instead of staying
    frozen at whatever the original enroll_speaker() call captured. Caller
    must only call this for a sample that already cleared the identity
    threshold (see core.social.SocialEngine._match_voice) — this function
    has no independent verification of its own, so feeding it an
    unconfirmed sample would let a false positive quietly drift the
    fingerprint toward someone else's voice.

    Same 'derive and discard' philosophy as finish_voice_enrollment(): only
    the updated averaged embedding is persisted, never the raw audio.
    No-op (returns False) if no fingerprint exists yet — absorption only
    ever refines an existing enrollment, it doesn't bootstrap one from
    scratch (that's enroll_speaker()'s job, which needs several samples at
    once specifically to average out a single recording's noise before
    anything is trusted at all). Never raises.

    Gated by 'voice_learning_enabled' — off means an accepted match still
    gets a normal reply, the fingerprint just stops being refined by new
    samples (useful for testing identification without letting a
    borderline sample quietly drift the enrolled profile). 'voice_trust_all'
    overrides this gate when on — its whole point is deliberately feeding
    bulk samples in, so it wouldn't make sense for the separate learning
    toggle to silently block that."""
    if not SPEAKER_VERIFICATION_ENABLED:
        return False
    if not _learning_enabled() and not _trust_all_enabled():
        return False
    fingerprint = _load_fingerprint()
    if fingerprint is None:
        return False
    try:
        emb = _embed_file(file_path)
        if emb is None:
            return False
        old_mean  = fingerprint["model_data"]
        old_count = int(fingerprint.get("samples_count", 1))
        if len(emb) != len(old_mean):
            logger.warning("absorb_sample: embedding dimension mismatch — skipping")
            return False
        new_count = old_count + 1
        new_mean  = [(old_mean[i] * old_count + emb[i]) / new_count for i in range(len(old_mean))]

        fingerprint["model_data"]    = new_mean
        fingerprint["samples_count"] = new_count
        fingerprint["last_absorbed_at"] = datetime.datetime.now().isoformat()

        # Pitch mean gets the same incremental-running-mean treatment as
        # the embedding above, keyed off its OWN sample count
        # (pitch_samples_count) since not every absorbed utterance
        # necessarily yields a usable F0 estimate (voice already handled
        # this asymmetry at enrollment — see enroll_speaker's own
        # comment). pitch_std_hz is deliberately left untouched here
        # (only enroll_speaker sets it) — updating a running std correctly
        # needs Welford's algorithm, not worth the complexity for a
        # tolerance floor that's already generous (_PITCH_STD_FLOOR_HZ).
        # A fingerprint enrolled before this feature (no pitch_mean_hz at
        # all) simply never gains one from absorption either — it stays
        # embedding-only until re-enrolled, same graceful-degrade
        # contract identify_speaker() already has.
        if fingerprint.get("pitch_mean_hz") is not None:
            pitch = _pitch_stats(file_path)
            if pitch is not None:
                old_pitch_mean  = fingerprint["pitch_mean_hz"]
                old_pitch_count = int(fingerprint.get("pitch_samples_count", 1))
                new_pitch_count = old_pitch_count + 1
                fingerprint["pitch_mean_hz"] = (
                    (old_pitch_mean * old_pitch_count + pitch["mean"]) / new_pitch_count
                )
                fingerprint["pitch_samples_count"] = new_pitch_count

        os.makedirs(os.path.dirname(FINGERPRINT_PATH) or ".", exist_ok=True)
        with open(FINGERPRINT_PATH, "w", encoding="utf-8") as f:
            json.dump(fingerprint, f)
        logger.info("[IDENTITY] Voice fingerprint refined — absorbed sample #%d", new_count)
        return True
    except Exception:
        logger.debug("absorb_sample failed (non-critical)", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Phase 4 — identification
# ---------------------------------------------------------------------------

def identify_speaker(file_path: str) -> float:
    """Confidence (0.0-1.0) that file_path is Joan's voice.

    Returns 0.0 immediately when SPEAKER_VERIFICATION_ENABLED is False, the
    file is missing/too short, or the model errors — never raises. Prefers
    the enrolled fingerprint (FINGERPRINT_PATH); when Joan hasn't enrolled
    yet, falls back to the legacy pairwise OWNER_VOICE_DIR comparison so
    identification degrades to 'no signal' rather than refusing to run at
    all (see the module docstring / CONFIDENCE_LOW — a 0.0 here plays into
    the same multi-factor blend core/commands.py uses, not an outright
    lockout). Returns 1.0 unconditionally when 'voice_trust_all' is on —
    see _trust_all_enabled()'s own docstring.
    """
    if not SPEAKER_VERIFICATION_ENABLED:
        return 0.0

    if _trust_all_enabled():
        return 1.0

    fingerprint = _load_fingerprint()
    if fingerprint is not None:
        emb = _embed_file(file_path)
        if emb is None:
            return 0.0
        embedding_confidence = _cosine_confidence(emb, fingerprint["model_data"])

        # Pitch fusion — see the module-level pitch-fingerprinting comment
        # for why this exists. Only applied when BOTH sides have a usable
        # pitch reading (this sample's F0 extracted successfully, and the
        # fingerprint was enrolled/absorbed with pitch data); otherwise
        # falls straight through to embedding-only, same as before this
        # feature existed — a fingerprint enrolled pre-pitch, or a sample
        # too short/noisy for a trustworthy F0 estimate, never lowers
        # confidence just because pitch was unavailable.
        pitch = _pitch_stats(file_path)
        pitch_confidence = _pitch_confidence(pitch["mean"], fingerprint) if pitch is not None else None
        if pitch_confidence is not None:
            return max(0.0, min(1.0,
                _VOICE_EMBEDDING_WEIGHT * embedding_confidence + _VOICE_PITCH_WEIGHT * pitch_confidence
            ))
        return embedding_confidence

    # ── Legacy fallback: no enrollment yet ───────────────────────────────
    if not os.path.exists(OWNER_VOICE_DIR):
        return 0.0
    try:
        verifier    = _get_verifier()
        wav_samples = [f for f in os.listdir(OWNER_VOICE_DIR) if f.endswith(".wav")]
    except Exception:
        return 0.0
    best = 0.0
    for sample_file in wav_samples:
        sample_path = os.path.join(OWNER_VOICE_DIR, sample_file)
        try:
            score, _prediction = verifier.verify_files(sample_path, file_path)
            best = max(best, max(0.0, min(1.0, float(score))))
        except Exception:
            logger.debug("identify_speaker: legacy compare against %s failed", sample_path, exc_info=True)
    return best


def verify_speaker(file_path: str) -> bool:
    """Backward-compatible boolean wrapper — True when identify_speaker()
    clears SCORE_THRESHOLD."""
    return identify_speaker(file_path) >= SCORE_THRESHOLD


# ---------------------------------------------------------------------------
# Self-voice fingerprinting — infrastructure for a future interrupt/barge-in
# feature (LIRA needs to know "that's just my own voice coming back through
# the mic" before the mic can safely stay live while she's talking). Not
# wired into core/listener.py yet: the mic is currently hard-blocked during
# her own TTS playback (core.listener.is_auto_muted/_auto_muted) — a much
# simpler and already-working way to avoid self-triggering, which stays in
# place. This is purely the "can she recognize her own synthesized voice"
# piece, built independently and ahead of the actual barge-in logic that
# will eventually consume it.
#
# Deliberately a SEPARATE profile store from FINGERPRINT_PATH/Joan's own
# fingerprint (below), one entry per TTS engine — Kokoro, XTTS, and macOS
# `say` are acoustically distinct voices (different engines/models, not
# variations of one voice), so conflating them into a single averaged
# fingerprint would blur all three rather than let is_own_voice() recognize
# whichever one is actually playing.
# ---------------------------------------------------------------------------
SELF_VOICE_FINGERPRINTS_PATH = "data/self_voice_fingerprints.json"
SELF_VOICE_ENGINES = ("kokoro", "xtts", "say")

# A few short, phonetically varied phrases (not just one) per engine, same
# "average out a single recording's noise" reasoning enroll_speaker() uses
# for real human enrollment — a synthesized voice is far more consistent
# sample-to-sample than a human one, but still varies some with content.
_SELF_VOICE_ENROLL_PHRASES = (
    "Hola, soy LIRA. Estoy lista para ayudarte.",
    "Sistemas en orden, todo funciona correctamente.",
    "¿Necesitas que revise algo más antes de continuar?",
    "Analizando la solicitud, dame un momento.",
    "Perfecto, ya quedó guardado en la memoria.",
    "Detecté un par de cosas que quizás te interesen.",
    "Entendido. Avísame si quieres que profundice en esto.",
)


def _load_self_voice_fingerprints() -> dict:
    try:
        with open(SELF_VOICE_FINGERPRINTS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {}


def _save_self_voice_fingerprints(data: dict) -> None:
    os.makedirs(os.path.dirname(SELF_VOICE_FINGERPRINTS_PATH) or ".", exist_ok=True)
    with open(SELF_VOICE_FINGERPRINTS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def enroll_self_voice(engine: str, wav_paths: list[str]) -> dict | None:
    """Builds (or replaces) one engine's self-voice fingerprint from a few
    synthesized WAV samples — same embedding-averaging + pitch-stats shape
    as enroll_speaker(), just keyed under `engine` in
    SELF_VOICE_FINGERPRINTS_PATH instead of Joan's own single-profile file.
    Returns the saved per-engine entry, or None if fewer than 2 samples
    embedded successfully."""
    if not SPEAKER_VERIFICATION_ENABLED:
        return None
    if engine not in SELF_VOICE_ENGINES:
        raise ValueError(f"unknown engine: {engine}")

    embeddings = []
    pitch_means = []
    for path in wav_paths:
        emb = _embed_file(path)
        if emb is not None:
            embeddings.append(emb)
        pitch = _pitch_stats(path)
        if pitch is not None:
            pitch_means.append(pitch["mean"])

    if len(embeddings) < 2:
        logger.warning(
            "enroll_self_voice(%s): only %d/%d samples embedded successfully — enrollment aborted",
            engine, len(embeddings), len(wav_paths),
        )
        return None

    dims = len(embeddings[0])
    mean_embedding = [sum(e[i] for e in embeddings) / len(embeddings) for i in range(dims)]

    entry = {
        "enrolled_at":   datetime.datetime.now().isoformat(),
        "samples_count": len(embeddings),
        "model_data":    mean_embedding,
    }
    if pitch_means:
        n = len(pitch_means)
        mean_pitch = sum(pitch_means) / n
        variance   = sum((p - mean_pitch) ** 2 for p in pitch_means) / n
        entry["pitch_mean_hz"] = mean_pitch
        entry["pitch_std_hz"]  = math.sqrt(variance)

    data = _load_self_voice_fingerprints()
    data[engine] = entry
    _save_self_voice_fingerprints(data)
    logger.info("[IDENTITY] Self-voice fingerprint enrolled — engine=%s, %d samples", engine, len(embeddings))
    return entry


def is_own_voice(file_path: str) -> float:
    """Confidence (0.0-1.0) that file_path is LIRA's OWN synthesized voice
    (any enrolled engine) rather than a real person talking — the mirror
    image of identify_speaker()'s 'is this Joan' question. Checks every
    enrolled engine and returns the best match (whichever engine is
    actually speaking, if any) — same cosine + pitch fusion as
    identify_speaker(), just against SELF_VOICE_FINGERPRINTS_PATH's
    per-engine entries instead of the single Joan fingerprint. Returns 0.0
    if verification is disabled, the file is invalid, or nothing has been
    enrolled yet (fails safe: an unrecognized sound is never mistaken for
    LIRA's own voice just because self-voice enrollment hasn't run).
    Never raises."""
    if not SPEAKER_VERIFICATION_ENABLED:
        return 0.0
    data = _load_self_voice_fingerprints()
    if not data:
        return 0.0
    emb = _embed_file(file_path)
    if emb is None:
        return 0.0
    pitch = _pitch_stats(file_path)

    best = 0.0
    for entry in data.values():
        embedding_confidence = _cosine_confidence(emb, entry["model_data"])
        pitch_confidence = _pitch_confidence(pitch["mean"], entry) if pitch is not None else None
        score = (
            _VOICE_EMBEDDING_WEIGHT * embedding_confidence + _VOICE_PITCH_WEIGHT * pitch_confidence
            if pitch_confidence is not None else embedding_confidence
        )
        best = max(best, score)
    return max(0.0, min(1.0, best))


def enroll_all_self_voices() -> dict:
    """Synthesizes _SELF_VOICE_ENROLL_PHRASES through every TTS engine
    (core.voice's Kokoro/say, core.tts_xtts's XTTS) and enrolls each as its
    own self-voice fingerprint — the one-call entry point for building/
    refreshing this whole feature (e.g. after a voice/engine setting
    changes). Best-effort per engine: one engine being unavailable (XTTS
    not installed, no GPU, whatever) doesn't block the other two. Returns
    {engine: bool} — whether each one enrolled successfully. Synchronous
    and slow (real model inference x3 engines x3 phrases) — call from a
    background thread or a one-off script, never from a request handler."""
    import tempfile
    import numpy as np

    results = {}
    for engine in SELF_VOICE_ENGINES:
        tmp_paths = []
        try:
            for phrase in _SELF_VOICE_ENROLL_PHRASES:
                pcm_bytes = None
                mono_float = None
                samplerate = None
                if engine == "kokoro":
                    from core import voice
                    pcm_bytes = voice.synthesize_pcm48(phrase, voice=voice.KOKORO_VOICE_LIRA)
                    samplerate = 48000
                elif engine == "say":
                    from core import voice
                    pcm_bytes = voice.synthesize_pcm48_say(phrase)
                    samplerate = 48000
                elif engine == "xtts":
                    from core import tts_xtts
                    mono_float = tts_xtts.synthesize_pcm(phrase, language="es")
                    samplerate = tts_xtts.XTTS_SAMPLE_RATE

                if pcm_bytes is not None:
                    pcm = np.frombuffer(pcm_bytes, dtype=np.int16).reshape(-1, 2)   # stereo, both channels identical
                    mono_float = (pcm[:, 0].astype(np.float32)) / 32767.0
                if mono_float is None or mono_float.size == 0:
                    continue

                fd, tmp_path = tempfile.mkstemp(suffix=f"_{engine}.wav")
                os.close(fd)
                sf.write(tmp_path, mono_float, samplerate, subtype="PCM_16")
                tmp_paths.append(tmp_path)
            entry = enroll_self_voice(engine, tmp_paths) if tmp_paths else None
            results[engine] = entry is not None
        except Exception:
            logger.warning("enroll_all_self_voices(%s) failed", engine, exc_info=True)
            results[engine] = False
        finally:
            for p in tmp_paths:
                try:
                    os.remove(p)
                except OSError:
                    pass
    logger.info("[IDENTITY] Self-voice enrollment complete — %s", results)
    return results


def check_interrupt_speaker(file_path: str) -> bool:
    """Interrupt feature, step 2 — the actual accept/reject decision for a
    candidate interruption core.listener has already ducked for and
    accumulated ~2s of audio on (see _INTERRUPT_CHECK_DURATION_SECS in that
    module). Two gates, in order:

      1. is_own_voice(file_path) >= INTERRUPT_OWN_VOICE_REJECT_THRESHOLD ->
         reject outright as self-echo that slipped past step 1's ducking.
      2. identify_speaker(file_path) >= INTERRUPT_CONFIDENCE_THRESHOLD ->
         accept as a genuine Joan interruption.

    Anything else rejects (the false-reject-over-false-accept design call —
    see INTERRUPT_CONFIDENCE_THRESHOLD's own comment). Meant to run on a
    background thread (ECAPA embedding isn't free) — never touches
    playback state itself, purely the decision; step 3 (not built yet)
    is what a caller does with a True result. Logs its own reasoning either
    way so this is observable before step 3 exists to act on it. Never
    raises — a failure of either underlying check reads as reject, same
    fail-safe direction as the threshold choice itself."""
    try:
        own_voice_score = is_own_voice(file_path)
        if own_voice_score >= INTERRUPT_OWN_VOICE_REJECT_THRESHOLD:
            logger.info("[IDENTITY] Interrupt REJECTED — reads as own voice (%.2f)", own_voice_score)
            return False
        confidence = identify_speaker(file_path)
        accepted = confidence >= INTERRUPT_CONFIDENCE_THRESHOLD
        logger.info(
            "[IDENTITY] Interrupt %s — speaker confidence=%.2f (own_voice=%.2f, threshold=%.2f)",
            "ACCEPTED" if accepted else "rejected", confidence, own_voice_score, INTERRUPT_CONFIDENCE_THRESHOLD,
        )
        return accepted
    except Exception:
        logger.warning("check_interrupt_speaker failed — rejecting (fail-safe)", exc_info=True)
        return False
