# Layer 1/2 fact-object storage: schema, similarity, dedup, and upsert onto
# disk. Split out of core/memory.py (pure refactor, no behavior change).
import json
import logging
import os
import re
import threading
import uuid
import datetime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FOUR-LAYER MEMORY ARCHITECTURE
#
# LAYER 1 — memory_shared.json: facts about the human USER, shared across
#   every personality. Categories: personal | preference | project | skill |
#   relationship. Written automatically by _extract_and_save_memory().
#
# LAYER 2 — memory_<personality>.json: facts about the RELATIONSHIP with one
#   specific personality (interaction style, inside references, what's been
#   shared with THAT personality). Categories: interaction | preference |
#   context | reference. NEVER written by _extract_and_save_memory() — these
#   files are manually curated only (edit the JSON directly).
#
# LAYER 3 — memory_instructions.json: static behavioral rules (capabilities,
#   limitations, roadmap) per personality. NEVER written by
#   _extract_and_save_memory() — manually curated, hot-reloadable via
#   reload_instructions() / POST /api/reload_instructions.
#
# LAYER 4 — volatile data (core/tools.py + this module): time, date, weather,
#   location, session duration, active personality, listen mode. Always
#   fetched fresh, never persisted as a fact — enforced by the
#   _TEMPORAL_FACT_PATTERNS check in _extract_and_save_memory (no exceptions).
#
# Layers 1 and 2 share the same fact-object schema:
#   {"fact": str, "category": str, "added": ISO8601 str, "weight": int}
# `weight` increases by 1 each time an equivalent fact is reinforced in
# conversation (see _upsert_fact) instead of being stored as a duplicate.
# ---------------------------------------------------------------------------

MEMORY_LIRA_PATH         = "data/memory_lira.json"
MEMORY_SHARED_PATH       = "data/memory_shared.json"
# MEMORY_INSTRUCTIONS_PATH (Layer 3) lives in core/memory_setup.py, next to
# the instructions-loading code that actually uses it.

# Cap on how many memory facts get surfaced in the CONTEXTO RELEVANTE block
# per prompt (see _select_relevant_facts) — deliberately small, since these
# are meant to be the handful of facts that actually relate to what the
# user just said, not a dump of everything known about them.
MAX_RELEVANT_FACTS = 8

# Facts sharing more than this fraction of keywords (Jaccard similarity over
# lowercased word sets — a cheap, dependency-free stand-in for semantic
# similarity) are treated as "the same fact": deduped on load, and a new
# extraction that matches an existing fact bumps its weight instead of being
# appended as a duplicate.
_FACT_SIMILARITY_THRESHOLD = 0.8

_SHARED_CATEGORIES      = {"personal", "preference", "project", "skill", "relationship"}
_PERSONALITY_CATEGORIES = {"interaction", "preference", "context", "reference"}

# ---------------------------------------------------------------------------
# STRUCTURED KNOWLEDGE (Memory V2) — every Layer 1/2 fact additionally
# carries a structured-content side alongside its plain-text 'fact'/
# 'category': 'type' classifies WHAT KIND of thing was learned (closer to
# real-world semantics than 'category', which is about where it's routed),
# 'content' is the parsed-out detail (summary/place/people/context), and
# 'date_event'/'date_recorded'/'importance'/'tags' round out the object.
# 'fact' (plain text) and 'category' are kept as-is and remain the ground
# truth for similarity/dedup/routing — 'type'/'content' are additive, never
# replace them, so every existing call site that only knows about
# 'fact'/'category' keeps working unchanged.
# ---------------------------------------------------------------------------
_CONTENT_TYPES = {
    "evento", "preferencia", "habilidad", "proyecto",
    "persona", "lugar", "decision", "patron",
}

# Best-effort default 'type' for a fact that only has a 'category' (legacy
# facts, or a fact upserted by code that doesn't know about 'type' yet) —
# used by _normalize_fact so every fact always has a valid 'type', never a
# missing/blank one.
_CATEGORY_TO_TYPE = {
    "preference":  "preferencia",
    "project":     "proyecto",
    "skill":       "habilidad",
    "relationship": "persona",
    "personal":    "patron",
    "interaction": "patron",
    "context":     "evento",
    "reference":   "patron",
}

