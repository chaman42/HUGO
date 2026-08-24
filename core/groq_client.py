# ═══════════════════════════════════════════════════════════════════════════
# GROQ CLIENT — streaming/non-streaming completion calls and <think>/
# <thinking> tag stripping. Model-chain configuration lives in
# core/groq_config.py. Split out of core/commands.py (pure refactor, no
# behavior change).
# ═══════════════════════════════════════════════════════════════════════════
import json
import logging
import queue
import re
import ssl
import threading
import time
import urllib.request

import certifi

from core import memory
from core import groq_config
from core import groq_circuit_breaker
from core import ollama_control

logger = logging.getLogger(__name__)

# Bug fix (2026-08-10): this machine's Python has no system CA bundle at
# all (/Library/Frameworks/Python.framework/Versions/3.11/etc/openssl/cert.pem
# doesn't exist — the classic 'never ran Install Certificates.command'
# state python.org's installer leaves behind) — every plain
# urllib.request HTTPS call fails with SSL: CERTIFICATE_VERIFY_FAILED as a
# result. Doesn't affect the Groq SDK itself (httpx, which it's built on,
# bundles its own certifi-backed default and never hit this), and doesn't
# affect the Ollama fallback (plain HTTP, no TLS involved) — only
# _cloudflare_chat_fallback below, the one urllib-based HTTPS call in this
# file. Built once at import time and reused, same as _get_groq()'s own
# client caching — no reason to reconstruct a cafile-backed SSLContext on
# every single call.
_HTTPS_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

# Last resort in _groq_complete()'s fallback chain — tried only after every
# tier of groq_config.GROQ_MODEL_CHAIN has failed, right before giving up to
# response._static_fallback()'s canned offline reply. llama3.2:1b — same
# model core.social_reasoning already uses for its own fast Ollama checks
# (duplicated as a literal rather than imported — same dependency-isolation
# reasoning ollama_control.py's own docstring already documents for
# OLLAMA_HOST/OLLAMA_TAGS_URL), so no extra `ollama pull` is required.
#
# 45s timeout — measured directly on this machine (CPU-only inference,
# consistently ~35-40s per call regardless of warm/cold state, not a
# cold-load artifact): 3b timed out at 20s with no reply at all, 1b
# consistently took ~37s, tinyllama ~16s but noticeably weaker/more
# error-prone in Spanish. Chose 1b for quality over tinyllama's speed —
# this path only ever runs after every Groq tier has already failed (a
# real outage), so Joan is already waiting; a real in-character answer at
# 45s beats a canned 'Sin conexión' either way. Revisit this constant if a
# future machine's Ollama inference is meaningfully faster/slower.
_OLLAMA_FALLBACK_MODEL   = "llama3.2:1b"
_OLLAMA_FALLBACK_TIMEOUT = 45.0
_OLLAMA_CHAT_URL         = f"{ollama_control.OLLAMA_HOST}/api/chat"


