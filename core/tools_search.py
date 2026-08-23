"""Calculator (safe local expression evaluation) and web search
(Serper.dev primary, DuckDuckGo Instant Answer API fallback, trusted-source
ranking)."""
import json
import logging
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()
    _SSL_CTX.check_hostname = False
    _SSL_CTX.verify_mode    = ssl.CERT_NONE

logger = logging.getLogger(__name__)

_MATH_WORD_SUBS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\b(más|mas|plus)\b',                                re.I), '+'),
    (re.compile(r'\b(menos|minus)\b',                                 re.I), '-'),
    (re.compile(r'\b(por|times|multiplicado\s+por)\b',                re.I), '*'),
    (re.compile(r'\b(entre|dividido\s+(?:entre|por)|divided\s+by)\b', re.I), '/'),
    (re.compile(r'\b(elevado\s+a|raised\s+to)\b',                     re.I), '**'),
    (re.compile(r'\bal\s+cuadrado\b',                                 re.I), '**2'),
    (re.compile(r'\bal\s+cubo\b',                                     re.I), '**3'),
]

# Whitelist: digits, whitespace, arithmetic operators, parens, decimal point, modulo
_SAFE_EXPR_RE = re.compile(r'^[\d\s\+\-\*\/\(\)\.\%]+$')


def evaluate_math(query: str) -> str | None:
    """
    Detect a math expression inside the user's query, evaluate it locally,
    and return the result as a string. Returns None if no evaluable expression
    is found or if evaluation fails. Never raises.

    Supports Spanish words: más, menos, por, entre, elevado a, al cuadrado, al cubo.
    Only evaluates expressions that pass a strict character whitelist — no code injection.
    """
    # Replace Spanish math words with Python operators
    normalized = query
    for pattern, replacement in _MATH_WORD_SUBS:
        normalized = pattern.sub(replacement, normalized)

    # Extract candidate: starts and ends with a digit or closing paren
    match = re.search(r'\d[\d\s\+\-\*\/\(\)\.\%\*]+[\d\)]', normalized)
    if not match:
        return None

    expr = match.group().strip()

    # Require at least one binary operator between two digits (including **)
    if not re.search(r'\d\s*(?:\*\*|[\+\-\*\/\%])\s*\d', expr):
        return None

    # Strip whitespace and validate against the safe character whitelist
    safe_expr = re.sub(r'\s+', '', expr)
    if not _SAFE_EXPR_RE.match(safe_expr):
        return None

    try:
        # eval is safe here: safe_expr passed the whitelist and builtins are removed
        result = eval(safe_expr, {"__builtins__": {}}, {})  # noqa: S307
        if isinstance(result, int):
            return str(result)
        if isinstance(result, float):
            # Whole-number float → drop the decimal part
            if result.is_integer():
                return str(int(result))
            # Keep up to 10 significant digits, strip trailing zeros
            return f"{result:.10g}"
        return str(result)
    except Exception:
        return None

SEARCH_TIMEOUT   = 5     # seconds — separate from FETCH_TIMEOUT, search APIs are slower
SEARCH_CACHE_TTL = 600   # 10 minutes — same query in this window skips the API entirely

SERPER_URL     = "https://google.serper.dev/search"
DUCKDUCKGO_URL = "https://api.duckduckgo.com/"

# Domains treated as authoritative — ranked first and tagged [FUENTE FIABLE]
# instead of [FUENTE] when injected into LIRA's prompt (see format_search_results).
TRUSTED_SOURCES: list[str] = [
    # News / general reference
    "bbc.com", "reuters.com", "elpais.com", "nationalgeographic.com", "rae.es",
    # UN agencies and other international/intergovernmental bodies
    "un.org", "unicef.org", "undp.org", "unfao.org", "who.int",
    "europa.eu", "worldbank.org", "imf.org", "oecd.org", "iaea.org",
    # Space agencies
    "nasa.gov", "esa.int", "spacex.com",
    # Science / medical journals and research
    "nature.com", "sciencedirect.com", "science.org", "newscientist.com",
    "scientificamerican.com", "thelancet.com", "nejm.org", "arxiv.org",
    "pubmed.ncbi.nlm.nih.gov",
    # US health agencies
    "nih.gov", "cdc.gov",
]

_search_cache: dict[str, dict] = {}
_search_cache_lock = threading.Lock()

def _is_trusted(url: str) -> bool:
    """True if url's domain is in TRUSTED_SOURCES (matches on netloc, so
    subdomains like 'pubmed.ncbi.nlm.nih.gov' still count)."""
    try:
        netloc = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        netloc = (url or "").lower()
    return any(domain in netloc for domain in TRUSTED_SOURCES)


