# ═══════════════════════════════════════════════════════════════════════════
# INTENT ARMOR — voice control for the physical ArmorOS suit. "Conéctate al
# modelo 8" sets a session-scoped context (_connected_model) so later bare
# commands ("enciende el reactor", "apaga la luz") know which suit they're
# talking about without Joan having to name it every time — mirrors how
# core/intent_ui.py's mode-switch/diamond-move are detected (local regex, no
# Groq call, checked early in core/commands.py's dispatch pipeline).
#
# Only Modelo 8's reactor chip exists physically right now (see
# core/armor_light.py) — other suits/components (flaps, etc.) aren't wired
# up, so only 'model-8' is recognized. See the "ArmorOS Reactor Hub Plan"
# memory for the wider planned architecture this is one voice-facing piece of.
#
# _connected_model is a plain module-level slot, not persisted — same
# reasoning as core/intent.py's _pending_action: only one suit connection
# realistically matters at a time in a single-user voice assistant, and a
# stale connection has no business surviving a restart.
# ═══════════════════════════════════════════════════════════════════════════
import re

_ARMOR_CONNECT_RE = re.compile(
    r"\bcon[eé]ctate\s+(?:al?|con\s+el)\s+modelo\s+(?:8|ocho)\b",
    re.IGNORECASE,
)
_ARMOR_DISCONNECT_RE = re.compile(
    r"\bdesconn?[eé]ctate\b",
    re.IGNORECASE,
)

# Component word ('reactor'/'luz') + on/off verb. '... del modelo 8' is an
# explicit, directly-addressed command — always actionable regardless of
# connection state. The bare form (no suffix) only means something once
# _connected_model is set — see _detect_armor_light's docstring.
_ARMOR_LIGHT_ON_RE = re.compile(
    r"\b(?:enciende|prende|activa)\s+(?:el\s+reactor|la\s+luz)\b",
    re.IGNORECASE,
)
_ARMOR_LIGHT_OFF_RE = re.compile(
    r"\b(?:apaga|desactiva)\s+(?:el\s+reactor|la\s+luz)\b",
    re.IGNORECASE,
)
_ARMOR_LIGHT_BALIZA_RE = re.compile(
    r"\b(?:activa|pon(?:le)?|enciende)\s+(?:el\s+)?modo\s+baliza\b",
    re.IGNORECASE,
)
_MODEL8_ADDRESSED_RE = re.compile(r"\bdel\s+modelo\s+(?:8|ocho)\b", re.IGNORECASE)

_connected_model: str | None = None


def _detect_armor_connect(text: str) -> str | None:
    """Returns 'connect', 'disconnect', or None."""
    if _ARMOR_CONNECT_RE.search(text):
        return "connect"
    if _ARMOR_DISCONNECT_RE.search(text):
        return "disconnect"
    return None


def set_connected_model(model_id: str | None) -> None:
    global _connected_model
    _connected_model = model_id


def get_connected_model() -> str | None:
    return _connected_model


def _detect_armor_light(text: str) -> str | None:
    """Returns 'on', 'off', 'baliza', or None. Only matches the bare form
    ('enciende el reactor', with no '... del modelo 8' suffix) when a suit is
    already connected (get_connected_model() is set) — otherwise a stray
    'luz'/'reactor' mention in ordinary conversation would get hijacked into
    a hardware command. The explicitly-addressed form always matches, whether
    or not anything is currently connected."""
    addressed = bool(_MODEL8_ADDRESSED_RE.search(text))
    if not addressed and get_connected_model() != "model-8":
        return None
    if _ARMOR_LIGHT_ON_RE.search(text):
        return "on"
    if _ARMOR_LIGHT_OFF_RE.search(text):
        return "off"
    if _ARMOR_LIGHT_BALIZA_RE.search(text):
        return "baliza"
    return None
