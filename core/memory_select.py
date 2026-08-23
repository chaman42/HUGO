# Relevance-scored fact selection for the CONTEXTO RELEVANTE prompt block.
# Split out of core/memory.py (pure refactor, no behavior change).
import datetime
import json

from core.memory_store import (
    MAX_RELEVANT_FACTS,
    MEMORY_SHARED_PATH,
    _get_personality_memory_path,
    _is_fact_expired,
    _keywords,
    _load_fact_file,
    time_since,
)

# ---------------------------------------------------------------------------
# Layer 1 / Layer 2 — active memory connection
#
# Instead of dumping every stored fact into the prompt every time, facts are
# scored against what the user just said (simple keyword overlap, see
# _keywords) and only the relevant handful are surfaced, under a single
# CONTEXTO RELEVANTE section (see _build_system_prompt) — e.g. asking about
# swimming surfaces swim-club/training facts, not unrelated armor facts.
# ---------------------------------------------------------------------------

def _load_shared_facts() -> list[dict]:
    """Layer 1: facts about the user, shared across every personality.
    Excludes facts marked outdated (see _mark_fact_outdated) and facts whose
    lifespan has expired (see _is_fact_expired) — both stay in the file for
    history/consolidation (and, for expired ones, for Sleep Phase 0 to
    actually delete — see core/sleep.py) but never surface in conversation."""
    return [
        f for f in _load_fact_file(MEMORY_SHARED_PATH, default_category="personal")
        if not f.get("outdated") and not _is_fact_expired(f)
    ]


def _load_personality_facts(personality: str) -> list[dict]:
    """Layer 2: facts about the relationship with ONE specific personality —
    never merged with another personality's file. Excludes outdated and
    lifespan-expired facts, same as _load_shared_facts."""
    return [
        f for f in _load_fact_file(_get_personality_memory_path(personality), default_category="context")
        if not f.get("outdated") and not _is_fact_expired(f)
    ]


def _fact_temporal_weight(fact: dict) -> float:
    """Decay multiplier based on how long ago 'added' was — which doubles as
    "last mentioned" since _upsert_fact refreshes it on every reinforcement,
    not just on creation. A fact reinforced last week is "recent" even if it
    was first created months ago; a fact created once and never mentioned
    again decays the way its 'added' date suggests. Missing/unparseable
    dates are treated as full weight rather than penalized."""
    added = fact.get("added")
    if not added:
        return 1.0
    try:
        added_dt = datetime.datetime.fromisoformat(added)
    except ValueError:
        return 1.0
    age_days = (datetime.datetime.now() - added_dt).days
    if age_days <= 7:
        return 1.0
    if age_days <= 30:
        return 0.8
    if age_days <= 90:
        return 0.5
    return 0.2


def _fact_usage_score(fact: dict, now: datetime.datetime | None = None) -> float:
    """Usage-based priority score (Memory V2 Part B) — added on top of
    keyword relevance as a tie-breaker in _select_relevant_facts, and
    usable standalone anywhere facts need ranking (e.g. memory_stats).

    Base score is 'importance' (1-5). +1 if the fact was actually injected
    into a prompt within the last 7 days ('last_used' — recency bonus).
    +1 if the user said something matching it within the last 7 days
    ('last_reinforced' — reinforcement bonus). -1.5 if it hasn't been used
    in 90+ days (falling back to 'added' when never used), UNLESS its
    importance is already 5 — a fact important enough to max out its
    rating shouldn't get penalized just for being quiet for a while."""
    now = now or datetime.datetime.now()
    importance = fact.get("importance", 3)
    score = float(importance)

    last_used = fact.get("last_used")
    if last_used:
        try:
            if (now - datetime.datetime.fromisoformat(last_used)).days <= 7:
                score += 1.0
        except ValueError:
            pass

    last_reinforced = fact.get("last_reinforced")
    if last_reinforced:
        try:
            if (now - datetime.datetime.fromisoformat(last_reinforced)).days <= 7:
                score += 1.0
        except ValueError:
            pass

    reference = last_used or fact.get("added")
    if reference and importance < 5:
        try:
            if (now - datetime.datetime.fromisoformat(reference)).days > 90:
                score -= 1.5
        except ValueError:
            pass

    return score


