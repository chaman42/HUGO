"""Flask-SocketIO app, device auth, and the SocketIO emit_*/get_* API every
other core module uses to talk to the frontend. Route handlers themselves
live in core.routes_control / core.routes_memory / core.routes_sleep."""
import json
import logging
import os
import threading
import time

from flask import Flask, redirect, request
from flask_socketio import SocketIO

# Silence Flask/SocketIO's own loggers before creating the app
logging.getLogger("werkzeug").setLevel(logging.ERROR)
logging.getLogger("engineio").setLevel(logging.ERROR)
logging.getLogger("socketio").setLevel(logging.ERROR)

_BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Path to the device whitelist shared with launcher.py
_ALLOWED_DEVICES_FILE = os.path.join(_BASE_DIR, "data", "allowed_devices.json")

app = Flask(
    __name__,
    static_folder=os.path.join(_BASE_DIR, "ui"),
    static_url_path="",   # serve ui/ files at the root URL
)
app.config["SECRET_KEY"] = "jarvis-hud-secret"

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
    # Prevent WebSocket upgrade attempts — Werkzeug's dev server doesn't support
    # the WebSocket protocol and raises "write() before start_response" on every
    # upgrade handshake. Long-polling is identical in practice for a local app.
    allow_upgrades=False,
    logger=False,
    engineio_logger=False,
)

logger = logging.getLogger(__name__)

# Set to True once all models are loaded and Jarvis is fully operational.
_is_ready = False


# ---------------------------------------------------------------------------
# Device authentication — mirrors the logic in launcher.py
# ---------------------------------------------------------------------------

def _is_fingerprint_allowed(fp: str) -> bool:
    """Return True if fp is in the device whitelist, or if no devices are registered yet.

    Bootstrap rule: an empty whitelist allows everyone so the first device can
    register without needing to be pre-approved.  Once any fingerprint is saved
    only registered devices are allowed.

    Handles two entry formats in allowed_devices.json (matching launcher.py):
      - plain string:  "abc123..."
      - labelled dict: {"hash": "abc123...", "label": "MacBook Pro Joan"}
    """
    if not fp:
        return False
    try:
        with open(_ALLOWED_DEVICES_FILE) as fh:
            data = json.load(fh)
        registered: set[str] = set()
        for item in data.get("fingerprints", []):
            if isinstance(item, str):
                registered.add(item)
            elif isinstance(item, dict) and "hash" in item:
                registered.add(item["hash"])
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        registered = set()
    if not registered:
        return True   # bootstrap: no devices registered yet
    return fp in registered


@socketio.on("connect")
def _on_jarvis_socket_connect():
    """Reject Jarvis SocketIO connections from unregistered device fingerprints.

    The frontend passes ?fp=<fingerprint> as a query param on every io() call.
    Returning False rejects the connection before any events are processed.
    """
    # AUTH TEMPORARILY DISABLED — allow all devices without fingerprint check.
    pass
    # fp = request.args.get("fp", "")
    # if not _is_fingerprint_allowed(fp):
    #     logger.warning(
    #     "Jarvis SocketIO rejected: unregistered fingerprint %r from %s",
    #         (fp[:16] + "...") if len(fp) > 16 else fp,
    #         request.remote_addr,
    #     )
    #     return False   # reject connection


def set_ready(ready: bool = True) -> None:
    """Called from jarvis.py after models finish loading."""
    global _is_ready
    _is_ready = ready
    if ready:
        logger.info("Jarvis marked as ready.")
        try:
            socketio.emit("jarvis_ready", {"ready": True})
        except Exception:
            pass

        # The frontend applies its own default theme (HUGO) at page load, but
        # that's a guess made before this socket ever connects — if the user
        # had switched personality in a previous session, or this fires before
        # the frontend's own init runs, it can end up showing the wrong one
        # with nothing to correct it. _switch_personality() won't help here:
        # it no-ops (no emit) when the target equals the current personality,
        # which is exactly the common case on a fresh start. Emit directly
        # with whatever's actually active right now so the frontend always
        # syncs to the real state instead of its own guess.
        try:
            import core.personality as personality
            with personality._personality_lock:
                name = personality._personality
            p = personality.PERSONALITIES[name]
            socketio.emit("personality_change", {
                "personality":   name,
                "display_name":  p["display_name"],
                "color":         p["color"],
            })
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.after_request
def _cors(response):
    """Allow cross-origin requests from the launcher (port 8179)."""
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


