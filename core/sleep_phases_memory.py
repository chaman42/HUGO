"""Sleep System — Phase 0 (memory maintenance) and Phase 1 (memory cleanup):
the two phases that read/write memory_shared.json/memory_hugo.json directly,
plus their shared helpers (fact sampling, mind-map maintenance, dedup)."""
import datetime
import json
import random
import re

from core.sleep_state import (
    MEMORY_SHARED_PATH, MEMORY_HUGO_PATH, EPISODES_PATH, CONNECTIONS_PATH,
    _load_json, _save_json, _now_iso, _today, _log, _fact_similarity,
    _is_fact_expired, _LIFESPAN_VALUES,
)
from core.sleep_llm import _groq_call

# Memory V2 Part B — usage-based review/promote/demote thresholds for the
# algorithmic pass below. Reimplemented here (not imported from
# core.memory_store) for the same dependency-isolation reason as
# _is_fact_expired/_LIFESPAN_VALUES above — this module must stay
# standalone-runnable via scripts/reflective_mode.py.
_STALE_REVIEW_DAYS      = 60   # use_count == 0 and older than this -> flagged for review
_PROMOTE_USE_COUNT      = 5    # use_count >= this -> importance +1 (capped at 5)
_DEMOTE_STALE_DAYS      = 90   # use_count == 0 and older than this -> importance -1 (floored at 1)


def _fact_age_days(f: dict) -> float:
    created = f.get("created_at") or f.get("added")
    if not created:
        return 0.0
    try:
        dt = datetime.datetime.fromisoformat(created)
    except ValueError:
        return 0.0
    return (datetime.datetime.now() - dt).total_seconds() / 86400

_MAINT_SYSTEM = (
    "Eres HUGO revisando tu propia memoria (hechos guardados sobre Joan). "
    "Detectas duplicados a fusionar, hechos que mezclan dos ideas distintas a "
    "separar, categoría/lifespan mal asignados, y hechos vagos que se repiten "
    "y merecen contexto temporal más preciso. Respondes solo con JSON válido, "
    "sin comentarios."
)

_LIFESPAN_RULES_ES = (
    "Valores válidos de lifespan: 'permanent' (identidad, habilidades, "
    "proyectos, preferencias, relaciones, logros — no caduca), 'weekly' "
    "(situaciones en curso, estado actual de un proyecto, decisiones "
    "recientes), 'daily' (planes de hoy, ánimo o energía actuales, algo que "
    "pasó hoy), 'hourly' (estado del momento: acaba de comer, tiene sueño "
    "ahora, está en Madrid hoy)."
)

_HUGO_INDEX_OFFSET = 1000

# Caps how many facts per file get sent to the LLM review step each cycle —
# independent of how large memory_shared.json/memory_hugo.json actually are.
# Keeps the prompt (and therefore local-inference latency) bounded even with
# hundreds of facts; a continuous run does another cycle immediately after
# this one anyway, and each cycle samples a fresh random subset (see
# _sample_indexed), so coverage across all facts still happens over time —
# it just isn't all-at-once. The free algorithmic pass (expiry deletion,
# weight-based promotion) still runs over EVERY fact, every cycle, regardless
# of this cap — only the semantic (merge/split/recategorize/reword) review
# is sampled.
_MAINT_REVIEW_SAMPLE_SIZE = 30

def _sample_indexed(facts: list[dict], cap: int, offset: int = 0) -> list[tuple[int, dict]]:
    """Returns up to *cap* (original_index, fact) pairs — original_index is
    this fact's real position in *facts*, so _apply_review_actions's
    _resolve() can look it back up correctly even though the LLM never sees
    the facts that got left out of this particular sample."""
    indexed = list(enumerate(facts))
    if len(indexed) > cap:
        indexed = random.sample(indexed, cap)
    return [(i + offset, f) for i, f in indexed]