# ---------------------------------------------------------------------------
# Variable-lifespan facts — every Layer 1/2 fact carries a 'lifespan'
# (how long it stays valid) alongside its 'category' (what kind of thing it
# is). Classified automatically by the LLM in _extract_and_save_memory(),
# never by the user:
#   permanent — identity, skills, projects, preferences, relationships,
#               achievements. Never expires.
#   weekly    — ongoing situations, current project status, recent
#               decisions. Expires after _LIFESPAN_EXPIRY_HOURS['weekly'].
#   daily     — today's plans, current mood/energy, what happened today.
#   hourly    — current state ('acaba de desayunar', 'tiene sueño ahora',
#               'está en Madrid hoy').
# Missing/invalid lifespan (legacy facts predating this field, or a bad LLM
# response) defaults to 'permanent' — the safe direction, since it just
# keeps the old always-persist behavior rather than silently expiring facts
# that were never meant to.
# 'created_at' is the fact's original creation timestamp — unlike 'added'
# (refreshed on every reinforcement, see _upsert_fact), it never changes
# after creation, which is what expiry and "when was this created" temporal
# references (_format_relevant_facts_block) are anchored to.
# ---------------------------------------------------------------------------
_LIFESPAN_VALUES = {"permanent", "weekly", "daily", "hourly"}
_LIFESPAN_EXPIRY_HOURS = {"hourly": 3, "daily": 48, "weekly": 240}   # weekly = 10 days; permanent never expires

# Fact counts above this in a single Layer 1/2 file are flagged at startup
# (and by GET /api/memory_stats) as needing a POST /api/memory_clean run.
_MEMORY_HEALTH_WARN_THRESHOLD = 100

# Words that must never appear in extracted memory facts.
# Includes every assistant name, every wake-word phonetic variant, and
# related proper nouns that the LLM might confuse with the user's name.
_MEMORY_BLACKLIST: frozenset[str] = frozenset({
    # Primary assistant names
    "jarvis", "friday", "lira",
    # LIRA / lyra variants
    "lyra", "leera", "liera", "liira", "lirra",
    # Jarvis variants
    "jarbi", "yarvis", "harviz", "jarvi", "jarbs", "jarbis", "harvey",
    # Friday variants
    "fraidy", "fraiday", "frade", "fridei", "fraydi", "freday",
    "fride", "fraday", "fridy", "frida",
    # Other virtual assistant names that could be confused
    "siri", "alexa", "cortana", "bixby",
    # Common false-positive names from STT
    "arby", "arbys", "bradley",
})

# Regex patterns for ephemeral / temporal facts that must never be stored.
# Catches "La hora actual es 10:33", "La temperatura en X es de Y°C",
# "inició el día a las HH:MM", and similar time-snapshot statements.
_TEMPORAL_FACT_PATTERNS: list[re.Pattern] = [
    re.compile(r'\bla\s+hora\s+actual\s+es\b', re.IGNORECASE),
    re.compile(r'\bhora\s+que\s+consulta\b', re.IGNORECASE),
    re.compile(r'\binici[oó]\s+el\s+d[ií]a\s+a\s+las\b', re.IGNORECASE),
    re.compile(r'\bha\s+iniciado\s+(la\s+interacci[oó]n|el\s+d[ií]a)\b', re.IGNORECASE),
    re.compile(r'\bla\s+temperatura\b.*\bes\s+de\b', re.IGNORECASE),
    re.compile(r'\bcerca\s+de\s+la\s+hora\b', re.IGNORECASE),
    re.compile(r'\b\d{1,2}:\d{2}(:\d{2})?\b'),   # any bare HH:MM or HH:MM:SS
]

_DAYS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_MONTHS_ES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]

# ---------------------------------------------------------------------------
# Layer 1 / Layer 2 — fact-object storage, similarity, dedup, upsert
# ---------------------------------------------------------------------------

_memory_lock = threading.Lock()


def _get_personality_memory_path(personality: str) -> str:
    return {
        "lira": MEMORY_LIRA_PATH,
    }.get(personality, MEMORY_SHARED_PATH)


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# time_since() — Memory V2 Part B. The single source of truth for turning an
# absolute date (a fact's 'date_event'/'created_at', never a relative
# expression — see _TEMPORAL_FACT_PATTERNS and memory_extract.py's own
# 'NUNCA guardes expresiones relativas' instruction to the LLM) into a
# natural Spanish relative-time phrase, computed fresh at read/query time.
# Facts on disk never store 'hace tres semanas' — only an ISO date; every
# place that surfaces a fact's age to the user (prompt injection, spoken
# replies) must call this instead of formatting a date directly, so the
# phrase is always correct relative to *now*, not to whenever the fact was
# written.
# ---------------------------------------------------------------------------

