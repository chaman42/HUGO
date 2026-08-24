# ═══════════════════════════════════════════════════════════════════════════
# GROQ CONFIG — model chain configuration and the lazy Groq client singleton.
# Split out of core/groq_client.py so the completion-call logic there isn't
# bundled with configuration constants (pure refactor, no behavior change).
# ═══════════════════════════════════════════════════════════════════════════
import os

from groq import Groq
from dotenv import load_dotenv

from core import active_person
from core import api_key_store

load_dotenv()
api_key_store.apply_saved_to_environ()   # a key saved in Ajustes on a PREVIOUS run must already be live before GROQ_API_KEYS is computed below

# Main conversational model chain — GROQ_MODEL_CHAIN, an ordered,
# best-to-lightest list of every active Groq text/chat model (confirmed via
# GET /openai/v1/models on 2026-07-17), configurable via .env as a
# comma-separated string. _groq_complete() tries each in order, one
# streamed attempt per model (GROQ_FIRST_TOKEN_TIMEOUT each), falling
# through immediately on a rate limit, any other error, or an empty
# response — see _groq_complete()'s own docstring. Goal: static offline
# responses (_static_fallback) become the true last resort, not something
# hit after one model's transient rate limit.
#
# NOTE on reintroducing openai/gpt-oss-*/qwen3-*: an earlier survey in this
# codebase found gpt-oss-20b/120b spontaneously emitting tool calls
# (Groq rejects with "Tool choice is none, but model called a tool" — the
# root cause of a prior empty-response bug) and qwen3-32b/qwen3.6-27b
# showing multi-second-to-25-second latency spikes on ~half of requests,
# and settled on llama-3.3-70b-versatile alone as primary+fallback. That
# finding is still valid for a single-model setup — it's why this chain
# puts llama-3.3-70b-versatile below them rather than removing it. What's
# different now: each tier gets only GROQ_FIRST_TOKEN_TIMEOUT (5s, not the
# 25s spikes observed) before falling through, and a tool-call rejection is
# just another caught exception that advances the chain — so those models'
# known failure modes now cost a few seconds of latency for this one
# request rather than a broken/empty reply. Revisit this chain's order if
# that assumption stops holding up in practice.
#
# Previously: deepseek-r1-distill-llama-70b, decommissioned by Groq on
# 2025-10-02 — see https://console.groq.com/docs/deprecations.
_DEFAULT_GROQ_MODEL_CHAIN = ",".join([
    "openai/gpt-oss-120b",                        # best reasoning, 131k context
    # qwen/qwen3-32b REMOVED 2026-08-10: confirmed dead for this account —
    # every real call hit a hard 404 ("model does not exist or you do not
    # have access to it"), not the transient latency spike the note above
    # describes. That's permanent for this API key, so it could never
    # actually serve as a fallback — pure dead weight every single tier
    # walk had to pay for nothing. Re-add only once confirmed reachable
    # again (GET /openai/v1/models).
    "qwen/qwen3.6-27b",                            # good reasoning, 131k context
    "llama-3.3-70b-versatile",                     # reliable workhorse
    "openai/gpt-oss-20b",                          # medium reasoning
    "groq/compound",                               # Groq native, 131k context
    "groq/compound-mini",                          # Groq native, lightweight
    # meta-llama/llama-4-scout-17b-16e-instruct REMOVED 2026-08-10: confirmed
    # dead for this account, same as qwen3-32b above — every real call over
    # a full day of production logs hit a hard 404 ("model does not exist or
    # you do not have access to it"), zero successes ever (grep
    # 'groq_call model=meta-llama' logs/activity.log — 0 hits, vs. 32 failed
    # attempts). Under load it sometimes surfaced as a 5s timeout instead of
    # an instant 404 (Groq presumably queuing the request before rejecting
    # it), but never once actually answered. Re-add only once confirmed
    # reachable again (GET /openai/v1/models).
    "llama-3.1-8b-instant",                        # smallest, fastest, last resort before static
])
GROQ_MODEL_CHAIN = [
    m.strip() for m in os.getenv("GROQ_MODEL_CHAIN", _DEFAULT_GROQ_MODEL_CHAIN).split(",")
    if m.strip()
]

