# ═══════════════════════════════════════════════════════════════════════════
# DISCORD VOICE — proof-of-concept for real-time voice commands over a
# Discord voice channel, mirroring core/listener.py's mic pipeline but fed
# from Discord audio instead of a local microphone. Separate module from
# core/discord_bridge.py's existing DM-only text bridge — voice channels
# are a GUILD concept in Discord's API (bots cannot join 'DM calls'), so
# this needs the bot invited to an actual server with a voice channel,
# unlike the text bridge.
#
# Scope (2026-08-10, extended 2026-08-10): join a voice channel, capture
# Joan's audio only (filtered by Discord user ID — DISCORD_JOAN_ID, same
# admin-only assumption discord_bridge.py already makes), transcribe via
# a dedicated small Spanish Vosk model (own copy, independent of
# core/listener.py's much bigger, higher-accuracy one — see
# _get_vosk_model_es's own comment on why), route the transcript through
# the same Groq-backed reply generation the DM bridge uses
# (core.discord_bridge.generate_reply), post both transcript and reply as
# text, and speak the reply back into the channel via macOS `say`
# (core.voice.synthesize_pcm48_say + speak() below) — not a
# reimplementation of core/commands.py's local dispatch, since that's built
# around local mic/speaker hardware state this process doesn't have here.
#
# Requires the discord-ext-voice-recv extension (not part of core
# discord.py — Discord's bot API doesn't officially support receiving
# voice audio at all; this hooks into the voice gateway after
# decryption/decoding via a community-maintained extension). Confirmed
# installed and compatible with discord.py 2.7.1 in this venv.
# ═══════════════════════════════════════════════════════════════════════════
import asyncio
import io
import json
import logging
import os
import re
import threading
import time

import numpy as np
from scipy.signal import resample_poly

import discord
from discord.ext import voice_recv
import vosk

logger = logging.getLogger(__name__)

# Discord voice (send AND receive) needs the native libopus library loaded
# — discord.py doesn't always auto-load it before the first connect(), so
# this loads it explicitly at import time rather than hoping auto-detection
# works. Confirmed available on this machine (2026-08-10). Best-effort: if
# it's genuinely missing, join_and_listen()'s own connect() call will fail
# loudly and return False rather than this import crashing the whole
# Discord bridge over a POC feature.
if not discord.opus.is_loaded():
    try:
        discord.opus._load_default()
    except Exception:
        logger.warning("[DISCORD-VOICE] libopus failed to load — voice features will not work", exc_info=True)

# Bug fix (2026-08-10): Discord's DAVE end-to-end voice encryption — a
# second, MLS-based encryption layer on top of the transport AEAD
# decryption discord.py already handles — is now mandatory to even open a
# VoiceClient in discord.py 2.7.1 (VoiceClient.__init__ raises if the
# `davey` library isn't installed) and gets negotiated automatically
# whenever it is. discord-ext-voice-recv (an alpha extension, no DAVE
# awareness at all) tries to Opus-decode audio that's still DAVE-encrypted
# and fails on literally every packet: `discord.opus.OpusError: corrupted
# stream`, confirmed via a real join+speak test — Joan joined the channel
# fine but every word she said errored out before ever reaching Vosk.
#
# First fix attempt (REVERTED): patching VoiceConnectionState's
# max_dave_protocol_version property to always report 0, so the client
# would claim no E2EE support during the voice IDENTIFY handshake. This
# broke the connection entirely — confirmed via a real join test, the
# voice websocket closed with code 4017 during handshake and retried
# until timing out, which is why the bot appeared to "join and then
# randomly leave" with no reply. This guild's voice session apparently
# requires DAVE; a client claiming version 0 gets rejected outright, it
# doesn't just fall back to no-E2EE.
#
# Actual fix: let DAVE negotiate normally, and decrypt each user's audio
# frame ourselves before it reaches voice-recv's Opus decoder, using the
# same davey.DaveSession object discord.py's own outgoing path already
# uses for encryption (VoiceClient.send_audio_packet ->
# dave_session.encrypt_opus). davey.DaveSession.decrypt(user_id,
# media_type, packet) is the inverse call, just never wired up by
# voice-recv. Patches PacketDecoder._decode_packet (per-SSRC/per-user
# decoder, so `self` already resolves which Discord user this packet
# belongs to) to decrypt via the active session before handing bytes to
# the Opus decoder. No-ops safely (passes ciphertext straight through,
# same as before) whenever there's no active DAVE session — e.g. a guild
# that doesn't use E2EE at all — so this doesn't regress that case.
import davey as _davey
from discord.ext.voice_recv import opus as _voice_recv_opus

