# ═══════════════════════════════════════════════════════════════════════════
# REMINDERS — lightweight regex-based reminder detection/storage/delivery
# (data/reminders.json). Split out of core/commands.py (pure refactor, no
# behavior change).
#
# A lightweight regex-based (not LLM-based — reminders are meant to stay
# simple, and this runs on every turn) detector, _maybe_store_reminder(),
# runs after every normal turn and looks for "te aviso cuando... / te
# recuerdo que..." — the ASSISTANT's own spoken promise, made in a Groq-
# generated reply. Joan's own direct "recuérdame que..." request no longer
# goes through this post-hoc path — it's now core.intent's explicit
# reminder_create intent (Level 1 of the three-level action philosophy,
# see core/commands.py's module comment), which stores it immediately AND
# gives Joan a spoken confirmation, something this silent post-hoc
# mechanism never did. Keeping both would double-store every "recuérdame
# que..." (once via the new intent, once via this matching the same
# transcript after the fact).
# Each reminder is either:
#   - time-based ("en 10 minutos" / "en 2 horas" / "en media hora") — a
#     simple relative offset from now, delivered by the same
#     background_loops._proactive_loop tick that handles time-aware
#     comments, or
#   - session-based ("la próxima vez que hables conmigo", the default when
#     no duration is mentioned) — delivered at the very start of the NEXT
#     dispatch_command() call, since "next session" just means "next time
#     we talk", not something a 30-min timer thread should have to guess at.
# Reminders bypass the proactive rate caps (max 1/hour, max 3/session) —
# those caps exist to keep spontaneous chatter rare; a reminder is an
# explicit commitment Joan (or the assistant) already made, not spontaneous
# chatter. They still always honor the processing/speaking guard
# (background_loops._proactive_blocked) — never interrupt, an overdue
# reminder just waits quietly for the next tick.
# ═══════════════════════════════════════════════════════════════════════════
import datetime
import json
import logging
import os
import re
import threading
import uuid

from core import memory
from core import background_loops

logger = logging.getLogger(__name__)

REMINDERS_PATH = "data/reminders.json"

_reminders_lock = threading.Lock()

_REMINDER_HALF_HOUR_RE = re.compile(r"en\s+media\s+hora", re.IGNORECASE)
_REMINDER_HOURS_RE     = re.compile(r"en\s+(\d+)\s*horas?", re.IGNORECASE)
_REMINDER_MINUTES_RE   = re.compile(r"en\s+(\d+)\s*minutos?", re.IGNORECASE)

_ASSISTANT_PROMISE_RE = re.compile(
    r"(?:te\s+aviso\s+cuando|te\s+recuerdo\s+cuando|te\s+recuerdo\s+que|te\s+lo\s+recuerdo)\s+(.+)",
    re.IGNORECASE,
)