# Snapshot of the most recent _groq_complete() call — which model actually
# answered and how long it took. In-memory only (resets on restart); read by
# GET /api/info for the CORE app's Estado tab. Set at the end of
# _groq_complete().
_last_latency: dict = {}

# GROQ_MODEL_FALLBACK is NOT env-configurable on purpose, and separate from
# GROQ_MODEL_CHAIN above — it's the known-fast, known-non-reasoning model
# small utility calls (_groq_complete_fast: memory-fact extraction, history
# compression, search-query translation) use directly, since they don't need
# chain-of-thought and shouldn't pay a reasoning model's latency cost, and
# don't warrant iterating the full 9-model chain for a low-stakes internal
# call.
GROQ_MODEL_FALLBACK = "llama-3.3-70b-versatile"

# Bug fix: a single hardcoded model is a single point of failure. In
# production, GROQ_MODEL_FALLBACK alone hit Groq's per-model daily token cap
# (TPD, not per-minute — a ~15-25min outage each time) repeatedly, which
# silently broke every _groq_complete_fast() caller — history compression
# AND memory extraction at once — for the rest of that window (see
# logs/errors.log / logs/activity.log, 2026-07-17 23:40-23:48). _GROQ_FAST_CHAIN
# gives _groq_complete_fast() one cheap fallback tier — llama-3.1-8b-instant,
# the smallest/fastest model in GROQ_MODEL_CHAIN — so one model's rate limit
# no longer takes down both background processes at once.
_GROQ_FAST_CHAIN = [GROQ_MODEL_FALLBACK, "llama-3.1-8b-instant"]

# Memory-fact extraction (_extract_and_save_memory in core/memory_extract.py)
# runs in its own daemon thread AFTER the reply has already been sent to the
# user — nothing in the conversation path waits on it, so unlike
# _GROQ_FAST_CHAIN above it has no reason to optimize for latency. It gets
# its own reasoning-tier chain instead: holding a long list of inclusion/
# exclusion rules (never save questions, classify lifespan, detect
# contradictions...) in one prompt is exactly where a small non-reasoning
# model like llama-3.1-8b-instant gets sloppy — accepting throwaway
# mentions (e.g. a passing question about a car) while missing genuinely
# important facts. Top two tiers of GROQ_MODEL_CHAIN, plus the same
# reliable-workhorse fallback _GROQ_FAST_CHAIN ends on, so a rate limit here
# still degrades gracefully instead of failing extraction outright.
_GROQ_EXTRACT_CHAIN = ["openai/gpt-oss-120b", "qwen/qwen3.6-27b", GROQ_MODEL_FALLBACK]

# Time-to-first-token budget per model in GROQ_MODEL_CHAIN before falling
# through to the next one — see _groq_stream_complete(). 5s per tier keeps a
# full walk of the chain's worst case (every tier timing out) bounded to
# under a minute rather than unbounded, while giving each model a realistic
# shot before being abandoned.
GROQ_FIRST_TOKEN_TIMEOUT = 5.0

# Bug fix (empty-response bug): a reasoning model's internal thinking
# tokens (delta.reasoning, a separate stream field — see
# _groq_stream_complete) count against max_tokens exactly like the visible
# answer does. At a model's default reasoning effort, a sufficiently
# open-ended prompt can make it spend the ENTIRE max_tokens budget
# "thinking" and never reach the final-channel answer, so `content` comes
# back completely empty — no exception, fast ttft, just silence. Originally
# only gated for gpt-oss; extended to the qwen tier 2026-08-10 after
# hitting the identical failure live ("qwen/qwen3.6-27b returned an empty
# response — reasoning likely exhausted max_tokens before any answer was
# written", logs/errors.log). Two different value spaces per model family —
# see _reasoning_effort_for() below — so this is a dict, not one shared
# constant.
GROQ_REASONING_EFFORT = "low"                # gpt-oss: low/medium/high
_GROQ_QWEN_REASONING_EFFORT = "none"         # qwen3.x: none/default — "none" is the one that avoids this bug

