# DEPLOYER — Phase 5, the final stage of the auto-development loop:
# build -> deploy -> health_check -> (rollback if needed), plus the
# LIRA-module-specific path that closes the loop back into
# core.module_manager.ManagerModule's public interface (install/update/
# rollback) — see deploy_lira_module()/update_lira_module()'s own
# docstrings for the exact safety gate.
#
# 'local' is the only deploy target actually implemented — a real remote/
# cloud target (systemd, a PaaS API, container orchestration) would be a
# whole new subsystem this phase's spec doesn't ask for and this codebase
# has no existing client for (same "don't reach past what's asked"
# reasoning as Phase 4's DocsBrowser reusing the existing search stack
# instead of adding a new one). "Deploying locally" here means: build the
# project, copy the resulting artifact into its own versioned directory
# under DEPLOYMENTS_DIR, and record it — for a lira_module deploy, the
# real "deployment" is calling ModuleManager.install()/update(), which is
# what actually makes the module live; the local artifact copy exists for
# every deploy type so rollback_deploy() has something to restore either
# way.
import datetime
import json
import logging
import os
import shutil
import subprocess
import threading
import time

from core.code_engine.tool_base import CodeEngineTool
from core.code_engine.permissions import check_permission

logger = logging.getLogger("code_engine")

DEPLOYMENTS_PATH = "data/deployments.json"
DEPLOYMENTS_DIR  = "data/code_engine_deployments"

_IGNORE_DIRS = {".git", "node_modules", "__pycache__", "venv", ".venv", "dist", "build", ".next", "target"}

# Testing.run_all()'s own exact string for "this project has no detectable
# test framework at all" (pytest/npm test) — see core.code_engine.tools.
# testing.Testing._detect_test_command. Treated as a soft-pass rather than
# a hard block in deploy_lira_module()'s safety gate: skills/ modules are
# simple LiraSkill classes with no pytest suite today, and treating
# "no test framework" the same as "tests exist and failed" would make the
# Phase 5 loop unable to ever deploy anything — see that method's own
# docstring for the full reasoning.
_NO_TEST_FRAMEWORK_ERROR = "no se detectó un framework de pruebas (pytest/npm test)"


