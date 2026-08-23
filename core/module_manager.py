# MODULE MANAGER — manages skills/ only. Never import from hugo_core/.
#
# (This codebase doesn't have a hugo_core/ subpackage — core/ is flat, and
# the loadable-capabilities layer lives at top-level skills/, not
# core/skills/. The rule still applies at the boundary that actually
# exists: this file imports nothing from core/ except the `skills` package
# below — no core.personality, core.memory*, core.commands, core.session,
# etc. Every read/write of feature-flag state goes through
# skills.is_loaded()/skills.set_enabled(), which is skills/__init__.py's
# own narrow window onto core.memory_flags — see that module's docstring.
# This way ModuleManager genuinely cannot reach personality/memory/
# conversation logic, even by accident.)
#
# A "module" here is exactly one skills/<name>.py + its manifest at
# skills/manifests/<name>/module.json — ModuleManager adds installable-unit
# bookkeeping (a version/status registry, health checks, isolation on
# failure) on top of the skills package's own discovery/loading, which it
# reuses rather than duplicating.
import datetime
import json
import logging
import os
import threading
import time

import skills

logger = logging.getLogger(__name__)

MANIFESTS_DIR         = "skills/manifests"
MODULES_REGISTRY_PATH = "data/modules.json"          # runtime state — what's loaded and running
CATALOG_PATH           = "data/modules_catalog.json"  # capability catalog — what exists and what's planned
HEALTH_CHECK_INTERVAL = 300   # 5 minutes
CONSECUTIVE_FAILURE_LIMIT = 3

VALID_STATUSES = ("active", "inactive", "error", "updating", "not_installed")

# ── capability catalog — separate lifecycle from the runtime registry
# above. A catalog entry describes a capability that may not be built at
# all yet ("planned"); nothing in this file ever calls
# update_catalog_status() itself — status changes here only ever happen
# because a caller (an API route Joan or HUGO triggered) asked for one.
CATALOG_STATUSES = (
    "planned", "researching", "designing", "developing", "testing",
    "ready", "installed", "updating", "error",
)

# Adjacent-stage-only, matching the spec's own example (planned->researching
# is fine, planned->installed is not) — a status can only move to a
# neighboring stage in the natural build pipeline, step back to the one
# before it, fail outright from any in-progress stage ('error' — added so
# core.code_engine.CodeEngine can mark a generation attempt that ran out of
# retries as failed no matter which active stage it died in, not just from
# 'installed'/'updating'), or (from 'error') resume from where it broke.
# 'planned' itself can't fail — no work has started yet, only 'researching'
# can begin.
_CATALOG_TRANSITIONS: dict[str, set] = {
    "planned":     {"researching"},
    "researching": {"designing", "planned", "error"},
    "designing":   {"developing", "researching", "error"},
    "developing":  {"testing", "designing", "error"},
    "testing":     {"ready", "developing", "error"},
    "ready":       {"installed", "testing", "error"},
    "installed":   {"updating", "error"},
    "updating":    {"installed", "error"},
    "error":       {"updating", "researching", "installed"},
}


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