# Which models in GROQ_MODEL_CHAIN accept a `reasoning_effort`-shaped param
# on Groq, and which value space each family uses — NOT supported by
# non-reasoning models (llama-3.3-70b-versatile, the compound/scout/instant
# tiers) — passing it to one is a 400 (confirmed) — hence resolving per
# prefix rather than applying either value unconditionally.
_REASONING_EFFORT_BY_PREFIX = (
    ("openai/gpt-oss", GROQ_REASONING_EFFORT),
    ("qwen/", _GROQ_QWEN_REASONING_EFFORT),
)


def _groq_supports_reasoning_effort(model: str) -> bool:
    return any(model.startswith(prefix) for prefix, _ in _REASONING_EFFORT_BY_PREFIX)


def _reasoning_effort_for(model: str) -> str | None:
    """The correct reasoning_effort VALUE for `model`'s family, or None if
    it doesn't take one at all — see _REASONING_EFFORT_BY_PREFIX above.
    Replaces the old single-constant GROQ_REASONING_EFFORT lookup (which
    only ever had gpt-oss's value space) as the call site in
    core.groq_client._groq_complete now uses."""
    for prefix, effort in _REASONING_EFFORT_BY_PREFIX:
        if model.startswith(prefix):
            return effort
    return None


# ---------------------------------------------------------------------------
# Groq client
# ---------------------------------------------------------------------------

# GROQ_API_KEYS — ordered list of every configured key, primary first.
# GROQ_API_KEY_2 (optional, .env) is a second key on a SEPARATE account/
# project — a distinct rate-limit budget, not just a spare copy of the same
# one. Currently commented out in .env at Joan's request (2026-08-10, ban-
# risk concern) — the value is preserved there, just inactive, so
# GROQ_API_KEYS resolves to one entry until it's uncommented again. Empty/
# unset entries are dropped, so a deployment with only GROQ_API_KEY set
# behaves exactly as before this existed (a one-key list, same as the old
# single-client singleton).
_ISOLATED_PERSONS = {"dani"}   # checked even when the person below has no key configured yet


def _rebuild_derived_keys() -> None:
    """(Re)builds GROQ_API_KEYS/_ISOLATED_PERSON_GROQ_KEYS from the current
    os.environ and drops every cached Groq client — both are otherwise only
    ever computed once at import time, so a key saved live via Ajustes
    (core.api_key_store.set_key -> on_change) would never take effect
    without this. Also runs once at the bottom of this module to do the
    original one-time computation."""
    global GROQ_API_KEYS, _ISOLATED_PERSON_GROQ_KEYS, CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN
    # GROQ_API_KEYS — ordered list of every configured key, primary first.
    # GROQ_API_KEY_2 (optional, .env) is a second key on a SEPARATE account/
    # project — a distinct rate-limit budget, not just a spare copy of the
    # same one. Currently commented out in .env at Joan's request
    # (2026-08-10, ban-risk concern) — the value is preserved there, just
    # inactive, so GROQ_API_KEYS resolves to one entry until it's
    # uncommented again. Empty/unset entries are dropped, so a deployment
    # with only GROQ_API_KEY set behaves exactly as before this existed (a
    # one-key list, same as the old single-client singleton).
    GROQ_API_KEYS = [k for k in (os.getenv("GROQ_API_KEY"), os.getenv("GROQ_API_KEY_2")) if k]

    # Per-person key isolation (2026-08-24, Joan's request: "when I access
    # from my computer it uses my api keys and when Dani accesses from his
    # computer it uses his"). GROQ_API_KEYS above stays Joan's own
    # resilience chain (primary + optional second-account key) exactly as
    # before — Joan is the default identity, so unidentified/Joan turns
    # keep using it unchanged. Dani is the only OTHER profile core.social
    # currently recognizes (see core.social._DEFAULT_PROFILES), so he's the
    # only one that needs an explicit override; a third profile would need
    # adding to _ISOLATED_PERSON_GROQ_KEYS the same way.
    #
    # Isolation is intentional, not a bug: Joan explicitly chose "no
    # fallback" over "fall back to my key" for Dani's turns (2026-08-24) —
    # if Dani's key is unset or rate-limited, his turns should degrade to
    # the local Ollama/offline fallback (see
    # core.groq_client._groq_complete's tail) rather than silently spending
    # Joan's quota. See active_groq_keys() below for where that's actually
    # enforced.
    _ISOLATED_PERSON_GROQ_KEYS = {
        person: key
        for person, key in {"dani": os.getenv("GROQ_API_KEY_DANI")}.items()
        if key
    }
    _groq_clients.clear()   # any client built from a now-replaced key must not linger

    # Cloudflare Workers AI fallback — see the module comment further down
    # this file for what these are. Rebuilt here too so a key saved via
    # Ajustes takes effect immediately, same as the Groq keys above.
    CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID")
    CLOUDFLARE_API_TOKEN  = os.getenv("CLOUDFLARE_API_TOKEN")


