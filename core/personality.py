# ═══════════════════════════════════════════════════════════════════════════
# PERSONALITY — active-personality state and switching. Character
# definitions and system-prompt assembly live in core/personalities/*
# (pure refactor, no behavior change) — this module aggregates PERSONALITIES
# from there and owns which one is currently active.
#
# core/commands.py imports this module at the top level; this module reaches
# back into core.server only via a function-local import inside
# _switch_personality to avoid a circular import.
# ═══════════════════════════════════════════════════════════════════════════
import logging
import re
import threading

from core.personalities.base import PERSONALITIES, _build_system_prompt

logger = logging.getLogger(__name__)

_personality      = "lira"   # the only personality (JARVIS/FRIDAY removed 2026-08-10)
_personality_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Wake-word-only acknowledgment — LIRA is the only personality now, so there's
# nothing left to SWITCH between (that machinery — _SWITCH_PATTERNS,
# _detect_personality_switch, _switch_personality — is gone), but a bare
# "lira" with no command content still needs a brief ready-ack instead of
# being forwarded to Groq as an empty query. Variant list kept in sync with
# listener.py's _LIRA_VARIANTS.
# ---------------------------------------------------------------------------
_WAKE_ONLY_RE = re.compile(
    r"^\s*(?:lira|lyra|leera|liera|liira|lirra|lila|lida)\s*$",
    re.IGNORECASE,
)
