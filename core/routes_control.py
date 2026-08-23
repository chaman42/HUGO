"""Flask routes: text-command dispatch, mic/TTS mute, feature flags,
listen mode, and misc status/reload endpoints."""
import logging
import threading

from flask import jsonify, request

import core.server as _server
from core.server import app, socketio, emit_force_reload

logger = logging.getLogger(__name__)

_MAX_IMAGE_B64_CHARS = 8_000_000   # ~6MB decoded — generous for a phone photo, cheap enough to reject before it ever reaches OpenRouter/Ollama

@app.route("/text_command", methods=["POST", "OPTIONS"])
def text_command():
    if request.method == "OPTIONS":
        return "", 204
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    # images: [{"data": <base64, no data: prefix>, "mime": "image/jpeg"}, ...] —
    # ui/js/chat-render.js's staged attachments, FileReader-encoded before
    # POST. Text-less-but-image-only messages are valid (a bare photo with
    # no caption), so the emptiness check below covers both together rather
    # than requiring text unconditionally.
    raw_images = data.get("images")
    images = [img for img in raw_images if isinstance(img, dict) and img.get("data")] if isinstance(raw_images, list) else []
    images = [img for img in images if len(img["data"]) <= _MAX_IMAGE_B64_CHARS]
    if not text and not images:
        return jsonify({"error": "empty text"}), 400

    def _run():
        import core.commands as commands
        try:
            commands.dispatch_command(text, images=images or None)
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True, name="text-dispatch").start()
    return jsonify({"ok": True})

@app.route("/api/session_history")
def api_session_history():
    """Read-only snapshot of the rolling conversation buffer (core.session's
    own _history, capped at MAX_HISTORY turns) — added for the mobile client,
    which polls this instead of holding a Socket.IO connection open. The web
    UI doesn't use this route; it renders off the 'log' Socket.IO event
    (core.server.SocketIOLogHandler) as replies happen in real time."""
    import core.session as session_mod
    try:
        history = session_mod._get_history_snapshot()
        return jsonify({"history": history})
    except Exception as exc:
        logger.error("Failed to load session history: %s", exc)
        return jsonify({"error": str(exc)}), 500

@app.route("/api/mute", methods=["POST"])
def api_mute():
    import core.listener as listener
    listener.set_muted(True)
    socketio.emit("mute_state", {"muted": True})
    return jsonify({"ok": True, "muted": True})


@app.route("/api/unmute", methods=["POST"])
def api_unmute():
    import core.listener as listener
    listener.set_muted(False)
    socketio.emit("mute_state", {"muted": False})
    return jsonify({"ok": True, "muted": False})


@app.route("/api/mute_state")
def api_mute_state():
    import core.listener as listener
    return jsonify({"muted": listener.is_muted()})

@app.route("/api/tts_mute", methods=["POST"])
def api_tts_mute():
    """Mute HUGO's spoken output only — the mic and command processing keep
    running as normal, replies just stop being spoken (see core.voice's
    is_tts_muted() check in the actual playback functions)."""
    import core.voice as voice
    voice.set_tts_muted(True)
    socketio.emit("tts_mute_state", {"muted": True})
    return jsonify({"ok": True, "muted": True})


@app.route("/api/tts_unmute", methods=["POST"])
def api_tts_unmute():
    import core.voice as voice
    voice.set_tts_muted(False)
    socketio.emit("tts_mute_state", {"muted": False})
    return jsonify({"ok": True, "muted": False})


@app.route("/api/tts_mute_state")
def api_tts_mute_state():
    import core.voice as voice
    return jsonify({"muted": voice.is_tts_muted()})

@app.route("/api/feature_flags", methods=["GET"])
def api_feature_flags_get():
    """Snapshot of every toggle in the Ajustes panel — proactividad,
    busqueda_web, copiloto_hud, paneles_dinamicos, deteccion_tono,
    memoria_episodica. Backed by data/feature_flags.json via
    core.memory.get_feature_flags()."""
    import core.memory as memory
    return jsonify(memory.get_feature_flags())


@app.route("/api/feature_flags", methods=["POST"])
def api_feature_flags_set():
    """Toggle one feature flag. Body: {"name": "proactividad", "enabled": false}.
    Persists immediately (core.memory.set_feature_flag) and broadcasts the
    full updated snapshot over SocketIO so every other connected HUD tab
    stays in sync without a refresh — same pattern as tts_mute_state above."""
    import core.memory as memory
    data    = request.get_json(silent=True) or {}
    name    = data.get("name")
    enabled = data.get("enabled")
    if not isinstance(name, str) or not isinstance(enabled, bool):
        return jsonify({"ok": False, "error": "expected {name: str, enabled: bool}"}), 400
    try:
        flags = memory.set_feature_flag(name, enabled)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    socketio.emit("feature_flags_state", flags)
    return jsonify({"ok": True, "flags": flags})