_SEASON_NAMES_ES = ["este invierno", "esta primavera", "este verano", "este otoño"]


def _season_index(d: datetime.date) -> int:
    """0=invierno, 1=primavera, 2=verano, 3=otoño (Northern-hemisphere,
    meteorological seasons — Dec-Feb / Mar-May / Jun-Aug / Sep-Nov)."""
    if d.month in (12, 1, 2):
        return 0
    if d.month in (3, 4, 5):
        return 1
    if d.month in (6, 7, 8):
        return 2
    return 3


def time_since(date_event, reference: datetime.datetime | None = None) -> str:
    """Natural Spanish relative-time expression for *date_event* (an ISO
    date or timestamp string), computed against *reference* (defaults to
    now). Returns '' on missing/unparseable/future input rather than
    guessing — callers can skip the phrase entirely in that case.

    Buckets, per spec:
      < 24h            -> 'hoy' / 'hace X horas'
      1-6 days         -> 'hace X días'
      1-3 weeks        -> 'hace X semanas'
      same season as *reference*, roughly 3-14 weeks out -> 'este verano' /
                           'esta primavera' / etc. (preferred over a bare
                           month count when it applies)
      1-11 months       -> 'hace X meses'
      1-2 years         -> 'hace aproximadamente un año' / 'hace un año y
                           X meses'
      > 2 years         -> 'en <year>'
    """
    if not date_event:
        return ""
    try:
        text = str(date_event)
        event_dt = datetime.datetime.fromisoformat(text[:19]) if len(text) > 10 else \
            datetime.datetime.combine(datetime.date.fromisoformat(text[:10]), datetime.time.min)
    except (ValueError, TypeError):
        return ""

    now = reference or datetime.datetime.now()
    if event_dt > now:
        return ""

    event_date = event_dt.date()
    now_date   = now.date()
    day_diff   = (now_date - event_date).days

    if day_diff <= 0:
        hours = (now - event_dt).total_seconds() / 3600
        if hours < 1:
            return "hoy"
        h = round(hours)
        return "hoy" if h <= 0 else f"hace {h} hora{'s' if h != 1 else ''}"

    if day_diff <= 6:
        return f"hace {day_diff} día{'s' if day_diff != 1 else ''}"

    if day_diff <= 21:
        weeks = max(1, round(day_diff / 7))
        return f"hace {weeks} semana{'s' if weeks != 1 else ''}"

    if 21 < day_diff <= 100 and _season_index(event_date) == _season_index(now_date):
        return _SEASON_NAMES_ES[_season_index(now_date)]

    if day_diff <= 335:
        months = max(1, min(11, round(day_diff / 30)))
        return f"hace {months} mes{'es' if months != 1 else ''}"

    years = day_diff / 365
    if years <= 2:
        extra_months = round((years - 1) * 12)
        if extra_months <= 0:
            return "hace aproximadamente un año"
        return f"hace un año y {extra_months} mes{'es' if extra_months != 1 else ''}"

    return f"en {event_date.year}"


def _fact_similarity(a: str, b: str) -> float:
    """Jaccard similarity over lowercased word sets — a cheap, dependency-free
    stand-in for semantic similarity. Values > _FACT_SIMILARITY_THRESHOLD are
    treated as 'the same fact' by both dedup-on-load and _upsert_fact."""
    wa = set(re.findall(r"\w+", a.lower()))
    wb = set(re.findall(r"\w+", b.lower()))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


# Common short Spanish function words — stripped before keyword matching
# (see _keywords) so relevance scoring reacts to actual topic words
# ("natación", "examen") instead of grammatical glue words shared by every
# sentence regardless of subject.
_STOPWORDS_ES = frozenset({
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del", "al",
    "y", "o", "u", "que", "en", "a", "por", "para", "con", "sin", "es", "son",
    "se", "su", "sus", "lo", "le", "les", "mi", "mis", "tu", "tus", "yo",
    "tú", "él", "ella", "nosotros", "vosotros", "ellos", "ellas", "me",
    "te", "nos", "os", "más", "pero", "como", "cuando", "donde", "qué",
    "quién", "cómo", "cuál", "cuáles", "cuánto", "cuánta", "cuántos", "muy",
    "ya", "este", "esta", "esto", "estos", "estas", "ese", "esa", "eso",
    "esos", "esas", "también", "hay", "no", "sí", "si", "soy", "eres", "era",
    "fue", "ser", "estar", "está", "están", "he", "has", "ha", "han",
})


