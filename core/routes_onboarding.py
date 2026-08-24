"""Flask routes for Dani's one-time first-launch onboarding sequence (see
ui/js/onboarding-intro.js): whether it's already been shown to the CURRENT
identified person, marking it seen, and speaking its 3 fixed lines.

Real incident (2026-08-24, found live-testing): the first version of this
spoke each line through the BROWSER's own <audio> element (fetch an id,
new Audio(...).play()) — but Chrome (and Electron's default Chromium too,
nothing in electron/window.js overrides its autoplay policy) blocks
audio.play() with zero prior user interaction, exactly the situation on a
fresh boot. Every OTHER HUGO utterance instead plays server-side via
core.voice._speak_edge_tts_blocking() (afplay as a subprocess, no browser
audio API involved at all), which never touches that policy — so
api_onboarding_speak() below calls that directly instead, synchronously,
and the frontend just awaits the response to know a line finished.

Deliberately its own module rather than living in core.routes_social —
that whole file is Joan-only by design (see its own docstring); this one
is for the opposite audience."""
import logging

from flask import jsonify

from core.server import app
from core import social

logger = logging.getLogger(__name__)


def _current_person_id() -> str | None:
    """Same who_is_present()-based identity resolution used throughout the
    app (core.commands, core.routes_api_keys, core.routes_social, ...)."""
    try:
        present = social.social_engine.who_is_present()
        return present[0].id if present else None
    except Exception:
        logger.debug("onboarding identity lookup failed (non-critical)", exc_info=True)
        return None


@app.route("/api/onboarding/status")
def api_onboarding_status():
    """Whether the CURRENT identified person has already seen onboarding,
    plus who that is — ui/js/onboarding-intro.js only ever runs the
    sequence when person_id is 'dani' AND seen is false."""
    person_id = _current_person_id()
    person = social.get_person(person_id) if person_id else None
    return jsonify({"seen": bool(person and person.onboarding_seen), "person_id": person_id})


# Minimum system volume for the first-launch sequence (2026-08-24, Joan's
# request) — a fresh Mac's default/low volume shouldn't mean Dani misses
# the whole spoken intro. Only ever raises, never lowers — see
# core.voice.ensure_min_system_volume's own docstring.
_ONBOARDING_MIN_VOLUME = 40


@app.route("/api/onboarding/ensure_volume", methods=["POST"])
def api_onboarding_ensure_volume():
    try:
        import core.voice as voice_mod
        voice_mod.ensure_min_system_volume(_ONBOARDING_MIN_VOLUME)
    except Exception as exc:
        logger.debug("ensure_volume failed (non-critical): %s", exc, exc_info=True)
    return jsonify({"ok": True})


@app.route("/api/onboarding/seen", methods=["POST"])
def api_onboarding_mark_seen():
    person_id = _current_person_id()
    if person_id:
        social.mark_onboarding_seen(person_id)
    return jsonify({"ok": True})


# Exact wording spoken/typed during the sequence — see ui/js/onboarding-intro.js's
# own beat-by-beat sequencing for how these three lines are used.
_ONBOARDING_LINES = {
    "intro": (
        "Hola, soy tu herramienta universal de gestión de olvidos, "
        "o puedes llamarme HUGO."
    ),
    "purpose": (
        "Estoy diseñado para asistirte en una amplia gama de campos y trabajos, "
        "ya sea haciendo resúmenes, esquemas, investigaciones, "
        "o recordándote que no seas un vago."
    ),
    "keys": (
        "Sin embargo, antes de activar mis funciones totalmente, necesito que "
        "pongas las llaves de API en Ajustes. Tendrás que entrar a internet y "
        "crearte una cuenta gratuita para obtener esas claves API. Una vez las "
        "tengas, podrás acceder a mis plenas capacidades."
    ),
    # Second sequence (2026-08-24) — plays once, the moment Dani's keys go
    # from incomplete to complete (see ui/js/onboarding-intro.js's
    # _runUnlockSequence()), unlocking full navigation. Not gated by
    # onboarding_seen at all — it's driven purely by the lock-status
    # transition, so it plays exactly once regardless of how many times he
    # reloads the app before finishing setup.
    "unlocked": (
        "Perfecto, ya tienes tus claves configuradas. A partir de ahora tienes "
        "acceso a todas mis funciones — resúmenes, esquemas, investigaciones, "
        "recordatorios, y todo lo demás. Adelante."
    ),
}

@app.route("/api/onboarding/speak/<line_key>", methods=["POST"])
def api_onboarding_speak(line_key):
    """Synthesizes (if not already cached) and PLAYS `line_key` synchronously
    server-side, blocking until afplay actually finishes — see this
    module's own docstring for why, instead of the browser fetching an
    audio id and playing it itself. The frontend just awaits this to know
    when the line is done and it's time for the next beat."""
    if line_key not in _ONBOARDING_LINES:
        return jsonify({"error": "unknown line"}), 404
    text = _ONBOARDING_LINES[line_key]
    try:
        import core.voice as voice_mod
        if not voice_mod.is_tts_muted():
            voice_mod._pre_speak(text)
            voice_mod._speak_edge_tts_blocking(text)
    except Exception as exc:
        logger.error("Onboarding speak failed for %s: %s", line_key, exc, exc_info=True)
        return jsonify({"ok": False, "error": "speak failed"}), 500
    return jsonify({"ok": True, "text": text})
