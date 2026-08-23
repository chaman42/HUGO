# ═══════════════════════════════════════════════════════════════════════════
# INTENT — local-regex intent classification (no Groq call needed): time/
# date, volume control, opening apps, Calendar.app read/write, reminders,
# and the Level 3 "implied action" triggers for the three-level action
# philosophy (see core/commands.py's own module comment for the full
# picture). Kept together in this one file because _detect_intent owns
# _pending_action, a module-level mutable slot that core/actions.py and
# core/commands.py read and write via qualified `intent_mod._pending_action`
# access — splitting _detect_intent out to a different file than that slot
# would desync the two (a `from x import name` binding elsewhere would
# freeze a stale copy, never seeing writes made through this module's own
# `global`).
#
# Listen-mode switch / floating-diamond-move detection (stateless) now live
# in core/intent_ui.py; web-search confidence/query-cleaning, tone
# detection, implicit-context inference and weather-icon mapping (stateless)
# now live in core/intent_context.py — both re-exported below so every
# existing `intent_mod.X`/`intent.X` call site keeps working unchanged.
# (Pure refactor, no behavior change.)
#
# core/commands.py imports this module at the top level; this module reaches
# back into core.commands only via a function-local `import core.commands as
# commands` inside _recurring_topic (see core/intent_context.py — same
# lazy-import pattern used throughout this codebase to avoid circular
# imports).
# ═══════════════════════════════════════════════════════════════════════════
import re
import datetime
import time

# Module object (not `from x import name`) — jarvis.py's watchdog
# hot-reloads core/memory.py independently of this file (see its
# _MODULE_MAP), and a name-bound import here would keep pointing at the
# pre-reload function forever. Same reasoning as core/commands.py's own
# import block.
from core import memory

from core.intent_ui import _detect_mode_switch, _detect_diamond_move
from core.intent_context import (
    _web_search_confidence,
    _clean_search_query,
    _detect_tone,
    _recurring_topic,
    _infer_implicit_context,
    _WEATHER_QUERY_RE,
    _weather_icon_category,
    # Needed directly (not just re-exported) — _detect_intent's web_search
    # branch below checks these two at module scope.
    _EXPLICIT_SEARCH_REQUEST_RE,
    _CURRENT_INFO_KEYWORD_RE,
)

# ---------------------------------------------------------------------------
# Intent detection — local regex (no Groq API call)
#
# The original implementation called Groq for every command just to classify
# one of three intents: get_time, get_date, or unknown. All three are trivially
# detectable with Spanish regex, matching what tools.py already does for math.
# This eliminates a full round-trip API call on every command.
# ---------------------------------------------------------------------------

