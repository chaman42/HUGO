"""Flask routes for Dani's one-time first-launch onboarding sequence (see
ui/js/onboarding-intro.js): whether it's already been shown to the CURRENT
identified person, marking it seen, and on-demand TTS for its 3 fixed
lines — reusing the exact same synth+cache+serve path every other HUGO
reply uses (core.voice._edge_tts_synthesize/_cache_tts_audio, served back
through the existing GET /api/tts_audio/<id> in core.routes_control — no
new audio-serving path needed).

Deliberately its own module rather than living in core.routes_social —
that whole file is Joan-only by design (see its own docstring); this one
is for the opposite audience."""
import asyncio
import logging
import tempfile

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
}

# line_key -> audio id, synthesized once per process. Not persisted to disk
# on purpose — re-synthesizing on a process restart is free (edge-tts, no
# API cost), and onboarding plays once, immediately, at first launch, well
# before core.voice's own MAX_CACHED_AUDIO=30 replay-cache ring buffer
# could ever evict these in the same session.
_onboarding_audio_ids: dict[str, str] = {}


@app.route("/api/onboarding/audio/<line_key>")
def api_onboarding_audio(line_key):
    if line_key not in _ONBOARDING_LINES:
        return jsonify({"error": "unknown line"}), 404
    if line_key not in _onboarding_audio_ids:
        import core.voice as voice_mod
        tmp_path = tempfile.mkstemp(suffix=".mp3", prefix="hugo_onboarding_")[1]
        try:
            edge_rate = voice_mod._wpm_to_edge_rate(175)
            asyncio.run(voice_mod._edge_tts_synthesize(
                _ONBOARDING_LINES[line_key], voice_mod.EDGE_TTS_VOICE, edge_rate, tmp_path,
            ))
            _onboarding_audio_ids[line_key] = voice_mod._cache_tts_audio(tmp_path)
        except Exception as exc:
            logger.error("Onboarding TTS synth failed for %s: %s", line_key, exc, exc_info=True)
            return jsonify({"error": "synthesis failed"}), 500
    return jsonify({"audio_id": _onboarding_audio_ids[line_key], "text": _ONBOARDING_LINES[line_key]})
