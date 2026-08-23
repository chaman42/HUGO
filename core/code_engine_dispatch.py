# CODE ENGINE DISPATCH — wires Code Engine (core/code_engine/) into the
# live conversation pipeline, mirroring core.skill_dispatch's shape but
# with two real differences:
#
#   1. Explicit-trigger only, never an LLM-decided implicit path. Code
#      Engine actions mutate the filesystem, run shell commands, install
#      dependencies, and can deploy — a materially higher blast radius
#      than picking a skill to answer a question, so this only ever fires
#      on a direct, unambiguous order (core/intent.py's
#      _INTENT_CODE_CREATE_RE/_INTENT_CODE_UPDATE_RE/_INTENT_CODE_REVIEW_RE),
#      the same "Level 1 direct order" bar core.actions' other autonomous
#      triggers (create_task, start_investigation) already hold
#      themselves to.
#
#   2. Two different weight classes, per action:
#        - review  -> CodeReviewer.review_full_project(), synchronous
#          (read-only, no filesystem mutation, fast enough to answer in
#          the same conversation turn).
#        - create/update -> CodeEngine.create_ad_hoc_module()/
#          update_module(), fire-and-forget on a background thread (can
#          take minutes, multiple LLM calls) — HUGO acknowledges
#          immediately, the real outcome is delivered via
#          core.notifications on Joan's next turn.
#
# Create/update deliberately do NOT go through
# core.code_engine.tools.orchestrator.ToolOrchestrator.execute_goal() —
# that WAS this module's first implementation, and a live end-to-end test
# exposed real problems with it for this specific use case: its generic,
# freelance LLM-driven tool-calling has no guardrail enforcing the
# HugoSkill class shape skills.reload_skills() actually requires (it
# produced bare functions, not a working module), no guarantee of file-
# path consistency across steps (it created two different files for one
# module), and never actually calls ModuleManager.install()/update() on
# success, so even a "completed" run never installed anything. Routing
# through core.code_engine.CodeEngine's existing create_module()/
# update_module() pipeline instead sidesteps all three at once: that
# pipeline's _CODE_CONTEXT prompt already enforces the HugoSkill shape,
# it always writes to exactly one deterministic path
# (skills/<module_name>.py), and it already calls ModuleManager.install()/
# .update() itself as its last step on success — the same proven path the
# Módulos catalog's own build/update buttons already use, just without
# requiring a pre-existing catalog entry (see create_ad_hoc_module()'s own
# docstring). Orchestrator/Planner/Deployer remain fully available for
# broader, non-module Code Engine work via the API
# (POST /api/code-engine/orchestrate, /deploy/hugo-module) — this module
# just no longer routes conversational module creation through them.
import logging
import re
import threading
import unicodedata

logger = logging.getLogger(__name__)

PROJECT_PATH = "skills"

# Common short Spanish function words stripped when deriving a module name
# from free text ("crea un módulo DE lanzar UNA moneda" -> "lanzar_moneda",
# not "de_lanzar_una_moneda") — purely cosmetic, never changes what gets
# built, just what the resulting skills/<name>.py is called.
_SPANISH_STOPWORDS = {"un", "una", "el", "la", "los", "las", "de", "del", "que", "para", "con"}
_MAX_MODULE_NAME_LEN = 50


def _code_engine_enabled() -> bool:
    try:
        from core import memory
        return memory.is_feature_enabled("code_engine_enabled")
    except Exception:
        return True


def slugify_module_name(topic: str) -> str:
    """Free-text topic -> a valid Python module name (skills/<name>.py,
    and the HugoSkill's own `name` attribute). Strips accents (NFKD ->
    ASCII), lowercases, drops short Spanish function words, joins the
    rest with underscores. Never returns empty — falls back to
    'modulo_nuevo' if the topic had no usable words at all (e.g. it was
    only stopwords or punctuation)."""
    ascii_text = unicodedata.normalize("NFKD", topic or "").encode("ascii", "ignore").decode("ascii")
    words = re.findall(r"[a-zA-Z0-9]+", ascii_text.lower())
    meaningful = [w for w in words if w not in _SPANISH_STOPWORDS]
    name = "_".join(meaningful or words)[:_MAX_MODULE_NAME_LEN].strip("_")
    return name or "modulo_nuevo"