_orig_decode_packet = _voice_recv_opus.PacketDecoder._decode_packet


def _dave_aware_decode_packet(self, packet):
    assert self._decoder is not None

    def _dave_decrypt(data: bytes) -> bytes:
        try:
            vc = self.sink.voice_client
            dave_session = getattr(vc._connection, "dave_session", None)
            if dave_session is None or not dave_session.ready:
                return data
            member = self._get_cached_member()
            if member is None:
                self._cached_id = vc._get_id_from_ssrc(self.ssrc)
                member = self._get_cached_member()
            user_id = member.id if member is not None else self._cached_id
            if user_id is None:
                return data
            return dave_session.decrypt(user_id, _davey.MediaType.audio, data)
        except ValueError as e:
            if "UnencryptedWhenPassthroughDisabled" in str(e):
                # Confirmed via a real live test (2026-08-10): some packets
                # arrive genuinely unencrypted — Discord's own transitional
                # passthrough behavior, not a bug on our end — and
                # davey.decrypt() refuses to just return those instead of
                # raising. Returning the raw AEAD-decrypted payload as-is
                # is the best available recovery here; it isn't always
                # decodable Opus even so (confirmed: a passthrough packet
                # can still fail Opus decode right afterward) — that's
                # fine, _decode_or_conceal below handles that uniformly
                # rather than this needing to be a perfect answer.
                return data
            logger.debug("[DISCORD-VOICE] DAVE decrypt failed for a packet (%r) — passing ciphertext through", e)
            return data
        except Exception as e:
            logger.debug("[DISCORD-VOICE] DAVE decrypt failed for a packet (%r) — passing ciphertext through", e)
            return data

    def _decode_or_conceal(data: bytes) -> bytes:
        # Bug fix (2026-08-10): a raw discord.opus.OpusError raised here
        # used to propagate all the way up through voice_recv's
        # PacketRouter.run() — which catches it, yes, but only to log it
        # and then call self.reader.voice_client.stop_listening(), ending
        # the ENTIRE listening session on ONE bad packet (see router.py's
        # own run()/finally). Confirmed via a real live test: the very
        # first Opus decode failure after a !escucha join silently killed
        # the whole session — every word Joan said afterward, for as long
        # as she kept talking, was simply never processed, with no error
        # visible anywhere except this log. _dave_decrypt above already
        # covers the one DAVE-specific failure mode found so far
        # (transitional plaintext packets); this is the general safety
        # net for anything else that makes a given 20ms frame
        # undecodable — Opus's own packet-loss-concealment (decode(None))
        # is the same recovery voice_recv's own code already uses for a
        # genuinely lost packet a few lines below, so a single bad frame
        # now degrades to one barely-perceptible gap instead of ending
        # the whole session.
        try:
            return self._decoder.decode(data, fec=False)
        except Exception as e:
            logger.debug("[DISCORD-VOICE] Opus decode failed for one packet (%r) — concealing instead of ending the session", e)
            return self._decoder.decode(None, fec=False)

    if packet:
        pcm = _decode_or_conceal(_dave_decrypt(packet.decrypted_data))
        return packet, pcm

    next_packet = self._buffer.peek_next()
    if next_packet is not None:
        nextdata = _dave_decrypt(next_packet.decrypted_data)
        _voice_recv_opus.log.debug("Generating fec packet: fake=%s, fec=%s", packet.sequence, next_packet.sequence)
        try:
            pcm = self._decoder.decode(nextdata, fec=True)
        except Exception as e:
            logger.debug("[DISCORD-VOICE] Opus FEC decode failed for one packet (%r) — concealing instead of ending the session", e)
            pcm = self._decoder.decode(None, fec=False)
    else:
        pcm = self._decoder.decode(None, fec=False)

    return packet, pcm


