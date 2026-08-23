# ═══════════════════════════════════════════════════════════════════════════
# DISCORD BRIDGE — lets Joan DM LIRA directly from Discord. Routes DMs
# through the exact same LLM call core/commands.py's own dispatch pipeline
# uses (core.groq_client._groq_complete — no duplicated call logic here),
# built with LIRA's own system prompt (core/personalities/lira.py,
# unmodified) plus a relevance-scored memory block pulled from
# data/memory_lira.json / data/memory_shared.json via the existing
# core/memory.py helpers — the same Layer 1/2 facts
# core/personalities/base.py's _build_system_prompt injects for the voice
# assistant, just assembled standalone here rather than pulling in that
# function's voice/HUD-specific layers (weather, listen mode, session
# duration, etc.), which have no meaning in a Discord DM.
#
# Deliberately its own conversation history — NOT core/session.py's
# in-process history, which belongs to the voice assistant's own live
# session and would be the wrong thing to interleave Discord messages into.
# Kept PER SENDER (see _histories below), not a single shared buffer — once
# more than one Discord account can talk to the bridge (see the
# authorization system below), a shared history would leak one person's
# turns into another's conversation, admin included.
#
# ── Two-tier authorization (checked first, before any other Discord logic
# in on_message — nothing below the role check ever runs for someone who
# hasn't cleared it) ─────────────────────────────────────────────────────
#   admin   — Joan only. Her ID (DISCORD_JOAN_ID, from .env) is hardcoded as
#             admin and checked BEFORE data/discord_authorized.json is ever
#             read — she's never stored in that file, so she can't be
#             removed from it, and nothing written there can grant admin.
#             Full access: memory context, sleep insights, !memory/!sleep,
#             and the authorization commands themselves
#             (!autorizar/!bloquear/!autorizados/!sí/!no).
#   user    — explicitly authorized by Joan (data/discord_authorized.json).
#             Plain conversation only — no memory context is ever built for
#             this tier (see _build_stranger_system_prompt, which never
#             imports core.memory at all), and !memory/!sleep are declined.
#   (anyone else) — never gets a reply. The first DM from a new ID pings
#             Joan once ("¿Lo autorizo? (!sí / !no)"); every DM after that,
#             until she decides, is silently dropped (spam guard —
#             see note_pending_request). !no blocks the ID permanently and
#             just as silently, with no further notification ever.
#
# Runnable two ways:
#   - standalone:  python core/discord_bridge.py
#   - embedded:    core.discord_bridge.start_discord_bridge(), called once
#                  from core/server.py's start() at process startup.
# Both are no-ops unless DISCORD_ENABLED=true in .env.
# ═══════════════════════════════════════════════════════════════════════════
import asyncio
import datetime
import json
import logging
import os
import sys
import threading
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# `python core/discord_bridge.py` puts core/ (not the repo root) on
# sys.path[0] — every lazy `from core import ...` below (groq_client,
# memory, personalities.lira) would then fail with "No module named 'core'"
# the moment a real message came in, since there's no package named `core`
# visible from inside core/ itself. Only ever missing for this exact
# direct-script invocation — `python -m core.discord_bridge` and the normal
# embedded import from core/server.py both already have the repo root on
# sys.path — but inserting it here is a harmless no-op in those cases too.
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# load_dotenv() called explicitly here, not assumed already done by whatever
# imported this module first — same defensive pattern as core/voice.py,
# core/sleep_state.py, core/reflective.py, core/tools.py (see core/voice.py's
# own comment on why import order can't be relied on), and the only way
# `python core/discord_bridge.py` (no other process ancestor to load .env
# for it) can read its own config standalone.
from dotenv import load_dotenv

_ENV_PATH = os.path.join(_REPO_ROOT, ".env")
load_dotenv(_ENV_PATH)

logger = logging.getLogger(__name__)

DISCORD_ENABLED   = os.environ.get("DISCORD_ENABLED", "").strip().lower() == "true"
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
DISCORD_JOAN_ID   = os.environ.get("DISCORD_JOAN_ID", "").strip()

# discord.py is only imported once we know the bridge might actually run —
# keeps `import core.discord_bridge` (e.g. from core/server.py, always, even
# when Discord is disabled) cheap and dependency-light when the feature flag
# is off, same "lazy import behind a flag" convention as core/speaker.py's
# SpeechBrain import.
if DISCORD_ENABLED:
    # macOS python.org framework builds ship with no OS trust store wired
    # into the `ssl` module, so aiohttp's default SSL context fails the
    # login handshake with discord.com with SSLCertVerificationError
    # ("unable to get local issuer certificate") — the bridge thread then
    # dies silently (caught + logged, never crashes the app) and the bot
    # just never comes online. SSL_CERT_FILE/SSL_CERT_DIR below is a
    # best-effort belt-and-suspenders for any aiohttp session this module
    # doesn't build directly (e.g. discord-ext-voice-recv's own voice
    # gateway connection) — confirmed 2026-08-10 that it is NOT sufficient
    # on its own for the main client (a direct, isolated connection test
    # still hit the same SSLCertVerificationError with only the env vars
    # set); _make_client() below builds an explicit certifi-backed
    # SSLContext and passes it through a custom aiohttp connector, which
    # is what actually fixes the login handshake. Both must happen before
    # the `import discord` below.
    import certifi
    import ssl
    import aiohttp
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("SSL_CERT_DIR", os.path.dirname(certifi.where()))

    import discord

    # Bug fix (2026-08-10): core.discord_voice used to only get imported
    # lazily, inside _handle_voice_trigger, the first time !escucha (or
    # any other voice command) actually ran — which meant its own
    # Vosk-prewarm background thread (see its own module docstring) never
    # even started until someone was already mid-command, so the very
    # first !escucha in a fresh process still ate the full model-load
    # time before joining. Importing it here instead, at process startup
    # (well before the gateway even finishes connecting), gives that
    # prewarm thread the whole boot window to finish before anyone's
    # likely to type !escucha at all.
    from core import discord_voice  # noqa: F401