def _ollama_chat_fallback(messages: list[dict], max_tokens: int) -> str | None:
    """One /api/chat call to the local Ollama daemon, given the exact same
    `messages` list Groq was just handed (HUGO's system prompt + rolling
    history + this turn) — unlike core.sleep_llm._ollama_generate's
    /api/generate (a flat system+user pair, fine for one-shot sleep-phase
    prompts), /api/chat accepts the same role/content shape Groq already
    uses, so nothing about character or conversation context is lost on
    this path. Best-effort: starts the daemon if it isn't already running
    (a no-op in the common case — 'ollama serve' is meant to stay up as an
    always-on service, see ollama_control's own module docstring), but
    never waits beyond one reachability probe for it to come up — a cold
    daemon start racing this call is treated the same as 'unreachable'.
    Returns None on any failure (daemon down, model not pulled, timeout,
    empty response); never raises."""
    ollama_control.ensure_ollama_daemon_running()
    if not ollama_control.is_ollama_daemon_reachable():
        return None
    try:
        payload = json.dumps({
            "model":    _OLLAMA_FALLBACK_MODEL,
            "messages": messages,
            "stream":   False,
            "options":  {"num_predict": max_tokens},
        }).encode("utf-8")
        req = urllib.request.Request(
            _OLLAMA_CHAT_URL, data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=_OLLAMA_FALLBACK_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = str((data.get("message") or {}).get("content", "")).strip()
        clean_text, _thinking = _strip_thinking(text)
        return clean_text or None
    except Exception as e:
        logger.debug("[GROQ] Ollama last-resort fallback failed: %s", e)
        return None


# Cloudflare Workers AI — cloud fallback, tried BEFORE the local Ollama
# attempt (Joan's own ordering: Groq -> Cloudflare -> Ollama -> static) —
# a real remote model should beat a small local one on quality whenever
# it's actually reachable; Ollama stays the true last-resort for when
# every cloud option, Groq included, is unreachable. See
# groq_config.CLOUDFLARE_MODEL_CHAIN's own module comment for the
# unverified-model-list caveat.
_CLOUDFLARE_TIMEOUT = 15.0   # real cloud inference — no reason to wait as long as Ollama's local CPU generation

# Bug fix / tuning (2026-08-10): tried swapping in @cf/openai/gpt-oss-120b
# (the same model Groq's own top tier uses) hoping for a closer character
# match — Cloudflare's serving of it is broken (None responses, raw
# '<|start|>assistant' control tokens leaking into content on 2 of 3 real
# test prompts). @cf/qwen/qwen3-30b-a3b-fp8 was worse (None on 2 of 3,
# same reasoning-token-exhaustion failure mode already documented for
# qwen on Groq — see _GROQ_QWEN_REASONING_EFFORT's own comment). The
# CLOUDFLARE_MODEL_CHAIN llama tier stayed the only reliable one (0
# failures across every real test) — so instead of chasing a better
# model, this reinforces HUGO's voice at the PROMPT level specifically for
# this fallback: a weaker/smaller model follows nuanced show-don't-tell
# examples worse than a blunt, explicit restatement of the same rules, and
# a lower temperature keeps it from drifting into generic assistant
# enthusiasm ('¡Hola!', exclamation marks, emojis) the way the default
# temperature did in side-by-side testing. Appended as its own system
# message right before the final user turn (not merged into the existing
# system message) so it reads as the most recent/salient instruction.
_CLOUDFLARE_VOICE_REINFORCEMENT = (
    "Recuerda tu voz: nunca uses signos de exclamación, nunca saludes de "
    "forma genérica ('¡Hola!'), nunca uses emojis. Sé seca, directa, "
    "irónica y con carácter — nunca entusiasta ni una asistente servicial "
    "de manual."
)
_CLOUDFLARE_TEMPERATURE = 0.6


def _cloudflare_chat_fallback(messages: list[dict], max_tokens: int) -> str | None:
    """Walks groq_config.CLOUDFLARE_MODEL_CHAIN, one non-streamed attempt
    per model, via Cloudflare's OpenAI-compatible chat-completions endpoint
    (https://api.cloudflare.com/client/v4/accounts/{id}/ai/v1/chat/completions).
    Not routed through _get_groq()/the `groq` SDK — see
    groq_config.CLOUDFLARE_ACCOUNT_ID's own module comment for why this
    provider's auth shape (account id + bearer token) doesn't fit that
    reuse the way a true single-api-key OpenAI-compatible provider would.
    No-op (returns None immediately) if CLOUDFLARE_ACCOUNT_ID/
    CLOUDFLARE_API_TOKEN aren't configured — same graceful-degradation
    shape as GROQ_API_KEY_2 being unset. Never raises."""
    account_id = groq_config.CLOUDFLARE_ACCOUNT_ID
    api_token  = groq_config.CLOUDFLARE_API_TOKEN
    if not account_id or not api_token:
        return None

    # Reinforcement inserted right before the final (user) turn — see
    # _CLOUDFLARE_VOICE_REINFORCEMENT's own comment above for why this
    # fallback needs it and the primary Groq chain doesn't.
    reinforced_messages = list(messages)
    reinforced_messages.insert(len(reinforced_messages) - 1 if reinforced_messages else 0,
                                {"role": "system", "content": _CLOUDFLARE_VOICE_REINFORCEMENT})

    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/chat/completions"
    for i, model in enumerate(groq_config.CLOUDFLARE_MODEL_CHAIN):
        try:
            payload = json.dumps({
                "model":       model,
                "messages":    reinforced_messages,
                "max_tokens":  max_tokens,
                "temperature": _CLOUDFLARE_TEMPERATURE,
            }).encode("utf-8")
            req = urllib.request.Request(
                url, data=payload, method="POST",
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_token}"},
            )
            with urllib.request.urlopen(req, timeout=_CLOUDFLARE_TIMEOUT, context=_HTTPS_SSL_CONTEXT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = str((data.get("choices") or [{}])[0].get("message", {}).get("content", "")).strip()
            clean_text, _thinking = _strip_thinking(text)
            if clean_text:
                return clean_text
            logger.debug("[CLOUDFLARE] %s returned an empty response", model)
        except Exception as e:
            logger.debug("[CLOUDFLARE] %s failed (%s)%s", model, e,
                         f" — trying {groq_config.CLOUDFLARE_MODEL_CHAIN[i + 1]}" if i < len(groq_config.CLOUDFLARE_MODEL_CHAIN) - 1 else "")
    return None


# ---------------------------------------------------------------------------
# Chain-of-thought stripping
#
# Most of groq_config.GROQ_MODEL_CHAIN's tiers have no reasoning step at all, so this is
# a no-op for those — but it's essential for the tiers that do: qwen3-32b
# and qwen3.6-27b inline reasoning as <think>...</think> in `content`
# (DeepSeek R1's style), while gpt-oss keeps it in a separate API field we
# simply never request/read. Reasoning must never reach chat or TTS
# regardless of which model in the chain actually answered.
# ---------------------------------------------------------------------------

_THINK_BLOCK_RE = re.compile(r"<think(?:ing)?>(.*?)</think(?:ing)?>", re.IGNORECASE | re.DOTALL)
_THINK_OPEN_RE  = re.compile(r"<think(?:ing)?>", re.IGNORECASE)


def _strip_thinking(text: str) -> tuple[str, str]:
    """Split raw LLM output into (clean_text, thinking_text).

    Handles a normally closed <think>/<thinking> block, and also an
    unclosed one (e.g. truncated by max_tokens) by treating everything from
    the opening tag onward as thinking and cutting it from the clean text.
    """
    thinking_parts = _THINK_BLOCK_RE.findall(text)
    clean = _THINK_BLOCK_RE.sub("", text)

    unclosed = _THINK_OPEN_RE.search(clean)
    if unclosed:
        thinking_parts.append(clean[unclosed.start():])
        clean = clean[:unclosed.start()]

    clean_text    = clean.strip()
    thinking_text = "\n".join(p.strip() for p in thinking_parts if p.strip())
    return clean_text, thinking_text


_THINK_CLOSE_RE = re.compile(r"</think(?:ing)?>", re.IGNORECASE)


def _groq_stream_complete(
    messages: list[dict],
    model: str,
    max_tokens: int = 256,
    first_token_timeout: float = groq_config.GROQ_FIRST_TOKEN_TIMEOUT,
    reasoning_effort: str | None = None,
    api_key: str | None = None,
) -> tuple[str, str, float, float | None]:
    """Stream a completion from `model`, returning
    (clean_text, thinking_text, ttft, thinking_done_at).

    Consumes the Groq stream from a background thread into a queue so the
    caller can enforce a hard time-to-first-token budget: if nothing arrives
    within `first_token_timeout` seconds, raises TimeoutError so the caller
    can retry with a faster model instead of waiting indefinitely on a slow
    reasoning model. Once the first token has arrived, the rest of the
    (already-started) stream is drained normally — the budget only guards
    the wait for the *first* token, matching "start streaming" in the spec.

    thinking_done_at is the elapsed time (from this call's start) at which
    a closing </think> tag was first seen in the running buffer — i.e. how
    much of the total latency was internal reasoning versus generating the
    visible answer. Reads as None (not tracked) for most of
    groq_config.GROQ_MODEL_CHAIN's tiers, which have no reasoning step at all — this
    only populates for a tier that inlines reasoning as <think> tags in
    content, the way DeepSeek R1 and qwen3 (on Groq) do; a tier like
    gpt-oss that keeps reasoning in a separate API field we don't
    request/read won't populate this either.
    This is purely a latency diagnostic: the
    *caller* still can't hand text to voice.py's speak()
    functions until the whole stream finishes, since those take one
    complete string — true token-level TTS streaming would require changes
    to core/voice.py, out of scope for this change.

    api_key: which of groq_config.GROQ_API_KEYS to use — None defaults to
    the primary key (see groq_config._get_groq's own docstring). Passed
    through by _groq_complete() when it's retrying the whole chain on a
    second configured key after the first one's chain fully failed.
    """
    q: queue.Queue = queue.Queue()
    exc_box: list[Exception] = []

    def _produce():
        try:
            extra_kwargs = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}
            stream = groq_config._get_groq(api_key).chat.completions.create(
                model=model, max_tokens=max_tokens, messages=messages, stream=True, **extra_kwargs,
            )
            for chunk in stream:
                delta = chunk.choices[0].delta.content
                if delta:
                    q.put(delta)
        except Exception as e:
            exc_box.append(e)
        finally:
            q.put(None)   # sentinel: stream done (success or failure)

    t_start = time.monotonic()
    threading.Thread(target=_produce, daemon=True, name="groq-stream").start()

    try:
        first = q.get(timeout=first_token_timeout)
    except queue.Empty:
        raise TimeoutError(f"no token from {model} within {first_token_timeout}s")

    ttft = time.monotonic() - t_start
    logger.info("[LATENCY] T3_first_token model=%s ttft=%.3fs", model, ttft)

    if first is None:
        # Stream ended before producing any content — either a genuinely
        # empty response or an upstream exception.
        if exc_box:
            raise exc_box[0]
        return "", "", ttft, None

    pieces: list[str] = [first]
    acc = first
    thinking_done_at: float | None = None
    if _THINK_CLOSE_RE.search(acc):
        thinking_done_at = time.monotonic() - t_start

    while True:
        item = q.get()   # stream already started — no further per-token timeout
        if item is None:
            break
        pieces.append(item)
        if thinking_done_at is None:
            acc += item
            if _THINK_CLOSE_RE.search(acc):
                thinking_done_at = time.monotonic() - t_start

    if exc_box and not pieces:
        raise exc_box[0]
    elif exc_box:
        logger.warning("[GROQ] stream for %s ended early: %s", model, exc_box[0])

    clean_text, thinking_text = _strip_thinking("".join(pieces))
    return clean_text, thinking_text, ttft, thinking_done_at


