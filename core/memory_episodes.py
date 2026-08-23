# ═══════════════════════════════════════════════════════════════════════════
# MEMORY EPISODES — episodic memory: significant past moments extracted from
# conversation history (data/episodes.json), pruning, relevance-scoring, and
# prompt formatting. Split out of core/memory.py (pure refactor, no behavior
# change).
#
# _extract_episodes_for_session reaches back into core.commands only via a
# function-local `import core.commands as commands` (same lazy-import
# pattern used throughout this codebase to avoid a circular import).
# ═══════════════════════════════════════════════════════════════════════════
import json
import logging
import os
import re
import threading
import datetime

from core.memory_store import _keywords
from core.memory_select import _natural_time_ago
from core.memory_flags import is_feature_enabled

logger = logging.getLogger(__name__)

EPISODES_PATH   = "data/episodes.json"
MAX_EPISODES    = 100
MAX_EPISODES_PER_SESSION = 3
EPISODE_RELEVANCE_DAYS   = 30   # only injected if from the last 30 days

_episodes_lock  = threading.Lock()
_episode_lock   = threading.Lock()   # guards _episode_cursor + extraction-in-flight
_episode_cursor = 0                  # index into _history already considered for extraction


def _load_episodes() -> list[dict]:
    try:
        with open(EPISODES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _save_episodes(episodes: list[dict]) -> None:
    os.makedirs(os.path.dirname(EPISODES_PATH) or ".", exist_ok=True)
    with open(EPISODES_PATH, "w", encoding="utf-8") as f:
        json.dump(episodes, f, ensure_ascii=False, indent=2)


def _prune_episodes(episodes: list[dict]) -> list[dict]:
    """Cap at MAX_EPISODES, dropping the oldest + lowest-importance first."""
    if len(episodes) <= MAX_EPISODES:
        return episodes
    ranked    = sorted(episodes, key=lambda e: (e.get("importance", 1), e.get("date", "")))
    drop_ids  = set(map(id, ranked[: len(episodes) - MAX_EPISODES]))
    return [e for e in episodes if id(e) not in drop_ids]


def _select_relevant_episodes(query: str, episodes: list[dict], max_episodes: int = 3) -> list[dict]:
    """Same keyword-overlap relevance approach as _select_relevant_facts,
    restricted to the last EPISODE_RELEVANCE_DAYS days. Episodes with
    importance < 3 only qualify when the topic match is strong (overlap
    ≥ 2) — i.e. 'directly relevant' — per the injection rule; importance
    ≥ 3 episodes just need any keyword overlap, same bar as facts."""
    msg_kw = _keywords(query)
    if not msg_kw:
        return []
    cutoff = datetime.date.today() - datetime.timedelta(days=EPISODE_RELEVANCE_DAYS)
    scored = []
    for e in episodes:
        try:
            e_date = datetime.date.fromisoformat(str(e.get("date", ""))[:10])
        except (ValueError, TypeError):
            continue
        if e_date < cutoff:
            continue
        ep_text = f"{e.get('topic', '')} {e.get('summary', '')} {' '.join(e.get('key_facts', []) or [])}"
        overlap = len(msg_kw & _keywords(ep_text))
        if overlap == 0:
            continue
        importance = e.get("importance", 1)
        if importance < 3 and overlap < 2:
            continue
        scored.append((overlap, importance, e))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [e for _, _, e in scored[:max_episodes]]


def _format_episodes_block(episodes: list[dict]) -> str:
    lines = []
    for e in episodes:
        when = _natural_time_ago(e.get("date")) or e.get("date", "")
        lines.append(f"- [{when}] — {e.get('summary', '')}")
    return "\n".join(lines)


def _extract_episodes_for_session() -> None:
    """Look at conversation turns since the last extraction and, via a
    single LLM call, pull out up to MAX_EPISODES_PER_SESSION genuinely
    significant episodes. Called on 30+ min inactivity (from
    _proactive_loop) and at process exit (atexit) — both are natural
    'session boundary' signals. Cheap no-op if nothing new has happened
    since the last call (see module comment above)."""
    if not is_feature_enabled("memoria_episodica"):
        return
    # Lazy import — core.commands imports this module at top level, so a
    # top-level import here would be circular.
    import core.commands as commands
    global _episode_cursor
    with _episode_lock:
        history   = commands._get_history_snapshot()
        new_turns = history[_episode_cursor:]
        if len(new_turns) < 2:   # nothing new, or too little to judge significance
            return
        _episode_cursor = len(history)

    try:
        transcript_block = "\n".join(
            f"{'Joan' if h['role'] == 'user' else 'HUGO'}: {h['content']}" for h in new_turns
        )
        raw = commands._groq_complete_fast(
            [
                {"role": "system", "content": (
                    "Eres HUGO. Repasas un fragmento de conversación y te quedas solo "
                    "con los momentos que de verdad vale la pena recordar a largo "
                    "plazo — nada trivial, nada operativo, nada de prueba. Respondes "
                    "solo con JSON válido, sin comentarios ni rodeos."
                )},
                {"role": "user", "content": (
                    f"Fragmento de conversación:\n{transcript_block}\n\n"
                    "Identifica hasta 3 EPISODIOS realmente significativos — un logro, "
                    "una decisión importante, un momento emocional genuino, o algo que "
                    "Joan compartió que va más allá de un simple hecho suelto.\n\n"
                    "Ejemplos que SÍ merecen un episodio: 'Joan consiguió el front lever "
                    "por primera vez', 'Joan decidió empezar a construir el Model 9'.\n"
                    "Ejemplos que NO merecen un episodio: 'Joan preguntó la hora', 'Joan "
                    "pidió el tiempo', 'Joan hizo un test de voz', o cualquier intercambio "
                    "trivial u operativo.\n\n"
                    "Nunca incluyas episodios sobre relaciones románticas o situaciones "
                    "emocionales personales de pareja — quedan fuera sin excepción, "
                    "aunque hayan salido en la conversación.\n\n"
                    "Si no hay nada realmente significativo, devuelve una lista vacía — "
                    "mejor no guardar nada que forzar un episodio de algo trivial.\n\n"
                    'Devuelve SOLO JSON válido: {"episodes": [{"summary": "...", '
                    '"topic": "...", "emotional_tone": "...", "key_facts": ["..."], '
                    '"importance": 1-5}, ...]}. Máximo 3 episodios.'
                )},
            ],
            max_tokens=400,
        )
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return
        result = json.loads(match.group())
        candidates = result.get("episodes", []) if isinstance(result, dict) else []
        if not isinstance(candidates, list) or not candidates:
            return

        today = datetime.date.today().isoformat()
        new_episodes = []
        for item in candidates[:MAX_EPISODES_PER_SESSION]:
            if not isinstance(item, dict):
                continue
            summary = str(item.get("summary", "")).strip()
            if not summary:
                continue
            try:
                importance = int(item.get("importance", 1))
            except (TypeError, ValueError):
                importance = 1
            importance = max(1, min(5, importance))
            key_facts = item.get("key_facts", [])
            if not isinstance(key_facts, list):
                key_facts = []
            new_episodes.append({
                "date":           today,
                "summary":        summary,
                "topic":          str(item.get("topic", "")).strip(),
                "emotional_tone": str(item.get("emotional_tone", "")).strip(),
                "key_facts":      [str(k).strip() for k in key_facts if str(k).strip()],
                "importance":     importance,
            })

        if not new_episodes:
            return

        with _episodes_lock:
            episodes = _load_episodes()
            episodes.extend(new_episodes)
            episodes = _prune_episodes(episodes)
            _save_episodes(episodes)

        # Entity Pillars Phase 2 — a genuinely significant episode (LLM
        # already screened out the trivial/operational ones above) is real
        # engagement signal; nudge 'interes' proportionally instead of a
        # flat bump, so a truly big moment (importance 5) moves it more
        # than a merely-notable one (importance 3).
        try:
            from core.internal_state import nudge
            top_importance = max((e["importance"] for e in new_episodes), default=0)
            if top_importance >= 3:
                nudge("interes", 0.04 * top_importance, f"episodio significativo (importancia {top_importance})")
        except Exception:
            pass
        logger.info("[MEMORY] %d episode(s) extracted this session", len(new_episodes))
    except Exception:
        logger.warning("Episode extraction failed (non-critical)", exc_info=True)
