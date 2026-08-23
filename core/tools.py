"""
Live data tools for JarvisLite — public entry point, re-assembled from
focused submodules (core.tools_environment / core.tools_search /
core.tools_system / core.tools_calendar). Every existing `from core import
tools` / `import core.tools as tools` call site keeps working unchanged.

All functions fetch real-time data — NEVER use LLM training data or cached
memory as substitutes for time, date, location, or weather.
"""
import datetime as _dt
import logging
import threading
import time

from dotenv import load_dotenv

load_dotenv()   # so SERPER_API_KEY is available even if tools.py is used standalone

from core.tools_environment import (  # noqa: E402
    get_current_datetime_string,
    get_session_duration_string,
    get_location,
    get_weather,
    get_weather_string,
)
from core.tools_search import (  # noqa: E402
    evaluate_math,
    search_web,
    format_search_results,
)
from core.tools_system import (  # noqa: E402
    get_volume,
    set_volume,
    volume_up,
    volume_down,
    mute_system,
    unmute_system,
    resolve_app_name,
    open_app,
    _run_applescript,
)
from core.tools_calendar import (  # noqa: E402
    get_today_events,
    get_week_events,
    create_event,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Calendar + Apple Health awareness — volatile, session-scoped context
# blocks refreshed every 30 minutes in the background so HUGO has Joan's
# agenda and health data present without ever blocking dispatch on a slow
# AppleScript/Shortcuts call. Mirrors core.tools_environment's location/
# weather cache-plus-background-refresh pattern; kept here rather than a
# new tools_*.py submodule since this integration is scoped to
# core/tools.py + core/commands.py only. get_today_events()/get_week_events()
# (core.tools_calendar) and create_event() are untouched — this only adds a
# cache layer + prompt-string formatting on top of the existing reads.
# ---------------------------------------------------------------------------

_CONTEXT_CACHE_TTL = 1800   # 30 minutes

_DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

_calendar_cache: dict = {"today": [], "upcoming": [], "timestamp": 0.0}
_calendar_lock         = threading.Lock()


def _day_label(date_str: str) -> str:
    """'mañana' for tomorrow, otherwise the Spanish weekday name, for a
    'YYYY-MM-DD' date string. Falls back to the raw string if unparsable."""
    try:
        d = _dt.date.fromisoformat(date_str)
    except ValueError:
        return date_str
    if (d - _dt.date.today()).days == 1:
        return "mañana"
    return _DIAS_ES[d.weekday()]


def _format_events(events: list[dict], with_day_label: bool) -> str:
    if not events:
        return ""
    parts = []
    for e in events:
        prefix = f"{_day_label(e['date'])} " if with_day_label else ""
        parts.append(f"{prefix}{e['time']} {e['title']}")
    return ", ".join(parts)


def _refresh_calendar_cache() -> None:
    """Unconditionally re-fetch today's + next-7-days events. Called by the
    background thread; get_today_events()/get_week_events() already fail
    soft (empty list) on any Calendar access problem, so this never raises."""
    today_events = get_today_events()
    week_events  = get_week_events()
    today_str = _dt.date.today().isoformat()
    # get_week_events() already includes today — split it off so
    # "PRÓXIMOS DÍAS" only covers tomorrow onward, not a duplicate of today.
    upcoming = [e for e in week_events if e["date"] != today_str]
    with _calendar_lock:
        _calendar_cache["today"]     = today_events
        _calendar_cache["upcoming"]  = upcoming
        _calendar_cache["timestamp"] = time.monotonic()
    logger.debug(
        "Calendar cache refreshed: %d today, %d upcoming",
        len(today_events), len(upcoming),
    )


def get_calendar_context_string() -> str:
    """'AGENDA HOY: ... . PRÓXIMOS DÍAS: ...' from the 30-minute cache —
    never blocks dispatch on a live AppleScript call (Calendar reads can
    take 10+ seconds, see core.tools_calendar.CALENDAR_READ_TIMEOUT); the
    background refresh thread below keeps this warm from startup onward."""
    with _calendar_lock:
        today    = _calendar_cache["today"]
        upcoming = _calendar_cache["upcoming"]

    today_str    = _format_events(today, with_day_label=False) or "sin eventos programados"
    upcoming_str = _format_events(upcoming, with_day_label=True) or "nada más programado por ahora"
    return f"AGENDA HOY: {today_str}. PRÓXIMOS DÍAS: {upcoming_str}"


# ---------------------------------------------------------------------------
# Apple Health — macOS has no native Health app/AppleScript dictionary, so
# this reads through a user-created Shortcut instead (Shortcuts app,
# macOS Monterey+), invoked via the same osascript path calendar/volume
# control already use (_run_applescript, imported from core.tools_system
# above — no new dependency). Fails silently (empty context string, one-
# time log note) if the Shortcut doesn't exist yet or Health access hasn't
# been granted — HUGO just works without health awareness in that case.
#
# ONE-TIME SETUP required in the macOS Shortcuts app (cannot be done from
# Python — Shortcuts must be authored by hand): create a shortcut named
# exactly HEALTH_SHORTCUT_NAME below that:
#   1. Finds today's Health samples for steps, active energy, and heart rate
#      (Find Health Samples where Start Date is Today)
#   2. Gets last night's sleep analysis (Find Health Samples, category
#      Sleep, Start Date is Yesterday)
#   3. Finds today's workouts (Find Workouts where Start Date is Today)
#   4. Combines all of the above into one final "Text" action formatted
#      EXACTLY as pipe-delimited fields, in this order:
#        steps||sleep_hours||active_calories||heart_rate||workout_summary
#      leaving a field empty (but keeping its "||") for anything unavailable,
#      e.g. "8432||7.2||420||||" if heart rate/workouts aren't available.
#   5. That Text action's output IS the shortcut's result — no further
#      actions after it.
# Requires the Mac's Shortcuts app to be signed into the same iCloud
# account as the iPhone/Watch that actually records the Health data.
# ---------------------------------------------------------------------------

HEALTH_SHORTCUT_NAME = "HUGO Salud"
HEALTH_TIMEOUT        = 15   # seconds — a Health-samples Shortcut run is quick once synced

_health_cache: dict = {"data": None, "timestamp": 0.0}
_health_lock         = threading.Lock()
_health_setup_warned = False


def _fetch_health_data() -> dict | None:
    """Run the HEALTH_SHORTCUT_NAME Shortcut via osascript and parse its
    pipe-delimited output. Returns None on any failure (Shortcut missing,
    Health access not granted, timeout, malformed output) — never raises."""
    global _health_setup_warned
    script = f'tell application "Shortcuts Events" to run shortcut "{HEALTH_SHORTCUT_NAME}"'
    raw = _run_applescript(script, timeout=HEALTH_TIMEOUT)
    if raw is None:
        if not _health_setup_warned:
            _health_setup_warned = True
            logger.info(
                "Apple Health data unavailable — create a Shortcut named '%s' in the "
                "macOS Shortcuts app to enable it (steps/sleep/active calories/heart "
                "rate/workouts; see core/tools.py's HEALTH_SHORTCUT_NAME comment block "
                "for the exact output format expected). HUGO works normally without it.",
                HEALTH_SHORTCUT_NAME,
            )
        return None

    parts = (raw.strip().split("||") + [""] * 5)[:5]
    steps_s, sleep_s, cal_s, hr_s, workouts_s = (p.strip() for p in parts)

    def _num(s: str, cast):
        try:
            return cast(s) if s else None
        except ValueError:
            return None

    return {
        "steps":       _num(steps_s, int),
        "sleep_hours": _num(sleep_s, float),
        "active_cal":  _num(cal_s, int),
        "heart_rate":  _num(hr_s, int),
        "workouts":    workouts_s or None,
    }


def _refresh_health_cache() -> None:
    """Called by the background thread — swallows its own failures via
    _fetch_health_data()'s None return, never raises."""
    data = _fetch_health_data()
    with _health_lock:
        _health_cache["data"]      = data
        _health_cache["timestamp"] = time.monotonic()


def get_health_context_string() -> str:
    """'SALUD HOY: ...' listing only the fields actually available, or ''
    if Health data has never been fetched successfully (permissions not
    granted / Shortcut missing) — omitted entirely from the prompt in that
    case, same graceful-degradation as calendar/weather."""
    with _health_lock:
        data = _health_cache["data"]
    if not data:
        return ""

    fragments = []
    if data.get("sleep_hours") is not None:
        fragments.append(f"{data['sleep_hours']}h de sueño anoche")
    if data.get("steps") is not None:
        fragments.append(f"{data['steps']} pasos")
    if data.get("active_cal") is not None:
        fragments.append(f"{data['active_cal']} kcal activas")
    if data.get("heart_rate") is not None:
        fragments.append(f"FC {data['heart_rate']}bpm")
    if data.get("workouts"):
        fragments.append(f"entrenó: {data['workouts']}")

    if not fragments:
        return ""
    return "SALUD HOY: " + ", ".join(fragments)


def _background_context_refresh_loop() -> None:
    """Warm calendar+health caches immediately at startup, then every
    _CONTEXT_CACHE_TTL seconds (30 min) — daemon thread, never blocks the
    dispatch pipeline."""
    while True:
        try:
            _refresh_calendar_cache()
        except Exception:
            logger.debug("Calendar background refresh failed", exc_info=True)
        try:
            _refresh_health_cache()
        except Exception:
            logger.debug("Health background refresh failed", exc_info=True)
        time.sleep(_CONTEXT_CACHE_TTL)


# Start immediately at module import (tools.py is imported at process
# startup by core.commands) — caches are warm within seconds of launch,
# then refreshed every 30 minutes for the rest of the session.
threading.Thread(
    target=_background_context_refresh_loop,
    daemon=True,
    name="tools-calendar-health-refresh",
).start()
