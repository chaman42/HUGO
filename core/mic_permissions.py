"""macOS microphone permission helpers (AVFoundation/PyObjC) used by the
launcher to check and actively request mic access before starting jarvis.py.
Safe no-ops on platforms without PyObjC.
"""
import subprocess
import threading

from core.launcher_app import logger


def _get_mic_status() -> str:
    """Return 'authorized' | 'denied' | 'restricted' | 'not_determined' | 'unknown'."""
    try:
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio
        code = AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio)
        return {0: "not_determined", 1: "restricted", 2: "denied", 3: "authorized"}.get(code, "unknown")
    except ImportError:
        return "unknown"
    except Exception as exc:
        logger.debug("mic_status check failed: %s", exc)
        return "unknown"


def _open_mic_preferences():
    """Open macOS Privacy → Microphone settings page."""
    try:
        subprocess.Popen(
            ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        logger.info("Opened System Preferences → Microphone.")
    except Exception as exc:
        logger.warning("Could not open System Preferences: %s", exc)


def _request_mic_permission(timeout: float = 30.0) -> bool:
    """Actively trigger the macOS microphone permission dialog via AVFoundation
    (PyObjC) — this REQUESTS access, unlike _get_mic_status() which only reads
    the current status. Replaces the old Terminal.app workaround: that existed
    solely to give the process a real GUI/Aqua/WindowServer session for the
    permission dialog to display correctly. Electron already provides that
    session to its launcher.py child (and SessionCreate=true in the old
    LaunchAgent did the same) — a real Terminal window was never actually
    required, just a working WindowServer connection, which requestAccess
    already has.

    Only does anything when status is 'not_determined': the dialog can only
    be triggered once, after which TCC remembers the answer and this call
    would just return the cached result. Blocks up to `timeout` seconds for
    the user to respond. Never raises — any PyObjC/AVFoundation failure is
    treated as "could not obtain permission" so the caller can fall back to
    opening System Preferences instead.
    """
    try:
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio
    except ImportError:
        logger.debug("PyObjC AVFoundation not available — cannot request mic permission.")
        return False

    status = _get_mic_status()
    if status == "authorized":
        return True
    if status != "not_determined":
        return False  # denied/restricted/unknown — a prompt won't help

    done   = threading.Event()
    result = {"granted": False}

    def _completion(granted):
        result["granted"] = bool(granted)
        done.set()

    try:
        AVCaptureDevice.requestAccessForMediaType_completionHandler_(AVMediaTypeAudio, _completion)
    except Exception as exc:
        logger.warning("AVFoundation mic permission request failed: %s", exc)
        return False

    logger.info("Waiting for the user to respond to the microphone permission dialog…")
    if not done.wait(timeout=timeout):
        logger.warning("Microphone permission dialog timed out after %.0f s.", timeout)
        return False

    logger.info("Microphone permission %s.", "granted" if result["granted"] else "denied")
    return result["granted"]