@app.route("/")
def index():
    # Redirect Dock shortcuts / bookmarks still pointing at the old port 8180
    # to the launcher at 8179 which is now the canonical entry point.
    host = request.host.split(":")[0]   # strip port
    return redirect(f"http://{host}:8179/", code=302)


# ---------------------------------------------------------------------------
# Public API used by other core modules
# ---------------------------------------------------------------------------

def emit_status(status: str) -> None:
    """Broadcast mic status ('listening' | 'processing' | 'speaking') to all clients."""
    try:
        socketio.emit("status", {"status": status})
    except Exception:
        pass  # no clients connected, or server not started yet


def emit_mic_active() -> None:
    """Signal that the microphone audio stream is open and capturing."""
    try:
        socketio.emit("mic_active", {})
    except Exception:
        pass


def emit_mic_inactive() -> None:
    """Signal that the microphone audio stream has stopped or failed."""
    try:
        socketio.emit("mic_inactive", {})
    except Exception:
        pass


def emit_mic_level(level: float) -> None:
    """Broadcast current RMS energy level (0.0–1.0, log-scaled for display)."""
    try:
        socketio.emit("mic_level", {"level": round(level, 4)})
    except Exception:
        pass


def emit_partial_transcript(text: str) -> None:
    """Broadcast a partial STT result so the UI can show it in real time."""
    try:
        socketio.emit("partial_transcript", {"text": text})
    except Exception:
        pass


def emit_diamond_move(region: str) -> None:
    """Broadcast a 'diamond_move' event so the floating HUGO diamond
    (ui/index.html's #hugoDiamond) animates to the requested general area.

    Called by core.intent_ui._detect_diamond_move()'s call site in
    _dispatch_command_impl right after detecting a move phrase ('muévete',
    've a la esquina', 'muévete a la derecha', ...) — `region` is one of
    the named region keys the frontend's DIAMOND_REGIONS table understands
    ('top-left'|'top-right'|'bottom-left'|'bottom-right'|'top'|'bottom'|
    'left'|'right'|'center'|'away'); the frontend picks the best specific
    low-density spot within that area itself (same density-scored grid
    used for autonomous positioning) rather than an exact pixel target.
    Purely additive/best-effort, same as every other emit_* here — never
    raises, never blocks dispatch_command."""
    try:
        socketio.emit("diamond_move", {"region": region})
    except Exception:
        pass


def emit_sleep_phase_update(data: dict) -> None:
    """Broadcast a 'sleep_phase_update' event so NÚCLEO HUGO's Estado tab
    can update its "ÚLTIMO SUEÑO" section in near-real-time instead of
    waiting for its own ~2.5s poll of GET /api/sleep/status.

    Called by core/commands.py's own background sleep-phase watcher — NOT
    from core/sleep.py directly, since continuous sleep runs as a separate
    child PROCESS (see core/commands.py's own section comment on
    _start_continuous_sleep) with no access to this process's `socketio`
    object. The watcher polls data/sleep_budget.json's 'continuous' state
    every few seconds while a sleep subprocess is alive and emits this
    exactly when current_cycle/current_phase_num/running actually change —
    see core.background_loops._sleep_phase_watch_loop().

    `data` carries {running, current_cycle, current_phase_num,
    current_phase}, forwarded as-is. Purely additive/best-effort, same as
    every other emit_* here — never raises."""
    try:
        socketio.emit("sleep_phase_update", data)
    except Exception:
        pass


def emit_show_panel(panel_data: dict) -> None:
    """Broadcast a contextual-panel event so the main menu can animate in a
    visual panel (weather, time, ...) while HUGO speaks about that topic.

    Called by core.session._maybe_emit_panel() right after intent
    detection, before the reply is generated — the frontend times the
    panel's actual reveal to the 'speaking' status transition itself, not
    to this event's arrival (see ui/index.html's setStatus()).

    `panel_data` is forwarded to the client as-is; at minimum it must carry
    a `type` key (e.g. "weather", "time") so the frontend's PANEL_RENDERERS
    registry knows how to render it. Purely additive/best-effort, same as
    every other emit_* here — never raises, never blocks dispatch_command,
    and no-ops harmlessly if no client is connected or the panel type is
    one the current frontend doesn't recognize yet.
    """
    try:
        socketio.emit("show_panel", panel_data)
    except Exception:
        pass


