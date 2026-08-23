# ═══════════════════════════════════════════════════════════════════════════
# GROQ CIRCUIT BREAKER — failure-type-aware skip state for
# groq_client._groq_complete's model chain, replacing pure timeout-based
# retry with real failure memory.
#
# Before this: every failure (a permanently-retired model 404ing, a rate
# limit with a known retry-after time, a genuine one-off timeout) was
# treated identically — catch, log, advance to the next tier, remember
# nothing. groq_config.py's own history is the concrete evidence this cost
# real latency: llama-3.3-70b-versatile and llama-3.1-8b-instant both
# hard-404'd on EVERY call for over a day (2026-08-10 through 2026-08-19)
# before a human noticed the pattern in logs and manually pruned them from
# GROQ_MODEL_CHAIN — every single conversation reply during that window
# paid the failed-attempt cost for a model that was never coming back
# without a config change.
#
# Three failure kinds, classified from the real Groq SDK exception type
# (see groq_client._groq_stream_complete — the original exception object
# propagates all the way up, never wrapped into something generic):
#   - PERMANENT (groq.NotFoundError — "model does not exist or you do not
#     have access to it"): not coming back without a human editing config.
#     Skip for _PERMANENT_COOLDOWN_SECS rather than forever, so a model
#     that genuinely comes back (access re-granted, Groq un-retires it)
#     self-heals without needing a process restart — just on a slow
#     schedule, since a human noticing and fixing the chain is the more
#     likely path for this kind either way.
#   - RATE_LIMITED (groq.RateLimitError): Groq's own error message tells
#     you exactly how long to wait ("Please try again in 7m29.28s") — skip
#     for exactly that long instead of guessing or retrying immediately.
#   - TRANSIENT (everything else — timeout, empty response, connection
#     error): no proactive skip at all, same as today's behavior — a
#     one-off blip shouldn't cause extended avoidance of an otherwise-fine
#     model.
#
# In-memory only, per-process — same pattern groq_config._last_latency
# already uses. Resets on restart, which is fine: the actual pain case
# (a model dead for a day+) is a long-running-process problem this exists
# to solve, not something that needs to survive a restart.
# ═══════════════════════════════════════════════════════════════════════════
import re
import threading
import time

import groq

_PERMANENT_COOLDOWN_SECS = 3600.0   # 1h — see module comment on why not forever
_RATE_LIMIT_DEFAULT_SECS = 60.0     # fallback if the retry-after can't be parsed from the message
_RATE_LIMIT_MAX_SECS     = 3600.0   # cap — never skip a model for more than an hour off one parsed value

_RETRY_AFTER_RE = re.compile(r"try again in (?:(\d+)h)?(?:(\d+)m)?([\d.]+)s")

_lock = threading.Lock()
_blocked_until: dict[str, float] = {}   # model -> monotonic timestamp


def _parse_retry_after_secs(message: str) -> float | None:
    match = _RETRY_AFTER_RE.search(message)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    total = float(seconds or 0)
    if minutes:
        total += int(minutes) * 60
    if hours:
        total += int(hours) * 3600
    return total


def should_skip(model: str) -> bool:
    """Whether `model` is currently in a failure-cooldown and should be
    skipped without even attempting the call."""
    with _lock:
        until = _blocked_until.get(model)
    return until is not None and time.monotonic() < until


def record_failure(model: str, exc: Exception) -> None:
    """Classifies `exc` and sets a cooldown if warranted. Never raises —
    a classification bug here should never break the fallback chain
    itself, worst case it just falls back to today's behavior (no skip)."""
    try:
        if isinstance(exc, groq.NotFoundError):
            cooldown = _PERMANENT_COOLDOWN_SECS
        elif isinstance(exc, groq.RateLimitError):
            parsed = _parse_retry_after_secs(str(exc))
            cooldown = min(parsed, _RATE_LIMIT_MAX_SECS) if parsed is not None else _RATE_LIMIT_DEFAULT_SECS
        else:
            return   # transient — no proactive skip, same as today
        with _lock:
            _blocked_until[model] = time.monotonic() + cooldown
    except Exception:
        pass


def record_success(model: str) -> None:
    """Clears any cooldown — the model answered, whatever caused a past
    failure (if any) no longer applies."""
    with _lock:
        _blocked_until.pop(model, None)


def get_status() -> dict:
    """Snapshot for diagnostics (GET /api/info's Estado tab, same spirit as
    groq_config._last_latency) — model -> seconds remaining in cooldown,
    only for models currently blocked."""
    now = time.monotonic()
    with _lock:
        return {m: round(until - now, 1) for m, until in _blocked_until.items() if until > now}
