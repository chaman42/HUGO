# Code Engine — session resume notes (2026-08-10)

Context ran high (854k uncached tokens) mid-verification. This file is for
picking the work back up in a fresh session. Point a new Claude Code
session at this file and it should be able to continue directly.

## What Code Engine is

An autonomous system letting LIRA read, write, test, review, and deploy
code — including her own `skills/` modules — built across 5 phases this
session (Phase 1: file/git/search tools, Phase 2: shell/deps/testing,
Phase 3: Planner/Debugger/Orchestrator autonomous cycle, Phase 4:
CodeReviewer/DocsBrowser/CodeMemory, Phase 5: Deployer). Wired into normal
conversation (not just the API) so Joan can say "crea un módulo de X" /
"actualiza el módulo de X" / "revisa el módulo de X" directly.

## Tonight's real bugs found + fixed (all committed + pushed to `main`)

In rough chronological order — each was found by actually running the
system live, not just unit tests:

1. **`jarvis.py`'s PATH was missing `/usr/local/bin`** — the live app
   could never reach the `ollama` binary at all (silently, in
   production). Fixed by prepending common Homebrew paths at the very
   top of `jarvis.py`.
2. **Non-streaming Ollama call had a fixed 300s timeout** that killed
   genuinely-still-working generations on CPU-only inference. Switched
   `LLMRouter._ollama()` to `stream: true`, which turns the timeout into
   a **stall** timeout (re-arms on every token) instead of a hard total
   cap. Also added periodic progress logging (visible live in the app's
   maintenance panel, since the `code_engine` logger already propagates
   to `jarvis.py`'s `SocketIOLogHandler`).
3. **120s stall timeout was still too short** for real prompts' first
   token on this hardware — bumped to 600s.
4. **Every individual LLM call killed + reloaded the Ollama model**
   around itself, even mid-cycle — new
   `core.ollama_control.mark_code_engine_cycle_running()` /
   `clear_code_engine_cycle_running()` / `is_code_engine_cycle_running()`
   (lock file at `data/code_engine_cycle.lock`) keeps the model warm for
   a whole `Orchestrator.execute_goal()` cycle; `kill_llama_server()`
   itself now checks this lock.
5. **`_REPO_ROOT` in `core/code_engine/__init__.py` was one directory too
   shallow** (`core/` instead of the real repo root) — `_sandbox_test()`'s
   subprocess (and `_safety_snapshot`/`_rollback`) were silently failing
   on EVERY module generation, old and new, catalog-based and ad-hoc.
   One-line fix (`"..", ".."` instead of `".."`), verified against both
   an ad-hoc module and a real catalog entry.
6. **Orchestrator's generic freelance step-execution is wrong for
   "create a module"** — a live end-to-end test produced bare functions
   (not `LiraSkill` subclasses), a stray duplicate file, and never
   actually called `ModuleManager.install()`. Rewired conversational
   create/update (`core/code_engine_dispatch.py`) to call
   `CodeEngine.create_ad_hoc_module()` / `update_module()` directly
   instead of `Orchestrator.execute_goal()`. `create_ad_hoc_module()` is
   new (in `core/code_engine/__init__.py`) — same
   generate→write→sandbox-test→retry→install pipeline as the existing
   catalog-based `create_module()`, refactored into shared
   `_generate_module_impl()` with `catalog_id=None`.
7. **CodeReviewer never ran on module generation at all** (only
   Orchestrator's own cycle used to run it, which is now bypassed for
   modules) — added `_review_gate()` in `core/code_engine/__init__.py`,
   runs after sandbox passes, before install, in both create and update.
   Verified it blocks real insecure code (`os.system()` on unsanitized
   input).
8. **`code_engine_cycle.lock` had no liveness check**, same bug class as
   an earlier `core/sleep_control.py` fix this session — a crashed
   jarvis.py would leave it looking "running" for up to an hour. Now
   writes the owning PID and verifies it's alive + genuinely `jarvis.py`
   (`core/ollama_control.py`).
9. **No wall-clock budget on `Orchestrator.execute_goal()`** — only
   retry-count was capped, not elapsed time. Added
   `MAX_GOAL_WALL_CLOCK_SECONDS = 45 * 60`.
10. **Módulos catalog view didn't show ad-hoc LIRA-created modules** —
    added `ModuleManager.get_catalog_with_ad_hoc()`, tags manifests with
    `created_via: "ad_hoc_conversation"` vs `"catalog"` (NOT just "no
    catalog entry" — several original hand-built skills like calculator/
    weather/discord_bridge also have no catalog entry and must NOT be
    mislabeled). `GET /api/modules/catalog` now uses this.
11. **`code_engine_dispatch.review()` ignored its own `topic` param** —
    always ran `review_full_project()` (up to 25 files × 2 LLM calls)
    even when a specific module was named. Fixed to scope to
    `review_file()` on the matching module when `topic` names one.
    Verified live: correctly reviewed just `lanzar_moneda.py` (~5 min)
    instead of the whole project.
12. **`dispatch_module_task('update', topic)` slugified the WHOLE spoken
    phrase as the module name** — "actualiza el módulo de lanzar_moneda
    para que también pueda decir 50/50" tried to update a module named
    `lanzar_moneda_tambien_pueda_decir_50_50` (doesn't exist, failed
    safely but uselessly). Added `resolve_existing_module()` — matches
    progressively shorter prefixes of the topic's words against the real
    installed module set, bidirectionally (so "discord para que..."
    correctly resolves to the real name `discord_bridge`, not just an
    exact match on "discord"). **Known remaining gap**: doesn't solve
    cross-language matching (Spanish "calendario" won't resolve to the
    English-named `calendar` module) — not attempted, would need real
    translation/fuzzy matching.

## What's verified live (real LLM calls, not mocks) tonight

- Conversational **create** ("crea un módulo de lanzar una moneda") —
  full success, multiple times. `skills/lanzar_moneda.py` is real,
  installed, committed, and callable (`skill_dispatch` correctly routes
  "tira una moneda" to it through the normal conversation pipeline).
- Conversational **review** ("revisa el módulo de lanzar_moneda") —
  correctly scoped to one file after fix #11, real result: "2
  problema(s) encontrado(s) — 1 advertencia(s), 1 sugerencia(s)".
- CodeReviewer's security gate actually blocking bad code (fix #7).
- Phase 1-2 tools (FileSystem, CodeSearch, Git, DependencyManager,
  Testing, Shell-denied-correctly) — fast direct smoke tests, all fine.

## IN PROGRESS right now — check this first

An update-path test is running independently on the live `jarvis.py`
process (started ~01:00, this is a real background thread inside that
app, NOT tied to this chat session — it keeps running regardless):

```
actualiza el módulo de lanzar_moneda para que también pueda decir 50/50
```//

As of context handoff: generation completed (713 chars, 355s), and the
`_review_gate()` step (2 more LLM calls: find_bugs + check_quality) was
in progress. **To check the outcome:**

```bash
tail -50 logs/code_engine.log   # look for "updated to v..." (success) or
                                  # "sandbox or review failed" / "failed after 3 attempts" (blocked)
cat data/modules.json | grep -A5 lanzar_moneda   # version should be 0.2 if it succeeded
git log --oneline -5             # look for an auto "checkpoint:" or similar around this update
```

If it's STILL running (check `ps aux | grep llama-server` — should show a
process actively using CPU), just wait/monitor
`logs/code_engine.log` the same way — each LLM call logs progress every
10s (`generando con qwen2.5-coder... N caracteres, Ns transcurridos`).