def _keywords(text: str) -> set[str]:
    """Lowercased word set with short/stopword noise filtered out — the
    'simple keyword matching' basis for both memory relevance scoring
    (_select_relevant_facts) and recurring-topic detection
    (_recurring_topic)."""
    words = re.findall(r"\w+", text.lower())
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS_ES}


def _normalize_fact(item, default_category: str) -> dict | None:
    """Accept either the new {fact, category, added, weight, outdated} object
    or a legacy plain string, and return the canonical object form. A legacy
    string is upgraded in memory with today's timestamp and weight 1 — the
    file itself is rewritten in the new format on the next save.

    'outdated' (see _mark_fact_outdated) marks a fact superseded by a newer,
    contradicting one — kept for history instead of deleted, but excluded
    from conversation surfacing (_load_shared_facts / _load_personality_facts).

    'source' ('conversation' | 'reflective') is passed through as-is rather
    than dropped — every fact file here gets rewritten wholesale through
    _normalize_fact on each save (dedup, consolidation, reinforcement), so
    without carrying this field through, core/reflective.py's 'reflective'
    tag on a fact would silently vanish the next time anything touched the
    file. Missing/unrecognized ⇒ 'conversation' (matches every fact written
    before this field existed).

    'epistemic' ('stated' | 'inferred', see core/epistemics.py) is a
    different axis than 'source': 'source' says which pipeline wrote the
    fact, 'epistemic' says whether Joan actually said it or LIRA concluded
    it from a pattern. Missing/unrecognized defaults off 'source' —
    'reflective' facts are pattern-derived (⇒ 'inferred'), everything else
    is Joan's own words (⇒ 'stated') — so every fact written before this
    field existed still gets a sane label.

    Structured-knowledge fields (Memory V2 — see _CONTENT_TYPES above):
    'id' (stable uuid, minted once), 'raw' (the original sentence the fact
    was parsed from — defaults to 'fact' when absent), 'type' (defaults via
    _CATEGORY_TO_TYPE off 'category' when absent/invalid), 'content' (parsed
    detail dict — defaults to {'summary': fact}), 'date_event' (absolute
    ISO date the fact's event happened on, or None — never a relative
    expression), 'date_recorded' (defaults to 'added's date), 'importance'
    (1-5, defaults to 3), 'tags' (defaults to []), and 'structured' (True
    only once an LLM — Groq extraction or the Ollama migration — has
    actually produced 'content'/'type' for this fact; False means it's
    still running on defaults and is a migration candidate, see
    core/memory_migrate.py).

    Usage tracking (Memory V2 Part B — see mark_facts_used / time_since):
    'last_used' (ISO timestamp of the last time this fact was actually
    injected into a system prompt, or None if never), 'use_count' (how many
    times that's happened), 'last_reinforced' (ISO timestamp of the last
    time the user said something that matched this fact closely enough to
    bump its 'weight' — see _upsert_fact — or None). All three default to
    "never" for a fact that predates this field."""
    if isinstance(item, dict) and str(item.get("fact", "")).strip():
        fact_text = str(item["fact"]).strip()
        category = item.get("category")
        source   = item.get("source")
        lifespan = item.get("lifespan")
        added    = item.get("added") or _now_iso()
        type_    = item.get("type")
        if type_ not in _CONTENT_TYPES:
            type_ = _CATEGORY_TO_TYPE.get(category if category else default_category, "patron")
        content = item.get("content")
        if not isinstance(content, dict) or not content:
            content = {"summary": fact_text}
        importance = item.get("importance")
        try:
            importance = int(importance)
        except (TypeError, ValueError):
            importance = 3
        importance = max(1, min(5, importance))
        tags = item.get("tags")
        if not isinstance(tags, list):
            tags = []
        tags = [str(t).strip() for t in tags if str(t).strip()]
        source = source if source in ("conversation", "reflective") else "conversation"
        epistemic = item.get("epistemic")
        if epistemic not in ("stated", "inferred"):
            epistemic = "inferred" if source == "reflective" else "stated"
        return {
            "fact":       fact_text,
            "category":   category if category else default_category,
            "lifespan":   lifespan if lifespan in _LIFESPAN_VALUES else "permanent",
            "added":      added,
            "created_at": item.get("created_at") or added,
            "weight":     int(item.get("weight", 1)),
            "outdated":   bool(item.get("outdated", False)),
            "outdated_at": item.get("outdated_at"),
            "outdated_reason": item.get("outdated_reason"),
            "source":     source,
            "epistemic":  epistemic,
            "id":         item.get("id") or uuid.uuid4().hex,
            "raw":        str(item.get("raw") or fact_text).strip(),
            "type":       type_,
            "content":    content,
            "date_event": item.get("date_event") or None,
            "date_recorded": item.get("date_recorded") or added[:10],
            "importance": importance,
            "tags":       tags,
            "structured": bool(item.get("structured", False)),
            "last_used":       item.get("last_used"),
            "use_count":       int(item.get("use_count") or 0),
            "last_reinforced": item.get("last_reinforced"),
        }
    if isinstance(item, str) and item.strip():
        now = _now_iso()
        fact_text = item.strip()
        type_ = _CATEGORY_TO_TYPE.get(default_category, "patron")
        return {
            "fact": fact_text, "category": default_category, "lifespan": "permanent",
            "added": now, "created_at": now,
            "weight": 1, "outdated": False, "outdated_at": None, "outdated_reason": None,
            "source": "conversation", "epistemic": "stated",
            "id": uuid.uuid4().hex, "raw": fact_text, "type": type_,
            "content": {"summary": fact_text}, "date_event": None,
            "date_recorded": now[:10], "importance": 3, "tags": [],
            "structured": False,
            "last_used": None, "use_count": 0, "last_reinforced": None,
        }
    return None