def _build_facts_index(indexed_facts: list[tuple[int, dict]]) -> str:
    lines = [
        f"{i}: ({f.get('category', '?')}/{f.get('lifespan', 'permanent')}, "
        f"x{f.get('weight', 1)}) {str(f.get('fact', ''))[:100]}"
        for i, f in indexed_facts
    ]
    return "\n".join(lines) or "(ninguno)"

def _phase_memory_maintenance(remaining_budget: int) -> tuple[int, int, str]:
    deleted_expired  = 0
    promoted         = 0
    promoted_texts: set[str] = set()
    mind_map_mapping: dict[str, str | None] = {}
    # Memory V2 Part B — usage-based review/promote/demote counters (see
    # thresholds above). 'flagged_for_review' facts aren't removed or
    # changed beyond the marker itself — they're use_count=0 and old enough
    # that a human (or a future LLM pass) might want to look at them, same
    # spirit as _MEMORY_HEALTH_WARN_THRESHOLD's "consider a cleanup" warning.
    flagged_for_review  = 0
    importance_promoted = 0
    importance_demoted  = 0

    shared_facts = _load_json(MEMORY_SHARED_PATH, [])
    hugo_facts   = _load_json(MEMORY_HUGO_PATH, [])
    shared_facts = shared_facts if isinstance(shared_facts, list) else []
    hugo_facts   = hugo_facts if isinstance(hugo_facts, list) else []

    def _algorithmic_pass(facts: list[dict]) -> list[dict]:
        nonlocal deleted_expired, promoted, flagged_for_review, importance_promoted, importance_demoted
        kept = []
        for f in facts:
            if not isinstance(f, dict) or not str(f.get("fact", "")).strip():
                continue
            if _is_fact_expired(f):
                deleted_expired += 1
                mind_map_mapping[f["fact"]] = None
                continue
            if f.get("weight", 1) >= 3 and f.get("lifespan", "permanent") != "permanent":
                f["lifespan"] = "permanent"
                promoted += 1
                promoted_texts.add(f["fact"])

            use_count = f.get("use_count", 0)
            age_days  = _fact_age_days(f)
            importance = f.get("importance", 3)

            f["flagged_for_review"] = use_count == 0 and age_days > _STALE_REVIEW_DAYS
            if f["flagged_for_review"]:
                flagged_for_review += 1

            if use_count >= _PROMOTE_USE_COUNT and importance < 5:
                f["importance"] = min(5, importance + 1)
                importance_promoted += 1
            elif use_count == 0 and age_days > _DEMOTE_STALE_DAYS and importance > 1:
                f["importance"] = max(1, importance - 1)
                importance_demoted += 1

            kept.append(f)
        return kept

    shared_facts = _algorithmic_pass(shared_facts)
    hugo_facts   = _algorithmic_pass(hugo_facts)

    tokens_used = 0
    merged = recategorized = reworded = split_n = 0
    if remaining_budget > 0 and (shared_facts or hugo_facts):
        shared_sample = _sample_indexed(shared_facts, _MAINT_REVIEW_SAMPLE_SIZE)
        hugo_sample   = _sample_indexed(hugo_facts, _MAINT_REVIEW_SAMPLE_SIZE, offset=_HUGO_INDEX_OFFSET)
        index_block = (
            "MEMORIA COMPARTIDA:\n" + _build_facts_index(shared_sample) +
            "\n\nMEMORIA HUGO (índices +1000):\n" + _build_facts_index(hugo_sample)
        )
        user = (
            f"{index_block}\n\n{_LIFESPAN_RULES_ES}\n\n"
            "Revisa esta memoria y devuelve hasta 4 acciones en una lista JSON. "
            'Cada acción tiene "action" igual a uno de:\n'
            '- "merge": hechos casi duplicados. {"action":"merge", "targets":[índices], '
            '"merged_fact":"texto único más preciso", "category":"...", "lifespan":"..."}\n'
            '- "split": un hecho que mezcla dos ideas distintas. {"action":"split", '
            '"target":índice, "facts":[{"fact":"...","category":"...","lifespan":"..."}, ...]}\n'
            '- "recategorize": categoría o lifespan mal asignados. {"action":"recategorize", '
            '"target":índice, "category":"...", "lifespan":"..."}\n'
            '- "reword": hecho vago que se repite y merece contexto temporal, ej. '
            '"Joan desayunó" -> "Joan suele desayunar antes de trabajar". '
            '{"action":"reword", "target":índice, "fact":"texto nuevo"}\n'
            "Solo incluye acciones con cambios reales y necesarios — si no hace falta "
            "ningún cambio, responde []. Responde solo con el JSON de la lista."
        )
        raw, tokens_used = _groq_call(_MAINT_SYSTEM, user, remaining_budget)
        if raw:
            shared_facts, hugo_facts, merged, recategorized, reworded, split_n, action_mapping = \
                _apply_review_actions(shared_facts, hugo_facts, raw)
            mind_map_mapping.update(action_mapping)

    _save_json(MEMORY_SHARED_PATH, shared_facts)
    _save_json(MEMORY_HUGO_PATH, hugo_facts)

    map_changes = _apply_mind_map_maintenance(mind_map_mapping, promoted_texts)

    _log(
        f"MEMORY MAINTENANCE — deleted {deleted_expired} facts, merged {merged}, "
        f"promoted {promoted} (split={split_n} recategorized={recategorized} "
        f"reworded={reworded} mind_map_updates={map_changes}) — usage: "
        f"flagged_for_review={flagged_for_review} importance_promoted={importance_promoted} "
        f"importance_demoted={importance_demoted}"
    )
    summary = (
        f"deleted={deleted_expired} merged={merged} split={split_n} "
        f"recategorized={recategorized} reworded={reworded} promoted={promoted} "
        f"mind_map_updates={map_changes} flagged_for_review={flagged_for_review} "
        f"importance_promoted={importance_promoted} importance_demoted={importance_demoted}"
    )
    insights = 1 if (
        deleted_expired or merged or split_n or recategorized or reworded or promoted
        or importance_promoted or importance_demoted
    ) else 0
    return tokens_used, insights, summary