def _select_relevant_facts(
    user_message: str, facts: list[dict], max_facts: int = MAX_RELEVANT_FACTS,
) -> list[dict]:
    """'Does any memory fact relate to what the user just said?' — simple
    keyword-overlap relevance scoring (see _keywords), not semantic
    matching. Only facts sharing at least one keyword are considered at
    all; among those, higher overlap counts as higher priority — relevance
    always dominates, so a highly relevant fact is never pushed out by a
    fresher but less relevant one. Ties (same overlap) are broken by
    reinforcement weight combined with temporal decay (see
    _fact_temporal_weight) plus usage-based priority (see
    _fact_usage_score — importance, recency-of-use, reinforcement, and
    staleness decay), so among equally relevant facts a rarely-mentioned,
    rarely-used one loses out to a recently reinforced/injected one —
    this only ever penalizes facts that were already low-relevance ties,
    never a strong keyword match."""
    msg_kw = _keywords(user_message)
    if not msg_kw:
        return []
    now = datetime.datetime.now()
    scored = []
    for f in facts:
        overlap = len(msg_kw & _keywords(f["fact"]))
        if overlap > 0:
            priority = f.get("weight", 1) * _fact_temporal_weight(f) + _fact_usage_score(f, now)
            scored.append((overlap, priority, f))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [f for _, _, f in scored[:max_facts]]


# ---------------------------------------------------------------------------
# Associative expansion — data/mind_map_connections.json, built by the
# sleep/reflective phase (see core.reflective._add_connection /
# _link_new_fact) to back the Mapa Mental UI panel. This is the "that
# reminds me of..." jump on top of the purely lexical match above — a real
# associative graph, not decoration.
#
# Backed by core/mindmap_graph.py's NetworkX traversal (2026-08-20) rather
# than a manual one-hop walk over the raw edge list — see that module's own
# docstring for why: a genuine A-B-C chain where A and C were never
# directly compared by the reflective phase used to be a structural blind
# spot for the old one-hop-only code, not just a missed edge case.
# ---------------------------------------------------------------------------

MIN_CONNECTION_STRENGTH = 0.3   # below this an edge is noise (see _add_connection's own strength scoring), not worth surfacing
MAX_CONNECTED_FACTS     = 2     # cap — associative expansion rides along with keyword matches, shouldn't outgrow them
MAX_CONNECTION_HOPS     = 2     # how many edges away a fact can still count as "connected"


def _expand_with_connections(selected_facts: list[dict], pool: list[dict]) -> list[dict]:
    """Multi-hop walk (see core.mindmap_graph.expand_multi_hop) over the
    connections graph, up to MAX_CONNECTION_HOPS away from each already
    keyword-matched fact. Connections reference facts by their literal text
    (see core.reflective._add_connection's from_node/to_node), so `pool` —
    the same fact list _select_relevant_facts scored against — is needed
    both to resolve that text back to a real fact object (id, category,
    importance) for formatting/usage-tracking downstream, and to restrict
    results to facts that still actually exist (a connection can reference
    a fact since marked outdated)."""
    if not selected_facts:
        return []
    try:
        from core import mindmap_graph
    except Exception:
        return []
    by_text = {f["fact"]: f for f in pool if f.get("fact")}
    seed_texts = [f["fact"] for f in selected_facts if f.get("fact")]
    ranked = mindmap_graph.expand_multi_hop(
        seed_texts, set(by_text.keys()),
        max_hops=MAX_CONNECTION_HOPS, max_results=MAX_CONNECTED_FACTS,
        min_strength=MIN_CONNECTION_STRENGTH,
    )
    return [by_text[text] for text, _strength in ranked if text in by_text]


# ---------------------------------------------------------------------------
# Semantic expansion — core/embeddings.py's local Chroma index. Catches what
# both the keyword scoring above AND the connections graph miss: a fact
# phrased with entirely different words from the user's message AND never
# explicitly linked by the reflective phase (e.g. it predates that feature,
# or the reflective session never happened to compare that particular
# pair). 'practica natación' / 'le gusta nadar' share zero keywords and
# have no graph edge unless reflective already drew one — cosine distance
# catches it directly, no keyword or graph dependency at all.
#
# Purely additive, same as _expand_with_connections — never replaces the
# keyword-scored results, and degrades to a no-op (not an error) if
# core.embeddings isn't available (chromadb/sentence-transformers not
# installed, or the model fails to load) so this is safe to call
# unconditionally.
# ---------------------------------------------------------------------------

MAX_SEMANTIC_FACTS  = 2      # cap — rides along with keyword matches, same budget reasoning as MAX_CONNECTED_FACTS
MAX_SEMANTIC_DISTANCE = 0.6  # cosine distance ceiling (normalized embeddings) — below this counts as genuinely related; not empirically tuned yet, revisit once real usage data exists