# ═══════════════════════════════════════════════════════════════════════════
# SENTENCE-CHUNKED STREAMING (2026-08-14) — the "true token-level TTS
# streaming" _groq_stream_complete's own docstring flagged as out of scope.
# core.voice's TTS pipeline turned out to already be the easy half: every
# speak_*() function just enqueues onto a single-worker FIFO (_tts_queue /
# _tts_worker in core/voice.py) and returns immediately, so calling it once
# per completed sentence — instead of once with the whole reply — already
# gets correct in-order playback with zero changes to voice.py. This is the
# other half: turn the raw token stream into ready-to-speak sentence chunks
# as they arrive, instead of buffering the entire reply before returning.
#
# See core.commands' streaming call site for how this is actually wired to
# _say_for() and why it's gated to confirmed-Joan turns only (the secret-
# protection filter needs the complete text before anything is spoken).
# ═══════════════════════════════════════════════════════════════════════════

_SENTENCE_END_RE  = re.compile(r"(?<=[.!?…])\s+")
_MAX_CHUNK_CHARS  = 220   # force a split even without punctuation, so one runaway "sentence" can't hold up playback indefinitely
_THINK_PROBE_LEN  = 12    # long enough to reliably tell '<think>'/'<thinking>' apart from real prose
_THINK_OPEN_PROBE_RE = re.compile(r"^\s*<think(?:ing)?>", re.IGNORECASE)