def _serper_search(query: str) -> list[dict] | None:
    """Query Serper.dev. Returns up to 5 {title, snippet, url, source} dicts,
    or None if the API key is missing or the request fails for any reason."""
    api_key = os.getenv("SERPER_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        payload = json.dumps({"q": query}).encode("utf-8")
        req = urllib.request.Request(
            SERPER_URL,
            data=payload,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=SEARCH_TIMEOUT, context=_SSL_CTX) as resp:
            raw = json.loads(resp.read().decode())

        results = []
        for item in raw.get("organic", [])[:5]:
            results.append({
                "title":   item.get("title", ""),
                "snippet": item.get("snippet", ""),
                "url":     item.get("link", ""),
                "source":  "serper",
            })
        return results or None
    except Exception as exc:
        logger.debug("Serper search failed: %s", exc)
        return None


def _flatten_ddg_topics(topics: list) -> list[dict]:
    """DuckDuckGo nests some RelatedTopics one level deep under 'Topics' —
    flatten so every leaf entry (the ones with actual Text/FirstURL) is at
    the top level."""
    flat = []
    for t in topics:
        if "Topics" in t:
            flat.extend(_flatten_ddg_topics(t["Topics"]))
        elif t.get("Text"):
            flat.append(t)
    return flat


def _duckduckgo_search(query: str) -> list[dict] | None:
    """Query the DuckDuckGo Instant Answer API (no key required). Returns up
    to 5 {title, snippet, url, source} dicts, or None on failure/no data.

    Not a full web-search index — mostly abstracts/disambiguation topics —
    but it's a reasonable no-key fallback when Serper is unavailable."""
    try:
        params = urllib.parse.urlencode({
            "q": query, "format": "json", "no_html": 1, "skip_disambig": 1,
        })
        url = f"{DUCKDUCKGO_URL}?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "JarvisLite/1.0"})
        with urllib.request.urlopen(req, timeout=SEARCH_TIMEOUT, context=_SSL_CTX) as resp:
            raw = json.loads(resp.read().decode())

        results = []
        if raw.get("AbstractText"):
            results.append({
                "title":   raw.get("Heading") or raw.get("AbstractSource") or query,
                "snippet": raw["AbstractText"],
                "url":     raw.get("AbstractURL", ""),
                "source":  "duckduckgo",
            })

        for topic in _flatten_ddg_topics(raw.get("RelatedTopics", [])):
            if len(results) >= 5:
                break
            text = topic.get("Text", "")
            title, _, snippet = text.partition(" - ")
            results.append({
                "title":   title or text,
                "snippet": snippet or text,
                "url":     topic.get("FirstURL", ""),
                "source":  "duckduckgo",
            })

        return results or None
    except Exception as exc:
        logger.debug("DuckDuckGo search failed: %s", exc)
        return None


def search_web(query: str) -> list[dict]:
    """
    Search the web for `query`. Tries Serper.dev first (needs SERPER_API_KEY
    in .env); falls back to the DuckDuckGo Instant Answer API if Serper fails
    or the key is missing. Returns the top 3-5 results as
    {title, snippet, url, source} dicts, with TRUSTED_SOURCES entries ranked
    first. Cached per-query for SEARCH_CACHE_TTL seconds. Never raises —
    returns [] if both engines fail or the query is empty.
    """
    key = (query or "").strip().lower()
    if not key:
        return []

    now_ts = time.monotonic()
    with _search_cache_lock:
        cached = _search_cache.get(key)
        if cached is not None and (now_ts - cached["timestamp"]) < SEARCH_CACHE_TTL:
            return cached["data"]

    results = _serper_search(query) or _duckduckgo_search(query) or []
    # Stable sort: trusted-source results move to the top, original relative
    # order preserved within each group.
    ranked = sorted(results, key=lambda r: not _is_trusted(r.get("url", "")))[:5]

    with _search_cache_lock:
        _search_cache[key] = {"data": ranked, "timestamp": now_ts}

    return ranked


def format_search_results(results: list[dict]) -> str:
    """Format search_web() results for injection into the LLM prompt — one
    line per result. Trusted sources (see TRUSTED_SOURCES) are tagged
    '[FUENTE FIABLE]' instead of '[FUENTE]' so the model can tell a reliable
    source from a general one at a glance and cite it naturally."""
    lines = []
    for r in results:
        tag     = "[FUENTE FIABLE]" if _is_trusted(r.get("url", "")) else "[FUENTE]"
        title   = (r.get("title") or "").strip()
        snippet = (r.get("snippet") or "").strip()
        lines.append(f"{tag} {title} — {snippet}")
    return "\n".join(lines)