@app.route("/api/modules", methods=["GET"])
def api_modules_get():
    """Module registry snapshot (data/modules.json) — one entry per
    skills/ module: {status, version, installed_at, last_health_check,
    error}. Backed by core.module_manager.manager.get_status()."""
    import core.module_manager as module_manager
    return jsonify(module_manager.manager.get_status())


@app.route("/api/modules/<name>/enable", methods=["POST"])
def api_modules_enable(name):
    import core.module_manager as module_manager
    ok = module_manager.manager.enable(name)
    return jsonify({"ok": ok, "status": module_manager.manager.get_status().get(name)})


@app.route("/api/modules/<name>/disable", methods=["POST"])
def api_modules_disable(name):
    import core.module_manager as module_manager
    ok = module_manager.manager.disable(name)
    return jsonify({"ok": ok, "status": module_manager.manager.get_status().get(name)})


@app.route("/api/modules/catalog", methods=["GET"])
def api_modules_catalog_get():
    """Full capability catalog (data/modules_catalog.json) — every
    capability HUGO has, is building, or has planned, independent of the
    runtime registry above — PLUS a synthetic 'CREADO POR HUGO' entry for
    every module Joan asked HUGO to build directly in conversation, which
    has no catalog entry by design. Backed by
    core.module_manager.manager.get_catalog_with_ad_hoc() — see that
    method's own docstring for how it tells those apart from the
    original hand-built skills (calculator, weather, ...), which also
    have no catalog entry but were never Code-Engine-generated."""
    import core.module_manager as module_manager
    return jsonify(module_manager.manager.get_catalog_with_ad_hoc())


@app.route("/api/modules/catalog/<category>", methods=["GET"])
def api_modules_catalog_by_category(category):
    import core.module_manager as module_manager
    return jsonify(module_manager.manager.get_catalog_by_category(category))


def _catalog_entry(manager, catalog_id):
    return next((m for m in manager.get_catalog() if m.get("id") == catalog_id), None)


@app.route("/api/modules/catalog/<catalog_id>/block", methods=["POST"])
def api_modules_catalog_block(catalog_id):
    """Body: {"blocked": bool} (default true). core.code_engine.CodeEngine
    refuses to create/update a blocked catalog entry — see
    ModuleManager.set_catalog_blocked."""
    import core.module_manager as module_manager
    data    = request.get_json(silent=True) or {}
    blocked = bool(data.get("blocked", True))
    ok = module_manager.manager.set_catalog_blocked(catalog_id, blocked)
    return jsonify({"ok": ok, "entry": _catalog_entry(module_manager.manager, catalog_id) if ok else None})


@app.route("/api/modules/catalog/<catalog_id>/priority", methods=["POST"])
def api_modules_catalog_priority(catalog_id):
    """Body: {"priority": int}. Lower number = higher priority."""
    import core.module_manager as module_manager
    data = request.get_json(silent=True) or {}
    try:
        priority = int(data["priority"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "expected {priority: int}"}), 400
    ok = module_manager.manager.set_catalog_priority(catalog_id, priority)
    return jsonify({"ok": ok, "entry": _catalog_entry(module_manager.manager, catalog_id) if ok else None})


@app.route("/api/tasks", methods=["GET"])
def api_tasks_get():
    """Every task regardless of status (data/tasks.json) — backed by
    core.task_engine.task_engine.get_all_tasks()."""
    import core.task_engine as task_engine_mod
    return jsonify({"tasks": task_engine_mod.task_engine.get_all_tasks()})


@app.route("/api/tasks", methods=["POST"])
def api_tasks_create():
    """Body: {"goal": str, "steps": list[str], "priority": int=1, "created_by": str="joan"}."""
    import core.task_engine as task_engine_mod
    data  = request.get_json(silent=True) or {}
    goal  = (data.get("goal") or "").strip()
    steps = data.get("steps")
    if not goal or not isinstance(steps, list) or not steps:
        return jsonify({"ok": False, "error": "expected {goal: str, steps: list[str]}"}), 400
    priority   = data.get("priority", 1)
    created_by = data.get("created_by", "joan")
    task_id = task_engine_mod.task_engine.create_task(goal, steps, priority=priority, created_by=created_by)
    return jsonify({"ok": True, "task_id": task_id, "task": task_engine_mod.task_engine.get_task_status(task_id)})


