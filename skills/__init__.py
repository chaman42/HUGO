# ═══════════════════════════════════════════════════════════════════════════
# SKILLS — loadable capabilities layer, kept separate from hugo_core's
# always-on internals (voice, memory, intent, server). Every skill is a
# HugoSkill subclass instantiated by the loader below and gated by its own
# `skill_*` entry in data/feature_flags.json (via core.memory_flags —
# core.memory re-exports the same is_feature_enabled/reload_feature_flags
# every other flag-gated feature in this codebase already uses, so toggling
# a skill off from the Ajustes panel needs no new plumbing).
#
# Skills wrap EXISTING capabilities (core.tools, core.tools_calendar,
# core.investigations, core.discord_bridge, core.commands.generate_schema)
# behind one uniform execute(query, context) interface — they don't
# reimplement any of that logic.
# ═══════════════════════════════════════════════════════════════════════════
import importlib
import inspect
import logging
import pkgutil
import threading

from core.memory_flags import is_feature_enabled, set_feature_flag

logger = logging.getLogger(__name__)


class HugoSkill:
    """Base interface every skill module implements via one concrete
    subclass. `flag` is the data/feature_flags.json key gating this skill;
    left blank it defaults to f"skill_{name}" (see _flag_for)."""
    name: str = ""
    description: str = ""
    triggers: list[str] = []
    enabled: bool = True
    flag: str = ""

    def execute(self, query: str, context: dict) -> str:
        raise NotImplementedError


_lock = threading.Lock()
_skills: dict[str, HugoSkill] = {}


def _flag_for(skill: HugoSkill) -> str:
    return skill.flag or f"skill_{skill.name}"


def _discover() -> dict[str, HugoSkill]:
    found: dict[str, HugoSkill] = {}
    for modinfo in pkgutil.iter_modules(__path__):
        if modinfo.name.startswith("_"):
            continue
        qualified = f"{__name__}.{modinfo.name}"
        try:
            module = importlib.import_module(qualified)
            module = importlib.reload(module)   # picks up on-disk edits on reload_skills()
        except Exception:
            logger.warning("skills: failed to load %s", modinfo.name, exc_info=True)
            continue
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, HugoSkill) and obj is not HugoSkill and obj.__module__ == qualified:
                if not obj.name:
                    logger.warning("skills: %s.%s has no `name` set, skipping", modinfo.name, obj.__name__)
                    continue
                try:
                    found[obj.name] = obj()
                except Exception:
                    logger.warning("skills: failed to instantiate %s", obj, exc_info=True)
    return found


def reload_skills() -> None:
    """Re-scan skills/ from disk. Call after editing a skill file, or after
    a skill_* feature flag flips, to pick up the change without restarting
    the server."""
    global _skills
    with _lock:
        _skills = _discover()


def get_skill(name: str) -> HugoSkill | None:
    """The named skill if it exists AND its feature flag is currently on;
    None otherwise (unknown name or disabled)."""
    with _lock:
        skill = _skills.get(name)
    if skill is None:
        return None
    skill.enabled = is_feature_enabled(_flag_for(skill))
    return skill if skill.enabled else None


def list_skills(enabled_only: bool = True) -> list[HugoSkill]:
    """Every loaded skill, or just the currently-enabled ones (default) —
    what the conversation engine should offer for this turn."""
    with _lock:
        skills = list(_skills.values())
    for s in skills:
        s.enabled = is_feature_enabled(_flag_for(s))
    return [s for s in skills if s.enabled] if enabled_only else skills


def is_loaded(name: str) -> bool:
    """True if `name` was discovered and instantiated successfully on the
    last scan, regardless of its feature flag — distinct from get_skill(),
    which also checks the flag. Used by core.module_manager to tell "module
    code is broken/missing" apart from "module is just switched off"."""
    with _lock:
        return name in _skills


def set_enabled(name: str, enabled: bool) -> bool:
    """Flip `name`'s skill_* feature flag on/off — the one piece of
    core.memory_flags access this package exposes outward, so callers like
    core.module_manager (which must not import core internals directly,
    only core.skills) can enable/disable a skill without reaching past
    this package's boundary. Returns False if `name` isn't a known skill or
    its flag isn't a registered feature flag; never raises."""
    with _lock:
        skill = _skills.get(name)
    if skill is None:
        return False
    try:
        set_feature_flag(_flag_for(skill), enabled)
    except ValueError:
        logger.warning("skills: %s's flag %r is not a registered feature flag", name, _flag_for(skill))
        return False
    skill.enabled = enabled
    return True


reload_skills()