def _load_reminders() -> list[dict]:
    try:
        with open(REMINDERS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _save_reminders(reminders: list[dict]) -> None:
    os.makedirs(os.path.dirname(REMINDERS_PATH) or ".", exist_ok=True)
    with open(REMINDERS_PATH, "w", encoding="utf-8") as f:
        json.dump(reminders, f, ensure_ascii=False, indent=2)


def _add_reminder(text: str, personality: str, trigger_type: str, trigger_at: str | None) -> None:
    with _reminders_lock:
        reminders = _load_reminders()
        reminders.append({
            "id":           uuid.uuid4().hex[:12],
            "text":         text,
            "personality":  personality,
            "trigger_type": trigger_type,
            "trigger_at":   trigger_at,
            "created":      memory._now_iso(),
            "delivered":    False,
        })
        _save_reminders(reminders)
    logger.debug("Reminder stored (%s): %s", trigger_type, text)


def _parse_relative_minutes(text: str) -> int | None:
    """Best-effort parse of a simple Spanish relative-time phrase into a
    minute count — 'en X minutos', 'en X horas', 'en media hora'. Returns
    None if no duration is present, so the caller falls back to a
    session-based reminder instead ('no complex scheduling' requirement)."""
    if _REMINDER_HALF_HOUR_RE.search(text):
        return 30
    m = _REMINDER_HOURS_RE.search(text)
    if m:
        return int(m.group(1)) * 60
    m = _REMINDER_MINUTES_RE.search(text)
    if m:
        return int(m.group(1))
    return None


def _strip_duration_phrase(text: str) -> str:
    """Remove a 'en X minutos/horas'/'en media hora' phrase (and a leading
    'que' left dangling after it) from reminder text — needed because the
    duration can land anywhere before the actual reminder content, e.g.
    'recuérdame EN 10 MINUTOS que saque la pizza' → 'saque la pizza'."""
    for pattern in (_REMINDER_HALF_HOUR_RE, _REMINDER_HOURS_RE, _REMINDER_MINUTES_RE):
        text = pattern.sub("", text)
    text = re.sub(r"^\s*que\s+", "", text.strip(), flags=re.IGNORECASE)
    return text.strip(" .,!?¡¿")


def _maybe_store_reminder(transcript: str, reply: str, personality: str) -> None:
    """Detect the ASSISTANT making its own promise this turn ('te aviso
    cuando...', 'te recuerdo que...') and store it. Joan's own direct
    request is handled earlier, in the intent pipeline (see module comment
    above) — this only ever looks at `reply`, never `transcript`, to avoid
    double-storing the same request. Regex-based on purpose, not an LLM
    call: reminders are meant to stay simple, and this runs on every turn."""
    match = _ASSISTANT_PROMISE_RE.search(reply)
    if not match:
        return
    source = reply

    text = match.group(1).strip(" .,!?¡¿")
    if not text:
        return

    minutes = _parse_relative_minutes(source)
    if minutes is not None:
        text = _strip_duration_phrase(text)
        if not text:
            return
        trigger_at = (
            datetime.datetime.now() + datetime.timedelta(minutes=minutes)
        ).isoformat(timespec="seconds")
        _add_reminder(text, personality, "time", trigger_at)
    else:
        _add_reminder(text, personality, "session", None)


def _deliver_session_reminders(personality: str) -> None:
    """Called at the very top of every dispatch_command() call — 'next time
    we talk' means the next real interaction, not a timer. Delivers at most
    one per call (oldest first) so a backlog doesn't turn into a monologue;
    any others surface on subsequent turns."""
    with _reminders_lock:
        reminders = _load_reminders()
        pending = [r for r in reminders if r.get("trigger_type") == "session" and not r.get("delivered")]
        if not pending:
            return
        due = pending[0]
        due["delivered"] = True
        _save_reminders(reminders)

    # Phrased naturally (see feedback_no_hardcoded_replies memory) — this
    # used to be a fixed f-string spoken verbatim every time.
    from core import response as response_mod
    spoken = response_mod._format_response(f"Por cierto — {due['text']}.", personality=personality)
    background_loops._speak_unprompted(personality, spoken)


def _reminder_is_due(trigger_at: str, now_dt: datetime.datetime) -> bool:
    try:
        return now_dt >= datetime.datetime.fromisoformat(trigger_at)
    except ValueError:
        return False


def _deliver_time_reminders() -> None:
    """Called on every background_loops._proactive_loop tick. Delivers at
    most the single earliest overdue time-based reminder, and only if
    nothing would be interrupted — reminders don't count against the
    hourly/per-session proactive caps (see module comment above), but they
    still never interrupt; an overdue reminder just waits for the next
    tick."""
    if background_loops._proactive_blocked():
        return

    now_dt = datetime.datetime.now()
    with _reminders_lock:
        reminders = _load_reminders()
        overdue = [
            r for r in reminders
            if r.get("trigger_type") == "time" and not r.get("delivered")
            and r.get("trigger_at") and _reminder_is_due(r["trigger_at"], now_dt)
        ]
        if not overdue:
            return
        overdue.sort(key=lambda r: r["trigger_at"])
        earliest = overdue[0]
        earliest["delivered"] = True
        _save_reminders(reminders)

    from core import personality as personality_mod
    with personality_mod._personality_lock:
        current_p = personality_mod._personality
    # Phrased naturally (see feedback_no_hardcoded_replies memory) — this
    # used to be a fixed f-string spoken verbatim every time.
    from core import response as response_mod
    spoken = response_mod._format_response(f"Por cierto — {earliest['text']}.", personality=current_p)
    background_loops._speak_unprompted(current_p, spoken)