class Deployer(CodeEngineTool):
    name = "deployer"
    description = "Compila, empaqueta, despliega (local), verifica y revierte — incluye el cierre del ciclo con ModuleManager para módulos de LIRA."
    version = "1.0"

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def ping(self) -> bool:
        return True

    # ── persistence ──────────────────────────────────────────────────────

    def _load(self) -> dict:
        try:
            with open(DEPLOYMENTS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {"deployments": []}
        if not isinstance(data, dict) or not isinstance(data.get("deployments"), list):
            return {"deployments": []}
        return data

    def _save_locked(self, data: dict) -> None:
        os.makedirs(os.path.dirname(DEPLOYMENTS_PATH) or ".", exist_ok=True)
        with open(DEPLOYMENTS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _next_id(self, data: dict) -> str:
        n = len(data["deployments"]) + 1
        existing = {d["id"] for d in data["deployments"]}
        deploy_id = f"deploy_{n:03d}"
        while deploy_id in existing:
            n += 1
            deploy_id = f"deploy_{n:03d}"
        return deploy_id

    def _find(self, data: dict, deploy_id: str) -> dict | None:
        return next((d for d in data["deployments"] if d["id"] == deploy_id), None)

    def _active_deployment_for(self, data: dict, project_path: str) -> dict | None:
        candidates = [d for d in data["deployments"] if d["project_path"] == project_path and d["status"] == "active"]
        return candidates[-1] if candidates else None

    # ── detection / build ────────────────────────────────────────────────

    def detect_deploy_type(self, project_path: str) -> str:
        allowed, _ = check_permission("read", project_path)
        if not allowed:
            return ""
        # lira_module takes priority over every other signal: a manifest
        # under skills/manifests/<name>/module.json is unambiguous, and a
        # plain requirements.txt/package.json inside JarvisLite's own repo
        # would otherwise misclassify it as a generic python_module. Strips
        # a '.py' suffix so this matches both a module FILE path
        # ('skills/calculator.py', what deploy_lira_module() itself takes)
        # and a bare module name/directory ('skills/calculator') —
        # manifest directories are named without the extension (see
        # skills/manifests/*).
        base = os.path.basename(os.path.normpath(project_path))
        if base.endswith(".py"):
            base = base[:-3]
        if os.path.isfile(os.path.join("skills", "manifests", base, "module.json")):
            return "lira_module"
        if os.path.isfile(os.path.join(project_path, "Dockerfile")):
            return "docker"
        if os.path.isfile(os.path.join(project_path, "package.json")):
            return "node_app"
        if any(
            os.path.isfile(os.path.join(project_path, f))
            for f in ("requirements.txt", "pyproject.toml", "setup.py", "Pipfile")
        ):
            return "python_module"
        if os.path.isfile(os.path.join(project_path, "index.html")):
            return "static"
        return "python_module" if os.path.isdir(project_path) else ""

    def _py_compile_one(self, fpath: str, errors: list) -> None:
        result = subprocess.run(
            ["python3", "-m", "py_compile", fpath], capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            errors.append(f"{fpath}: {result.stderr.strip()}")

    def _py_compile_check(self, project_path: str) -> tuple:
        """py_compile every .py file — stdlib-only, no new dependency.
        Real signal (a syntax error fails this), not a rubber stamp.
        `project_path` may be a single module FILE (deploy_lira_module()'s
        own 'skills/<name>.py' shape — os.walk() on a bare file path
        yields nothing at all, which silently "passed" with zero files
        ever actually checked until this branch was added) or a directory
        to walk."""
        errors: list = []
        if os.path.isfile(project_path):
            if project_path.endswith(".py"):
                self._py_compile_one(project_path, errors)
            return (not errors, "\n".join(errors) or "compiló correctamente")

        for root, dirs, files in os.walk(project_path):
            dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS and not d.startswith(".")]
            for f in files:
                if not f.endswith(".py"):
                    continue
                self._py_compile_one(os.path.join(root, f), errors)
        return (not errors, "\n".join(errors) or "compiló correctamente")

    def build(self, project_path: str) -> dict:
        allowed, reason = check_permission("read", project_path)
        if not allowed:
            return {"success": False, "output": reason, "artifact_path": None}

        deploy_type = self.detect_deploy_type(project_path)

        if deploy_type in ("lira_module", "python_module"):
            ok, output = self._py_compile_check(project_path)
            return {"success": ok, "output": output, "artifact_path": project_path if ok else None}

        if deploy_type == "node_app":
            try:
                with open(os.path.join(project_path, "package.json"), "r", encoding="utf-8") as f:
                    pkg = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                return {"success": False, "output": str(e), "artifact_path": None}
            has_build_script = "build" in (pkg.get("scripts") or {})
            cmd = ["npm", "run", "build"] if has_build_script else ["npm", "install"]
            try:
                result = subprocess.run(cmd, cwd=project_path, capture_output=True, text=True, timeout=300)
            except Exception as e:
                return {"success": False, "output": str(e), "artifact_path": None}
            artifact = project_path
            for candidate in ("dist", "build"):
                if os.path.isdir(os.path.join(project_path, candidate)):
                    artifact = os.path.join(project_path, candidate)
                    break
            output = (result.stdout or "") + (result.stderr or "")
            return {"success": result.returncode == 0, "output": output[-3000:], "artifact_path": artifact if result.returncode == 0 else None}

        if deploy_type == "docker":
            tag = f"lira-deploy-{os.path.basename(os.path.normpath(project_path)).lower()}"
            try:
                result = subprocess.run(
                    ["docker", "build", "-t", tag, "."], cwd=project_path, capture_output=True, text=True, timeout=600,
                )
            except Exception as e:
                return {"success": False, "output": str(e), "artifact_path": None}
            output = (result.stdout or "") + (result.stderr or "")
            return {"success": result.returncode == 0, "output": output[-3000:], "artifact_path": tag if result.returncode == 0 else None}

        if deploy_type == "static":
            has_index = os.path.isfile(os.path.join(project_path, "index.html"))
            return {
                "success": has_index,
                "output": "sitio estático — sin paso de compilación" if has_index else "no se encontró index.html",
                "artifact_path": project_path if has_index else None,
            }

        return {"success": False, "output": f"tipo de proyecto no reconocido en {project_path!r}", "artifact_path": None}

    # ── deploy / health / rollback ───────────────────────────────────────

    def deploy(self, project_path: str, target: str = "local") -> dict:
        allowed, reason = check_permission("deploy", project_path)
        if not allowed:
            return {"success": False, "error": reason}
        if target != "local":
            return {"success": False, "error": f"target no soportado: {target!r} (solo 'local' está implementado)"}

        from core.code_engine.tool_manager import tool_manager
        checkpointer = tool_manager.get_tool("checkpoint_manager")
        starting_hash = ""
        if checkpointer:
            snapshot = checkpointer.create(project_path, "pre-deploy snapshot", reason=f"antes de deploy a {target}")
            starting_hash = snapshot.get("hash", "") if isinstance(snapshot, dict) else ""

        build_result = self.build(project_path)
        if not build_result.get("success"):
            return {"success": False, "error": f"build falló: {build_result.get('output', '')[:500]}"}

        deploy_type = self.detect_deploy_type(project_path)

        with self._lock:
            data = self._load()
            deploy_id = self._next_id(data)
            previous = self._active_deployment_for(data, project_path)

            dest = os.path.join(DEPLOYMENTS_DIR, deploy_id)
            artifact_source = build_result.get("artifact_path")
            local_artifact_path = artifact_source
            if artifact_source and os.path.isdir(artifact_source) and deploy_type != "docker":
                try:
                    os.makedirs(DEPLOYMENTS_DIR, exist_ok=True)
                    shutil.copytree(artifact_source, dest, ignore=shutil.ignore_patterns(*_IGNORE_DIRS))
                    local_artifact_path = dest
                except OSError as e:
                    logger.warning("Deployer: could not copy artifact for %s (%s) — recording source path instead", deploy_id, e)

            if previous:
                previous["status"] = "superseded"

            record = {
                "id": deploy_id,
                "project_path": project_path,
                "type": deploy_type,
                "module_name": None,
                "deployed_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "status": "active",
                "artifact_path": local_artifact_path,
                "previous_deploy_id": previous["id"] if previous else None,
                "checkpoint_hash": starting_hash or None,
                "health_checks": [],
            }
            data["deployments"].append(record)
            self._save_locked(data)

        return {"success": True, "url_or_path": local_artifact_path, "deploy_id": deploy_id}

    def health_check(self, deploy_id: str) -> dict:
        with self._lock:
            data = self._load()
            record = self._find(data, deploy_id)
            if record is None:
                return {"healthy": False, "checks": [{"name": "deployment_exists", "ok": False}], "response_time_ms": 0}

            started = time.monotonic()
            checks = []
            if record["type"] == "lira_module" and record.get("module_name"):
                import core.module_manager as module_manager_mod
                result = module_manager_mod.manager.health_check(record["module_name"])
                checks.append({"name": "module_health", "ok": bool(result.get("ok")), "detail": result.get("error")})
                healthy = bool(result.get("ok"))
            else:
                artifact = record.get("artifact_path")
                exists = bool(artifact) and os.path.exists(artifact)
                checks.append({"name": "artifact_exists", "ok": exists, "detail": artifact})
                healthy = exists
            response_time_ms = round((time.monotonic() - started) * 1000, 1)

            record["health_checks"].append({
                "healthy": healthy, "checks": checks, "response_time_ms": response_time_ms,
                "checked_at": datetime.datetime.now().isoformat(timespec="seconds"),
            })
            record["health_checks"] = record["health_checks"][-20:]
            self._save_locked(data)

        return {"healthy": healthy, "checks": checks, "response_time_ms": response_time_ms}

    def rollback_deploy(self, deploy_id: str) -> bool:
        with self._lock:
            data = self._load()
            record = self._find(data, deploy_id)
            if record is None or not record.get("previous_deploy_id"):
                logger.warning("Deployer: rollback_deploy(%s) — no previous deployment to roll back to", deploy_id)
                return False
            previous = self._find(data, record["previous_deploy_id"])
            if previous is None:
                return False

            project_path = record["project_path"]
            ok = True

            if record.get("checkpoint_hash"):
                from core.code_engine.tool_manager import tool_manager
                checkpointer = tool_manager.get_tool("checkpoint_manager")
                if checkpointer:
                    ok = checkpointer.rollback(project_path, record["checkpoint_hash"], confirm=True) and ok

            if record["type"] == "lira_module" and record.get("module_name"):
                self._resync_module_manager(record["module_name"])

            record["status"] = "rolled_back"
            previous["status"] = "active"
            self._save_locked(data)

        return ok

    def list_deployments(self, project_path: str) -> list:
        with self._lock:
            data = self._load()
        return [d for d in data["deployments"] if d["project_path"] == project_path]

    def get_deployment(self, deploy_id: str) -> dict | None:
        with self._lock:
            data = self._load()
        return self._find(data, deploy_id)

    # ── LIRA module-specific — closes the auto-development loop ─────────

    def _pre_deploy_gate(self, module_path: str, module_name: str) -> tuple:
        """The three checks deploy_lira_module()/update_lira_module() both
        require before ever touching ModuleManager — see their own
        docstrings for what happens on failure. Returns (ok: bool,
        reason: str)."""
        from core.code_engine.tool_manager import tool_manager

        testing = tool_manager.get_tool("testing")
        if testing:
            test_result = testing.run_all("skills")
            no_framework = test_result.get("error") == _NO_TEST_FRAMEWORK_ERROR
            if not test_result.get("ok") and not no_framework:
                return False, f"tests fallaron: {test_result.get('stderr', test_result.get('error', ''))[:500]}"

        reviewer = tool_manager.get_tool("code_reviewer")
        if reviewer:
            review = reviewer.review_file(module_path)
            if review.get("critical"):
                return False, f"revisión encontró {len(review['critical'])} problema(s) crítico(s): {review['summary']}"

        try:
            from core.code_engine import code_engine as code_engine_singleton
            sandbox_ok, sandbox_detail = code_engine_singleton._sandbox_test(module_path)
        except Exception as e:
            return False, f"sandbox health check falló: {e}"
        if not sandbox_ok:
            return False, f"sandbox health check falló: {sandbox_detail}"

        return True, "ok"

    def deploy_lira_module(self, module_path: str) -> bool:
        """The final step of the auto-development loop (see this module's
        own header comment for the full pipeline). Requires, in order:
        Testing.run_all('skills') passes (or no framework is detected at
        all — see _NO_TEST_FRAMEWORK_ERROR), CodeReviewer.review_file()
        finds zero critical issues, and a sandbox dry-run (reused from
        core.code_engine.CodeEngine._sandbox_test — the same subprocess-
        isolated check create_module()/update_module() already use, not a
        duplicate implementation) passes. Only then calls
        ModuleManager.install() — its PUBLIC interface, never touching
        its internal registry directly. Any failure blocks and notifies
        Joan via the same notification queue Orchestrator._escalate()
        uses; nothing here is installed on failure."""
        module_name = os.path.splitext(os.path.basename(module_path))[0]
        allowed, reason = check_permission("deploy", "skills")
        if not allowed:
            logger.warning("Deployer: denied deploy_lira_module(%s) (%s)", module_name, reason)
            return False

        from core.code_engine.tool_manager import tool_manager
        checkpointer = tool_manager.get_tool("checkpoint_manager")
        starting_hash = ""
        if checkpointer:
            snapshot = checkpointer.create("skills", f"pre-deploy: {module_name}", reason="antes de deploy_lira_module")
            starting_hash = snapshot.get("hash", "") if isinstance(snapshot, dict) else ""

        ok, reason = self._pre_deploy_gate(module_path, module_name)
        if not ok:
            self._notify_blocked(module_name, reason)
            return False

        import core.module_manager as module_manager_mod
        installed = module_manager_mod.manager.install(module_name)
        if not installed:
            self._notify_blocked(module_name, "ModuleManager.install() falló tras pasar todas las verificaciones")
            return False

        with self._lock:
            data = self._load()
            deploy_id = self._next_id(data)
            previous = self._active_deployment_for(data, "skills")
            if previous:
                previous["status"] = "superseded"
            data["deployments"].append({
                "id": deploy_id, "project_path": "skills", "type": "lira_module",
                "module_name": module_name,
                "deployed_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "status": "active", "artifact_path": module_path,
                "previous_deploy_id": previous["id"] if previous else None,
                "checkpoint_hash": starting_hash or None,
                "health_checks": [],
            })
            self._save_locked(data)

        health = self.health_check(deploy_id)
        if not health.get("healthy"):
            self.rollback_deploy(deploy_id)
            self._notify_blocked(module_name, "el health check posterior al deploy falló — se revirtió")
            return False

        return True

    def update_lira_module(self, module_name: str, new_path: str) -> bool:
        """Same safety gate as deploy_lira_module(), for an already-
        installed module being updated to the code at `new_path`. On any
        failure: the pre-deploy checkpoint (taken before `new_path`'s
        content ever touched skills/<module_name>.py) is restored via
        CheckpointManager, AND ModuleManager.rollback() is called so its
        own registry state (version/status) matches — 'rollback on
        failure' per spec, using only ModuleManager's public interface."""
        module_path = os.path.join("skills", f"{module_name}.py")
        allowed, reason = check_permission("deploy", "skills")
        if not allowed:
            logger.warning("Deployer: denied update_lira_module(%s) (%s)", module_name, reason)
            return False
        allowed_write, reason_write = check_permission("write", "skills")
        if not allowed_write:
            logger.warning("Deployer: denied update_lira_module(%s) write (%s)", module_name, reason_write)
            return False

        from core.code_engine.tool_manager import tool_manager
        checkpointer = tool_manager.get_tool("checkpoint_manager")
        starting_hash = ""
        if checkpointer:
            snapshot = checkpointer.create("skills", f"pre-update: {module_name}", reason="antes de update_lira_module")
            starting_hash = snapshot.get("hash", "") if isinstance(snapshot, dict) else ""

        editor = tool_manager.get_tool("editor")
        if editor is None:
            return False
        try:
            with open(new_path, "r", encoding="utf-8", errors="ignore") as f:
                new_code = f.read()
        except OSError as e:
            self._notify_blocked(module_name, f"no se pudo leer {new_path!r}: {e}")
            return False

        if os.path.abspath(new_path) != os.path.abspath(module_path):
            # A full-file replacement, not a substring swap — replace_text()
            # needs an existing 'old' string to match, which isn't the right
            # tool here. Write directly, backed by Editor's own _backup()
            # first (same discipline as every other Editor-driven mutation
            # in this package — see debugger.py's remove_temp_logging()).
            editor._backup(module_path)
            try:
                with open(module_path, "w", encoding="utf-8") as f:
                    f.write(new_code)
            except OSError as e:
                self._notify_blocked(module_name, f"no se pudo escribir {module_path!r}: {e}")
                return False

        import skills
        skills.reload_skills()

        ok, reason = self._pre_deploy_gate(module_path, module_name)
        if not ok:
            self._rollback_update(module_name, starting_hash)
            self._notify_blocked(module_name, reason)
            return False

        import core.module_manager as module_manager_mod
        updated = module_manager_mod.manager.update(module_name)
        if not updated:
            self._rollback_update(module_name, starting_hash)
            self._notify_blocked(module_name, "ModuleManager.update() falló tras pasar todas las verificaciones")
            return False

        with self._lock:
            data = self._load()
            deploy_id = self._next_id(data)
            previous = self._active_deployment_for(data, "skills")
            if previous:
                previous["status"] = "superseded"
            data["deployments"].append({
                "id": deploy_id, "project_path": "skills", "type": "lira_module",
                "module_name": module_name,
                "deployed_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "status": "active", "artifact_path": module_path,
                "previous_deploy_id": previous["id"] if previous else None,
                "checkpoint_hash": starting_hash or None,
                "health_checks": [],
            })
            self._save_locked(data)

        health = self.health_check(deploy_id)
        if not health.get("healthy"):
            self.rollback_deploy(deploy_id)
            self._notify_blocked(module_name, "el health check posterior a la actualización falló — se revirtió")
            return False

        return True

    def _rollback_update(self, module_name: str, checkpoint_hash: str) -> None:
        if not checkpoint_hash:
            return
        from core.code_engine.tool_manager import tool_manager
        checkpointer = tool_manager.get_tool("checkpoint_manager")
        if checkpointer:
            checkpointer.rollback("skills", checkpoint_hash, confirm=True)
        self._resync_module_manager(module_name)

    def _resync_module_manager(self, module_name: str) -> None:
        """Re-syncs ModuleManager's registry entry (version/status) with
        whatever is now on disk after a git-level checkpoint rollback —
        public interface only. Tries manager.rollback() first (it only
        actually restores anything if the LAST ModuleManager operation on
        this module was update() — see ModuleManager.rollback()'s own
        docstring: it reads an in-memory snapshot only update() writes).
        deploy_lira_module() calls install(), not update(), so that
        snapshot usually doesn't exist there — falling back to
        manager.install() re-registers whatever content the checkpoint
        rollback just restored, which is always correct regardless of
        which operation originally deployed it. Never lets either call's
        success/failure propagate — this is always best-effort, secondary
        to the checkpoint rollback that already reverted the actual file."""
        import core.module_manager as module_manager_mod
        if not module_manager_mod.manager.rollback(module_name):
            module_manager_mod.manager.install(module_name)
        try:
            import skills
            skills.reload_skills()
        except Exception:
            logger.warning("Deployer: reload_skills() failed after rollback", exc_info=True)

    def _notify_blocked(self, module_name: str, reason: str) -> None:
        try:
            from core import notifications as notifications_mod
            notifications_mod.create_notification(
                "code_engine",
                f"Deploy bloqueado: {module_name}",
                f"No se pudo desplegar el módulo '{module_name}' — {reason}. ¿Cómo procedo?",
            )
        except Exception:
            logger.error("Deployer: notification failed", exc_info=True)
        logger.info("[DEPLOYER] blocked — module=%r reason=%s", module_name, reason)