def _apply_review_actions(
    shared_facts: list[dict], hugo_facts: list[dict], raw: str,
) -> tuple[list[dict], list[dict], int, int, int, int, dict[str, str | None]]:
    """Parses/applies _phase_memory_maintenance's LLM action list. Returns
    (shared_facts, hugo_facts, merged_count, recategorized_count,
    reworded_count, split_count, mind_map_mapping) — mind_map_mapping maps
    an old fact TEXT to either its replacement text (merge/reword) or None
    (deleted with no single replacement, e.g. split), for
    _apply_mind_map_maintenance to repoint/drop the matching graph edges.
    Returns everything unchanged on any parse failure — a broken response
    here must never corrupt or drop existing facts."""
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        return shared_facts, hugo_facts, 0, 0, 0, 0, {}
    try:
        actions = json.loads(match.group())
    except json.JSONDecodeError:
        return shared_facts, hugo_facts, 0, 0, 0, 0, {}
    if not isinstance(actions, list):
        return shared_facts, hugo_facts, 0, 0, 0, 0, {}

    def _resolve(idx) -> tuple[list[dict], int] | None:
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            return None
        if idx >= _HUGO_INDEX_OFFSET:
            i = idx - _HUGO_INDEX_OFFSET
            return (hugo_facts, i) if 0 <= i < len(hugo_facts) else None
        return (shared_facts, idx) if 0 <= idx < len(shared_facts) else None

    mapping: dict[str, str | None] = {}
    merged_n = recats_n = reworded_n = split_n = 0
    removed_shared: set[int] = set()
    removed_hugo: set[int] = set()
    added_shared: list[dict] = []
    added_hugo: list[dict] = []

    for action in actions[:8]:   # hard safety cap regardless of what the model returns
        if not isinstance(action, dict):
            continue
        kind = action.get("action")

        if kind == "merge":
            raw_targets = action.get("targets", [])
            merged_text = str(action.get("merged_fact", "")).strip()
            if not isinstance(raw_targets, list) or len(raw_targets) < 2 or not merged_text:
                continue
            resolved = [r for r in (_resolve(t) for t in raw_targets) if r]
            if len(resolved) < 2:
                continue
            target_list = resolved[0][0]
            if any(lst is not target_list for lst, _ in resolved):
                continue   # cross-file merge not supported — skip rather than guess
            removed_set = removed_shared if target_list is shared_facts else removed_hugo
            originals = [lst[i] for lst, i in resolved]
            for _, i in resolved:
                removed_set.add(i)
            for o in originals:
                mapping[o["fact"]] = merged_text
            new_fact = {
                "fact": merged_text,
                "category": action.get("category") or originals[0].get("category", "personal"),
                "lifespan": action.get("lifespan") if action.get("lifespan") in _LIFESPAN_VALUES else originals[0].get("lifespan", "permanent"),
                "created_at": min((o.get("created_at") or o.get("added", "")) for o in originals),
                "added": max(o.get("added", "") for o in originals),
                "weight": sum(o.get("weight", 1) for o in originals),
                "outdated": False, "outdated_at": None, "source": "conversation",
            }
            (added_shared if target_list is shared_facts else added_hugo).append(new_fact)
            merged_n += 1

        elif kind == "split":
            resolved = _resolve(action.get("target"))
            new_items = action.get("facts", [])
            if not resolved or not isinstance(new_items, list) or len(new_items) < 2:
                continue
            lst, i = resolved
            original = lst[i]
            (removed_shared if lst is shared_facts else removed_hugo).add(i)
            mapping[original["fact"]] = None
            for item in new_items[:3]:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("fact", "")).strip()
                if not text:
                    continue
                new_fact = {
                    "fact": text,
                    "category": item.get("category") or original.get("category", "personal"),
                    "lifespan": item.get("lifespan") if item.get("lifespan") in _LIFESPAN_VALUES else original.get("lifespan", "permanent"),
                    "created_at": _now_iso(), "added": _now_iso(), "weight": 1,
                    "outdated": False, "outdated_at": None, "source": "conversation",
                }
                (added_shared if lst is shared_facts else added_hugo).append(new_fact)
            split_n += 1

        elif kind == "recategorize":
            resolved = _resolve(action.get("target"))
            if not resolved:
                continue
            lst, i = resolved
            if i in (removed_shared if lst is shared_facts else removed_hugo):
                continue
            f = lst[i]
            changed = False
            if action.get("category"):
                f["category"] = action["category"]
                changed = True
            if action.get("lifespan") in _LIFESPAN_VALUES:
                f["lifespan"] = action["lifespan"]
                changed = True
            if changed:
                recats_n += 1

        elif kind == "reword":
            resolved = _resolve(action.get("target"))
            new_text = str(action.get("fact", "")).strip()
            if not resolved or not new_text:
                continue
            lst, i = resolved
            if i in (removed_shared if lst is shared_facts else removed_hugo):
                continue
            old_text = lst[i]["fact"]
            if new_text == old_text:
                continue
            lst[i]["fact"] = new_text
            mapping[old_text] = new_text
            reworded_n += 1

    shared_out = [f for i, f in enumerate(shared_facts) if i not in removed_shared] + added_shared
    hugo_out   = [f for i, f in enumerate(hugo_facts) if i not in removed_hugo] + added_hugo
    return shared_out, hugo_out, merged_n, recats_n, reworded_n, split_n, mapping

