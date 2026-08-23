"""macOS system control: AppleScript helpers, volume control, and opening
applications via `open -a`. Everything here fails soft (None/False) rather
than raising — Automation permission denials, missing apps, etc. are all
just another failure signal for core.commands to turn into a spoken reply."""
import logging
import subprocess

logger = logging.getLogger(__name__)

FETCH_TIMEOUT = 3   # seconds — shared with core.tools_environment's own constant of the same value, used here only by open_app()

OSASCRIPT_TIMEOUT = 10          # seconds — volume/mute/open-app/create-event calls


def _run_applescript(script: str, timeout: int = OSASCRIPT_TIMEOUT) -> str | None:
    """Run `script` via osascript and return its stdout (stripped), or None
    on any failure — non-zero exit (includes a denied Automation/Calendar
    permission prompt, which osascript reports as an error rather than
    blocking), timeout, or missing osascript binary. Never raises."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            logger.debug("osascript failed (exit %s): %s", result.returncode, result.stderr.strip())
            return None
        return result.stdout.strip()
    except Exception as exc:
        logger.debug("osascript failed: %s", exc)
        return None


def _applescript_escape(s: str) -> str:
    """Escape a string for safe interpolation inside a double-quoted
    AppleScript string literal (backslash and double-quote)."""
    return str(s).replace("\\", "\\\\").replace('"', '\\"')

def get_volume() -> int | None:
    """Current macOS system output volume, 0-100. None on failure."""
    raw = _run_applescript("output volume of (get volume settings)")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def set_volume(level: int) -> bool:
    """Set macOS system output volume to `level` (clamped to 0-100).
    Returns True only if the volume actually moved to (approximately) the
    requested level. A clean osascript exit code alone isn't trustworthy
    here — confirmed live: some audio output configurations accept the
    'set volume output volume N' command with no error at all and simply
    never change the level. Reading it back afterward catches that silent
    no-op instead of misreporting it as success."""
    level = max(0, min(100, int(level)))
    if _run_applescript(f"set volume output volume {level}") is None:
        return False
    actual = get_volume()
    return actual is not None and abs(actual - level) <= 2


def volume_up(amount: int = 10) -> int | None:
    """Increase system volume by `amount` (clamped at 100). Returns the new
    level, or None if either reading or setting the volume failed."""
    current = get_volume()
    if current is None:
        return None
    new_level = min(100, current + amount)
    return new_level if set_volume(new_level) else None


def volume_down(amount: int = 10) -> int | None:
    """Decrease system volume by `amount` (floored at 0). Returns the new
    level, or None if either reading or setting the volume failed."""
    current = get_volume()
    if current is None:
        return None
    new_level = max(0, current - amount)
    return new_level if set_volume(new_level) else None


def mute_system() -> bool:
    """Mute macOS system output. Returns True on success."""
    return _run_applescript("set volume with output muted") is not None


def unmute_system() -> bool:
    """Unmute macOS system output. Returns True on success."""
    return _run_applescript("set volume without output muted") is not None

_APP_NAME_MAP: dict[str, str] = {
    "spotify":              "Spotify",
    "safari":                "Safari",
    "chrome":                "Google Chrome",
    "google chrome":         "Google Chrome",
    "notas":                 "Notes",
    "notes":                 "Notes",
    "calendario":            "Calendar",
    "calendar":              "Calendar",
    "terminal":              "Terminal",
    "mensajes":              "Messages",
    "messages":              "Messages",
    "correo":                "Mail",
    "mail":                  "Mail",
    "fotos":                 "Photos",
    "photos":                "Photos",
    "musica":                "Music",
    "música":                "Music",
    "music":                 "Music",
    "recordatorios":         "Reminders",
    "reminders":             "Reminders",
    "mapas":                 "Maps",
    "maps":                  "Maps",
    "preferencias":          "System Settings",
    "ajustes":               "System Settings",
    "ajustes del sistema":   "System Settings",
    "calculadora":           "Calculator",
    "calculator":            "Calculator",
    "finder":                "Finder",
    "whatsapp":              "WhatsApp",
    "slack":                 "Slack",
    "zoom":                  "zoom.us",
    "vscode":                "Visual Studio Code",
    "visual studio code":    "Visual Studio Code",
    "word":                  "Microsoft Word",
    "excel":                 "Microsoft Excel",
    "powerpoint":            "Microsoft PowerPoint",
}


def resolve_app_name(spoken_name: str) -> str:
    """Map a spoken/typed app name to its real macOS application name via
    _APP_NAME_MAP. Anything not in the map is returned title-cased as-is."""
    key = (spoken_name or "").strip().lower()
    return _APP_NAME_MAP.get(key, spoken_name.strip().title())


def open_app(name: str) -> bool:
    """Open a macOS application by name via `open -a` (resolved through
    resolve_app_name first). Returns True if it launched, False if the app
    isn't installed/found or anything else failed — never raises."""
    resolved = resolve_app_name(name)
    try:
        result = subprocess.run(
            ["open", "-a", resolved],
            capture_output=True, text=True, timeout=FETCH_TIMEOUT,
        )
        return result.returncode == 0
    except Exception as exc:
        logger.debug("open_app(%r -> %r) failed: %s", name, resolved, exc)
        return False