@app.route("/api/tasks/<task_id>/block", methods=["POST"])
def api_tasks_block(task_id):
    """Body: {"reason": str}."""
    import core.task_engine as task_engine_mod
    data   = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip() or "Sin motivo especificado"
    ok = task_engine_mod.task_engine.block_task(task_id, reason)
    return jsonify({"ok": ok, "task": task_engine_mod.task_engine.get_task_status(task_id) if ok else None})


@app.route("/api/tasks/<task_id>/cancel", methods=["POST"])
def api_tasks_cancel(task_id):
    """Body (optional): {"reason": str}. A cancel is a fail with a
    caller-supplied (or default) reason — see core.task_engine.TaskEngine.fail_task."""
    import core.task_engine as task_engine_mod
    data   = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip() or "Cancelada por Joan"
    ok = task_engine_mod.task_engine.fail_task(task_id, reason)
    return jsonify({"ok": ok, "task": task_engine_mod.task_engine.get_task_status(task_id) if ok else None})


# ---------------------------------------------------------------------------
# Code Engine — generates/updates skills/ modules. create_module()/
# update_module() each make one or more LLM calls plus a sandboxed subprocess
# test, so they run in a background thread rather than blocking the request —
# same "fire and forget, poll/log for status" shape as /text_command above.
# ---------------------------------------------------------------------------

@app.route("/api/code-engine/create/<catalog_id>", methods=["POST"])
def api_code_engine_create(catalog_id):
    import core.code_engine as code_engine_mod

    def _run():
        try:
            code_engine_mod.code_engine.create_module(catalog_id)
        except Exception:
            code_engine_mod.logger.error("create_module(%s) raised unexpectedly", catalog_id, exc_info=True)

    threading.Thread(target=_run, daemon=True, name="code-engine-create").start()
    return jsonify({"ok": True, "started": True, "catalog_id": catalog_id})


@app.route("/api/code-engine/update/<module_name>", methods=["POST"])
def api_code_engine_update(module_name):
    """Body: {"change": str}."""
    import core.code_engine as code_engine_mod
    data   = request.get_json(silent=True) or {}
    change = (data.get("change") or "").strip()
    if not change:
        return jsonify({"ok": False, "error": "expected {change: str}"}), 400

    def _run():
        try:
            code_engine_mod.code_engine.update_module(module_name, change)
        except Exception:
            code_engine_mod.logger.error("update_module(%s) raised unexpectedly", module_name, exc_info=True)

    threading.Thread(target=_run, daemon=True, name="code-engine-update").start()
    return jsonify({"ok": True, "started": True, "module_name": module_name})


