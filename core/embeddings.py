# ═══════════════════════════════════════════════════════════════════════════
# EMBEDDINGS — local semantic index over HUGO's memory (facts) and Estudio
# documents (summaries, schemas, investigations, explorations), backed by a
# local Chroma store (data/chroma/, no server) and a local
# sentence-transformers model. No cloud calls, no per-query cost.
#
# Why this exists: core/memory_select.py's _select_relevant_facts and
# core/reflective.py's connection-building both rely on keyword overlap
# (_keywords) or a small local LLM's judgment — both miss genuinely related
# content phrased with different words ('practica natación' / 'le gusta
# nadar' share zero keywords and a 3B local model judged them unrelated
# twice in testing, 2026-08-20). Embedding distance catches this without
# needing a model to reason about it at all.
#
# One shared collection ('hugo_memory'), documents tagged by a 'type'
# metadata field (fact/episode/investigation/summary/schema/exploration)
# rather than one collection per type — so a single query
# can search across everything at once (optionally filtered to one type via
# query()'s doc_type param), which is the actual point: one semantic index
# for "what does HUGO know/have that relates to X", not eight separate ones.
#
# Kept dependency-light beyond chromadb/sentence-transformers themselves —
# no core.commands/core.voice — so this can be imported from a standalone
# reindex script the same way core/reflective.py can run outside jarvis.py.
# ═══════════════════════════════════════════════════════════════════════════
import json
import logging
import os
import threading

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__)).rsplit(os.sep + "core", 1)[0]


def _p(rel: str) -> str:
    return os.path.join(_REPO_ROOT, rel)


CHROMA_PATH      = _p("data/chroma")
COLLECTION_NAME  = "hugo_memory"

# Compact multilingual model (~470MB, CPU-friendly) — matches this project's
# Spanish-first content; a general English-only model would score Spanish
# paraphrases much worse.
EMBED_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"

_model = None
_client = None
_collection = None


def _get_model():
    """Lazy singleton — loading a sentence-transformers model costs real
    time/memory, so this only happens once per process, on first actual use
    (indexing or querying), not at import time."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(EMBED_MODEL_NAME)
    return _model


def _get_collection():
    global _client, _collection
    if _collection is None:
        import chromadb
        os.makedirs(CHROMA_PATH, exist_ok=True)
        _client = chromadb.PersistentClient(path=CHROMA_PATH)
        _collection = _client.get_or_create_collection(COLLECTION_NAME)
    return _collection


def available() -> bool:
    """Whether the embeddings stack can actually be used right now — chromadb/
    sentence-transformers installed, model loadable. Never raises; callers
    (memory_select.py etc.) should treat False as 'skip semantic expansion,
    keyword matching still works on its own' rather than an error."""
    try:
        _get_model()
        _get_collection()
        return True
    except Exception as e:
        logger.debug("[EMBEDDINGS] not available: %s", e)
        return False


def _embed(texts: list[str]) -> list[list[float]]:
    model = _get_model()
    return model.encode(texts, normalize_embeddings=True, show_progress_bar=False).tolist()


def upsert(doc_type: str, doc_id: str, text: str, metadata: dict | None = None) -> bool:
    """Adds or updates one document. id is namespaced '{type}:{doc_id}' so
    the same raw id (e.g. a UUID reused across collections, unlikely but
    not impossible) can never collide across types. No-ops (returns False)
    on empty text or any failure — never raises."""
    text = (text or "").strip()
    if not text:
        return False
    try:
        collection = _get_collection()
        meta = {"type": doc_type, **(metadata or {})}
        embedding = _embed([text])[0]
        collection.upsert(
            ids=[f"{doc_type}:{doc_id}"], embeddings=[embedding],
            documents=[text], metadatas=[meta],
        )
        return True
    except Exception as e:
        logger.debug("[EMBEDDINGS] upsert failed for %s:%s — %s", doc_type, doc_id, e)
        return False


def query(query_text: str, n_results: int = 5, doc_type: str | None = None) -> list[dict]:
    """Top-n_results documents by embedding distance, optionally restricted
    to one doc_type. Returns [] on any failure (store unavailable, empty
    index, etc.) rather than raising — callers should treat this as 'no
    semantic matches found', same as an empty keyword-match result."""
    query_text = (query_text or "").strip()
    if not query_text:
        return []
    try:
        collection = _get_collection()
        count = collection.count()
        if count == 0:
            return []
        embedding = _embed([query_text])[0]
        where = {"type": doc_type} if doc_type else None
        result = collection.query(
            query_embeddings=[embedding], n_results=min(n_results, count), where=where,
        )
        out = []
        ids       = (result.get("ids") or [[]])[0]
        docs      = (result.get("documents") or [[]])[0]
        metas     = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        for id_, doc, meta, dist in zip(ids, docs, metas, distances):
            out.append({"id": id_, "text": doc, "metadata": meta, "distance": dist})
        return out
    except Exception as e:
        logger.debug("[EMBEDDINGS] query failed: %s", e)
        return []


def delete(doc_type: str, doc_id: str) -> None:
    try:
        _get_collection().delete(ids=[f"{doc_type}:{doc_id}"])
    except Exception as e:
        logger.debug("[EMBEDDINGS] delete failed for %s:%s — %s", doc_type, doc_id, e)


# ---------------------------------------------------------------------------
# Full reindex — reads every document collection from disk and upserts it.
# Safe to run repeatedly (upsert, not insert) — used both for the initial
# build and to catch anything an incremental upsert call site missed.
# ---------------------------------------------------------------------------

