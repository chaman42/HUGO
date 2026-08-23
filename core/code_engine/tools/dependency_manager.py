# DEPENDENCY MANAGER — detects a project's dependency file and installs
# packages, HARD-restricted to the project's own venv: _find_venv() only
# ever looks inside the given project path, and install() refuses
# outright (no subprocess call at all) if no venv is found there — it
# never falls back to system/global pip. Every install also requires the
# 'install_dependencies' permission (False by default).
#
# Trust gate (added 2026-08-10, same day as the deploy_hugo_module() live
# verification — see that session's own notes): 'install_dependencies'
# being on only proves JOAN is willing to let installs happen at all, not
# that any SPECIFIC package an LLM picks is one she'd actually approve —
# same gap Claude Code itself closes with a per-action permission prompt.
# A single, explicit package (install(path, package=...), the normal
# Orchestrator/API call shape — batch requirements.txt installs are left
# as before, out of scope here) now goes through TRUSTED_PACKAGES_PATH
# first: a match installs immediately (today's behavior, unchanged); no
# match means _request_install_approval() below runs instead of pip —
# looks the package up via DocsBrowser.research_package() (PyPI/npm
# metadata, gated by its own 'internet' permission separately), then
# blocks a TaskEngine task and fires a notification carrying that
# lookup + the package NAME, same 'HUGO mentions it next time we talk'
# delivery core.notifications._deliver_pending_notifications already uses
# for everything else. Joan's reply ('sí' / 'no' / 'sí, siempre') is
# handled by core.actions._execute_pending_confirm's own
# 'install_package_approval' branch — that's what actually calls
# _do_install() below, never this module reacting on its own.
import json
import logging
import os
import re
import subprocess

from core.code_engine.tool_base import CodeEngineTool
from core.code_engine.permissions import check_permission

logger = logging.getLogger("code_engine")

_DEPENDENCY_FILES = ("requirements.txt", "Pipfile", "pyproject.toml", "package.json")
_VENV_DIR_NAMES = ("venv", ".venv", "env")

_PACKAGE_SPEC_RE = re.compile(r"^([A-Za-z0-9_.\-]+)\s*==\s*([A-Za-z0-9_.\-]+)$")

TRUSTED_PACKAGES_PATH = "data/code_engine_trusted_packages.json"
PENDING_INSTALLS_PATH = "data/code_engine_pending_installs.json"


def _bare_name(package: str) -> str:
    """'requests>=2.31.0' -> 'requests', 'lodash@^4.17.21' -> 'lodash' —
    same idea as the sandbox runner's own dependency-name stripping in
    core.code_engine's _SANDBOX_RUNNER, just as a reusable function here."""
    name = (package or "").strip()
    scoped = name.startswith("@")   # npm scoped package, e.g. '@babel/core' — its own '@' isn't a version separator
    rest = name[1:] if scoped else name
    for sep in (">=", "==", "<=", "!=", "~=", ">", "<", "@"):
        if sep in rest:
            rest = rest.split(sep)[0]
            break
    return (("@" + rest) if scoped else rest).strip().strip('"\'').lower()


