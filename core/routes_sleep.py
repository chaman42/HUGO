"""Flask routes: manual sleep-session start/stop and status/summary/
insights polling for NUCLEO HUGO's Estado/Pensamiento tabs."""
import logging
import time

from flask import jsonify

from core.server import app

logger = logging.getLogger(__name__)

@app.route("/api/sleep/start", methods=["POST"])
def api_sleep_start():
    """Starts continuous sleep (spawns the child process) and returns
    immediately — it now runs cycle after cycle indefinitely, so there's no
    "session" to wait out; the frontend polls GET /api/sleep/status for
    live cycle/phase progress instead. 409 if already sleeping.

    TEST MODE: refuses outright (same 409 shape) rather than silently
    no-op'ing, so the Ajustes button can show a clear inline message
    instead of looking like it did nothing."""
    import core.commands as commands
    from core import memory
    if memory.is_feature_enabled("modo_test"):
        return jsonify({"ok": False, "error": "modo_test_active"}), 409
    try:
        if commands.is_continuous_sleep_running():
            return jsonify({"ok": False, "error": "a sleep session is already running"}), 409
        started = commands._start_continuous_sleep(trigger="manual")
        if not started:
            return jsonify({"ok": False, "error": "a sleep session is already running"}), 409
        return jsonify({"ok": True})
    except Exception as exc:
        logger.error("Failed to start sleep session: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500

@app.route("/api/sleep/stop", methods=["POST"])
def api_sleep_stop():
    """Manual 'Detener Sueño' — signals the continuous-sleep subprocess to
    stop (see commands.stop_continuous_sleep()); it finishes whatever phase
    is in flight, then exits on its own. Returns immediately either way —
    the frontend's next GET /api/sleep/status poll will show
    continuous.running flip to false once the process actually exits."""
    import core.commands as commands
    try:
        stopped = commands.stop_continuous_sleep()
        return jsonify({"ok": True, "stopped": stopped})
    except Exception as exc:
        logger.error("Failed to stop sleep session: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500

@app.route("/api/sleep/status")
def api_sleep_status():
    """Budget/last-session snapshot (the old one-shot mechanism — still
    used by the standalone launchd 'app closed' path, see core.sleep's
    module docstring) plus the continuous-sleep state — see
    core.sleep.get_continuous_status()/data/sleep_budget.json's
    'continuous' key. 'running' in that block is corrected here to the
    DEFINITIVE answer (commands.is_continuous_sleep_running(), a live Popen
    handle in this process) rather than trusting the state file's own
    'running' flag on its own — a subprocess killed uncleanly (e.g. -9)
    would never get to flip that flag itself.

    Also computes next_trigger_seconds — a rough estimate of when the
    20-minute idle auto-trigger could next fire, from
    core.commands._last_interaction_mono, since core.sleep itself has no
    visibility into live interaction timing (see that module's own
    docstring on why it stays dependency-light)."""
    try:
        import core.commands as commands
        import core.sleep as sleep_mod
        status = sleep_mod.get_status()

        continuous = sleep_mod.get_continuous_status()
        continuous["running"] = commands.is_continuous_sleep_running()
        status["continuous"] = continuous

        next_trigger_seconds = None
        try:
            if not continuous["running"]:
                idle_elapsed = time.monotonic() - commands._last_interaction_mono
                next_trigger_seconds = max(0, int(sleep_mod.IDLE_TRIGGER_SECONDS - idle_elapsed))
        except Exception:
            pass
        status["next_trigger_seconds"] = next_trigger_seconds

        return jsonify(status)
    except Exception as exc:
        logger.error("Failed to read sleep status: %s", exc)
        return jsonify({"error": str(exc)}), 500

@app.route("/api/sleep_summary")
def api_sleep_summary():
    """Purpose-built summary of the last (or currently running)
    continuous-sleep run — backs NÚCLEO HUGO's Estado "ÚLTIMO SUEÑO"
    section (see core.sleep.get_sleep_summary()): when it happened, how
    many cycles, how long, and what it actually did (facts deleted/merged/
    promoted, insights generated, mind-map connections touched).

    'current.running' is corrected here to the DEFINITIVE answer
    (commands.is_continuous_sleep_running(), a live Popen handle in this
    process), same reasoning as GET /api/sleep/status above — the state
    file's own flag alone can't be trusted if the subprocess died
    uncleanly."""
    try:
        import core.commands as commands
        import core.sleep as sleep_mod
        summary = sleep_mod.get_sleep_summary()
        summary["current"]["running"] = commands.is_continuous_sleep_running()
        return jsonify(summary)
    except Exception as exc:
        logger.error("Failed to read sleep summary: %s", exc)
        return jsonify({"error": str(exc)}), 500

@app.route("/api/sleep_insights")
def api_sleep_insights():
    """Pending questions + reflections generated during sleep — backs
    NÚCLEO HUGO's Pensamiento tab ('PREGUNTAS DURANTE EL SUEÑO' /
    'REFLEXIONES DEL SUEÑO'). See core.sleep.get_sleep_insights_summary()."""
    try:
        import core.sleep as sleep_mod
        return jsonify(sleep_mod.get_sleep_insights_summary())
    except Exception as exc:
        logger.error("Failed to read sleep insights: %s", exc)
        return jsonify({"error": str(exc)}), 500
