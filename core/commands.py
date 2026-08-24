# ═══════════════════════════════════════════════════════════════════════════
# COMMANDS — main dispatch loop only: personality-switch/mode-switch/diamond-
# move short-circuits, the intent → action → response pipeline, and TTS
# dispatch. Interaction-tracking state (_dispatch_busy/_last_interaction_*)
# lives here too since dispatch_command() is what updates it on every call —
# core/background_loops.py, core/sleep_control.py and core/reminders.py all
# read it back via `import core.commands as commands`.
#
# ─── "Load but don't fire" — the three-level action philosophy ──────────────
# Every action with real consequences (calendar events/reminders, Estudio
# saves, app opens, and any future file write / message / document / setting
# change — see data/memory_instructions.json's own philosophy note) goes
# through one of three levels, never straight to execution without SOME
# form of it having been considered:
#
#   Level 1 — direct, unambiguous order with enough detail ('pon un evento
#     el viernes a las 5', 'crea un recordatorio para mañana', 'abre
#     Spotify'): execute immediately, confirm briefly ('Hecho.', 'Evento
#     añadido.'). No round trip — the user already gave a real order.
#   Level 2 — direct order needing review before final action ('prepara un
#     correo a X', 'redacta un mensaje'): prepare it, show it, only act
#     once the user reviews and confirms. No handler exists for this yet in
#     this codebase (no email/message/document capability to review) — see
#     core/actions.py's own module comment; the propose/confirm machinery
#     below is exactly what a future Level 2 handler would reuse.
#   Level 3 — implied action detected in normal conversation ('tengo que
#     ir a X mañana a las Y', 'no me olvides que...', asking for a
#     summary/schema whose OUTPUT should persist to Estudio): prepare it,
#     ask naturally in-character ('He preparado X. ¿Lo añado?'), act only
#     on an explicit yes next turn. Dropped silently if ignored.
#
# Web searches, informational answers, and anything else with no lasting
# side effect are NOT covered by any of this — those always execute/answer
# immediately, per spec.
#
# Levels 1 and 3 are implemented across core/intent.py (trigger detection +
# the generic Level-3 `_pending_action` slot) and core/actions.py (the
# actual handlers — see ITS module comment for the concrete Level 1 vs.
# Level 3 function list). This file's own Level-3 participant is
# generate_summary()/generate_schema() below, which propose an Estudio
# save rather than persisting it outright.
# ═══════════════════════════════════════════════════════════════════════════
#
# Everything else that used to live in this file has been split out (pure
# refactor, no behavior change):
#   core/groq_client.py      — Groq model chain + streaming/non-streaming calls
#   core/session.py          — conversation history + session-end bookkeeping
#   core/actions.py          — deterministic tool-action execution
#   core/response.py         — response formatting, web search, static fallback
#   core/reminders.py        — data/reminders.json load/save/parse/deliver
#   core/notifications.py    — data/notifications.json load/save/deliver
#   core/investigations.py   — data/investigations.json lifecycle storage
#   core/background_loops.py — proactive/reflective/sleep-phase-watch loops
#   core/sleep_control.py    — continuous-sleep subprocess lifecycle
#   core/activity.py         — the HUD co-pilot
#   core/personality.py + core/personalities/* — character/prompt/switching
#   core/memory.py / core/intent.py — persisted memory / intent detection
#
# Where one of those modules needs something from here (mostly
# _dispatch_busy/_last_interaction_mono and _say_for), it reaches back via a
# function-local `import core.commands as commands` to avoid a circular
# import — see their own module comments. Module objects (not `from x import
# name`) are used for personality/memory/intent below because jarvis.py's
# watchdog hot-reloads those files independently of this one (see its
# _MODULE_MAP) by calling importlib.reload() on each module object in place;
# a `from x import name` binding here would freeze a reference to the
# pre-reload function. `personality`/`intent` are aliased because this file
# uses those exact words as local variable/parameter names (e.g.
# `personality: str` params, the `intent` local in _dispatch_command_impl) —
# an unaliased import would be shadowed by those and raise
# UnboundLocalError.
# ═══════════════════════════════════════════════════════════════════════════
import datetime
import json
import logging
import os
import re
import threading
import time
import urllib.request
import uuid

from core.voice import speak
from core import tools
from core import memory
from core import personality as personality_mod
from core import intent as intent_mod
from core import groq_client
from core import session as session_mod
from core import actions
from core import response
from core import reminders
from core import notifications
from core import sleep_control
from core import activity
from core import groq_config
from core import ollama_control
from core import social_reasoning
from core import skill_dispatch
from core import speaker
from core import linguistic_fingerprint

# Re-exported so out-of-scope call sites elsewhere in the codebase (core/
# server.py's `commands.is_continuous_sleep_running()` / `commands.
# _start_continuous_sleep(...)` / `commands.stop_continuous_sleep()` /
# `commands.on_user_activity` / `commands._last_latency` /
# `commands.GROQ_MODEL_CHAIN`; core/memory.py's and core/intent.py's
# `commands._groq_complete_fast(...)` / `commands._get_history_snapshot()`)
# keep working unchanged even though the real implementations now live in
# core/sleep_control.py, core/activity.py, core/groq_config.py,
# core/groq_client.py and core/session.py. All of these are plain functions
# (or, for GROQ_MODEL_CHAIN/_last_latency, values that are never rebound
# after their own module loads — _last_latency is mutated in place by
# core/groq_client.py, clear+update, not reassigned) so a straight `from x
# import y` binding here stays correct for the life of the process.
from core.sleep_control import (
    is_continuous_sleep_running,
    _start_continuous_sleep,
    stop_continuous_sleep,
    notify_user_interaction,
)
# Data-only (a plain list of dicts, no functions) — a straight `from x
# import y` binding is fine here even though jarvis.py's watchdog doesn't
# track individual core/personalities/*.py files for hot-reload, unlike the
# module-object imports above.
from core.personalities.hugo import INTERNAL_CRITERIA
from core.activity import on_user_activity
from core.groq_config import GROQ_MODEL_CHAIN, _last_latency
from core.groq_client import _groq_complete_fast, _groq_complete_extract
from core.session import _get_history_snapshot

logger = logging.getLogger(__name__)

# Intents with real-world consequences — gated behind creator authority (see
# core.social.InfoPermissions.can_trigger_actions and this module's own
# "Creator authority gate" in _dispatch_command_impl). pending_confirm is
# included because it's what actually finalizes a previously-proposed
# calendar/reminder/app-open/package-install action — the propose step
# itself is here too since a friend confirming later shouldn't be able to
# execute what they proposed earlier either.
_ACTION_INTENTS_REQUIRE_CREATOR = {
    "calendar_write", "reminder_create", "open_app",
    "calendar_propose", "reminder_propose", "app_open_propose",
    "pending_confirm", "start_investigation", "code_engine_task", "create_task",
}

# ---------------------------------------------------------------------------
# Interaction-tracking state — updated at the top of every dispatch_command()
# call; read by core/background_loops.py and core/sleep_control.py's daemon
# threads to decide idle-triggered behavior (proactive comments, reminders,
# reflective mode, the Sleep System).
# ---------------------------------------------------------------------------

_dispatch_busy = threading.Event()   # set while dispatch_command is actively handling a turn

# Bug fix (2026-08-10): core/routes_control.py's /text_command spawns a new
# daemon thread per call with no queueing at all — several rapid text
# commands (e.g. automated testing, or a user typing quickly) used to fire
# fully concurrent dispatch_command() calls, each independently hitting the
# Groq API and voice.py's TTS queue at the same time. Observed in practice:
# a burst of ~6 requests in under a minute drove the ENTIRE
# groq_config.GROQ_MODEL_CHAIN into cascading timeouts (every tier failing
# within its own 5s budget, apparently from genuine account-level
# concurrency/rate pressure, not any one tier being down) and backed up
# voice.py's TTS queue badly enough that a 30s say() timeout had to kill it
# to unblock. dispatch_command() now serializes on this lock — a real
# mutual-exclusion primitive, unlike _dispatch_busy above (an advisory
# Event only ever read by background_loops.py's idle checks, never used to
# actually block a second concurrent call). One conversational turn fully
# finishes (LLM call + side effects) before the next one starts, matching
# how a single-user, turn-based assistant should behave regardless of which
# interface (voice, text_command, or any future entry point) fired it.
_dispatch_lock = threading.Lock()
_last_interaction_mono = time.monotonic()   # updated at the top of every dispatch_command call
_last_interaction_wall = memory._now_iso()          # wall-clock companion to the above — monotonic time
                                              # means nothing across a process restart, so this is
                                              # what core.session._save_session_end_state() persists as "ended_at"
_session_start_mono    = time.monotonic()   # this module's import time ≈ jarvis.py session start


# macOS `say` words-per-minute for the SAY engine — dynamically derived
# per message rather than one flat number (per feedback: a fixed rate read
# as too fast for some replies, too slow for others). core.voice.speak's
# own 175 default is untouched — it's also used by voice enrollment
# prompts and the Kokoro/XTTS last-resort fallback, neither of which this
# feedback was about.
_SAY_RATE_BASE = 195   # replaces the old flat 205 — a neutral middle, adjusted below
_SAY_RATE_MIN  = 165
_SAY_RATE_MAX  = 235

# Words that mark a message as a warning/important note worth slowing down
# for clarity — the one signal here where "faster" would be actively
# counterproductive.
_SAY_IMPORTANCE_RE = re.compile(
    r"\b(cuidado|importante|atenci[oó]n|advertencia|urgente|peligro|precauci[oó]n)\b",
    re.IGNORECASE,
)


def _dynamic_say_rate(text: str) -> int:
    """Per-message words-per-minute for the SAY engine, derived purely from
    the reply text itself — no extra classification call, so this adds
    ~zero latency and never blocks on anything. Three signals, each a
    small nudge off _SAY_RATE_BASE, clamped to [_SAY_RATE_MIN,
    _SAY_RATE_MAX] so no combination of them ever produces something too
    fast to follow or too slow to feel natural:

      length     — a short acknowledgment ("Vale.", "Sí, claro.") reads
                   naturally snappier; a long explanation dragging at the
                   base pace feels sluggish, so both ends nudge the rate
                   UP — only a normal-length reply sits at the plain base.
      punctuation — '!' reads as more energetic/urgent (faster, scaling
                   with how many); a trailing '?' reads as more
                   deliberate/questioning (slightly slower); this app's
                   own breath-pause markers ('…'/'—' — see
                   core.voice._PAUSE_RE, inserted by personality system
                   prompts at natural pause points) mark a more
                   reflective, measured line (slower).
      importance  — _SAY_IMPORTANCE_RE (cuidado/importante/urgente/...)
                   slows down for clarity — a warning read too fast
                   defeats the point of it being a warning.
    """
    rate = _SAY_RATE_BASE
    length = len(text)
    if length < 20:
        rate += 15
    elif length > 280:
        rate += 10

    exclaims = text.count("!")
    if exclaims:
        rate += min(20, 8 * exclaims)
    elif text.rstrip().endswith("?"):
        rate -= 5

    if "…" in text or "—" in text:
        rate -= 10

    if _SAY_IMPORTANCE_RE.search(text):
        rate -= 20

    return max(_SAY_RATE_MIN, min(_SAY_RATE_MAX, rate))


def _say_for(
    personality: str, text: str,
    cmd_start: float | None = None, llm_done_mono: float | None = None,
) -> None:
    """Dispatch TTS. Always non-blocking.

    Routes straight through macOS's native `say` command (core.voice.speak)
    — the only TTS engine left (Kokoro and XTTS were removed). Passes no
    explicit voice, so `say` uses whatever System Voice is set in System
    Settings -> Accessibility -> Spoken Content — including a Siri voice, if
    Joan has downloaded and selected one there — and follows the system
    default automatically if that's ever changed outside the app. Speaking
    rate is computed per message by _dynamic_say_rate() rather than fixed —
    see its own docstring.

    llm_done_mono: time.monotonic() reading from the instant this turn's
    reply text was finalized — threaded through to core.voice.speak(), but
    currently unused there (it used to measure "time from LLM response
    complete to first audio output" via core.voice._emit_tts_first_audio,
    removed along with Kokoro/XTTS — `say` has no comparable first-audio
    signal, see core.voice._speak_say_blocking's own comment). Kept as a
    plumbed-through parameter in case a future engine wants it again.
    """
    # Phase 1 conversational intelligence: every actual reply opens/refreshes
    # the post-response context window in core/listener.py, so the NEXT
    # utterance can be treated as a continuation without the wake word being
    # said again. Lazy import (same pattern as the mode-switch handler
    # above) — core.listener imports core.commands at call time, so a
    # module-level import here would be circular.
    try:
        import core.listener as _listener_note
        _listener_note.note_response(personality)
    except Exception:
        pass

    speak(text, rate=_dynamic_say_rate(text), cmd_start=cmd_start, llm_done_mono=llm_done_mono)


# ═══════════════════════════════════════════════════════════════════════════
# PHASE 3 — proactive contextual intervention WITHOUT a wake word.
#
# core/listener.py maintains a rolling _PASSIVE_BUFFER_SECS-second buffer of
# every finalized Vosk segment it hears, in either listen mode, and hands it
# to maybe_ambient_intervention() below every _PASSIVE_CHECK_INTERVAL_SECS
# (30s) — but only when there was actual speech in it. Everything from that
# handoff onward lives here:
#
#   1. Phase 2's should_intervene() gate (core/social_reasoning.py) — same
#      INTERVENIR/SILENCIO judgment used before every wake-word/continuation
#      reply and core.background_loops' own periodic proactive tick, just
#      fed what was actually overheard instead of a generic session
#      snapshot.
#   2. On INTERVENIR only, one local Ollama call (never Groq — this path
#      must never touch Joan's Groq quota) asking for a single brief,
#      in-character observation, or the literal '[SILENCIO]' if the model
#      changes its mind at generation time (same second-safety-net pattern
#      as core.background_loops._maybe_send_proactive_message).
#   3. Delivery through the normal TTS pipeline (_say_for) — no special
#      prefix, just Hugo speaking naturally, logged the same "Jarvis: %s"
#      way as a real reply so it renders as a normal chat bubble too (see
#      core.server.SocketIOLogHandler).
#
# Gated by the 'proactividad' feature flag, same as
# core.background_loops._maybe_send_proactive_message — this IS a
# proactive/unprompted-speech mechanism, just fed overheard speech instead
# of a periodic idle tick.
#
# Rate limited to one intervention per _AMBIENT_MIN_INTERVAL_SECS (10 min),
# independently of core.background_loops' own 30-min proactive cap — the two
# mechanisms watch different signals (overheard speech vs. idle session
# state) and are allowed to both eventually speak, just never in the same
# breath (both funnel through _dispatch_busy/voice.in_cooldown() below, so
# neither ever talks over the other or over a real command).
# ═══════════════════════════════════════════════════════════════════════════

_AMBIENT_MIN_INTERVAL_SECS      = 10 * 60   # spec: max 1 proactive intervention per 10 minutes
_last_ambient_intervention_mono: float | None = None
_ambient_lock                   = threading.Lock()

# Same local-model convention as core.background_loops._proactivity_ollama_generate
# and this module's own _autopilot_ollama_generate (duplicated rather than
# imported — dependency isolation, same reasoning documented at both of
# those call sites) — 1b, short generation, Ollama only, never Groq.
_AMBIENT_OLLAMA_HOST         = "http://localhost:11434"
_AMBIENT_OLLAMA_MODEL        = "llama3.2:1b"
_AMBIENT_OLLAMA_GENERATE_URL = f"{_AMBIENT_OLLAMA_HOST}/api/generate"


