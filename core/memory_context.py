# Armor knowledge and concept-knowledge summaries injected into personality
# system prompts. Split out of core/memory.py (pure refactor, no behavior
# change).
import json
import logging
import re
import threading

from core.memory_store import _keywords

logger = logging.getLogger(__name__)

# Roman-numeral models ("Modelo IX") vs. how Joan actually says it ("modelo
# 9") — without this, "modelo" alone (shared by every single model's name)
# was the only keyword _select_relevant_armor had to work with, so a query
# naming one specific model tied across all of them instead of picking the
# right one. Covers this project's current 0-X range; extend if a model
# past X ever gets added.
_ROMAN_TO_ARABIC = {
    "I": "1", "II": "2", "III": "3", "IV": "4", "V": "5",
    "VI": "6", "VII": "7", "VIII": "8", "IX": "9", "X": "10",
}


def _armor_numeral_synonyms(name: str) -> set[str]:
    return {_ROMAN_TO_ARABIC[t] for t in re.findall(r"\w+", name.upper()) if t in _ROMAN_TO_ARABIC}


# ---------------------------------------------------------------------------
# Armor knowledge — loaded once at startup, injected into LIRA's system prompt
# ---------------------------------------------------------------------------

ARMOR_KNOWLEDGE_PATH = "data/armor_knowledge.json"