@app.route("/api/code-engine/log", methods=["GET"])
def api_code_engine_log():
    import core.code_engine as code_engine_mod
    try:
        with open(code_engine_mod.LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []
    return jsonify({"lines": [line.rstrip("\n") for line in lines[-50:]]})


# ---------------------------------------------------------------------------
# Procedural skills (core.skill_forge, data/procedural_skills.json) — the
# know-how SkillForge extracts from completed TaskEngine tasks. Distinct
# from the runnable skills/ modules core.module_manager tracks — see
# core/skill_forge.py's own module comment.
# ---------------------------------------------------------------------------

@app.route("/api/skills/procedural", methods=["GET"])
def api_skills_procedural_get():
    import core.skill_forge as skill_forge_mod
    return jsonify({"skills": skill_forge_mod.skill_forge.get_all_skills()})


@app.route("/api/skills/procedural/<skill_id>", methods=["GET"])
def api_skills_procedural_detail(skill_id):
    import core.skill_forge as skill_forge_mod
    skill = skill_forge_mod.skill_forge.get_skill(skill_id)
    if not skill:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify(skill)


@app.route("/api/skills/procedural/<skill_id>/apply", methods=["POST"])
def api_skills_procedural_apply(skill_id):
    import core.skill_forge as skill_forge_mod
    skill = skill_forge_mod.skill_forge.apply_skill(skill_id)
    if not skill:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({"ok": True, "skill": skill})


# ---------------------------------------------------------------------------
# Subagents (core.subagent, data/subagents.json) — read/cancel only here.
# Nothing spawns or executes a subagent over HTTP: spawning is
# TaskEngine.spawn_subagents_for_step()'s job, and execution only happens
# from scripts/reflective_mode.py's sleep phase — see core/subagent.py's
# own module comment for why.
# ---------------------------------------------------------------------------

@app.route("/api/subagents", methods=["GET"])
def api_subagents_get():
    import core.subagent as subagent_mod
    return jsonify({"active": subagent_mod.subagent_manager.get_active()})


@app.route("/api/subagents/<subagent_id>", methods=["GET"])
def api_subagents_detail(subagent_id):
    import core.subagent as subagent_mod
    result = subagent_mod.subagent_manager.get_result(subagent_id)
    if not result:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify(result)


@app.route("/api/subagents/<subagent_id>/cancel", methods=["POST"])
def api_subagents_cancel(subagent_id):
    import core.subagent as subagent_mod
    ok = subagent_mod.subagent_manager.cancel(subagent_id)
    return jsonify({"ok": ok})


# ---------------------------------------------------------------------------
# Code Engine Phase 1 — project_analyzer/file_system/code_search/editor/git
# tools (core/code_engine/tools/), gated by data/code_engine_permissions.json's
# allowed_project_paths (empty by default — every one of these routes 404s
# down to an empty/false result until Joan adds a path there herself; see
# core/code_engine/permissions.py). Completely separate from the skills/
# module-generation endpoints above (POST /api/code-engine/create|update) —
# those touch HUGO's own skills/, these touch whatever project path Joan
# points them at.
# ---------------------------------------------------------------------------

@app.route("/api/code-engine/analyze", methods=["POST"])
def api_code_engine_analyze():
    """Body: {"path": str}."""
    from core.code_engine.tool_manager import tool_manager
    data = request.get_json(silent=True) or {}
    path = data.get("path")
    if not path:
        return jsonify({"ok": False, "error": "expected {path: str}"}), 400
    analyzer = tool_manager.get_tool("project_analyzer")
    if analyzer is None:
        return jsonify({"ok": False, "error": "project_analyzer tool unavailable"}), 500
    return jsonify(analyzer.analyze(path))


@app.route("/api/code-engine/tools", methods=["GET"])
def api_code_engine_tools():
    from core.code_engine.tool_manager import tool_manager
    return jsonify({"tools": tool_manager.list_tools()})


@app.route("/api/code-engine/search", methods=["POST"])
def api_code_engine_search():
    """Body: {"path": str, "query": str, "type": "text"|"regex"|"function"|"class"}."""
    from core.code_engine.tool_manager import tool_manager
    data = request.get_json(silent=True) or {}
    path, query, kind = data.get("path"), data.get("query"), data.get("type", "text")
    if not path or not query:
        return jsonify({"ok": False, "error": "expected {path: str, query: str, type?: str}"}), 400
    search = tool_manager.get_tool("code_search")
    if search is None:
        return jsonify({"ok": False, "error": "code_search tool unavailable"}), 500
    if kind == "regex":
        results = search.search_regex(path, query)
    elif kind == "function":
        results = search.find_function(path, query)
    elif kind == "class":
        results = search.find_class(path, query)
    else:
        results = search.search_text(path, query)
    return jsonify({"results": results})


@app.route("/api/code-engine/git/checkpoint", methods=["POST"])
def api_code_engine_git_checkpoint():
    """Body: {"path": str, "label": str}."""
    from core.code_engine.tool_manager import tool_manager
    data = request.get_json(silent=True) or {}
    path, label = data.get("path"), data.get("label", "")
    if not path:
        return jsonify({"ok": False, "error": "expected {path: str, label?: str}"}), 400
    git_tool = tool_manager.get_tool("git")
    if git_tool is None:
        return jsonify({"ok": False, "error": "git tool unavailable"}), 500
    commit_hash = git_tool.checkpoint(path, label)
    return jsonify({"ok": bool(commit_hash), "hash": commit_hash or None})


@app.route("/api/code-engine/git/rollback", methods=["POST"])
def api_code_engine_git_rollback():
    """Body: {"path": str, "hash": str}."""
    from core.code_engine.tool_manager import tool_manager
    data = request.get_json(silent=True) or {}
    path, commit_hash = data.get("path"), data.get("hash")
    if not path or not commit_hash:
        return jsonify({"ok": False, "error": "expected {path: str, hash: str}"}), 400
    git_tool = tool_manager.get_tool("git")
    if git_tool is None:
        return jsonify({"ok": False, "error": "git tool unavailable"}), 500
    ok = git_tool.rollback_to_checkpoint(path, commit_hash)
    return jsonify({"ok": ok})


# ---------------------------------------------------------------------------
# Code Engine Phase 2 — Shell/DependencyManager/Testing. shell.run() and
# deps.install() are both denied unless Joan explicitly flips 'shell'/
# 'install_dependencies' to true in data/code_engine_permissions.json
# first — these routes don't grant anything themselves, they're just a
# transport for tools that already refuse by default.
# ---------------------------------------------------------------------------

@app.route("/api/code-engine/shell/run", methods=["POST"])
def api_code_engine_shell_run():
    """Body: {"command": str, "cwd": str}."""
    from core.code_engine.tool_manager import tool_manager
    data = request.get_json(silent=True) or {}
    command, cwd = data.get("command"), data.get("cwd")
    if not command or not cwd:
        return jsonify({"ok": False, "error": "expected {command: str, cwd: str}"}), 400
    shell = tool_manager.get_tool("shell")
    if shell is None:
        return jsonify({"ok": False, "error": "shell tool unavailable"}), 500
    return jsonify(shell.run(command, cwd))


@app.route("/api/code-engine/deps/detect", methods=["POST"])
def api_code_engine_deps_detect():
    """Body: {"path": str}."""
    from core.code_engine.tool_manager import tool_manager
    data = request.get_json(silent=True) or {}
    path = data.get("path")
    if not path:
        return jsonify({"ok": False, "error": "expected {path: str}"}), 400
    dm = tool_manager.get_tool("dependency_manager")
    if dm is None:
        return jsonify({"ok": False, "error": "dependency_manager tool unavailable"}), 500
    return jsonify(dm.detect(path))


@app.route("/api/code-engine/deps/install", methods=["POST"])
def api_code_engine_deps_install():
    """Body: {"path": str, "package": str (optional)}."""
    from core.code_engine.tool_manager import tool_manager
    data = request.get_json(silent=True) or {}
    path, package = data.get("path"), data.get("package")
    if not path:
        return jsonify({"ok": False, "error": "expected {path: str, package?: str}"}), 400
    dm = tool_manager.get_tool("dependency_manager")
    if dm is None:
        return jsonify({"ok": False, "error": "dependency_manager tool unavailable"}), 500
    return jsonify({"ok": dm.install(path, package)})


@app.route("/api/code-engine/test/run", methods=["POST"])
def api_code_engine_test_run():
    """Body: {"path": str, "file": str (optional), "test": str (optional)}."""
    from core.code_engine.tool_manager import tool_manager
    data = request.get_json(silent=True) or {}
    path = data.get("path")
    if not path:
        return jsonify({"ok": False, "error": "expected {path: str}"}), 400
    testing = tool_manager.get_tool("testing")
    if testing is None:
        return jsonify({"ok": False, "error": "testing tool unavailable"}), 500
    if data.get("test"):
        result = testing.run_test(path, data["test"])
    elif data.get("file"):
        result = testing.run_file(path, data["file"])
    else:
        result = testing.run_all(path)
    return jsonify(result)


@app.route("/api/code-engine/shell/log", methods=["GET"])
def api_code_engine_shell_log():
    from core.code_engine.tools.shell import SHELL_LOG_PATH
    try:
        with open(SHELL_LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []
    return jsonify({"lines": [line.rstrip("\n") for line in lines[-100:]]})


# ---------------------------------------------------------------------------
# Code Engine Phase 3 — Orchestrator/Planner/CheckpointManager. orchestrate()
# runs the full autonomous cycle (plan -> checkpoint -> execute -> debug/
# retry -> test -> commit -> escalate) which can involve many LLM calls and
# take minutes, so it's fire-and-forget on a daemon thread same as
# create/update above — poll GET /api/code-engine/plans/<id> for progress.
# ---------------------------------------------------------------------------

@app.route("/api/code-engine/orchestrate", methods=["POST"])
def api_code_engine_orchestrate():
    """Body: {"goal": str, "project_path": str}."""
    from core.code_engine.tool_manager import tool_manager
    data = request.get_json(silent=True) or {}
    goal, project_path = data.get("goal"), data.get("project_path")
    if not goal or not project_path:
        return jsonify({"ok": False, "error": "expected {goal: str, project_path: str}"}), 400
    orchestrator = tool_manager.get_tool("orchestrator")
    if orchestrator is None:
        return jsonify({"ok": False, "error": "orchestrator tool unavailable"}), 500

    def _run():
        try:
            orchestrator.execute_goal(goal, project_path)
        except Exception:
            import core.code_engine as code_engine_mod
            code_engine_mod.logger.error("orchestrate(%r) raised unexpectedly", goal, exc_info=True)

    threading.Thread(target=_run, daemon=True, name="code-engine-orchestrate").start()
    return jsonify({"ok": True, "started": True})


@app.route("/api/code-engine/plans", methods=["GET"])
def api_code_engine_plans():
    from core.code_engine.tool_manager import tool_manager
    planner = tool_manager.get_tool("planner")
    if planner is None:
        return jsonify({"ok": False, "error": "planner tool unavailable"}), 500
    return jsonify({"plans": planner.get_all_plans()})


@app.route("/api/code-engine/plans/<plan_id>", methods=["GET"])
def api_code_engine_plan_detail(plan_id):
    from core.code_engine.tool_manager import tool_manager
    planner = tool_manager.get_tool("planner")
    if planner is None:
        return jsonify({"ok": False, "error": "planner tool unavailable"}), 500
    plan = planner.load_plan(plan_id)
    if not plan:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({"plan": plan, "current_step": planner.next_step(plan)})


@app.route("/api/code-engine/plans/<plan_id>/cancel", methods=["POST"])
def api_code_engine_plan_cancel(plan_id):
    from core.code_engine.tool_manager import tool_manager
    planner = tool_manager.get_tool("planner")
    if planner is None:
        return jsonify({"ok": False, "error": "planner tool unavailable"}), 500
    return jsonify({"ok": planner.cancel_plan(plan_id)})


@app.route("/api/code-engine/checkpoints", methods=["GET"])
def api_code_engine_checkpoints():
    """Body or query string: {"path": str}."""
    from core.code_engine.tool_manager import tool_manager
    data = request.get_json(silent=True) or {}
    path = data.get("path") or request.args.get("path")
    if not path:
        return jsonify({"ok": False, "error": "expected {path: str}"}), 400
    cm = tool_manager.get_tool("checkpoint_manager")
    if cm is None:
        return jsonify({"ok": False, "error": "checkpoint_manager tool unavailable"}), 500
    return jsonify({"checkpoints": cm.list_checkpoints(path)})


@app.route("/api/code-engine/checkpoints/rollback", methods=["POST"])
def api_code_engine_checkpoints_rollback():
    """Body: {"path": str, "hash": str, "confirm": bool (optional)}."""
    from core.code_engine.tool_manager import tool_manager
    data = request.get_json(silent=True) or {}
    path, commit_hash = data.get("path"), data.get("hash")
    if not path or not commit_hash:
        return jsonify({"ok": False, "error": "expected {path: str, hash: str}"}), 400
    cm = tool_manager.get_tool("checkpoint_manager")
    if cm is None:
        return jsonify({"ok": False, "error": "checkpoint_manager tool unavailable"}), 500
    ok = cm.rollback(path, commit_hash, confirm=bool(data.get("confirm", False)))
    return jsonify({"ok": ok})


# ---------------------------------------------------------------------------
# Code Engine Phase 4 — CodeReviewer/DocsBrowser/CodeMemory. docs/* routes
# 404 down to empty results (never an exception) until Joan flips
# 'internet' to true in data/code_engine_permissions.json — same
# fail-closed shape every other Phase 1-3 route already has for its own
# permission. memory/<path> uses Flask's <path:...> converter (not the
# default <path>) since a real filesystem path contains slashes.
# ---------------------------------------------------------------------------

@app.route("/api/code-engine/review", methods=["POST"])
def api_code_engine_review():
    """Body: {"path": str, "since_checkpoint": str (optional — full
    project review if omitted)}."""
    from core.code_engine.tool_manager import tool_manager
    data = request.get_json(silent=True) or {}
    path, since_checkpoint = data.get("path"), data.get("since_checkpoint")
    if not path:
        return jsonify({"ok": False, "error": "expected {path: str, since_checkpoint?: str}"}), 400
    reviewer = tool_manager.get_tool("code_reviewer")
    if reviewer is None:
        return jsonify({"ok": False, "error": "code_reviewer tool unavailable"}), 500
    report = reviewer.review_changes(path, since_checkpoint) if since_checkpoint else reviewer.review_full_project(path)
    return jsonify(report)


@app.route("/api/code-engine/flagged", methods=["GET"])
def api_code_engine_flagged():
    """Modules CodeEngine has created or modified (module_manager.
    ModuleManager.get_hugo_flagged_modules(), reading the hugo_review_flag
    every create_module()/create_ad_hoc_module()/update_module() stamps
    onto its manifest — see core.code_engine._stamp_hugo_review_flag) —
    a quick way to pull just the LLM-generated/-modified skills for a
    code-error review pass instead of every module in skills/."""
    import core.module_manager as module_manager_mod
    return jsonify({"modules": module_manager_mod.manager.get_hugo_flagged_modules()})


@app.route("/api/code-engine/review/<plan_id>", methods=["GET"])
def api_code_engine_review_for_plan(plan_id):
    """Last self-review report Orchestrator ran for this plan (see
    orchestrator.py's execute_goal() — stored on the plan itself as
    'last_review')."""
    from core.code_engine.tool_manager import tool_manager
    planner = tool_manager.get_tool("planner")
    if planner is None:
        return jsonify({"ok": False, "error": "planner tool unavailable"}), 500
    plan = planner.load_plan(plan_id)
    if not plan:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({"review": plan.get("last_review")})


@app.route("/api/code-engine/docs/search", methods=["POST"])
def api_code_engine_docs_search():
    """Body: {"query": str, "language": str (optional)}."""
    from core.code_engine.tool_manager import tool_manager
    data = request.get_json(silent=True) or {}
    query, language = data.get("query"), data.get("language")
    if not query:
        return jsonify({"ok": False, "error": "expected {query: str, language?: str}"}), 400
    docs = tool_manager.get_tool("docs_browser")
    if docs is None:
        return jsonify({"ok": False, "error": "docs_browser tool unavailable"}), 500
    return jsonify({"results": docs.search_docs(query, language)})


@app.route("/api/code-engine/docs/error", methods=["POST"])
def api_code_engine_docs_error():
    """Body: {"error": str, "language": str}."""
    from core.code_engine.tool_manager import tool_manager
    data = request.get_json(silent=True) or {}
    error, language = data.get("error"), data.get("language")
    if not error or not language:
        return jsonify({"ok": False, "error": "expected {error: str, language: str}"}), 400
    docs = tool_manager.get_tool("docs_browser")
    if docs is None:
        return jsonify({"ok": False, "error": "docs_browser tool unavailable"}), 500
    return jsonify(docs.research_error(error, language))


@app.route("/api/code-engine/memory/preferences", methods=["GET"])
def api_code_engine_memory_preferences():
    """Registered BEFORE /api/code-engine/memory/<path:...> below so
    'preferences' is never swallowed by that catch-all path converter."""
    from core.code_engine.tool_manager import tool_manager
    code_memory = tool_manager.get_tool("code_memory")
    if code_memory is None:
        return jsonify({"ok": False, "error": "code_memory tool unavailable"}), 500
    return jsonify({"preferences": code_memory.recall_preferences()})


@app.route("/api/code-engine/memory/<path:project_path>", methods=["GET"])
def api_code_engine_memory_get(project_path):
    from core.code_engine.tool_manager import tool_manager
    code_memory = tool_manager.get_tool("code_memory")
    if code_memory is None:
        return jsonify({"ok": False, "error": "code_memory tool unavailable"}), 500
    memory_entry = code_memory.recall_project("/" + project_path)
    if memory_entry is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({"memory": memory_entry})


@app.route("/api/code-engine/memory/<path:project_path>", methods=["DELETE"])
def api_code_engine_memory_forget(project_path):
    from core.code_engine.tool_manager import tool_manager
    code_memory = tool_manager.get_tool("code_memory")
    if code_memory is None:
        return jsonify({"ok": False, "error": "code_memory tool unavailable"}), 500
    return jsonify({"ok": code_memory.forget_project("/" + project_path)})


# ---------------------------------------------------------------------------
# Code Engine Phase 5 — Deployer. Every route below 404s down to a plain
# {"success": false, ...}/{"ok": false, ...} until Joan flips 'deploy' to
# true in data/code_engine_permissions.json for the target path — same
# fail-closed shape as every other gated Phase 1-4 route.
# ---------------------------------------------------------------------------

@app.route("/api/code-engine/deploy", methods=["POST"])
def api_code_engine_deploy():
    """Body: {"path": str, "target": str (optional, default 'local')}."""
    from core.code_engine.tool_manager import tool_manager
    data = request.get_json(silent=True) or {}
    path, target = data.get("path"), data.get("target", "local")
    if not path:
        return jsonify({"success": False, "error": "expected {path: str, target?: str}"}), 400
    deployer = tool_manager.get_tool("deployer")
    if deployer is None:
        return jsonify({"success": False, "error": "deployer tool unavailable"}), 500
    return jsonify(deployer.deploy(path, target))


@app.route("/api/code-engine/deployments", methods=["GET"])
def api_code_engine_deployments():
    """Query string: {"path": str}."""
    from core.code_engine.tool_manager import tool_manager
    path = request.args.get("path")
    if not path:
        return jsonify({"ok": False, "error": "expected ?path=..."}), 400
    deployer = tool_manager.get_tool("deployer")
    if deployer is None:
        return jsonify({"ok": False, "error": "deployer tool unavailable"}), 500
    return jsonify({"deployments": deployer.list_deployments(path)})


@app.route("/api/code-engine/deployments/<deploy_id>", methods=["GET"])
def api_code_engine_deployment_detail(deploy_id):
    """Single deployment + a fresh health check (per spec: '→ single
    deployment + health')."""
    from core.code_engine.tool_manager import tool_manager
    deployer = tool_manager.get_tool("deployer")
    if deployer is None:
        return jsonify({"ok": False, "error": "deployer tool unavailable"}), 500
    record = deployer.get_deployment(deploy_id)
    if record is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({"deployment": record, "health": deployer.health_check(deploy_id)})


@app.route("/api/code-engine/deployments/<deploy_id>/rollback", methods=["POST"])
def api_code_engine_deployment_rollback(deploy_id):
    from core.code_engine.tool_manager import tool_manager
    deployer = tool_manager.get_tool("deployer")
    if deployer is None:
        return jsonify({"ok": False, "error": "deployer tool unavailable"}), 500
    return jsonify({"ok": deployer.rollback_deploy(deploy_id)})


@app.route("/api/code-engine/deploy/hugo-module", methods=["POST"])
def api_code_engine_deploy_hugo_module():
    """Body: {"module_path": str}. Fire-and-forget, same pattern as
    /api/code-engine/create|update|orchestrate above — the full safety
    gate (tests, review, sandbox health check) can take a while."""
    from core.code_engine.tool_manager import tool_manager
    data = request.get_json(silent=True) or {}
    module_path = data.get("module_path")
    if not module_path:
        return jsonify({"ok": False, "error": "expected {module_path: str}"}), 400
    deployer = tool_manager.get_tool("deployer")
    if deployer is None:
        return jsonify({"ok": False, "error": "deployer tool unavailable"}), 500

    def _run():
        try:
            deployer.deploy_hugo_module(module_path)
        except Exception:
            import core.code_engine as code_engine_mod
            code_engine_mod.logger.error("deploy_hugo_module(%r) raised unexpectedly", module_path, exc_info=True)

    threading.Thread(target=_run, daemon=True, name="code-engine-deploy-hugo-module").start()
    return jsonify({"ok": True, "started": True})


@app.route("/api/mic_stop", methods=["POST"])
def api_mic_stop():
    """Stop the sounddevice capture stream so the macOS orange mic dot disappears.

    Called by the Electron Tray when the user mutes — pauses PortAudio so
    CoreAudio releases the input unit and the menu bar indicator clears.
    """
    import core.listener as listener
    listener.mic_stop()
    socketio.emit("mic_stream", {"active": False})
    return jsonify({"ok": True, "streaming": False})


@app.route("/api/mic_start", methods=["POST"])
def api_mic_start():
    """Resume the sounddevice capture stream after a mic_stop() call.

    Called by the Electron Tray when the user unmutes — PortAudio restarts and
    the orange mic indicator reappears naturally as CoreAudio begins capturing.
    """
    import core.listener as listener
    listener.mic_start()
    socketio.emit("mic_stream", {"active": True})
    return jsonify({"ok": True, "streaming": True})

@app.route("/api/ready")
def api_ready():
    """Polled by the launcher to know when Jarvis is fully initialized."""
    return jsonify({"ready": _server._is_ready})


@app.route("/api/mic_status")
def api_mic_status_jarvis():
    """Return current macOS microphone permission status."""
    try:
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio
        code = AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio)
        status = {0: "not_determined", 1: "restricted", 2: "denied", 3: "authorized"}.get(code, "unknown")
    except ImportError:
        status = "unknown"
    except Exception:
        status = "unknown"
    return jsonify({"mic_status": status})

@app.route("/api/reload", methods=["POST"])
def api_reload():
    """Force every connected HUD client to perform a hard page reload.

    Useful after a frontend deployment: bump the SW cache key in sw.js, then
    POST to this endpoint so all currently-open tabs reload and pick up the
    fresh assets without waiting for the browser's own SW update cycle.
    """
    emit_force_reload()
    return jsonify({"ok": True})


@app.route("/api/mode", methods=["GET"])
def api_mode_get():
    """Return the current listen mode ('wake_word' | 'conversation')."""
    import core.listener as listener
    return jsonify({"mode": listener.get_listen_mode()})


@app.route("/api/mode", methods=["POST", "OPTIONS"])
def api_mode_set():
    """Switch listen mode.  Body: {"mode": "wake_word" | "conversation"}."""
    if request.method == "OPTIONS":
        return "", 204
    import core.listener as listener
    data = request.get_json(force=True, silent=True) or {}
    mode = data.get("mode", "")
    if mode not in ("wake_word", "conversation"):
        return jsonify({"error": "mode must be 'wake_word' or 'conversation'"}), 400
    listener.set_listen_mode(mode)
    return jsonify({"ok": True, "mode": mode})