def _split_ready_sentences(buf: str) -> tuple[list[str], str]:
    """Given accumulated safe-to-speak text, returns (complete sentences
    ready to hand to TTS now, remainder still being accumulated). The last
    piece is always held back — more text may still arrive that belongs to
    the same trailing sentence — except when nothing has looked like a
    sentence boundary in a while, at which point _MAX_CHUNK_CHARS forces a
    chunk anyway rather than letting an unpunctuated stream withhold
    playback forever."""
    parts = _SENTENCE_END_RE.split(buf)
    if len(parts) > 1:
        complete, remainder = parts[:-1], parts[-1]
        return [p.strip() for p in complete if p.strip()], remainder
    if len(buf) > _MAX_CHUNK_CHARS:
        cut = buf.rfind(" ", 0, _MAX_CHUNK_CHARS)
        if cut <= 0:
            cut = _MAX_CHUNK_CHARS
        return [buf[:cut].strip()], buf[cut:].lstrip()
    return [], buf


def _groq_stream_chunks_one_model(
    messages: list[dict],
    model: str,
    max_tokens: int,
    first_token_timeout: float = groq_config.GROQ_FIRST_TOKEN_TIMEOUT,
    reasoning_effort: str | None = None,
    api_key: str | None = None,
):
    """One tier's worth of streaming, chunked into ready-to-speak sentences
    — the sentence-level sibling of _groq_stream_complete, same
    producer-thread/queue/TTFT-timeout shape (see its docstring for that
    part). Raises TimeoutError / the underlying API exception / a
    'reasoning exhausted the budget' RuntimeError under the exact same
    conditions _groq_stream_complete does, so _groq_stream_chunks' chain-
    walking reacts identically to a tier that produced nothing at all.

    Handles an inline <think>/<thinking> block (qwen3.6-27b on Groq does
    this; most tiers don't) by buffering silently until it's seen the
    whole thing — nothing inside it is ever yielded — same "cut from the
    start of the tag onward" treatment as _strip_thinking gives an
    unclosed one truncated by max_tokens."""
    q: queue.Queue = queue.Queue()
    exc_box: list[Exception] = []

    def _produce():
        try:
            extra_kwargs = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}
            stream = groq_config._get_groq(api_key).chat.completions.create(
                model=model, max_tokens=max_tokens, messages=messages, stream=True, **extra_kwargs,
            )
            for chunk in stream:
                delta = chunk.choices[0].delta.content
                if delta:
                    q.put(delta)
        except Exception as e:
            exc_box.append(e)
        finally:
            q.put(None)

    t_start = time.monotonic()
    threading.Thread(target=_produce, daemon=True, name="groq-stream-chunks").start()

    try:
        first = q.get(timeout=first_token_timeout)
    except queue.Empty:
        raise TimeoutError(f"no token from {model} within {first_token_timeout}s")

    ttft = time.monotonic() - t_start
    logger.info("[LATENCY] T3_first_token model=%s ttft=%.3fs (streaming)", model, ttft)

    if first is None:
        if exc_box:
            raise exc_box[0]
        raise RuntimeError(f"{model} returned an empty response (streaming)")

    raw_buf = first
    speak_buf = ""
    in_thinking: bool | None = None   # None=undecided, True=suppressing, False=normal

    def _maybe_decide(final: bool):
        nonlocal in_thinking, speak_buf, raw_buf
        if in_thinking is not None or (len(raw_buf) < _THINK_PROBE_LEN and not final):
            return
        if _THINK_OPEN_PROBE_RE.match(raw_buf):
            in_thinking = True
        else:
            in_thinking = False
            speak_buf, raw_buf = raw_buf, ""

    def _maybe_close_thinking():
        nonlocal in_thinking, speak_buf, raw_buf
        if in_thinking is not True:
            return
        m = _THINK_CLOSE_RE.search(raw_buf)
        if m:
            in_thinking = False
            speak_buf = raw_buf[m.end():]
            raw_buf = ""

    item = first
    is_first_iteration = True
    while True:
        stream_ended = item is None
        # Bug fix: identity-comparing `item is not first` to skip
        # re-appending the seed token broke if a later token happened to
        # be an interned single-char string equal (by identity, not just
        # value) to `first` — CPython interns short ASCII strings, so that
        # collision is real, not hypothetical. An explicit flag has no such
        # footgun.
        if not is_first_iteration and item is not None:
            raw_buf += item
            if in_thinking is False:
                speak_buf += item
        is_first_iteration = False

        _maybe_decide(final=stream_ended)
        _maybe_close_thinking()

        if in_thinking is False:
            ready, speak_buf = _split_ready_sentences(speak_buf)
            for s in ready:
                yield s

        if stream_ended:
            if in_thinking is True:
                # Unclosed thinking block ate the whole budget — same bug
                # class _groq_stream_complete's empty-response guard exists
                # for, just discovered here instead of after the fact.
                if exc_box:
                    raise exc_box[0]
                raise RuntimeError(
                    f"{model} returned an empty response (streaming) — "
                    "reasoning likely exhausted max_tokens before any answer was written"
                )
            if speak_buf.strip():
                yield speak_buf.strip()
            if exc_box:
                raise exc_box[0]
            return

        item = q.get()


