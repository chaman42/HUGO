# api_key_store.py — user-editable API key overrides, backing Ajustes'
# "Claves API" panel (core/routes_api_keys.py, ui/js/settings-updates.js).
# Lets Joan paste a key for each provider (including Dani's own
# GROQ_API_KEY_DANI/SERPER_API_KEY_DANI — see core/active_person.py) without
# hand-editing .env.
#
# Persisted to data/api_key_overrides.json — personal/runtime config, not
# source (see .gitignore's own comment on data/), rather than rewriting
# .env itself, which would blow away all of .env.example's hand-written
# documentation comments on every save.
#
# Every managed key is pushed straight into os.environ, live, the moment
# it's set — so every existing `os.getenv(...)` call site (core.tools_search,
# core.code_engine, ...) picks up a saved key immediately with ZERO code
# changes there, no restart needed. The one exception is
# core.groq_config's GROQ_API_KEYS list (computed once at import instead of
# read fresh per call) — see on_change() below, which groq_config
# registers itself with so a saved Groq key takes effect there too.
import json
import logging
import os
import threading

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv()   # idempotent — guarantees .env is loaded before _ENV_BASELINE is captured below, regardless of import order

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_PATH     = os.path.join(_DATA_DIR, "api_key_overrides.json")
_lock     = threading.Lock()

# Every key Ajustes is allowed to set — matches .env.example's LLM/search
# provider keys. Deliberately excludes REGISTER_TOKEN/DISCORD_*/MIC_NAME:
# different security posture (one-time whitelist token, bot credential) or
# not a secret at all, and Discord's bridge only reads its token at process
# startup (core.server.start()) regardless, so a live edit wouldn't even
# take effect without a restart the way these do.
#
# KEY_OWNERS (2026-08-24) — Dani gets his own fully separate HUGO install
# (his own Mac, his own .env/data/), but it's the SAME codebase, and the
# identity system already defaults an unrecognized device to "dani" (see
# core.social's own module docstring) — so on Dani's own machine, HE is
# who gets identified there, on day one. core.routes_api_keys uses this
# map to scope Ajustes' "Claves API" panel to only the current person's
# OWN keys: Joan's keys don't exist to Dani (his explicit request, "api
# keys that only me use, simply do not exist to him"), and — symmetrically,
# no cross-access even for Joan — Dani's keys aren't Joan's to touch either.
KEY_OWNERS = {
    "GROQ_API_KEY":          "joan",
    "GROQ_API_KEY_2":        "joan",
    "SERPER_API_KEY":        "joan",
    "DEEPSEEK_API_KEY":      "joan",
    "CLOUDFLARE_ACCOUNT_ID": "joan",
    "CLOUDFLARE_API_TOKEN":  "joan",
    "GROQ_API_KEY_DANI":     "dani",
    "SERPER_API_KEY_DANI":   "dani",
}
MANAGED_KEYS = list(KEY_OWNERS)


def keys_for(person_id: str | None) -> list[str]:
    """The subset of MANAGED_KEYS visible/editable to `person_id` (a
    core.social profile id) — anyone who isn't specifically "dani" gets
    Joan's group, matching the rest of the app's permissive-default-to-Joan
    convention for admin-ish surfaces when nobody's been identified yet."""
    owner = "dani" if person_id == "dani" else "joan"
    return [key for key, o in KEY_OWNERS.items() if o == owner]

# Snapshot of whatever .env/the real environment already had BEFORE any
# saved override is applied — set_key(key, "") restores this instead of
# just deleting the env var outright, so clearing a slot in Ajustes falls
# back to .env's own value (if any) rather than losing it until the next
# process restart.
_ENV_BASELINE = {key: os.environ.get(key) for key in MANAGED_KEYS}

_on_change_callbacks: list = []


def on_change(callback) -> None:
    """Registers `callback` (no args) to run after any key is set/cleared —
    core.groq_config uses this to rebuild its derived GROQ_API_KEYS list."""
    _on_change_callbacks.append(callback)


def _load_saved() -> dict:
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        logger.warning("Failed to read %s — treating as empty", _PATH, exc_info=True)
        return {}


def apply_saved_to_environ() -> None:
    """Pushes every saved override into os.environ. Called once by
    core.groq_config at import time (early in the app's startup, before
    anything else reads its own os.getenv default) so a key saved in a
    PREVIOUS run is already live for the very first request, not just
    after the next Ajustes edit."""
    for key, value in _load_saved().items():
        if key in MANAGED_KEYS and value:
            os.environ[key] = value


def get_status() -> dict:
    """{key: bool} for every MANAGED_KEYS entry — whether it currently has
    a value (from .env OR a saved override). Never the value itself; a key
    already set is shown in Ajustes as filled-but-masked, never echoed."""
    return {key: bool(os.environ.get(key, "").strip()) for key in MANAGED_KEYS}


def set_key(key: str, value: str) -> None:
    """Saves `value` for `key` (persisted + applied to os.environ live), or
    — if `value` is blank — clears the override and restores whatever
    .env/the real environment had for it before (see _ENV_BASELINE above)."""
    if key not in MANAGED_KEYS:
        raise ValueError(f"unknown API key {key!r}")
    value = value.strip()
    with _lock:
        saved = _load_saved()
        if value:
            saved[key] = value
            os.environ[key] = value
        else:
            saved.pop(key, None)
            baseline = _ENV_BASELINE.get(key)
            if baseline:
                os.environ[key] = baseline
            else:
                os.environ.pop(key, None)
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(_PATH, "w", encoding="utf-8") as f:
            json.dump(saved, f, indent=2)
    for cb in _on_change_callbacks:
        try:
            cb()
        except Exception:
            logger.warning("api_key_store on_change callback failed", exc_info=True)