def _load_trusted_packages() -> dict:
    try:
        with open(TRUSTED_PACKAGES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def add_trusted_package(name: str, ecosystem: str = "pypi") -> None:
    """Called from core.actions._execute_pending_confirm when Joan's reply
    to an approval prompt includes 'siempre' — grows the allowlist so the
    same package never asks again. Best-effort; never raises."""
    try:
        data = _load_trusted_packages()
        key = ecosystem if ecosystem in ("pypi", "npm") else "pypi"
        packages = data.setdefault(key, [])
        bare = _bare_name(name)
        if bare and bare not in packages:
            packages.append(bare)
            os.makedirs(os.path.dirname(TRUSTED_PACKAGES_PATH) or ".", exist_ok=True)
            with open(TRUSTED_PACKAGES_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.error("add_trusted_package(%r, %r) failed", name, ecosystem, exc_info=True)


def _load_pending_installs() -> dict:
    try:
        with open(PENDING_INSTALLS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_pending_install(task_id: str, record: dict) -> None:
    data = _load_pending_installs()
    data[task_id] = record
    os.makedirs(os.path.dirname(PENDING_INSTALLS_PATH) or ".", exist_ok=True)
    with open(PENDING_INSTALLS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def pop_pending_install(task_id: str) -> dict | None:
    """Called once from core.actions._execute_pending_confirm when Joan
    resolves the prompt (either way) — removes it so the same record can
    never be replayed twice."""
    data = _load_pending_installs()
    record = data.pop(task_id, None)
    if record is not None:
        os.makedirs(os.path.dirname(PENDING_INSTALLS_PATH) or ".", exist_ok=True)
        with open(PENDING_INSTALLS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    return record


class DependencyManager(CodeEngineTool):
    name = "dependency_manager"
    description = "Detecta e instala dependencias, exclusivamente dentro del venv propio del proyecto."
    version = "1.0"

    def ping(self) -> bool:
        return True

    def _find_venv_python(self, path: str) -> str | None:
        """Only ever looks inside `path` — never system/global Python."""
        for name in _VENV_DIR_NAMES:
            candidate = os.path.join(path, name, "bin", "python")
            if os.path.isfile(candidate):
                return candidate
        return None

    def detect(self, path: str) -> dict:
        allowed, reason = check_permission("read", path)
        if not allowed:
            return {"error": reason}

        dependency_file = next(
            (f for f in _DEPENDENCY_FILES if os.path.isfile(os.path.join(path, f))), None,
        )
        packages = []
        if dependency_file == "requirements.txt":
            try:
                with open(os.path.join(path, dependency_file), "r", encoding="utf-8", errors="ignore") as f:
                    packages = [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]
            except OSError:
                pass
        elif dependency_file == "package.json":
            try:
                with open(os.path.join(path, dependency_file), "r", encoding="utf-8", errors="ignore") as f:
                    pkg = json.load(f)
                packages = sorted(
                    f"{name}@{ver}" for name, ver in {
                        **pkg.get("dependencies", {}), **pkg.get("devDependencies", {}),
                    }.items()
                )
            except (OSError, json.JSONDecodeError):
                pass

        venv_python = self._find_venv_python(path)
        return {
            "dependency_file": dependency_file,
            "packages": packages,
            "has_venv": venv_python is not None,
            "venv_python": venv_python,
        }

    def _installed_version(self, venv_python: str, name: str) -> str | None:
        try:
            result = subprocess.run(
                [venv_python, "-m", "pip", "show", name],
                capture_output=True, text=True, timeout=15,
            )
            m = re.search(r"^Version:\s*(\S+)", result.stdout, re.MULTILINE)
            return m.group(1) if m else None
        except Exception:
            return None

    def _check_changelog_before_update(self, venv_python: str, package: str) -> None:
        """Phase 4 auto-trigger: if `package` pins a specific version
        that's actually a CHANGE from what's currently installed,
        DocsBrowser.check_changelog() runs and flags breaking changes to
        Joan before pip ever touches anything. Best-effort and entirely
        non-blocking — no permission ('internet' off), no pinned version,
        or nothing currently installed all just skip this silently and
        installation proceeds exactly as before Phase 4."""
        m = _PACKAGE_SPEC_RE.match((package or "").strip())
        if not m:
            return
        name, to_version = m.group(1), m.group(2)
        from_version = self._installed_version(venv_python, name)
        if not from_version or from_version == to_version:
            return
        try:
            from core.code_engine.tool_manager import tool_manager
            docs = tool_manager.get_tool("docs_browser")
            if docs is None:
                return
            summary = docs.check_changelog(name, from_version, to_version)
            from core import notifications as notifications_mod
            notifications_mod.create_notification(
                "code_engine",
                f"Actualizando {name}: {from_version} -> {to_version}",
                summary,
            )
        except Exception:
            logger.warning("DependencyManager: check_changelog pass failed (continuing anyway)", exc_info=True)

    def install(self, path: str, package: str = None) -> bool:
        allowed, reason = check_permission("install_dependencies", path)
        if not allowed:
            logger.warning("DependencyManager: denied install() in %r (%s)", path, reason)
            return False

        venv_python = self._find_venv_python(path)
        if venv_python is None:
            logger.error(
                "DependencyManager: refusing install() in %r — no venv found "
                "(venv/.venv/env), never installs outside a project's own venv",
                path,
            )
            return False

        if package:
            self._check_changelog_before_update(venv_python, package)
            if _bare_name(package) not in (_load_trusted_packages().get("pypi") or []):
                self._request_install_approval(path, package)
                return False

        return self._do_install(path, venv_python, package)

    def _do_install(self, path: str, venv_python: str, package: str = None) -> bool:
        """The actual pip subprocess call — factored out of install() so
        core.actions._execute_pending_confirm's 'install_package_approval'
        branch can run this SAME code once Joan approves an untrusted
        package, instead of duplicating it. Callers here have already
        passed the permission/venv/trust checks; this never re-checks
        them, so it must never be called directly from outside this class
        or core.actions' approval handler."""
        try:
            from core.code_engine.tools.checkpoint_manager import CheckpointManager
            CheckpointManager().auto_checkpoint(path, f"instalar {package or 'requirements.txt'}")
        except Exception:
            logger.warning("DependencyManager: auto_checkpoint failed (continuing anyway)", exc_info=True)

        args = [venv_python, "-m", "pip", "install"]
        args += [package] if package else ["-r", "requirements.txt"]
        try:
            result = subprocess.run(args, cwd=path, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                logger.warning("DependencyManager.install(%r) failed: %s", path, result.stderr[-500:])
            return result.returncode == 0
        except Exception:
            logger.error("DependencyManager.install(%r) errored", path, exc_info=True)
            return False

    def _request_install_approval(self, path: str, package: str) -> None:
        """An untrusted package never installs itself — this blocks a
        TaskEngine task and fires a notification HUGO delivers verbatim
        next time Joan talks to her (core.notifications.
        _deliver_pending_notifications), same as every other pending-input
        task. The notification's `data` payload is what lets
        core.notifications wire up intent_mod._pending_action so Joan can
        just answer in conversation ('sí' / 'no' / 'sí, siempre') instead
        of going anywhere else — see that function's own docstring."""
        name = _bare_name(package)
        research = {}
        try:
            from core.code_engine.tool_manager import tool_manager
            docs = tool_manager.get_tool("docs_browser")
            if docs:
                research = docs.research_package(name, "pypi")
        except Exception:
            logger.warning("DependencyManager: research_package(%r) failed (continuing anyway)", name, exc_info=True)

        try:
            from core.task_engine import task_engine
            task_id = task_engine.create_task(
                f"Instalar paquete: {name}",
                [f"Instalar {package} en {path}"],
                priority=2, created_by="hugo",
            )
            task_engine.block_task(
                task_id,
                f"'{name}' no está en la lista de confianza — necesito tu aprobación antes de instalarlo.",
            )
        except Exception:
            logger.error("DependencyManager: failed to create/block approval task for %r", name, exc_info=True)
            return

        _save_pending_install(task_id, {"package": package, "path": path, "name": name})

        # `message` here is a plain, deterministic fallback ONLY — the
        # actual line Joan hears is phrased fresh from `data['research']`
        # (raw PyPI facts) at delivery time, in her current personality —
        # see core.notifications._deliver_pending_notifications, which
        # calls core.response._format_response() (the same Groq-based
        # "phrase this raw result naturally" helper every other tool
        # result already goes through) instead of a hand-written template.
        # This field only ever gets spoken if that call fails.
        try:
            from core import notifications as notifications_mod
            notifications_mod.create_notification(
                "code_engine_install_approval",
                f"¿Instalo el paquete '{name}'?",
                f"Quiero instalar el paquete '{name}', que no está en mi lista de confianza. "
                f"¿Lo instalo? Puedes decir sí, no, o sí siempre.",
                data={"task_id": task_id, "package": package, "path": path, "name": name, "research": research},
            )
        except Exception:
            logger.error("DependencyManager: failed to notify approval request for %r", name, exc_info=True)