def _groq_stream_chunks(messages: list[dict], max_tokens: int = 256):
    """Generator version of _groq_complete — yields each ready-to-speak
    sentence chunk of the reply as soon as it's available, instead of
    buffering the whole thing before returning. Walks the same
    groq_config.GROQ_MODEL_CHAIN chain and multi-key retry
    _groq_complete does, with one real difference: once a tier has
    yielded at least one real chunk, a later failure from that SAME tier
    just ends the generator instead of retrying elsewhere — there's no
    way to take back speech that's likely already reached the TTS queue.
    Failure before any chunk is yielded (timeout, rate limit, empty
    response) still walks the rest of the chain exactly like
    _groq_complete.

    Callers that need the complete final text (session history, skill-
    marker detection, memory extraction) should join everything this
    yields — see core.commands' streaming call site, which does exactly
    that while also handing each chunk to _say_for() as it arrives."""
    chain: list[str] = []
    for candidate in groq_config.GROQ_MODEL_CHAIN:
        if candidate not in chain:
            chain.append(candidate)
    if not chain:
        chain = [groq_config.GROQ_MODEL_FALLBACK]

    # See _groq_complete's own comment on active_groq_keys() — same
    # per-person isolation applies here. Streaming is only ever attempted
    # for Joan/unidentified turns today (core.commands' _safe_to_stream
    # gate), so an isolated person hitting this at all would be a future
    # change elsewhere, not a case seen in practice right now — handled
    # here anyway so it's correct if that gate ever changes. An empty list
    # just yields nothing, and the "every tier failed" comment below
    # already documents the caller's own non-streaming fallback.
    keys = groq_config.active_groq_keys()

    for api_key in keys:
        for model in chain:
            yielded_any = False
            try:
                effort = groq_config._reasoning_effort_for(model)
                for chunk in _groq_stream_chunks_one_model(
                    messages, model, max_tokens, reasoning_effort=effort, api_key=api_key,
                ):
                    yielded_any = True
                    yield chunk
                if yielded_any:
                    return
                logger.warning("[GROQ-STREAM] %s produced no content — falling back", model)
            except Exception as e:
                if yielded_any:
                    logger.warning(
                        "[GROQ-STREAM] %s failed mid-reply after already yielding chunks (%s) — "
                        "stopping, not retrying elsewhere", model, e,
                    )
                    return
                logger.warning("[GROQ-STREAM] %s unavailable/empty (%s) — falling back", model, e)
                continue
    # Every tier failed with zero output — caller falls back to the
    # non-streaming _groq_complete() as a last resort (see core.commands).