def _is_fact_expired(fact: dict) -> bool:
    """True if *fact*'s lifespan has run out, based on its 'created_at'
    (never 'added' — reinforcement refreshes 'added' but must not reset the
    expiry clock, or a fact could be kept alive forever by repeating it).
    'permanent' (and any unrecognized lifespan) never expires."""
    lifespan = fact.get("lifespan", "permanent")
    if lifespan not in _LIFESPAN_EXPIRY_HOURS:
        return False
    created_at = fact.get("created_at") or fact.get("added")
    if not created_at:
        return False
    try:
        created_dt = datetime.datetime.fromisoformat(created_at)
    except ValueError:
        return False
    age_hours = (datetime.datetime.now() - created_dt).total_seconds() / 3600
    return age_hours > _LIFESPAN_EXPIRY_HOURS[lifespan]


def _dedup_facts(facts: list[dict]) -> list[dict]:
    """Semantic dedup: when two facts share > _FACT_SIMILARITY_THRESHOLD of
    their keywords, keep only the newer one (by 'added' timestamp)."""
    ordered = sorted(facts, key=lambda f: f.get("added", ""), reverse=True)
    kept: list[dict] = []
    for f in ordered:
        if any(_fact_similarity(f["fact"], k["fact"]) > _FACT_SIMILARITY_THRESHOLD for k in kept):
            continue
        kept.append(f)
    kept.sort(key=lambda f: f.get("added", ""))  # restore chronological order
    return kept


