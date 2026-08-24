# User-toggleable feature flags (Ajustes panel). Split out of
# core/memory.py (pure refactor, no behavior change).
import json
import os
import threading

# ---------------------------------------------------------------------------
# Feature flags — user-toggleable features in the Ajustes panel (see
# core/server.py's GET/POST /api/feature_flags and ui/index.html's
# .settings-toggles). Persisted to disk so they survive a restart; the
# in-memory _feature_flags dict is the single source of truth the rest of
# this module reads via is_feature_enabled() at each feature's own trigger
# point — no restart needed for a toggle to take effect, and no polling:
# set_feature_flag() writes straight through to disk on every change.
# ---------------------------------------------------------------------------
FEATURE_FLAGS_PATH = "data/feature_flags.json"

_DEFAULT_FEATURE_FLAGS = {
    "proactividad":      True,   # context-driven proactive comments (_maybe_send_proactive_message)
    "busqueda_web":      True,   # Serper/DuckDuckGo web search (intent == "web_search")
    "copiloto_hud":      True,   # reacts to frontend activity events (on_user_activity)
    "paneles_dinamicos": True,   # weather/time visual panels (_maybe_emit_panel)
    "deteccion_tono":    True,   # emotional tone detection (_detect_tone)
    "memoria_episodica": True,   # saves conversation episodes (_extract_episodes_for_session)
    "modo_test":         False, # ephemeral conversations — see "TEST MODE" section below
    # Voice-identification test tools (Ajustes -> Modo Test's expandable
    # panel) — see core.commands._identify_speaker_multi_factor and
    # core.speaker for where each is actually read. Independent of
    # modo_test: these control WHO HUGO thinks is speaking, not whether
    # the conversation gets persisted.
    "voice_recognition_enabled": True,  # off = skip the Phase 4/5 identity gate entirely, full trust (same effect as speaker.SPEAKER_VERIFICATION_ENABLED=False, just runtime-toggleable)
    "voice_learning_enabled":    True,  # off = speaker.absorb_sample() no-ops — an accepted match still gets a reply, just stops refining the enrolled fingerprint
    "voice_trust_all":           False, # on = every voice reads as a confirmed match (confidence 1.0) AND gets absorbed into the fingerprint — bulk/rapid enrollment, overrides voice_learning_enabled while active (see speaker.absorb_sample)
    # Interrupt feature, step 1 (see ~/.claude memory project_interrupt_feature.md
    # for the full design/status) — defaults OFF: this changes core.listener's
    # audio loop behavior during her own TTS playback (RMS-triggered ducking
    # instead of a hard skip), a genuinely new live-loop behavior, not just a
    # personalization toggle like the ones above. Off means zero behavior
    # change from before this feature existed.
    "interrupt_ducking_enabled": False,
    "code_engine_enabled": False, # kill switch for all of Code Engine — see core.code_engine.tool_manager.CodeEngineToolManager.get_tool() and core.code_engine.CodeEngine.create_module()/update_module(). No Ajustes toggle for this anymore (removed from FEATURE_FLAG_LABELS) — defaults off with no UI path to re-enable.
    "auto_update_enabled": False, # gates the UNATTENDED 6-hourly com.joan.hugo.autoupdate LaunchAgent run only — see scripts/rebuild_app.sh's own "Auto-actualización" guard; never affects the manual "Actualizar Sistema" button (HUGO_FORCE_UPDATE=1 always bypasses it). No Ajustes toggle for this anymore (removed from FEATURE_FLAG_LABELS) — defaults off with no UI path to re-enable.
    # skills/ loadable-capabilities gates — see skills/__init__.py's
    # HugoSkill.flag / _flag_for(); each skill checks its own flag on
    # every get_skill()/list_skills() call, so toggling one here (or via
    # the Ajustes panel, which calls set_feature_flag()) takes effect
    # without a server restart.
    "skill_web_search":  True,
    "skill_weather":     True,
    "skill_calculator":  True,
    "skill_calendar":    True,
    "skill_discord":     True,
    "skill_schemas":     True,
    "skill_investigations": True,
}

# ---------------------------------------------------------------------------
# TEST MODE — when "modo_test" is on, HUGO responds normally but nothing from
# the conversation is persisted: no fact extraction (_extract_and_save_memory,
# gated at its own top), no episode extraction or session-end snapshot (both
# gated together in _end_of_session_bookkeeping, since a leftover
# last_messages/last_episode_summary in data/session_state.json would leak
# test-mode content into the NEXT real session's context just as much as an
# actual saved episode would). The sleep system (scripts/reflective_mode.py)
# and its manual-trigger endpoint (launcher.py's /api/sleep/start) check this
# same flag independently before running at all — see those files.
# ---------------------------------------------------------------------------