def emit_hugo_thinking(data: dict) -> None:
    """Broadcast a completed thinking block so the CORE app's Pensamiento
    tab can show it live, the moment it's available.

    Called by core.groq_client._groq_complete() right after a streamed reply
    that actually had <think> content finishes — see that function's
    GROQ_MODEL_CHAIN comment for which tiers produce reasoning at all.
    `data` carries {query, thinking, model}, forwarded as-is. Purely
    additive/best-effort, same as emit_show_panel above — never raises,
    never blocks dispatch_command.
    """
    try:
        socketio.emit("hugo_thinking", data)
    except Exception:
        pass


def emit_response_timing(data: dict) -> None:
    """Chat response-latency display (ui/js/chat-render.js) — 'LLM: Xs ·
    VOZ: Xs' shown faintly below each assistant reply.

    Only 'llm_latency' is actually reported now — {'llm_latency': X} from
    core.commands._dispatch_command_impl, right after the reply text is
    finalized (time to first Groq token). A 'tts_latency' half used to be
    reported too (core.voice._emit_tts_first_audio, fired when Kokoro/XTTS
    audio genuinely started playing), fired independently since TTS could
    take dramatically longer than the LLM call to start — removed along
    with those two engines; `say` has no comparable "first audio" signal
    worth reporting (see core.voice._speak_say_blocking's own comment).

    The frontend has no per-message id to correlate against — turns are
    processed one at a time in the common case (see
    core.commands._dispatch_busy) — so it merges each partial update onto
    the chat log's MOST RECENTLY ADDED assistant bubble. A user who fires
    off a second message before the first one's (very slow) XTTS audio
    has even started would see that first reply's tts_latency mis-attach
    to the second bubble instead — a known, accepted edge case, not
    something this event carries enough information to prevent.
    """
    try:
        socketio.emit("response_timing", data)
    except Exception:
        pass


def emit_tts_audio_ready(audio_id: str) -> None:
    """'Repeat that' replay button (ui/js/chat-render.js's .msg-replay-btn) —
    fired by core.voice._speak_edge_tts_blocking once a reply's audio is
    synthesized and cached (core.voice.get_cached_audio_path), served over
    GET /api/tts_audio/<id> (core.routes_control). Same 'no per-message id,
    merge onto the most recently added assistant bubble' convention as
    emit_response_timing right above — see that function's own docstring
    for the accepted edge case this shares (a second message arriving
    before this one's audio is ready would misattach the button)."""
    try:
        socketio.emit("tts_audio_ready", {"id": audio_id})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# User activity — frontend HUD events (navigate / typing / opening / idle)
# so HUGO can act as a co-pilot noticing what Joan is doing in the interface
# itself, not just what he says out loud (see ui/index.html's _emitUserActivity
# and core.commands.on_user_activity / _build_system_prompt's ACTIVIDAD
# ACTUAL block). Storage lives here (alongside the SocketIO connection that
# receives it) rather than in core.commands, mirroring how mic/listen-mode
# state lives in core.listener — commands.py reads it via get_user_activity().
# ---------------------------------------------------------------------------

_user_activity_lock = threading.Lock()
_user_activity: dict = {"section": None, "action": None, "context": {}, "updated_at": 0.0}


@socketio.on("user_activity")
def _on_user_activity(data):
    """Frontend HUD activity event. Stores the latest snapshot (read by
    core.personalities.base._build_system_prompt) and hands off to
    core.commands.on_user_activity() — off this SocketIO thread, in a
    background thread — which decides via an LLM call whether an
    unprompted comment fits. Never raises back to the client; malformed
    payloads are just dropped.
    """
    try:
        if not isinstance(data, dict):
            return
        section = data.get("section")
        action  = data.get("action")
        if not section or not action:
            return
        context = data.get("context")
        context = context if isinstance(context, dict) else {}

        with _user_activity_lock:
            _user_activity["section"]    = section
            _user_activity["action"]     = action
            _user_activity["context"]    = context
            _user_activity["updated_at"] = time.time()
    except Exception:
        logger.debug("user_activity handler failed (non-critical)", exc_info=True)
        return

    try:
        import core.commands as commands
        threading.Thread(
            target=commands.on_user_activity,
            args=(section, action, context),
            daemon=True, name="activity-observer",
        ).start()
    except Exception:
        logger.debug("Could not hand off user_activity to core.commands", exc_info=True)


def get_user_activity() -> dict:
    """Snapshot of the most recent frontend HUD activity event — used by
    core.personalities.base._build_system_prompt to inject the ACTIVIDAD ACTUAL block.
    Returns a dict with section=None if nothing has been reported yet."""
    with _user_activity_lock:
        return dict(_user_activity)