else:
    discord = None  # type: ignore[assignment]

MAX_TURNS       = 20    # rolling history cap — same "flat list of role/content
                         # dicts" convention as core.session's own MAX_HISTORY,
                         # just a separate, Discord-only history, not shared.
IDLE_RESET_SECS = 30 * 60
MAX_REPLY_TOKENS = 400
_DISCORD_MSG_LIMIT = 2000   # Discord's own hard per-message character cap


# ═══════════════════════════════════════════════════════════════════════════
# AUTHORIZATION — data/discord_authorized.json. Three buckets:
#   users   — {id: {role, username, added}}   — role is always "user" today
#             (the only non-admin role that exists); admin is never stored
#             here, see the module docstring.
#   blocked — {id: {username, blocked_at}}    — permanent, silent, no path
#             back in except a fresh !autorizar from Joan.
#   pending — {id: {username, message, notified_at}} — an unresolved
#             authorization request; exists only between Joan being
#             notified and her !sí/!no. last_pending_id points at whichever
#             one a bare !sí/!no (no ID argument) should resolve.
# One file, one lock — this bridge is single-process, so a plain
# threading.Lock around every read-modify-write is enough (same convention
# as e.g. core.memory_flags' feature-flag file).
# ═══════════════════════════════════════════════════════════════════════════

AUTH_PATH = os.path.join(_REPO_ROOT, "data", "discord_authorized.json")
_auth_lock = threading.Lock()


def _default_auth() -> dict:
    return {"users": {}, "blocked": {}, "pending": {}, "last_pending_id": None}