_feature_flags_lock = threading.Lock()


def _load_feature_flags() -> dict:
    try:
        with open(FEATURE_FLAGS_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(_DEFAULT_FEATURE_FLAGS)
    # Merge over the defaults (not replace) so a flag added in a later
    # version doesn't silently vanish just because an older saved file on
    # disk predates it — new flags default ON until this file is rewritten.
    # 'skill_*' names are always accepted even when not in
    # _DEFAULT_FEATURE_FLAGS: skills/ is a dynamically discovered set (see
    # skills/__init__.py), not a fixed enumeration — core.code_engine can
    # create a brand new skill_<name> flag for a module that didn't exist
    # when this file was written, and it must survive a restart just like
    # any other flag, not get silently dropped by this filter.
    flags = dict(_DEFAULT_FEATURE_FLAGS)
    if isinstance(saved, dict):
        flags.update({
            k: bool(v) for k, v in saved.items()
            if k in _DEFAULT_FEATURE_FLAGS or k.startswith("skill_")
        })
    return flags


def _save_feature_flags() -> None:
    os.makedirs(os.path.dirname(FEATURE_FLAGS_PATH) or ".", exist_ok=True)
    with open(FEATURE_FLAGS_PATH, "w", encoding="utf-8") as f:
        json.dump(_feature_flags, f, ensure_ascii=False, indent=2)


_feature_flags = _load_feature_flags()


def get_feature_flags() -> dict:
    """Snapshot of every flag's current state — backs GET /api/feature_flags."""
    with _feature_flags_lock:
        return dict(_feature_flags)


def reload_feature_flags() -> None:
    """Re-read data/feature_flags.json and refresh the in-memory cache —
    same purpose as core.memory_setup.reload_instructions(). Needed
    because scripts/reflective_mode.py (a separate OS process — see its own
    module docstring) toggles 'proactividad' off/on directly on disk while
    a sleep session is running (see its _disable_proactivity/
    _restore_proactivity), and this module's own in-memory _feature_flags
    dict — loaded once at import — has no other way to observe that write.
    Called every tick by core.sleep_control's idle-trigger thread, same
    idiom as its neighboring memory.reload_instructions() call there."""
    global _feature_flags
    with _feature_flags_lock:
        _feature_flags = _load_feature_flags()


def _current_speaker_is_joan() -> bool:
    """Best-effort 'is Joan the identified speaker right now' check — same
    who_is_present() lookup as core.sleep_curiosity_search._admin_device_active,
    duplicated here rather than imported to keep this module free of a
    core.social dependency at import time. A lookup failure defaults to
    False (not Joan) so a bug here can never silently make is_feature_enabled
    ephemeral-ize a real Dani conversation."""
    try:
        from core import social as social_mod
        present = social_mod.social_engine.who_is_present()
        current = present[0] if present else None
        return current is not None and current.id == "joan"
    except Exception:
        return False


def is_feature_enabled(name: str) -> bool:
    """True unless explicitly toggled off. Unknown names default True rather
    than raising, so a typo'd flag name fails open (feature stays on) instead
    of silently disabling something.

    'modo_test' has a second, automatic trigger on top of the manual Ajustes
    toggle: HUGO is Dani's assistant, not Joan's (see the 2026-08-24 identity
    redesign) — Joan's own testing/admin conversations should never become
    part of HUGO's memory of Dani, so every 'modo_test'-gated ephemeral-
    conversation path (fact extraction, episodes, session-end snapshot,
    linguistic fingerprint, pattern tracking — see this module's own TEST
    MODE section) also fires automatically whenever Joan is the currently
    identified speaker, with no toggle required."""
    if name == "modo_test" and _current_speaker_is_joan():
        return True
    with _feature_flags_lock:
        return _feature_flags.get(name, True)


def set_feature_flag(name: str, enabled: bool) -> dict:
    """Toggle one flag and persist immediately. Returns the full updated
    snapshot — backs POST /api/feature_flags, which broadcasts that snapshot
    over SocketIO so every connected HUD tab stays in sync. 'skill_*' names
    are always accepted (see _load_feature_flags's own comment) so
    core.module_manager can enable/disable a module core.code_engine only
    just created, without this file needing to know about it in advance."""
    if name not in _DEFAULT_FEATURE_FLAGS and not name.startswith("skill_"):
        raise ValueError(f"unknown feature flag: {name}")
    with _feature_flags_lock:
        _feature_flags[name] = bool(enabled)
        snapshot = dict(_feature_flags)
        _save_feature_flags()
    return snapshot
