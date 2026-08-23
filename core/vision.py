# VISION — describes an attached image so LIRA can react to what's in it.
# Same cloud-primary/local-fallback shape as core.code_engine.LLMRouter
# (DeepSeek -> Ollama qwen2.5-coder for code), just for image understanding:
# a free vision model via OpenRouter tried first, falling back to a local
# Ollama vision model (moondream — small, CPU-tolerable) only if OpenRouter
# is unreachable/unconfigured/errors. Callers get back plain descriptive
# text, which core.commands folds into user_content as extra context for
# the normal Groq personality reply — this module never talks to Groq or
# decides what LIRA says, it only describes what's in the image.
#
# OpenRouter, not Gemini direct — Google AI Studio's own API key signup
# hit a hard age-verification/regional block on Joan's account (nothing
# fixable from this codebase; that's a Google account-level gate, not a
# code or API problem). OpenRouter is a separate company/signup with no
# such gate and aggregates several genuinely-free vision models (including
# Gemini's own, via OPENROUTER_VISION_MODEL below) behind one OpenAI-
# compatible endpoint.
import json
import logging
import os
import subprocess

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("vision")
if not logger.handlers:
    os.makedirs("logs", exist_ok=True)
    _file_handler = logging.FileHandler("logs/vision.log", encoding="utf-8")
    _file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_file_handler)
    logger.setLevel(logging.INFO)

# Registers HEIC/HEIF with Pillow's own Image.open() dispatch (patches
# Image.register_open() under the hood) — plain Pillow has no built-in
# HEIF codec, so it can't decode iPhone's default photo format at all
# (verified directly: Image.open() on a real HEIC-encoded copy of the
# same test photo raises UnidentifiedImageError without this registration,
# and _normalize_image() round-trips it to PNG successfully with it) —
# exactly the format a shared-not-exported iPhone photo can arrive in,
# even though everything else _normalize_image() handles (WEBP, JPEG,
# PNG, ...) works out of the box via plain Pillow. One-time module-load
# registration, same "best-effort, never block the rest of the app"
# spirit as this file's other optional-dependency handling — an
# environment without pillow-heif installed just falls back to
# _normalize_image()'s existing decode-failure path (original bytes
# passed through, provider's own error surfaces) instead of gaining HEIC
# support.
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    logger.warning("VisionRouter: pillow-heif not installed — HEIC/HEIF images will fail to normalize")

OPENROUTER_API_URL   = "https://openrouter.ai/api/v1/chat/completions"
# ":free" suffix is OpenRouter's own convention for zero-cost model
# variants — this one (Meta's Llama 3.2 11B Vision) has been consistently
# available on the free tier; override via OPENROUTER_VISION_MODEL if
# that changes or a better free vision model shows up.
OPENROUTER_VISION_MODEL = os.environ.get("OPENROUTER_VISION_MODEL", "meta-llama/llama-3.2-11b-vision-instruct:free")
OLLAMA_HOST      = "http://localhost:11434"
OLLAMA_VISION_MODEL = "moondream"

_DEFAULT_QUESTION = "Describe esta imagen en detalle, en español."

# moondream (1B, English-centric) is NOT the same "just works in Spanish"
# situation as the Groq/DeepSeek/qwen2.5-coder models this codebase's other
# routers fall back to — verified live: a Spanish prompt ("Describe esta
# imagen en detalle, en español.") on the exact same image returned literal
# garbage ('!!!IMAGES!!!'), while an English prompt on the identical image
# returned a real (if mediocre — it's a tiny model) description. So the
# Ollama fallback always asks in English regardless of what the user typed
# or what `question` the caller passed — core.commands' Groq call downstream
# is the multilingual, personality-aware one; it can paraphrase an English
# description into Spanish in LIRA's own voice just fine. OpenRouter
# (_openrouter below) has no such restriction and uses the real `question`
# as given.
_OLLAMA_QUESTION = "Describe this image in detail — objects, colors, setting, any visible text."


def _normalize_image(image_b64: str, mime_type: str) -> tuple[str, str]:
    """Re-encodes any decodable image to PNG — verified live against a real
    phone photo (IMG_4339.WEBP): Ollama's /api/chat rejected the raw WEBP
    bytes outright ("Failed to load image or audio file", HTTP 400) even
    though the browser's own File/Blob accepted it fine (accept="image/*"
    in ui/index.html has no format allowlist beyond that). PNG is the one
    format every backend here (moondream's decoder, OpenRouter's various
    upstream providers) is guaranteed to handle, so this runs once up
    front for BOTH paths rather than hoping each provider's decoder covers
    whatever the phone/browser handed over. On any decode failure (corrupt
    data, truly unsupported format), returns the input unchanged — better
    to let the provider's own error surface than to swallow a real problem
    here."""
    try:
        import base64
        import io
        from PIL import Image
        raw = base64.b64decode(image_b64)
        img = Image.open(io.BytesIO(raw))
        img.load()   # WEBP/etc. are lazily decoded — force it now, inside this try
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode(), "image/png"
    except Exception as e:
        logger.warning("VisionRouter: image normalization failed (%s) — using original bytes", e)
        return image_b64, mime_type


