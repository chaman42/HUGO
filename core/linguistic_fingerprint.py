# ═══════════════════════════════════════════════════════════════════════════
# LINGUISTIC FINGERPRINT — Phase 5 of the conversational intelligence system.
#
# A second, independent identity signal alongside core/speaker.py's voice
# embedding: HOW Joan talks (muletillas, sentence length, vocabulary),
# tracked in data/linguistic_fingerprint.json and updated only during sleep
# (see update_fingerprint(), called from core/sleep.py — zero Groq cost,
# pure local text statistics over this session's own history, no LLM call
# needed for what is just word counting).
#
# The whole point is robustness when voice alone is unreliable: a cold,
# fatigue, or a noisy mic can all drop core.speaker.identify_speaker()'s
# confidence even though it's genuinely Joan talking — score() below gives
# core/commands.py a second, voice-independent opinion to blend in (see
# that module's _identify_speaker_multi_factor / [IDENTITY] log line).
# ═══════════════════════════════════════════════════════════════════════════
import datetime
import json
import logging
import math
import re
import threading

logger = logging.getLogger(__name__)

FINGERPRINT_PATH = "data/linguistic_fingerprint.json"

# Below this many learned utterances the fingerprint has nothing reliable to
# compare against yet — score() returns a neutral 0.5 (see its docstring)
# rather than letting a near-empty fingerprint drag the combined confidence
# down for what is just a lack of data, not a mismatch.
_MIN_SAMPLES_FOR_SCORE = 8

# Fixed vocabulary tracked in the fingerprint, capped so the file never
# grows unbounded across months of sessions — same "rolling cap" convention
# as e.g. core.commands._MAX_PATTERN_TURNS.
_MAX_VOCAB_WORDS   = 300
_MAX_EXPRESSIONS   = 40

# A representative set of Spanish filler words / muletillas — not
# exhaustive, just enough common ones that any speaker's real usage
# frequency of them is a meaningful, hard-to-fake signal. Checked as
# substrings against the lowered transcript.
_MULETILLAS_ES = (
    "o sea", "vale", "bueno", "pues", "sabes", "tipo", "literal", "a ver",
    "digamos", "en plan", "es que", "la verdad", "de hecho", "osea",
    "venga", "mira", "oye", "claro", "eh", "ehh", "mmm",
)

_WORD_RE     = re.compile(r"[a-záéíóúñü]+", re.IGNORECASE)
_SENTENCE_RE = re.compile(r"[.!?¿¡]+")

_lock = threading.Lock()

_STOPWORDS_ES = frozenset({
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del",
    "a", "en", "y", "o", "que", "es", "por", "para", "con", "no", "se",
    "su", "sus", "lo", "le", "les", "me", "mi", "te", "tu", "al", "como",
    "más", "pero", "si", "ya", "yo", "esto", "eso", "esta", "este",
})


def _default_fingerprint() -> dict:
    return {
        "updated_at":          None,
        "sample_count":        0,
        "common_expressions":  {},   # muletilla -> count
        "vocabulary":          {},   # word -> count (stopwords excluded)
        "avg_sentence_length": 0.0,  # words per utterance
        "question_ratio":      0.0,
        "topic_patterns":      {},   # keyword -> count, reuses memory._keywords
    }


def _load() -> dict:
    try:
        with open(FINGERPRINT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            merged = _default_fingerprint()
            merged.update(data)
            return merged
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return _default_fingerprint()


def _save(data: dict) -> None:
    import os
    os.makedirs(os.path.dirname(FINGERPRINT_PATH) or ".", exist_ok=True)
    with open(FINGERPRINT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text or "")]


def _cap_dict(d: dict, max_size: int) -> dict:
    if len(d) <= max_size:
        return d
    return dict(sorted(d.items(), key=lambda kv: kv[1], reverse=True)[:max_size])


# ---------------------------------------------------------------------------
# Update — called from core/sleep.py's "Actualización de huella lingüística"
# sub-step, once per sleep session.
# ---------------------------------------------------------------------------