_voice_recv_opus.PacketDecoder._decode_packet = _dave_aware_decode_packet

DISCORD_VOICE_SAMPLERATE = 48000   # Discord's own PCM format — fixed, not configurable
VOSK_SAMPLERATE           = 16000   # what core/listener.py's Vosk models expect
# 48000 / 16000 = 3 exactly, so a fixed integer ratio is enough — unlike
# core/voice.py's Kokoro resampling (arbitrary output-device rates there,
# a single fixed known ratio here since Discord's input format never varies).
_RESAMPLE_UP, _RESAMPLE_DOWN = 1, 3

_active_sinks: dict[int, "_DiscordSTTSink"] = {}   # guild_id -> sink, one voice session per guild


# Bug fix / optimization (2026-08-10): this used to reuse
# core.listener._get_models()'s own 2.2GB "big" Spanish model — reasonable
# in principle ("no duplicate multi-GB load in this process"), except this
# bridge runs as its OWN standalone process (see core/server.py's own
# comment on why), where that model was never actually shared with
# anything — it was really just A 2.2GB load, on its own, paid fresh here
# every time. That's also most of what made !escucha's join time and
# per-utterance transcription both slow (a bigger decode graph is slower
# to search per chunk, not just slower to load). Loads its own dedicated
# SMALL Spanish model instead — 58MB vs 2.2GB, ~1s load vs ~20-30s,
# confirmed via a real timed load 2026-08-10 — independent of whatever
# core.listener uses locally (that model stays untouched; its accuracy
# tuning is for the local mic pipeline, not this one). Cached at module
# level (like core.voice._get_kokoro()'s own pattern) so it's loaded once
# per process, not once per !escucha.
VOSK_MODEL_ES_SMALL_PATH = "data/modelos/vosk-model-small-es-0.42"
_vosk_model_es: "vosk.Model | None" = None
_vosk_model_lock = threading.Lock()


def _get_vosk_model_es() -> "vosk.Model":
    global _vosk_model_es
    with _vosk_model_lock:
        if _vosk_model_es is None:
            _vosk_model_es = vosk.Model(VOSK_MODEL_ES_SMALL_PATH)
        return _vosk_model_es


def _prewarm_vosk() -> None:
    """Loads the small Spanish Vosk model once, in the background, right
    when this standalone process starts — same reasoning as core.voice's
    own Kokoro pre-warm thread. Without this, the first !escucha in a
    fresh process run pays the (now much smaller, but still nonzero)
    model-load cost synchronously inside join_and_listen (even off the
    event loop now — see its own comment — that's still real wall-clock
    time Joan is left waiting after saying !escucha before the sink is
    actually listening)."""
    try:
        _get_vosk_model_es()
        logger.info("[DISCORD-VOICE] Vosk ES (small) model pre-warmed")
    except Exception:
        logger.warning("[DISCORD-VOICE] Vosk pre-warm failed (non-critical — will load on first !escucha instead)", exc_info=True)


threading.Thread(target=_prewarm_vosk, daemon=True, name="discord-voice-vosk-prewarm").start()


# ═══════════════════════════════════════════════════════════════════════════
# JOIN MODE — how HUGO reacts when Joan joins a voice channel in a guild
# she's already in. Persisted (survives a restart, same as every other
# data/*.json setting in this app) rather than in-memory-only, since this
# is a standing preference, not per-session state.
#   "off"  — never auto-anything; !escucha/!callate (see discord_bridge.py)
#            stay the only way to start/stop listening. Default — the base
#            voice pipeline itself hasn't been confirmed working with real
#            audio yet, so nothing auto-triggers until it has.
#   "ask"  — DMs Joan once per join asking to confirm (see
#            _pending_join_offer below) — a plain 'sí'/'no' reply answers
#            it, same yes/no vocabulary core.intent's own pending-action
#            gate already uses for consistency.
#   "auto" — joins immediately, no prompt.
# ═══════════════════════════════════════════════════════════════════════════
_SETTINGS_PATH = "data/discord_voice_settings.json"
_VALID_JOIN_MODES = ("off", "ask", "auto")