class ModuleManager:
    """Installable-module bookkeeping over the skills/ package: a
    data/modules.json registry (status/version/health), manifest-declared
    permissions, and the isolation guarantee that a broken module can never
    take the assistant down with it (see call())."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._registry: dict = self._load_registry()
        self._fail_counts: dict[str, int] = {}
        self._rollback_snapshots: dict[str, dict] = {}
        # Separate lock from self._lock (the runtime registry's) — the
        # catalog is a distinct file with its own read/write path, per the
        # "strict separation" rule: modules.json and modules_catalog.json
        # are independent, not entangled with each other's bookkeeping.
        self._catalog_lock = threading.Lock()
        self._bootstrap()
        self._health_thread = threading.Thread(
            target=self._health_loop, daemon=True, name="module-manager-health",
        )
        self._health_thread.start()

    # ── registry persistence ────────────────────────────────────────────

    def _load_registry(self) -> dict:
        try:
            with open(MODULES_REGISTRY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _save_registry_locked(self) -> None:
        """Caller must hold self._lock."""
        os.makedirs(os.path.dirname(MODULES_REGISTRY_PATH) or ".", exist_ok=True)
        with open(MODULES_REGISTRY_PATH, "w", encoding="utf-8") as f:
            json.dump(self._registry, f, ensure_ascii=False, indent=2)

    def _entry_locked(self, module_name: str) -> dict:
        """Caller must hold self._lock. Creates a not_installed entry on
        first touch rather than raising, so every method below can assume
        one exists."""
        return self._registry.setdefault(module_name, {
            "status":             "not_installed",
            "version":            None,
            "installed_at":       None,
            "last_health_check":  None,
            "error":              None,
        })

    # ── manifests ────────────────────────────────────────────────────────

    def _manifest_path(self, module_name: str) -> str:
        return os.path.join(MANIFESTS_DIR, module_name, "module.json")

    def _load_manifest(self, module_name: str) -> dict | None:
        try:
            with open(self._manifest_path(module_name), "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        return manifest if isinstance(manifest, dict) else None

    def list_manifests(self) -> list[str]:
        """Every module name that has a manifest on disk, whether or not
        it's installed/enabled — the universe install()/get_status() draw
        from."""
        try:
            return sorted(
                d for d in os.listdir(MANIFESTS_DIR)
                if os.path.isfile(os.path.join(MANIFESTS_DIR, d, "module.json"))
            )
        except (FileNotFoundError, NotADirectoryError):
            return []

    def _check_dependencies(self, manifest: dict) -> list[str]:
        """Best-effort — dependency strings are pip-style ('discord.py>=2.0'),
        not necessarily valid Python import names, so an unresolvable one is
        logged, not treated as a hard install failure. Returns the ones that
        looked resolvable but weren't found importable."""
        import importlib.util
        missing = []
        for dep in manifest.get("dependencies", []):
            pkg = dep.split(">=")[0].split("==")[0].split("<")[0].strip()
            import_name = pkg.replace("-", "_").removesuffix(".py") or pkg
            try:
                if importlib.util.find_spec(import_name) is None:
                    missing.append(dep)
            except (ImportError, ValueError, ModuleNotFoundError):
                missing.append(dep)
        return missing

    # ── bootstrap ────────────────────────────────────────────────────────

    def _bootstrap(self) -> None:
        """First run: register every manifest found on disk that isn't in
        data/modules.json yet — auto_start ones as installed/active
        (matching skills/__init__.py's own default-on flags), the rest as
        not_installed."""
        for name in self.list_manifests():
            with self._lock:
                already_known = name in self._registry
            if already_known:
                continue
            manifest = self._load_manifest(name)
            if manifest and manifest.get("auto_start"):
                self.install(name)
            else:
                with self._lock:
                    self._entry_locked(name)
                    self._save_registry_locked()

    # ── lifecycle ────────────────────────────────────────────────────────

    def install(self, module_name: str) -> bool:
        manifest = self._load_manifest(module_name)
        if manifest is None:
            logger.warning("module_manager: install(%s) — no manifest found", module_name)
            return False

        missing_deps = self._check_dependencies(manifest)
        if missing_deps:
            logger.warning(
                "module_manager: %s declares dependencies not importable: %s "
                "(continuing — pip-name/import-name mismatches are common)",
                module_name, missing_deps,
            )

        loaded = skills.is_loaded(module_name)
        with self._lock:
            entry = self._entry_locked(module_name)
            entry["version"]      = manifest.get("version")
            entry["installed_at"] = entry["installed_at"] or _now_iso()
            entry["error"]        = None if loaded else "module code failed to load"
            if not loaded:
                entry["status"] = "error"
            elif skills.set_enabled(module_name, True):
                entry["status"] = "active"
            else:
                entry["status"] = "inactive"
            self._save_registry_locked()
        return loaded

    def uninstall(self, module_name: str) -> bool:
        with self._lock:
            if module_name not in self._registry:
                return False
        skills.set_enabled(module_name, False)
        with self._lock:
            entry = self._entry_locked(module_name)
            entry["status"] = "not_installed"
            entry["error"]  = None
            self._save_registry_locked()
        self._fail_counts.pop(module_name, None)
        return True

    def enable(self, module_name: str) -> bool:
        ok = skills.set_enabled(module_name, True)
        with self._lock:
            entry = self._entry_locked(module_name)
            if ok and skills.is_loaded(module_name):
                entry["status"] = "active"
                entry["error"]  = None
            elif ok:
                entry["status"] = "error"
                entry["error"]  = "module code failed to load"
            self._save_registry_locked()
        return ok

    def disable(self, module_name: str) -> bool:
        ok = skills.set_enabled(module_name, False)
        with self._lock:
            entry = self._entry_locked(module_name)
            if ok:
                entry["status"] = "inactive"
            self._save_registry_locked()
        return ok

    def update(self, module_name: str) -> bool:
        """Re-reads the module's manifest from disk (picking up a version
        bump) and re-validates it loads. Snapshots the pre-update
        {version, status} so rollback() can restore it."""
        manifest = self._load_manifest(module_name)
        if manifest is None:
            return False

        with self._lock:
            entry = self._entry_locked(module_name)
            self._rollback_snapshots[module_name] = {
                "version": entry.get("version"), "status": entry.get("status"),
            }
            entry["status"] = "updating"
            self._save_registry_locked()

        loaded = skills.is_loaded(module_name)
        with self._lock:
            entry = self._entry_locked(module_name)
            entry["version"] = manifest.get("version")
            if loaded and skills.set_enabled(module_name, True):
                entry["status"] = "active"
                entry["error"]  = None
            else:
                entry["status"] = "error"
                entry["error"]  = "update failed: module code did not load"
            self._save_registry_locked()
        return entry["status"] == "active"

    def rollback(self, module_name: str) -> bool:
        snapshot = self._rollback_snapshots.get(module_name)
        if snapshot is None:
            logger.warning("module_manager: rollback(%s) — no prior update to roll back to", module_name)
            return False
        with self._lock:
            entry = self._entry_locked(module_name)
            entry["version"] = snapshot["version"]
            entry["status"]  = snapshot["status"]
            entry["error"]   = None
            self._save_registry_locked()
        return True

    # ── isolation — CRITICAL: a module that throws never propagates ─────

    def isolate(self, module_name: str) -> None:
        """Called automatically on any error from a module call or 3
        consecutive failed health checks. Disables the module's flag (so
        the conversation engine's own skills.get_skill() stops offering it
        too, not just this manager) and marks it 'error' in the registry."""
        skills.set_enabled(module_name, False)
        with self._lock:
            entry = self._entry_locked(module_name)
            entry["status"] = "error"
            self._save_registry_locked()
        self._fail_counts[module_name] = 0

    def call(self, module_name: str, query: str, context: dict | None = None):
        """The one path anything outside this file should use to actually
        run a module — wraps skill.execute() exactly per the isolation
        rule: any exception isolates the module and returns None instead of
        propagating to the caller."""
        skill = skills.get_skill(module_name)
        if skill is None:
            return None
        try:
            result = skill.execute(query, context or {})
        except Exception as e:
            self.isolate(module_name)
            logger.error(f"Module {module_name} isolated: {e}")
            return None
        return result

    # ── health ───────────────────────────────────────────────────────────

    def health_check(self, module_name: str) -> dict:
        manifest = self._load_manifest(module_name)
        checked_at = _now_iso()

        with self._lock:
            prior = dict(self._entry_locked(module_name))

        if manifest is None:
            status, ok, error = "not_installed", False, "no manifest"
        elif prior.get("status") == "error":
            # Already isolated (either by a prior 3-strikes health check or
            # by call()'s isolation rule). isolate() disables the module's
            # flag, which would otherwise make the flag-based check below
            # read as a plain "inactive" and silently clear the error on
            # the very next tick — auto-healing a module that's supposed to
            # stay down until someone explicitly enable()/update()/
            # rollback()s it. Keep reporting the existing error instead of
            # re-deriving status from the (now-off) flag.
            status, ok, error = "error", False, prior.get("error") or "isolated"
        elif not skills.is_loaded(module_name):
            status, ok, error = "error", False, "module code failed to load"
        else:
            skill = skills.get_skill(module_name)   # None here means: loaded, but flag is off
            if skill is None:
                status, ok, error = "inactive", True, None
            else:
                ping = getattr(skill, "ping", None)
                if callable(ping):
                    try:
                        ping()
                        status, ok, error = "active", True, None
                    except Exception as e:
                        status, ok, error = "error", False, str(e)
                else:
                    # No ping() — "verify the module loads without error" is
                    # already satisfied by skills.is_loaded()/get_skill()
                    # above, so this doesn't call execute() with dummy
                    # arguments (avoids side effects like creating a bogus
                    # investigation or a live Discord/network call during a
                    # background health tick).
                    status, ok, error = "active", True, None

        already_isolated = prior.get("status") == "error"
        if ok:
            self._fail_counts[module_name] = 0
        elif not already_isolated:
            self._fail_counts[module_name] = self._fail_counts.get(module_name, 0) + 1
            if self._fail_counts[module_name] >= CONSECUTIVE_FAILURE_LIMIT:
                self.isolate(module_name)
                status = "error"

        with self._lock:
            entry = self._entry_locked(module_name)
            entry["status"]            = status
            entry["last_health_check"] = checked_at
            entry["error"]             = error
            self._save_registry_locked()

        return {"module": module_name, "status": status, "ok": ok, "checked_at": checked_at, "error": error}

    def health_check_all(self) -> dict:
        return {name: self.health_check(name) for name in self.list_manifests()}

    def _health_loop(self) -> None:
        while True:
            try:
                self.health_check_all()
            except Exception:
                logger.debug("module_manager: health_check_all failed", exc_info=True)
            time.sleep(HEALTH_CHECK_INTERVAL)

    # ── introspection ────────────────────────────────────────────────────

    def get_status(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._registry))   # deep copy

    def get_permissions(self, module_name: str) -> list:
        manifest = self._load_manifest(module_name)
        return list(manifest.get("permissions", [])) if manifest else []

    # ── capability catalog (data/modules_catalog.json) ─────────────────
    # Separate from the runtime registry above: this tracks WHAT EXISTS
    # AND WHAT'S PLANNED (including capabilities with no code at all yet),
    # not what's currently loaded/running. Read via get_catalog*(), written
    # only via update_catalog_status() — never by this file's own bootstrap
    # or health-check logic, per "only Joan or HUGO explicitly trigger
    # status changes".

    def _load_catalog(self) -> list:
        try:
            with open(CATALOG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []

    def _save_catalog_locked(self, catalog: list) -> None:
        """Caller must hold self._catalog_lock."""
        os.makedirs(os.path.dirname(CATALOG_PATH) or ".", exist_ok=True)
        with open(CATALOG_PATH, "w", encoding="utf-8") as f:
            json.dump(catalog, f, ensure_ascii=False, indent=2)

    def get_catalog(self) -> list:
        with self._catalog_lock:
            return self._load_catalog()

    def get_catalog_with_ad_hoc(self) -> list:
        """get_catalog() plus one synthetic, DISPLAY-ONLY entry per module
        Joan asked HUGO to build directly in conversation
        (core.code_engine.CodeEngine.create_ad_hoc_module(), catalog_id=
        None on purpose — an ad-hoc request never touches this curated,
        planned-capabilities catalog) — so the Módulos UI can show it too,
        per Joan's request, without polluting modules_catalog.json itself.

        Distinguishing "created by HUGO via conversation" from "just
        happens to have no catalog entry" needs a real signal, not merely
        "absent from the catalog": several of the ORIGINAL hand-built
        skills (calculator, weather, calendar, discord_bridge,
        investigations, schema_generator, web_search) predate Code Engine
        entirely and have never been in the catalog either — treating
        every catalog-less module as "HUGO-created" would mislabel every
        one of those. The real signal is each module's own manifest
        (skills/manifests/<name>/module.json) — CodeEngine._generate_module_impl()
        tags every manifest it writes with created_via: 'ad_hoc_conversation'
        (catalog_id=None) or 'catalog' (a real catalog build/update);
        manifests never touched by Code Engine at all (the original
        hand-built skills) have no created_via key, so they're correctly
        excluded here.

        These synthetic entries are never persisted to
        modules_catalog.json and are intentionally NOT valid catalog_ids —
        update_catalog_status()/set_catalog_blocked()/set_catalog_priority()
        will just log 'no such catalog entry' and no-op if ever called
        against one, same as for any other unknown id."""
        catalog = self.get_catalog()
        catalog_ids = {m.get("id") for m in catalog}
        status = self.get_status()

        ad_hoc = []
        for module_name, runtime_entry in status.items():
            if module_name in catalog_ids:
                continue
            manifest = self._load_manifest(module_name)
            if not manifest or manifest.get("created_via") != "ad_hoc_conversation":
                continue
            runtime_status = runtime_entry.get("status")
            ad_hoc.append({
                "id": module_name,
                "name": manifest.get("name", module_name).replace("_", " ").title(),
                "category": "CREADO POR HUGO",
                "description": manifest.get("description", ""),
                "status": "installed" if runtime_status in ("active", "inactive") else (runtime_status or "not_installed"),
                "version": manifest.get("version"),
                "priority": 99,
                "dependencies": manifest.get("dependencies", []),
                "permissions": manifest.get("permissions", []),
                "ad_hoc": True,
            })
        return catalog + ad_hoc

    def get_hugo_flagged_modules(self) -> list:
        """Every module whose manifest carries CodeEngine's
        hugo_review_flag (see core.code_engine._stamp_hugo_review_flag —
        set on every create_module()/create_ad_hoc_module()/update_module()
        that actually lands on disk) — the whole point being a fast,
        no-argument way for a code-error review pass to pull just the
        LLM-generated/LLM-modified modules instead of every module
        (including the original hand-built skills that predate Code Engine
        and were never touched by this flag). Not surfaced in
        get_catalog()/get_catalog_with_ad_hoc() — this is a review-tooling
        query, not a Módulos UI one."""
        out = []
        for module_name in self.list_manifests():
            manifest = self._load_manifest(module_name)
            if not manifest or not manifest.get("hugo_review_flag"):
                continue
            out.append({
                "id": module_name,
                "version": manifest.get("version"),
                "last_action": manifest.get("hugo_review_last_action"),
                "flagged_at": manifest.get("hugo_review_flagged_at"),
            })
        return out

    def get_catalog_by_category(self, category: str) -> list:
        wanted = (category or "").strip().upper()
        return [m for m in self.get_catalog() if str(m.get("category", "")).upper() == wanted]

    def get_catalog_by_status(self, status: str) -> list:
        wanted = (status or "").strip().lower()
        return [m for m in self.get_catalog() if m.get("status") == wanted]

    def update_catalog_status(self, module_id: str, status: str, version: str | None = None) -> bool:
        """Moves a catalog entry to `status`, only if that's a sensible
        next step from its current one (see _CATALOG_TRANSITIONS) — e.g.
        planned->researching is accepted, planned->installed is rejected
        and logged rather than silently applied."""
        if status not in CATALOG_STATUSES:
            logger.warning("module_manager: update_catalog_status(%s, %r) — unknown status", module_id, status)
            return False
        with self._catalog_lock:
            catalog = self._load_catalog()
            entry = next((m for m in catalog if m.get("id") == module_id), None)
            if entry is None:
                logger.warning("module_manager: update_catalog_status(%s) — no such catalog entry", module_id)
                return False
            current = entry.get("status")
            if status != current and status not in _CATALOG_TRANSITIONS.get(current, set()):
                logger.warning(
                    "module_manager: rejected invalid catalog transition for %s: %s -> %s",
                    module_id, current, status,
                )
                return False
            entry["status"] = status
            if version is not None:
                entry["version"] = version
            self._save_catalog_locked(catalog)
        return True

    def set_catalog_blocked(self, module_id: str, blocked: bool) -> bool:
        """Manual override, orthogonal to `status` — when true,
        core.code_engine.CodeEngine refuses to create or update this
        module no matter what stage it's at or how it's prioritized. Not a
        status value itself (a blocked 'planned' entry is still 'planned';
        a blocked 'installed' one is still 'installed') — it's a separate
        flag so it can be set/cleared at any pipeline stage without
        disturbing status or requiring a valid transition."""
        with self._catalog_lock:
            catalog = self._load_catalog()
            entry = next((m for m in catalog if m.get("id") == module_id), None)
            if entry is None:
                logger.warning("module_manager: set_catalog_blocked(%s) — no such catalog entry", module_id)
                return False
            entry["blocked"] = bool(blocked)
            self._save_catalog_locked(catalog)
        return True

    def set_catalog_priority(self, module_id: str, priority: int) -> bool:
        """Lower number = higher priority, same convention as every
        manifest's own `priority` field elsewhere in this file. Purely
        advisory bookkeeping today — nothing reads catalog priority to pick
        what to build next yet (create_module()/update_module() are only
        ever invoked one at a time, by name), but it's what a future
        scheduler/TaskEngine integration would sort by."""
        with self._catalog_lock:
            catalog = self._load_catalog()
            entry = next((m for m in catalog if m.get("id") == module_id), None)
            if entry is None:
                logger.warning("module_manager: set_catalog_priority(%s) — no such catalog entry", module_id)
                return False
            entry["priority"] = int(priority)
            self._save_catalog_locked(catalog)
        return True


manager = ModuleManager()
