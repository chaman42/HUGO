# Bug Hunter — guide for future sessions

Orientation doc for whoever (human or Claude Code session) picks up work
on Bug Hunter next. Not a session log — see git history for that. This
covers what the feature is, the hard rules it operates under, what's
built, and the mistakes already made once so they don't get made again.

## What it is

An app inside LIRA (`ui/index.html`'s `#section-bughunter`, tabs: Scope /
Programas / Status / Scan / Hallazgos / Supervisión) that runs a passive
recon + vulnerability scanner against bug-bounty targets Joan has
explicitly authorized, and prepares (never submits) findings for him to
review and report himself.

Files:
- `core/bughunter_scan.py` (~1500 lines) — the scan engine, all checks.
- `core/bughunter_routes.py` (~550 lines) — Flask routes, data
  persistence, Auto Mode's scan-thread, dedup/auto-resolve logic.
- `core/background_loops.py` — `_bughunter_auto_loop` /
  `_bughunter_auto_tick` / `_bughunter_maybe_discover_programs` (Auto
  Mode's background thread — scan rotation + hourly program discovery).
- `ui/js/bughunter.js` (~850 lines) + `ui/css/bughunter.css` — frontend.
- `data/bughunter_scope.json` — the authorization boundary (see below).
- `data/bughunter_findings.json`, `data/bughunter_state.json`,
  `data/bughunter_suggestions.json`.

## The hard rule

Read `core/bughunter_routes.py`'s module docstring and
`core/bughunter_scan.py`'s module docstring in full before touching
either file — they're kept current and are the actual source of truth,
this doc just orients you faster. The short version: **passive/read-only
checks only, never touch anything outside Scope, never auto-submit a
finding anywhere.** If a change would make any check more active (sends a
payload, brute-forces, mutates target state), that's a deliberate,
separately-reviewed decision — don't slip it in quietly.

## Scope is the whole safety boundary — treat it accordingly

`data/bughunter_scope.json` gates literally everything the scan engine
and Auto Mode are allowed to touch. Getting an entry wrong here isn't a
UI bug, it's LIRA scanning something nobody authorized. Two real
incidents already happened this session:

1. **Domain auto-guessing from a suggestion's URL** guessed the bounty
   *platform's* own domain (`yeswehack.com`) instead of the target
   company's, because the suggestion URL was a listing page hosted on the
   platform, not the target's site. Got saved to Scope, would have been
   scanned next by Auto Mode's rotation. Fixed by removing domain
   auto-guessing entirely — `_bhPromoteSuggestion` in `ui/js/bughunter.js`
   now always leaves the domain blank, forcing Joan to type it. **Don't
   reintroduce domain-guessing from a URL.** If asked to make Scope-add
   more convenient again, find a different way.
2. **Several real bug-bounty programs explicitly ban automated/passive
   scanning tools** in their rules of engagement — this is the norm, not
   the exception, among programs actually checked (see table below). A
   program not caring in spirit doesn't matter; the *platform* enforces
   its written rules (account bans, sanctions, loss of legal safe-harbor
   protection) independent of how the underlying company feels. Fixed by
   adding a required `automation_allowed` confirmation to the Scope-add
   flow — enforced both in the frontend (checkbox, must be checked) and
   server-side (`api_bughunter_add_scope` in `core/bughunter_routes.py`
   rejects the POST with 400 if `automation_allowed` isn't `true`).
   **This is a deliberate friction point, not decoration — don't remove
   it or default it to `true`.** It cannot verify Joan actually checked
   the real rules; it only forces a conscious "yes I did."

**Before adding anything to Scope**, verify and document (in the entry's
`notes` field, same style as the existing entries):
- The target's *real* domain — from the company's own site/trust-center,
  not guessed from a bounty-platform listing URL.
- The program is public (not invite-only).
- The program's actual rules of engagement don't prohibit automated
  tools — read the real text, don't infer from vibes. Distinguish
  **methodology bans** ("do not use automatic scanners," "raw or
  lightly-edited output of automated tools... won't be considered") —
  these are NOT cured by Joan reviewing before submitting — from
  **submission-eligibility restrictions** ("reports from automated tools
  aren't eligible for reward") and **general human-validation
  requirements** (HackerOne's "Hackbot" policy, Bugcrowd's 2026 "AI slop"
  policy) — these last two ARE satisfied by Bug Hunter's existing design
  (LIRA never auto-submits, Joan reviews/edits/submits every report
  himself).
- Concrete in-scope domains/wildcards and known exclusions, if available.
  A vague "all our products" from a marketing/trust page is not
  sufficient — try to get the platform's actual scope table.

## Known-verified programs (as of 2026-08-18)

| Program | Platform | Verdict | Why |
|---|---|---|---|
| Cloudflare Public Bug Bounty | HackerOne | ✅ in Scope | Real scope + exclusions verified from hackerone.com/cloudflare. Automation: requires narrow IP scope (matches — cloudflare.com only) + human validation before submit (matches Bug Hunter design). |
| LastPass Bug Bounty | Bugcrowd | ✅ in Scope, caveated | Public, automation not banned (only cautioned re: false positives). Bugcrowd's actual scope table is JS-rendered and couldn't be read — no verified exclusions list, flagged honestly in the entry's notes. Treat any discovered subdomain as needing manual review, not auto-safe. |
| Intigriti "Exact" VDP | Intigriti | ❌ | Invite-only, AND explicit methodology ban ("do not use automatic scanners"). |
| YesWeHack BIND 9 | YesWeHack | ❌ | In-scope asset is a source-code repo, not a web target. AND explicit methodology ban, penalizes "lightly-edited output of automated tools." |
| Dropbox VDP | Intigriti | ❌ | Reports from automated tools/scans explicitly not eligible. |
| GitKraken | (unclear) | ❌ | "Strictly prohibits the use of automated scanners." |
| Screenly | self-hosted | ❌ | Program paused as of 2026-05, independent of automation policy. |
| Supabase | HackerOne | ❌ (for now) | Requires emailing them *before* any automated scan — a manual prerequisite step, not something reviewable after the fact. Also restricts scanning to your own project, not other customers' `*.supabase.co` — a real risk given crt.sh-based subdomain discovery. |
| RoboForm | self-hosted | ❌ (for now) | Automation policy plausibly fine (bans only service-degrading scans), but scope listed vaguely ("web services, APIs, official domains") — no concrete domain list found. |
| Google VRP | bughunters.google.com | ❌ (not pursued) | Page unreadable via available tools (JS-rendered). Separately, Google's 2026 policy overhaul explicitly de-prioritizes automated/AI-driven findings in favor of "concrete proof, feasible exploit demonstrations" — a poor fit for what this scanner produces even where technically allowed. |

Re-verify before trusting any of these if a lot of time has passed —
policies change.

## Tooling limitation to know about up front

The Chrome browser extension is **not connected** in this environment
(`mcp__claude-in-chrome__tabs_context_mcp` returns "Browser extension is
not connected"). `WebFetch` can't render JS — Bugcrowd, Intigriti, and
YesWeHack's actual scope tables are JS-rendered SPAs and come back empty
(title/nav only). HackerOne's `bughunters.google.com` and some
`hackerone.com` program pages actively 403 WebFetch. This is *why*
several programs above couldn't get a concrete scope verified — not
necessarily because the information doesn't exist. If the Chrome
extension gets connected in a future session, it's worth re-attempting
the ❌ (for now) entries above with real browser rendering before writing
them off permanently.

## Scanner architecture (as of 2026-08-18, ~16 checks)

All in `core/bughunter_scan.py`, called from `run_scan(target,
on_progress)`. Every check takes `base_url`/`headers`/`body` already
fetched once at the top (the "reachability gate") wherever possible —
avoid adding a check that does its own redundant GET if the data's
already in hand.

- Headers/cookies: CSP (presence + quality — unsafe-inline/unsafe-eval/
  wildcard), HSTS, X-Frame-Options, Permissions-Policy, COOP, CORP,
  Server/X-Powered-By version disclosure, cookie flags (Secure/HttpOnly/
  SameSite/`__Host-`-`__Secure-` prefix hygiene), mixed content.
- TLS: protocol version, cert expiry.
- HTTPS enforcement: plain-http:// not redirecting.
- security.txt presence.
- Sensitive path exposure: fixed list (`_SENSITIVE_PATHS`) — same
  function (`_check_sensitive_paths`) also powers the Wayback and
  robots/sitemap checks below via a dynamic path list + `no_auto_resolve`
  flag (see Findings hygiene).
- Wayback Machine: historically-indexed sensitive-looking paths,
  re-checked live before becoming a finding.
- robots.txt/sitemap.xml: same live-recheck pattern, disallowed/listed
  paths filtered through the same interesting-keyword list.
- Error/debug disclosure: free, reused from the reachability body.
- CORS misconfiguration (reflected origin + credentials).
- Open redirect.
- JS analysis (`_check_js_secrets`): hardcoded secrets (fixed vendor
  key-format patterns, not generic heuristics), exposed source maps, API
  endpoint extraction (informational only, surfaced via `on_progress`,
  never a finding).
- SPF/DMARC: DNS-only, via the system `dig` binary (no dnspython
  dependency).
- GitHub exposure: dork search via `core.tools_search.search_web()`
  (same infra as program discovery), never touches the target.
- Known-vulnerable version fingerprinting + NVD CVE cross-reference
  (`_check_known_vulnerable_versions`) — deliberately the most
  conservative check in the file (Joan's explicit instruction: prefer
  false negative over false positive here). Curated product list only,
  clean-version-string requirement, NVD resolves version ranges (no
  hand-rolled parsing), only CRITICAL/HIGH surfaced, capped at "alta"
  severity even if NVD says CRITICAL, cached per (product, version) for
  6h (`_nvd_cache`).
- Subdomain discovery via crt.sh, feeding: takeover-fingerprint check
  (sample of 8) AND the full check suite above run again against a
  bounded sample of 5 subdomains (`_run_subdomain_check_suite`,
  `_MAX_SUBDOMAINS_FULL_SCAN`) — deliberately excludes domain-level/
  third-party-API-heavy checks (SPF/DMARC, GitHub, Wayback/robots/
  sitemap) since those don't meaningfully differ per subdomain. Findings
  from this path get their title prefixed `[subdomain] ` — load-bearing,
  not cosmetic: it's what keeps different subdomains' findings from
  colliding under the dedup key (see below).

## Findings hygiene

`core/bughunter_routes.py::_run_scan_thread` on every scan:

1. **Dedup by `(target, title)`** — but a title matching an existing
   `resuelto` (auto-resolved) entry gets **reopened** (`status` →
   `nuevo`, `reappeared_at` set) instead of silently dropped — a
   regression is exactly as real as a new finding. A title matching
   `enviado`/`duplicado`/`descartado` stays untouched (Joan's own
   terminal calls, or already-submitted).
2. **Auto-resolve** — an open (`nuevo`/`borrador`) finding whose host was
   actually re-checked this run (`checked_hosts`, returned by
   `run_scan` — NOT just any subdomain crt.sh ever mentioned) and that's
   tagged `auto_resolvable: true` (set at generation time in
   `bughunter_scan.py` — `False` for anything sourced from third-party
   search/discovery like GitHub/Wayback/robots-sitemap, since their
   absence next scan isn't reliable evidence the issue is gone) and
   didn't reappear this scan → marked `resuelto`.
3. Status values: `nuevo` / `borrador` / `enviado` / `duplicado` /
   `resuelto` (auto, reopens on regression) / `descartado` (Joan's manual
   false-positive/accepted-risk call, does NOT reopen — set only via the
   Findings UI, never by the scanner).

## Frontend gotchas

- **`_bhScopeAddDraft`** (module-level state in `ui/js/bughunter.js`) is
  what the Scope-add form always renders from and writes to on every
  keystroke — not the DOM directly. This exists because a
  `bughunter_updated` socket event (e.g. from `_bhPromoteSuggestion`'s
  own dismiss-suggestion call) triggers a full `_loadBughunterData()`
  reload that used to wipe an in-progress form back to blank. If you add
  a new field to this form, wire it through the draft object the same
  way, don't just poke `.value` after render.
- **Any change to `ui/js/*.js`, `ui/css/*.css`, or `ui/index.html`
  requires bumping `ui/sw.js`'s `CACHE` const AND doing a full disk-cache
  nuke before the running app reflects it** — see the
  `feedback_lira_app_restart` memory / `ARCHITECTURE.md`'s "Running/
  restarting the app during development" section for the exact procedure
  (`pkill` the LIRA process, `rm -rf` the 5 cache folders under
  `~/Library/Application Support/LIRA/`, `open` again, wait ~20s, check
  `logs/launcher.log` for "Jarvis ready"). A plain `pkill`+`open` is NOT
  enough for frontend changes — confirmed to bite this exact feature
  more than once this session. Backend-only Python changes just need the
  process restarted, no cache nuke.

## Auto Mode

`core/background_loops.py::_bughunter_auto_tick`, gated on
`state["auto_mode"]`. Rotates through Scope (skips if a scan's already
running), interval `auto_mode_interval_hours` in `bughunter_state.json`
(currently 0.1667h ≈ 10 min — tuned for a small Scope, would need
revisiting if Scope grows a lot, since scans now do meaningfully more
work per target — see "still open" below). Separately runs
`_bughunter_maybe_discover_programs` roughly hourly — searches
HackerOne/Bugcrowd/Intigriti/YesWeHack for new candidate programs, adds
them to `bughunter_suggestions.json`, never touches Scope directly (see
`_SUGGESTION_EXCLUDE_PATH_KEYWORDS`/`_SUGGESTION_EXCLUDE_EXACT_PATHS` for
what gets filtered out as noise before it's even surfaced as a
suggestion — directory/marketing pages, not individual programs).

Critical-severity findings bypass the normal proactive gate and
force-announce via TTS (`_announce_critical_findings`) — reopened
critical regressions trigger this too, not just brand-new ones.

## Still open / not built

- **GraphQL introspection check** — flagged multiple times, needs an
  explicit go-ahead since it's the first check that would POST a
  constructed query body rather than just reading what's there.
- **Shallow same-domain crawl** beyond each host's root `/` — bigger
  lift, would surface app routes the homepage never reveals.
- **Per-scan time budget/timeout guard** — nothing currently caps total
  scan duration; a slow external API (NVD, GitHub search, Wayback) could
  make one scan run long and hold `_scan_lock` the whole time, starving
  Auto Mode's next tick for every other target, not just the slow one.
- **Auto Mode's flat rescan cadence** — same interval for a target that's
  been clean for weeks as one that just had a critical finding.
- Only 2 verified Scope entries as of this writing (Cloudflare, LastPass)
  — see the tooling-limitation note above for why growing this list
  further has been slow going.