def _load_settings() -> dict:
    try:
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("join_mode") in _VALID_JOIN_MODES:
            return data
    except (FileNotFoundError, ValueError, OSError):
        pass
    return {"join_mode": "off"}


def _save_settings(data: dict) -> None:
    os.makedirs(os.path.dirname(_SETTINGS_PATH) or ".", exist_ok=True)
    with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_join_mode() -> str:
    return _load_settings().get("join_mode", "off")


def set_join_mode(mode: str) -> bool:
    if mode not in _VALID_JOIN_MODES:
        return False
    _save_settings({"join_mode": mode})
    logger.info("[DISCORD-VOICE] join_mode set to %r", mode)
    return True


# 'The next DM from Joan is a yes/no answer to a pending join offer' — same
# single-slot, TTL-guarded shape as core.intent._pending_action (only one
# outstanding offer makes sense at a time: she can only be joining one
# voice channel at once). Cleared the moment it's used or expires.
_PENDING_OFFER_TTL_SECONDS = 120
_pending_join_offer: dict | None = None   # {"channel": VoiceChannel, "text_channel": abc.Messageable, "at": float}

_AFFIRMATIVE_RE = re.compile(
    r"^\s*(s[ií]|confirmo|claro|correcto|vale|dale|ok(?:ay)?|exacto|as[ií]\s+es)\b", re.IGNORECASE,
)
_NEGATIVE_RE = re.compile(
    r"^\s*(no|cancela(?:lo)?|olv[ií]dalo|mejor\s+no|para|espera|ahora\s+no)\b", re.IGNORECASE,
)


def has_pending_offer() -> bool:
    global _pending_join_offer
    if _pending_join_offer is None:
        return False
    if time.monotonic() - _pending_join_offer["at"] > _PENDING_OFFER_TTL_SECONDS:
        _pending_join_offer = None
        return False
    return True


def set_pending_offer(voice_channel, text_channel) -> None:
    global _pending_join_offer
    _pending_join_offer = {"channel": voice_channel, "text_channel": text_channel, "at": time.monotonic()}


async def resolve_pending_offer(reply_text: str) -> str | None:
    """Called from discord_bridge.py's DM handler when has_pending_offer()
    is True — consumes the offer (whatever the reply is, per
    core.intent._pending_action's own 'one message, one look' rule) and
    returns a short confirmation string to send back, or None if the reply
    wasn't a recognizable yes/no (offer still gets cleared — an unrelated
    reply shouldn't leave a stale offer around to confuse a later message)."""
    global _pending_join_offer
    offer = _pending_join_offer
    _pending_join_offer = None
    if offer is None:
        return None
    if _AFFIRMATIVE_RE.search(reply_text or ""):
        ok = await join_and_listen(offer["channel"], _joan_id(), offer["text_channel"])
        return "Escuchando." if ok else "No he podido conectarme al canal de voz."
    if _NEGATIVE_RE.search(reply_text or ""):
        return "Vale."
    return None


def _joan_id() -> int:
    return int(os.environ.get("DISCORD_JOAN_ID", "0"))