**If a stray `llama-server` process is idle (0% CPU, no recent log
activity) when you pick this up, it's orphaned — kill it** (`kill -9
<pid>`) same as several times earlier this session.

## Still genuinely unverified (do these next)

1. **`Deployer.deploy_lira_module()`** — depends on the same
   `_sandbox_test()` fixed in #5, never re-tested since. Needs `deploy`
   permission temporarily flipped to `true` in
   `data/code_engine_permissions.json` (default `false` — **revert after
   testing**). Test against the already-installed `skills/lanzar_moneda.py`.
2. **`Orchestrator.execute_goal()`'s generic (non-module) cycle** — the
   actual original Phase 3 use case via `POST /api/code-engine/orchestrate`
   — only ever exercised indirectly through the flawed module-creation
   attempt (now bypassed for modules specifically). Worth one real test
   with a non-module goal against `skills/` to confirm the keep-warm/
   streaming/wall-clock fixes hold up there too.
3. **DocsBrowser** — needs `internet` permission (`data/code_engine_permissions.json`,
   default `false`). Untested live all session.
4. **CodeMemory** — untested since Phase 5, no specific reason to doubt
   it but no fresh verification either.

## Known, accepted gaps (deliberately not fixed — deprioritized)

- Preference-learning (Phase 4) no longer fires for conversational
  module create/update, since that path no longer goes through
  `Orchestrator.execute_goal()` (which used to run it).
- No token/cost budget tracking beyond the new wall-clock cap.
- Cross-language module-name resolution (see bug #12's own note).

## Environment facts that matter for continuing this work

- **No `DEEPSEEK_API_KEY` set** — Joan's deliberate choice. Every real
  generation goes through slow CPU-only Ollama (`qwen2.5-coder`). Single
  LLM calls commonly take 60–700+ seconds. A full module create/update
  cycle (generation + review gate's 2 calls) can take 10-15+ minutes.
  **Do not be surprised by this — it's expected, not a bug.**
- **`code_engine_enabled`** (Ajustes toggle) and **`deploy`** permission
  (`data/code_engine_permissions.json`) — `deploy` is off by default;
  flip it temporarily for Deployer tests, then revert.
- **Git workflow**: this repo has a CI job that bumps `electron/package.json`'s
  version and pushes back to `main` on every push (with `[skip ci]`), AND
  live runtime `data/*.json` files churn constantly from the running app.
  Convention used all session: `git fetch`, stash CODE changes and LIVE
  DATA changes **separately** (two stash calls), `git rebase origin/main`,
  pop code stash, pop data stash (in that order), verify, commit, push.
  Never `git add -A`.
- **After any backend code change**: restart via
  `curl -X POST http://localhost:8079/api/restart`, then poll
  `http://localhost:8079/api/health` for `jarvis_ready: true` before
  testing (usually ~15-20s, occasionally needs a couple retries).
- **Watching a live test**: use the `Monitor` tool tailing
  `logs/code_engine.log` AND `logs/activity.log` together (grep for
  `generación completa|Traceback|Jarvis: [^¿]` — the `[^¿]` excludes
  unrelated proactive-comment noise), not blind polling/sleeping. Give
  it a generous timeout (20-30 min) given real call durations above.
- **Test cleanup discipline**: every ad-hoc test module
  (`skills/test_*.py` + `skills/manifests/test_*/`) and any stray
  `data/modules.json`/`data/modules_catalog.json` entries from testing
  must be cleaned up before committing. Orphaned `llama-server`
  processes (from interrupted test scripts) need `kill -9` — check
  `ps aux | grep llama-server` periodically.

## Key files touched tonight (for a quick `git log -p` review if needed)

`jarvis.py`, `core/code_engine/__init__.py`,
`core/code_engine/tools/orchestrator.py`, `core/code_engine_dispatch.py`,
`core/ollama_control.py`, `core/module_manager.py`,
`core/routes_control.py`, `core/intent.py`, `core/commands.py`,
`scripts/ollama_guard.py`.

Latest commit at handoff time: `cc6461f` "Fix update() resolving the
whole spoken phrase as the module name" (already pushed to `main`).