def _load_fact_file(path: str, default_category: str) -> list[dict]:
    """Load a Layer 1/2 fact file, upgrading any legacy plain-string entries
    in memory, then apply semantic dedup (see _dedup_facts) before returning."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    facts = [nf for nf in (_normalize_fact(x, default_category) for x in raw) if nf]
    return _dedup_facts(facts)


def _save_fact_file(path: str, facts: list[dict]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(facts, f, ensure_ascii=False, indent=2)


def _upsert_fact(
    path: str, fact_text: str, category: str, default_category: str, lifespan: str = "permanent",
    *, type_: str | None = None, content: dict | None = None, date_event: str | None = None,
    importance: int | None = None, tags: list[str] | None = None, raw: str | None = None,
    structured: bool = False,
) -> None:
    """Add a new fact, or — if it matches one already stored (see
    _fact_similarity) — just bump that fact's weight (reinforcement) instead
    of storing a duplicate. 'added' is refreshed to now on reinforcement too,
    so it doubles as "last mentioned" for temporal decay (see
    _fact_temporal_weight) — a fact repeatedly reinforced stays "recent"
    even if first created long ago. 'created_at' is deliberately NOT touched
    on reinforcement — it anchors lifespan expiry (_is_fact_expired) and must
    reflect the fact's original creation, not its last mention. Caller must
    hold _memory_lock.

    The structured-knowledge kwargs (type_/content/date_event/importance/
    tags/raw/structured — see _CONTENT_TYPES) are all optional: omitted,
    _normalize_fact fills sane defaults so every caller written before
    Memory V2 keeps working unchanged. On reinforcement, any of them that
    ARE passed overwrite the stored value — a later extraction pass often
    has richer detail (e.g. a date_event) than the first mention did."""
    if lifespan not in _LIFESPAN_VALUES:
        lifespan = "permanent"
    facts = _load_fact_file(path, default_category)
    for f in facts:
        if _fact_similarity(fact_text, f["fact"]) > _FACT_SIMILARITY_THRESHOLD:
            f["weight"] = f.get("weight", 1) + 1
            f["added"] = _now_iso()
            f["last_reinforced"] = f["added"]
            f.setdefault("created_at", f["added"])
            if type_ is not None:
                f["type"] = type_
            if content is not None:
                f["content"] = content
            if date_event is not None:
                f["date_event"] = date_event
            if importance is not None:
                f["importance"] = importance
            if tags is not None:
                f["tags"] = tags
            if raw is not None:
                f["raw"] = raw
            if structured:
                f["structured"] = True
            _save_fact_file(path, facts)
            return
    now = _now_iso()
    new_fact = _normalize_fact(
        {
            "fact": fact_text, "category": category, "lifespan": lifespan,
            "added": now, "created_at": now, "weight": 1,
            "outdated": False, "outdated_at": None, "source": "conversation",
            "type": type_, "content": content, "date_event": date_event,
            "date_recorded": now[:10], "importance": importance, "tags": tags,
            "raw": raw, "structured": structured,
        },
        default_category,
    )
    facts.append(new_fact)
    _save_fact_file(path, facts)


def mark_facts_used(paths: list[str], fact_ids: set[str]) -> None:
    """Bumps 'last_used'/'use_count' for every fact in *fact_ids* across
    every file in *paths* — called once per prompt build, right after
    _select_relevant_facts picks the handful of facts actually injected
    (see core/personalities/base.py). Facts are pooled from more than one
    file (shared + one personality's own), so this takes the full path list
    rather than a single path and just no-ops on files with no match.
    Acquires _memory_lock itself — callers must NOT already hold it."""
    if not fact_ids:
        return
    now = _now_iso()
    with _memory_lock:
        for path in paths:
            facts = _load_fact_file(path, default_category="personal")
            changed = False
            for f in facts:
                if f.get("id") in fact_ids:
                    f["last_used"] = now
                    f["use_count"] = f.get("use_count", 0) + 1
                    changed = True
            if changed:
                _save_fact_file(path, facts)


def _mark_fact_outdated(path: str, old_text: str, reason: str | None = None) -> bool:
    """Mark the stored fact most similar to *old_text* as outdated instead
    of deleting it — 'how a person remembers': the old belief becomes
    history rather than being silently erased, but stops surfacing in
    conversation (see _load_shared_facts / _load_personality_facts) until
    the weekly consolidation pass (_consolidate_memory) eventually prunes
    it. Threshold is looser than dedup's (0.5 vs 0.8) since the LLM is only
    asked to reproduce the old fact's text closely, not verbatim-exact.
    Caller must hold _memory_lock. Returns True if a match was found.

    `reason` (optional — the new fact's text, when the caller has it) is
    stored as 'outdated_reason', the seed of a belief-revision trail: not
    just THAT the fact changed but what replaced it and why."""
    facts = _load_fact_file(path, default_category="personal")
    best, best_score = None, 0.0
    for f in facts:
        score = _fact_similarity(old_text, f["fact"])
        if score > best_score:
            best_score, best = score, f
    if best is None or best_score < 0.5:
        return False
    best["outdated"]        = True
    best["outdated_at"]     = _now_iso()
    best["outdated_reason"] = reason
    _save_fact_file(path, facts)
    return True