def _expand_with_semantic_search(user_message: str, already_selected: list[dict], pool: list[dict]) -> list[dict]:
    """Queries core.embeddings for facts semantically close to the raw user
    message, independent of both keyword overlap and the connections
    graph. Matches results back to `pool` by fact text (embeddings.py
    indexes facts with id 'fact:{fact_id_or_text_prefix}', but text is the
    reliable join key here since pool's own dicts are the source of truth
    for category/weight/id used downstream)."""
    if not user_message:
        return []
    try:
        from core import embeddings as embeddings_mod
    except Exception:
        return []
    seen_texts = {f["fact"] for f in already_selected if f.get("fact")}
    by_text = {f["fact"]: f for f in pool if f.get("fact")}
    matches = embeddings_mod.query(user_message, n_results=MAX_SEMANTIC_FACTS + len(seen_texts), doc_type="fact")
    extra: list[dict] = []
    for m in matches:
        if m.get("distance", 1.0) > MAX_SEMANTIC_DISTANCE:
            continue
        text = m.get("text")
        if not text or text in seen_texts:
            continue
        fact = by_text.get(text)
        if fact:
            extra.append(fact)
            seen_texts.add(text)
            if len(extra) >= MAX_SEMANTIC_FACTS:
                break
    return extra


def _natural_time_ago(date_str: str | None) -> str:
    """Thin wrapper over core.memory_store.time_since — kept under its old
    name since it's imported by core/memory_episodes.py and referenced from
    core/personalities/base.py. Computed fresh at call time, never stored
    (see time_since's own docstring: facts on disk only ever keep an
    absolute ISO date)."""
    return time_since(date_str)


# Display order for type groups in _format_relevant_facts_block — roughly
# "who/what's going on" before "how they are", so the block reads like a
# person would introduce someone rather than a random bag of facts.
_TYPE_GROUP_ORDER = [
    "persona", "proyecto", "evento", "decision",
    "preferencia", "habilidad", "lugar", "patron",
]
_TYPE_GROUP_LABELS = {
    "persona":     "Personas",
    "proyecto":    "Proyectos",
    "evento":      "Eventos",
    "decision":    "Decisiones",
    "preferencia": "Preferencias",
    "habilidad":   "Habilidades",
    "lugar":       "Lugares",
    "patron":      "Otros",
}


def _format_relevant_facts_block(facts: list[dict]) -> str:
    """'when' is based on 'created_at' (when the fact was first learned),
    not 'added' (last reinforced) — per the temporal-awareness rule: recent
    facts (< 24h, i.e. 'hoy'/'ayer') can be referenced directly, older ones
    get a relative-time hedge ('hace unos días', 'la semana pasada', ...),
    so LIRA never implies she just learned something she's actually known
    for a while.

    Grouped by 'type' (Memory V2 structured field — see _CONTENT_TYPES in
    core/memory_store.py) rather than dumped flat, and ordered within each
    group by 'importance' (highest first) — so the handful of facts
    surfaced per prompt read as organized recall ('sobre sus proyectos: ...
    sobre gente: ...') instead of an arbitrary list. Group headers are
    skipped entirely when everything selected happens to share one type,
    since a single-line header would add noise without adding information."""
    if not facts:
        return ""
    groups: dict[str, list[dict]] = {}
    for f in facts:
        groups.setdefault(f.get("type", "patron"), []).append(f)

    def _fact_line(f: dict) -> str:
        # Prefers 'date_event' (when the thing actually happened) over
        # 'created_at' (when it was learned) — time_since(date_event) is
        # the spec'd call; falls back to created_at/added for facts with
        # no event date (preferences, skills — nothing "happened").
        when   = time_since(f.get("date_event") or f.get("created_at") or f.get("added"))
        prefix = f"({when}) " if when else ""
        # Entity Pillars Phase 1 (facts vs. interpretations, see
        # core/epistemics.py): 'inferred' facts (core.reflective's pattern
        # extraction, never something Joan said outright) get a visible
        # hedge marker so LIRA doesn't state her own conclusion with the
        # same certainty as something Joan told her directly. 'stated'
        # facts (the vast majority) are unmarked, same as before this field
        # existed.
        if f.get("epistemic") == "inferred":
            prefix += "(crees que) "
        return f"- {prefix}{f['fact']}"

    if len(groups) <= 1:
        only = next(iter(groups.values())) if groups else facts
        ordered = sorted(only, key=lambda f: f.get("importance", 3), reverse=True)
        return "\n".join(_fact_line(f) for f in ordered)

    blocks = []
    for type_ in sorted(groups, key=lambda t: (_TYPE_GROUP_ORDER.index(t) if t in _TYPE_GROUP_ORDER else len(_TYPE_GROUP_ORDER))):
        group_facts = sorted(groups[type_], key=lambda f: f.get("importance", 3), reverse=True)
        label = _TYPE_GROUP_LABELS.get(type_, type_.capitalize())
        lines = "\n".join(_fact_line(f) for f in group_facts)
        blocks.append(f"{label}:\n{lines}")
    return "\n".join(blocks)