# ---------------------------------------------------------------------------
# HUD context — precise, full-detail state about exactly what's on screen
# right now, reported by ui/index.html's _emitHudContext() on every
# meaningful state change. Distinct from user_activity above: that's a
# lightweight "what's happening" signal for co-pilot commentary; this
# carries the FULL object so core.personalities.base._build_system_prompt's
# PANTALLA ACTUAL block can inject exact facts HUGO can answer from
# directly — see that block and ui/index.html's hud_context emit call
# sites. No section currently emits this event (Conceptuales, the last one
# that did, was removed) — the channel is kept for whichever section needs
# it next.
# ---------------------------------------------------------------------------

_hud_context_lock = threading.Lock()
_hud_context: dict = {"type": None, "updated_at": 0.0}


@socketio.on("hud_context")
def _on_hud_context(data):
    """Frontend precise-context event. Stores the event as-is (flat —
    'type', plus whatever other keys that event type sends), read by
    core.personalities.base._build_system_prompt via get_hud_context().
    Never raises back to the client; malformed payloads are just dropped.
    """
    try:
        if not isinstance(data, dict):
            return
        ctx_type = data.get("type")
        if not ctx_type:
            return
        with _hud_context_lock:
            _hud_context.clear()
            _hud_context.update(data)
            _hud_context["updated_at"] = time.time()
    except Exception:
        logger.debug("hud_context handler failed (non-critical)", exc_info=True)


def get_hud_context() -> dict:
    """Snapshot of the most recent precise HUD context event — used by
    core.personalities.base._build_system_prompt to inject the PANTALLA ACTUAL block.
    Returns a dict with type=None if nothing has been reported yet."""
    with _hud_context_lock:
        return dict(_hud_context)


def emit_user_transcript(text: str) -> None:
    """Add a voice command to the chat log as a user message — same 'log'
    event/shape the frontend already renders for typed input (jarvisSocket.on
    'log' → addMessage(type, message)), so voice and text turns look
    identical in the conversation view.

    Called by core.listener right after VAD closes the command window (the
    final transcript is known) and BEFORE dispatch_command() runs, so the
    user's turn always appears before the assistant's reply.
    """
    try:
        socketio.emit("log", {"type": "user", "message": text})
    except Exception:
        pass


def emit_force_reload() -> None:
    """Tell every connected HUD client to perform a hard page reload.

    Call this (or POST /api/reload) after bumping the Service Worker cache key
    so that all open browser tabs immediately discard the stale cached frontend
    and load the fresh version.
    """
    try:
        socketio.emit("force_reload", {})
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Route modules — imported for their side effect of registering routes onto
# the shared `app`/`socketio` objects above. Split out of this file into
# core.routes_control / core.routes_memory / core.routes_sleep; nothing here
# imports them back, since Flask dispatches routes by URL, never by name.
# core.estudio_routes follows the same pattern (GET /api/estudio) — it's
# jarvis's own app data (ideas/investigations/etc.), same as memory/sleep,
# so it's registered here rather than on launcher.py's separate Flask app.
# ---------------------------------------------------------------------------
import core.routes_control       # noqa: E402,F401
import core.routes_memory        # noqa: E402,F401
import core.routes_sleep         # noqa: E402,F401
import core.estudio_routes       # noqa: E402,F401
import core.routes_notifications # noqa: E402,F401
import core.routes_situation     # noqa: E402,F401
import core.routes_judgment      # noqa: E402,F401
import core.routes_initiative    # noqa: E402,F401
import core.routes_spontaneity   # noqa: E402,F401
import core.routes_social        # noqa: E402,F401
import core.routes_api_keys      # noqa: E402,F401 — imported AFTER routes_social so it can reuse its _joan_only
import core.routes_hugo_mobile   # noqa: E402,F401


# ---------------------------------------------------------------------------
# Logging → WebSocket bridge
# ---------------------------------------------------------------------------

# Only core.commands produces chat-visible messages ("Jarvis: ..." responses).
# core.listener and core.voice are pure operational/diagnostic loggers — their
# output belongs in the maintenance panel, not the conversation view.
_ROLE_MAP = {
    "core.listener":          "system",
    "core.commands":          "jarvis",
    "core.voice":             "system",
    # _speak_unprompted() logs "Jarvis: %s" here (proactive comments,
    # reminders, and now initiative/spontaneity deliveries) — without this
    # mapping the record falls through to the "system" default below and
    # gets routed to the maintenance panel instead of the chat log, so
    # every unprompted line HUGO says was invisible in the chat section.
    "core.background_loops": "jarvis",
}

