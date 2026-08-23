# ═══════════════════════════════════════════════════════════════════════════
# WAKE WORD — variant list, fuzzy matching, and Vosk-result scanning for
# LIRA wake-word detection (JARVIS/FRIDAY removed 2026-08-10 — LIRA is the
# only personality now). Pure, stateless functions — no shared mutable
# state with core/listener.py's audio loop. Split out of core/listener.py
# (pure refactor, no behavior change).
# ═══════════════════════════════════════════════════════════════════════════
import json
import re

_LIRA_VARIANTS: frozenset[str] = frozenset({
    "lira", "lyra", "leera", "liera", "liira", "lirra",
    # Common Spanish misreadings
    "lila", "lida",
})

# Text-normalization pass (2026-08-20) — _WAKE_ONLY_RE in core/personality.py
# only cleans up a mis-heard variant when it's the ENTIRE utterance ("Lyra"
# alone); a real command like "Lyra, enciende el reactor" keeps the
# misspelling in the dispatched text verbatim, which then gets logged,
# shown in the chat transcript, and sent to Groq as-is. Deliberately only
# the curated exact variants here, NOT _match_wake_word's broader
# edit-distance-1 fuzzy fallback — that fallback is fine for a binary
# wake/no-wake decision on a single token, but running it across every
# word in a full sentence risks corrupting unrelated real words that
# happen to sit at distance 1 from "lira".
_VARIANT_NORMALIZE_PATTERN = re.compile(
    r"\b(?:" + "|".join(sorted(_LIRA_VARIANTS - {"lira"}, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def normalize_wake_word_text(text: str) -> str:
    """Replaces any curated mis-heard variant of 'Lira' (lyra, leera, ...)
    anywhere in `text` with 'Lira' itself. Called once per dispatched
    utterance (core/listener.py's _do_dispatch/_do_conv_dispatch) so every
    downstream consumer — chat transcript, history, the text Groq actually
    sees — gets the corrected spelling, not just the narrow bare-wake-word
    case _WAKE_ONLY_RE already handled."""
    if not text:
        return text
    return _VARIANT_NORMALIZE_PATTERN.sub("Lira", text)

# Common, everyday Spanish words that happen to sit at edit distance 1 from
# "lira" (e.g. "mira" = "look", imperative of mirar — said constantly in
# normal speech). Without this guard the generic fuzzy fallback below treats
# any of these as the wake word, causing false wakes. Curated variants above
# are exempt (they're deliberate misreadings of LIRA itself, not real words).
_FUZZY_BLOCKLIST: frozenset[str] = frozenset({
    "mira", "gira", "tira", "vira", "pira", "dira", "lima", "lisa",
})

# Per-word confidence thresholds
_CONF_THRESHOLD    = 0.35   # Spanish recognizer (per-word)
_EN_CONF_THRESHOLD = 0.85   # English recognizer (stricter)

# Overall transcript confidence threshold — skip low-confidence segments
_TRANSCRIPT_CONF_THRESHOLD = 0.5

# Grammar for the English wake-word recognizer
_EN_WAKE_GRAMMAR = json.dumps(["lira", "lyra", "[unk]"])


def _edit_distance(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for ca in a:
        curr = [prev[0] + 1]
        for j, cb in enumerate(b):
            curr.append(min(
                prev[j + 1] + 1,
                curr[j]     + 1,
                prev[j]     + (ca != cb),
            ))
        prev = curr
    return prev[-1]


def _match_wake_word(token: str) -> str | None:
    t = token.lower().strip(".,!?¿¡")
    if not t:
        return None

    # Exact / curated variant match first
    if t in _LIRA_VARIANTS:
        return "lira"

    # Fuzzy fallback — edit distance <= 1, standalone guard: token length
    # must be within 1 char of the target. Real Spanish words in
    # _FUZZY_BLOCKLIST are excluded even at distance 1 (see comment there).
    if len(t) >= 3 and t not in _FUZZY_BLOCKLIST:
        if abs(len(t) - len("lira")) <= 1 and _edit_distance(t, "lira") <= 1:
            return "lira"
    return None


def _scan_result(result_json: dict, conf_threshold: float = _CONF_THRESHOLD) -> str | None:
    """Return 'lira' or None from a Vosk result dict."""
    full_text = result_json.get("text", "").lower()

    found_lira = False
    for w in result_json.get("result", []):
        word = w.get("word", "")
        conf = w.get("conf", 1.0)
        if conf < conf_threshold:
            continue
        if _match_wake_word(word) == "lira":
            found_lira = True

    if found_lira:
        return "lira"

    # Pass 2: fuzzy text scan (no per-word confidence available)
    for token in full_text.split():
        if _match_wake_word(token) == "lira":
            return "lira"

    return None


def _overall_confidence(result_json: dict) -> float:
    """Return the average word confidence for a Vosk result, or 1.0 if no words."""
    words = result_json.get("result", [])
    if not words:
        return 1.0
    return sum(w.get("conf", 1.0) for w in words) / len(words)
