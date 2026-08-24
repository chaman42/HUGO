# api_key_validation.py — real, minimal test calls against a provider before
# core.routes_api_keys persists a key someone just pasted into Ajustes.
# Kept out of core.api_key_store (a dumb persistence layer, see its own
# docstring) and out of the route module (keeps routes thin) on purpose.
#
# Scope: Groq and Serper only — the two providers that actually gate Dani's
# usable chat (core.commands' onboarding gate). DEEPSEEK_API_KEY/
# CLOUDFLARE_* have no equally cheap, universally-documented "just verify
# this" call, and both are Joan-only/internal-fallback keys with far lower
# blast radius than a bad Groq/Serper key silently blocking Dani — they
# keep saving unvalidated, same as before this module existed.
import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


def validate_groq(value: str) -> tuple[bool, str | None]:
    """Groq(api_key=value).models.list() — a real auth check with no
    completion tokens billed."""
    try:
        from groq import Groq
        Groq(api_key=value).models.list()
        return True, None
    except Exception as exc:
        logger.debug("Groq key validation failed: %s", exc, exc_info=True)
        return False, "No se pudo verificar esa clave de Groq."


def validate_serper(value: str) -> tuple[bool, str | None]:
    """One real minimal POST to Serper's own search endpoint — same
    urllib.request approach core.tools_search already uses for the real
    thing, no new dependency."""
    try:
        req = urllib.request.Request(
            "https://google.serper.dev/search",
            data=json.dumps({"q": "test"}).encode("utf-8"),
            headers={"X-API-KEY": value, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status == 200:
                return True, None
            return False, "Serper rechazó esa clave."
    except urllib.error.HTTPError as exc:
        logger.debug("Serper key validation failed: %s", exc, exc_info=True)
        return False, "Serper rechazó esa clave."
    except Exception as exc:
        logger.debug("Serper key validation failed: %s", exc, exc_info=True)
        return False, "No se pudo contactar con Serper para verificar la clave."


VALIDATORS = {
    "GROQ_API_KEY":        validate_groq,
    "GROQ_API_KEY_2":      validate_groq,
    "GROQ_API_KEY_DANI":   validate_groq,
    "SERPER_API_KEY":      validate_serper,
    "SERPER_API_KEY_DANI": validate_serper,
}