class VisionRouter:
    def describe_image(self, image_b64: str, mime_type: str, question: str | None = None) -> str | None:
        """Returns a plain-text description, or None if both the cloud
        model and the local fallback failed — never raises, matching
        LLMRouter's own "degrade, don't crash the turn" contract. Callers
        should treat None as "couldn't see it" and say so, not silently
        drop the attachment."""
        image_b64, mime_type = _normalize_image(image_b64, mime_type)
        q = (question or _DEFAULT_QUESTION).strip() or _DEFAULT_QUESTION
        try:
            return self._openrouter(image_b64, mime_type, q)
        except Exception as e:
            logger.warning("VisionRouter: OpenRouter unavailable (%s) — falling back to Ollama", e)
        try:
            return self._ollama(image_b64)   # ignores `q` — see _OLLAMA_QUESTION's own comment for why
        except Exception as e:
            logger.error("VisionRouter: Ollama fallback also failed (%s)", e)
            return None

    def _openrouter(self, image_b64: str, mime_type: str, question: str) -> str:
        import urllib.request

        api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY not set")

        # OpenAI-compatible chat/completions shape — image as a data: URI
        # inside a multi-part user message, same "text part + image part"
        # convention every OpenAI-style vision API (including OpenRouter's
        # own passthrough to whichever provider backs the model) expects.
        payload = json.dumps({
            "model": OPENROUTER_VISION_MODEL,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_b64}"}},
                ],
            }],
        }).encode("utf-8")
        req = urllib.request.Request(
            OPENROUTER_API_URL, data=payload, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                # Optional but recommended by OpenRouter for free-tier
                # rate-limit attribution — a real value is safe to send,
                # not a secret, same spirit as this codebase's other
                # public-facing identifiers (e.g. DISCORD_JOAN_ID).
                "HTTP-Referer": "https://github.com/chaman42/JarvisLite",
                "X-Title": "LIRA",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(f"unexpected OpenRouter response shape: {data}")
        if not text or not text.strip():
            raise RuntimeError("OpenRouter returned an empty response")
        return text.strip()

    def _ensure_ollama_model(self) -> None:
        """Same idempotent pull-if-missing pattern as
        LLMRouter._ensure_ollama_model — `ollama pull` no-ops if the model
        is already present, so this is safe to call on every fallback
        rather than caching the check."""
        try:
            result = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=10)
            if OLLAMA_VISION_MODEL in (result.stdout or ""):
                return
        except Exception:
            pass   # fall through and try to pull anyway
        try:
            logger.info("VisionRouter: pulling %s (first use — may take a while)", OLLAMA_VISION_MODEL)
            subprocess.run(["ollama", "pull", OLLAMA_VISION_MODEL], timeout=600)
        except Exception as e:
            logger.warning("VisionRouter: could not pull %s (%s)", OLLAMA_VISION_MODEL, e)

    def _ollama(self, image_b64: str) -> str:
        import urllib.request
        import core.ollama_control as ollama_control

        ollama_control.ensure_ollama_daemon_running()
        self._ensure_ollama_model()

        # /api/chat, not /api/generate — verified live against this exact
        # model: /api/generate's top-level "images" field silently produces
        # an immediate empty completion for moondream (done_reason "stop",
        # eval_count 1) even though prompt_eval succeeds and the image
        # tokens are clearly in context — a real quirk of this model/Ollama
        # version combo, not a timeout or bad payload. /api/chat's
        # per-message "images" field (the officially documented multimodal
        # shape) returns a real description with the identical image.
        payload = json.dumps({
            "model": OLLAMA_VISION_MODEL,
            "messages": [{"role": "user", "content": _OLLAMA_QUESTION, "images": [image_b64]}],
            "stream": False,
        }).encode("utf-8")

        def _one_attempt() -> str:
            req = urllib.request.Request(
                f"{OLLAMA_HOST}/api/chat", data=payload, method="POST",
                headers={"Content-Type": "application/json"},
            )
            # Same "CPU inference can genuinely be slow" reasoning as
            # LLMRouter._ollama's OLLAMA_STALL_TIMEOUT_SECONDS — measured
            # this directly at ~80s of prompt-eval alone for a single
            # 300x300 image on this CPU-only hardware, so 180s total is
            # generous without stalling a chat reply indefinitely.
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return str((data.get("message") or {}).get("content", "")).strip()

        # One retry on an empty (but non-erroring) response — observed live
        # even on /api/chat: occasionally done_reason "stop" fires after a
        # single token for no apparent reason (same model, same image,
        # same prompt that worked seconds before/after) — a genuine local-
        # inference flake, not something the payload/endpoint fix above
        # controls. A second attempt has consistently succeeded when this
        # happens, so it's cheaper to retry once than to fall through to
        # "couldn't see it" for what's usually a one-off hiccup.
        text = _one_attempt()
        if not text:
            logger.debug("VisionRouter: Ollama returned empty on first attempt — retrying once")
            text = _one_attempt()
        if not text:
            raise RuntimeError("Ollama returned an empty response (twice)")
        return text


vision_router = VisionRouter()