def _build_armor_summary() -> str:
    """Load armor_knowledge.json and build a compact multi-line summary for LIRA.
    Returns empty string if file is missing or malformed."""
    try:
        with open(ARMOR_KNOWLEDGE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        lines = []
        for m in data.get("models", []):
            name   = m.get("name", "?")
            hours  = m.get("hours", "?")
            status = m.get("status", "?")
            inno   = m.get("innovaciones", "")[:120]
            specs  = m.get("specs", "")[:80]
            lines.append(f"- {name} ({hours}, {status}): {inno} | Specs: {specs}")
        return "\n".join(lines)
    except Exception as exc:
        logger.debug("Could not load armor knowledge: %s", exc)
        return ""

_ARMOR_SUMMARY: str = _build_armor_summary()

# Relevance-filtered retrieval (2026-08-14) — _ARMOR_SUMMARY above is a full
# dump of every model, still used verbatim by core.discord_bridge, but
# unconditionally injecting it into LIRA's own system prompt meant every
# single turn — including ones with nothing to do with armor — carried the
# full spec sheet for every model. Same "score against what the user just
# said, surface only the relevant handful" approach core.memory_select
# already uses for Layer 1/2 facts (_select_relevant_facts), applied here
# instead of a second bespoke mechanism. Loaded once at startup, same as
# _ARMOR_SUMMARY — no live-reload endpoint exists for armor_knowledge.json
# (unlike concepts below), so a new model requires a restart either way.
_ARMOR_MODELS: list[dict] = []


def _load_armor_models() -> list[dict]:
    try:
        with open(ARMOR_KNOWLEDGE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("models", []) if isinstance(data, dict) else []
    except Exception as exc:
        logger.debug("Could not load armor models: %s", exc)
        return []


_ARMOR_MODELS = _load_armor_models()


def _get_armor_models() -> list[dict]:
    """Accessor, not a bare re-exported list — core.memory's aggregator
    does `from core.memory_context import X`, which copies the reference
    at import time; a function is the only way a cross-module caller sees
    this module's current value rather than a stale one (armor has no
    reload path today, but concepts' _get_concepts below does, and this
    keeps both call sites the same shape)."""
    return _ARMOR_MODELS


def _format_armor_line(m: dict) -> str:
    name   = m.get("name", "?")
    hours  = m.get("hours", "?")
    status = m.get("status", "?")
    inno   = (m.get("innovaciones") or "")[:120]
    specs  = (m.get("specs") or "")[:80]
    return f"- {name} ({hours}, {status}): {inno} | Specs: {specs}"


def _select_relevant_armor(user_message: str, models: list[dict], max_items: int = 3) -> list[dict]:
    """Same keyword-overlap relevance scoring as
    core.memory_select._select_relevant_facts, scored against each model's
    name/innovaciones/specs instead of a fact's text. Each model's keyword
    set also carries its Arabic-numeral synonym (see
    _armor_numeral_synonyms) so 'modelo 9' scores 'Modelo IX' above every
    other model instead of tying with them on 'modelo' alone.

    _keywords() alone isn't enough on the query side either — it drops
    tokens of length <=2 as stopword noise (correct for words like 'el',
    'un', wrong here since it silently ate the '9' out of 'modelo 9' too),
    so bare digit tokens are pulled back in separately before scoring."""
    msg_kw = _keywords(user_message) | set(re.findall(r"\b\d+\b", user_message))
    if not msg_kw:
        return []
    scored = []
    for m in models:
        name = m.get("name", "")
        text = f"{name} {m.get('innovaciones','')} {m.get('specs','')}"
        kw = _keywords(text) | _armor_numeral_synonyms(name)
        overlap = len(msg_kw & kw)
        if overlap > 0:
            scored.append((overlap, m))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [m for _, m in scored[:max_items]]


_MIN_NAME_LEN_FOR_REFERENCE = 4   # below this a name is too generic to substring-match safely (false positives)


def _expand_armor_with_references(selected: list[dict], all_models: list[dict], max_extra: int = 2) -> list[dict]:
    """Armor's own associative connections — Phase 2 of the mind-map work
    (2026-08-14): unlike memory facts, armor models don't need a separate
    generated connections file, because they already carry explicit
    references to each other in their own data. 'evolucion' literally
    names what a model leads to ('Lleva al Modelo X con...'), and
    descripcion/innovaciones/limitaciones sometimes name a related model
    directly — asking about Modelo IX should also surface Modelo X because
    IX's own record says so, the same way a person remembers 'oh, that
    leads into the next one' without being asked. Detected fresh from
    current armor_knowledge.json on every call, not a stored/generated
    graph — always in sync, no sleep-cycle step required."""
    if not selected:
        return []
    by_name = {m["name"]: m for m in all_models if m.get("name")}
    selected_names = {m.get("name") for m in selected}
    extra: list[dict] = []
    for m in selected:
        blob = " ".join(str(m.get(k, "")) for k in
                         ("descripcion", "innovaciones", "specs", "evolucion", "limitaciones")).lower()
        for name, other in by_name.items():
            if name in selected_names or len(name) < _MIN_NAME_LEN_FOR_REFERENCE:
                continue
            if re.search(r"\b" + re.escape(name.lower()) + r"\b", blob):
                extra.append(other)
                selected_names.add(name)
                if len(extra) >= max_extra:
                    return extra
    return extra


def _format_relevant_armor_block(models: list[dict]) -> str:
    if not models:
        return ""
    return "\n".join(_format_armor_line(m) for m in models)


# ---------------------------------------------------------------------------
# Concept knowledge — HUD "Conceptuales" tab. data/concepts.json is now the
# backend-owned source of truth (see core.server's GET/POST /api/concepts;
# ui/index.html only keeps localStorage as an offline fallback, it no longer
# owns this data). Loaded once at startup, then refreshed in place by
# reload_concepts() whenever POST /api/concepts saves a create/edit/delete,
# so LIRA's prompt stays current without restarting jarvis.py.
# ---------------------------------------------------------------------------

CONCEPTS_PATH  = "data/concepts.json"
_concepts_lock = threading.Lock()

def _build_concepts_summary() -> str:
    """Load data/concepts.json and build a compact multi-line summary for LIRA.
    Returns empty string if the file is missing, empty or malformed."""
    try:
        with open(CONCEPTS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        lines = []
        for c in data.get("concepts", []):
            name   = c.get("name", "?")
            status = c.get("status", "?")
            desc   = (c.get("desc") or "")[:120]
            lines.append(f"- {name} ({status}): {desc}")
        return "\n".join(lines)
    except Exception as exc:
        logger.debug("Could not load concepts: %s", exc)
        return ""

_CONCEPTS_SUMMARY: str = _build_concepts_summary()

# Relevance-filtered retrieval — same rationale as _ARMOR_MODELS above,
# kept in sync with data/concepts.json by reload_concepts() below (unlike
# armor, concepts already has a live-reload path via POST /api/concepts).
_CONCEPTS: list[dict] = []


def _load_concepts_list() -> list[dict]:
    try:
        with open(CONCEPTS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("concepts", []) if isinstance(data, dict) else []
    except Exception as exc:
        logger.debug("Could not load concepts list: %s", exc)
        return []


_CONCEPTS = _load_concepts_list()


def _get_concepts() -> list[dict]:
    """Accessor, not a bare re-exported list — see _get_armor_models'
    docstring. This one matters more than that one: reload_concepts()
    below reassigns _CONCEPTS on every POST /api/concepts, and
    core.memory's aggregator would otherwise keep pointing at the list
    object from whenever memory.py was first imported."""
    return _CONCEPTS


def _format_concept_line(c: dict) -> str:
    name   = c.get("name", "?")
    status = c.get("status", "?")
    desc   = (c.get("desc") or "")[:120]
    return f"- {name} ({status}): {desc}"


def _select_relevant_concepts(user_message: str, concepts: list[dict], max_items: int = 3) -> list[dict]:
    """Same keyword-overlap relevance scoring as _select_relevant_armor."""
    msg_kw = _keywords(user_message)
    if not msg_kw:
        return []
    scored = []
    for c in concepts:
        text = f"{c.get('name','')} {c.get('desc','')}"
        overlap = len(msg_kw & _keywords(text))
        if overlap > 0:
            scored.append((overlap, c))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [c for _, c in scored[:max_items]]


def _expand_concepts_with_references(selected: list[dict], all_concepts: list[dict], max_extra: int = 2) -> list[dict]:
    """Same approach as _expand_armor_with_references, applied to
    concepts: a concept's own 'desc' sometimes names another saved
    concept directly, so that one rides along too — detected fresh from
    current data.json, no generated graph needed."""
    if not selected:
        return []
    by_name = {c["name"]: c for c in all_concepts if c.get("name")}
    selected_names = {c.get("name") for c in selected}
    extra: list[dict] = []
    for c in selected:
        blob = str(c.get("desc", "")).lower()
        for name, other in by_name.items():
            if name in selected_names or len(name) < _MIN_NAME_LEN_FOR_REFERENCE:
                continue
            if re.search(r"\b" + re.escape(name.lower()) + r"\b", blob):
                extra.append(other)
                selected_names.add(name)
                if len(extra) >= max_extra:
                    return extra
    return extra


def _format_relevant_concepts_block(concepts: list[dict]) -> str:
    if not concepts:
        return ""
    return "\n".join(_format_concept_line(c) for c in concepts)


def reload_concepts() -> None:
    """Re-read data/concepts.json and refresh the in-memory summary + the
    structured list used by relevance retrieval.

    Called by core.server's POST /api/concepts handler after it writes a
    create/edit/delete to disk, so the change is reflected in LIRA's system
    prompt immediately — no jarvis.py restart needed.
    """
    global _CONCEPTS_SUMMARY, _CONCEPTS
    with _concepts_lock:
        _CONCEPTS_SUMMARY = _build_concepts_summary()
        _CONCEPTS = _load_concepts_list()