class _DiscordSTTSink(voice_recv.AudioSink):
    """Captures ONE target Discord user's audio (Joan, via DISCORD_JOAN_ID),
    ignoring everyone else who might be in the same voice channel — same
    single-speaker assumption core/listener.py's own local-mic pipeline
    already makes, just enforced by filtering on Discord user ID instead
    of relying on there being only one microphone in the room. Feeds a
    fresh KaldiRecognizer built from an already-loaded Spanish Vosk model
    (`model_es`, passed in by join_and_listen — see its own docstring for
    why loading happens there and not here)."""

    def __init__(self, target_user_id: int, on_transcript, loop: asyncio.AbstractEventLoop, model_es, on_speech_detected=None):
        super().__init__()
        self._target_user_id    = target_user_id
        self._on_transcript     = on_transcript
        self._on_speech_detected = on_speech_detected
        self._loop              = loop
        self._rec = vosk.KaldiRecognizer(model_es, VOSK_SAMPLERATE)
        # One-shot flag — fires `on_speech_detected` the first time ANY real
        # audio chunk from the target user makes it this far (i.e. past
        # voice_recv's Opus decode, whatever DAVE decryption that needed —
        # see _dave_aware_decode_packet). Doubles as a diagnostic: if this
        # never fires, audio genuinely never reached Vosk at all, as
        # distinct from "reached Vosk but Vosk never finalized a phrase".
        self._heard_notified = False
        self._write_debug_count = 0   # temporary diagnostic (2026-08-10) — see write()'s own comment

    def wants_opus(self) -> bool:
        return False   # decoded PCM, not raw Opus — this POC has no Opus decoder of its own

    def write(self, user, data) -> None:
        # Called from voice_recv's own audio-processing thread, NOT the
        # asyncio event loop — never call back into discord.py directly
        # from here (see _on_transcript's own run_coroutine_threadsafe use).

        # Temporary diagnostic (2026-08-10): logs the first 10 write()
        # calls unconditionally (before the user-filter below), including
        # ones that get filtered out — to answer directly whether
        # write() is even being invoked at all, and if so, whether `user`
        # is resolving to the right Discord ID, the wrong one, or None.
        # Remove once the receive pipeline's real behavior is confirmed.
        if self._write_debug_count < 10:
            self._write_debug_count += 1
            ssrc_map = None
            if user is None:
                try:
                    ssrc_map = dict(self.voice_client._ssrc_to_id)
                except Exception:
                    ssrc_map = "unavailable"
            logger.warning(
                "[DISCORD-VOICE] WRITE-DEBUG call=%d user=%r user_id=%r target=%r pcm_len=%d ssrc_map=%r",
                self._write_debug_count, user, getattr(user, "id", None), self._target_user_id,
                len(data.pcm) if getattr(data, "pcm", None) else 0, ssrc_map,
            )

        if user is None or user.id != self._target_user_id:
            return   # ignore anyone else in the channel — see class docstring
        try:
            if not self._heard_notified and self._on_speech_detected is not None:
                self._heard_notified = True
                asyncio.run_coroutine_threadsafe(self._on_speech_detected(), self._loop)
            stereo    = np.frombuffer(data.pcm, dtype=np.int16).reshape(-1, 2)
            mono      = stereo.mean(axis=1).astype(np.int16)
            resampled = resample_poly(mono, _RESAMPLE_UP, _RESAMPLE_DOWN).astype(np.int16)
            if self._rec.AcceptWaveform(resampled.tobytes()):
                result = json.loads(self._rec.Result())
                text = (result.get("text") or "").strip()
                if text:
                    from core import social as social_mod
                    logger.info("[DISCORD-VOICE] transcript: %r", social_mod.redact_identity_code(text))
                    asyncio.run_coroutine_threadsafe(self._on_transcript(text), self._loop)
        except Exception:
            logger.warning("[DISCORD-VOICE] chunk processing failed (non-critical)", exc_info=True)

    def cleanup(self) -> None:
        pass


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")