# Prefixes to strip so the frontend shows clean text
_STRIP_PREFIXES = ("Jarvis: ", "Speaking: ", "Jarvis (static): ")

# Logger names whose messages should not be forwarded
_SKIP_LOGGERS = {"werkzeug", "engineio", "socketio", "flask", "core.server"}

# Operational/diagnostic prefixes that must always go to the maintenance panel,
# even when emitted by a "jarvis"-mapped logger like core.commands.
# "[MEMORY]" covers the four-layer memory system's startup health-check logs
# (see core.memory._log_memory_health) — those are diagnostics, not
# assistant replies, and must never show up as chat messages.
_OP_PREFIXES = ("[LATENCY]", "[CONV]", "[VAD]", "[MIC]", "[MEMORY]", "Personality switched", "Migrated ")


class SocketIOLogHandler(logging.Handler):
    """Forwards log records to all connected WebSocket clients."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # Drop noisy internal loggers
            root = record.name.split(".")[0]
            if record.name in _SKIP_LOGGERS or root in _SKIP_LOGGERS:
                return

            if record.levelno >= logging.ERROR:
                msg_type = "error"
            else:
                msg_type = _ROLE_MAP.get(record.name, "system")

            message = record.getMessage()

            # Skip "Speaking: ..." — already forwarded as "Jarvis: ..."
            if message.startswith("Speaking: "):
                return

            # Strip known prefixes so the label in the UI carries the context
            for prefix in _STRIP_PREFIXES:
                if message.startswith(prefix):
                    message = message[len(prefix):]
                    break

            # Force operational/diagnostic messages to the maintenance panel
            # regardless of which logger emitted them.
            for p in _OP_PREFIXES:
                if message.startswith(p):
                    msg_type = "system"
                    break

            # Strip surrounding quotes from transcripts  → jarvis qué hora es
            if message.startswith('"') and message.endswith('"') and len(message) > 2:
                message = message[1:-1]

            socketio.emit("log", {"type": msg_type, "message": message})
        except Exception:
            pass  # never crash the logging pipeline


# ---------------------------------------------------------------------------
# Server startup
# ---------------------------------------------------------------------------

def _heartbeat_loop() -> None:
    """Emit a heartbeat every 30 s to keep SocketIO connections alive."""
    while True:
        time.sleep(30)
        try:
            socketio.emit("heartbeat", {})
        except Exception:
            pass


def start() -> threading.Thread:
    """Start Flask-SocketIO on 0.0.0.0:8180 in a daemon thread."""
    # Task Engine wakeup check — logs any in_progress task's state (see
    # core.task_engine.TaskEngine.resume_on_wakeup's own docstring for why
    # the "Avancé en X..." summary Joan actually hears doesn't need to be
    # (re)computed here: advance_during_sleep() already queued it via
    # core.notifications while sleeping, delivered on her next real message
    # regardless of this restart). Best-effort — must never block server
    # startup.
    try:
        import core.task_engine as task_engine_mod
        task_engine_mod.task_engine.resume_on_wakeup()
    except Exception:
        logger.debug("Task engine resume_on_wakeup failed (non-critical)", exc_info=True)

    # Code Engine wakeup check — same reasoning as the task engine one just
    # above: a create/update cycle killed mid-flight by a crash, restart,
    # or the Mac sleeping leaves a torn module state behind (file rewritten
    # but never reviewed/version-bumped/installed) unless something rolls
    # it back on next boot. See core.code_engine.recover_orphaned_jobs()'s
    # own docstring.
    try:
        import core.code_engine as code_engine_mod
        code_engine_mod.recover_orphaned_jobs()
    except Exception:
        logger.debug("Code Engine recover_orphaned_jobs failed (non-critical)", exc_info=True)

    def _run():
        socketio.run(
            app,
            host="0.0.0.0",
            port=8180,
            use_reloader=False,
            log_output=False,
            allow_unsafe_werkzeug=True,
        )

    t = threading.Thread(target=_run, daemon=True, name="flask-server")
    t.start()

    hb = threading.Thread(target=_heartbeat_loop, daemon=True, name="heartbeat")
    hb.start()

    # Discord bridge is NOT started here. It now runs as its own always-on
    # launchd agent (scripts/com.jarvislite.discordbridge.plist), independent
    # of whether the Electron app / jarvis.py is even open — that's the
    # whole point of the Discord integration. Starting it again from here
    # would open a second Gateway connection on the same bot token and
    # answer every DM twice.

    return t