def update_fingerprint(user_utterances: list[str]) -> int:
    """Folds `user_utterances` (this session's raw user turns — see
    core/sleep.py for where they come from) into the stored fingerprint.
    Weighted merge, not overwrite: existing counts persist and grow, so the
    fingerprint reflects Joan's speech pattern across *all* sessions it has
    ever seen, not just the most recent one. Returns the number of
    utterances actually folded in (0 if the list was empty — sleep logs
    this as a no-op rather than a failure)."""
    utterances = [u.strip() for u in (user_utterances or []) if u and u.strip()]
    if not utterances:
        return 0

    with _lock:
        data = _load()

        total_words     = 0
        total_sentences = 0
        question_count  = 0

        for utt in utterances:
            words = _words(utt)
            total_words += len(words)
            for w in words:
                if w in _STOPWORDS_ES or len(w) < 3:
                    continue
                data["vocabulary"][w] = data["vocabulary"].get(w, 0) + 1

            lowered = utt.lower()
            for expr in _MULETILLAS_ES:
                if expr in lowered:
                    data["common_expressions"][expr] = data["common_expressions"].get(expr, 0) + 1

            n_sentences = max(1, len(_SENTENCE_RE.split(utt)) - 1) if _SENTENCE_RE.search(utt) else 1
            total_sentences += n_sentences
            if "?" in utt or "¿" in utt:
                question_count += 1

            try:
                from core import memory
                for kw in memory._keywords(utt):
                    data["topic_patterns"][kw] = data["topic_patterns"].get(kw, 0) + 1
            except Exception:
                pass

        prev_count = data["sample_count"]
        new_count  = prev_count + len(utterances)

        # Running weighted average for the two scalar stats — new batch
        # weighted by its own size against everything seen before.
        batch_avg_len = total_words / total_sentences if total_sentences else 0.0
        batch_q_ratio = question_count / len(utterances)
        if prev_count > 0:
            data["avg_sentence_length"] = (
                (data["avg_sentence_length"] * prev_count + batch_avg_len * len(utterances)) / new_count
            )
            data["question_ratio"] = (
                (data["question_ratio"] * prev_count + batch_q_ratio * len(utterances)) / new_count
            )
        else:
            data["avg_sentence_length"] = batch_avg_len
            data["question_ratio"]      = batch_q_ratio

        data["sample_count"]       = new_count
        data["vocabulary"]         = _cap_dict(data["vocabulary"], _MAX_VOCAB_WORDS)
        data["common_expressions"] = _cap_dict(data["common_expressions"], _MAX_EXPRESSIONS)
        data["topic_patterns"]     = _cap_dict(data["topic_patterns"], _MAX_VOCAB_WORDS)
        data["updated_at"]         = datetime.datetime.now().isoformat()

        _save(data)

    logger.info("[IDENTITY] Linguistic fingerprint updated — +%d utterances (total=%d)",
                len(utterances), new_count)
    return len(utterances)


# ---------------------------------------------------------------------------
# Score — called live, on every voice-gated turn, from core/commands.py.
# ---------------------------------------------------------------------------

def score(transcript: str) -> float:
    """Confidence (0.0-1.0) that `transcript` reads like Joan's established
    speech pattern. Returns a neutral 0.5 when the fingerprint doesn't have
    enough data yet (_MIN_SAMPLES_FOR_SCORE) — 'no signal' should never drag
    the multi-factor combined score down, only a real mismatch should."""
    transcript = (transcript or "").strip()
    if not transcript:
        return 0.5

    data = _load()
    if data["sample_count"] < _MIN_SAMPLES_FOR_SCORE:
        return 0.5

    words = _words(transcript)
    lowered = transcript.lower()

    # Vocabulary overlap — fraction of this utterance's content words that
    # appear anywhere in the learned vocabulary.
    content_words = [w for w in words if w not in _STOPWORDS_ES and len(w) >= 3]
    if content_words:
        known = sum(1 for w in content_words if w in data["vocabulary"])
        vocab_score = known / len(content_words)
    else:
        vocab_score = 0.5   # too short to judge on vocabulary alone

    # Expression usage — does this utterance use any muletilla, and if so,
    # is it one Joan actually uses? Silent on expressions is neutral (most
    # short utterances legitimately use none).
    used_exprs = [e for e in _MULETILLAS_ES if e in lowered]
    if used_exprs:
        known_exprs = sum(1 for e in used_exprs if data["common_expressions"].get(e, 0) > 0)
        expr_score = known_exprs / len(used_exprs)
    else:
        expr_score = 0.5

    # Sentence-length closeness — Gaussian falloff around the learned
    # average, sigma scaled to that average so short/long speakers alike get
    # a fair comparison.
    avg = data["avg_sentence_length"] or len(words) or 1
    sigma = max(3.0, avg * 0.6)
    length_score = math.exp(-((len(words) - avg) ** 2) / (2 * sigma ** 2))

    combined = 0.45 * vocab_score + 0.25 * expr_score + 0.30 * length_score
    return max(0.0, min(1.0, combined))


def sample_count() -> int:
    return _load().get("sample_count", 0)


def update_from_session() -> int:
    """'Actualización de huella lingüística' — the sleep sub-phase entry
    point (see core/sleep.py, called right after Phase 0 in both
    run_sleep_session and run_continuous_sleep, unconditionally and free —
    no Groq token budget involved). Pulls this session's raw user turns
    from core.session's in-memory history and folds them into the stored
    fingerprint. Skipped in test mode, same as every other memory-writing
    operation (core.memory._extract_and_save_memory,
    core.commands._record_turn_for_patterns) — a test conversation
    shouldn't leave a linguistic trace any more than a factual one. Never
    raises."""
    try:
        from core import memory
        if memory.is_feature_enabled("modo_test"):
            return 0
        from core import session as session_mod
        turns = [
            h["content"] for h in session_mod._get_history_snapshot()
            if h.get("role") == "user" and h.get("content")
        ]
        return update_fingerprint(turns)
    except Exception:
        logger.warning("Linguistic fingerprint update failed (non-critical)", exc_info=True)
        return 0
