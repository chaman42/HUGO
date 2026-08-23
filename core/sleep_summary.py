"""Sleep System — shared context builders reused across phases: a compact
facts/episodes summary for prompts, recent error count, and memory health
stats."""
from core.sleep_state import MEMORY_SHARED_PATH, EPISODES_PATH, ERRORS_LOG_PATH, _load_json, _is_fact_expired

def _build_state_summary(max_facts: int = 12, max_episodes: int = 6) -> str:
    """Bug fix (confirmed live, against real data): facts saved by the
    (separate) reflective-mode system tend to be long, multi-clause
    sentences — unlike episode summaries, this used to NOT truncate fact
    text at all, so even a "lean" 10-fact/8-episode call measured ~700
    prompt tokens in practice before a single completion token was spent,
    and a real end-to-end phase call landed at ~1300 total tokens against
    a nominal ~500 budget. Facts are now truncated the same way episode
    summaries already were, with tighter default caps — this brought a
    real call down to ~770 total tokens, a real improvement but still
    above the nominal per-phase numbers (max_tokens caps completion only,
    never the prompt — same acknowledged trade-off as core/reflective.py).
    In practice this means a full 7-phase auto (idle-triggered) session may
    not always fit in one day's 3000-token auto_budget — the remaining <= 0
    check before each phase (see run_sleep_session) is what actually
    enforces the real ceiling, stopping gracefully after however many
    phases fit that day rather than ever overspending it, exactly per
    spec ("if budget runs low, stops gracefully"). Manual sessions draw
    from the separate, larger manual_budget (5000) instead, which has
    enough headroom over the phases' nominal ~3000-token sum to absorb
    this same overrun and still complete all 7 phases."""
    facts    = _load_json(MEMORY_SHARED_PATH, [])
    episodes = _load_json(EPISODES_PATH, [])

    fact_lines = [
        f"- ({f.get('category', '?')}) {str(f.get('fact', ''))[:90]}"
        for f in facts if isinstance(f, dict) and not f.get("outdated") and not _is_fact_expired(f)
    ][-max_facts:]
    episode_lines = [
        f"- [{e.get('date', '?')}] {e.get('topic', '')}: {str(e.get('summary', ''))[:70]}"
        for e in episodes if isinstance(e, dict)
    ][-max_episodes:]

    return (
        "FACTS:\n" + ("\n".join(fact_lines) or "(ninguno)") + "\n\n"
        "EPISODIOS RECIENTES:\n" + ("\n".join(episode_lines) or "(ninguno)")
    )

def _count_recent_errors(max_lines: int = 200) -> int:
    try:
        with open(ERRORS_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-max_lines:]
        return sum(1 for line in lines if "[ERROR]" in line or "Traceback" in line)
    except Exception:
        return 0

def _memory_health() -> tuple[int, int]:
    """(total_facts, outdated_facts) in memory_shared.json."""
    facts = _load_json(MEMORY_SHARED_PATH, [])
    if not isinstance(facts, list):
        return 0, 0
    total    = len(facts)
    outdated = sum(1 for f in facts if isinstance(f, dict) and f.get("outdated"))
    return total, outdated
