# CODE ENGINE — generates and updates skills/ modules only. Never writes
# outside skills/ (hard-enforced by _safe_path() before every write) and
# never touches hugo_core/ files (doesn't exist — core/ is flat; the rule
# applies at core.module_manager.py/core.task_engine.py, whose PUBLIC APIS
# this file calls for orchestration — install()/update()/
# update_catalog_status()/block_task() — but never edits their source).
#
# Safety hardening beyond the literal spec: the spec's own _sandbox_test
# describes a bare `importlib.import_module()` in the SAME process. That
# executes arbitrary LLM-generated code with this process's full
# privileges (mic, calendar, Discord bot token, every data/*.json file)
# the instant it's tested, before any human ever looks at it. Instead,
# _sandbox_test here runs the equivalent checks in a throwaway subprocess
# (same interpreter/venv via sys.executable, 30s timeout) — a broken or
# malicious generation can hang, crash, or misbehave in that subprocess
# without touching the live assistant. The pass/fail contract
# (tuple[bool, str]) is unchanged.
import datetime
import json
import logging
import os
import re
import subprocess
import sys

from dotenv import load_dotenv

# So DEEPSEEK_API_KEY is available even if this module is used standalone,
# same defensive load as core/tools.py, core/groq_config.py, etc. — there's
# no single central load_dotenv() call in this codebase (see those files).
load_dotenv()

logger = logging.getLogger("code_engine")
if not logger.handlers:
    os.makedirs("logs", exist_ok=True)
    _file_handler = logging.FileHandler("logs/code_engine.log", encoding="utf-8")
    _file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_file_handler)
    logger.setLevel(logging.INFO)

LOG_PATH = "logs/code_engine.log"

# Bug fix ("manifest not valid JSON: No such file or directory" on every
# sandbox test): this file lives at core/code_engine/__init__.py — TWO
# directories below the real repo root — so a single ".." here resolved
# to core/, not the actual root. Confirmed directly: _sandbox_test()'s
# subprocess (cwd=_REPO_ROOT) was looking for skills/manifests/.../
# module.json under core/skills/..., which never existed, so EVERY
# create_module()/update_module() sandbox test failed this way — not
# just create_ad_hoc_module()'s new path added in this same change, this
# bug predates it and affected the existing Módulos catalog build/update
# buttons too. _safety_snapshot()/_rollback() use the same _REPO_ROOT for
# their own subprocess cwd and had the identical bug.
_REPO_ROOT    = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SKILLS_DIR    = "skills"
MANIFESTS_DIR = "skills/manifests"
MAX_ATTEMPTS  = 3

# ═══════════════════════════════════════════════════════════════════════════
# Job-in-progress tracking — closes a real gap found live 2026-08-10:
# create_module()/update_module() run on a plain background thread
# (core.code_engine_dispatch.dispatch_module_task) with no persisted record
# that a cycle is running. When the owning jarvis.py process dies mid-cycle
# (crash, restart, the Mac sleeping overnight — exactly what happened:
# jarvis.py was stopped by the launcher at 01:07:41 while _review_gate()
# was still running), the work is abandoned in whatever half-written state
# it was in: for an update, the module FILE was already overwritten with
# new, never-reviewed code, while the manifest version, data/modules.json,
# and git all still reflect the OLD version — a silent, torn state that
# looks installed but was never actually approved. Same pid-liveness
# pattern as core.ollama_control's CODE_ENGINE_CYCLE_LOCK_PATH /
# core.sleep_control's own copy of the same check (this codebase's existing
# convention is a small private copy per module rather than a shared
# import — see those two files).
# ═══════════════════════════════════════════════════════════════════════════
JOBS_PATH = "data/code_engine_active_jobs.json"


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists, just not ours to signal — still alive
    except OSError:
        return False
    return True


def _is_jarvis_process(pid: int) -> bool:
    try:
        result = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        return "jarvis.py" in result.stdout
    except OSError:
        return False


