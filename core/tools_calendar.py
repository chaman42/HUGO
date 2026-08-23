"""Calendar.app integration via AppleScript: read today's/this week's
events and create new events. Requires Automation access to Calendar
(System Settings -> Privacy & Security -> Automation)."""
import datetime
import logging

from core.tools_system import _run_applescript, _applescript_escape, OSASCRIPT_TIMEOUT

logger = logging.getLogger(__name__)

CALENDAR_READ_TIMEOUT = 25      # seconds — confirmed live: a `whose start date...`
                                 # filter scan across every calendar (get_today_events/
                                 # get_week_events) can genuinely take 10+ seconds on a
                                 # real Mac (Birthdays/Holidays calendars in particular
                                 # can hold many auto-generated recurring events) — this
                                 # is normal AppleScript+Calendar.app behavior, not a
                                 # hang, so it gets its own longer, dedicated timeout
                                 # rather than sharing the fast-call default above.

def _events_script(days_ahead: int) -> str:
    """AppleScript that collects every event starting in [today 00:00, today
    + days_ahead*24h) across every calendar, one per output line as
    'Title||YYYY-MM-DD HH:MM' — date/time built from individual AppleScript
    date components (year/month/day/hours/minutes), not `as string`, since
    that's locale-formatted and not reliably parseable back in Python."""
    return f'''
set outputList to {{}}
tell application "Calendar"
    set startOfDay to current date
    set hours of startOfDay to 0
    set minutes of startOfDay to 0
    set seconds of startOfDay to 0
    set endOfDay to startOfDay + ({days_ahead} * days)
    repeat with cal in calendars
        try
            set calEvents to (every event of cal whose start date ≥ startOfDay and start date < endOfDay)
            repeat with evt in calEvents
                set evtStart to start date of evt
                set y to (year of evtStart) as string
                set mo to text -2 thru -1 of ("0" & ((month of evtStart) as integer))
                set d to text -2 thru -1 of ("0" & (day of evtStart))
                set h to text -2 thru -1 of ("0" & (hours of evtStart))
                set mi to text -2 thru -1 of ("0" & (minutes of evtStart))
                set outputList to outputList & {{(summary of evt) & "||" & y & "-" & mo & "-" & d & " " & h & ":" & mi}}
            end repeat
        end try
    end repeat
end tell
set AppleScript's text item delimiters to linefeed
set outputString to outputList as string
set AppleScript's text item delimiters to ""
return outputString
'''


def _parse_calendar_events(raw: str) -> list[dict]:
    """Parse the 'Title||YYYY-MM-DD HH:MM' lines from _events_script() into
    {title, date, time} dicts. Skips any malformed line rather than
    raising — never lets one weird event break the whole list."""
    events = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or "||" not in line:
            continue
        title, _, when = line.partition("||")
        when = when.strip()
        date_part, _, time_part = when.partition(" ")
        if not date_part or not time_part:
            continue
        events.append({"title": title.strip(), "date": date_part, "time": time_part})
    return events


def get_today_events() -> list[dict]:
    """Today's Calendar.app events (all calendars) as
    [{title, date, time}, ...], sorted by start time. Empty list if there
    are none, Calendar access is denied, or anything else fails."""
    raw = _run_applescript(_events_script(1), timeout=CALENDAR_READ_TIMEOUT)
    if raw is None:
        return []
    events = _parse_calendar_events(raw)
    events.sort(key=lambda e: (e["date"], e["time"]))
    return events


def get_week_events() -> list[dict]:
    """Calendar.app events over the next 7 days (all calendars), sorted by
    date then time. Empty list if there are none or access fails."""
    raw = _run_applescript(_events_script(7), timeout=CALENDAR_READ_TIMEOUT)
    if raw is None:
        return []
    events = _parse_calendar_events(raw)
    events.sort(key=lambda e: (e["date"], e["time"]))
    return events


def create_event(title: str, date: str, time: str, duration: int = 60) -> bool:
    """Create a new event on the first Calendar.app calendar.
    `date` is 'YYYY-MM-DD', `time` is 'HH:MM' (24h), `duration` is minutes.
    Returns True on success, False on any failure (bad date/time, denied
    Automation permission, anything else) — never raises, never partially
    creates an event."""
    try:
        year, month, day = (int(p) for p in date.split("-"))
        hour, minute = (int(p) for p in time.split(":"))
        datetime.date(year, month, day)   # validates the calendar date is real
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("time out of range")
    except (ValueError, AttributeError) as exc:
        logger.debug("create_event: invalid date/time %r %r: %s", date, time, exc)
        return False

    safe_title = _applescript_escape(title or "Evento")
    duration = max(1, int(duration))

    # "set day to 1" before touching year/month is the standard AppleScript
    # safe-date-construction idiom — it avoids a transient invalid date
    # (e.g. currently the 31st, target month has 30 days) that would
    # otherwise silently roll over to the wrong day.
    script = f'''
tell application "Calendar"
    set targetCal to calendar 1
    set startDate to current date
    set day of startDate to 1
    set year of startDate to {year}
    set month of startDate to {month}
    set day of startDate to {day}
    set hours of startDate to {hour}
    set minutes of startDate to {minute}
    set seconds of startDate to 0
    set endDate to startDate + ({duration} * minutes)
    tell targetCal
        make new event with properties {{summary:"{safe_title}", start date:startDate, end date:endDate}}
    end tell
end tell
return "OK"
'''
    return _run_applescript(script, timeout=OSASCRIPT_TIMEOUT) == "OK"