def _groq_complete_fast(messages: list[dict], max_tokens: int = 256) -> str:
    """Non-streamed completion for small internal utility calls (memory-fact
    extraction, history compression, search-query translation) that don't
    need chain-of-thought reasoning and shouldn't pay a heavier
    groq_config.GROQ_MODEL_CHAIN tier's latency. Walks groq_config._GROQ_FAST_CHAIN (see its module
    comment) top to bottom, falling through immediately on a rate limit, any
    other error, or an empty response — same failure-handling shape as
    _groq_complete(). Passes reasoning_effort per-model (added 2026-08-19,
    when GROQ_MODEL_FALLBACK became a reasoning model for the first time —
    see its own comment): without this, a gpt-oss/qwen tier here can burn
    its whole max_tokens budget on internal reasoning and return empty, the
    same bug groq_config.GROQ_REASONING_EFFORT's comment documents for the
    main chain. If every tier fails, the last exception propagates out
    uncaught; every caller here already treats that as non-critical (logs a
    warning and keeps going) rather than crashing."""
    last_exc: Exception | None = None
    for i, model in enumerate(groq_config._GROQ_FAST_CHAIN):
        try:
            effort = groq_config._reasoning_effort_for(model)
            extra_kwargs = {"reasoning_effort": effort} if effort else {}
            response = groq_config._get_groq().chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=messages,
                **extra_kwargs,
            )
            content = response.choices[0].message.content or ""
            clean_text, _thinking = _strip_thinking(content.strip())
            if not clean_text.strip():
                raise RuntimeError(f"{model} returned an empty response")
            return clean_text
        except Exception as e:
            last_exc = e
            if i < len(groq_config._GROQ_FAST_CHAIN) - 1:
                logger.warning(
                    "[GROQ-FAST] %s unavailable/empty (%s) — falling back to %s",
                    model, e, groq_config._GROQ_FAST_CHAIN[i + 1],
                )
            continue
    raise last_exc


def _groq_complete_extract(messages: list[dict], max_tokens: int = 500) -> str:
    """Non-streamed completion for memory-fact extraction
    (_extract_and_save_memory in core/memory_extract.py) — the one
    _groq_complete_fast caller that runs in a background daemon thread after
    the reply is already sent, so it can afford a reasoning-tier model
    instead of the small/fast one, in exchange for more reliably following
    the extraction prompt's inclusion/exclusion rules. Walks
    groq_config._GROQ_EXTRACT_CHAIN (see its module comment) top to bottom,
    same failure-handling shape as _groq_complete_fast: falls through
    immediately on a rate limit, any other error, or an empty response, and
    passes reasoning_effort per-model for the same reason _groq_complete_fast
    now does (see its own comment); if every tier fails, the last exception
    propagates out uncaught (the caller already treats extraction failure as
    non-critical)."""
    last_exc: Exception | None = None
    for i, model in enumerate(groq_config._GROQ_EXTRACT_CHAIN):
        try:
            effort = groq_config._reasoning_effort_for(model)
            extra_kwargs = {"reasoning_effort": effort} if effort else {}
            response = groq_config._get_groq().chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=messages,
                **extra_kwargs,
            )
            content = response.choices[0].message.content or ""
            clean_text, _thinking = _strip_thinking(content.strip())
            if not clean_text.strip():
                raise RuntimeError(f"{model} returned an empty response")
            return clean_text
        except Exception as e:
            last_exc = e
            if i < len(groq_config._GROQ_EXTRACT_CHAIN) - 1:
                logger.warning(
                    "[GROQ-EXTRACT] %s unavailable/empty (%s) — falling back to %s",
                    model, e, groq_config._GROQ_EXTRACT_CHAIN[i + 1],
                )
            continue
    raise last_exc