async def speak(guild, text: str) -> None:
    """Synthesizes `text` via macOS `say` (core.voice.synthesize_pcm48_say
    — no local playback side effect, just raw PCM bytes) and plays it into
    `guild`'s current voice connection. `say` instead of Kokoro
    (core.voice.synthesize_pcm48) deliberately, per Joan's own 2026-08-10
    test: no multi-GB model to load (Kokoro's own first-ever load in a
    fresh process took ~40s before the first reply could even start), and
    the system-default voice this Mac is configured with (a Siri voice)
    sounded better here anyway.

    Splits `text` into sentences and runs synthesis one sentence ahead of
    playback (a producer task fills a queue while a consumer plays each
    item in order) — the same "don't wait for the whole reply before any
    sound comes out" goal core.voice's own Kokoro streaming pipeline
    already achieves locally, just at sentence granularity since `say`
    itself can't stream mid-utterance. Without this, a multi-sentence
    reply sat in total silence for the ENTIRE reply's synthesis time
    before playing anything — the single biggest gap between Discord
    replies and the local app's felt latency (confirmed 2026-08-10: doing
    the whole 3+ sentence reply as one `say` call took several seconds of
    dead air before any audio started).

    No-op if the bot isn't connected. Blocks the calling coroutine until
    all sentences finish playing (not the event loop itself — vc.play()
    runs on discord.py's own player thread, this just awaits an Event it
    sets per sentence), so callers naturally serialize consecutive
    replies instead of overlapping them."""
    vc = guild.voice_client
    if vc is None or not vc.is_connected():
        return

    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()] or [text]

    loop = asyncio.get_running_loop()
    from core import voice as voice_mod

    if vc.is_playing():
        vc.stop()

    t_speak_start = time.monotonic()
    logger.info("[DISCORD-VOICE] [LATENCY] speak_start sentences=%d", len(sentences))
    pcm_queue: asyncio.Queue = asyncio.Queue()

    async def _producer() -> None:
        try:
            for i, sentence in enumerate(sentences):
                t0 = time.monotonic()
                pcm = await loop.run_in_executor(None, voice_mod.synthesize_pcm48_say, sentence)
                logger.info("[DISCORD-VOICE] [LATENCY] say_synth sentence=%d t=+%.3fs synth_dur=%.3fs",
                            i, time.monotonic() - t_speak_start, time.monotonic() - t0)
                await pcm_queue.put(pcm)
        finally:
            await pcm_queue.put(None)   # sentinel — always signals the consumer to stop, even on error

    async def _play_one(pcm: bytes) -> None:
        done = asyncio.Event()

        def _after(err: Exception | None) -> None:
            if err:
                logger.warning("[DISCORD-VOICE] playback error: %s", err)
            loop.call_soon_threadsafe(done.set)

        source = discord.PCMVolumeTransformer(discord.PCMAudio(io.BytesIO(pcm)))
        vc.play(source, after=_after)
        await done.wait()

    producer_task = asyncio.create_task(_producer())
    try:
        while True:
            pcm = await pcm_queue.get()
            if pcm is None:
                break
            if not pcm:
                logger.warning("[DISCORD-VOICE] a sentence produced no audio — skipping it, not the whole reply")
                continue
            if not vc.is_connected():
                break
            await _play_one(pcm)
    finally:
        producer_task.cancel()
        try:
            await producer_task
        except asyncio.CancelledError:
            pass