_INTENT_TIME_RE = re.compile(
    r"\b(qu[eé]\s+hora|la\s+hora|dime\s+la\s+hora|cu[aá]nto[s]?\s+son|son\s+las|hora\s+es|hora\s+son)\b",
    re.IGNORECASE,
)
_INTENT_DATE_RE = re.compile(
    r"\b(qu[eé]\s+d[ií]a|fecha\s+de\s+hoy|d[ií]a\s+de\s+hoy|cu[aá]ndo\s+es|qu[eé]\s+fecha|hoy\s+es|qu[eé]\s+d[ií]a\s+de\s+la\s+semana)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# System control intents — volume, opening apps, Calendar.app (core.tools).
# All patterns tested directly against the exact example phrases from spec
# ("sube el volumen", "pon el volumen al 50", "silencia el Mac", "abre
# Spotify", "abre el calendario", "abre notas", "qué tengo hoy", "tengo
# algo mañana?", "crea un evento para el viernes a las 5") plus common
# variants — none of these groups cross-match each other's example phrases.
# ---------------------------------------------------------------------------

_INTENT_VOLUME_MUTE_RE = re.compile(
    r"\b(silencia(?:me)?|mutea(?:me)?)\s+(?:el\s+)?(mac|sonido|audio|volumen)\b",
    re.IGNORECASE,
)
_INTENT_VOLUME_UNMUTE_RE = re.compile(
    r"\b(quita(?:me)?\s+el\s+silencio|activa(?:me)?\s+el\s+sonido|desmutea(?:me)?|unmute)\b",
    re.IGNORECASE,
)
# Checked before UP/DOWN below — an explicit numeric target ("sube el
# volumen a 80") should win over a generic +/-10 nudge even though it also
# starts with sube/baja.
_INTENT_VOLUME_SET_RE = re.compile(
    r"\b(?:pon(?:me)?|ajusta|cambia|sube|baja)?\s*(?:el\s+)?volumen\s+(?:al?|a)\s+(\d{1,3})\b",
    re.IGNORECASE,
)
_INTENT_VOLUME_UP_RE = re.compile(
    r"\b(sube(?:me)?|subir|aumenta(?:me)?)\s+(?:el\s+)?volumen\b|\bvolumen\s+(?:m[aá]s\s+)?alto\b",
    re.IGNORECASE,
)
_INTENT_VOLUME_DOWN_RE = re.compile(
    r"\b(baja(?:me)?|bajar|disminuye(?:me)?)\s+(?:el\s+)?volumen\b|\bvolumen\s+(?:m[aá]s\s+)?bajo\b",
    re.IGNORECASE,
)
_INTENT_VOLUME_GET_RE = re.compile(
    r"\b(qu[eé]\s+volumen|cu[aá]l\s+es\s+el\s+volumen|a\s+cu[aá]nto\s+est[aá]\s+el\s+volumen)\b",
    re.IGNORECASE,
)

# Open apps — 'abre/abrir/inicia/lanza X' with a leading article stripped;
# whatever's left is handed to tools.resolve_app_name() for the actual
# name-to-app mapping (see core/tools.py's _APP_NAME_MAP). Deliberately
# broad (matches "abre X" for any X) rather than trying to enumerate every
# possible app name in the regex itself.
_INTENT_OPEN_APP_RE = re.compile(
    r"\b(?:abre|[aá]bre(?:me)?|abrir|inicia|lanza)\s+(?:el\s+|la\s+|los\s+|las\s+)?(.+)",
    re.IGNORECASE,
)

# Investigations — 'investiga X' / 'quiero saber sobre X' / 'analiza X en
# profundidad' start a background investigation (core/investigations.py),
# advanced during sleep (core/sleep_phases_incubation.py). Checked below,
# ahead of the web_search fallback at the bottom of this function — the
# negative lookahead on 'investiga' excludes 'investiga en internet ...',
# which is _EXPLICIT_SEARCH_REQUEST_RE's own territory (an immediate,
# one-shot search, not a standing investigation).
_INTENT_INVESTIGATE_RE = re.compile(
    r"\binvestiga(?:me)?\s+(?!en\s+internet\b)(?:sobre\s+|acerca\s+de\s+)?(.+)",
    re.IGNORECASE,
)
_INTENT_INVESTIGATE_WANT_RE = re.compile(
    r"\bquiero\s+saber\s+sobre\s+(.+)",
    re.IGNORECASE,
)
_INTENT_INVESTIGATE_DEEP_RE = re.compile(
    r"\banaliza(?:me)?\s+(.+?)\s+en\s+profundidad\b",
    re.IGNORECASE,
)
# 'haz/crea/realiza/inicia una investigación (sobre|acerca de|de) X' —
# same trigger, natural alternate phrasing to 'investiga X' above (seen in
# real usage — see core.actions._execute_start_investigation's own
# docstring for the estudio_updated fix this phrasing gap sat next to).
_INTENT_INVESTIGATE_MAKE_RE = re.compile(
    r"\b(?:haz(?:me)?|crea(?:me)?|realiza(?:me)?|inicia(?:me)?)\s+una?\s+investigaci[oó]n\s+"
    r"(?:sobre\s+|acerca\s+de\s+|de\s+)?(.+)",
    re.IGNORECASE,
)

# Code Engine — 'crea/construye un módulo de X' / 'actualiza/arregla el
# módulo X' / 'revisa el código del módulo X'. Direct order, Level 1 like
# start_investigation/task above — but unlike those, this one triggers a
# tool that writes files, runs shell commands, and can deploy (see
# core.code_engine_dispatch, which core/commands.py routes these to),
# so the patterns require the explicit word 'módulo'/'skill', not a bare
# 'crea X' that a generic request could accidentally match. Checked
# BEFORE _INTENT_TASK_CREATE_RE below so 'crea un módulo de X' can never
# be swallowed by the generic 'crea una tarea' task-creation pattern.
_INTENT_CODE_CREATE_RE = re.compile(
    r"\b(?:crea(?:me)?|constru(?:ye|ime))\s+(?:un\s+|una\s+)?(?:m[oó]dulo|skill)\s+"
    r"(?:de\s+|para\s+)?(.+)",
    re.IGNORECASE,
)
_INTENT_CODE_UPDATE_RE = re.compile(
    r"\b(?:actualiza(?:me)?|arregla(?:me)?|corrige(?:me)?)\s+(?:el\s+)?(?:m[oó]dulo|skill)\s+"
    r"(?:de\s+)?(.+)",
    re.IGNORECASE,
)
_INTENT_CODE_REVIEW_RE = re.compile(
    r"\brevisa(?:me)?\s+(?:el\s+c[oó]digo\s+(?:del?\s+|de\s+)?|el\s+)?(?:m[oó]dulo|skill)s?"
    r"(?:\s+(?:de\s+)?(.+))?",
    re.IGNORECASE,
)

# Tasks — 'crea una tarea (para|de|sobre) X' / 'empieza una tarea de X' /
# 'quiero que trabajes en X' — starts a persistent, step-tracked task
# (core/task_engine.py). core.commands.create_task_from_goal breaks the
# goal into concrete steps via one Groq call (task_engine itself never
# calls an LLM — see its own module docstring), then advances one step per
# sleep cycle thereafter (TaskEngine.advance_during_sleep). Same Level 1
# treatment as start_investigation just above — direct order, executes
# immediately, no propose/confirm round trip.
_INTENT_TASK_CREATE_RE = re.compile(
    r"\b(?:crea(?:me)?|abre(?:me)?|empieza(?:me)?|inicia(?:me)?)\s+una\s+tarea\s+"
    r"(?:para\s+|de\s+|sobre\s+)?(.+)|"
    r"\bquiero\s+que\s+trabajes\s+en\s+(.+)",
    re.IGNORECASE,
)

# Summary/schema generation (ESTUDIO → RESÚMENES/ESQUEMAS) — routed to
# core.commands.generate_summary()/generate_schema(), both of which make an
# actual Groq call to produce the structured content, so these are handled
# as their own dispatch branches in core/commands.py rather than through
# core/actions.py's deterministic _NO_GROQ_INTENTS templates.
#
# Anchored to the start of the (post-wake-word) transcript — trigger words
# like 'estructura'/'resume' are common enough Spanish words that an
# unanchored match would false-positive on sentences that merely CONTAIN
# them ("investiga la estructura de las armaduras" is start_investigation's
# territory, not this). Checked after start_investigation above for the
# same reason, belt-and-suspenders.
_INTENT_SUMMARY_RE = re.compile(
    r"^\s*(?:(?:hazme|haz|crea(?:me)?)\s+un\s+resumen\s+(?:de|sobre)|resume|sintetiza|"
    r"qu[eé]\s+puntos\s+clave\s+tiene)\s*(.*)",
    re.IGNORECASE,
)
_INTENT_SCHEMA_MAPA_RE = re.compile(
    r"^\s*mapa\s+conceptual\s+(?:de|sobre)\s*(.*)",
    re.IGNORECASE,
)
_INTENT_SCHEMA_ESTRUCTURA_RE = re.compile(
    r"^\s*estructura\s*(.*)",
    re.IGNORECASE,
)
_INTENT_SCHEMA_OUTLINE_RE = re.compile(
    r"^\s*(?:(?:hazme|haz|crea(?:me)?)\s+un\s+esquema\s+(?:de|sobre)|organiza\s+esto)\s*(.*)",
    re.IGNORECASE,
)

# Calendar — write (create) checked before read, though in practice their
# trigger words never overlap. Level 1 of the three-level action philosophy
# (see core/commands.py's own module comment) — an explicit, direct order
# with a trigger verb ("crea/pon/agenda un evento...") executes immediately
# once parsed (core.actions._execute_calendar_write), no confirmation round
# trip, as long as date+time both parse; missing detail still asks to
# repeat rather than guessing (that's not what Level 1 covers).
_INTENT_CALENDAR_WRITE_RE = re.compile(
    r"\b(?:crea(?:me)?|agenda(?:me)?|a[ñn]ade(?:me)?|programa(?:me)?|pon(?:me)?)\s+"
    r"(?:un\s+|una\s+)?(?:evento|reuni[oó]n|cita)\b",
    re.IGNORECASE,
)
_INTENT_CALENDAR_TODAY_RE = re.compile(
    r"\b(qu[eé]\s+tengo\s+hoy|qu[eé]\s+eventos\s+tengo\s+hoy|mi\s+agenda\s+de\s+hoy|"
    r"tengo\s+algo\s+hoy|hay\s+algo\s+en\s+(?:mi\s+)?(?:calendario|agenda)\s+hoy)\b",
    re.IGNORECASE,
)
# "mañana" (tomorrow) has no dedicated tools function per spec (only
# get_today_events/get_week_events) — routed to the week range, which
# already covers tomorrow; Groq picks out the relevant day when phrasing
# the answer (see calendar_read's _format_response step).
_INTENT_CALENDAR_WEEK_RE = re.compile(
    r"\b(qu[eé]\s+tengo\s+(?:esta\s+semana|est[aá]\s+semana)|tengo\s+algo\s+(?:esta\s+semana|ma[ñn]ana)|"
    r"eventos\s+de\s+la\s+semana|mi\s+agenda\s+de\s+la\s+semana|qu[eé]\s+tengo\s+programado|"
    r"tengo\s+algo\s+pasado\s+ma[ñn]ana)\b",
    re.IGNORECASE,
)

# Reminder — explicit direct order ('recuérdame que...', 'crea un
# recordatorio para...'). Level 1: executes immediately, no round trip (see
# core.actions._execute_reminder_create). Deliberately distinct from
# core.reminders' own _USER_REMINDER_RE, which used to ALSO match
# 'recuérdame que...' post-hoc on every turn — that path now only handles
# the assistant's own spoken promises ('te aviso cuando...'), so the same
# phrase is never stored twice.
_INTENT_REMINDER_CREATE_RE = re.compile(
    r"\b(?:recu[ée]rdame\s+(?:que\s+)?|"
    r"crea(?:me)?\s+un\s+recordatorio\s+(?:para|de)\s+|"
    r"ponme\s+un\s+recordatorio\s+(?:para|de)\s+|"
    r"a[ñn]ade(?:me)?\s+un\s+recordatorio\s+(?:para|de)\s+)"
    r"(.+)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Level 3 — implied actions: the user mentions something in normal
# conversation that IMPLIES an action without explicitly requesting it (see
# core/commands.py's module comment for the full three-level philosophy).
# Each of these, once matched, PROPOSES the action (core.actions'
# *_propose handlers, which stash it in _pending_action below rather than
# doing anything yet) instead of executing directly like their Level 1
# counterparts above. Deliberately narrow/conservative patterns — false
# positives here mean LIRA interrupting a normal conversation with an
# unwanted proposal, which is worse than occasionally missing one.
# ---------------------------------------------------------------------------

# 'no me olvides que...' / 'tengo que acordarme de...' — note this is
# DIFFERENT from _INTENT_REMINDER_CREATE_RE above ('recuérdame que...' is a
# direct order to LIRA; these two are the user thinking out loud, which is
# exactly what Level 3 covers).
_INTENT_REMINDER_IMPLIED_RE = re.compile(
    r"\bno\s+me\s+olvides\s+(?:de\s+)?que\s+(.+)|\btengo\s+que\s+acordarme\s+de\s+(.+)",
    re.IGNORECASE,
)

# 'tengo que ir a X mañana a las Y' — only proposed if a date actually
# parses out of the sentence (see _detect_intent below); otherwise it's too
# vague to prepare anything concrete and LIRA should say nothing rather
# than guess. 'a(?:l)?' covers both 'ir a la playa' and the contracted
# 'ir al dentista' (a + el -> al).
_INTENT_CALENDAR_IMPLIED_GO_RE = re.compile(r"\btengo\s+que\s+ir\s+a(?:l)?\s+(.+)", re.IGNORECASE)
# Trims the captured place name at the first date/time phrase that follows
# it — 'al dentista mañana a las 4' -> 'al dentista'.
_IMPLIED_GO_DATE_CUT_RE = re.compile(
    r"\bel\s+\w+\b|\bma[ñn]ana\b|\bpasado\s+ma[ñn]ana\b|\bhoy\b|\ba\s+las\s+\d",
    re.IGNORECASE,
)

# 'quedamos el viernes' / 'quedamos con X el viernes' and 'reunión el
# lunes' / 'reunión con X el lunes'. Skips past-tense mentions ('tuvimos
# una reunión el lunes pasado') via _IMPLIED_PAST_TENSE_RE below — those
# describe something that already happened, not something to schedule.
_INTENT_CALENDAR_IMPLIED_MEET_RE = re.compile(
    r"\bquedamos\b(?:\s+con\s+(.+?))?(?=\s+el\b|\s+ma[ñn]ana\b|\s+hoy\b|\s*$)",
    re.IGNORECASE,
)
_INTENT_CALENDAR_IMPLIED_REUNION_RE = re.compile(
    r"\breuni[oó]n\b(?:\s+con\s+(.+?))?(?=\s+el\b|\s+ma[ñn]ana\b|\s+hoy\b|\s*$)",
    re.IGNORECASE,
)
_IMPLIED_PAST_TENSE_RE = re.compile(
    r"\b(?:tuve|tuvimos|fue|hubo|hab[ií]a)\s+(?:una\s+|un\s+)?(?:reuni[oó]n|cita)\b|"
    r"\bquedamos\b[^.!?]*\bpasad[oa]\b",
    re.IGNORECASE,
)

# 'quiero poner música' / 'necesito escuchar música' -> implies opening
# Spotify; 'quiero/necesito ver el calendario' -> implies opening Calendar.
# app_key is handed straight to core.tools.resolve_app_name (see
# _APP_NAME_MAP in core/tools_system.py), matching how the explicit
# open_app intent already resolves names.
_INTENT_APP_OPEN_IMPLIED_MUSIC_RE = re.compile(
    r"\b(?:quiero|necesito|voy\s+a)\s+(?:poner|escuchar)\s+m[uú]sica\b",
    re.IGNORECASE,
)
_INTENT_APP_OPEN_IMPLIED_CALENDAR_RE = re.compile(
    r"\b(?:quiero|necesito)\s+ver\s+(?:el\s+|mi\s+)?(?:calendario|agenda)\b",
    re.IGNORECASE,
)

# Spanish weekday/month names → indices, reused by both the date parser
# below and memory._MONTHS_ES/_DAYS_ES already defined earlier in this file.
_WEEKDAYS_ES_IDX: dict[str, int] = {
    "lunes": 0, "martes": 1, "miércoles": 2, "miercoles": 2, "jueves": 3,
    "viernes": 4, "sábado": 5, "sabado": 5, "domingo": 6,
}
_MONTHS_ES_IDX: dict[str, int] = {name: i + 1 for i, name in enumerate(memory._MONTHS_ES)}


def _parse_event_date(text: str) -> datetime.date | None:
    """Best-effort Spanish date extraction for calendar_write: 'hoy',
    'mañana', 'pasado mañana', a bare weekday name (today counts as a
    match if today IS that weekday, otherwise the next upcoming
    occurrence), or 'el DD de MES'. Returns None if nothing recognizable
    is found — a regex heuristic, not a full NLP date parser (same level
    of sophistication as tools.py's own _MATH_WORD_SUBS)."""
    t = text.lower()
    today = datetime.date.today()
    if re.search(r"\bpasado\s+ma[ñn]ana\b", t):
        return today + datetime.timedelta(days=2)
    if re.search(r"\bma[ñn]ana\b", t):
        return today + datetime.timedelta(days=1)
    if re.search(r"\bhoy\b", t):
        return today
    m = re.search(r"\bel\s+(\d{1,2})\s+de\s+(\w+)\b", t)
    if m:
        month = _MONTHS_ES_IDX.get(m.group(2))
        if month:
            day = int(m.group(1))
            try:
                candidate = datetime.date(today.year, month, day)
                if candidate < today:
                    candidate = datetime.date(today.year + 1, month, day)
                return candidate
            except ValueError:
                pass   # e.g. "31 de febrero" — falls through to None
    for name, idx in _WEEKDAYS_ES_IDX.items():
        if re.search(rf"\b{name}\b", t):
            delta = (idx - today.weekday()) % 7
            return today + datetime.timedelta(days=delta)
    return None


def _parse_event_time(text: str) -> str | None:
    """Best-effort time extraction: 'a las 5', 'a las 17:30', optionally
    'de la mañana/tarde/noche' to disambiguate. Returns 'HH:MM' (24h) or
    None if no time phrase is found. A bare small hour with no AM/PM
    marker ('a las 5') defaults to PM — matches the spec's own example
    ('a las 5' for a same-day-ish event almost always means 5pm) — while
    hours ≥7 are left as literal 24h without a marker, since those are
    common as either a morning or a 24h-clock reading and guessing wrong
    either way is no better than the other."""
    t = text.lower()
    m = re.search(r"\ba\s+las\s+(\d{1,2})(?::(\d{2}))?\b", t)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    if hour > 23 or minute > 59:
        return None
    if re.search(r"\bde\s+la\s+tarde\b", t) and hour < 12:
        hour += 12
    elif re.search(r"\bde\s+la\s+noche\b", t) and hour < 12:
        hour += 12
    elif re.search(r"\bde\s+la\s+ma[ñn]ana\b", t) and hour == 12:
        hour = 0
    elif not re.search(r"\bde\s+la\s+(ma[ñn]ana|tarde|noche)\b", t) and 1 <= hour <= 6:
        hour += 12
    return f"{hour:02d}:{minute:02d}"


def _parse_event_details(transcript: str) -> dict:
    """Extracts {title, date, time, duration} from a calendar_write
    transcript. title falls back to a generic 'Evento' if nothing
    meaningful remains after stripping the trigger phrase, date phrase,
    and time phrase — matches the spec's own example ('crea un evento
    para el viernes a las 5' has no explicit title at all)."""
    date     = _parse_event_date(transcript)
    time_str = _parse_event_time(transcript)

    remainder = _INTENT_CALENDAR_WRITE_RE.sub("", transcript, count=1)
    remainder = re.sub(r"\bpara\s+el\b|\bpara\b", "", remainder, flags=re.IGNORECASE)
    remainder = re.sub(r"\bpasado\s+ma[ñn]ana\b|\bma[ñn]ana\b|\bhoy\b", "", remainder, flags=re.IGNORECASE)
    remainder = re.sub(r"\bel\s+\d{1,2}\s+de\s+\w+\b", "", remainder, flags=re.IGNORECASE)
    for name in _WEEKDAYS_ES_IDX:
        remainder = re.sub(rf"\b(?:el\s+)?{name}\b", "", remainder, flags=re.IGNORECASE)
    remainder = re.sub(r"\ba\s+las\s+\d{1,2}(?::\d{2})?\b", "", remainder, flags=re.IGNORECASE)
    remainder = re.sub(r"\bde\s+la\s+(?:ma[ñn]ana|tarde|noche)\b", "", remainder, flags=re.IGNORECASE)
    remainder = re.sub(r"[¿?¡!.,]+", " ", remainder)
    remainder = re.sub(r"\s+", " ", remainder).strip()

    return {
        "title":    remainder if len(remainder) > 2 else "Evento",
        "date":     date,
        "time":     time_str,
        "duration": 60,
    }

# ═══════════════════════════════════════════════════════════════════════════
# PENDING ACTION — the "load but don't fire" backbone for every Level 3
# (implied) action across core/commands.py + core/actions.py: calendar
# events, reminders, Estudio saves, app opens (see each module's own
# *_propose / _execute_pending_confirm handlers). One single module-level
# slot, not persisted — only one proposal can realistically be outstanding
# at a time in a single-user voice assistant, and a stale/unconfirmed one
# has no business surviving a restart. Generalizes what used to be a
# calendar-only `_pending_calendar_event` + `calendar_confirm` pair (same
# idea, one slot per action kind) into a single {"kind": ..., "data": ...}
# shape every proposer can use.
#
# "Drops it silently after next message" (the spec's own wording) is
# implemented literally: the message immediately following a proposal gets
# exactly one look — if it's not an explicit yes/no, the proposal is
# cleared right there (this same call), and that message is still handled
# as itself below (a normal question shouldn't get eaten just because a
# proposal happened to be pending). _PENDING_ACTION_TTL is a wall-clock
# safety net on top of that, in case nothing at all is said for a while —
# a much-later unrelated "sí" should never resurrect a forgotten proposal.
# ═══════════════════════════════════════════════════════════════════════════
_pending_action: dict | None = None
_PENDING_ACTION_TTL = 120  # seconds

_AFFIRMATIVE_RE = re.compile(
    r"^\s*(s[ií]|confirmo|claro|correcto|vale|dale|ok(?:ay)?|exacto|as[ií]\s+es)\b",
    re.IGNORECASE,
)
_NEGATIVE_RE = re.compile(
    r"^\s*(no|cancela(?:lo)?|olv[ií]dalo|mejor\s+no|para|espera|ahora\s+no)\b",
    re.IGNORECASE,
)

# A leading vocative naming LIRA ('lira, hazme un resumen de X') is
# completely natural — typed chat has no wake-word boundary to strip it at,
# and even voice conversation mode often has the user address her by name
# mid-sentence. Every START-anchored (^) pattern below (_INTENT_SUMMARY_RE,
# _INTENT_SCHEMA_*_RE) would otherwise silently miss on 'lira hazme un
# resumen de X' — it doesn't start with 'hazme' once you count the name —
# and fall through to a normal conversational Groq reply that has no idea a
# summary/schema/investigation was ever supposed to happen, yet cheerfully
# claims 'Hecho, guardado en Estudio' anyway (see core.personalities.base's
# anti-hallucination line for the other half of guarding against that).
# Stripped once, up front, so every check below — including the
# pending-action affirmative/negative check just above — sees the same
# clean transcript.
_LEADING_VOCATIVE_RE = re.compile(r"^\s*lira\s*[,;:]?\s+", re.IGNORECASE)


def _detect_intent(transcript: str) -> dict:
    """Classify transcript intent using local regex — no API call needed."""
    global _pending_action

    transcript = _LEADING_VOCATIVE_RE.sub("", transcript, count=1)

    # A pending Level-3 proposal takes priority over every other pattern
    # below — a bare "sí"/"no" wouldn't match anything else anyway, but
    # this must be checked FIRST so it wins even if a reply happens to also
    # look like some other intent. `raw_transcript` rides along so
    # core.actions._execute_pending_confirm's reminder branch can pull a
    # timing phrase ('en una hora') straight out of the confirming message
    # itself, not just a bare yes/no.
    if _pending_action is not None:
        if time.monotonic() - _pending_action["at"] > _PENDING_ACTION_TTL:
            _pending_action = None
        elif _AFFIRMATIVE_RE.search(transcript):
            return {"intent": "pending_confirm", "parameters": {"confirmed": True, "raw_transcript": transcript}}
        elif _NEGATIVE_RE.search(transcript):
            return {"intent": "pending_confirm", "parameters": {"confirmed": False, "raw_transcript": transcript}}
        else:
            # This is the one message the proposal got — it didn't address
            # it, so it's gone now. Still falls through to normal detection
            # below: a genuinely new, unrelated message is handled as
            # itself, not silently swallowed just because something was
            # pending.
            _pending_action = None

    if _INTENT_TIME_RE.search(transcript):
        return {"intent": "get_time", "parameters": {}}
    if _INTENT_DATE_RE.search(transcript):
        return {"intent": "get_date", "parameters": {}}

    if _INTENT_VOLUME_MUTE_RE.search(transcript):
        return {"intent": "volume_control", "parameters": {"action": "mute"}}
    if _INTENT_VOLUME_UNMUTE_RE.search(transcript):
        return {"intent": "volume_control", "parameters": {"action": "unmute"}}
    m = _INTENT_VOLUME_SET_RE.search(transcript)
    if m:
        return {"intent": "volume_control", "parameters": {"action": "set", "level": int(m.group(1))}}
    if _INTENT_VOLUME_UP_RE.search(transcript):
        return {"intent": "volume_control", "parameters": {"action": "up", "amount": 10}}
    if _INTENT_VOLUME_DOWN_RE.search(transcript):
        return {"intent": "volume_control", "parameters": {"action": "down", "amount": 10}}
    if _INTENT_VOLUME_GET_RE.search(transcript):
        return {"intent": "volume_control", "parameters": {"action": "get"}}

    m = _INTENT_OPEN_APP_RE.search(transcript)
    if m:
        return {"intent": "open_app", "parameters": {"name": m.group(1).strip()}}

    if _INTENT_CALENDAR_WRITE_RE.search(transcript):
        return {"intent": "calendar_write", "parameters": _parse_event_details(transcript)}
    if _INTENT_CALENDAR_TODAY_RE.search(transcript):
        return {"intent": "calendar_read", "parameters": {"range": "today"}}
    if _INTENT_CALENDAR_WEEK_RE.search(transcript):
        return {"intent": "calendar_read", "parameters": {"range": "week"}}

    m = _INTENT_REMINDER_CREATE_RE.search(transcript)
    if m:
        return {"intent": "reminder_create", "parameters": {"text": m.group(1).strip(" ¿?¡!.")}}

    m = (
        _INTENT_INVESTIGATE_DEEP_RE.search(transcript)
        or _INTENT_INVESTIGATE_WANT_RE.search(transcript)
        or _INTENT_INVESTIGATE_RE.search(transcript)
        or _INTENT_INVESTIGATE_MAKE_RE.search(transcript)
    )
    if m:
        return {"intent": "start_investigation", "parameters": {"topic": m.group(1).strip(" ¿?¡!.")}}

    m = _INTENT_CODE_REVIEW_RE.search(transcript)
    if m:
        topic = (m.group(1) or "").strip(" ¿?¡!.")
        return {"intent": "code_engine_review", "parameters": {"topic": topic}}
    m = _INTENT_CODE_CREATE_RE.search(transcript)
    if m:
        topic = m.group(1).strip(" ¿?¡!.")
        if topic:
            return {"intent": "code_engine_task", "parameters": {"action": "create", "topic": topic}}
    m = _INTENT_CODE_UPDATE_RE.search(transcript)
    if m:
        topic = m.group(1).strip(" ¿?¡!.")
        if topic:
            return {"intent": "code_engine_task", "parameters": {"action": "update", "topic": topic}}

    m = _INTENT_TASK_CREATE_RE.search(transcript)
    if m:
        goal = (m.group(1) or m.group(2) or "").strip(" ¿?¡!.")
        if goal:
            return {"intent": "create_task", "parameters": {"goal": goal}}

    m = _INTENT_SUMMARY_RE.search(transcript)
    if m:
        return {"intent": "generate_summary", "parameters": {"topic": m.group(1).strip(" ¿?¡!.")}}

    m = _INTENT_SCHEMA_MAPA_RE.search(transcript)
    if m:
        return {"intent": "generate_schema", "parameters": {"topic": m.group(1).strip(" ¿?¡!."), "schema_type": "mapa conceptual"}}
    m = _INTENT_SCHEMA_ESTRUCTURA_RE.search(transcript)
    if m:
        return {"intent": "generate_schema", "parameters": {"topic": m.group(1).strip(" ¿?¡!."), "schema_type": "estructura"}}
    m = _INTENT_SCHEMA_OUTLINE_RE.search(transcript)
    if m:
        return {"intent": "generate_schema", "parameters": {"topic": m.group(1).strip(" ¿?¡!."), "schema_type": "outline"}}

    # ── Level 3 — implied actions (see the module comment above these
    # patterns) — checked last, after every explicit intent above, so an
    # implied mention never overrides a more specific direct command. Still
    # ahead of web_search/unknown: an implied action, once detected with
    # enough to actually prepare, should propose itself rather than fall
    # through to a normal conversational reply.
    m = _INTENT_REMINDER_IMPLIED_RE.search(transcript)
    if m:
        text = (m.group(1) or m.group(2) or "").strip(" ¿?¡!.")
        if text:
            return {"intent": "reminder_propose", "parameters": {"text": text}}

    m = _INTENT_CALENDAR_IMPLIED_GO_RE.search(transcript)
    if m:
        date = _parse_event_date(transcript)
        if date is not None:
            place = _IMPLIED_GO_DATE_CUT_RE.split(m.group(1), maxsplit=1)[0].strip(" ,.¿?¡!")
            title = f"Ir a {place}" if place else "Salida"
            return {"intent": "calendar_propose", "parameters": {
                "title": title, "date": date, "time": _parse_event_time(transcript), "duration": 60,
            }}

    if not _IMPLIED_PAST_TENSE_RE.search(transcript):
        m = _INTENT_CALENDAR_IMPLIED_MEET_RE.search(transcript)
        base = "Quedada"
        if not m:
            m = _INTENT_CALENDAR_IMPLIED_REUNION_RE.search(transcript)
            base = "Reunión"
        if m:
            date = _parse_event_date(transcript)
            if date is not None:
                who = (m.group(1) or "").strip(" ,.¿?¡!")
                title = f"{base} con {who}" if who else base
                return {"intent": "calendar_propose", "parameters": {
                    "title": title, "date": date, "time": _parse_event_time(transcript), "duration": 60,
                }}

    if _INTENT_APP_OPEN_IMPLIED_MUSIC_RE.search(transcript):
        return {"intent": "app_open_propose", "parameters": {"app_key": "música"}}
    if _INTENT_APP_OPEN_IMPLIED_CALENDAR_RE.search(transcript):
        return {"intent": "app_open_propose", "parameters": {"app_key": "calendario"}}

    if _EXPLICIT_SEARCH_REQUEST_RE.search(transcript) or _CURRENT_INFO_KEYWORD_RE.search(transcript):
        return {"intent": "web_search", "parameters": {}}
    return {"intent": "unknown", "parameters": {}}


# ═══════════════════════════════════════════════════════════════════════════
# DESIGN MODE — Armor Design Studio (Diseño → Armaduras). 'desarrolla esto' /
# 'no sé cómo seguir' / 'toma el control' / 'qué harías tú aquí' — the phrases
# that hand a design zone over to LIRA so she proposes the decision instead
# of just asking clarifying questions. Not routed through _detect_intent()
# above: the design workspace's chat panel (ui/js/design-studio.js) talks to
# its own endpoint (core/design_routes.py's POST /api/designs/chat), which
# calls is_design_takeover() directly on each message rather than going
# through the full voice-assistant dispatch pipeline — the workspace already
# knows which zone/design is active, context _detect_intent has no access
# to. Kept here anyway (not in design_routes.py) since it's classification,
# same category as every other _INTENT_*_RE above.
# ═══════════════════════════════════════════════════════════════════════════
_DESIGN_TAKEOVER_RE = re.compile(
    r"\bdesarrolla(?:me)?\s+esto\b|"
    r"\bno\s+s[eé]\s+c[oó]mo\s+seguir\b|"
    r"\btoma\s+(?:el\s+)?control\b|"
    r"\bqu[eé]\s+har[ií]as\s+t[uú]\s+aqu[ií]\b|"
    r"\bqu[eé]\s+har[ií]as\s+t[uú]\b|"
    r"\bpropon\s+(?:algo|una?\s+idea)\b|"
    r"\bt[uú]\s+decides\b",
    re.IGNORECASE,
)


def is_design_takeover(text: str) -> bool:
    """True if `text` (one message from the design workspace's chat panel)
    is asking LIRA to take over the current zone's design decisions rather
    than just answer/clarify. See core.commands.handle_design_mode, which
    branches its system prompt on this."""
    return bool(_DESIGN_TAKEOVER_RE.search(text or ""))
