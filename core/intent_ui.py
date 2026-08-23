# ═══════════════════════════════════════════════════════════════════════════
# INTENT UI — listen-mode switch and floating-diamond move detection: local
# regex, no Groq call, no shared mutable state with anything else. Split out
# of core/intent.py (pure refactor, no behavior change).
# ═══════════════════════════════════════════════════════════════════════════
import re

# ---------------------------------------------------------------------------
# Listen-mode switch via voice
# Part 2: detect "modo conversación" / "modo normal" phrases so the user can
# switch listen modes hands-free.  Handled before the Groq pipeline so no API
# call is wasted on a simple mode toggle.
# ---------------------------------------------------------------------------

_MODE_SWITCH_PATTERNS = {
    # Activate conversation mode
    "conversation": re.compile(
        r"\b(modo\s+conversaci[oó]n|activa\s+(el\s+)?modo\s+conversaci[oó]n"
        r"|conversaci[oó]n|activa\s+conversaci[oó]n)\b",
        re.IGNORECASE,
    ),
    # Return to wake-word mode
    "wake_word": re.compile(
        r"\b(modo\s+normal|modo\s+est[aá]ndar|desactiva\s+(el\s+)?modo\s+conversaci[oó]n"
        r"|desactiva\s+conversaci[oó]n|modo\s+wake\s*word)\b",
        re.IGNORECASE,
    ),
}


def _detect_mode_switch(text: str) -> str | None:
    """Return 'conversation', 'wake_word', or None."""
    for mode_name, pattern in _MODE_SWITCH_PATTERNS.items():
        if pattern.search(text):
            return mode_name
    return None

# ---------------------------------------------------------------------------
# Floating diamond — voice/text move commands ('muévete', 'quítate de ahí',
# 've a la esquina', 'muévete a la derecha', ...). Detected the same
# local-regex-first way as the listen-mode switch just above (no Groq call
# wasted on a request this deterministic) — checked at the very top of
# _dispatch_command_impl, before personality switch, since it's a UI-only
# side effect that should never block or delay the rest of that turn's
# actual command handling.
#
# _DIAMOND_MOVE_TRIGGER_RE alone ('muévete', 'quítate de ahí', ...) with no
# direction word present maps to the 'away' region — "just get out of the
# way", not a specific corner; ui/index.html's frontend picks its own best
# low-density spot for that case, same algorithm as an idle re-home.
# ---------------------------------------------------------------------------

_DIAMOND_MOVE_TRIGGER_RE = re.compile(
    r"\b(mu[eé]vete|qu[ií]tate(?:\s+de\s+ah[ií])?|vete\s+de\s+ah[ií]|ap[aá]rtate|"
    r"hazte\s+a\s+un\s+lado|c[aá]mbiate\s+de\s+lugar|"
    r"ve\s+(?:a\s+(?:la\s+|el\s+)?|al\s+)(?:esquina|lado|centro|medio))\b",
    re.IGNORECASE,
)

# Order matters: corner (two-word) patterns are checked before the single-
# word 'top'/'bottom'/'left'/'right' patterns, so "arriba a la derecha"
# resolves to the corner 'top-right', not just 'top' (whichever pattern
# list position matched first would otherwise win arbitrarily on overlap).
_DIAMOND_DIRECTION_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("top-left",     re.compile(r"arriba\s+a\s+la\s+izquierda|esquina\s+superior\s+izquierda", re.IGNORECASE)),
    ("top-right",    re.compile(r"arriba\s+a\s+la\s+derecha|esquina\s+superior\s+derecha", re.IGNORECASE)),
    ("bottom-left",  re.compile(r"abajo\s+a\s+la\s+izquierda|esquina\s+inferior\s+izquierda", re.IGNORECASE)),
    ("bottom-right", re.compile(r"abajo\s+a\s+la\s+derecha|esquina\s+inferior\s+derecha", re.IGNORECASE)),
    ("top",          re.compile(r"\barriba\b", re.IGNORECASE)),
    ("bottom",       re.compile(r"\babajo\b", re.IGNORECASE)),
    ("left",         re.compile(r"\bizquierda\b", re.IGNORECASE)),
    ("right",        re.compile(r"\bderecha\b", re.IGNORECASE)),
    ("center",       re.compile(r"\bcentro\b|\bmedio\b", re.IGNORECASE)),
]


def _detect_diamond_move(text: str) -> str | None:
    """Returns a target region key for ui/index.html's DIAMOND_REGIONS table
    ('top-left'|'top-right'|'bottom-left'|'bottom-right'|'top'|'bottom'|
    'left'|'right'|'center'|'away'), or None if *text* isn't asking the
    floating diamond to move at all."""
    if not _DIAMOND_MOVE_TRIGGER_RE.search(text):
        return None
    for region, pattern in _DIAMOND_DIRECTION_PATTERNS:
        if pattern.search(text):
            return region
    return "away"