def _apply_mind_map_maintenance(mapping: dict[str, str | None], promoted_texts: set[str]) -> int:
    """Applies Phase 0's fact deletions/merges/rewords to
    data/mind_map_connections.json: an edge whose endpoint text no longer
    exists (mapping[text] is None) is dropped; an edge whose endpoint was
    merged/reworded (mapping[text] is the new text) is repointed at the new
    text instead of left dangling on text nobody will ever see again.
    Promoted facts (weight >= 3 -> 'permanent') get a small strength boost
    on any edge touching them, since a pattern reinforced that often is a
    stronger node in the map, not just a longer-lived fact. Returns how many
    edges were changed/dropped/deduplicated."""
    if not mapping and not promoted_texts:
        return 0
    connections = _load_json(CONNECTIONS_PATH, [])
    if not isinstance(connections, list):
        return 0

    changed = 0
    kept: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for c in connections:
        if not isinstance(c, dict):
            continue
        frm, to = c.get("from", ""), c.get("to", "")
        if mapping.get(frm, "unset") is None or mapping.get(to, "unset") is None:
            changed += 1
            continue
        new_frm = mapping[frm] if isinstance(mapping.get(frm), str) else frm
        new_to  = mapping[to] if isinstance(mapping.get(to), str) else to
        if new_frm != frm or new_to != to:
            changed += 1
        c["from"], c["to"] = new_frm, new_to
        if new_frm in promoted_texts or new_to in promoted_texts:
            c["strength"] = round(min(1.0, c.get("strength", 0.5) + 0.15), 4)
        key = (new_frm, new_to)
        if key in seen:
            changed += 1
            continue
        seen.add(key)
        kept.append(c)

    if changed:
        _save_json(CONNECTIONS_PATH, kept)
    return changed

