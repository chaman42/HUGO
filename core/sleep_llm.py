"""Sleep System — LLM call layer: Ollama (primary, local, free) with a
Groq fallback (own spend cap during continuous mode). Every sleep phase
calls _groq_call(), never Ollama/Groq directly."""
import json
import logging
import os
import re
import urllib.request

from groq import Groq

from core.sleep_state import MODEL, GROQ_FALLBACK_BUDGET

logger = logging.getLogger(__name__)

# Continuous-mode-only state (see core/sleep.py's run_continuous_sleep) — set
# directly on this module by that function (`sleep_llm._continuous_mode_active
# = True`) since it lives in a different module now; _groq_call() below still
# reads/mutates it as plain module globals exactly as before.
def _groq_call_impl(system: str, user: str, max_tokens: int, api_key: str | None = None) -> tuple[str | None, int]:
    """Raw Groq SDK call — returns (text, tokens_used). text is None on any
    failure (missing key, network error, empty response) — tokens_used is 0
    in that case. Never raises. Only reached via _groq_call()'s Ollama-first
    dispatch below (either as the deliberate fallback, or directly for the
    old one-shot run_sleep_session() path, which never touches Ollama at
    all — see that function's own docstring)."""
    if max_tokens <= 0:
        return None, 0
    try:
        key = api_key or os.getenv("GROQ_API_KEY")
        if not key:
            raise RuntimeError("GROQ_API_KEY is not set in .env")
        client = Groq(api_key=key)
        response = client.chat.completions.create(
            model=MODEL,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        text = (response.choices[0].message.content or "").strip()
        usage = getattr(response, "usage", None)
        tokens = getattr(usage, "total_tokens", None) or max_tokens
        if not text:
            return None, tokens
        return text, tokens
    except Exception as e:
        logger.debug("Sleep Groq call failed: %s", e)
        return None, 0

OLLAMA_HOST         = "http://localhost:11434"
OLLAMA_MODEL        = "llama3.2:3b"
OLLAMA_GENERATE_URL = f"{OLLAMA_HOST}/api/generate"
OLLAMA_TAGS_URL     = f"{OLLAMA_HOST}/api/tags"

def _ollama_available() -> bool:
    """Cheap reachability probe — a local request, so a short timeout is
    enough to tell 'not running' from 'slow'. Never raises."""
    try:
        req = urllib.request.Request(OLLAMA_TAGS_URL, method="GET")
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False

def _ollama_generate(system: str, user: str, max_tokens: int) -> str | None:
    """One /api/generate call (non-streaming) — returns the response text,
    or None on any failure (server down, timeout, empty response). Never
    raises. max_tokens caps generation length (num_predict) — there's no
    token COST to cap, unlike Groq, but a runaway completion would still
    slow a phase down for nothing.

    Generous timeout (240s, vs Groq's implicit ~30s default) — a 3B model on
    CPU-only hardware can genuinely take a while, and unlike a Groq call
    there's no per-second cost to waiting it out; continuous sleep mode has
    no wall-clock budget at all (see run_continuous_sleep). Timing out into
    the Groq fallback is worse than just waiting, since a Groq fallback call
    still bills real prompt+completion tokens against GROQ_FALLBACK_BUDGET —
    see _phase_memory_maintenance's own sampling cap (_MAINT_REVIEW_SAMPLE_SIZE)
    for the other half of keeping this fast: bounding prompt SIZE, not just
    tolerating slow generation."""
    try:
        payload = json.dumps({
            "model":   OLLAMA_MODEL,
            "prompt":  user,
            "system":  system,
            "stream":  False,
            "options": {"num_predict": max_tokens},
        }).encode("utf-8")
        req = urllib.request.Request(
            OLLAMA_GENERATE_URL, data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=240) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = str(data.get("response", "")).strip()
        return text or None
    except Exception as e:
        logger.debug("Ollama call failed: %s", e)
        return None

# Continuous-mode-only state (see run_continuous_sleep): while active, Groq
# is used purely as a fallback with its OWN spend cap (GROQ_FALLBACK_BUDGET,
# same value as the old MANUAL_SESSION_BUDGET) — once that's used up, calls
# just stop going to Groq (phases fall back to their existing "no LLM
# response" no-op path) rather than the cycle itself ever stopping; Ollama
# has no equivalent cap since it has no token cost. Left at defaults (False/0)
# outside a continuous run, where _groq_call() behaves exactly as before
# this feature (old run_sleep_session() path — unlimited-by-this-cap Groq,
# gated instead by the pre-existing auto_budget/manual_budget machinery).
_continuous_mode_active = False
_continuous_groq_used   = 0

def _groq_call(system: str, user: str, max_tokens: int, api_key: str | None = None) -> tuple[str | None, int]:
    """Every phase's actual LLM call site (name kept as '_groq_call' rather
    than renamed, so no phase function needed to change) — tries Ollama
    first (free, local); falls back to real Groq only if Ollama is
    unreachable or returns nothing. Returns (text, tokens_used) exactly like
    the old Groq-only version — tokens_used is always 0 for an Ollama
    response (no cost to track), and only reflects real spend when the
    Groq fallback path was actually used."""
    if max_tokens <= 0:
        return None, 0

    if _ollama_available():
        text = _ollama_generate(system, user, max_tokens)
        if text:
            return text, 0
        logger.debug("Ollama reachable but returned nothing — falling back to Groq")

    if _continuous_mode_active:
        global _continuous_groq_used
        remaining = GROQ_FALLBACK_BUDGET - _continuous_groq_used
        if remaining <= 0:
            return None, 0
        text, tokens = _groq_call_impl(system, user, min(max_tokens, remaining), api_key)
        _continuous_groq_used += tokens
        return text, tokens

    return _groq_call_impl(system, user, max_tokens, api_key)

def _parse_json_list(raw: str, key: str, min_confidence: float, cap: int) -> list[dict]:
    """Parses a '[{<key>: "...", "confidence": 0-1}, ...]' response —
    same shape every phase below asks for. Returns [] on any parse
    failure, filtered to confidence > min_confidence, capped at `cap`."""
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        return []
    try:
        parsed = json.loads(match.group())
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []

    out = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        text = str(item.get(key, "")).strip()
        try:
            confidence = float(item.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        if text and confidence > min_confidence:
            out.append({key: text, "confidence": confidence})
    return out[:cap]
