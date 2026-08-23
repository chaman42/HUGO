# PERMISSIONS — the safety gate every Code Engine Phase 1 tool checks
# before touching a filesystem/git path (data/code_engine_permissions.json).
#
# The spec that requested this package showed `_check_permission` as a
# FileSystem-only method. It's implemented here as ONE shared function
# instead, and every tool (ProjectAnalyzer, FileSystem, CodeSearch, Editor,
# Git) calls it — not just FileSystem. The stated goal ("Do NOT: Give
# FileSystem access outside allowed_project_paths") is clearly about
# arbitrary filesystem/git access in general, not one specific class; a
# CodeSearch or Git call takes a `path` argument just as freely as
# FileSystem does, and leaving those four ungated while only FileSystem
# checks would defeat the entire allowlist model the very first time
# something calls CodeSearch.search_text() on an unapproved path. Each
# tool still exposes its own `_check_permission`/`_check` method (matching
# the spec's naming where it specified one), thinly wrapping this shared
# implementation so there's exactly one place the actual path-safety logic
# lives.
#
# Default state (data/code_engine_permissions.json as first written):
# allowed_project_paths is EMPTY. Every operation — including plain
# reads — is denied until Joan manually adds a path. Nothing here ever
# writes to that list itself.
import json
import logging
import os
import threading

logger = logging.getLogger("code_engine")

PERMISSIONS_PATH = "data/code_engine_permissions.json"

_DEFAULT_PERMISSIONS = {
    "allowed_project_paths": [],
    "permissions": {
        "read": True, "write": True, "delete": False, "git_push": False,
        # Phase 2 — shell/install_dependencies default OFF: arbitrary shell
        # execution and package installation both need Joan to explicitly
        # flip these to true in this file first, on top of the path itself
        # already being in allowed_project_paths. run_tests defaults ON —
        # running a project's own test suite (no writes, no shell) is the
        # one Phase 2 operation the spec itself calls safe by default.
        "shell": False, "install_dependencies": False, "run_tests": True,
        # Phase 4 — 'internet' gates DocsBrowser (docs_browser.py) entirely:
        # every one of its methods calls check_internet_permission() before
        # doing anything. Off by default, same reasoning as shell/
        # install_dependencies — outbound network access from an
        # autonomous tool needs Joan to explicitly opt in. Not path-scoped
        # like the other permissions (a doc search/fetch isn't "operating
        # on" allowed_project_paths the way a file write is), so it's
        # checked with check_internet_permission() instead of
        # check_permission().
        "internet": False,
        # Phase 5 — 'deploy' gates every Deployer method that actually
        # ships something (deploy/deploy_hugo_module/update_hugo_module) —
        # path-scoped like shell/install_dependencies/run_tests (checked
        # via check_permission("deploy", path), not the global-only shape
        # 'internet' uses), since a deploy always targets one specific
        # project. Off by default — Joan enables it explicitly per project,
        # same reasoning as every other high-blast-radius Phase 1-4 flag.
        "deploy": False,
    },
}

_lock = threading.Lock()


def _load() -> dict:
    with _lock:
        try:
            with open(PERMISSIONS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return json.loads(json.dumps(_DEFAULT_PERMISSIONS))
        if not isinstance(data, dict):
            return json.loads(json.dumps(_DEFAULT_PERMISSIONS))
        data.setdefault("allowed_project_paths", [])
        perms = _DEFAULT_PERMISSIONS["permissions"].copy()
        if isinstance(data.get("permissions"), dict):
            perms.update(data["permissions"])
        data["permissions"] = perms
        return data


def _is_within_allowed(path: str, allowed_paths: list) -> bool:
    """realpath-based — resolves both '..' traversal AND symlinks, so a
    symlink inside an allowed directory that points OUTSIDE it correctly
    fails this check (realpath follows it to its real, unapproved target
    before comparing)."""
    if not path:
        return False
    try:
        target = os.path.realpath(os.path.abspath(path))
    except (OSError, ValueError):
        return False
    for allowed in allowed_paths:
        try:
            base = os.path.realpath(os.path.abspath(allowed))
        except (OSError, ValueError):
            continue
        if target == base or target.startswith(base + os.sep):
            return True
    return False


def check_permission(operation: str, path: str) -> tuple:
    """operation: 'read' | 'write' | 'delete' | 'git_push'. Returns
    (allowed: bool, reason: str) — never raises."""
    data = _load()
    if operation not in data["permissions"]:
        return False, f"unknown operation: {operation!r}"
    if not data["permissions"][operation]:
        return False, f"operation {operation!r} is disabled in {PERMISSIONS_PATH}"
    if not _is_within_allowed(path, data["allowed_project_paths"]):
        return False, f"path not in allowed_project_paths: {path!r}"
    return True, "ok"


def check_internet_permission() -> tuple:
    """Global on/off gate for DocsBrowser — no path involved, unlike
    check_permission(). Returns (allowed: bool, reason: str)."""
    data = _load()
    if not data["permissions"].get("internet"):
        return False, f"'internet' is disabled in {PERMISSIONS_PATH}"
    return True, "ok"


def get_permissions_snapshot() -> dict:
    """Read-only snapshot — for introspection/API responses. Never used
    for the actual gating decision (check_permission() re-reads fresh
    every time, so a path Joan just added takes effect immediately without
    needing any cache invalidation)."""
    return _load()