def _dedup_and_clean(path: str) -> tuple[int, int]:
    """Returns (outdated_removed, duplicates_merged). Caller must not run
    this concurrently with anything else writing the same file — sleep
    sessions are single-threaded and this codebase has no cross-process
    lock for these files, same as core/reflective.py's own append-only
    writes; a lost update here is a rare, low-stakes race, not a
    correctness-critical one."""
    facts = _load_json(path, [])
    if not isinstance(facts, list):
        return 0, 0

    before  = len(facts)
    current = [f for f in facts if isinstance(f, dict) and not f.get("outdated")]
    outdated_removed = before - len(current)

    ordered = sorted(current, key=lambda f: f.get("added", ""), reverse=True)
    kept: list[dict] = []
    for f in ordered:
        if any(_fact_similarity(f.get("fact", ""), k.get("fact", "")) > 0.8 for k in kept):
            continue
        kept.append(f)
    duplicates_merged = len(current) - len(kept)
    kept.sort(key=lambda f: f.get("added", ""))

    if outdated_removed or duplicates_merged:
        _save_json(path, kept)
    return outdated_removed, duplicates_merged

def _phase_memory_cleanup(remaining_budget: int) -> tuple[int, int, str]:
    out1, dup1 = _dedup_and_clean(MEMORY_SHARED_PATH)
    out2, dup2 = _dedup_and_clean(MEMORY_HUGO_PATH)

    episodes = _load_json(EPISODES_PATH, [])
    tokens_used = 0
    episodes_summarized = 0
    if isinstance(episodes, list) and len(episodes) > 15 and remaining_budget > 0:
        oldest = [e for e in episodes[:5] if isinstance(e, dict)]
        rest   = episodes[5:]
        lines  = "\n".join(f"- [{e.get('date', '?')}] {e.get('topic', '')}: {e.get('summary', '')}" for e in oldest)
        text, tokens_used = _groq_call(
            "Resumes episodios antiguos en uno solo, breve y neutral, en tercera persona.",
            f"Condensa estos episodios antiguos en UN solo resumen breve:\n{lines}",
            remaining_budget,
        )
        if text and oldest:
            consolidated = {
                "date": oldest[-1].get("date", _today()), "summary": text,
                "topic": "Resumen de episodios antiguos", "emotional_tone": "neutral",
                "key_facts": [], "importance": 2,
            }
            _save_json(EPISODES_PATH, [consolidated] + rest)
            episodes_summarized = len(oldest)

    insights = (1 if out1 + dup1 else 0) + (1 if out2 + dup2 else 0) + (1 if episodes_summarized else 0)
    summary = (
        f"outdated_removed={out1 + out2} duplicates_merged={dup1 + dup2} "
        f"episodes_summarized={episodes_summarized}"
    )
    return tokens_used, insights, summary