def _load_jobs() -> dict:
    try:
        with open(JOBS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_jobs(jobs: dict) -> None:
    try:
        os.makedirs(os.path.dirname(JOBS_PATH) or ".", exist_ok=True)
        with open(JOBS_PATH, "w", encoding="utf-8") as f:
            json.dump(jobs, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.debug("code_engine: failed to persist %s", JOBS_PATH, exc_info=True)


def _mark_job_active(module_name: str, action: str) -> None:
    """Best-effort — a failure to write this just means an interrupted run
    of THIS module won't be auto-recovered next boot, never a hard failure
    for the run itself. Never raises."""
    jobs = _load_jobs()
    jobs[module_name] = {
        "action": action,
        "pid": os.getpid(),
        "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    _save_jobs(jobs)


def _clear_job_active(module_name: str) -> None:
    jobs = _load_jobs()
    if module_name in jobs:
        del jobs[module_name]
        _save_jobs(jobs)


def recover_orphaned_jobs() -> None:
    """Call once at server startup (core.server.start(), same spot as
    core.task_engine.TaskEngine.resume_on_wakeup() — see that call site).
    Any job record whose owning pid is no longer a live jarvis.py process
    means that create/update cycle was killed mid-flight last time. Rather
    than leaving the torn state around (module file already rewritten but
    never reviewed/version-bumped/installed, or a stray never-installed
    ad-hoc create), roll it back to the last known-good state
    automatically, the same way a normal failed-after-3-attempts run
    already does — an interrupted run should end up indistinguishable from
    a cleanly failed one, not a silent third outcome. Best-effort and
    non-blocking: must never prevent server startup."""
    jobs = _load_jobs()
    if not jobs:
        logger.info("code_engine: recover_orphaned_jobs — no in-progress jobs")
        return

    import core.module_manager as module_manager_mod
    from core import notifications as notifications_mod

    remaining = dict(jobs)
    for module_name, job in jobs.items():
        pid = job.get("pid")
        action = job.get("action")
        if isinstance(pid, int) and _is_pid_alive(pid) and _is_jarvis_process(pid):
            continue   # a genuinely different, still-running jarvis.py owns this — leave it

        logger.warning(
            "code_engine: recover_orphaned_jobs — %s(%s) was interrupted (owning pid %s no longer alive) — rolling back",
            action, module_name, pid,
        )
        module_path, manifest_dir, _ = code_engine._module_paths(module_name)
        try:
            if action == "update":
                code_engine._rollback(module_name)
            elif action == "create":
                registry = module_manager_mod.manager._load_registry()
                if module_name not in registry:   # never reached install() — safe to remove entirely
                    for p in (module_path,):
                        if os.path.exists(p):
                            os.remove(p)
                    import shutil
                    if os.path.isdir(manifest_dir):
                        shutil.rmtree(manifest_dir, ignore_errors=True)
            import skills
            skills.reload_skills()
            notifications_mod.create_notification(
                "code_engine",
                f"Ciclo interrumpido: {module_name}",
                f"{'El update' if action == 'update' else 'La creación'} de '{module_name}' se interrumpió "
                f"(el proceso anterior murió a mitad del ciclo) y se revirtió automáticamente al estado anterior.",
            )
        except Exception:
            logger.error("code_engine: recover_orphaned_jobs — failed to roll back %s", module_name, exc_info=True)
        del remaining[module_name]

    _save_jobs(remaining)

_MANIFEST_REQUIRED_FIELDS = (
    "name", "version", "description", "dependencies", "permissions", "entry_point", "auto_start",
)

# ═══════════════════════════════════════════════════════════════════════════
# LLM ROUTER — cloud primary (DeepSeek, free-tier API), local fallback
# (Ollama qwen2.5-coder). Same cloud-then-local shape as
# core.sleep_llm._groq_call falling back to Ollama, just with the roles
# swapped (DeepSeek is the free/cheap primary here; Groq isn't involved at
# all — this is code generation, not conversation).
# ═══════════════════════════════════════════════════════════════════════════
DEEPSEEK_API_URL  = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL    = "deepseek-coder"
OLLAMA_HOST       = "http://localhost:11434"
OLLAMA_CODE_MODEL = "qwen2.5-coder"

# Bug fix ("Ollama generation killed mid-work"): the old non-streaming call
# (stream: False) makes Ollama generate the ENTIRE response server-side
# before returning a single byte — the client's one blocking read then has
# to complete within whatever timeout is set, which on CPU-only inference
# (no DeepSeek key configured, no GPU here) routinely took longer than any
# reasonable fixed timeout, killing genuinely-still-working generations.
# Streaming (below) turns that fixed "must finish entirely within N
# seconds" cap into a STALL timeout instead, for free: urlopen()'s
# `timeout=` applies to the underlying socket, which re-arms on every
# individual read — as long as a new token keeps arriving within
# OLLAMA_STALL_TIMEOUT_SECONDS, the read succeeds and total generation
# time is effectively unbounded; only genuine silence for that long (a
# truly hung connection) aborts it. Not literally "no timeout" (a
# background thread hung forever on a dead socket is its own problem) —
# this is the version of "remove the timeout" that's actually safe.
# Bumped 120 -> 600: measured directly that 120s wasn't even enough for a
# real prompt's FIRST token on this CPU-only setup (a live test's stream
# produced nothing at all for the full 120s, then errored — not a stuck
# connection, just genuinely slow time-to-first-token for a longer/more
# complex prompt than a trivial one-word test). 600s (10 min) of total
# silence is still a real, finite bound — a genuinely dead connection
# still gets noticed eventually — just generous enough that legitimate
# slow inference on this hardware isn't mistaken for one.
OLLAMA_STALL_TIMEOUT_SECONDS  = 600   # no new token for this long = genuinely stuck, abort
OLLAMA_PROGRESS_LOG_INTERVAL  = 10    # seconds between "still generating..." log lines


class LLMRouter:
    def generate_code(self, prompt: str, context: str) -> str:
        try:
            return self._deepseek(prompt, context)
        except Exception as e:
            logger.warning("LLMRouter: DeepSeek unavailable (%s) — falling back to Ollama", e)
            return self._ollama(prompt, context)

    def _deepseek(self, prompt: str, context: str) -> str:
        import urllib.request

        api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("DEEPSEEK_API_KEY not set")

        payload = json.dumps({
            "model": DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": context},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }).encode("utf-8")
        req = urllib.request.Request(
            DEEPSEEK_API_URL, data=payload, method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["choices"][0]["message"]["content"]
        if not text or not text.strip():
            raise RuntimeError("DeepSeek returned an empty response")
        return text

    def _ensure_ollama_model(self) -> None:
        """Pulls qwen2.5-coder if it's not already present. `ollama pull`
        is idempotent — a no-op if already pulled — so this is safe to call
        every time rather than trying to cache the check."""
        try:
            result = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=10)
            if OLLAMA_CODE_MODEL in (result.stdout or ""):
                return
        except Exception:
            pass   # fall through and try to pull anyway
        try:
            logger.info("LLMRouter: pulling %s (first use — may take a while)", OLLAMA_CODE_MODEL)
            subprocess.run(["ollama", "pull", OLLAMA_CODE_MODEL], timeout=600)
        except Exception as e:
            logger.warning("LLMRouter: could not pull %s (%s)", OLLAMA_CODE_MODEL, e)

    def _ollama(self, prompt: str, context: str) -> str:
        import time
        import urllib.request
        import core.ollama_control as ollama_control

        ollama_control.ensure_ollama_daemon_running()
        self._ensure_ollama_model()

        payload = json.dumps({
            "model": OLLAMA_CODE_MODEL, "prompt": prompt, "system": context, "stream": True,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{OLLAMA_HOST}/api/generate", data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )

        started = time.monotonic()
        last_progress_log = started
        chunks: list[str] = []
        # timeout= here is a per-read SOCKET timeout, not a total-duration
        # one — see OLLAMA_STALL_TIMEOUT_SECONDS' own comment above for why
        # streaming makes that the case.
        with urllib.request.urlopen(req, timeout=OLLAMA_STALL_TIMEOUT_SECONDS) as resp:
            for raw_line in resp:
                if not raw_line.strip():
                    continue
                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                piece = obj.get("response", "")
                if piece:
                    chunks.append(piece)

                now = time.monotonic()
                if now - last_progress_log >= OLLAMA_PROGRESS_LOG_INTERVAL:
                    # "code_engine" logger propagates to root -> jarvis.py's
                    # SocketIOLogHandler -> maintenance panel, live, no new
                    # socket event/UI needed — see this module's own logger
                    # setup above.
                    logger.info(
                        "LLMRouter: generando con %s... %d caracteres, %ds transcurridos",
                        OLLAMA_CODE_MODEL, sum(len(c) for c in chunks), round(now - started),
                    )
                    last_progress_log = now

                if obj.get("done"):
                    break

        text = "".join(chunks).strip()
        if not text:
            raise RuntimeError("Ollama returned an empty response")
        logger.info(
            "LLMRouter: generación completa — %d caracteres en %ds",
            len(text), round(time.monotonic() - started),
        )
        return text


# ═══════════════════════════════════════════════════════════════════════════
# Sandbox runner — executed in a subprocess by _sandbox_test(), never
# imported directly by this file. Prints "OK" and exits 0 on success, or
# "FAIL <reason>" and exits 1 on the first failed check.
# ═══════════════════════════════════════════════════════════════════════════
_SANDBOX_RUNNER = r'''
import importlib, importlib.util, inspect, json, sys

module_name, manifest_path = sys.argv[1], sys.argv[2]

try:
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
except Exception as e:
    print(f"FAIL manifest not valid JSON: {e}"); sys.exit(1)

required = ("name", "version", "description", "dependencies", "permissions", "entry_point", "auto_start")
missing = [k for k in required if k not in manifest]
if missing:
    print(f"FAIL manifest missing fields: {missing}"); sys.exit(1)

for dep in manifest.get("dependencies", []):
    pkg = dep.split(">=")[0].split("==")[0].split("<")[0].strip()
    import_name = pkg.replace("-", "_")
    if import_name.endswith(".py"):
        import_name = import_name[:-3]
    try:
        found = importlib.util.find_spec(import_name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        found = False
    if not found:
        print(f"FAIL dependency not installed: {dep}"); sys.exit(1)

try:
    mod = importlib.import_module(f"skills.{module_name}")
except Exception as e:
    print(f"FAIL import error: {e}"); sys.exit(1)

from skills import HugoSkill
skill_cls = None
for _, obj in inspect.getmembers(mod, inspect.isclass):
    if issubclass(obj, HugoSkill) and obj is not HugoSkill and obj.__module__ == mod.__name__:
        skill_cls = obj
        break
if skill_cls is None:
    print("FAIL no HugoSkill subclass found in module"); sys.exit(1)

try:
    instance = skill_cls()
except Exception as e:
    print(f"FAIL could not instantiate skill class: {e}"); sys.exit(1)

ping = getattr(instance, "ping", None)
if callable(ping):
    try:
        ping()
    except Exception as e:
        print(f"FAIL ping() raised: {e}"); sys.exit(1)

print("OK")
sys.exit(0)
'''

_CODE_FENCE_RE = re.compile(r"^```(?:python)?\s*\n(.*?)\n?```\s*$", re.DOTALL)


def _strip_code_fences(text: str) -> str:
    """LLMs routinely wrap code in ```python fences despite instructions
    not to — strip them defensively rather than writing a fenced file that
    can never import."""
    text = (text or "").strip()
    m = _CODE_FENCE_RE.match(text)
    return m.group(1) if m else text


# Every module create_module()/create_ad_hoc_module()/update_module() ever
# touches gets this stamped onto its manifest — additive, same as
# created_via above, never in _MANIFEST_REQUIRED_FIELDS so ModuleManager's
# validation and the Módulos catalog UI both ignore it entirely (see
# module_manager.py's own manifest-field allowlist). The point isn't to
# show Joan anything; it's a cheap `grep -l '"hugo_review_flag": true'
# skills/manifests/*/module.json` (or the equivalent programmatic scan a
# review pass can run) to instantly separate LLM-generated/LLM-modified
# code from the hand-built skills (calculator, weather, ...) that predate
# Code Engine — exactly the code most worth double-checking for the
# eval()/exec()/hardcoded-secret class of bug _review_gate()'s
# find_security_issues() heuristics can miss.
def _stamp_hugo_review_flag(manifest: dict, action: str) -> dict:
    manifest["hugo_review_flag"] = True
    manifest["hugo_review_last_action"] = action   # "created" | "updated"
    manifest["hugo_review_flagged_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    return manifest


def _bump_version(manifest_path: str) -> str:
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        manifest = {}
    old = str(manifest.get("version", "0.1"))
    parts = old.split(".")
    try:
        parts[-1] = str(int(parts[-1]) + 1)
    except (ValueError, IndexError):
        parts = ["1", "1"]
    new_version = ".".join(parts)
    manifest["version"] = new_version
    _stamp_hugo_review_flag(manifest, "updated")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return new_version


# Forward stage order for catalog status — used to walk update_catalog_status()
# through every intermediate hop core.module_manager's transition table
# requires, rather than trying to jump straight to a target status.
_FORWARD_ORDER = ["planned", "researching", "designing", "developing", "testing", "ready", "installed"]


def _advance_catalog_status(manager, catalog_id: str, target: str) -> None:
    entry = next((m for m in manager.get_catalog() if m.get("id") == catalog_id), None)
    if entry is None:
        return
    current = entry.get("status")

    # Retrying a previously-blocked module: 'error' only permits
    # {updating, researching, installed} — it can't jump straight into the
    # forward chain below. Re-enter via 'researching' first (the one hop
    # the table allows) so a retry after a prior failure can still resume
    # instead of getting silently stuck.
    if current == "error":
        if not manager.update_catalog_status(catalog_id, "researching"):
            logger.warning("CodeEngine: catalog %s stuck in error — could not resume", catalog_id)
            return
        current = "researching"

    if current not in _FORWARD_ORDER or target not in _FORWARD_ORDER:
        manager.update_catalog_status(catalog_id, target)   # updating/etc. — just the direct hop
        return
    start_idx, end_idx = _FORWARD_ORDER.index(current), _FORWARD_ORDER.index(target)
    for step in _FORWARD_ORDER[start_idx + 1 : end_idx + 1]:
        if not manager.update_catalog_status(catalog_id, step):
            logger.warning("CodeEngine: catalog %s could not advance to %s", catalog_id, step)
            return


_CODE_CONTEXT = (
    "Eres un generador de código Python para HUGO, un asistente de voz. "
    "Generas EXCLUSIVAMENTE el contenido completo de un archivo Python para "
    "un 'skill': una clase que hereda de HugoSkill (atributos name, "
    "description, triggers: list[str], y un método "
    "execute(self, query: str, context: dict) -> str). El archivo debe "
    "empezar con 'from skills import HugoSkill'. No incluyas explicaciones, "
    "comentarios de markdown, backticks, ni ningún texto fuera del código "
    "Python del archivo."
)


class CodeEngine:
    def __init__(self) -> None:
        self.router = LLMRouter()

    # ── safety ───────────────────────────────────────────────────────────

    def _safe_path(self, path: str) -> bool:
        """Hard constraint: CodeEngine may never write outside skills/."""
        base   = os.path.abspath(SKILLS_DIR)
        target = os.path.abspath(path)
        ok = target == base or target.startswith(base + os.sep)
        if not ok:
            logger.critical("CodeEngine: BLOCKED a write outside skills/ — attempted path: %s", path)
        return ok

    def _module_paths(self, module_name: str) -> tuple[str, str, str]:
        module_path   = os.path.join(SKILLS_DIR, f"{module_name}.py")
        manifest_dir  = os.path.join(MANIFESTS_DIR, module_name)
        manifest_path = os.path.join(manifest_dir, "module.json")
        return module_path, manifest_dir, manifest_path

    def _safety_snapshot(self, module_name: str) -> bool:
        """Commits the module's CURRENT on-disk state before any
        modification — always runs before update_module() touches the
        file. Scoped to exactly this module's two files, never `git add
        -A` (this repo has other, unrelated runtime state files churning
        constantly — see every other core module's own commit hygiene)."""
        module_path, _, manifest_path = self._module_paths(module_name)
        try:
            subprocess.run(
                ["git", "add", "--", module_path, manifest_path],
                cwd=_REPO_ROOT, check=True, capture_output=True, timeout=15,
            )
            result = subprocess.run(
                ["git", "commit", "--allow-empty", "-m", f"snapshot: before update {module_name}"],
                cwd=_REPO_ROOT, capture_output=True, text=True, timeout=15,
            )
            logger.info("safety_snapshot(%s): %s", module_name, (result.stdout or result.stderr).strip())
            return True
        except Exception as e:
            logger.error("safety_snapshot(%s) failed: %s", module_name, e)
            return False

    def _rollback(self, module_name: str) -> None:
        """Restores both files to the state _safety_snapshot() just
        committed. data/modules.json is untouched by this — ModuleManager.
        update() is only ever called on the success path, never here."""
        module_path, _, manifest_path = self._module_paths(module_name)
        try:
            subprocess.run(
                ["git", "checkout", "--", module_path, manifest_path],
                cwd=_REPO_ROOT, check=True, capture_output=True, timeout=15,
            )
            logger.info("rollback(%s): restored from last snapshot", module_name)
        except Exception as e:
            logger.error("rollback(%s) failed: %s", module_name, e)

    def _review_gate(self, module_path: str) -> tuple[bool, str]:
        """Runs AFTER a passing sandbox test, before install — the safety
        layer create_module()/update_module() never had (only
        Orchestrator.execute_goal()'s cycle ran CodeReviewer before this,
        and conversational module creation stopped going through that
        cycle — see core.code_engine_dispatch's own module comment for
        why). A sandbox pass only proves the module IMPORTS and PING()S;
        it says nothing about eval()/exec() on unsanitized input, a
        hardcoded secret, or similar — exactly what CodeReviewer's
        heuristic find_security_issues() catches. Same (ok, detail) shape
        as _sandbox_test() so both slot into the identical retry loop
        below. Best-effort: if code_reviewer is unavailable/disabled
        (Code Engine's 'code_engine_enabled' kill switch also gates the
        Phase 1-5 tool registry — see CodeEngineToolManager.get_tool()),
        this passes through rather than blocking a module Joan's already
        past the sandbox gate for on a reviewer that simply isn't there."""
        try:
            from core.code_engine.tool_manager import tool_manager
            reviewer = tool_manager.get_tool("code_reviewer")
            if reviewer is None:
                return True, "ok (code_reviewer unavailable — skipped)"
            report = reviewer.review_file(module_path)
            critical = report.get("critical") or []
            if critical:
                return False, f"revisión encontró {len(critical)} problema(s) crítico(s): {report.get('summary', '')}"
            return True, "ok"
        except Exception as e:
            logger.warning("_review_gate(%s) failed — treating as pass: %s", module_path, e)
            return True, f"ok (review check failed: {e})"

    def _sandbox_test(self, module_path: str) -> tuple[bool, str]:
        module_name = os.path.splitext(os.path.basename(module_path))[0]
        _, _, manifest_path = self._module_paths(module_name)
        try:
            result = subprocess.run(
                [sys.executable, "-c", _SANDBOX_RUNNER, module_name, manifest_path],
                cwd=_REPO_ROOT, capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            return False, "sandbox test timed out after 30s"
        except Exception as e:
            return False, f"sandbox test could not run: {e}"

        if result.returncode == 0 and "OK" in (result.stdout or ""):
            return True, "ok"
        detail = (result.stdout or "").strip() or (result.stderr or "").strip() or f"exit code {result.returncode}"
        return False, detail

    def _block_and_notify(self, module_name: str, reason: str) -> None:
        """Fail (retry == 3) — creates or reuses a TaskEngine task tracking
        this module's generation and blocks it, which fires the Joan
        notification via the existing core.notifications queue (see
        TaskEngine.block_task) — no separate notify step needed here."""
        try:
            from core.task_engine import task_engine
            existing = next(
                (t for t in task_engine.get_all_tasks()
                 if t.get("goal", "").startswith(f"Generar módulo: {module_name}")
                 and t.get("status") not in ("completed", "failed")),
                None,
            )
            task_id = existing["id"] if existing else task_engine.create_task(
                f"Generar módulo: {module_name}",
                [f"Generar y probar {module_name} en sandbox"],
                priority=2, created_by="hugo",
            )
            task_engine.block_task(task_id, reason[:300])
        except Exception:
            logger.error("CodeEngine: failed to create/block TaskEngine task for %s", module_name, exc_info=True)

    # ── prompts ──────────────────────────────────────────────────────────

    def _relevant_skills_block(self, goal_text: str, tags: list) -> str:
        """Best-effort procedural knowledge from past completed tasks (see
        core.skill_forge) — 'so HUGO doesn't repeat past mistakes'. Queries
        SkillForge directly with the capability's own name/id as the goal/
        tags, rather than only reading a TaskEngine task's
        context_snapshot['relevant_skills'] (TaskEngine.create_task()
        already populates that when a task exists — see that module): a
        create_module()/update_module() call doesn't always have an
        associated task (both are directly reachable via the API), so
        querying SkillForge here works either way."""
        try:
            from core.skill_forge import skill_forge
            relevant = skill_forge.find_relevant_skills(goal_text, tags)
        except Exception:
            return ""
        pitfall_lines = []
        for s in relevant[:3]:
            pitfalls = "; ".join(s.get("pitfalls", [])[:3])
            if pitfalls:
                pitfall_lines.append(f"- {s.get('title', '')}: {pitfalls}")
        if not pitfall_lines:
            return ""
        return "\n\nCONOCIMIENTO DE TAREAS ANTERIORES — evita repetir estos errores:\n" + "\n".join(pitfall_lines)

    def _build_creation_prompt(self, catalog_entry: dict) -> str:
        return (
            f"Genera el contenido completo de skills/{catalog_entry['id']}.py para esta capacidad:\n"
            f"Nombre: {catalog_entry.get('name')}\n"
            f"Descripción: {catalog_entry.get('description')}\n"
            f"Dependencias: {catalog_entry.get('dependencies') or 'ninguna'}\n"
            f"Permisos: {catalog_entry.get('permissions') or 'ninguno'}\n\n"
            f"La clase debe fijar name = \"{catalog_entry['id']}\" exactamente. "
            f"Si alguna dependencia no es de la biblioteca estándar, impórtala de "
            f"forma perezosa dentro de execute() y falla con un mensaje claro si "
            f"no está instalada, en vez de romper la carga del módulo."
        ) + self._relevant_skills_block(catalog_entry.get("name", ""), [catalog_entry.get("id", "")])

    def _build_update_prompt(self, module_name: str, current_code: str, change: str) -> str:
        return (
            f"Este es el contenido actual de skills/{module_name}.py:\n\n"
            f"```python\n{current_code}\n```\n\n"
            f"Aplica este cambio y devuelve el ARCHIVO COMPLETO actualizado "
            f"(no un diff, no fragmentos): {change}\n\n"
            f"Mantén la clase HugoSkill existente (mismo name) salvo que el "
            f"cambio pida explícitamente lo contrario."
        ) + self._relevant_skills_block(change, [module_name])

    # ── module creation ─────────────────────────────────────────────────

    def _enabled(self) -> bool:
        """'code_engine_enabled' Ajustes toggle — same kill switch
        core.code_engine.tool_manager.CodeEngineToolManager.get_tool()
        checks for the Phase 1-4 tool package; checked here too since
        module generation (this class) doesn't go through that registry."""
        try:
            from core import memory
            return memory.is_feature_enabled("code_engine_enabled")
        except Exception:
            return True   # flag lookup failing should never itself block this

    def create_module(self, catalog_id: str) -> bool:
        """Thin wrapper — see _create_module_impl() for the real logic.
        Just adds ensure-before/kill-after Ollama daemon lifecycle around
        the whole (possibly multi-attempt) call, same discipline as
        _run_sleep()/SkillForge/SubagentManager elsewhere in this codebase.
        LLMRouter._ollama() itself only ever ensures the daemon is
        running, never kills it — without this wrapper, every module
        generation that falls back to Ollama left llama-server resident
        indefinitely."""
        if not self._enabled():
            logger.info("create_module(%s): skipped — code_engine_enabled is off", catalog_id)
            return False
        import core.ollama_control as ollama_control
        ollama_control.ensure_ollama_daemon_running()
        _mark_job_active(catalog_id, "create")
        try:
            return self._create_module_impl(catalog_id)
        finally:
            ollama_control.kill_llama_server()
            _clear_job_active(catalog_id)

    def _create_module_impl(self, catalog_id: str) -> bool:
        import core.module_manager as module_manager_mod
        manager = module_manager_mod.manager

        entry = next((m for m in manager.get_catalog() if m.get("id") == catalog_id), None)
        if entry is None:
            logger.error("create_module(%s): no such catalog entry", catalog_id)
            return False
        if entry.get("blocked"):
            logger.info("create_module(%s): blocked — skipping (unblock via set_catalog_blocked)", catalog_id)
            return False

        return self._generate_module_impl(catalog_id, entry, catalog_id=catalog_id)

    def create_ad_hoc_module(self, module_name: str, description: str) -> bool:
        """Same ensure-before/kill-after Ollama lifecycle as create_module()'s
        own wrapper — for a module with no catalog entry at all (e.g. one
        Joan asked for directly in conversation — see
        core.code_engine_dispatch, which is this method's only caller
        today). Builds a synthetic entry (no dependencies/permissions
        declared — the generation prompt already instructs lazy-importing
        anything non-stdlib) and reuses the EXACT SAME generate -> write ->
        sandbox-test -> retry -> install pipeline create_module() uses via
        _generate_module_impl(), just with catalog_id=None so no catalog
        status update ever runs for a module that was never in the
        catalog to begin with."""
        if not self._enabled():
            logger.info("create_ad_hoc_module(%s): skipped — code_engine_enabled is off", module_name)
            return False
        import core.ollama_control as ollama_control
        ollama_control.ensure_ollama_daemon_running()
        _mark_job_active(module_name, "create")
        try:
            entry = {"id": module_name, "name": module_name, "description": description, "dependencies": [], "permissions": []}
            return self._generate_module_impl(module_name, entry, catalog_id=None)
        finally:
            ollama_control.kill_llama_server()
            _clear_job_active(module_name)

    def _generate_module_impl(self, module_name: str, entry: dict, catalog_id: str | None) -> bool:
        """Shared by _create_module_impl() (catalog_id set — every catalog
        status transition below actually runs) and create_ad_hoc_module()
        (catalog_id=None — every catalog_id-guarded call below is skipped,
        since there's no catalog entry to update). Everything else —
        prompt, generation, sandbox test, retry, install — is identical
        either way, so a module Joan asks for directly in conversation
        goes through the exact same proven path as one built from the
        Módulos catalog."""
        import core.module_manager as module_manager_mod
        manager = module_manager_mod.manager

        module_path, manifest_dir, manifest_path = self._module_paths(module_name)
        logger.info("create_module(%s): starting — %s", module_name, entry.get("name"))
        if catalog_id:
            _advance_catalog_status(manager, catalog_id, "developing")

        error_feedback = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            logger.info("create_module(%s): attempt %d/%d", module_name, attempt, MAX_ATTEMPTS)
            prompt = self._build_creation_prompt(entry)
            if error_feedback:
                prompt += f"\n\nEl intento anterior falló con este error — corrígelo:\n{error_feedback}"

            try:
                code = _strip_code_fences(self.router.generate_code(prompt, _CODE_CONTEXT))
            except Exception as e:
                error_feedback = f"LLM generation failed: {e}"
                logger.error("create_module(%s): generation failed (%s)", module_name, e)
                continue

            if not self._safe_path(module_path) or not self._safe_path(manifest_path):
                error_feedback = "generated file path was outside skills/"
                continue

            try:
                with open(module_path, "w", encoding="utf-8") as f:
                    f.write(code)
                os.makedirs(manifest_dir, exist_ok=True)
                manifest = {
                    "name": module_name, "version": "0.1",
                    "description": entry.get("description", ""),
                    "dependencies": entry.get("dependencies", []),
                    "permissions": entry.get("permissions", []),
                    "entry_point": f"{module_name}.py", "auto_start": False,
                    # Extra field beyond _MANIFEST_REQUIRED_FIELDS (additive —
                    # never validated as forbidden) — lets ModuleManager tell
                    # a module Joan asked HUGO to build directly in
                    # conversation (catalog_id=None, no catalog entry at all)
                    # apart from one of the original hand-built skills that
                    # also happens to have no catalog entry (calculator,
                    # weather, etc. predate Code Engine entirely and were
                    # never tagged this way) — see
                    # ModuleManager.get_catalog_with_ad_hoc()'s own docstring
                    # for why "no catalog entry" alone isn't a safe signal.
                    "created_via": "catalog" if catalog_id else "ad_hoc_conversation",
                }
                _stamp_hugo_review_flag(manifest, "created")
                with open(manifest_path, "w", encoding="utf-8") as f:
                    json.dump(manifest, f, ensure_ascii=False, indent=2)
            except Exception as e:
                error_feedback = f"failed to write module files: {e}"
                logger.error("create_module(%s): write failed (%s)", module_name, e)
                continue

            import skills
            skills.reload_skills()   # the new file didn't exist at last discovery — pick it up now

            if catalog_id:
                _advance_catalog_status(manager, catalog_id, "testing")
            ok, detail = self._sandbox_test(module_path)
            if ok:
                ok, detail = self._review_gate(module_path)
            if ok:
                logger.info("create_module(%s): sandbox + review passed — installing", module_name)
                if manager.install(module_name):
                    if catalog_id:
                        # testing -> installed isn't a direct hop in ModuleManager's
                        # transition table (only testing->ready is) — advance through
                        # 'ready' first, then set the version on the final hop.
                        _advance_catalog_status(manager, catalog_id, "ready")
                        manager.update_catalog_status(catalog_id, "installed", version=manifest["version"])
                    logger.info("create_module(%s): installed successfully", module_name)
                    return True
                error_feedback = "ModuleManager.install() failed after a passing sandbox test"
                logger.error("create_module(%s): install() failed", module_name)
                continue

            error_feedback = detail
            logger.warning("create_module(%s): sandbox or review failed — %s", module_name, detail)
            if catalog_id and attempt < MAX_ATTEMPTS:
                manager.update_catalog_status(catalog_id, "developing")

        logger.error("create_module(%s): failed after %d attempts — blocking", module_name, MAX_ATTEMPTS)
        if catalog_id:
            manager.update_catalog_status(catalog_id, "error")
        self._block_and_notify(module_name, error_feedback or "unknown failure")
        return False

    # ── module update ───────────────────────────────────────────────────

    def update_module(self, module_name: str, change_description: str) -> bool:
        """Thin wrapper — see _update_module_impl(). Same ensure-before/
        kill-after reasoning as create_module()'s own wrapper above."""
        if not self._enabled():
            logger.info("update_module(%s): skipped — code_engine_enabled is off", module_name)
            return False
        import core.ollama_control as ollama_control
        ollama_control.ensure_ollama_daemon_running()
        _mark_job_active(module_name, "update")
        try:
            return self._update_module_impl(module_name, change_description)
        finally:
            ollama_control.kill_llama_server()
            _clear_job_active(module_name)

    def _update_module_impl(self, module_name: str, change_description: str) -> bool:
        import core.module_manager as module_manager_mod
        manager = module_manager_mod.manager

        entry = next((m for m in manager.get_catalog() if m.get("id") == module_name), None)
        if entry and entry.get("blocked"):
            logger.info("update_module(%s): blocked — skipping (unblock via set_catalog_blocked)", module_name)
            return False

        module_path, _, manifest_path = self._module_paths(module_name)
        if not self._safe_path(module_path) or not os.path.exists(module_path):
            logger.error("update_module(%s): module file not found", module_name)
            return False

        with open(module_path, "r", encoding="utf-8") as f:
            current_code = f.read()

        if not self._safety_snapshot(module_name):
            logger.error("update_module(%s): safety snapshot failed — aborting", module_name)
            return False

        error_feedback = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            logger.info("update_module(%s): attempt %d/%d", module_name, attempt, MAX_ATTEMPTS)
            prompt = self._build_update_prompt(module_name, current_code, change_description)
            if error_feedback:
                prompt += f"\n\nEl intento anterior falló con este error — corrígelo:\n{error_feedback}"

            try:
                new_code = _strip_code_fences(self.router.generate_code(prompt, _CODE_CONTEXT))
            except Exception as e:
                error_feedback = f"LLM generation failed: {e}"
                logger.error("update_module(%s): generation failed (%s)", module_name, e)
                continue

            if not self._safe_path(module_path):
                logger.critical("update_module(%s): path escaped skills/ — aborting", module_name)
                self._rollback(module_name)
                return False

            try:
                with open(module_path, "w", encoding="utf-8") as f:
                    f.write(new_code)
            except Exception as e:
                error_feedback = f"failed to write updated module: {e}"
                logger.error("update_module(%s): write failed (%s)", module_name, e)
                continue

            import skills
            skills.reload_skills()

            ok, detail = self._sandbox_test(module_path)
            if ok:
                ok, detail = self._review_gate(module_path)
            if ok:
                new_version = _bump_version(manifest_path)
                if manager.update(module_name):
                    manager.update_catalog_status(module_name, "installed", version=new_version)
                    logger.info("update_module(%s): updated to v%s", module_name, new_version)
                    return True
                error_feedback = "ModuleManager.update() failed after a passing sandbox test"
                logger.error("update_module(%s): update() failed", module_name)
                continue

            error_feedback = detail
            logger.warning("update_module(%s): sandbox or review failed — %s", module_name, detail)

        logger.error("update_module(%s): failed after %d attempts — rolling back", module_name, MAX_ATTEMPTS)
        self._rollback(module_name)
        import skills
        skills.reload_skills()   # back to the pre-update code after the checkout above
        manager.update_catalog_status(module_name, "error")
        self._block_and_notify(module_name, error_feedback or "unknown failure")
        return False


code_engine = CodeEngine()