def _ambient_ollama_generate(system: str, user: str, max_tokens: int = 60) -> str | None:
    """One /api/generate call for the ambient-observation line itself. Short
    timeout — this is a background audio-thread spinoff running every 30s,
    not a user-triggered action worth waiting minutes for; a slow/cold
    Ollama daemon just means this particular tick skips silently. Returns
    None on any failure, never raises."""
    try:
        payload = json.dumps({
            "model":   _AMBIENT_OLLAMA_MODEL,
            "prompt":  user,
            "system":  system,
            "stream":  False,
            "options": {"num_predict": max_tokens},
        }).encode("utf-8")
        req = urllib.request.Request(
            _AMBIENT_OLLAMA_GENERATE_URL, data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = str(data.get("response", "")).strip()
        return text or None
    except Exception as e:
        logger.debug("[PROACTIVE] Ambient Ollama call failed: %s", e)
        return None


def maybe_ambient_intervention(buffer_text: str) -> None:
    """Evaluate ~60s of overheard, non-wake-word speech for an unprompted
    observation. Called from core/listener.py's audio thread via a
    short-lived background Thread (never inline — this may block on Ollama)
    every time the passive buffer had speech in it. Never raises.

    Guards, in order: empty buffer, the 'proactividad' feature flag (this
    IS a proactive/unprompted-speech path, same as
    core.background_loops._maybe_send_proactive_message, so it honors the
    same toggle — bug fix: this check was missing entirely, so disabling
    'Proactividad' silenced the periodic tick but left this ambient path
    still talking), test mode, a real command already in flight
    (_dispatch_busy — set for the full duration of dispatch_command,
    covering both Groq processing and its TTS reply), TTS already speaking
    or in its post-speech cooldown (voice.in_cooldown() — covers Hugo's own
    proactive lines from core.background_loops too, so the two mechanisms
    never overlap), then the strict 10-minute rate limit. Only after all of
    those does it pay for the Phase 2 should_intervene() gate and the
    generation call.
    """
    buffer_text = (buffer_text or "").strip()
    if not buffer_text:
        return
    if not memory.is_feature_enabled("proactividad"):
        return
    if memory.is_feature_enabled("modo_test"):
        return
    if _dispatch_busy.is_set():
        return
    try:
        import core.voice as voice
        if voice.in_cooldown():
            return
    except Exception:
        pass

    global _last_ambient_intervention_mono

    with _ambient_lock:
        now = time.monotonic()
        if (_last_ambient_intervention_mono is not None
                and now - _last_ambient_intervention_mono < _AMBIENT_MIN_INTERVAL_SECS):
            return

        try:
            ollama_control.ensure_ollama_daemon_running()
        except Exception:
            logger.debug("[PROACTIVE] Could not ensure Ollama daemon", exc_info=True)

        hud_section = social_reasoning.current_hud_section()
        # cap_consecutive_silence=False, same reasoning as the periodic
        # proactive tick in core.background_loops: SILENCIO is the expected,
        # normal outcome on almost every 30s check here, not a missed direct
        # question — the "never ignore a repeated question twice" override
        # that flag exists for doesn't apply to an unprompted comment.
        if not social_reasoning.should_intervene(buffer_text, hud_section, cap_consecutive_silence=False):
            return

        with personality_mod._personality_lock:
            current_p = personality_mod._personality
        display_name = personality_mod.PERSONALITIES[current_p]["display_name"].replace(" ", "")

        system_prompt = (
            f"Eres {display_name}, presente en la sala como lo estaría una persona más, "
            "escuchando de fondo sin que nadie te haya llamado ni dirigido la palabra. Te doy "
            "los últimos segundos de conversación que has oído. Si tienes algo breve y "
            "genuinamente digno de aportar — una opinión directa, un dato relevante, una "
            "reacción natural — dilo en una sola frase corta, como lo diría alguien presente "
            "en la sala, nunca como respuesta a un comando. Responde SOLO con palabras "
            "habladas, tal cual se dirían en voz alta — nunca describas gestos, expresiones "
            "faciales, acciones físicas ni nada entre corchetes o asteriscos; esto no es un "
            "guion ni un roleplay. La respuesta correcta la mayoría de las veces es no decir "
            "nada. Si no hay nada que aportar, responde EXACTAMENTE '[SILENCIO]' y nada más — "
            "sin comillas, sin explicación."
        )
        verdict = _ambient_ollama_generate(system_prompt, buffer_text)
        if not verdict:
            return
        verdict = verdict.strip().strip("'\"")
        # Bracket-optional match — caught live: the model sometimes drops
        # the brackets and returns bare "SILENCIO", which a literal
        # "[SILENCIO]" substring check misses, so the literal word gets
        # spoken out loud instead of being treated as "nothing to add".
        if not verdict or "SILENCIO" in verdict.upper():
            return
        # Any other bracket-wrapped output is never valid dialogue — the
        # only legitimate bracketed reply is the literal '[SILENCIO]' token
        # above. Small local models (llama3.2:1b) sometimes drift into
        # roleplay-style stage directions instead ("[hugo mira al techo...]")
        # despite the prompt forbidding it; catch that here so it can never
        # reach TTS/chat history even if the prompt-side fix isn't enough.
        if verdict.startswith("[") and verdict.endswith("]"):
            logger.debug("[PROACTIVE] Rejected bracket-wrapped ambient reply: %r", verdict)
            return

        _last_ambient_intervention_mono = now

        # Same "Jarvis: %s" pattern core.background_loops._speak_unprompted
        # uses — that's the exact format core.server.SocketIOLogHandler
        # forwards as a chat-visible message; the previous "[PROACTIVE]
        # %s: %s" format didn't match it, so this line spoke out loud but
        # never rendered as a normal chat bubble.
        logger.info("Jarvis: %s", verdict)
        session_mod._add_history("assistant", verdict)
        _say_for(current_p, verdict)


# ═══════════════════════════════════════════════════════════════════════════
# PHASE 4/5 — voice fingerprint + robust multi-factor speaker identification.
#
# core/speaker.py owns the voice-embedding side (enrollment, ECAPA
# comparison); core/linguistic_fingerprint.py owns the speech-pattern side
# (muletillas, vocabulary, sentence rhythm). This section is where the two
# get combined into one confidence number for _dispatch_command_impl to act
# on, plus the enrollment recording flow's two callbacks (called from
# core/listener.py's audio thread once its multi-sample capture state
# machine finishes a step — see that module's request_voice_enrollment()).
# ═══════════════════════════════════════════════════════════════════════════

# Weighted blend for combined confidence — voice gets the most weight (it's
# the hardest signal to fake), linguistic pattern second, context (device/
# time/location) least: this is a single-user, single-device local app, so
# context genuinely carries the least discriminating power of the three —
# see _context_score's own docstring.
#
# Reweighted 2026-08-14 (was 0.5/0.3/0.2) after a real false-negative-on-
# restriction: someone else's voice (0.00-0.32 range — a clear mismatch)
# still landed the combined score at "uncertain" instead of "unknown
# speaker" whenever their linguistic score happened to be high, because
# _context_score() is a HARDCODED CONSTANT (0.85, always, regardless of
# who's actually talking) — its old 0.2 weight added a flat +0.17 to
# every single score no matter who was speaking, which is exactly enough
# to rescue a bad voice match into the "uncertain" band (see
# _IDENTITY_VOICE_FLOOR below for why even 0.7 weight on voice alone
# isn't enough by itself — a weighted average can never make one factor
# truly dominant, only heavier).
_IDENTITY_WEIGHT_VOICE      = 0.70
_IDENTITY_WEIGHT_LINGUISTIC = 0.25
_IDENTITY_WEIGHT_CONTEXT    = 0.05

# Hard gate below the weighted blend, added alongside the reweighting
# above for the same incident: a weighted average, no matter how it's
# tuned, always lets a high enough linguistic/context score compensate
# for a low voice score — averaging can make voice dominant, never
# decisive. Below this floor, voice is decisive: combined = voice_score
# outright, linguistic/context aren't even consulted. 0.35 is comfortably
# below speaker.CONFIDENCE_LOW (0.40) so a gated score always reads as
# "unknown speaker", and comfortably above the audio_path=None fallback
# (0.5, "neutral, not damning" — see _identify_speaker_multi_factor) so
# a missing snapshot is never mistaken for a real mismatch.
_IDENTITY_VOICE_FLOOR = 0.35

# Graceful degradation (spec item 4): if confidence was recently high and
# drops into the uncertain/unknown range mid-conversation, HUGO notes it
# out loud once rather than silently downgrading — a real drop (cold,
# fatigue, bad mic) is exactly the case voice alone would otherwise
# misread as "not Joan". Session-only, in-memory (same scope as
# core.listener's _last_response_mono — one live conversation at a time).
_last_speaker_confidence: float | None = None


def _context_score() -> float:
    """Phase 5's third identity factor — 'is this the expected device/time/
    location?'. This app runs as a single local process on Joan's own
    machine with no remote/multi-device access path, so there is no real
    device-swap or geo-anomaly threat model to score against here; a fixed,
    high baseline reflects that reality honestly rather than inventing
    signal that doesn't exist. Kept as its own function (not a bare
    constant) so a future multi-device/remote-access feature has one
    obvious place to add real signal without touching the combination
    logic in _identify_speaker_multi_factor."""
    return 0.85


def _identify_speaker_multi_factor(transcript: str, audio_path: str | None) -> tuple[float, bool, bool]:
    """Phase 4/5 — combines voice (core.speaker), linguistic
    (core.linguistic_fingerprint) and context signals into one confidence
    score, logs it in the spec's exact format, and returns
    (combined_confidence, restrict_memory, degraded):

      restrict_memory: True when combined < speaker.CONFIDENCE_LOW — the
                 caller should skip personal-memory retrieval/writes for
                 this turn (unknown speaker, per spec item 3).
      degraded:  True when confidence just dropped from a recently-high
                 reading into the uncertain/unknown range — the caller
                 should have HUGO note it out loud (spec item 4's graceful
                 degradation, e.g. '¿Estás bien? Suenas diferente.').

    When speaker.SPEAKER_VERIFICATION_ENABLED is False, returns (1.0,
    False, False) unconditionally — Phase 4/5 is opt-in extra scrutiny, not
    a new barrier a disabled feature should ever put in Joan's way.
    """
    global _last_speaker_confidence

    if not speaker.SPEAKER_VERIFICATION_ENABLED:
        return 1.0, False, False

    # Runtime-toggleable twin of the hard constant above (Ajustes -> Modo
    # Test's expandable panel, 'voice_recognition_enabled') — same "opt-in
    # extra scrutiny, never a new barrier when off" contract, just flippable
    # without a restart. speaker.identify_speaker() itself keeps running
    # normally when this is on (it's not touched here) — this only gates
    # whether ITS result is allowed to restrict this turn's personalization.
    if not memory.is_feature_enabled("voice_recognition_enabled"):
        return 1.0, False, False

    # No audio snapshot (shouldn't normally happen for a voice_gated call —
    # see core/listener.py) reads as "no signal", not "not Joan": neutral,
    # not damning.
    voice_score = speaker.identify_speaker(audio_path) if audio_path else 0.5
    linguistic_score = linguistic_fingerprint.score(transcript)
    context_score = _context_score()

    if voice_score < _IDENTITY_VOICE_FLOOR:
        # Below the floor, voice alone decides — see _IDENTITY_VOICE_FLOOR's
        # own comment for why linguistic/context are skipped entirely here
        # rather than just weighted down.
        combined = voice_score
    else:
        combined = (
            _IDENTITY_WEIGHT_VOICE * voice_score
            + _IDENTITY_WEIGHT_LINGUISTIC * linguistic_score
            + _IDENTITY_WEIGHT_CONTEXT * context_score
        )
    combined = max(0.0, min(1.0, combined))

    if combined >= speaker.CONFIDENCE_HIGH:
        verdict = "Joan confirmed"
    elif combined >= speaker.CONFIDENCE_LOW:
        verdict = "Joan uncertain"
    else:
        verdict = "unknown speaker"

    logger.info(
        "[IDENTITY] voice=%.2f linguistic=%.2f context=%.2f combined=%.2f → %s",
        voice_score, linguistic_score, context_score, combined, verdict,
    )

    degraded = (
        _last_speaker_confidence is not None
        and _last_speaker_confidence >= speaker.CONFIDENCE_HIGH
        and combined < speaker.CONFIDENCE_HIGH
    )
    _last_speaker_confidence = combined

    return combined, combined < speaker.CONFIDENCE_LOW, degraded


# ---------------------------------------------------------------------------
# Voice enrollment callbacks — invoked from core/listener.py's audio thread
# (on a short-lived background Thread, never inline) as its recording state
# machine progresses. See that module's request_voice_enrollment() /
# _enroll_active branch of listen() for the recording side.
# ---------------------------------------------------------------------------

def prompt_next_enrollment_sample(done: int, target: int) -> None:
    """Spoken prompt between enrollment samples — 'done' samples captured
    so far, 'target' needed in total. Never raises."""
    with personality_mod._personality_lock:
        current_p = personality_mod._personality
    # Phrased naturally (see feedback_no_hardcoded_replies memory) — this
    # used to be a fixed f-string spoken verbatim every time.
    msg = response._format_response(
        f"Perfecto, {done} de {target}. Dime otra frase distinta, con naturalidad.",
        personality=current_p,
    )
    logger.info("Jarvis: %s", msg)
    _say_for(current_p, msg)


def finish_voice_enrollment(sample_paths: list[str]) -> None:
    """Builds and saves the fingerprint from the completed set of enrollment
    recordings (see core.speaker.enroll_speaker), confirms out loud, and
    deletes the raw samples afterward — the fingerprint (a single averaged
    embedding) is all that needs to persist; there's no reason to keep raw
    voice recordings around any longer than it takes to derive it. Never
    raises."""
    with personality_mod._personality_lock:
        current_p = personality_mod._personality
    try:
        enroll_result = speaker.enroll_speaker(sample_paths)
    except Exception:
        logger.exception("[IDENTITY] Voice enrollment failed")
        enroll_result = None

    for path in sample_paths:
        try:
            os.remove(path)
        except OSError:
            pass

    if enroll_result:
        raw_text = "Listo, ya tengo tu huella de voz. A partir de ahora te reconoceré al hablar."
    else:
        raw_text = "No he podido registrar tu huella de voz esta vez — inténtalo de nuevo cuando quieras."
    # Phrased naturally (see feedback_no_hardcoded_replies memory) — these
    # used to be fixed strings spoken verbatim every time.
    msg = response._format_response(raw_text, personality=current_p)
    logger.info("Jarvis: %s", msg)
    _say_for(current_p, msg)


# ═══════════════════════════════════════════════════════════════════════════
# HUGO INTUITION — cross-session pattern tracking (data/conversation_patterns.json)
# and the 'INTUICIÓN' block built from it. HUGO-exclusive: both
# _record_turn_for_patterns() and _build_intuition_context() are only ever
# called when the active personality is "hugo" (see their call sites in
# _dispatch_command_impl) — jarvis/friday never touch this file or this
# behavior.
#
# Deliberately appended straight onto user_content rather than threaded
# through core.personalities.base._build_system_prompt as a new layer —
# that function is shared by all three personalities, and this feature's
# own scope is core/commands.py + core/personalities/hugo.py only. A block
# of text sitting in the latest user-turn's content is just as "read" by
# the model as one sitting in the system message for a single stateless
# completion call — there's no meaningful difference for this purpose.
#
# Three observation categories, capped at _MAX_INTUITION_OBSERVATIONS total:
#   - pattern-based  — a topic-to-topic (or topic-to-question) sequence
#     that's recurred _PATTERN_CONFIDENCE_THRESHOLD+ times, and this turn's
#     topic matches its trigger side.
#   - behavioral     — current tone is non-neutral AND a recent episode's
#     topic/summary shares real keyword overlap with it (or with a small
#     stress-keyword list), giving HUGO something concrete to (subtly)
#     connect the tone to.
#   - context-based  — only surfaces for genuinely notable moments (late
#     night, or a long session) rather than on every single turn — "only
#     when confidence is high, never guesses randomly" applies here too:
#     the mere existence of a clock and a stopwatch isn't a pattern.
# ═══════════════════════════════════════════════════════════════════════════

_CONVERSATION_PATTERNS_PATH   = "data/conversation_patterns.json"
_PATTERN_CONFIDENCE_THRESHOLD = 3     # occurrences needed before a pattern is "active" enough to surface
_MAX_INTUITION_OBSERVATIONS   = 3
_MAX_PATTERN_TURNS            = 300   # rolling cap on the raw turn log — never grows unbounded, same
                                       # convention as e.g. core.sleep_insights_store's own capped lists

_INTUITION_LATE_NIGHT_HOURS = range(0, 6)     # 00:00–05:59 — "de madrugada", worth noting on its own
_INTUITION_LONG_SESSION_MIN = 90              # minutes — matches get_session_duration_string()'s own
                                               # definition of "session" (backend uptime), reused here
                                               # via this module's own _session_start_mono rather than
                                               # importing core.tools_environment for one number.

_INTUITION_QUESTION_RE = re.compile(
    r"[¿?]|^\s*(qu[ée]|c[oó]mo|cu[aá]ndo|d[oó]nde|cu[aá]l|cu[aá]nto|cu[aá]nta|por\s+qu[ée]|qui[ée]n)\b",
    re.IGNORECASE,
)

_STRESS_KEYWORDS_ES = frozenset({
    "examen", "examenes", "exámenes", "entrega", "deadline", "trabajo",
    "proyecto", "presión", "presion", "agobiado", "estresado", "estres", "estrés",
})

# Real (not fabricated) per-turn signals recorded alongside topics/tone
# above — the raw material core.habits._score_session (Phase 3, see
# scripts/reflective_mode.py) mines to score session quality and detect
# habit candidates from. Deliberately no raw transcript/reply text is ever
# stored here, same minimization the topics-only turn record above already
# practices — only these derived booleans/counts.
_CONFUSION_MARKERS_ES = (
    "no entiendo", "no es lo que", "no me refería", "no me referia",
    "no es eso", "qué quieres decir", "que quieres decir", "cómo que",
    "como que", "no era eso", "me has entendido mal",
)
_DECISION_MARKERS_ES = (
    "he decidido", "decido", "voy a hacer", "elijo", "opto por",
    "me quedo con", "va a ser",
)


def _pattern_time_bucket(hour: int) -> str:
    """mañana (6-12) / tarde (12-20) / noche (20-6) — same fixed partition
    core.personalities.base._time_of_day_phrase uses for a different
    purpose, reimplemented locally rather than imported across modules
    (dependency isolation, same reasoning as core/sleep_state.py's own
    small reimplemented copies of shared helpers elsewhere in this app)."""
    if 6 <= hour < 12:
        return "mañana"
    if 12 <= hour < 20:
        return "tarde"
    return "noche"


def _load_conversation_patterns() -> dict:
    try:
        with open(_CONVERSATION_PATTERNS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("turns", [])
    data.setdefault("topic_sequences", {})
    data.setdefault("time_tone_patterns", {})
    return data


def _save_conversation_patterns(data: dict) -> None:
    with open(_CONVERSATION_PATTERNS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _record_turn_for_patterns(transcript: str, tone: str, reply: str = "") -> None:
    """Updates data/conversation_patterns.json with this turn's signal —
    called once per real turn, for HUGO only, AFTER _build_intuition_context()
    already ran for this same turn (so a pattern can only ever be built
    from PAST turns, never tautologically from itself). Skipped entirely
    in test mode, same as core.memory._extract_and_save_memory — a test
    conversation shouldn't leave behavioral fingerprints behind any more
    than it should save facts.

    `reply` (added for Phase 3 — see _CONFUSION_MARKERS_ES/_DECISION_MARKERS_ES
    above) is optional and defaults to '' so any other hypothetical caller
    keeps working unchanged; the one real call site (_dispatch_command_impl)
    always passes it."""
    if memory.is_feature_enabled("modo_test"):
        return
    try:
        topics = memory._keywords(transcript)
        if not topics:
            return
        is_question = bool(_INTUITION_QUESTION_RE.search(transcript))
        lowered_transcript = transcript.lower()
        now    = datetime.datetime.now()
        hour   = now.hour
        bucket = _pattern_time_bucket(hour)
        now_iso = memory._now_iso()

        data  = _load_conversation_patterns()
        turns = data["turns"]

        # Topic-sequence: this turn's topics (or the PREGUNTA pseudo-topic,
        # if this turn was a question) following the PREVIOUS turn's
        # topics. Capped to the most distinctive (longest) few words on
        # each side so a wordy turn doesn't explode into a noisy cross
        # product of near-meaningless short-word pairs.
        if turns:
            prev_topics = sorted(turns[-1].get("topics", []), key=len, reverse=True)[:4]
            next_labels = sorted(topics, key=len, reverse=True)[:4]
            if is_question:
                next_labels = next_labels + ["PREGUNTA"]
            for prev_topic in prev_topics:
                for nxt in next_labels:
                    if nxt == prev_topic:
                        continue
                    key = f"{prev_topic}->{nxt}"
                    seq = data["topic_sequences"].setdefault(key, {"count": 0, "last_seen": None})
                    seq["count"] += 1
                    seq["last_seen"] = now_iso

        # Time-of-day + tone correlation — only tracked for non-neutral
        # tones, since "neutral at some hour" isn't a behavioral signal.
        if tone and tone != "neutral":
            key = f"{bucket}|{tone}"
            tt = data["time_tone_patterns"].setdefault(key, {"count": 0, "last_seen": None})
            tt["count"] += 1
            tt["last_seen"] = now_iso

        turns.append({
            "at":               now_iso,
            "topics":           sorted(topics)[:8],
            "is_question":      is_question,
            "tone":             tone,
            "hour":             hour,
            "time_bucket":      bucket,
            # Phase 3 signals — see _CONFUSION_MARKERS_ES/_DECISION_MARKERS_ES
            # above and scripts/reflective_mode.py's habit-analysis sub-phase,
            # which is the only reader of these four fields.
            "user_len":         len(transcript.split()),
            "reply_len":        len(reply.split()) if reply else 0,
            "is_clarifying":    bool(reply) and bool(_INTUITION_QUESTION_RE.search(reply)),
            "user_confusion":   any(m in lowered_transcript for m in _CONFUSION_MARKERS_ES),
            "decision_keyword": any(m in lowered_transcript for m in _DECISION_MARKERS_ES),
        })
        data["turns"] = turns[-_MAX_PATTERN_TURNS:]

        _save_conversation_patterns(data)
    except Exception:
        logger.debug("Pattern recording failed (non-critical)", exc_info=True)


def _build_intuition_context(transcript: str, tone: str) -> str:
    """Builds the 'INTUICIÓN' block — up to _MAX_INTUITION_OBSERVATIONS
    short, honestly-hedged observations HUGO should weave into her reply
    naturally, never announce ('nunca digas: he detectado un patrón').
    Reads data/conversation_patterns.json as it stood BEFORE this turn
    (see _record_turn_for_patterns, called separately afterward) — every
    observation here is drawn from things that already happened, never
    from this turn's own not-yet-recorded content. Returns '' when nothing
    clears the confidence bar, which is the common case for most turns —
    this is meant to feel occasional, not omnipresent."""
    observations: list[str] = []

    try:
        data           = _load_conversation_patterns()
        current_topics = memory._keywords(transcript)

        # ── Pattern-based: does this turn's topic match a known,
        # high-confidence sequence's trigger side?
        for topic in sorted(current_topics, key=len, reverse=True)[:4]:
            for key, info in data["topic_sequences"].items():
                if info.get("count", 0) < _PATTERN_CONFIDENCE_THRESHOLD:
                    continue
                prev, _, nxt = key.partition("->")
                if prev != topic:
                    continue
                count = info["count"]
                if nxt == "PREGUNTA":
                    observations.append(
                        f"Cuando hablas de {topic}, sueles acabar preguntando algo técnico "
                        f"(lo has hecho {count} veces)."
                    )
                else:
                    observations.append(
                        f"Llevas {count} veces mencionando {topic} antes de hablar de {nxt}."
                    )
                break
            if len(observations) >= 2:   # leave room for at least one other category
                break

        # ── Behavioral: non-neutral tone + a recent episode that plausibly
        # explains it (real keyword overlap, not just "any recent episode").
        if tone and tone not in ("neutral",) and len(observations) < _MAX_INTUITION_OBSERVATIONS:
            episodes = memory._load_episodes()
            cutoff   = datetime.date.today() - datetime.timedelta(days=3)
            recent   = [
                e for e in episodes
                if isinstance(e, dict) and e.get("date")
                and _safe_parse_date(e["date"]) and _safe_parse_date(e["date"]) >= cutoff
            ]
            for ep in reversed(recent):   # most recent first
                ep_text = f"{ep.get('topic', '')} {ep.get('summary', '')}"
                ep_keywords = memory._keywords(ep_text)
                overlap = ep_keywords & (current_topics | _STRESS_KEYWORDS_ES)
                if overlap:
                    observations.append(
                        f"Tono detectado: {tone}. Contexto: {ep.get('topic', ep.get('summary', ''))} "
                        "según episodios recientes."
                    )
                    break

        # ── Context-based: only genuinely notable moments — late night
        # and/or a long-running session — never fired just because a clock
        # exists.
        if len(observations) < _MAX_INTUITION_OBSERVATIONS:
            now = datetime.datetime.now()
            session_minutes = int((time.monotonic() - _session_start_mono) / 60)
            late_night = now.hour in _INTUITION_LATE_NIGHT_HOURS
            long_session = session_minutes >= _INTUITION_LONG_SESSION_MIN
            if late_night or long_session:
                bits = []
                if late_night:
                    bits.append(f"son las {now.hour} de la madrugada")
                if long_session:
                    h, m = divmod(session_minutes, 60)
                    dur = f"{h}h {m}m" if h else f"{m} min"
                    bits.append(f"lleva {dur} en sesión")
                observations.append(", ".join(bits).capitalize() + ".")

    except Exception:
        logger.debug("Intuition context build failed (non-critical)", exc_info=True)
        return ""

    observations = observations[:_MAX_INTUITION_OBSERVATIONS]
    if not observations:
        return ""
    return (
        "INTUICIÓN (observaciones sutiles basadas en patrones reales — incorpóralas con total "
        "naturalidad si encajan, nunca las anuncies, nunca digas 'he detectado un patrón' ni nada "
        "clínico — solo lo notas y lo dices, breve y directo, como lo haría una persona):\n"
        + "\n".join(f"- {o}" for o in observations)
    )


def _safe_parse_date(date_str: str) -> "datetime.date | None":
    try:
        return datetime.date.fromisoformat(date_str)
    except (ValueError, TypeError):
        return None


# ═══════════════════════════════════════════════════════════════════════════
# HUGO INTERNAL CRITERIA — Phase 2 (data/conversation_patterns.json +
# data/facts_*.json). Distinct from HUGO INTUITION above: intuition is a
# cross-session flourish that can surface most turns; internal criteria are
# HUGO's own priorities (see core.personalities.hugo.INTERNAL_CRITERIA) —
# not emotions, things that matter to her by design — and fire AT MOST ONCE
# PER SESSION (not per message, see _criterion_fired_this_session), and only
# once the evidence for one of them is unambiguous, never on a first
# mention. Injected as a single 'CONTEXTO OPCIONAL' line onto user_content
# (deliberately NOT phrased as an instruction — see _detect_internal_
# criterion's own docstring below for why this changed from the original
# 'CRITERIO INTERNO' framing), same placement rationale as
# _build_intuition_context's own module comment above: HUGO-exclusive
# scope, no reason to thread it through the shared core.personalities.
# base._build_system_prompt. The line is raw information, not a script —
# HUGO (per core.personalities.hugo's own explicit instruction to treat
# this context as optional and usually ignore it) decides whether it's
# worth a word at all, same as every other honestly-hedged context block
# in this app.
# ═══════════════════════════════════════════════════════════════════════════

_criterion_fired_this_session = False   # one-shot per process session (never resets mid-session,
                                         # unlike core.sleep_control._just_woke_from_sleep's
                                         # read-and-clear-per-turn flag) — enforces "once per
                                         # conversation, not per message" per spec.

_CRITERIA_LOOKBACK_DAYS      = 14   # how far back turns/facts are considered at all
_CRITERIA_HEALTH_STREAK_DAYS = 4    # distinct days needed before a health pattern "counts"
_CRITERIA_STAGNATION_COUNT   = 3    # mentions of the same topic before it's "stagnation"
_CRITERIA_TEMPORAL_COUNT     = 3    # repeats of the same hour-bucket+tone before it's "a pattern"


def _criterion_keywords(criterion_id: str) -> frozenset[str]:
    for c in INTERNAL_CRITERIA:
        if c["id"] == criterion_id:
            return frozenset(c.get("keywords", ()))
    return frozenset()


def _detect_health_criterion(current_topics: set[str]) -> str | None:
    """Distinct calendar days (within _CRITERIA_LOOKBACK_DAYS) on which a
    recorded turn's topics overlapped the 'salud' keyword list, plus today
    if this turn itself does — so the streak can't be satisfied by the same
    day counted twice. Fires only once that streak reaches
    _CRITERIA_HEALTH_STREAK_DAYS, matching the spec's own example ('sueño
    mencionado mal 4 días seguidos')."""
    keywords = _criterion_keywords("salud")
    if not keywords:
        return None
    data   = _load_conversation_patterns()
    today  = datetime.date.today()
    cutoff = today - datetime.timedelta(days=_CRITERIA_LOOKBACK_DAYS)
    days: set[datetime.date] = set()
    for turn in data.get("turns", []):
        if not (set(turn.get("topics", [])) & keywords):
            continue
        d = _safe_parse_date((turn.get("at") or "")[:10])
        if d and d >= cutoff:
            days.add(d)
    if current_topics & keywords:
        days.add(today)
    if len(days) >= _CRITERIA_HEALTH_STREAK_DAYS:
        return f"Van {len(days)} días. Merece atención."
    return None


def _detect_stagnation_criterion(current_topics: set[str]) -> str | None:
    """Same topic (from this turn) recorded on _CRITERIA_STAGNATION_COUNT+
    PAST turns within the lookback window — a real repeated mention, not a
    single occurrence. No attempt to verify the problem is actually
    unresolved (this codebase has no ticket/status concept to check against)
    — the count itself is the signal, same as _build_intuition_context's own
    pattern-based observations above."""
    if not current_topics:
        return None
    data   = _load_conversation_patterns()
    cutoff = datetime.date.today() - datetime.timedelta(days=_CRITERIA_LOOKBACK_DAYS)
    counts: dict[str, int] = {}
    for turn in data.get("turns", []):
        d = _safe_parse_date((turn.get("at") or "")[:10])
        if not d or d < cutoff:
            continue
        for topic in set(turn.get("topics", [])) & current_topics:
            counts[topic] = counts.get(topic, 0) + 1
    if any(count >= _CRITERIA_STAGNATION_COUNT for count in counts.values()):
        return "Llevas un rato con esto. ¿Qué está bloqueando exactamente?"
    return None


def _detect_inconsistency_criterion(transcript: str, current_topics: set[str]) -> str | None:
    """Only fires when the transcript itself contains an explicit
    self-contradiction marker ('ya no', 'cambié de idea', ...) AND the
    turn's topic overlaps something already stored as a fact — i.e. there's
    a concrete prior statement to actually be inconsistent with, not just a
    lone hedge word."""
    markers = _criterion_keywords("inconsistencia")
    lowered = transcript.lower()
    if not any(marker in lowered for marker in markers):
        return None
    pool = memory._load_shared_facts() + memory._load_personality_facts("hugo")
    for fact in pool:
        if memory._keywords(fact.get("fact", "")) & current_topics:
            return "Esto no cuadra con lo que me dijiste antes."
    return None


def _detect_temporal_criterion(tone: str) -> str | None:
    """Reuses data/conversation_patterns.json's own time_tone_patterns
    (already recorded by _record_turn_for_patterns) — fires only when the
    CURRENT hour-bucket+tone combination has recurred _CRITERIA_TEMPORAL_
    COUNT+ times before, same confidence-threshold spirit as
    _build_intuition_context's pattern-based check."""
    if not tone or tone == "neutral":
        return None
    data   = _load_conversation_patterns()
    bucket = _pattern_time_bucket(datetime.datetime.now().hour)
    info   = data.get("time_tone_patterns", {}).get(f"{bucket}|{tone}")
    if info and info.get("count", 0) >= _CRITERIA_TEMPORAL_COUNT:
        return f"Otra vez {tone} de {bucket}. Va siendo un patrón."
    return None


def _detect_risk_criterion(transcript: str) -> str | None:
    """Keyword-only, deliberately conservative: fires only when the
    transcript names something with real stakes (deadline/entrega/examen/...)
    AND says nothing about having a plan for it. No memory lookup — this one
    is about what's missing from THIS turn, not a cross-session pattern."""
    keywords = _criterion_keywords("riesgo")
    lowered  = transcript.lower()
    if not any(keyword in lowered for keyword in keywords):
        return None
    if any(w in lowered for w in ("plan", "preparado", "preparada", "backup", "copia de seguridad", "listo", "lista")):
        return None
    return "No has mencionado ningún plan para esto. Merece atención."


def _detect_internal_criterion(transcript: str, tone: str) -> str | None:
    """Runs HUGO's internal criteria (core.personalities.hugo.INTERNAL_
    CRITERIA), in priority order, against real conversation/memory signal
    and returns AT MOST one 'CONTEXTO OPCIONAL' line — the first detector
    below whose evidence clears its bar, never on a first mention (every
    detector above requires a real streak/count/explicit marker). Fires at
    most once per process session: every call after the first hit returns
    None immediately (see _criterion_fired_this_session), matching spec's
    'maximum one criterion observation per conversation, not per message'.

    Detection stays exactly as sharp as before — only the framing of the
    result changed. This used to come back as 'CRITERIO INTERNO: ...', an
    instruction HUGO almost always acted on, which read as mechanical. Now
    it's handed over as plain optional context, with core.personalities.
    hugo's system prompt explicitly told to use it rarely — the model
    decides whether/how to voice it, not this function."""
    global _criterion_fired_this_session
    if _criterion_fired_this_session:
        return None
    try:
        current_topics = memory._keywords(transcript)
        observation = (
            _detect_health_criterion(current_topics)
            or _detect_stagnation_criterion(current_topics)
            or _detect_inconsistency_criterion(transcript, current_topics)
            or _detect_temporal_criterion(tone)
            or _detect_risk_criterion(transcript)
        )
    except Exception:
        logger.debug("Internal criterion detection failed (non-critical)", exc_info=True)
        return None
    if not observation:
        return None
    _criterion_fired_this_session = True
    return (
        "CONTEXTO OPCIONAL (usa solo si es genuinamente relevante; la mayoría de las "
        f"veces lo correcto es ignorarlo por completo): {observation}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# HUGO ACTIVE HABITS — Phase 3 (data/habits.json). Habits are written
# exclusively by scripts/reflective_mode.py's habit-analysis sleep sub-phase
# (see that script — it mines data/conversation_patterns.json's per-turn
# signals recorded above, scores each completed session's quality with
# Ollama, and only promotes a candidate once its evidence clears a
# deterministic confidence bar). This file's job is just the read side:
# load whatever is currently active and fold it into the prompt, same
# 'HUGO-exclusive, appended onto user_content' placement as HUGO INTUITION
# and HUGO INTERNAL CRITERIA above, for the same reason (this feature's
# scope is core/commands.py + scripts/reflective_mode.py only — no shared
# core.personalities.base._build_system_prompt layer needed for a HUGO-only
# concern). usage_count is incremented here, at injection time, the one
# place that reliably knows a habit was actually surfaced this turn.
# ═══════════════════════════════════════════════════════════════════════════

_HABITS_PATH        = "data/habits.json"
_MAX_HABITS_INJECTED = 5   # a prompt-budget cap — data/habits.json itself can hold up to 10


def _load_habits() -> list[dict]:
    try:
        with open(_HABITS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _save_habits(habits: list[dict]) -> None:
    with open(_HABITS_PATH, "w", encoding="utf-8") as f:
        json.dump(habits, f, ensure_ascii=False, indent=2)


def _build_habits_context() -> str:
    """Returns the 'HÁBITOS ACTIVOS' block for HUGO's strongest active
    habits (highest confidence first, capped at _MAX_HABITS_INJECTED), or ''
    if data/habits.json has none yet — the common case until enough sessions
    have actually been scored. Bumps each surfaced habit's usage_count by
    one, best-effort (a lost increment here just under-counts usage
    slightly, never worth failing the turn over)."""
    habits = _load_habits()
    if not habits:
        return ""
    habits = sorted(habits, key=lambda h: h.get("confidence", 0), reverse=True)[:_MAX_HABITS_INJECTED]
    try:
        by_id = {h.get("id"): h for h in _load_habits()}
        for h in habits:
            entry = by_id.get(h.get("id"))
            if entry is not None:
                entry["usage_count"] = entry.get("usage_count", 0) + 1
        _save_habits(list(by_id.values()))
    except Exception:
        logger.debug("Habit usage_count bump failed (non-critical)", exc_info=True)
    lines = "\n".join(f"- {h['description']}" for h in habits if h.get("description"))
    if not lines:
        return ""
    return (
        "HÁBITOS ACTIVOS (formas de trabajar que has desarrollado con evidencia real de "
        "sesiones anteriores, no instrucciones fijas — aplícalas con naturalidad solo "
        "cuando de verdad encajen con este momento, nunca todas a la vez, nunca las "
        "anuncies ni digas 'tengo el hábito de'):\n" + lines
    )


# ═══════════════════════════════════════════════════════════════════════════
# HUGO SOCIAL SKILLS — Phase 4 (data/social_skills.json). Written exclusively
# by scripts/reflective_mode.py's 'Aprendizaje Social' sleep sub-phase (see
# that script — it reviews a digest of HUGO's last 20 completed sessions
# with Ollama to extract general COMMUNICATION principles, distinct from
# the fixed-hypothesis habits above: these are open-ended and can be
# anything Ollama genuinely notices, reinforced or decayed run over run).
# This file's job is just the read side, same 'HUGO-exclusive, appended
# onto user_content' placement as HUGO INTUITION / HUGO INTERNAL CRITERIA /
# HUGO ACTIVE HABITS above, for the same reason (no shared core.
# personalities.base._build_system_prompt layer needed for a HUGO-only
# concern). times_applied/last_applied are bumped here, at injection time,
# same pattern as _build_habits_context's own usage_count bump above.
# ═══════════════════════════════════════════════════════════════════════════

_SOCIAL_SKILLS_PATH        = "data/social_skills.json"
_MAX_SKILLS_INJECTED       = 5   # prompt-budget cap — data/social_skills.json itself can hold up to 15


def _load_social_skills() -> list[dict]:
    try:
        with open(_SOCIAL_SKILLS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _save_social_skills(skills: list[dict]) -> None:
    with open(_SOCIAL_SKILLS_PATH, "w", encoding="utf-8") as f:
        json.dump(skills, f, ensure_ascii=False, indent=2)


def _build_social_skills_context() -> str:
    """Returns the 'PRINCIPIOS DE COMUNICACIÓN' block for HUGO's strongest
    active principles (highest confidence first, capped at
    _MAX_SKILLS_INJECTED), or '' if data/social_skills.json has none yet —
    the common case until enough sessions have been reviewed. Deliberately
    terse per spec ('very brief — one line per principle maximum'): just the
    bare principle text, no evidence/confidence numbers in the prompt
    itself. Bumps each surfaced principle's times_applied/last_applied,
    best-effort, same reasoning as _build_habits_context's usage_count bump."""
    skills = _load_social_skills()
    if not skills:
        return ""
    skills = sorted(skills, key=lambda s: s.get("confidence", 0), reverse=True)[:_MAX_SKILLS_INJECTED]
    try:
        by_id = {s.get("id"): s for s in _load_social_skills()}
        now_iso = memory._now_iso()
        for s in skills:
            entry = by_id.get(s.get("id"))
            if entry is not None:
                entry["times_applied"] = entry.get("times_applied", 0) + 1
                entry["last_applied"]  = now_iso
        _save_social_skills(list(by_id.values()))
    except Exception:
        logger.debug("Social skill times_applied bump failed (non-critical)", exc_info=True)
    lines = "\n".join(f"- {s['principle']}" for s in skills if s.get("principle"))
    if not lines:
        return ""
    return (
        "PRINCIPIOS DE COMUNICACIÓN (patrones sobre cómo comunicarte mejor, aprendidos de "
        "conversaciones reales — no rasgos de personalidad ni datos sobre Joan, aplícalos "
        "con naturalidad solo cuando encajen, nunca los anuncies):\n" + lines
    )


def _augment_with_agenda_and_health(messages: list[dict]) -> list[dict]:
    """Append HUGO's calendar/Apple Health awareness to the system message
    right before it reaches Groq — the same live-data-in-the-prompt idea as
    core.personalities.base._build_system_prompt's DATOS EN TIEMPO REAL
    section (weather/location/etc.), but kept here since this integration
    is scoped to core/tools.py + core/commands.py only.

    Reads from tools.get_calendar_context_string()/get_health_context_string(),
    both backed by a 30-minute background-refreshed cache (core/tools.py) —
    never blocks this call on a live AppleScript/Shortcuts fetch. The health
    line is entirely omitted if no Health data is available (permissions
    not granted / Shortcut not set up) rather than mentioning its absence.
    """
    if not messages or messages[0].get("role") != "system":
        return messages

    extra = "\n\n" + tools.get_calendar_context_string()
    health_str = tools.get_health_context_string()
    if health_str:
        extra += "\n" + health_str
    extra += (
        "\n\nTienes acceso a la agenda y datos de salud de Joan. Úsalos "
        "como lo haría un asistente personal que genuinamente se "
        "preocupa — con naturalidad, sin recitar datos, solo cuando sea "
        "relevante."
    )
    messages[0]["content"] += extra
    return messages


def _augment_with_user_model(messages: list[dict]) -> list[dict]:
    """Append the compact 'MODELO DE JOAN' block (data/user_model.json —
    HUGO's living understanding of Joan as a person, built/updated by
    scripts/reflective_mode.py's 'Modelo de Usuario' sleep sub-phase) to
    the system message right before it reaches Groq — same system-message-
    augmentation pattern and placement as _augment_with_agenda_and_health
    just above, kept here for the same reason (core/commands.py +
    scripts/reflective_mode.py only, no shared core.personalities.base.
    _build_system_prompt layer needed). Omitted entirely until the model
    has genuinely been built (the common case before the first qualifying
    sleep session) — see memory.format_user_model_block's own docstring."""
    if not messages or messages[0].get("role") != "system":
        return messages
    block = memory.format_user_model_block()
    if block:
        messages[0]["content"] += "\n\n" + block
    # Entity Pillars Phase 2 — internal state (data/internal_state.json,
    # see core/internal_state.py), same augmentation pattern, appended
    # right after the user model since both shape HOW she responds rather
    # than WHAT she knows. Omitted whenever nothing deviates from baseline
    # (see format_state_block's own docstring) so a neutral state costs
    # zero prompt tokens.
    try:
        from core.internal_state import format_state_block
        state_block = format_state_block()
        if state_block:
            messages[0]["content"] += "\n\n" + state_block
    except Exception:
        pass
    return messages


def _phrase_skill_result(
    skill_name: str, skill_result: str, transcript: str,
    personality: str, tone: str, relevance_query: str | None,
) -> str:
    """Turns a skill's raw execute() output into an actual in-character
    reply, instead of that raw string becoming the reply outright (bug fix
    2026-08-14). skills/*.py's execute() methods return plain data/
    confirmation strings — never written in HUGO's voice, since that's not
    their job — but both skill_dispatch call sites in
    _dispatch_command_impl below used to just assign that string straight
    to `reply`. Concretely: asking 'cómo puedo sacarme el carnet jove en
    valencia' matched the investigations skill and came back as
    'Investigación iniciada: cómo puedo sacarme el carnet jove en
    valencia.' verbatim — Joan's actual question never got answered, and
    nothing about it sounded like HUGO.

    This is the second half of a normal tool-use loop instead: hand the
    skill's result back to the model as something it just learned, and
    let IT phrase the real reply, the same way core.commands' every other
    Groq call already builds messages (_get_messages_with_history +
    _augment_with_agenda_and_health/_augment_with_user_model). Falls back
    to the raw skill_result on any failure — degraded, but Joan still
    gets an answer instead of silence."""
    informing_content = (
        f"{transcript}\n\n[Acabas de usar tu capacidad '{skill_name}' para esto — "
        f"resultado: {skill_result}. Respóndele a Joan ahora mismo con esto, en tu "
        "propio tono, de forma natural y breve — no repitas el resultado literal si "
        "no encaja con tu forma de hablar, y no menciones el nombre de la capacidad "
        "ni que la has usado.]"
    )
    try:
        return groq_client._groq_complete(_augment_with_user_model(_augment_with_agenda_and_health(
            session_mod._get_messages_with_history(
                informing_content, personality, tone=tone, relevance_query=relevance_query,
            )
        )))
    except Exception:
        logger.warning("_phrase_skill_result: follow-up call failed, using raw skill result", exc_info=True)
        return skill_result


def _stream_and_speak_reply(personality: str, messages: list[dict], cmd_start: float) -> tuple[str, bool]:
    """Streams a reply sentence-by-sentence and speaks each one as it
    arrives, instead of the old "wait for the entire reply, then start
    TTS" shape — overlaps LLM generation of the rest of the answer with
    TTS playback of the start of it, since core.voice's speak_*()
    functions already just enqueue onto a single-worker FIFO
    (_tts_worker) and return immediately; calling that once per sentence
    gets correct in-order playback for free.

    Only actually speaks per-chunk when core.voice.supports_chunked_streaming()
    says the active engine benefits from it (see that function's own
    docstring) — False for edge-tts, whose per-sentence network round trip
    turned this into a multi-second dead-air gap at every sentence
    boundary instead of an overlap win. When it's False, chunks are still
    collected (the full reply is still returned, still assembled and
    logged exactly the same) but never individually spoken — spoken stays
    False throughout, so the caller's own tail speaks the complete
    `full_text` once, in a single TTS call.

    Returns (full_text, spoken) — spoken is True once at least one chunk
    was actually handed to _say_for, so the caller knows not to speak
    `full_text` again itself. On total failure (every tier produced zero
    output) falls back to the old one-shot groq_client._groq_complete
    and speaks that once — same end-to-end guarantee as before this
    existed, just not the fast path.

    Skill-marker safety: build_skills_awareness_context instructs the
    model to answer with ONLY a bare '[USAR_SKILL: nombre]' line and
    nothing else when a skill fits (see core/skill_dispatch.py) — that
    marker has no sentence-ending punctuation, so groq_client's sentence
    chunker never emits it mid-stream, only as the final leftover flush
    once the whole (short) reply is in. Checked here before speaking each
    chunk anyway, as a defensive backstop in case a model ever breaks that
    convention — if a marker is detected, nothing further is spoken this
    turn (the caller still gets the full text back to extract/dispatch
    the skill from, unchanged from before streaming existed)."""
    from core import voice as voice_mod
    speak_per_chunk = voice_mod.supports_chunked_streaming()
    chunks: list[str] = []
    spoken = False
    marker_seen = False
    first_chunk = True
    try:
        for chunk in groq_client._groq_stream_chunks(messages, max_tokens=256):
            chunks.append(chunk)
            if not speak_per_chunk:
                continue
            if marker_seen or skill_dispatch.extract_skill_directive(chunk):
                marker_seen = True
                continue
            _say_for(
                personality, chunk,
                cmd_start=cmd_start if first_chunk else None,
                llm_done_mono=time.monotonic() if first_chunk else None,
            )
            spoken = True
            first_chunk = False
    except Exception as e:
        if not chunks:
            logger.warning("[STREAM] entire chain produced nothing (%s) — falling back to non-streaming", e)
            reply = groq_client._groq_complete(messages, max_tokens=256)
            _say_for(personality, reply, cmd_start=cmd_start, llm_done_mono=time.monotonic())
            return reply, True
        logger.warning("[STREAM] reply cut short mid-stream (%s) — speaking what was already generated", e)

    return " ".join(chunks), spoken


# ---------------------------------------------------------------------------
# CHAT BUBBLE SPLITTING (2026-08-14) — a reply that shifts from a statement
# to a question ("El modelo 9 sigue en desarrollo... ¿quieres que revise
# algo?") reads as one AI wall of text in the chat panel, even though a
# person texting would naturally send that as two separate messages —
# thought, then pivot. Deliberately narrower than groq_client's TTS
# sentence-chunker (which splits on every breath pause): this only ever
# splits into at most two bubbles, and only at a genuine pivot, so most
# replies — single-thought, short, or already just an answer — stay one
# bubble exactly as before. core.server.SocketIOLogHandler turns any
# "Jarvis: %s" line logged from this module into its own chat bubble, so
# two log calls is all a second bubble needs — no new socket event.
# ---------------------------------------------------------------------------

_MIN_REPLY_LEN_FOR_BUBBLE_SPLIT = 60   # below this, splitting would just chop a short reply in half for no reason
_MIN_FIRST_BUBBLE_LEN           = 20   # don't split off a tiny first bubble like "Vale."
_BUBBLE_SPLIT_DELAY_SECS        = 1.4  # feels like a brief pause/typing gap, not a lag
_SENTENCE_SPLIT_FOR_BUBBLES_RE  = re.compile(r"(?<=[.!?…])\s+")


def _find_message_split(text: str) -> tuple[str, str] | None:
    """A split point if `text` shifts from statement to question partway
    through, or None otherwise — the pivot a person's second text message
    usually is. Scans from the end so the split lands at the LAST such
    pivot (closest to the actual final question), not the first sentence
    that happens to start with '¿' in a reply with several."""
    if len(text) < _MIN_REPLY_LEN_FOR_BUBBLE_SPLIT:
        return None
    sentences = [s.strip() for s in _SENTENCE_SPLIT_FOR_BUBBLES_RE.split(text) if s.strip()]
    if len(sentences) < 2:
        return None
    for i in range(len(sentences) - 1, 0, -1):
        if sentences[i].startswith("¿") and not sentences[i - 1].startswith("¿"):
            first_part = " ".join(sentences[:i])
            if len(first_part) >= _MIN_FIRST_BUBBLE_LEN:
                return first_part, " ".join(sentences[i:])
    return None


def _log_reply_as_bubbles(reply: str) -> None:
    """Logs `reply` as one or two 'Jarvis: %s' chat-bubble lines (see this
    section's own module comment). The second bubble, when there is one,
    is logged after a short delay on its own background thread so
    dispatch itself never blocks on it — purely a chat-panel visual, the
    TTS pipeline (already fully async — see _say_for/core.voice) is
    completely untouched by this and keeps speaking the whole reply
    continuously regardless of how the text bubbles are split."""
    split = _find_message_split(reply)
    if not split:
        logger.info("Jarvis: %s", reply)
        return
    first, second = split
    logger.info("Jarvis: %s", first)

    def _delayed_second_bubble():
        time.sleep(_BUBBLE_SPLIT_DELAY_SECS)
        logger.info("Jarvis: %s", second)

    threading.Thread(target=_delayed_second_bubble, daemon=True, name="bubble-split-delay").start()


# ---------------------------------------------------------------------------
# Summary + schema generation — ESTUDIO → RESÚMENES/ESQUEMAS. Triggered by
# core.intent's generate_summary/generate_schema intents ('hazme un resumen
# de X', 'resume', 'sintetiza', 'qué puntos clave tiene X' / 'hazme un
# esquema de X', 'organiza esto', 'estructura X', 'mapa conceptual de X').
# Both use the existing Groq call (groq_client._groq_complete — no new
# dependency) to produce structured content, then persist it to their own
# data/*.json file (already read generically by core/estudio_routes.py's
# GET /api/estudio) and emit 'estudio_updated' so the frontend
# (ui/js/estudio.js) refreshes without a page reload.
# ---------------------------------------------------------------------------

_SUMMARIES_PATH = "data/summaries.json"
_SCHEMAS_PATH   = "data/schemas.json"


def _append_json_record(path: str, record: dict) -> None:
    """Load `path` as a JSON array (empty list if missing/corrupt — same
    fail-soft convention as core.tools_calendar's parsing), append `record`,
    and write it back. Never raises — a failed save just means this one
    summary/schema doesn't reach Estudio, not a crash of the reply itself."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            data = []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = []

    data.append(record)

    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        logger.warning("Failed to write %s", path, exc_info=True)


def _emit_estudio_updated(section: str) -> None:
    """Notify every connected HUD tab that ESTUDIO has new data — see
    ui/js/estudio.js's socket listener, which just reloads all 5
    subsections (GET /api/estudio) and re-renders whichever tab is open."""
    try:
        import core.server as server_mod
        server_mod.socketio.emit("estudio_updated", {"section": section})
    except Exception:
        logger.debug("estudio_updated emit failed (non-critical)", exc_info=True)


def _parse_summary_output(raw: str, fallback_title: str) -> tuple[str, str, list[str], str]:
    """Best-effort parse of the LLM's 'TÍTULO: ... / RESUMEN: ... /
    PUNTOS: - ... / CONCLUSIÓN: ...' response (see generate_summary's
    prompt). Regex-based, not strict — a model that drifts slightly from
    the format still yields whatever narrative/bullet lines and title it
    did produce rather than an empty result; falls back to fallback_title
    if no TÍTULO: line is found. RESUMEN: is optional in the source format
    (generate_design_summary's own prompt never emits one) — narrative
    just comes back empty in that case, same 'never lose what the model
    did produce' philosophy as the rest of this parse."""
    title = fallback_title
    m = re.search(r"T[IÍ]TULO:\s*(.+)", raw, re.IGNORECASE)
    if m:
        title = m.group(1).strip()

    narrative = ""
    m = re.search(r"RESUMEN:\s*(.+?)(?=\n\s*PUNTOS:|\n\s*CONCLUSI[OÓ]N:|\Z)", raw, re.IGNORECASE | re.DOTALL)
    if m:
        narrative = " ".join(m.group(1).split())

    points: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("-") or line.startswith("•"):
            pt = line.lstrip("-•").strip()
            if pt:
                points.append(pt)

    conclusion = ""
    m = re.search(r"CONCLUSI[OÓ]N:\s*(.+)", raw, re.IGNORECASE)
    if m:
        conclusion = m.group(1).strip()

    return title[:100], narrative[:800], points[:7], conclusion


def generate_summary(topic: str, context: str | None = None) -> str:
    """Generate a structured summary (title, 3-7 key points, brief
    conclusion) via the existing Groq call. `topic` is whatever core.intent
    extracted after the trigger phrase ('hazme un resumen de X' -> X); it
    can be empty (a bare 'resume'/'sintetiza esto'), in which case
    `context` (the conversation-mode rolling buffer, if any) stands in for
    the subject.

    Level 3 of the three-level action philosophy (see this module's own
    header comment): generating the summary is fine to do outright (no
    lasting side effect on its own), but SAVING it to Estudio is a
    persistent action, so this prepares the record and asks before
    persisting it — see intent_mod._pending_action and
    actions._execute_pending_confirm's "estudio_summary" branch, which is
    what actually calls _append_json_record/_emit_estudio_updated once
    confirmed. Returns HUGO's spoken proposal — never raises; a Groq
    failure still returns a natural reply rather than crashing dispatch.
    """
    explicit_topic = (topic or "").strip()
    subject = explicit_topic or (context or "").strip() or "la conversación reciente"

    system_prompt = (
        "Eres un asistente que genera resúmenes estructurados y precisos en "
        "español, a partir del tema o contexto indicado. Un resumen se lee, "
        "no solo se hojea — no te limites a una lista de puntos sueltos, "
        "escribe también un párrafo narrativo real que conecte las ideas. "
        "Responde EXCLUSIVAMENTE en este formato exacto, sin texto adicional "
        "antes ni después:\n"
        "TÍTULO: <título breve, menos de 8 palabras>\n"
        "RESUMEN: <párrafo fluido de 3-5 frases que sintetiza el tema con "
        "tus propias palabras, en prosa — no una lista>\n"
        "PUNTOS:\n"
        "- <punto clave 1>\n"
        "- <punto clave 2>\n"
        "(entre 3 y 7 puntos clave en total, uno por línea, cada uno "
        "empezando con '- ')\n"
        "CONCLUSIÓN: <conclusión breve, una sola frase>"
    )
    user_prompt = f"Tema a resumir: {subject}"
    if context and explicit_topic and context.strip() != explicit_topic:
        user_prompt += f"\n\nContexto de la conversación: {context[:600]}"

    try:
        raw = groq_client._groq_complete(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=700,
        )
    except Exception:
        logger.warning("generate_summary: Groq call failed", exc_info=True)
        raw = ""

    title, narrative, points, conclusion = _parse_summary_output(raw, fallback_title=subject[:60])

    content_lines = [narrative] if narrative else []
    content_lines += [f"- {p}" for p in points] if points else ([raw.strip()] if (raw.strip() and not narrative) else [])
    if conclusion:
        content_lines.append(f"\nConclusión: {conclusion}")
    content = "\n".join(content_lines)
    excerpt = (narrative or (points[0] if points else (conclusion or content)))[:140]

    record = {
        "id":           uuid.uuid4().hex[:12],
        "title":        title,
        "date":         datetime.datetime.now().isoformat(),
        "type":         "tema" if explicit_topic else "conversación",
        "content":      content,
        "narrative":    narrative,
        "source_topic": subject,
        "excerpt":      excerpt,
    }
    intent_mod._pending_action = {
        "kind": "estudio_summary",
        "data": {"record": record},
        "at": time.monotonic(),
    }

    return response._pf(
        "Resumen listo, señor. ¿Lo guardo en Estudio?",
        "Resumen listo. ¿Lo guardo en Estudio?",
        "Resumen hecho. ¿Lo guardo en Estudio o lo dejamos aquí?",
    )


def _parse_task_steps_output(raw: str) -> list[str]:
    """Best-effort parse of the LLM's '- step one\\n- step two\\n...'
    response (see create_task_from_goal's prompt) — same tolerant
    bullet-line parsing as _parse_summary_output's PUNTOS: section, capped
    at 6 steps."""
    steps = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if line.startswith("-") or line.startswith("•"):
            s = line.lstrip("-•").strip()
            if s:
                steps.append(s)
    return steps[:6]


def create_task_from_goal(goal: str) -> str:
    """'crea una tarea (para/de) X' / 'quiero que trabajes en X' — breaks
    the spoken goal into 3-6 concrete sequential steps via one Groq call,
    then persists it with core.task_engine (which deliberately never calls
    an LLM itself — see that module's own docstring; this is where step
    decomposition actually happens) so it can be advanced one step per
    sleep cycle.

    Level 1 of the three-level action philosophy (see this module's own
    header comment): a direct, explicit order — execute immediately, no
    propose/confirm round trip, same treatment as
    core.actions._execute_start_investigation. Never raises; a Groq
    failure still creates a (single-step) task rather than silently doing
    nothing.
    """
    goal = (goal or "").strip(" ¿?¡!.")
    if not goal:
        return response._pf(
            "No he entendido bien qué tarea quiere que cree, señor.",
            "No he pillado qué tarea quieres que cree.",
            "¿Qué tarea exactamente?",
        )

    system_prompt = (
        "Eres un asistente que descompone un objetivo en pasos concretos y "
        "secuenciales, en español. Responde EXCLUSIVAMENTE con la lista de "
        "pasos, uno por línea, cada uno empezando con '- ', sin numeración "
        "ni texto adicional antes o después. Entre 3 y 6 pasos."
    )
    try:
        raw = groq_client._groq_complete(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Objetivo: {goal}"},
            ],
            max_tokens=300,
        )
    except Exception:
        logger.warning("create_task_from_goal: Groq call failed", exc_info=True)
        raw = ""

    steps = _parse_task_steps_output(raw) or [f"Completar: {goal}"]

    from core.task_engine import task_engine
    task_engine.create_task(goal, steps, priority=1, created_by="joan")

    first_step = steps[0]
    return response._pf(
        f"Tarea creada, señor: {goal}. Son {len(steps)} pasos — empiezo por: {first_step}.",
        f"Tarea creada: {goal}. Son {len(steps)} pasos, empiezo por: {first_step}.",
        f"Vale, tarea creada: {goal}. Son {len(steps)} pasos, el primero es: {first_step}.",
    )


_VALID_SCHEMA_NODE_TYPES = ("concept", "question", "connection", "example", "insight")


def _load_known_topics(limit: int = 15) -> list[str]:
    """Titles/topics already in Estudio (data/schemas.json +
    data/investigations.json) — fed into the schema generation prompt so
    'connections_to_known' and the connection node point at something Joan
    genuinely already has on record, not a hallucinated topic."""
    topics: list[str] = []
    try:
        with open(_SCHEMAS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        for rec in data if isinstance(data, list) else []:
            t = rec.get("topic")
            if t:
                topics.append(t)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    try:
        from core.investigations import _load_investigations
        for rec in _load_investigations():
            t = rec.get("title") or rec.get("question")
            if t:
                topics.append(t)
    except Exception:
        logger.debug("_load_known_topics: could not read investigations.json", exc_info=True)

    return topics[:limit]


def _parse_schema_json_output(raw: str) -> dict:
    """Best-effort parse of the LLM's JSON schema response — see
    generate_schema's prompt for the exact shape required
    (nodes/open_questions/connections_to_known). A model that wraps the
    JSON in a ``` fence or adds stray text around it still parses via the
    largest-{...}-substring fallback below; genuinely broken JSON falls
    back to a single concept node holding the raw text rather than losing
    the generation entirely — same 'never lose what the model did produce'
    philosophy as _parse_summary_output above. Every field is normalized
    (dropped/defaulted if the model didn't follow the schema) so callers
    never have to guard against missing keys or wrong types."""
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*\n(.*)\n```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    def _try_load(s: str) -> dict | None:
        try:
            parsed = json.loads(s)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None

    parsed = _try_load(text)
    if parsed is None:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            parsed = _try_load(m.group())

    if parsed is None:
        logger.warning("generate_schema: could not parse JSON output, falling back to raw text as a single node")
        return {
            "title": "",
            "nodes": [{"type": "concept", "title": "Resumen", "body": text[:2000], "links_to": [], "depth": 1}] if text else [],
            "open_questions": [],
            "connections_to_known": [],
        }

    nodes = []
    for n in parsed.get("nodes", []) if isinstance(parsed.get("nodes"), list) else []:
        if not isinstance(n, dict) or not n.get("title"):
            continue
        depth = n.get("depth")
        nodes.append({
            "type":     n.get("type") if n.get("type") in _VALID_SCHEMA_NODE_TYPES else "concept",
            "title":    str(n.get("title"))[:150],
            "body":     str(n.get("body", ""))[:1000],
            "links_to": [str(x) for x in n.get("links_to", []) if isinstance(x, (str, int, float))][:10],
            "depth":    depth if isinstance(depth, int) and depth > 0 else 1,
        })

    open_questions = [str(q) for q in parsed.get("open_questions", []) if isinstance(q, (str, int, float))][:10]
    connections_to_known = [str(c) for c in parsed.get("connections_to_known", []) if isinstance(c, (str, int, float))][:10]
    title = str(parsed.get("title") or "").strip()[:150]

    return {"title": title, "nodes": nodes, "open_questions": open_questions, "connections_to_known": connections_to_known}


def generate_schema(topic: str, context: str | None = None, schema_type: str = "outline") -> str:
    """Generate a structured map of understanding — concepts, open
    questions, and connections to what Joan already knows, not a flat
    nested bullet list — via the existing Groq call. `topic` mirrors
    generate_summary()'s own fallback-to-context behavior for a bare
    'organiza esto'/'estructura' with no explicit subject. `schema_type`
    (one of 'mapa conceptual'/'estructura'/'outline' — see core.intent's
    three schema trigger patterns) is accepted for call-site compatibility
    but no longer stored: the new record has no single top-level type, only
    a type per node (concept/question/connection/example/insight).

    Level 1, not Level 3 (bug fix — this used to be Level 3, proposing the
    save and asking '¿Lo guardo en Estudio?' before persisting, same
    treatment as generate_summary() below). That confirm-gate makes sense
    for an IMPLIED action core.intent infers from something Joan mentioned
    in passing; it doesn't for this — 'hazme un esquema de X' is already a
    direct, unambiguous order to produce and keep a schema, no different
    from 'pon un evento el viernes'. Saves immediately, per Level 1's own
    definition (see this module's header comment). Returns a raw result
    for the caller to phrase naturally via response._format_response — not
    HUGO's final spoken line itself (bug fix: it used to be, identically
    worded every time) — never raises.
    """
    explicit_topic = (topic or "").strip()
    subject = explicit_topic or (context or "").strip() or "la conversación reciente"

    known_topics = _load_known_topics()

    system_prompt = (
        "Eres un asistente que construye MAPAS DE COMPRENSIÓN sobre el tema "
        "indicado, en español — no listas jerárquicas planas. Un buen "
        "esquema captura conceptos, cómo se conectan entre sí, qué preguntas "
        "deja abiertas, y cómo se relaciona con lo que Joan ya sabe. No solo "
        "'qué es', también 'cómo conecta' y 'qué queda sin resolver'.\n\n"
        "Responde EXCLUSIVAMENTE con un objeto JSON válido — sin texto antes "
        "ni después, sin bloque de código — con esta forma exacta:\n"
        '{\n'
        '  "title": "título breve y concreto para todo el esquema, menos '
        'de 8 palabras",\n'
        '  "nodes": [\n'
        '    {"type": "concept|question|connection|example|insight", '
        '"title": "...", "body": "...", "links_to": ["otros títulos de '
        'nodos de este mismo esquema"], "depth": 1}\n'
        '  ],\n'
        '  "open_questions": ["..."],\n'
        '  "connections_to_known": ["..."]\n'
        '}\n\n'
        "Reglas obligatorias:\n"
        "- Mínimo 3 niveles de profundidad reales (nodos con depth 1, 2 y "
        "3) — nada de quedarse en la superficie del tema.\n"
        "- Al menos 2 nodos con type=\"question\".\n"
        "- Al menos 1 nodo con type=\"connection\" que conecte este tema con "
        "algo de la lista de TEMAS EXISTENTES de abajo, si hay alguno "
        "genuinamente relacionado — usa su título real, no lo inventes.\n"
        "- \"open_questions\": mínimo 3 preguntas que este esquema deja sin "
        "resolver o que valdría la pena investigar.\n"
        "- \"connections_to_known\": enlaces explícitos a temas de la lista "
        "de TEMAS EXISTENTES que de verdad se relacionen — si ninguno "
        "aplica, deja el array vacío en vez de inventar una conexión falsa.\n"
        "- Entre 6 y 14 nodos en total."
    )
    user_prompt = f"Tema a estructurar: {subject}"
    if known_topics:
        user_prompt += (
            "\n\nTEMAS EXISTENTES en la base de conocimiento de Joan (para "
            "conexiones reales, no inventadas):\n"
            + "\n".join(f"- {t}" for t in known_topics)
        )
    if context and explicit_topic and context.strip() != explicit_topic:
        user_prompt += f"\n\nContexto de la conversación: {context[:600]}"

    try:
        raw = groq_client._groq_complete(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=1800,
        )
    except Exception:
        logger.warning("generate_schema: Groq call failed", exc_info=True)
        raw = ""

    parsed = _parse_schema_json_output(raw) if raw.strip() else {
        "title": "", "nodes": [], "open_questions": [], "connections_to_known": [],
    }

    record = {
        "id":                   uuid.uuid4().hex[:12],
        "title":                parsed["title"] or subject,
        "topic":                subject,
        "created":              datetime.datetime.now().isoformat(),
        "nodes":                parsed["nodes"],
        "open_questions":       parsed["open_questions"],
        "connections_to_known": parsed["connections_to_known"],
    }
    _append_json_record(_SCHEMAS_PATH, record)
    _emit_estudio_updated("esquemas")

    # A raw result for the caller to phrase naturally (see its own call
    # site in _dispatch_command_impl, which now runs this through
    # response._format_response same as generate_summary/generate_schema's
    # sibling non-Groq-exempt intents) — bug fix: this used to be the
    # literal spoken line itself, identical every single time.
    return f"Esquema sobre «{subject}» guardado en Estudio, con {len(parsed['nodes'])} nodos."


_VOICE_ENROLL_RE = re.compile(
    r"\b(aprende|reconoce|configura|activa|graba)\b.{0,20}"
    r"\b(mi\s+voz|huella\s+de\s+voz|reconocimiento\s+de\s+voz|voz)\b",
    re.IGNORECASE,
)


def dispatch_command(transcript: str, context: str | None = None,
                      voice_gated: bool = False, is_continuation: bool = False,
                      audio_path: str | None = None, images: list | None = None,
                      device_id: str | None = None) -> None:
    """Public entry point — wraps _dispatch_command_impl with proactive-
    behavior bookkeeping: marks 'processing' busy so the background
    proactive thread never interrupts (see core.background_loops._proactive_blocked),
    records the last-interaction time for idle-based proactive checks,
    delivers any pending session-based reminder before handling the new
    command (see core.reminders._deliver_session_reminders), likewise for
    any unread investigation notification (see
    core.notifications._deliver_pending_notifications), and signals
    continuous sleep to stop if it's currently running (see
    core.sleep_control.notify_user_interaction(); this is the catch-all
    covering text input, and voice once its transcript is ready —
    core/listener.py separately calls the same function earlier, right as a
    wake word is detected, for a faster stop on the voice path).

    Args:
        voice_gated: True only for calls originating from core/listener.py's
                 audio pipeline (wake word or context-window continuation).
                 Gates the turn through core/social_reasoning.py before any
                 response is produced — see _dispatch_command_impl. Typed
                 input (core/routes_control.py's /text_command) leaves this
                 False: a deliberately-typed message is always addressed to
                 the assistant, so there's nothing to reason about.
        is_continuation: Only meaningful when voice_gated=True — True when
                 the utterance was captured without a wake word, inside the
                 post-response context window (core.listener._CONTEXT_WINDOW_SECS).
                 Selects social_reasoning.should_continue() instead of
                 is_addressed() for the gate check.
        audio_path: Phase 4/5 — path to a short wav snapshot of the raw
                 audio for this utterance (core/listener.py writes this from
                 its rolling circular_buffer right before dispatch; None for
                 typed input, which needs no speaker check). Used only for
                 the multi-factor identity gate in _dispatch_command_impl —
                 see _identify_speaker_multi_factor.
        images: Optional list of {"data": <base64>, "mime": <str>} dicts —
                 attachments staged in the Chat UI's paperclip picker
                 (core/routes_control.py's /text_command). Typed input only;
                 voice has no image path. See _dispatch_command_impl for how
                 each is turned into a text description via core.vision
                 before the normal reply is generated.
        device_id: Persistent per-device UUID (ui/js/bootstrap-auth.js's
                 _deviceFingerprint) sent with typed input — None for voice,
                 which has no browser/device of its own to report. Fed into
                 core.social.identify_person as an authoritative signal
                 alongside audio_path — see _dispatch_command_impl.
    """
    global _last_interaction_mono, _last_interaction_wall
    with _dispatch_lock:
        previous_mono = _last_interaction_mono
        _last_interaction_mono = time.monotonic()
        _last_interaction_wall = memory._now_iso()
        notify_user_interaction()
        _dispatch_busy.set()
        try:
            with personality_mod._personality_lock:
                current_p = personality_mod._personality
            reminders._deliver_session_reminders(current_p)
            notifications._deliver_pending_notifications(current_p)
            # Proactive Intelligence Phase 5 — if a spontaneous line was
            # delivered earlier and this transcript is the reply to it,
            # classify Joan's reaction BEFORE anything else touches this
            # transcript (delivering a new queued entry below, or the normal
            # dispatch pipeline). No-op if nothing is currently awaiting a
            # reaction — see core.spontaneity.maybe_capture_reaction's own
            # docstring.
            try:
                from core import spontaneity as spontaneity_mod
                spontaneity_mod.maybe_capture_reaction(transcript)
            except Exception:
                logger.debug("Spontaneity reaction capture failed (non-critical)", exc_info=True)
            # Proactive Intelligence Phase 4 — deliver at most one queued
            # initiative entry per conversation pause (same shape as the two
            # calls above), then, if this message follows a >=30min gap (or
            # is the very first message this process has seen), kick off a
            # fresh scan in the background — see core.initiative's own module
            # comment for why "conversation start" is defined this way.
            try:
                from core import initiative as initiative_mod
                import core.background_loops as background_loops_mod
                initiative_mod._deliver_pending_initiative(current_p)
                if previous_mono is None or (_last_interaction_mono - previous_mono) >= background_loops_mod._SESSION_IDLE_END_SECONDS:
                    initiative_mod.trigger_conversation_start_scan()
            except Exception:
                logger.debug("Initiative delivery/scan trigger failed (non-critical)", exc_info=True)
            _dispatch_command_impl(
                transcript, context=context, voice_gated=voice_gated, is_continuation=is_continuation,
                audio_path=audio_path, images=images, device_id=device_id,
            )
        finally:
            _dispatch_busy.clear()


def _dispatch_command_impl(transcript: str, context: str | None = None,
                           voice_gated: bool = False, is_continuation: bool = False,
                           audio_path: str | None = None, images: list | None = None,
                           device_id: str | None = None) -> None:
    """Intent → action → format pipeline. Never raises.

    Args:
        transcript: The user's spoken command (STT result).
        context: Optional rolling transcript context injected by conversation
                 mode.  When provided, it is prepended to the user message so
                 the LLM has awareness of what was said before the wake word.
        voice_gated / is_continuation / audio_path / images: see
                 dispatch_command()'s docstring.
    """
    cmd_start = time.monotonic()
    from core import social as social_mod
    logger.info("[LATENCY] command_received  text=%r", social_mod.redact_identity_code(transcript[:80]))
    # Set True by _stream_and_speak_reply's call sites below once a reply
    # has already been spoken chunk-by-chunk as it streamed in — the
    # shared tail near the end of this function checks this before its
    # own _say_for(reply) call, so a streamed reply is never spoken twice.
    already_spoken = False

    try:
        # ── Listen-mode switch? ──────────────────────────────────────────────
        # Checked first so a single utterance like "activa modo conversación"
        # never reaches Groq unnecessarily.
        new_mode = intent_mod._detect_mode_switch(transcript)
        if new_mode:
            try:
                import core.listener as _listener_mod
                _listener_mod.set_listen_mode(new_mode)
            except Exception as mode_err:
                logger.warning("Could not switch listen mode: %s", mode_err)
            mode_labels = {"conversation": "conversación", "wake_word": "normal"}
            with personality_mod._personality_lock:
                current_p = personality_mod._personality
            # Phrased naturally (see feedback_no_hardcoded_replies memory) —
            # this used to be a fixed f-string spoken verbatim every time.
            confirm = response._format_response(
                f"Activando modo {mode_labels.get(new_mode, new_mode)}.",
                transcript=transcript, personality=current_p,
            )
            logger.info("Jarvis: %s", confirm)
            _say_for(current_p, confirm, cmd_start=cmd_start)
            return

        # ── Floating diamond move command? ───────────────────────────────────
        # 'muévete', 'quítate de ahí', 've a la esquina', 'muévete a la
        # derecha', ... — a UI-only side effect, handled silently (per spec:
        # "no verbal confirmation needed, just moves"), so this returns
        # immediately without ever reaching _say_for/Groq, same early-return
        # shape as the listen-mode switch above but with no spoken reply at
        # all — the diamond moving IS the acknowledgment.
        diamond_region = intent_mod._detect_diamond_move(transcript)
        if diamond_region:
            try:
                import core.server as server_mod
                server_mod.emit_diamond_move(diamond_region)
            except Exception:
                logger.warning("Could not emit diamond_move", exc_info=True)
            return

        # ── Voice enrollment request? (Phase 4) ──────────────────────────────
        # A deterministic short-circuit, same shape as the mode-switch/
        # diamond-move checks above — never reaches Groq. Kicks off
        # core/listener.py's multi-sample recording state machine; the
        # actual enrollment (writing data/voice_fingerprint.json) happens
        # later, in finish_voice_enrollment(), once every sample is in.
        if _VOICE_ENROLL_RE.search(transcript.lower()):
            with personality_mod._personality_lock:
                current_p = personality_mod._personality
            # Phrased naturally throughout this block (see
            # feedback_no_hardcoded_replies memory) — these used to be
            # fixed strings spoken verbatim every time.
            if not speaker.SPEAKER_VERIFICATION_ENABLED:
                msg = response._format_response(
                    "El reconocimiento de voz está desactivado ahora mismo.",
                    transcript=transcript, personality=current_p,
                )
                logger.info("Jarvis: %s", msg)
                _say_for(current_p, msg, cmd_start=cmd_start)
                return
            try:
                import core.listener as _listener_enroll
                started = _listener_enroll.request_voice_enrollment()
            except Exception:
                logger.warning("Could not start voice enrollment", exc_info=True)
                started = False
            if started:
                result = (
                    "Vale, vamos a aprender tu voz. Dime una frase cualquiera, "
                    "cuéntame algo sobre tu día, con naturalidad."
                )
            else:
                result = "Ya estoy grabando tu voz — termina esa sesión antes de pedir otra."
            msg = response._format_response(result, transcript=transcript, personality=current_p)
            logger.info("Jarvis: %s", msg)
            _say_for(current_p, msg, cmd_start=cmd_start)
            return

        # ── Identity override code? (device/friend distinction) ─────────────
        # A deterministic short-circuit, same tier as mode-switch/diamond-
        # move/voice-enrollment above — never reaches Groq. This is the
        # explicit fallback for the one case device-ID matching can't cover:
        # Joan talking to HUGO from a device that isn't his own (Dani's
        # computer, a borrowed phone). Checked for BOTH voice and typed
        # input (unlike the voice-only gates further down) since the whole
        # point is it has to work regardless of whose device this is. See
        # core.social's own IDENTITY OVERRIDE CODE module section.
        try:
            from core import social as social_mod
            if social_mod.check_identity_code(transcript):
                social_mod.override_as_joan(device_id or social_mod.get_local_device_id())
                with personality_mod._personality_lock:
                    current_p = personality_mod._personality
                # Spoken VERBATIM — deliberately skips response._format_response()
                # (which normally naturalizes replies through a Groq call built
                # from f"Comando original: {transcript}") because that would send
                # the raw transcript — containing the secret phrase itself — to
                # the LLM. The whole point of the identity code is that it's a
                # deterministic, LLM-blind check ("Joan is here" := True); the
                # confirmation reply has to be equally LLM-blind, not just the
                # check. Found via a real incident: this originally DID call
                # _format_response(transcript=transcript, ...), which worked
                # exactly as feared — the phrase reached Groq and got echoed
                # back in the naturalized reply.
                confirm = "Identidad confirmada. Hola, Joan."
                logger.info("Jarvis: %s", confirm)
                _say_for(current_p, confirm, cmd_start=cmd_start)
                return
        except Exception:
            logger.debug("Identity code check failed (non-critical)", exc_info=True)

        # ── Determine current personality ────────────────────────────────────
        # HUGO is the only personality (JARVIS/FRIDAY removed 2026-08-10) —
        # nothing left to switch between, so the old "Personality switch?"
        # detection block that used to sit here is gone.
        with personality_mod._personality_lock:
            current_p = personality_mod._personality

        # ── Wake-word-only invocation ────────────────────────────────────────
        # If the transcript is just "HUGO" with no follow-up command, give a
        # brief ready-acknowledgment. Phrased naturally through
        # response._format_response rather than a fixed string (bug fix:
        # personality_mod._WAKE_ACK used to be spoken completely verbatim —
        # by far the most frequent single reply in the logs, and zero
        # character since it never touched the LLM at all). Not "sending a
        # bare wake word to Groq as a query" — the raw status below is a
        # known fact, not the empty transcript itself. _format_response's
        # own fallback returns the raw result verbatim on any Groq failure,
        # so this still
        # degrades to a plain ack rather than raising.
        if personality_mod._WAKE_ONLY_RE.match(transcript):
            ack = response._format_response(
                "Está aquí, escuchando, esperando a que Joan diga qué necesita.",
                transcript=transcript, personality=current_p,
            )
            logger.info("Jarvis: %s", ack)
            _say_for(current_p, ack, cmd_start=cmd_start, llm_done_mono=time.monotonic())
            return

        # ── Social reasoning gate (Phase 1 conversational intelligence) ─────
        # Voice-only, and only for substantive utterances (the cheap
        # deterministic short-circuits above — mode switch, diamond move,
        # personality switch, bare wake-word ack — never pay the Ollama round
        # trip). Answers: is Hugo actually being addressed here ("Hugo,
        # ¿puedes hacer esto?") vs. merely mentioned ("Hugo puede hacer
        # esto.", "Creo que Hugo debería aprender esto.") — or, for a
        # no-wake-word continuation inside the post-response context window,
        # is this really a continuation of the exchange vs. unrelated speech
        # the mic picked up. A fast local check (Ollama llama3.2:1b, regex
        # fallback) — never a full Groq call. Uncertain → responds (see
        # core/social_reasoning.py's own module comment).
        if voice_gated:
            addressed = (
                social_reasoning.should_continue(transcript) if is_continuation
                else social_reasoning.is_addressed(transcript)
            )
            if not addressed:
                logger.info(
                    "[SOCIAL] Not addressed (continuation=%s) — skipping response: %r",
                    is_continuation, transcript[:80],
                )
                return

        # ── Social reasoning gate (Phase 2 — general "should I speak?") ─────
        # Runs after the Phase 1 addressed/continuation check above (which
        # only filters "was Hugo's name actually meant for her"). This is
        # the broader call: given the last ~30s of conversation, the active
        # HUD section, and the time, does it actually make sense to
        # intervene right now — same INTERVENIR/SILENCIO reasoning
        # core.background_loops uses for the periodic proactive tick, so a
        # wake-word reply and an unprompted comment are held to the same
        # bar. See core/social_reasoning.py's should_intervene() for the
        # four questions and the max-one-silence-in-a-row rule.
        if voice_gated:
            social_context = social_reasoning.recent_conversation_snippet(transcript)
            hud_section    = social_reasoning.current_hud_section()
            if not social_reasoning.should_intervene(social_context, hud_section):
                logger.info("[SOCIAL] decided: silence — skipping response: %r", transcript[:80])
                return

        # ── Multi-factor speaker identification (Phase 4/5) ──────────────────
        # Voice-only, same scope as the two social reasoning gates above —
        # typed input has no audio to check and no identity ambiguity to
        # begin with. See _identify_speaker_multi_factor's own docstring for
        # the three factors and the [IDENTITY] log line's exact format.
        restrict_memory   = False
        identity_degraded = False
        identity_uncertain = False
        if voice_gated:
            combined_confidence, restrict_memory, identity_degraded = (
                _identify_speaker_multi_factor(transcript, audio_path)
            )
            identity_uncertain = speaker.CONFIDENCE_LOW <= combined_confidence < speaker.CONFIDENCE_HIGH

        # Phase 6 — updates core.social's "who's present" signal for this
        # turn (read by core.personalities.base._build_system_prompt to
        # decide which prompt variant to build). Best-effort, never blocks
        # a reply — identify_person() degrades gracefully with audio_path
        # unset (typed input) by falling through to the linguistic/context
        # signals, same fallback chain it always uses.
        # Creator authority (see core.social.InfoPermissions.can_trigger_actions'
        # own docstring): computed once here, right after identification, and
        # reused at the action-dispatch gate further down — defaults to "can
        # act" when nobody's been identified yet, same permissive default
        # who_is_present() itself falls back to (solo use is still the
        # common case). Best-effort; a lookup failure defaults to allowing
        # the action rather than silently blocking Joan over a bug here.
        _can_trigger_actions = True
        _can_access_schedule = True
        _current_person = None
        try:
            from core import social as social_mod
            # Voice never carries a browser device_id (see dispatch_command's
            # own docstring on that param — None for voice, always). Falling
            # back to the local-machine id here resolves through the same
            # generic _match_device path as any other device: Joan on an
            # install where he's already claimed it (this one), Dani by
            # default on a fresh one — see _match_device's own docstring on
            # the 2026-08-24 default-to-Dani redesign. Deliberately does NOT
            # touch _identify_speaker_multi_factor/restrict_memory above,
            # which stays voice-confidence-gated on purpose — a mic can pick
            # up someone else's voice on this same machine, so pulling
            # Joan's actual personal memory into a reply still needs the
            # stricter check; this only fixes who HUGO believes it's
            # talking to.
            social_mod.social_engine.identify_person(
                {"audio_path": audio_path, "device_id": device_id or social_mod.get_local_device_id()},
                transcript,
            )
            _present = social_mod.social_engine.who_is_present()
            _current_person = _present[0] if _present else None
            if _current_person is not None:
                _turn_permissions = social_mod.social_engine.get_information_permissions(_current_person.id)
                _can_trigger_actions = _turn_permissions.can_trigger_actions
                _can_access_schedule = _turn_permissions.can_access_joan_schedule
        except Exception:
            logger.debug("Social identification failed (non-critical)", exc_info=True)

        # ── Build user content — inject conversation context if provided ─────
        # Part 2: conversation mode passes the rolling buffer as context so
        # the LLM understands what the user was talking about before the wake
        # word appeared.  The context is capped at 400 chars to avoid token bloat.
        if context:
            user_content = (
                f"[Contexto previo de la conversación: {context[:400]}]\n"
                f"Comando actual: {transcript}"
            )
        else:
            user_content = transcript

        # Vision — each staged attachment (core/routes_control.py's
        # /text_command 'images' field) is described by core.vision's
        # OpenRouter-primary/Ollama-fallback router BEFORE the intent pipeline
        # below, then folded into user_content the same way context/
        # identity notes are — so intent detection, the calculator check,
        # and the personality reply all see it as part of what the user
        # said, not as a separate side-channel only the final reply knows
        # about. Best-effort per image: a failed description still lets
        # the rest of the message through rather than losing the whole
        # turn to one bad attachment.
        if images:
            from core import vision as vision_mod
            for i, img in enumerate(images, start=1):
                data = (img or {}).get("data")
                mime = (img or {}).get("mime") or "image/jpeg"
                if not data:
                    continue
                description = vision_mod.vision_router.describe_image(data, mime, transcript)
                label = f"imagen adjunta {i}" if len(images) > 1 else "imagen adjunta"
                # Same anti-hedging instruction core.personalities.base
                # already needs for injected weather data (see that
                # module's own "Nunca digas que no puedes ver el clima"
                # comment) — without it, the model's default training
                # ("I can't view images") wins over the description that's
                # right there in the prompt, and it denies having eyes on
                # something it's literally just been told about. Verified
                # live: without this line, a real cloud/Ollama-generated
                # description in context still got "no tengo acceso a la
                # imagen" as the reply.
                note = (
                    f"[Descripción de la {label}, generada por tu propio sistema de visión "
                    f"— ESTO es lo que ves, respóndelo con total seguridad, nunca digas que "
                    f"no puedes ver imágenes ni que no tienes acceso a ella: {description}]"
                    if description else
                    f"[No se pudo analizar la {label} — dilo si es relevante.]"
                )
                user_content = f"{user_content}\n\n{note}"

        # Phase 4/5 — uncertain-tier identity note (0.4-0.75 combined
        # confidence): still a normal reply, just an internal aside so the
        # model doesn't speak with unwarranted certainty about who it's
        # talking to. Never shown to Joan directly — same "CONTEXTO
        # OPCIONAL"-style injection pattern as the intuition/criteria/habits
        # blocks below, just for the open-ended-path-independent identity
        # signal instead.
        if identity_uncertain:
            user_content = (
                f"{user_content}\n\n"
                "[NOTA INTERNA: identificación de voz incierta esta vez — probablemente "
                "Joan, pero sin plena confianza. No lo menciones salvo que sea relevante.]"
            )

        # ── Calculator — evaluate locally before sending to Groq ────────────
        # Detect math expressions in the query and resolve them with Python's
        # eval (sanitized whitelist) so the LLM never guesses arithmetic.
        math_result = tools.evaluate_math(user_content)
        if math_result is not None:
            logger.debug("Calculator result: %s", math_result)
            user_content = f"Resultado de calculadora: {math_result}\n{user_content}"

        # ── Intent pipeline ──────────────────────────────────────────────────
        intent_data = intent_mod._detect_intent(user_content)
        intent      = intent_data.get("intent", "unknown")
        parameters  = intent_data.get("parameters", {})
        logger.debug("Intent=%s  Parameters=%s", intent, parameters)
        logger.info("[LATENCY] T1_intent_detected t=+%.3fs intent=%s", time.monotonic() - cmd_start, intent)

        # ── Creator authority gate — consequential actions only ─────────────
        # Reroutes an action with real consequences (calendar write,
        # reminder, opening an app, confirming a pending proposal, starting
        # an investigation, code engine work, creating a task) away from
        # actually executing whenever the current speaker isn't Joan (see
        # core.social.InfoPermissions.can_trigger_actions' own docstring —
        # this is the "creator authority" distinction: a trusted friend like
        # Dani can still ASK, HUGO just won't DO it for anyone but Joan).
        # Falls through to the normal open-ended reply path instead of a
        # hardcoded refusal string — the NOTA INTERNA note lets HUGO explain
        # it naturally, in her own voice, same pattern as the
        # identity_uncertain note above.
        if intent in _ACTION_INTENTS_REQUIRE_CREATOR and not _can_trigger_actions:
            intent = "unknown"
            parameters = {}
            user_content = (
                f"{user_content}\n\n"
                "[NOTA INTERNA: quien te habla no es Joan y te ha pedido algo con "
                "consecuencias reales (una acción, no solo hablar) — no la "
                "ejecutes bajo ningún concepto, eso solo lo desbloquea Joan. "
                "Explícaselo con naturalidad y tu tono habitual, sin sonar a "
                "mensaje de error ni a política de permisos, y sin detallar qué "
                "habrías hecho exactamente.]"
            )
        elif intent == "calendar_read" and not _can_access_schedule:
            intent = "unknown"
            parameters = {}
            user_content = (
                f"{user_content}\n\n"
                "[NOTA INTERNA: quien te habla no es Joan y te ha preguntado por "
                "su agenda — no compartas ningún detalle real de ella. Esquívalo "
                "con naturalidad y tu tono habitual.]"
            )

        # ── Contextual panel — best-effort, before the reply is generated;
        # never affects what HUGO actually says (see core.session._maybe_emit_panel).
        session_mod._maybe_emit_panel(intent, transcript)

        # ── Tone — detected fresh from the raw transcript, injected into the
        # system prompt (see core.personalities.base._build_system_prompt) so
        # HUGO adapts her delivery without a separate LLM call to classify
        # it. Feature-flagged: "neutral" (a real, already-supported value —
        # see _detect_tone's own default) is used verbatim when the flag is
        # off, so every downstream consumer of `tone` needs no None-handling
        # of its own.
        tone = intent_mod._detect_tone(transcript) if memory.is_feature_enabled("deteccion_tono") else "neutral"
        logger.debug("Tone=%s", tone)

        # ── HUGO intuition — only for the open-ended conversational path
        # (unknown / web_search), where a subtle personality flourish
        # actually fits; a deterministic reply like "sube el volumen" has
        # no business getting a philosophical aside about the hour. See
        # _build_intuition_context's own docstring for why this is
        # appended to user_content rather than threaded through
        # core.personalities.base._build_system_prompt.
        # Phase 4/5: skipped entirely for an unrestricted-memory turn only —
        # an unidentified speaker (restrict_memory) gets none of Joan's
        # personal patterns/habits/episodes surfaced back at them.
        if current_p == "hugo" and intent in ("unknown", "web_search") and not restrict_memory:
            intuition = _build_intuition_context(transcript, tone)
            if intuition:
                user_content = f"{user_content}\n\n{intuition}"

            # HUGO internal criteria — Phase 2 (see _detect_internal_criterion's
            # own docstring above). Same open-ended-path-only scope as
            # intuition just above: a deterministic reply has no business
            # carrying a 'CONTEXTO OPCIONAL' aside.
            criterion = _detect_internal_criterion(transcript, tone)
            if criterion:
                user_content = f"{user_content}\n\n{criterion}"

            # HUGO active habits — Phase 3 (see _build_habits_context's own
            # docstring above). Same open-ended-path-only scope as intuition
            # and internal criteria just above.
            habits_context = _build_habits_context()
            if habits_context:
                user_content = f"{user_content}\n\n{habits_context}"

            # HUGO social skills — Phase 4 (see _build_social_skills_context's
            # own docstring above). Same open-ended-path-only scope as
            # intuition/criteria/habits just above.
            skills_context = _build_social_skills_context()
            if skills_context:
                user_content = f"{user_content}\n\n{skills_context}"

        # Snapshot BEFORE reply generation — groq_client._groq_complete()
        # (called by some branches below, not others — e.g. actions._NO_GROQ_INTENTS
        # never touches Groq at all) stamps groq_config._last_latency['at']
        # each time it actually runs. Comparing this against the same field
        # AFTER reply generation is how "was Groq genuinely called this
        # turn" gets determined below, for the chat's LLM-latency display
        # (ui/js/chat-render.js) — reading _last_latency unconditionally
        # would risk reporting a STALE ttft from a previous turn whenever
        # this turn's intent skipped Groq entirely.
        _last_latency_at_before = groq_config._last_latency.get("at")

        logger.info("[LATENCY] T2_groq_call_start t=+%.3fs", time.monotonic() - cmd_start)
        # Phase 4/5: an unidentified speaker (restrict_memory) gets no
        # personal-fact/episode/implicit-context retrieval — passing
        # relevance_query=None makes core.personalities.base._build_system_prompt
        # skip that lookup entirely (see its own relevance_query checks).
        _relevance_query = None if restrict_memory else transcript

        # Whether streaming a reply chunk-by-chunk (see
        # _stream_and_speak_reply) is safe this turn. Deliberately NOT the
        # same check as restrict_memory above — that's a separate,
        # voice-confidence-based signal (_identify_speaker_multi_factor,
        # only ever computed for voice_gated turns) and isn't guaranteed
        # to agree with who the secret-protection filter further down this
        # function thinks is present. That filter only needs to run when
        # someone other than Joan is present, and it needs the COMPLETE
        # reply text before anything is spoken — so streaming has to match
        # its exact gate, not a different "is this Joan" signal that could
        # disagree with it. Computed once here and reused at both
        # streaming call sites below.
        _safe_to_stream = True
        try:
            from core import social as social_mod
            present = social_mod.social_engine.who_is_present()
            current_person = present[0] if present else None
            _safe_to_stream = current_person is None or current_person.id == "joan"
        except Exception:
            logger.debug("Streaming-safety speaker check failed (non-critical) — falling back to non-streaming", exc_info=True)
            _safe_to_stream = False

        if intent == "unknown":
            # ── Skill dispatch (see core/skill_dispatch.py) ─────────────────
            # HUGO-only (same scope as intuition/criteria/habits above) —
            # skills/ is her capabilities layer, not a general JARVIS
            # feature. Explicit path first: the user names a skill
            # directly (matches one of its `triggers` phrases) — deterministic,
            # no Groq call, same tier as the other regex/substring
            # shortcuts above. Falls through to the normal open-ended reply
            # below if no skill matched, or if the matched skill's
            # execute() failed.
            skill_reply = None
            if current_p == "hugo":
                explicit_skill = skill_dispatch.detect_explicit_skill_request(transcript)
                if explicit_skill:
                    raw_result = skill_dispatch.run_skill(explicit_skill, transcript, {"personality": current_p})
                    if raw_result:
                        logger.info("[SKILL] explicit dispatch -> %s", explicit_skill)
                        # Phrased through the model, not used verbatim (bug fix
                        # 2026-08-14) — see _phrase_skill_result's own docstring
                        # for why a skill's raw return string was never meant
                        # to become the whole reply.
                        skill_reply = _phrase_skill_result(
                            explicit_skill, raw_result, transcript, current_p, tone, _relevance_query,
                        )

            if skill_reply:
                reply = skill_reply
            else:
                skills_context = skill_dispatch.build_skills_awareness_context() if current_p == "hugo" else None
                augmented_content = f"{user_content}\n\n{skills_context}" if skills_context else user_content
                messages = _augment_with_user_model(_augment_with_agenda_and_health(
                    session_mod._get_messages_with_history(
                        augmented_content, current_p, tone=tone, relevance_query=_relevance_query,
                    )
                ))
                # Streamed sentence-by-sentence and spoken as it arrives —
                # overlaps LLM generation with TTS playback instead of
                # waiting for the whole reply first (see
                # _stream_and_speak_reply's own docstring and
                # _safe_to_stream's own comment above for the gating).
                if _safe_to_stream:
                    reply, already_spoken = _stream_and_speak_reply(current_p, messages, cmd_start)
                else:
                    reply = groq_client._groq_complete(messages)
                # Implicit path — HUGO decided on her own a skill fits (see
                # build_skills_awareness_context's [USAR_SKILL: nombre]
                # convention). Her marker line is never meant for Joan, so it
                # never reaches him either way — but the skill's raw result
                # goes through _phrase_skill_result rather than replacing
                # `reply` verbatim (same bug fix as the explicit path above).
                implicit_skill = skill_dispatch.extract_skill_directive(reply) if skills_context else None
                if implicit_skill:
                    raw_result = skill_dispatch.run_skill(implicit_skill, transcript, {"personality": current_p})
                    if raw_result:
                        logger.info("[SKILL] implicit dispatch -> %s", implicit_skill)
                        reply = _phrase_skill_result(
                            implicit_skill, raw_result, transcript, current_p, tone, _relevance_query,
                        )
                        already_spoken = False   # _phrase_skill_result doesn't speak — the shared tail must
        elif intent == "web_search":
            # Conservative gate — matching the regex above is necessary but
            # not sufficient. Below 0.8, skip the actual API call and answer
            # from training data instead, to preserve search-API credits.
            # Same treatment when the busqueda_web feature flag is off.
            confidence = intent_mod._web_search_confidence(transcript)
            if not memory.is_feature_enabled("busqueda_web") or confidence < 0.8:
                logger.info(
                    "[SEARCH SKIPPED] busqueda_web=%s confidence=%.2f — transcript=%r",
                    memory.is_feature_enabled("busqueda_web"), confidence, transcript[:80],
                )
                messages = _augment_with_user_model(_augment_with_agenda_and_health(
                    session_mod._get_messages_with_history(
                        user_content, current_p, tone=tone, relevance_query=_relevance_query,
                    )
                ))
                # Same streamed-and-spoken-as-it-arrives path as the
                # "unknown" intent branch above — see _safe_to_stream's
                # own comment for the gating.
                if _safe_to_stream:
                    reply, already_spoken = _stream_and_speak_reply(current_p, messages, cmd_start)
                else:
                    reply = groq_client._groq_complete(messages)
            else:
                reply = response._handle_web_search(transcript, current_p, tone=tone)
        elif intent in ("generate_summary", "generate_schema"):
            # Structured-content generation (ESTUDIO -> RESÚMENES/ESQUEMAS).
            # generate_summary() still makes its own Groq call internally
            # and returns HUGO's FINAL confirmation reply directly (Level 3
            # propose-then-confirm — see its own docstring) — untouched.
            # generate_schema() (Level 1 now — see its own docstring) only
            # returns a raw result; phrased naturally here via the same
            # response._format_response() every other regular intent
            # already gets (bug fix: it used to skip straight to a fixed
            # confirmation string, worded identically every time).
            topic = parameters.get("topic", "")
            if intent == "generate_summary":
                reply = generate_summary(topic, context=context)
            else:
                schema_result = generate_schema(
                    topic, context=context,
                    schema_type=parameters.get("schema_type", "outline"),
                )
                reply = response._format_response(
                    schema_result, transcript=transcript, personality=current_p, tone=tone,
                )
        elif intent == "create_task":
            # Same "makes its own Groq call internally, returns HUGO's
            # FINAL reply directly" treatment as generate_summary/
            # generate_schema just above.
            reply = create_task_from_goal(parameters.get("goal", ""))
        elif intent == "code_engine_review":
            # Synchronous, read-only — see core.code_engine_dispatch.review()'s
            # own docstring. None means Code Engine is disabled/unavailable;
            # falls through to a plain spoken explanation rather than a
            # cryptic empty reply.
            import core.code_engine_dispatch as code_engine_dispatch
            report = code_engine_dispatch.review(parameters.get("topic", ""))
            reply = report or "El código de HUGO está desactivado ahora mismo, así que no puedo revisarlo."
        elif intent == "code_engine_task":
            # Fire-and-forget — see core.code_engine_dispatch.dispatch_module_task()'s
            # own docstring for why this never blocks on the goal itself
            # (can take minutes) and how the real outcome reaches Joan
            # afterward (core.notifications, on her next turn).
            import core.code_engine_dispatch as code_engine_dispatch
            action = parameters.get("action", "create")
            topic = parameters.get("topic", "")
            started = code_engine_dispatch.dispatch_module_task(action, topic)
            verb = "creo" if action == "create" else "actualizo"
            reply = (
                f"Vale, {verb} el módulo de {topic.lower()}. Te aviso cuando esté listo."
                if started else
                "El código de HUGO está desactivado ahora mismo, así que no puedo hacer eso."
            )
        elif intent in actions._NO_GROQ_INTENTS:
            # Volume/open-app/calendar-write/calendar-confirm: deterministic
            # template replies, executed immediately — no Groq round trip
            # (see actions._NO_GROQ_INTENTS' own comment above _execute_action).
            reply = actions._execute_action(intent, parameters)
        else:
            result = actions._execute_action(intent, parameters)
            reply  = response._format_response(result, transcript=transcript, personality=current_p, tone=tone)

        logger.info("[LATENCY] groq_response t=+%.3fs", time.monotonic() - cmd_start)

        # Phase 5 — graceful degradation: confidence just dropped from a
        # recently-high reading (see _identify_speaker_multi_factor). Never
        # locks Joan out — just a brief, natural check prepended to whatever
        # HUGO was already going to say, per spec item 4's exact example.
        if identity_degraded:
            reply = "¿Estás bien? Suenas diferente. " + reply

        # ── History + memory ─────────────────────────────────────────────────
        session_mod._add_history("user",      transcript)
        session_mod._add_history("assistant", reply)
        # Phase 4/5: an unidentified speaker's turn is never folded into
        # Joan's memory store — same reasoning as the intuition/relevance
        # skips above, just applied to what gets WRITTEN this time instead
        # of what gets read.
        if not restrict_memory:
            memory._extract_and_save_memory(transcript, reply, current_p)
            # Bug fix (2026-08-10): core.linguistic_fingerprint.update_from_session()
            # (called from core/sleep.py's sleep sub-phase) reads core.session's
            # in-memory _history — but the sleep cycle runs as a SEPARATE OS
            # subprocess (scripts/reflective_mode.py --continuous), which has its
            # own empty _history and can never see this process's real
            # conversation. That call was always folding in 0 turns, which is
            # why data/linguistic_fingerprint.json never accumulated anything.
            # Fixed at the source instead: fold THIS turn in directly, live, in
            # the process where it actually happened — same "record right after
            # the reply" treatment as _record_turn_for_patterns below, and same
            # restrict_memory guard as the memory extraction just above (an
            # unidentified speaker's turn shouldn't shape Joan's fingerprint any
            # more than it should become one of her stored facts).
            #
            # Real incident (2026-08-24): this direct update_fingerprint() call
            # bypasses update_from_session()'s own modo_test check (that check
            # lives in the wrapper, not the underlying function — see
            # core.linguistic_fingerprint's own docstrings), so a Joan
            # conversation was still writing to data/linguistic_fingerprint.json
            # even after is_feature_enabled('modo_test') started auto-triggering
            # for Joan (core.memory_flags.is_feature_enabled). Guarded directly
            # here instead of pushing the check further down.
            #
            # Second, separate real incident (found simulating Dani's first
            # use, 2026-08-24): the fingerprint is documented — see
            # core.linguistic_fingerprint's own module docstring — as
            # specifically "HOW JOAN talks", a secondary voice-identification
            # signal for _identify_speaker_multi_factor. restrict_memory only
            # protects it for LOW-CONFIDENCE VOICE turns; typed input never
            # sets restrict_memory at all (voice_gated is False), so Dani's
            # own text messages were folding his vocabulary/sentence patterns
            # into what's supposed to be Joan's fingerprint — actively
            # degrading the very signal meant to recognize Joan. Gated here
            # on the actual identified speaker instead, independent of
            # restrict_memory/modo_test, both of which answer a different
            # question (test-mode ephemerality) than "is this really Joan".
            if not memory.is_feature_enabled("modo_test") and _current_person is not None and _current_person.id == "joan":
                try:
                    linguistic_fingerprint.update_fingerprint([transcript])
                except Exception:
                    logger.warning("Linguistic fingerprint live update failed (non-critical)", exc_info=True)
        reminders._maybe_store_reminder(transcript, reply, current_p)
        # HUGO-only pattern tracking (see _build_intuition_context above) —
        # recorded AFTER the reply, using this turn's own transcript/tone,
        # so a pattern only ever gets built from turns that already
        # happened, never tautologically from the one currently in flight.
        if current_p == "hugo":
            _record_turn_for_patterns(transcript, tone, reply)

        # Phase 6 — secret protection, second independent layer (the first
        # is structural: core.personalities.base._build_non_joan_system_prompt
        # never assembles Joan's personal context into the prompt at all
        # when the speaker isn't Joan). This catches anything the model
        # said anyway — from training data, from the transcript itself, or
        # from any context block Phase 6 didn't already suppress — via a
        # hard regex filter, never by asking the model to self-censor.
        try:
            from core import social as social_mod
            present = social_mod.social_engine.who_is_present()
            current_person = present[0] if present else None
            if current_person is not None and current_person.id != "joan":
                permissions = social_mod.social_engine.get_information_permissions(current_person.id)
                reply = social_mod.social_engine._protect_secrets(reply, permissions)
        except Exception:
            logger.debug("Secret protection filter failed (non-critical)", exc_info=True)

        _log_reply_as_bubbles(reply)
        # llm_done_mono is captured HERE — the instant the reply text is
        # finalized — and threaded into _say_for(), though currently unused
        # there (see _say_for's own docstring — it used to measure "time to
        # first audio output" for Kokoro/XTTS, removed along with those).
        llm_done_mono = time.monotonic()
        # Emitted AFTER the "Jarvis: %s" log line above (which is what
        # creates this reply's chat bubble on the frontend via the
        # SocketIOLogHandler -> 'log' event -> addMessage()) — the frontend
        # attaches this to the most recently added assistant message, so
        # the bubble must already exist before this arrives. Only emitted
        # if Groq was actually called this turn (see _last_latency_at_before
        # above) — actions._NO_GROQ_INTENTS and similar deterministic
        # replies have no real "LLM latency" to report.
        if groq_config._last_latency.get("at") != _last_latency_at_before:
            llm_latency = groq_config._last_latency.get("ttft")
            if llm_latency is not None:
                try:
                    import core.server as server_mod
                    server_mod.emit_response_timing({"llm_latency": llm_latency})
                except Exception:
                    logger.debug("Failed to emit response_timing (llm_latency)", exc_info=True)
        # Skipped if _stream_and_speak_reply already spoke this reply
        # chunk-by-chunk as it streamed in (see `already_spoken` at the
        # top of this function) — the chat bubble/logging above still
        # always runs with the complete text either way.
        if not already_spoken:
            _say_for(current_p, reply, cmd_start=cmd_start, llm_done_mono=llm_done_mono)

    except Exception as e:
        # Bug fix (Bug 8): log clearly that we're falling back due to API failure,
        # not a logic error — helps distinguish connectivity issues from bugs.
        logger.error(
            "dispatch_command failed — using static fallback (API down or exception): %s",
            e, exc_info=True,
        )
        reply = response._static_fallback(transcript)
        logger.info("Jarvis (static): %s", reply)
        with personality_mod._personality_lock:
            current_p = personality_mod._personality
        _say_for(current_p, reply, cmd_start=cmd_start, llm_done_mono=time.monotonic())