def review(topic: str = "") -> str | None:
    """Synchronous — returns a human-readable Spanish summary, or None if
    Code Engine is disabled/unavailable (caller falls back to a normal
    reply in that case, same as skill_dispatch.run_skill() returning
    None).

    Bug fix: `topic` used to be accepted but never actually used — every
    call went through CodeReviewer.review_full_project() regardless,
    meaning "revisa el módulo de X" reviewed ALL of skills/ (up to 25
    files, 2 LLM calls each) instead of just X. Now: if `topic` names an
    existing module (skills/<slugified_topic>.py), review just that one
    file via review_file() — both more correct (matches what was
    actually asked) and far faster on this hardware. Falls back to the
    full-project review only when topic is empty (a bare "revisa el
    módulo" with nothing named) or doesn't match any real file."""
    if not _code_engine_enabled():
        return None
    from core.code_engine.tool_manager import tool_manager
    reviewer = tool_manager.get_tool("code_reviewer")
    if reviewer is None:
        return None

    if topic:
        import os
        module_path = os.path.join(PROJECT_PATH, f"{slugify_module_name(topic)}.py")
        if os.path.isfile(module_path):
            report = reviewer.review_file(module_path)
            return reviewer.generate_report(report)

    report = reviewer.review_full_project(PROJECT_PATH)
    return reviewer.generate_report(report)


def resolve_existing_module(topic: str) -> str | None:
    """For UPDATE only: a spoken/typed update request almost always states
    the change in the same breath as the module name ("actualiza el
    módulo de X para que también Y") — topic is the WHOLE thing, module
    name and change description mixed together. slugify_module_name()
    alone can't tell them apart (bug, found live: "lanzar_moneda para que
    también pueda decir 50/50" slugified into one long bogus name,
    "lanzar_moneda_tambien_pueda_decir_50_50", and update_module()
    correctly-but-uselessly failed with 'module file not found').

    Since an update only ever makes sense against an EXISTING module,
    resolve against the real, known set instead of guessing from text
    alone. Matches in BOTH directions against progressively longer
    prefixes of the topic's own words (longest prefix first, so a
    hypothetical short module name never shadows a longer real match):
      - topic prefix == an existing name, or that name is a further
        prefix of it ("lanzar_moneda para que..." -> 'lanzar_moneda').
      - an existing name STARTS WITH the topic prefix ("discord para
        que..." -> 'discord' is itself a prefix of the real module
        'discord_bridge' — the module's actual filename doesn't always
        match how Joan naturally refers to it).
    Returns None if nothing matches — caller falls back to plain
    slugification, which then fails the same honest, safe way
    update_module() already does for a module that truly doesn't
    exist."""
    import os
    import re
    import unicodedata
    ascii_text = unicodedata.normalize("NFKD", topic or "").encode("ascii", "ignore").decode("ascii")
    words = [w for w in re.findall(r"[a-zA-Z0-9]+", ascii_text.lower()) if w not in _SPANISH_STOPWORDS]
    if not words:
        return None
    try:
        existing = sorted(os.listdir("skills/manifests"), key=len, reverse=True)
    except OSError:
        return None
    for length in range(len(words), 0, -1):
        candidate = "_".join(words[:length])
        if len(candidate) < 3:   # too short to mean anything — avoid single-letter false matches
            continue
        for name in existing:
            if candidate == name or name.startswith(candidate) or candidate.startswith(name + "_"):
                return name
    return None


def dispatch_module_task(action: str, topic: str) -> bool:
    """Fire-and-forget — create_ad_hoc_module()/update_module() on a
    background thread. Returns True if it was actually started (Code
    Engine enabled) so the caller (core.commands) knows whether to speak
    an acknowledgment or a "can't do that right now" reply — never blocks
    on the goal itself, which can take minutes on this hardware (see
    core.code_engine.OLLAMA_STALL_TIMEOUT_SECONDS' own docstring)."""
    if not _code_engine_enabled():
        return False

    if action == "update":
        module_name = resolve_existing_module(topic) or slugify_module_name(topic)
    else:
        module_name = slugify_module_name(topic)

    def _run():
        try:
            from core.code_engine import code_engine
            if action == "create":
                ok = code_engine.create_ad_hoc_module(module_name, topic)
            else:
                ok = code_engine.update_module(module_name, topic)
            _notify_module_result(action, module_name, topic, ok)
        except Exception:
            logger.error("code_engine_dispatch: %s(%r) raised unexpectedly", action, module_name, exc_info=True)

    threading.Thread(target=_run, daemon=True, name="code-engine-module-task").start()
    return True


def _notify_module_result(action: str, module_name: str, topic: str, ok: bool) -> None:
    """create_ad_hoc_module()/update_module() already notify Joan
    themselves on FAILURE (CodeEngine._block_and_notify(), via
    TaskEngine.block_task() — see that method's own docstring), so this
    only ever needs to add the missing SUCCESS notification — same gap
    core.code_engine_dispatch previously had to patch for Orchestrator's
    own execute_goal()."""
    if not ok:
        return
    try:
        from core import notifications as notifications_mod
        verb = "creado" if action == "create" else "actualizado"
        notifications_mod.create_notification(
            "code_engine",
            f"Módulo {verb}: {module_name}",
            f"Módulo '{module_name}' {verb} e instalado — {topic}.",
        )
    except Exception:
        logger.error("code_engine_dispatch: success notification failed", exc_info=True)