GROQ_API_KEYS: list[str] = []
_ISOLATED_PERSON_GROQ_KEYS: dict[str, str] = {}
_groq_clients: dict[str, Groq] = {}   # one cached client per key, keyed by the key itself


def active_groq_keys() -> list[str | None]:
    """The ordered key list _groq_complete()/_groq_stream_chunks() should
    walk for the CURRENTLY IDENTIFIED person (core.active_person, set by
    core.commands right after identify_person() resolves each turn).

    Joan (or nobody identified yet — voice-restricted/low-confidence turns)
    gets the existing shared GROQ_API_KEYS chain, unchanged from before this
    existed. An isolated person (currently just Dani) gets ONLY their own
    key — an empty list if it's not configured, never Joan's — so the
    caller's own key-exhausted tail (Cloudflare/Ollama/static fallback)
    kicks in instead of ever touching Joan's quota on Dani's behalf.
    """
    person = active_person.get_active_person()
    if person in _ISOLATED_PERSONS:
        key = _ISOLATED_PERSON_GROQ_KEYS.get(person)
        return [key] if key else []
    return GROQ_API_KEYS or [None]


def _get_groq(api_key: str | None = None):
    """Returns a cached Groq client for `api_key`, creating one on first
    use. Defaults to the primary key (GROQ_API_KEYS[0]) when api_key is
    omitted — every pre-existing call site that calls _get_groq() with no
    args keeps working unchanged."""
    key = api_key or (GROQ_API_KEYS[0] if GROQ_API_KEYS else None)
    if not key:
        raise RuntimeError("GROQ_API_KEY is not set in .env")
    if key not in _groq_clients:
        _groq_clients[key] = Groq(api_key=key)
    return _groq_clients[key]


# ---------------------------------------------------------------------------
# Cloudflare Workers AI — fallback provider (2026-08-10, added after the
# Cerebras attempt was reverted as unsuitable). Genuinely different
# infrastructure from Groq (Cloudflare has nothing to do with Groq's
# account/billing/rate-limit surface at all, unlike a second Groq key),
# with a real free tier. Deliberately NOT reused through _get_groq()/the
# `groq` SDK the way a true OpenAI-compatible provider would be — auth
# here needs BOTH an account id and a bearer token, which doesn't map onto
# the SDK's single api_key parameter — see core.groq_client._cloudflare_chat_fallback
# for the dedicated raw-HTTP adapter this uses instead (same
# dependency-light approach as the Ollama fallback).
#
# Model names verified 2026-08-10 against a real account — both tiers
# below confirmed reachable and answering (curl-equivalent smoke test via
# the actual /v1/chat/completions endpoint, real credentials, real
# response content). Re-verify the same way GROQ_MODEL_CHAIN's own history
# does if either ever starts silently failing (dead tiers there were
# caught the same way: grep 'CLOUDFLARE' in logs/activity.log for real
# per-model failure counts, same pattern that caught qwen3-32b/llama-4-scout).
# ---------------------------------------------------------------------------
CLOUDFLARE_ACCOUNT_ID: str | None = None
CLOUDFLARE_API_TOKEN: str | None = None

_DEFAULT_CLOUDFLARE_MODEL_CHAIN = ",".join([
    "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
    "@cf/meta/llama-3.1-8b-instruct-fast",
])
CLOUDFLARE_MODEL_CHAIN = [
    m.strip() for m in os.getenv("CLOUDFLARE_MODEL_CHAIN", _DEFAULT_CLOUDFLARE_MODEL_CHAIN).split(",")
    if m.strip()
]

_rebuild_derived_keys()               # initial computation — see that function's own docstring
api_key_store.on_change(_rebuild_derived_keys)   # keep it correct after every Ajustes key edit too