def _groq_complete(messages: list[dict], max_tokens: int = 256) -> str:
    """Main conversational LLM call. Walks groq_config.GROQ_MODEL_CHAIN (see its module
    comment above) top to bottom — one streamed attempt per model, each
    capped by groq_config.GROQ_FIRST_TOKEN_TIMEOUT (5s) — deduping any tier that repeats
    a model already earlier in the chain. reasoning_effort is applied only
    if a given tier's model actually supports it (see
    groq_config._groq_supports_reasoning_effort). A rate limit, any other error, or a
    genuinely empty response (see groq_config.GROQ_REASONING_EFFORT's comment for the
    gpt-oss-era bug this guards against) all count as a failure and
    immediately advance to the next tier — no retries within a tier, and no
    extra non-streamed attempt tacked onto the last one; every tier is
    treated identically.

    Before each attempt, core.groq_circuit_breaker.should_skip() can skip a
    tier entirely without even making the call, if that model is in a
    failure cooldown from a PAST request — a permanently-404ing model
    (groq.NotFoundError) or a rate limit with a known retry-after time
    (groq.RateLimitError), as opposed to this function's own per-request
    5s timeout, which only ever judges the current attempt in isolation
    and has no memory across requests. See that module's own comment for
    the concrete motivating case (two models 404ing on every single call
    for over a day before a human noticed and pruned the chain).

    If groq_config.GROQ_API_KEYS has more than one entry (GROQ_API_KEY_2 set
    in .env — a second key on a separate account/project, a genuinely
    separate rate-limit budget), the ENTIRE chain above is retried once per
    additional key before giving up — the failure mode this addresses is
    real account-level rate/concurrency pressure exhausting every tier at
    once (observed 2026-08-10: a burst of concurrent requests drove all 8
    tiers into cascading timeouts within their own 5s budgets each), which
    a same-account key wouldn't fix but a distinct account's budget does.

    Once every key × the whole chain has failed, one local Ollama attempt
    runs before giving up entirely (see _ollama_chat_fallback) —
    dispatch_command's own exception handler then falls through to
    _static_fallback() (the offline chain's true final tier) only if that
    fails too. Any <think>/<thinking> block is stripped and logged at
    DEBUG, never shown to the user. Which model/key is attempted is logged
    at DEBUG on every try (key identified by index only — 'key 1'/'key 2',
    never the value itself); final latency is logged at INFO.
    """
    t_start = time.monotonic()

    chain: list[str] = []
    for candidate in groq_config.GROQ_MODEL_CHAIN:
        if candidate not in chain:
            chain.append(candidate)
    if not chain:
        chain = [groq_config.GROQ_MODEL_FALLBACK]   # groq_config.GROQ_MODEL_CHAIN misconfigured empty — don't crash outright

    # core.groq_config.active_groq_keys() — Joan (or nobody identified)
    # gets the shared GROQ_API_KEYS chain as before; an isolated person
    # (currently just Dani, see that function's own docstring) gets ONLY
    # their own key, or an empty list if unset — deliberately NOT
    # `or [None]` for that case, so an empty list skips the loop below
    # entirely rather than falling back to the shared/default key.
    keys = groq_config.active_groq_keys()

    model_used = chain[0]
    thinking_text = ""
    ttft: float | None = None
    thinking_done_at: float | None = None
    clean_text = ""
    # Priming last_exc when keys is empty (isolated person, no key
    # configured) makes the loop below's zero iterations look like "every
    # key's chain already failed" to the fallback tail further down —
    # otherwise last_exc would stay None (its "success" value) despite
    # clean_text never having been set, silently returning an empty reply
    # instead of falling through to Cloudflare/Ollama/static.
    last_exc: Exception | None = None if keys else RuntimeError(
        "no Groq API key configured for the current identified person"
    )

    for key_idx, api_key in enumerate(keys):
        for i, model in enumerate(chain):
            # Circuit breaker (see core/groq_circuit_breaker.py's own module
            # comment) — skip a model already known to be in a failure
            # cooldown (permanently 404ing, or rate-limited with a known
            # retry-after) without even attempting the call. Still records
            # last_exc so this key's chain isn't mistaken for a success if
            # every tier ends up skipped.
            if groq_circuit_breaker.should_skip(model):
                model_used = model
                last_exc = RuntimeError(f"{model} skipped (circuit breaker cooldown)")
                logger.debug("[GROQ] %s skipped — still in circuit-breaker cooldown", model)
                continue
            logger.debug(
                "[GROQ] attempting model=%s (tier %d/%d, key %d/%d)",
                model, i + 1, len(chain), key_idx + 1, len(keys),
            )
            try:
                effort = groq_config._reasoning_effort_for(model)
                clean_text, thinking_text, ttft, thinking_done_at = _groq_stream_complete(
                    messages, model, max_tokens, reasoning_effort=effort, api_key=api_key,
                )
                if not clean_text.strip():
                    raise RuntimeError(
                        f"{model} returned an empty response "
                        "(reasoning likely exhausted max_tokens before any answer was written)"
                    )
                model_used = model
                last_exc = None
                groq_circuit_breaker.record_success(model)
                break
            except Exception as e:
                model_used = model
                last_exc = e
                groq_circuit_breaker.record_failure(model, e)
                if i < len(chain) - 1:
                    logger.warning(
                        "[GROQ] %s unavailable/slow/empty (%s) — falling back to %s for this request",
                        model, e, chain[i + 1],
                    )
                continue
        if last_exc is None:
            break   # this key's chain succeeded — no need to try the next key
        if key_idx < len(keys) - 1:
            logger.warning(
                "[GROQ] entire chain failed on key %d/%d (%s) — retrying full chain on key %d",
                key_idx + 1, len(keys), last_exc, key_idx + 2,
            )

    if last_exc is not None:
        # Every Groq tier failed — try Cloudflare Workers AI (real cloud
        # inference, different infrastructure entirely from Groq) before
        # falling all the way to the local Ollama model, and only then to
        # response._static_fallback()'s canned offline reply. Joan's own
        # ordering: Groq -> Cloudflare -> Ollama -> static. Both fallbacks
        # are no-ops (return None) if unconfigured, so an install with
        # neither set behaves exactly as before either existed.
        cloudflare_reply = _cloudflare_chat_fallback(messages, max_tokens)
        if cloudflare_reply:
            clean_text  = cloudflare_reply
            model_used  = "cloudflare"
            thinking_text = ""   # clear whatever a FAILED Groq tier left behind — never misattribute stale thinking to this model
            ttft        = None
            thinking_done_at = None
            last_exc    = None
            logger.warning("[GROQ] entire chain failed — answered via Cloudflare Workers AI fallback")
        else:
            ollama_reply = _ollama_chat_fallback(messages, max_tokens)
            if ollama_reply:
                clean_text  = ollama_reply
                model_used  = f"ollama:{_OLLAMA_FALLBACK_MODEL}"
                thinking_text = ""   # clear whatever a FAILED Groq/Cloudflare tier left behind — never misattribute stale thinking to this model
                ttft        = None
                thinking_done_at = None
                last_exc    = None
                logger.warning("[GROQ] entire chain failed — answered via local Ollama fallback (%s)", _OLLAMA_FALLBACK_MODEL)
            else:
                raise last_exc

    if thinking_text:
        logger.debug("[THINK] %s", thinking_text)
        user_query = next(
            (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        # Distinct INFO-level tag (the DEBUG line above never reaches
        # logs/activity.log — that handler is INFO+, see jarvis.py) so
        # GET /api/think_log has real data to read: one JSON object per
        # line, parsed back out by get_think_log(). Best-effort — a
        # logging hiccup must never break the actual reply.
        try:
            logger.info(
                "[THINK_LOG] %s",
                json.dumps(
                    {"query": user_query, "thinking": thinking_text, "model": model_used, "ts": memory._now_iso()},
                    ensure_ascii=False,
                ),
            )
        except Exception:
            logger.debug("Failed to write [THINK_LOG] entry", exc_info=True)
        # Live 'hugo_thinking' socket event — CORE app's Pensamiento tab
        # listens for this so a new thinking block appears the moment this
        # call finishes (see ui/index.html's 'hugo_thinking' listener).
        # Best-effort, mirrors _maybe_emit_panel()'s import pattern.
        try:
            import core.server as server_mod
            server_mod.emit_hugo_thinking({
                "query": user_query,
                "thinking": thinking_text,
                "model": model_used,
            })
        except Exception:
            logger.debug("Failed to emit hugo_thinking", exc_info=True)

    ttft_str    = f"{ttft:.3f}s" if ttft is not None else "n/a"
    think_str   = f"{thinking_done_at:.3f}s" if thinking_done_at is not None else "n/a"
    total_s     = time.monotonic() - t_start
    logger.info(
        "[LATENCY] groq_call model=%s ttft=%s thinking_done=%s total=%.3fs",
        model_used, ttft_str, think_str, total_s,
    )
    # Queryable snapshot of the last call — GET /api/info reads this for the
    # CORE app's Estado tab (model in use, last response latency). Not
    # persisted to disk; resets on restart, which is fine for a live status
    # display. Mutated in place (clear+update), not rebound, so any other
    # module holding a `from core.groq_config import _last_latency` reference
    # keeps seeing live updates rather than a stale first-import snapshot.
    groq_config._last_latency.clear()
    groq_config._last_latency.update({
        "model": model_used,
        "ttft": ttft,
        "thinking_done": thinking_done_at,
        "total": total_s,
        "at": memory._now_iso(),
    })

    return clean_text
