"""Root logging configuration for jarvis.py: the coloured per-role console
formatter, rotating activity/error log files, and startup audio-device
diagnostics. Must be applied (via _setup_logging()) before any other core
module is imported, so every module's logger inherits the same handlers.
"""
import logging
import sys
from logging.handlers import RotatingFileHandler

# ---------------------------------------------------------------------------
# ANSI colours
# ---------------------------------------------------------------------------
_RST    = "\033[0m"
_YELLOW = "\033[33m"
_BLUE   = "\033[34m"
_RED    = "\033[31m"

# role → (label, emoji, ansi_color | None)
_ROLE_MAP = {
    "__main__":      ("SYSTEM:", "⚙️ ", _YELLOW),
    "core.listener": ("USER:  ", "🎤 ", None),       # no colour per spec
    "core.commands": ("JARVIS:", "🤖 ", _BLUE),
    "core.voice":    ("JARVIS:", "🤖 ", _BLUE),
}
_ROLE_SYSTEM = ("SYSTEM:", "⚙️ ", _YELLOW)
_ROLE_ERROR  = ("ERROR: ", "❌ ", _RED)


class _JarvisFormatter(logging.Formatter):
    """Formats every line as:  ROLE:  [ HH:MM:SS ] emoji  message"""

    def format(self, record: logging.LogRecord) -> str:
        if record.levelno >= logging.ERROR:
            label, emoji, color = _ROLE_ERROR
        else:
            label, emoji, color = _ROLE_MAP.get(record.name, _ROLE_SYSTEM)

        ts  = self.formatTime(record, datefmt="%H:%M:%S")
        msg = record.getMessage()

        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            msg = f"{msg}\n{record.exc_text}"

        line = f"{label}  [ {ts} ] {emoji}  {msg}"
        if color:
            line = f"{color}{line}{_RST}"
        return line


_PLAIN_FMT = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def _setup_logging():
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Silence noisy third-party loggers
    logging.getLogger("speechbrain").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # Console — coloured, INFO and above, written to stderr (unbuffered)
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.INFO)
    ch.setFormatter(_JarvisFormatter())
    root.addHandler(ch)

    # Rotating activity log — plain text, INFO and above (5 MB × 3 backups)
    ah = RotatingFileHandler(
        "logs/activity.log", maxBytes=5 * 1024 * 1024, backupCount=3
    )
    ah.setLevel(logging.INFO)
    ah.setFormatter(_PLAIN_FMT)
    root.addHandler(ah)

    # Rotating error log — plain text, ERROR and above
    eh = RotatingFileHandler(
        "logs/errors.log", maxBytes=5 * 1024 * 1024, backupCount=3
    )
    eh.setLevel(logging.ERROR)
    eh.setFormatter(_PLAIN_FMT)
    root.addHandler(eh)


_logger = logging.getLogger(__name__)


def _log_audio_devices() -> None:
    """
    Query sounddevice for all audio devices and log them.
    Surfacing this early makes it easy to spot launchd/TCC microphone issues
    in the log (e.g. empty device list or missing input devices).
    """
    try:
        import sounddevice as sd

        devices = sd.query_devices()
        default_in  = sd.default.device[0]
        default_out = sd.default.device[1]

        _logger.info("--- Audio devices ---")
        for idx, dev in enumerate(devices):
            marker = ""
            if idx == default_in:
                marker += " [default-in]"
            if idx == default_out:
                marker += " [default-out]"
            _logger.info(
                "  [%d] %s  (in=%d out=%d)%s",
                idx, dev["name"],
                dev["max_input_channels"], dev["max_output_channels"],
                marker,
            )

        if default_in < 0:
            _logger.warning("No default input device — microphone may be unavailable in this session.")
        else:
            try:
                default_dev = devices[default_in]
                _logger.info(
                    "Default input: [%d] %s (%d ch @ %g Hz)",
                    default_in, default_dev["name"],
                    default_dev["max_input_channels"],
                    default_dev["default_samplerate"],
                )
            except Exception as exc:
                _logger.warning("Could not read default input device info: %s", exc)

    except Exception as exc:
        _logger.warning("sounddevice query failed: %s", exc)