async def join_and_listen(voice_channel, target_user_id: int, text_channel) -> bool:
    """Joins `voice_channel`, transcribes `target_user_id`'s speech, routes
    each finalized segment through the same Groq-backed reply generation
    core/discord_bridge.py's DM path uses (core.discord_bridge.generate_reply
    — admin role, since target_user_id is always Joan here), posts both the
    transcript and the reply as text (for a visible log of the exchange),
    and speaks the reply back into the channel via speak() above. Returns
    True if the connection succeeded. Best-effort — logs and returns False
    on any failure rather than raising into the bot's event loop."""
    guild_id = voice_channel.guild.id
    if guild_id in _active_sinks:
        return False   # already listening in this guild — caller should leave() first

    loop = asyncio.get_running_loop()

    async def _on_speech_detected() -> None:
        try:
            await text_channel.send("🎧 Te oigo.")
        except Exception:
            logger.warning("[DISCORD-VOICE] failed to post speech-detected notice", exc_info=True)

    async def _on_transcript(text: str) -> None:
        t_transcript = time.monotonic()
        try:
            await text_channel.send(f"🎤 {text}")
            logger.info("[DISCORD-VOICE] [LATENCY] transcript_posted t=+%.3fs", time.monotonic() - t_transcript)
        except Exception:
            logger.warning("[DISCORD-VOICE] failed to post transcript", exc_info=True)

        try:
            from core import discord_bridge
            reply = await loop.run_in_executor(
                None, discord_bridge.generate_reply, str(target_user_id), text, "admin",
            )
            logger.info("[DISCORD-VOICE] [LATENCY] reply_generated t=+%.3fs", time.monotonic() - t_transcript)
        except Exception:
            logger.exception("[DISCORD-VOICE] failed to generate reply")
            reply = "Error en el LLM. Inténtalo en un momento."

        try:
            await text_channel.send(reply)
            logger.info("[DISCORD-VOICE] [LATENCY] reply_text_posted t=+%.3fs", time.monotonic() - t_transcript)
        except Exception:
            logger.warning("[DISCORD-VOICE] failed to post reply text", exc_info=True)

        try:
            await speak(voice_channel.guild, reply)
            logger.info("[DISCORD-VOICE] [LATENCY] speak_done t=+%.3fs", time.monotonic() - t_transcript)
        except Exception:
            logger.warning("[DISCORD-VOICE] failed to speak reply", exc_info=True)

    try:
        # Bug fix (2026-08-10): constructing the sink used to load the
        # Vosk model directly inside __init__, on the event loop thread —
        # a first-ever load (of the old 2.2GB model at the time) took long
        # enough to block the loop past Discord's heartbeat timeouts,
        # which killed both the gateway and voice connections mid-join.
        # Confirmed via a real join test: "she joined and then randomly
        # left the call" with heartbeat-blocked warnings in the log at
        # the exact moment the sink was constructed. Loading it here, off
        # the event loop, before even connecting, fixes that regardless
        # of model size — and _get_vosk_model_es()'s own caching means
        # this is a no-op after the first call anyway (or after
        # _prewarm_vosk() above already finished).
        model_es = await loop.run_in_executor(None, _get_vosk_model_es)
        vc = await voice_channel.connect(cls=voice_recv.VoiceRecvClient)
        sink = _DiscordSTTSink(target_user_id, _on_transcript, loop, model_es, on_speech_detected=_on_speech_detected)
        vc.listen(sink)
        _active_sinks[guild_id] = sink
        logger.info("[DISCORD-VOICE] joined %r, listening for user_id=%s", voice_channel.name, target_user_id)
        return True
    except Exception:
        logger.exception("[DISCORD-VOICE] failed to join/listen on %r", voice_channel.name)
        return False


async def leave(guild) -> bool:
    """Disconnects from `guild`'s voice channel if currently connected via
    this module. Returns True if it actually disconnected anything."""
    guild_id = guild.id
    if guild_id not in _active_sinks:
        return False
    del _active_sinks[guild_id]
    vc = guild.voice_client
    if vc is not None:
        await vc.disconnect(force=True)
    logger.info("[DISCORD-VOICE] left voice channel in guild=%s", guild_id)
    return True


def is_listening(guild_id: int) -> bool:
    return guild_id in _active_sinks


async def simulate_transcript(guild_id: int, text: str) -> bool:
    """Test hook (2026-08-10) — invokes the active session's on_transcript
    callback directly with `text`, exactly as if Vosk had just produced it
    from real audio. Skips the entire receive path (Discord's Opus decode,
    the DAVE decrypt fix in _dave_aware_decode_packet, Vosk itself) — that
    path genuinely needs real Discord voice traffic and can't be
    meaningfully faked without it — but exercises everything after it for
    real: Groq reply generation (core.discord_bridge.generate_reply),
    posting to the text channel, and actual Kokoro synthesis + real
    Discord voice playback (speak()). Added so iterating on the
    reply/speak half of this pipeline doesn't require physically speaking
    into a mic for every single test. Returns False if nobody's
    listening in this guild right now (caller should say !escucha
    first)."""
    sink = _active_sinks.get(guild_id)
    if sink is None:
        return False
    await sink._on_transcript(text)
    return True