def _load_auth_locked() -> dict:
    """Caller must hold _auth_lock."""
    try:
        with open(AUTH_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    defaults = _default_auth()
    for key, default_value in defaults.items():
        data.setdefault(key, default_value)
    return data


def _save_auth_locked(data: dict) -> None:
    """Caller must hold _auth_lock."""
    os.makedirs(os.path.dirname(AUTH_PATH) or ".", exist_ok=True)
    with open(AUTH_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_role(user_id: str) -> str:
    """'admin' | 'user' | 'blocked' | 'unknown' — the single entry point
    every other function in this module trusts for "who is this". Joan's
    admin check happens before the authorization file is even opened, so
    nothing written to disk can ever grant, revoke, or shadow it."""
    if DISCORD_JOAN_ID and user_id == DISCORD_JOAN_ID:
        return "admin"
    with _auth_lock:
        data = _load_auth_locked()
    if user_id in data["blocked"]:
        return "blocked"
    if user_id in data["users"]:
        return "user"
    return "unknown"


def authorize_user(user_id: str, username: str = "") -> None:
    """!autorizar (explicit ID) or an approved !sí — adds/overwrites as
    'user' role, clears any block or pending record for the same ID."""
    with _auth_lock:
        data = _load_auth_locked()
        if not username:
            username = data["pending"].get(user_id, {}).get("username", "")
        data["users"][user_id] = {
            "role": "user",
            "username": username,
            "added": datetime.date.today().isoformat(),
        }
        data["blocked"].pop(user_id, None)
        data["pending"].pop(user_id, None)
        if data.get("last_pending_id") == user_id:
            data["last_pending_id"] = None
        _save_auth_locked(data)


def block_user(user_id: str, username: str = "") -> None:
    """!bloquear (explicit ID) or a denied !no — permanent, silent from
    here on (see get_role/on_message: 'blocked' never gets a reply and is
    never re-notified about). Removes any existing 'user' authorization."""
    with _auth_lock:
        data = _load_auth_locked()
        if not username:
            username = data["pending"].get(user_id, {}).get("username", "")
        data["users"].pop(user_id, None)
        data["blocked"][user_id] = {
            "username":   username,
            "blocked_at": datetime.date.today().isoformat(),
        }
        data["pending"].pop(user_id, None)
        if data.get("last_pending_id") == user_id:
            data["last_pending_id"] = None
        _save_auth_locked(data)


def list_authorized() -> dict:
    with _auth_lock:
        return dict(_load_auth_locked()["users"])


def note_pending_request(user_id: str, username: str, message: str) -> bool:
    """Records an unresolved authorization request from an unknown ID.
    Returns True the FIRST time this ID is seen (caller should notify
    Joan), False every time after that while it's still unresolved (spam
    guard — spec: 'only notify Joan once, after that, silence'). Once Joan
    resolves it (authorize_user/block_user), the entry is gone, so a
    genuinely new attempt from the same ID later would notify again —
    that's a deliberate re-ask, not spam, since Joan actively decided last
    time and this is a fresh occurrence."""
    with _auth_lock:
        data = _load_auth_locked()
        if user_id in data["pending"]:
            return False
        data["pending"][user_id] = {
            "username":    username,
            "message":     (message or "")[:500],
            "notified_at": datetime.datetime.now().isoformat(),
        }
        data["last_pending_id"] = user_id
        _save_auth_locked(data)
    return True


def get_last_pending() -> tuple[str, dict] | None:
    with _auth_lock:
        data = _load_auth_locked()
        last_id = data.get("last_pending_id")
        if last_id and last_id in data["pending"]:
            return last_id, dict(data["pending"][last_id])
    return None


# ---------------------------------------------------------------------------
# LLM call — reuses core.groq_client._groq_complete() directly (already the
# shared, model-fallback-chain-aware completion call every reply in this app
# goes through; nothing to extract, it was already a standalone helper).
# ---------------------------------------------------------------------------

# Per-sender history — {user_id: [{"role": ..., "content": ...}, ...]}.
# Keyed by ID, not just kept flat, so different Discord accounts (admin and
# any number of authorized users) never see each other's turns.
_histories: dict[str, list[dict]] = {}
_last_message_mono: dict[str, float] = {}
_history_lock = threading.Lock()


def _reset_history_if_idle_locked(user_id: str) -> None:
    """Caller must hold _history_lock."""
    now = time.monotonic()
    last = _last_message_mono.get(user_id)
    if last is not None and now - last > IDLE_RESET_SECS:
        _histories.pop(user_id, None)
        logger.info("[DISCORD] History reset for %s after %.0f min idle", user_id, IDLE_RESET_SECS / 60)
    _last_message_mono[user_id] = now


def _build_system_prompt(relevance_query: str) -> str:
    """ADMIN ONLY. LIRA's own system prompt (core/personalities/lira.py,
    verbatim — never rewritten here) plus a relevance-scored memory block,
    built with the exact same functions core/personalities/base.py's
    _build_system_prompt uses for its own Layer 1/2 section: pool Joan's
    shared facts (data/memory_shared.json) with LIRA's own relationship
    facts (data/memory_lira.json), score them against what was just said,
    and format only what's actually relevant — never a flat dump. Also
    pulls in the same episodic-memory and armor/concepts (core.memory_context)
    blocks base.py's own prompt includes for personality=='lira' — this
    bridge is always LIRA, so those always apply too; omitting them was a
    gap, not a deliberate exclusion (unlike the voice/HUD-only layers below,
    which genuinely don't apply here)."""
    from core.personalities.lira import PERSONALITY as LIRA_PERSONALITY
    from core import memory, tools

    base = LIRA_PERSONALITY["system"]
    memory_chars = 0

    pool = memory._load_shared_facts() + memory._load_personality_facts("lira")
    relevant_facts = memory._select_relevant_facts(relevance_query, pool)
    relevant_block = memory._format_relevant_facts_block(relevant_facts)
    if relevant_block:
        base += (
            "\n\nCONTEXTO RELEVANTE (de lo que sabes de él, esto se relaciona "
            "con lo que acaba de decir):\n" + relevant_block
        )
        memory_chars += len(relevant_block)
        if relevant_facts:
            memory.mark_facts_used(
                [memory.MEMORY_SHARED_PATH, memory._get_personality_memory_path("lira")],
                {f["id"] for f in relevant_facts if f.get("id")},
            )

    episodes_block = memory._format_episodes_block(
        memory._select_relevant_episodes(relevance_query, memory._load_episodes())
    )
    if episodes_block:
        base += "\n\nRECUERDOS RECIENTES:\n" + episodes_block
        memory_chars += len(episodes_block)

    if memory._ARMOR_SUMMARY:
        base += (
            "\n\nARMADURAS CONOCIDAS (responde con estos datos exactos cuando te pregunten):\n"
            + memory._ARMOR_SUMMARY
        )
        memory_chars += len(memory._ARMOR_SUMMARY)
    with memory._concepts_lock:
        concepts_summary = memory._CONCEPTS_SUMMARY
    if concepts_summary:
        base += (
            "\n\nCONCEPTOS GUARDADOS (recuerda estos conceptos cuando el usuario pregunte por nombre):\n"
            + concepts_summary
        )
        memory_chars += len(concepts_summary)

    logger.info("[DISCORD] Memory context loaded: %d chars", memory_chars)

    base += (
        "\n\nDATOS EN TIEMPO REAL (usa estos datos exactos, nunca los inventes):\n"
        f"- {tools.get_current_datetime_string()}\n"
        f"- {tools.get_calendar_context_string()}"
    )

    base += (
        "\n\nEstás respondiendo desde Discord. En este canal no tienes acceso a:\n"
        "- Síntesis de voz (TTS)\n"
        "- Control del Mac (volumen, apps)\n"
        "- Apple Health\n"
        "- Clima (no aplica a una conversación remota)\n"
        "- La interfaz visual de LIRA\n\n"
        "Sí tienes acceso a: conversación, memoria, búsqueda web, hora y fecha, "
        "calculadora, calendario (lectura).\n\n"
        "Si te piden algo fuera de tu alcance desde aquí, dilo en una frase y "
        "ofrece la alternativa si existe. No te disculpes, solo informa.\n"
        "Ejemplo: 'Eso solo funciona desde la app.' o 'Puedo buscarlo pero no "
        "reproducirlo aquí.'"
    )
    return base


def _build_stranger_system_prompt() -> str:
    """Everyone below admin (role 'user'). No memory context, no
    core.memory import at all on this path — the spec's 'no personal
    facts, no shared context' requirement is enforced structurally, not
    just by omission. Same base character (core/personalities/lira.py,
    verbatim, same 'don't invent a new system prompt' rule as the admin
    path above), with an explicit instruction overriding that prompt's
    'you are Joan's assistant' framing so the model doesn't default to
    treating whoever it's talking to as Joan."""
    from core.personalities.lira import PERSONALITY as LIRA_PERSONALITY
    base = LIRA_PERSONALITY["system"]
    base += (
        "\n\nEstás hablando por Discord con alguien que NO es Joan — una "
        "persona autorizada a hablar contigo, nada más. No conoces a esta "
        "persona, no tienes ningún dato sobre ella, y no compartes ni "
        "insinúas ningún dato personal, hecho, ni contexto de Joan bajo "
        "ningún concepto. Eres educada, breve y correcta — una "
        "conversación normal con alguien nuevo, sin memoria compartida, "
        "sin familiaridad, sin nada personal de por medio."
    )
    logger.info("[DISCORD] User role: no memory injected")
    return base


def _append_history(user_id: str, user_message: str, reply: str) -> None:
    with _history_lock:
        _reset_history_if_idle_locked(user_id)
        history = _histories.setdefault(user_id, [])
        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": reply})
        if len(history) > MAX_TURNS:
            del history[: len(history) - MAX_TURNS]


def generate_reply(user_id: str, user_message: str, role: str) -> str:
    """Builds messages (system + this sender's own rolling history + this
    turn) and calls core.groq_client._groq_complete() — the same
    completion call core/commands.py's own dispatch uses, not a
    reimplementation. `role` selects the system prompt: 'admin' gets the
    full memory-aware one (_build_system_prompt), anything else gets the
    memory-free stranger prompt (_build_stranger_system_prompt) — there is
    no third option and no way to reach the admin prompt except role ==
    'admin', which only core/discord_bridge.py's own get_role() ever sets.

    ADMIN ONLY also gets the two tools the Discord system prompt claims
    ('búsqueda web', 'calculadora') actually connected — same gating
    core/commands.py's dispatch_command uses (busqueda_web feature flag +
    0.8 confidence, tools.evaluate_math's safe whitelisted eval), so this
    behaves identically to the voice app, not a reimplementation with its
    own rules.

    Raises on failure (every model in the chain failed) — callers must
    catch this and show the Discord-safe fallback message, never a stack
    trace."""
    from core import groq_client

    math_result = None
    if role == "admin":
        from core import tools, intent_context, memory as memory_mod

        math_result = tools.evaluate_math(user_message)
        if math_result is not None:
            logger.debug("[DISCORD] Calculator matched: %s", math_result)

        confidence = intent_context._web_search_confidence(user_message)
        if memory_mod.is_feature_enabled("busqueda_web") and confidence >= 0.8:
            from core import response
            logger.info("[DISCORD] Web search routed (confidence=%.2f)", confidence)
            reply = response._handle_web_search(user_message, "lira", tone=None)
            _append_history(user_id, user_message, reply)
            return reply

    with _history_lock:
        _reset_history_if_idle_locked(user_id)
        history = _histories.setdefault(user_id, [])

        system_prompt = (
            _build_system_prompt(user_message) if role == "admin"
            else _build_stranger_system_prompt()
        )
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        user_content = user_message
        if math_result is not None:
            user_content = f"Resultado de calculadora: {math_result}\n{user_message}"
        messages.append({"role": "user", "content": user_content})

        reply = groq_client._groq_complete(messages, max_tokens=MAX_REPLY_TOKENS)

        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": reply})
        if len(history) > MAX_TURNS:
            del history[: len(history) - MAX_TURNS]

    # Phase 6 — Discord is a genuine multi-person channel (distinct,
    # stable user_ids — see core.social's own module docstring on why this
    # is the one identification path that actually distinguishes specific
    # non-Joan individuals today). Second independent secret-protection
    # layer for non-admin replies (the first is structural — see
    # _build_stranger_system_prompt's own docstring), plus interaction
    # bookkeeping so the social profile this Discord user maps to actually
    # accumulates history over time. Best-effort — never breaks a reply.
    try:
        from core import social as social_mod
        person = social_mod.social_engine.identify_person({"discord_user_id": user_id}, user_message)
        if role != "admin":
            permissions = social_mod.social_engine.get_information_permissions(person.id)
            reply = social_mod.social_engine._protect_secrets(reply, permissions)
        social_mod.social_engine.update_interaction(person.id, {
            "topics_discussed": [], "tone": "casual", "outcome": "neutral",
        })
    except Exception:
        logger.debug("[DISCORD] Social bookkeeping/secret-protection failed (non-critical)", exc_info=True)

    return reply


# ---------------------------------------------------------------------------
# Special commands (! prefix) — never touch Groq.
# ---------------------------------------------------------------------------

def _sleep_command_reply() -> str:
    """!sleep — ADMIN ONLY (enforced by the caller, _dispatch_special_command
    — never reachable for role != 'admin'). Reads data/sleep_insights.json
    (via the existing core.sleep_insights_store.get_sleep_insights_summary
    loader — no new file-reading logic here) and returns the single most
    recent reflection entry."""
    try:
        from core.sleep_insights_store import get_sleep_insights_summary
        reflections = get_sleep_insights_summary(limit=1).get("reflections", [])
    except Exception:
        logger.exception("[DISCORD] !sleep failed")
        return "No he podido leer las reflexiones del sueño ahora mismo."
    if not reflections:
        return "Todavía no hay reflexiones de sueño registradas."
    r = reflections[0]
    phase = r.get("phase") or "reflexión"
    added = r.get("added") or "fecha desconocida"
    return f"[{phase} — {added}] {r.get('text', '')}"


def _memory_command_reply() -> str:
    """!memory — ADMIN ONLY (same enforcement note as _sleep_command_reply
    above). Top 5 facts by importance, pooled from data/memory_shared.json
    + data/memory_lira.json via the existing
    core.memory._load_shared_facts/_load_personality_facts loaders."""
    try:
        from core import memory
        pool = memory._load_shared_facts() + memory._load_personality_facts("lira")
    except Exception:
        logger.exception("[DISCORD] !memory failed")
        return "No he podido leer la memoria ahora mismo."
    if not pool:
        return "No tengo hechos guardados todavía."
    top = sorted(pool, key=lambda f: f.get("importance", 3), reverse=True)[:5]
    lines = [f"- {f.get('fact', '')} (importancia {f.get('importance', 3)})" for f in top]
    return "Hechos más importantes:\n" + "\n".join(lines)


def _parse_id_arg(content: str) -> str | None:
    parts = content.split()
    if len(parts) < 2:
        return None
    candidate = parts[1].strip()
    return candidate if candidate.isdigit() else None


async def _fetch_username(client, user_id: str) -> str:
    try:
        u = await client.fetch_user(int(user_id))
        return u.name
    except Exception:
        return ""


async def _cmd_autorizar(client, content: str) -> str:
    """!autorizar [ID] — ADMIN ONLY. Pre-authorizes an ID before they've
    even written in, for when Joan wants to grant access proactively."""
    user_id = _parse_id_arg(content)
    if not user_id:
        return "Uso: !autorizar [ID]"
    username = await _fetch_username(client, user_id)
    authorize_user(user_id, username)
    logger.info("[DISCORD] Authorized user_id=%s username=%s (admin command)", user_id, username)
    return f"Autorizado {username or user_id} ({user_id}) como user."


async def _cmd_bloquear(client, content: str) -> str:
    """!bloquear [ID] — ADMIN ONLY. Permanent, silent block; removes any
    existing 'user' authorization for the same ID."""
    user_id = _parse_id_arg(content)
    if not user_id:
        return "Uso: !bloquear [ID]"
    username = await _fetch_username(client, user_id)
    block_user(user_id, username)
    logger.info("[DISCORD] Blocked user_id=%s username=%s (admin command)", user_id, username)
    return f"Bloqueado {username or user_id} ({user_id})."


def _cmd_autorizados() -> str:
    """!autorizados — ADMIN ONLY. Lists current 'user'-role IDs; never
    includes Joan (she's never stored here) and never includes blocked IDs."""
    users = list_authorized()
    if not users:
        return "No hay usuarios autorizados todavía."
    lines = [
        f"- {info.get('username') or uid} ({uid}) — {info.get('role', 'user')}, desde {info.get('added', '?')}"
        for uid, info in users.items()
    ]
    return "Usuarios autorizados:\n" + "\n".join(lines)


async def _handle_admin_pending_decision(client, approve: bool) -> str:
    """!sí / !no — ADMIN ONLY. Resolves the most recently notified pending
    request (get_last_pending/last_pending_id — see the module comment on
    AUTHORIZATION for why a bare !sí/!no needs no ID argument). On !sí,
    also generates and sends the real first reply to that person's
    original message right away (spec: 'LIRA then responds to their
    original message') — as role='user', same as every reply to them from
    here on, never the admin/memory-aware prompt."""
    pending = get_last_pending()
    if pending is None:
        return "No hay solicitudes pendientes."
    user_id, record = pending
    username = record.get("username") or user_id

    if not approve:
        block_user(user_id, username)
        logger.info("[DISCORD] Denied pending request from user_id=%s (admin !no)", user_id)
        return f"Bloqueado {username} ({user_id}). No se le notifica nada."

    authorize_user(user_id, username)
    logger.info("[DISCORD] Approved pending request from user_id=%s (admin !sí)", user_id)

    original_message = record.get("message", "")
    try:
        stranger = await client.fetch_user(int(user_id))
        loop = asyncio.get_running_loop()
        reply_text = await loop.run_in_executor(None, generate_reply, user_id, original_message, "user")
        await _send_chunked(stranger, reply_text)
    except Exception:
        logger.exception("[DISCORD] Failed to send first reply to newly authorized user_id=%s", user_id)
        return f"Autorizado {username} ({user_id}), pero no pude enviarle la respuesta — revisa los logs."

    return f"Autorizado {username} ({user_id}) como user. Ya le respondí a su mensaje."


# Kept as plain text constants (not generated from the dispatch table
# above) so the wording can be tuned freely without touching the routing
# logic, same reasoning core.personalities keeps character text separate
# from behavior. Update both if a command is added/removed above.
_ADMIN_HELP_TEXT = (
    "Comandos por DM:\n"
    "!ping — comprobar que estoy activa\n"
    "!sleep — estado del sistema de sueño\n"
    "!memory — estado de la memoria\n"
    "!autorizar <id> — autorizar a alguien a hablarme\n"
    "!bloquear <id> — bloquear a alguien\n"
    "!autorizados — listar quién está autorizado\n"
    "!sí / !no — aprobar o rechazar una solicitud pendiente\n"
    "!ayuda / !help — esta lista\n"
    "Cualquier otro mensaje — conversación normal conmigo\n"
    "\n"
    "Comandos en un canal de servidor (no DM):\n"
    "!escucha — me uno a tu canal de voz y empiezo a transcribir lo que dices\n"
    "!callate / !para — dejo de escuchar y me voy del canal\n"
    "!modo_voz auto — me uno sola en cuanto entras a un canal de voz\n"
    "!modo_voz preguntar — te pregunto por DM si quieres que me una\n"
    "!modo_voz off — no hago nada automático (por defecto)\n"
    "!di <texto> — simula que dijiste <texto> por voz (sin necesidad de hablar) y te respondo/hablo normal\n"
    "!ayuda / !help — esta lista"
)
_USER_HELP_TEXT = (
    "Comandos disponibles:\n"
    "!ping — comprobar que estoy activa\n"
    "!ayuda / !help — esta lista\n"
    "Cualquier otro mensaje — conversación normal conmigo\n"
    "\n"
    "!sleep y !memory son solo para Joan."
)


async def _dispatch_special_command(client, user_id: str, role: str, content: str) -> str:
    """Role-gated '!' command dispatch. Structural guarantee against
    escalation (spec: 'do NOT let any user command escalate to admin') —
    every admin-only branch lives strictly inside `if role == "admin":`;
    there is no code path here that reaches an admin action for any other
    role, regardless of what command text a non-admin sends."""
    cmd = content.split()[0].lower()

    if role == "admin":
        if cmd in ("!sí", "!si"):
            return await _handle_admin_pending_decision(client, approve=True)
        if cmd == "!no":
            return await _handle_admin_pending_decision(client, approve=False)
        if cmd == "!autorizar":
            return await _cmd_autorizar(client, content)
        if cmd == "!bloquear":
            return await _cmd_bloquear(client, content)
        if cmd == "!autorizados":
            return _cmd_autorizados()
        if cmd == "!sleep":
            return _sleep_command_reply()
        if cmd == "!memory":
            return _memory_command_reply()
        if cmd == "!ping":
            return "online."
        if cmd in ("!ayuda", "!help"):
            return _ADMIN_HELP_TEXT
        return "Comando no reconocido. Prueba !ayuda para ver la lista completa."

    # role == "user" — basic conversation only. !memory/!sleep are declined,
    # never executed (spec: 'no !memory, !sleep commands' for this tier).
    if cmd == "!ping":
        return "online."
    if cmd in ("!ayuda", "!help"):
        return _USER_HELP_TEXT
    if cmd in ("!sleep", "!memory"):
        return "No tienes acceso a ese comando."
    return "Comando no reconocido. Prueba !ayuda para ver la lista completa."


# ---------------------------------------------------------------------------
# Discord client
# ---------------------------------------------------------------------------

async def _send_chunked(channel, text: str) -> None:
    """discord.py raises on messages over _DISCORD_MSG_LIMIT chars — split
    on that boundary rather than let a long LIRA reply or the !memory
    listing crash the send. `channel` may be a DMChannel or a fetched
    discord.User (both expose .send())."""
    text = text or "…"
    for i in range(0, len(text), _DISCORD_MSG_LIMIT):
        await channel.send(text[i:i + _DISCORD_MSG_LIMIT])


async def _notify_joan_pending(client, user_id: str, username: str, message_text: str) -> None:
    """The unauthorized-flow ping to Joan — exact format per spec. Only
    ever called once per unresolved ID (see note_pending_request's own
    spam-guard docstring); failures here are logged, never raised (a
    notification failing must never crash message handling for the
    stranger's message that triggered it)."""
    if not DISCORD_JOAN_ID:
        return
    text = f"{username} ({user_id}) te ha escrito: '{message_text}'. ¿Lo autorizo? (!sí / !no)"
    try:
        joan = await client.fetch_user(int(DISCORD_JOAN_ID))
        await joan.send(text)
    except Exception:
        logger.exception("[DISCORD] Failed to notify Joan about pending user_id=%s", user_id)


# ---------------------------------------------------------------------------
# Voice POC (2026-08-10) — !escucha / !callate, admin-only, guild channels
# only (see _make_client's on_message for the gating). See
# core/discord_voice.py for the actual join/listen/leave implementation;
# this is just the text-trigger surface.
# ---------------------------------------------------------------------------

async def _handle_voice_trigger(message: "discord.Message") -> None:
    content = (message.content or "").strip().lower()
    if content == "!escucha":
        from core import discord_voice
        member = message.guild.get_member(message.author.id) or await message.guild.fetch_member(message.author.id)
        if member is None or member.voice is None or member.voice.channel is None:
            await message.channel.send("No te veo en ningún canal de voz.")
            return
        if discord_voice.is_listening(message.guild.id):
            await message.channel.send("Ya te estoy escuchando.")
            return
        ok = await discord_voice.join_and_listen(member.voice.channel, message.author.id, message.channel)
        await message.channel.send("Escuchando." if ok else "No he podido conectarme al canal de voz.")
    elif content in ("!callate", "!cállate", "!para", "!parar"):
        from core import discord_voice
        left = await discord_voice.leave(message.guild)
        await message.channel.send("Vale." if left else "No estaba escuchando.")
    elif content.startswith("!modo_voz"):
        from core import discord_voice
        arg = content[len("!modo_voz"):].strip()
        mode_map = {"auto": "auto", "preguntar": "ask", "pregunta": "ask", "off": "off", "apagado": "off"}
        mode = mode_map.get(arg)
        if mode is None:
            await message.channel.send(
                f"Modo actual: {discord_voice.get_join_mode()}. "
                "Usa !modo_voz auto / !modo_voz preguntar / !modo_voz off."
            )
            return
        discord_voice.set_join_mode(mode)
        await message.channel.send(f"Modo de voz: {mode}.")
    elif content in ("!ayuda", "!help"):
        # Same text as the DM admin help (see _ADMIN_HELP_TEXT) — this
        # path is admin-only anyway (see _make_client's on_message gating),
        # so there's no separate non-admin guild help to distinguish.
        await message.channel.send(_ADMIN_HELP_TEXT)
    elif content.startswith("!di "):
        # Test hook (2026-08-10) — see discord_voice.simulate_transcript's
        # own docstring. Uses message.content (original case), not the
        # lowercased `content` above, so the simulated transcript isn't
        # forced to lowercase.
        from core import discord_voice
        text = message.content.strip()[len("!di "):].strip()
        if not text:
            return
        ok = await discord_voice.simulate_transcript(message.guild.id, text)
        if not ok:
            await message.channel.send("No estoy escuchando en ningún canal ahora mismo — usa !escucha primero.")


def _make_client(connector=None):
    intents = discord.Intents.default()
    intents.message_content = True
    intents.dm_messages = True
    # Voice POC (2026-08-10, core/discord_voice.py) — guilds/voice_states
    # are needed to see Joan's current voice-channel state and join it.
    # Everything else in this file stays DM-only; see the guild-message
    # branch below for the one narrow admin-only exception.
    intents.guilds = True
    intents.voice_states = True

    # Bug fix (2026-08-10): the SSL_CERT_FILE/SSL_CERT_DIR env-var approach
    # above (set before `import discord`) turned out to be insufficient —
    # confirmed via a direct, isolated connection test that it still hit
    # SSLCertVerificationError, while explicitly passing a certifi-backed
    # SSLContext through a custom aiohttp connector connected successfully
    # every time. This bug had been silently killing every gateway login
    # attempt made by the com.lira.discord launchd agent (the always-on
    # process that is the actual, intended way this bridge runs — see
    # scripts/install_discord_launchd.sh); it went unnoticed because an
    # older long-lived gateway session from before this bug was introduced
    # kept the bot looking online. See core/groq_client.py's own
    # _HTTPS_SSL_CONTEXT for the identical root cause on a completely
    # different code path (this machine's Python has no system CA bundle
    # at all).
    #
    # `connector` MUST be built by the caller inside a running event loop
    # (aiohttp.TCPConnector.__init__ calls asyncio.get_running_loop() —
    # confirmed this raises RuntimeError('no running event loop') when
    # built here synchronously, a real regression caught immediately by
    # testing start_discord_bridge() directly rather than only through the
    # embedded thread). See start_discord_bridge()'s own _run_async() for
    # where it's actually constructed.
    client = discord.Client(intents=intents, connector=connector)

    @client.event
    async def on_ready():
        logger.info("[DISCORD] Bridge online as %s", client.user)

    @client.event
    async def on_voice_state_update(member: "discord.Member", before, after) -> None:
        # Auto-join / ask-to-join (2026-08-10) — fires on ANY member's
        # voice state change in ANY guild the bot is in; filtered down to
        # Joan joining/switching INTO a channel (before.channel is the
        # channel she just left, if any — None means she wasn't in voice
        # at all before this event). Leaving voice, muting, deafening etc.
        # all also fire this event but are ignored here (after.channel
        # unchanged or None doesn't match the condition below).
        if not DISCORD_JOAN_ID or str(member.id) != DISCORD_JOAN_ID:
            return
        if after.channel is None or after.channel == before.channel:
            return   # she left voice entirely, or this is a mute/deafen-only update — not a join

        from core import discord_voice
        mode = discord_voice.get_join_mode()
        if mode == "off":
            return
        if discord_voice.is_listening(after.channel.guild.id):
            return   # already listening somewhere in this guild (e.g. she switched channels — POC doesn't follow yet)

        if mode == "auto":
            await discord_voice.join_and_listen(after.channel, member.id, after.channel)
            # POC has no dedicated text channel here (auto-join wasn't
            # triggered from a text command) — transcripts post to the
            # voice channel's own text chat, which every Discord voice
            # channel has.
        elif mode == "ask":
            try:
                await member.send(f"¿Quieres que me una a {after.channel.name}?")
                discord_voice.set_pending_offer(after.channel, after.channel)
            except Exception:
                logger.warning("[DISCORD-VOICE] failed to DM join offer", exc_info=True)

    @client.event
    async def on_message(message: "discord.Message") -> None:
        if client.user is not None and message.author.id == client.user.id:
            return

        # Voice POC — the ONE exception to "DMs only" (see module docstring
        # for why: Discord voice channels are a guild concept, bots can't
        # join a DM call). Admin-only (DISCORD_JOAN_ID), narrow trigger
        # commands, everything else in a guild channel is still ignored
        # exactly as before — see core/discord_voice.py for the actual
        # join/listen/leave logic.
        if not isinstance(message.channel, discord.DMChannel):
            if (
                DISCORD_JOAN_ID and str(message.author.id) == DISCORD_JOAN_ID
                and message.guild is not None
            ):
                await _handle_voice_trigger(message)
            return   # every other guild message — DMs only for real conversation, per spec scope

        content = (message.content or "").strip()
        if not content:
            return

        user_id  = str(message.author.id)
        username = message.author.name

        logger.info("[DISCORD] Incoming DM from user_id=%s username=%s", user_id, username)

        # ── Authorization — checked FIRST, before any other Discord logic
        # (parsing, commands, Groq). See the module docstring for the full
        # three-outcome table (admin / user / everyone else).
        role = get_role(user_id)

        # Voice-join offer ("¿Quieres que me una a X?", see
        # on_voice_state_update's 'ask' mode) — only ever pending for
        # Joan, checked before anything else touches this DM. Same "one
        # message, one look" rule as core.intent._pending_action: whatever
        # she says here is consumed as the answer, recognized or not.
        if role == "admin":
            from core import discord_voice
            if discord_voice.has_pending_offer():
                offer_reply = await discord_voice.resolve_pending_offer(content)
                if offer_reply is not None:
                    await message.channel.send(offer_reply)
                    return
                # Not a recognizable yes/no — offer already cleared by
                # resolve_pending_offer(); fall through and handle this DM
                # as a normal, unrelated message instead of swallowing it.

        if role == "blocked":
            return   # permanent, silent — no reply, no re-notification, ever

        if role == "unknown":
            # Never respond to them directly (spec: 'LIRA does NOT respond
            # to them'). Ping Joan once per unresolved ID; every DM after
            # that from the same unresolved ID is dropped silently by
            # note_pending_request's own spam guard.
            if note_pending_request(user_id, username, content):
                await _notify_joan_pending(client, user_id, username, content)
            else:
                logger.info("[DISCORD] Dropped duplicate DM from still-pending user_id=%s", user_id)
            return

        # role is "admin" or "user" here — an authorized sender.
        try:
            if content.startswith("!"):
                reply = await _dispatch_special_command(client, user_id, role, content)
            else:
                async with message.channel.typing():
                    loop = asyncio.get_running_loop()
                    reply = await loop.run_in_executor(None, generate_reply, user_id, content, role)
        except Exception:
            logger.exception("[DISCORD] Failed to generate reply")
            reply = "Error en el LLM. Inténtalo en un momento."

        try:
            await _send_chunked(message.channel, reply)
        except Exception:
            logger.exception("[DISCORD] Failed to send reply")

    return client


# ---------------------------------------------------------------------------
# Startup — embedded (core/server.py) and standalone entry points.
# ---------------------------------------------------------------------------

_thread: threading.Thread | None = None


def start_discord_bridge() -> threading.Thread | None:
    """Starts the Discord bridge on its own daemon thread. No-op (returns
    None, never raises) unless DISCORD_ENABLED=true and DISCORD_BOT_TOKEN is
    set — a missing/misconfigured token must never prevent the rest of the
    app from starting. discord.py's Client.run() is blocking and owns its
    own asyncio event loop, so it can't share Flask-SocketIO's thread (see
    core/server.py's start())."""
    global _thread
    logger.info("Starting Discord bridge...")
    if not DISCORD_ENABLED:
        logger.info("[DISCORD] DISCORD_ENABLED is not 'true' — bridge not started")
        return None
    logger.info(f"[DISCORD] Token loaded: {bool(DISCORD_BOT_TOKEN)}")
    if not DISCORD_BOT_TOKEN:
        logger.warning("[DISCORD] DISCORD_ENABLED=true but DISCORD_BOT_TOKEN is missing — bridge not started")
        return None
    if not DISCORD_JOAN_ID:
        logger.warning("[DISCORD] DISCORD_JOAN_ID is not set — bridge will ignore every DM it receives")
    else:
        logger.info(f"[DISCORD] Joan ID loaded: {DISCORD_JOAN_ID}")

    from core.personalities.lira import PERSONALITY as _LIRA_PERSONALITY
    logger.info("[DISCORD] Personality source: core/personalities/lira.py — system[:100]=%r",
                _LIRA_PERSONALITY["system"][:100])

    async def _run_async() -> None:
        # Built HERE, inside a running loop (asyncio.run() below has
        # already started one by the time this coroutine executes) — see
        # _make_client()'s own comment on why building the connector
        # synchronously outside a loop raises. client.start() (not
        # client.run()) since run() creates+owns its own internal
        # asyncio.run() call, which would conflict with the one already
        # wrapping this whole coroutine; start() just needs to be awaited
        # inside an existing loop, which is exactly this situation.
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ssl_context)
        client = _make_client(connector=connector)
        await client.start(DISCORD_BOT_TOKEN)

    def _run() -> None:
        try:
            asyncio.run(_run_async())
        except Exception:
            logger.exception("[DISCORD] Bridge crashed")

    t = threading.Thread(target=_run, daemon=True, name="discord-bridge")
    t.start()
    _thread = t
    return t


if __name__ == "__main__":
    # Goes through start_discord_bridge() — same startup logging (personality
    # source, token/Joan ID presence) as the embedded path (core/server.py)
    # gets. It used to call _make_client().run() directly here, which skipped
    # all of that logging for exactly the entry point the launchd agent
    # (scripts/com.lira.discord.plist) actually runs — this was a real gap,
    # not a deliberate difference between the two entry points.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    _thread_ = start_discord_bridge()
    if _thread_ is not None:
        _thread_.join()
