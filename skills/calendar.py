"""Calendar skill — thin LiraSkill wrapper over core.tools_calendar
(Calendar.app reads/writes via AppleScript). No calendar logic lives here.

`context` may carry {"action": "today" | "week" | "create", ...}; for
"create" it must also carry {"title", "date", "time", "duration"} —
same shape core.intent._parse_event_details already produces for the
calendar_write intent (see core/intent.py)."""
from skills import LiraSkill
from core import tools_calendar


class CalendarSkill(LiraSkill):
    name = "calendar"
    description = "Lee y crea eventos en Calendar.app."
    triggers = ["qué tengo hoy", "mi agenda", "crea un evento", "agenda una reunión"]

    def execute(self, query: str, context: dict) -> str:
        context = context or {}
        action = context.get("action", "today")

        if action == "create":
            ok = tools_calendar.create_event(
                title=context.get("title", "Evento"),
                date=context["date"],
                time=context["time"],
                duration=context.get("duration", 60),
            )
            return "Evento creado." if ok else "No pude crear el evento."

        events = tools_calendar.get_week_events() if action == "week" else tools_calendar.get_today_events()
        if not events:
            return "No hay eventos programados."
        return ", ".join(f"{e['date']} {e['time']} {e['title']}" for e in events)