def _load_json(path: str, default):
    try:
        with open(_p(path), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def reindex_all() -> dict:
    """Rebuilds the full index from data/*.json. Returns a per-type count
    dict for visibility (see scripts/reindex_embeddings.py). Never raises —
    each collection is independently try/excepted so one malformed file
    can't abort the whole reindex."""
    counts: dict[str, int] = {}

    def _index_collection(doc_type: str, items, id_fn, text_fn, meta_fn=None):
        n = 0
        for item in items:
            try:
                doc_id = id_fn(item)
                text   = text_fn(item)
                meta   = meta_fn(item) if meta_fn else {}
                if doc_id and upsert(doc_type, doc_id, text, meta):
                    n += 1
            except Exception:
                continue
        counts[doc_type] = n

    facts = _load_json("data/memory_shared.json", [])
    if isinstance(facts, list):
        _index_collection(
            "fact", [f for f in facts if isinstance(f, dict) and not f.get("outdated")],
            id_fn=lambda f: f.get("id") or f.get("fact", "")[:40],
            text_fn=lambda f: f.get("fact", ""),
            meta_fn=lambda f: {"category": f.get("category", "")},
        )

    episodes = _load_json("data/episodes.json", [])
    if isinstance(episodes, list):
        _index_collection(
            "episode", [e for e in episodes if isinstance(e, dict)],
            id_fn=lambda e: f"{e.get('date', '')}:{e.get('topic', '')[:30]}",
            text_fn=lambda e: f"{e.get('topic', '')}: {e.get('summary', '')}",
            meta_fn=lambda e: {"date": e.get("date", ""), "importance": e.get("importance", 0)},
        )

    investigations = _load_json("data/investigations.json", [])
    if isinstance(investigations, list):
        _index_collection(
            "investigation", [i for i in investigations if isinstance(i, dict)],
            id_fn=lambda i: i.get("id", ""),
            text_fn=lambda i: " — ".join(filter(None, [
                i.get("title"), i.get("question"), i.get("summary"), i.get("conclusions"),
            ])),
            meta_fn=lambda i: {"status": i.get("status", ""), "title": i.get("title", "")},
        )

    summaries = _load_json("data/summaries.json", [])
    if isinstance(summaries, list):
        _index_collection(
            "summary", [s for s in summaries if isinstance(s, dict)],
            id_fn=lambda s: s.get("id", ""),
            text_fn=lambda s: f"{s.get('title', '')}: {s.get('excerpt') or s.get('content', '')}",
            meta_fn=lambda s: {"title": s.get("title", ""), "date": s.get("date", "")},
        )

    schemas = _load_json("data/schemas.json", [])
    if isinstance(schemas, list):
        _index_collection(
            "schema", [s for s in schemas if isinstance(s, dict)],
            id_fn=lambda s: s.get("id", ""),
            text_fn=lambda s: f"{s.get('title', '')} ({s.get('topic', '')})",
            meta_fn=lambda s: {"title": s.get("title", ""), "date": s.get("date", "")},
        )

    explorations = _load_json("data/explorations.json", [])
    if isinstance(explorations, list):
        _index_collection(
            "exploration", [e for e in explorations if isinstance(e, dict)],
            id_fn=lambda e: e.get("url", "")[:60] or e.get("title", "")[:40],
            text_fn=lambda e: f"{e.get('title', '')}: {e.get('excerpt') or e.get('summary', '')}",
            meta_fn=lambda e: {"title": e.get("title", ""), "date": e.get("date", "")},
        )

    logger.info("[EMBEDDINGS] reindex_all: %s", counts)
    return counts


# ---------------------------------------------------------------------------
# Pre-warm — loading the sentence-transformers model the first time costs
# ~25s (confirmed 2026-08-20), which would otherwise land on whichever
# conversation turn happens to be the first one to call query() after
# process start (_get_model() is a lazy singleton — see its own docstring).
# For a live voice assistant that means a silent ~25s hang on some
# unlucky first reply. Same pattern core/voice.py uses for Kokoro
# (_prewarm_kokoro + a module-level background thread started at import
# time) — fires in the background as soon as this module is imported, so
# by the time a real query lands the model is normally already loaded
# (jarvis.py's own boot sequence already takes ~16s, most of this cost is
# absorbed in parallel with that). Fire-and-forget, no readiness flag to
# wait on anywhere — unlike TTS this feature is fully optional/degradable
# (see available()'s own docstring), so a query landing mid-pre-warm just
# blocks briefly on the same lazy singleton rather than needing a
# dedicated ready-check like Kokoro's.
# ---------------------------------------------------------------------------

def _prewarm() -> None:
    try:
        # available() alone only constructs the model/collection objects —
        # confirmed live (2026-08-20) that the real cost is in the first
        # actual .encode() call (PyTorch's one-time kernel/graph warmup),
        # not object construction: a pre-warm that only called available()
        # still left the first real query taking 4-8s instead of the ~0.2s
        # a genuinely warm model gives. Running one real embed call here,
        # same reasoning as _prewarm_kokoro's own comment on forcing every
        # kernel to run once ahead of real-utterance time.
        if available():
            _embed(["precalentamiento"])
            logger.debug("[EMBEDDINGS] pre-warm complete.")
    except Exception as e:
        logger.debug("[EMBEDDINGS] pre-warm failed: %s", e)


threading.Thread(target=_prewarm, daemon=True, name="embeddings-prewarm").start()
