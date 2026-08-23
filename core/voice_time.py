"""Natural time-of-day speech — 24h "HH:MM" -> casual spoken Spanish, used
only for the copy of text handed to the TTS engines (core.voice /
core.voice's Kokoro path). Chat/log text elsewhere is never touched."""
import random
import re

# Optionally consumes a preceding 'la'/'las' article (e.g. "Son las 14:30")
# and/or a trailing "de la mañana/tarde/noche" (e.g. "14:30 de la tarde") so
# both are discarded along with the digits — _speak_time_natural()'s return
# value always supplies its own correct article and time-of-day suffix
# (including neither, for medianoche/mediodía), so anything already in the
# surrounding text would otherwise be duplicated rather than replaced.
_TIME_RE = re.compile(
    r"\b(?:las?\s+)?([01]?\d|2[0-3]):([0-5]\d)(?!:\d{2})\b"
    r"(?:\s+de\s+la\s+(?:mañana|tarde|noche))?",
    re.IGNORECASE,
)

_HOUR_WORDS_ES = {
    1: "una", 2: "dos", 3: "tres", 4: "cuatro", 5: "cinco", 6: "seis",
    7: "siete", 8: "ocho", 9: "nueve", 10: "diez", 11: "once", 12: "doce",
}


def _hour_phrase(hour24: int) -> str:
    """24h hour -> 'la una' / 'las dos' / ... (12h, with the correct
    feminine singular article for 1 o'clock — real speakers never say
    'las una')."""
    h12 = hour24 % 12
    if h12 == 0:
        h12 = 12
    word    = _HOUR_WORDS_ES[h12]
    article = "la" if h12 == 1 else "las"
    return f"{article} {word}"


def _time_of_day_suffix(hour24: int) -> str:
    if hour24 <= 11:
        return "de la mañana"
    if hour24 <= 17:
        return "de la tarde"
    return "de la noche"


def _speak_time_natural(hour: int, minute: int) -> str:
    """Convert a 24h (hour, minute) pair into a casual spoken Spanish time
    phrase, following the exact minute-bucket idioms real speakers use
    instead of reading digits aloud."""
    hour = hour % 24

    if hour == 0 and minute == 0:
        return "medianoche"
    if hour == 12 and minute == 0:
        return "mediodía"

    suffix = _time_of_day_suffix(hour)

    # "Just passed the hour" / "almost the next hour" are idioms tied to
    # being right on a boundary, not something to reach via rounding.
    if minute in (1, 2):
        current = _hour_phrase(hour)
        phrase = random.choice([f"{current} y poco", f"{current} pasadas"])
        return f"{phrase} {suffix}"

    if minute in (58, 59):
        nxt = _hour_phrase(hour + 1)
        phrase = random.choice([f"casi {nxt}", f"{nxt} menos dos"])
        return f"{phrase} {suffix}"

    # Every other minute value: round to the nearest 5-minute mark so the
    # phrasing below always lands on one of the idioms below — nobody says
    # "las dos y treinta y siete" out loud.
    rounded_minute = int(round(minute / 5)) * 5
    if rounded_minute == 60:
        hour   = (hour + 1) % 24
        suffix = _time_of_day_suffix(hour)
        rounded_minute = 0

    current = _hour_phrase(hour)
    nxt     = _hour_phrase(hour + 1)

    if rounded_minute == 0:
        phrase = random.choice([f"{current} en punto", current])
    elif rounded_minute == 5:
        phrase = f"{current} y cinco"
    elif rounded_minute == 10:
        phrase = f"{current} y diez"
    elif rounded_minute == 15:
        phrase = f"{current} y cuarto"
    elif rounded_minute == 20:
        phrase = f"{current} y veinte"
    elif rounded_minute == 25:
        phrase = f"{current} y veinticinco"
    elif rounded_minute == 30:
        phrase = f"{current} y media"
    elif rounded_minute == 35:
        phrase = random.choice([f"{current} y treinta y cinco", f"casi {nxt} menos veinticinco"])
    elif rounded_minute == 40:
        phrase = f"{nxt} menos veinte"
    elif rounded_minute == 45:
        phrase = f"{nxt} menos cuarto"
    elif rounded_minute == 50:
        phrase = f"{nxt} menos diez"
    else:  # 55
        phrase = random.choice([f"{nxt} menos cinco", f"casi {nxt}"])

    return f"{phrase} {suffix}"


def _naturalize_times(text: str) -> str:
    """Replace every 24h 'HH:MM' occurrence in `text` with a casual spoken
    Spanish time phrase before it reaches TTS. Never touches chat/log text
    — callers only ever pass the copy of the text headed to the TTS engine."""
    def _sub(m: re.Match) -> str:
        return _speak_time_natural(int(m.group(1)), int(m.group(2)))
    return _TIME_RE.sub(_sub, text)
