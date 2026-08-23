# ═══════════════════════════════════════════════════════════════════════════
# MEMORY — thin aggregator. All persisted read/write state used to live in
# this one file; it's now split into focused modules (pure refactor, no
# behavior change), re-exported here so every existing `from core import
# memory; memory.some_function(...)` call site across the codebase keeps
# working unchanged:
#
#   core/memory_context.py  — armor/concepts summaries injected into prompts
#   core/memory_flags.py    — feature flags (data/feature_flags.json)
#   core/memory_store.py    — Layer 1/2 fact persistence (load/save/dedup/
#                              upsert/expire), shared paths + constants
#   core/memory_setup.py    — legacy-memory migration + static instructions
#   core/memory_migrate.py  — Memory V2 structured-knowledge migration
#                              (Ollama, one-time, marker-file guarded)
#   core/memory_extract.py  — _extract_and_save_memory (LLM fact extraction)
#   core/memory_select.py   — relevance-filtered fact selection for prompts
#   core/memory_episodes.py — episodic memory (data/episodes.json)
#   core/memory_stats.py    — health/stats endpoints, active-memory snapshot,
#                              think-log reader, weekly consolidation
#   core/memory_user_model.py — the explicit user model (data/user_model.json)
#                              — LIRA's living understanding of Joan as a
#                              person, built/updated by scripts/
#                              reflective_mode.py's 'Modelo de Usuario' sleep
#                              sub-phase, consulted on every response
#
# Module objects (not `from x import name`) are used for cross-references
# between these files where jarvis.py's watchdog hot-reloads matter — see
# each submodule's own comment for its specific lazy-import spots.
# ═══════════════════════════════════════════════════════════════════════════

from core.memory_context import (
    _build_armor_summary,
    _build_concepts_summary,
    reload_concepts,
    ARMOR_KNOWLEDGE_PATH,
    _ARMOR_SUMMARY,
    _get_armor_models,
    _select_relevant_armor,
    _expand_armor_with_references,
    _format_relevant_armor_block,
    CONCEPTS_PATH,
    _concepts_lock,
    _CONCEPTS_SUMMARY,
    _get_concepts,
    _select_relevant_concepts,
    _expand_concepts_with_references,
    _format_relevant_concepts_block,
)
from core.memory_flags import (
    _load_feature_flags,
    _save_feature_flags,
    get_feature_flags,
    is_feature_enabled,
    reload_feature_flags,
    set_feature_flag,
)
from core.memory_store import (
    MEMORY_LIRA_PATH,
    MEMORY_SHARED_PATH,
    _MEMORY_HEALTH_WARN_THRESHOLD,
    _TEMPORAL_FACT_PATTERNS,
    _DAYS_ES,
    _MONTHS_ES,
    _memory_lock,
    _get_personality_memory_path,
    _now_iso,
    _fact_similarity,
    _keywords,
    _normalize_fact,
    _is_fact_expired,
    _dedup_facts,
    _load_fact_file,
    _save_fact_file,
    _upsert_fact,
    _mark_fact_outdated,
    time_since,
    mark_facts_used,
)
from core.memory_setup import (
    _migrate_legacy_memory,
    _load_instructions_file,
    reload_instructions,
    _build_instructions_block,
)
from core.memory_migrate import run_memory_v2_migration
from core.memory_extract import _extract_and_save_memory
from core.memory_select import (
    _load_shared_facts,
    _load_personality_facts,
    _fact_temporal_weight,
    _fact_usage_score,
    _select_relevant_facts,
    _expand_with_connections,
    _expand_with_semantic_search,
    _natural_time_ago,
    _format_relevant_facts_block,
)
from core.memory_episodes import (
    EPISODES_PATH,
    MAX_EPISODES,
    MAX_EPISODES_PER_SESSION,
    EPISODE_RELEVANCE_DAYS,
    _load_episodes,
    _save_episodes,
    _prune_episodes,
    _select_relevant_episodes,
    _format_episodes_block,
    _extract_episodes_for_session,
)
from core.memory_stats import (
    ACTIVITY_LOG_PATH,
    CONSOLIDATION_LOG_PATH,
    _memory_file_map,
    _log_memory_health,
    get_memory_stats,
    clean_all_memory,
    get_active_memory,
    get_think_log,
    _consolidate_memory_file,
    _consolidate_memory,
    _maybe_run_weekly_consolidation,
)
from core.memory_user_model import (
    USER_MODEL_PATH,
    get_user_model,
    update_user_model,
    format_user_model_block,
)

# ---------------------------------------------------------------------------
# CONTEXTO TEMPORAL — session-gap awareness. Persists a small snapshot of
# how each session ends (see core.session._save_session_end_state) so the
# next session's system prompt can report the gap (see
# core.personalities.base._build_contexto_temporal). Kept here rather than
# in any one submodule since it's a bare path, not tied to facts/episodes/
# stats specifically.
# ---------------------------------------------------------------------------
SESSION_STATE_PATH = "data/session_state.json"
