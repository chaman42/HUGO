"""Flask routes: BUG HUNTER app persistence — GET /api/bughunter (all 4
data files in one call, same "avoid N round trips on section-open" reasoning
as core/estudio_routes.py's /api/estudio) plus the mutation endpoints the
front end (ui/js/bughunter.js) needs for Scope/Findings/Auto Mode/Scan/
Suggestions.

Phase 2 (see "Bug Hunter Backend Plan" memory): POST /api/bughunter/scan
runs the real scan engine (core/bughunter_scan.py, passive/read-only
checks) on a background thread and appends whatever it finds to
data/bughunter_findings.json — via start_scan() below, the same function
Phase 3's Auto Mode loop calls.

Phase 3: core.background_loops._bughunter_auto_loop cycles through Scope
on its own while state["auto_mode"] is true, calling start_scan() exactly
like the manual Scan tab does — it is NOT a separate code path with its
own scanning logic, so the same non-destructive checks and the same
_scan_lock (one scan at a time, whole app, manual or auto) apply either
way. The same loop also runs core.bughunter_scan.discover_program_suggestions
on a much slower cadence (once an hour, not once a scan-rotation) to
populate data/bughunter_suggestions.json — candidate bounty programs found
via web search, surfaced for Joan to review. Discovery NEVER writes to
Scope itself; promoting a suggestion is a manual action in the UI (opens
the Scope 'add target' form pre-filled, domain left for Joan to fill in
after actually reading the program's real scope page).

HARD RULE governing anything that reads/writes these files, and the scan
engine this module calls into: LIRA never scans outside
data/bughunter_scope.json, never runs anything but a non-destructive
proof-of-concept check, and never auto-submits a finding anywhere — see
the "Bug Hunter Constraints" memory. Discovered suggestions are explicitly
exempt from ever becoming a Scope entry without Joan's own action.

data/bughunter_scope.json — target allowlist:
    {"id": str, "name": str, "domain": str, "platform": str, "notes": str,
     "automation_allowed": bool, "added_at": str (ISO)}
    automation_allowed is required True on every NEW entry as of
    2026-08-18 (api_bughunter_add_scope rejects the POST otherwise) — a
    human confirmation that the program's real rules of engagement permit
    automated/passive scanning, since several real programs explicitly
    prohibit it (found live: Intigriti and YesWeHack examples in that
    route's docstring). Entries added before this field existed (e.g. the
    original Cloudflare entry) won't have it — this is an add-time gate
    only, not a scan-time re-check, so it doesn't retroactively affect
    already-saved targets.
data/bughunter_findings.json:
    {"id": str, "target": str, "title": str,
     "severity": "critica"|"alta"|"media"|"baja",
     "status": "nuevo"|"borrador"|"enviado"|"duplicado"|"resuelto"|"descartado",
     "summary": str, "description": str, "repro_steps": list[str],
     "impact": str, "fix_suggestion": str, "discovered_at": str (ISO),
     "auto_resolvable": bool, "resolved_at": str (ISO), "reappeared_at":
     str (ISO), all three optional}
    auto_resolvable is set by core.bughunter_scan.run_scan — only True lets
    _run_scan_thread's auto-resolve step set status="resuelto" +
    resolved_at when a repeat scan on the same (re-checked) host no longer
    detects the issue. Never applied to "enviado"/"duplicado"/already-
    "resuelto" findings, and never to findings sourced from third-party
    search/discovery (GitHub/Wayback/robots-sitemap) since their absence
    next scan isn't reliable evidence the issue is actually gone.
    "resuelto" is not a one-way door: if a later scan re-detects the same
    (target, title), _run_scan_thread reopens the existing entry back to
    status="nuevo" and sets reappeared_at (clearing resolved_at) instead
    of silently dropping the detection as a duplicate — a fixed issue
    regressing is exactly as real as a brand-new one. "descartado" (set
    only manually, via POST /api/bughunter/findings/status) is the
    opposite case — Joan's own "false positive" or "accepted risk" call —
    and unlike "resuelto" it is NOT reopened on rediscovery, same
    permanently-closed treatment as "enviado"/"duplicado".
    next scan isn't reliable evidence the issue is actually gone.
data/bughunter_suggestions.json — candidate programs found by discovery:
    {"id": str, "name": str, "platform": str, "url": str, "note": str,
     "status": "pendiente"|"descartada", "discovered_at": str (ISO)}
data/bughunter_state.json — NOT a list, a single dict:
    {"auto_mode": bool, "auto_mode_interval_hours": float,
     "current_activity": str | None,
     "last_run": {"target": str, "when": str (ISO), "findings_count": int} | None,
     "activity_log": [{"time": str (ISO), "message": str}],
     "auto_mode_last_target_id": str | None,  (Phase 3 — set only when the
     "auto_mode_last_tick": str (ISO) | None,   auto loop itself starts a
     "auto_mode_last_discovery": str (ISO) | None}   scan/discovery run,
     never by a manual one — see core.background_loops._bughunter_auto_tick)
"""
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timezone

from flask import jsonify, request

from core.server import app
from core import bughunter_scan

logger = logging.getLogger(__name__)

# One scan at a time, whole-app — simple enough for a single-user tool, and
# it keeps "what is LIRA doing right now" (the Status tab) unambiguous.
_scan_lock = threading.Lock()

# Guards every load-mutate-save cycle on the 4 data files below, same
# pattern ~20 other frequently-written data/*.json modules already use
# (core.notifications, core.preferences, etc.) — bughunter_routes was the
# one outlier without it. Closes a real lost-update race: Auto Mode's
# hourly discovery (core.background_loops._bughunter_maybe_discover_programs)
# reads suggestions, does several slow web searches, then writes back —
# without this lock a dismiss/promote landing mid-search got silently
# reverted when discovery's stale snapshot saved over it.
_data_lock = threading.Lock()

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR  = os.path.join(_REPO_ROOT, "data")

_SCOPE_PATH       = os.path.join(_DATA_DIR, "bughunter_scope.json")
_FINDINGS_PATH    = os.path.join(_DATA_DIR, "bughunter_findings.json")
_STATE_PATH       = os.path.join(_DATA_DIR, "bughunter_state.json")
_SUGGESTIONS_PATH = os.path.join(_DATA_DIR, "bughunter_suggestions.json")

_MAX_ACTIVITY_LOG = 100

# 0.1667h (~10 min) — deliberately brisker than LIRA's other background
# loops (Joan was explicit, 2026-08-18, that enabling Auto Mode is a "go
# now" signal), but NOT so brisk it re-hammers a small Scope pointlessly:
# the original ~72s default was tuned for cycling through MANY targets
# quickly, but with only 1-2 targets in Scope it just re-scans the same
# target over and over finding nothing new — Joan adjusted this down
# (2026-08-18) once Scope had just one real target. Tunable per-install by
# editing data/bughunter_state.json directly (no UI control for it yet).
_DEFAULT_STATE = {
    "auto_mode": False,
    "auto_mode_interval_hours": 0.1667,
    "current_activity": None,
    "last_run": None,
    "activity_log": [],
    "auto_mode_last_target_id": None,
    "auto_mode_last_tick": None,
    "auto_mode_last_discovery": None,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_json_array(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _save_json(path: str, data) -> bool:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except OSError as exc:
        logger.error("Failed to write %s: %s", path, exc)
        return False


def _load_state() -> dict:
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else dict(_DEFAULT_STATE)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return dict(_DEFAULT_STATE)


def _emit_updated():
    try:
        import core.server as server_mod
        server_mod.socketio.emit("bughunter_updated", {})
    except Exception:
        pass


def _log_activity(state: dict, message: str) -> None:
    state.setdefault("activity_log", []).insert(0, {"time": _now_iso(), "message": message})
    state["activity_log"] = state["activity_log"][:_MAX_ACTIVITY_LOG]


@app.route("/api/bughunter")
def api_bughunter():
    """Backs the BUG HUNTER app launcher section — Scope/Programas/Status/
    Scan/Hallazgos all render on section-open, so one call covers all 4
    files. Dismissed suggestions are filtered out here (kept on disk so
    core.bughunter_scan.discover_program_suggestions doesn't re-suggest
    the same URL) — the frontend only ever sees pending ones."""
    try:
        suggestions = [s for s in _load_json_array(_SUGGESTIONS_PATH) if s.get("status") != "descartada"]
        return jsonify({
            "scope":       _load_json_array(_SCOPE_PATH),
            "findings":    _load_json_array(_FINDINGS_PATH),
            "state":       _load_state(),
            "suggestions": suggestions,
        })
    except Exception as exc:
        logger.error("Failed to load BUG HUNTER data: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/bughunter/scope", methods=["POST", "OPTIONS"])
def api_bughunter_add_scope():
    """Scope tab's '+ Añadir objetivo' form. Body: {name, domain, platform,
    notes, automation_allowed}. This is the safety boundary — the scan
    engine and Auto Mode only ever pick a target from this file.
    automation_allowed must be true (enforced server-side, not just as a
    frontend checkbox someone could bypass by calling this route
    directly) — found live 2026-08-18 that several real bug bounty
    programs explicitly PROHIBIT automated/passive scanning tools in their
    rules of engagement (e.g. Intigriti's "Exact" VDP: "do not use
    automatic scanners"; YesWeHack's BIND 9 program penalizes "raw or
    lightly-edited output of automated tools... AI assistants and agents"
    with possible removal). Nothing here can verify Joan actually checked
    the real rules — this is a deliberate friction point, not a
    verification, forcing a conscious "yes I checked" instead of silently
    defaulting to permitted."""
    if request.method == "OPTIONS":
        return "", 204
    data = request.get_json(force=True, silent=True) or {}
    name   = (data.get("name") or "").strip()
    domain = (data.get("domain") or "").strip()
    if not name or not domain:
        return jsonify({"error": "name and domain are required"}), 400
    if not data.get("automation_allowed"):
        return jsonify({"error": "automation_allowed must be confirmed — check the program's real rules of engagement first"}), 400

    target = {
        "id":                 uuid.uuid4().hex[:12],
        "name":               name,
        "domain":             domain,
        "platform":           (data.get("platform") or "").strip() or "—",
        "notes":              (data.get("notes") or "").strip() or "Sin notas de alcance.",
        "automation_allowed": True,
        "added_at":           _now_iso(),
    }
    with _data_lock:
        scope = _load_json_array(_SCOPE_PATH)
        scope.append(target)
        if not _save_json(_SCOPE_PATH, scope):
            return jsonify({"error": "failed to save"}), 500

        state = _load_state()
        _log_activity(state, f"{name} añadido a Scope")
        _save_json(_STATE_PATH, state)

    _emit_updated()
    return jsonify({"ok": True, "target": target})


@app.route("/api/bughunter/scope/delete", methods=["POST", "OPTIONS"])
def api_bughunter_delete_scope():
    """Body: {id}. Removing a target from Scope is itself a safety action
    (it's the allowlist), so this is a plain hard delete, no soft-delete/
    archive step."""
    if request.method == "OPTIONS":
        return "", 204
    data = request.get_json(force=True, silent=True) or {}
    target_id = data.get("id")
    if not target_id:
        return jsonify({"error": "id is required"}), 400

    with _data_lock:
        scope = _load_json_array(_SCOPE_PATH)
        remaining = [t for t in scope if t.get("id") != target_id]
        if len(remaining) == len(scope):
            return jsonify({"error": "not found"}), 404
        if not _save_json(_SCOPE_PATH, remaining):
            return jsonify({"error": "failed to save"}), 500

    _emit_updated()
    return jsonify({"ok": True})


@app.route("/api/bughunter/findings/status", methods=["POST", "OPTIONS"])
def api_bughunter_finding_status():
    """Body: {id, status}. Joan marks a finding 'enviado' manually after
    actually submitting the copied report himself — LIRA never sets this
    on her own. 'resuelto' is normally set automatically by
    _run_scan_thread's auto-resolve step (see below) when a repeat scan
    no longer detects the issue, but is accepted here too so Joan can
    apply/undo it manually if needed. 'descartado' is Joan's own manual
    triage call — "I looked at this, it's a false positive" or "we accept
    this risk" — distinct from 'duplicado' (someone else already reported
    it) and from 'resuelto' (LIRA re-verified it's gone): unlike
    'resuelto', a 'descartado' finding does NOT reopen on rediscovery —
    see _run_scan_thread's dedup, which only reopens "resuelto" entries."""
    if request.method == "OPTIONS":
        return "", 204
    data = request.get_json(force=True, silent=True) or {}
    finding_id = data.get("id")
    status = data.get("status")
    if status not in ("nuevo", "borrador", "enviado", "duplicado", "resuelto", "descartado"):
        return jsonify({"error": "invalid status"}), 400

    with _data_lock:
        findings = _load_json_array(_FINDINGS_PATH)
        match = next((f for f in findings if f.get("id") == finding_id), None)
        if not match:
            return jsonify({"error": "not found"}), 404
        match["status"] = status
        if not _save_json(_FINDINGS_PATH, findings):
            return jsonify({"error": "failed to save"}), 500

    _emit_updated()
    return jsonify({"ok": True})


@app.route("/api/bughunter/suggestions/dismiss", methods=["POST", "OPTIONS"])
def api_bughunter_dismiss_suggestion():
    """Body: {id}. Used both by the Programas tab's own 'Descartar' button
    AND (implicitly) by 'Añadir a Scope' — either way the suggestion is
    'handled' and shouldn't be re-surfaced. Kept on disk with
    status='descartada' rather than deleted, so
    core.bughunter_scan.discover_program_suggestions's existing_urls dedup
    doesn't re-suggest the same URL on the next discovery tick."""
    if request.method == "OPTIONS":
        return "", 204
    data = request.get_json(force=True, silent=True) or {}
    suggestion_id = data.get("id")

    with _data_lock:
        suggestions = _load_json_array(_SUGGESTIONS_PATH)
        match = next((s for s in suggestions if s.get("id") == suggestion_id), None)
        if not match:
            return jsonify({"error": "not found"}), 404
        match["status"] = "descartada"
        if not _save_json(_SUGGESTIONS_PATH, suggestions):
            return jsonify({"error": "failed to save"}), 500

    _emit_updated()
    return jsonify({"ok": True})


@app.route("/api/bughunter/automode", methods=["POST", "OPTIONS"])
def api_bughunter_automode():
    """Body: {on: bool}. Persists the toggle; the Auto Mode background
    loop that actually reads this flag isn't built yet (Phase 3)."""
    if request.method == "OPTIONS":
        return "", 204
    data = request.get_json(force=True, silent=True) or {}
    on = bool(data.get("on"))

    with _data_lock:
        state = _load_state()
        state["auto_mode"] = on
        _log_activity(state, "Modo automático activado" if on else "Modo automático desactivado")
        if not _save_json(_STATE_PATH, state):
            return jsonify({"error": "failed to save"}), 500

    _emit_updated()
    return jsonify({"ok": True, "auto_mode": on})


def _emit_scan_log(message: str) -> None:
    try:
        import core.server as server_mod
        server_mod.socketio.emit("bughunter_scan_log", {"message": message})
    except Exception:
        pass


def _announce_critical_findings(target_name: str, findings: list) -> None:
    """Bypasses the normal proactive should_intervene gate entirely — same
    two-tier design as core.reminders._deliver_time_reminders() (gated only
    by background_loops._proactive_blocked(), i.e. "don't interrupt active
    speech/dispatch", nothing else — no rate cap, no session-idle check).
    Reserved for severity='critica' ONLY, per Joan's explicit split
    (2026-08-18): critical findings notify no matter what he's doing;
    everything else is just a soft "by the way" candidate folded into
    core.background_loops._gather_proactivity_context() for should_intervene
    to weigh naturally — same treatment ESTUDIO investigations already get.
    Never raises — a failed announcement must never break scan completion."""
    try:
        from core import background_loops
        if background_loops._proactive_blocked():
            return
        from core import personality as personality_mod
        with personality_mod._personality_lock:
            current_p = personality_mod._personality
        titles = "; ".join(f["title"] for f in findings[:3])
        # Phrased naturally (see feedback_no_hardcoded_replies memory) —
        # this used to be a fixed f-string spoken verbatim every time.
        from core import response as response_mod
        message = response_mod._format_response(
            f"Encontré algo crítico escaneando {target_name}: {titles}. Deberías revisarlo en Bug Hunter en cuanto puedas.",
            personality=current_p,
        )
        background_loops._speak_unprompted(current_p, message)
    except Exception:
        logger.warning("Failed to announce critical Bug Hunter finding (non-critical)", exc_info=True)


_FINDING_HOST_PREFIX_RE = re.compile(r'^\[([^\]]+)\]')


def _finding_host(title: str, primary_host: str) -> str:
    """Subdomain findings get their title prefixed '[sub.example.com] ...'
    by core.bughunter_scan._run_subdomain_check_suite — extract that, or
    fall back to the target's own primary host for everything else."""
    m = _FINDING_HOST_PREFIX_RE.match(title or "")
    return m.group(1) if m else primary_host


def _run_scan_thread(target: dict) -> None:
    """Runs on a background thread, started by api_bughunter_scan below.
    Holds _scan_lock for its whole lifetime — released in `finally` no
    matter how it exits, so a crashed scan can never wedge the app into
    'a scan is always running'."""
    name = target.get("name", target.get("domain", "?"))
    primary_host = (target.get("domain") or "").strip().lstrip("*.").split("/")[0]
    try:
        findings, subdomains, checked_hosts = bughunter_scan.run_scan(target, on_progress=_emit_scan_log)
        this_scan_titles = {f["title"] for f in findings}

        with _data_lock:
            existing = _load_json_array(_FINDINGS_PATH)

            # Dedup by (target, title) — a repeat scan shouldn't pile up
            # the same "CSP header missing" finding every time it runs.
            # BUT a title that matches an existing "resuelto" entry means
            # the issue regressed (e.g. HSTS got removed again after being
            # fixed) — reopen that entry in place rather than silently
            # dropping the detection, otherwise "resuelto" would be a
            # one-way door and a real regression could never resurface.
            existing_by_key = {(f.get("target"), f.get("title")): f for f in existing}
            new_findings = []
            reopened_findings = []
            for f in findings:
                key = (f["target"], f["title"])
                match = existing_by_key.get(key)
                if match is None:
                    new_findings.append(f)
                    existing_by_key[key] = f  # covers a duplicate title within this same scan's findings
                elif match.get("status") == "resuelto":
                    match["status"] = "nuevo"
                    match["reappeared_at"] = _now_iso()
                    match.pop("resolved_at", None)
                    reopened_findings.append(match)
                # else: already tracked under nuevo/borrador/enviado/duplicado — leave as-is
            reopened_count = len(reopened_findings)
            if new_findings:
                existing.extend(new_findings)

            # Auto-resolve: an existing open finding for this target, on a
            # host actually re-checked this run (checked_hosts — NOT just
            # "any subdomain crt.sh ever returned"), tagged auto_resolvable
            # by the check that produced it (see core.bughunter_scan's
            # "no_auto_resolve" — third-party search/discovery-sourced
            # findings never qualify), that simply didn't reappear this
            # scan → the underlying issue is gone. Never touches "enviado"/
            # "duplicado" (Joan's own terminal states) or anything already
            # "resuelto". Missing the auto_resolvable field entirely
            # (findings saved before this feature existed) defaults to NOT
            # eligible — safer to leave old data alone than guess.
            resolved_count = 0
            for f in existing:
                if f.get("target") != name or f.get("status") not in ("nuevo", "borrador"):
                    continue
                if not f.get("auto_resolvable", False):
                    continue
                if _finding_host(f.get("title", ""), primary_host) not in checked_hosts:
                    continue
                if f.get("title") in this_scan_titles:
                    continue  # still detected this run
                f["status"] = "resuelto"
                f["resolved_at"] = _now_iso()
                resolved_count += 1

            if new_findings or resolved_count or reopened_count:
                _save_json(_FINDINGS_PATH, existing)

        # A reopened critical finding is just as worth Joan's immediate
        # attention as a brand-new one — a regressed critical issue isn't
        # less urgent for having been fixed once already.
        critical_new = [f for f in (new_findings + reopened_findings) if f.get("severity") == "critica"]
        if critical_new:
            _announce_critical_findings(name, critical_new)

        skipped = len(findings) - len(new_findings) - reopened_count
        summary = f"Escaneo completado: {name} — {len(new_findings)} hallazgo(s) nuevo(s)"
        if skipped:
            summary += f" ({skipped} ya existente(s), omitido(s))"
        if reopened_count:
            summary += f" — {reopened_count} hallazgo(s) reabierto(s) tras reaparecer"
        if resolved_count:
            summary += f" — {resolved_count} hallazgo(s) previo(s) marcado(s) como resuelto(s)"

        with _data_lock:
            state = _load_state()
            state["current_activity"] = None
            state["last_run"] = {"target": name, "when": _now_iso(), "findings_count": len(new_findings)}
            _log_activity(state, summary)
            if subdomains:
                _log_activity(state, f"{len(subdomains)} subdominio(s) de {name} encontrados vía crt.sh — revisar si añadirlos a Scope")
            _save_json(_STATE_PATH, state)

        _emit_scan_log(summary)
        _emit_updated()
    except Exception as exc:
        logger.error("Bug Hunter scan failed for %s: %s", name, exc)
        with _data_lock:
            state = _load_state()
            state["current_activity"] = None
            _log_activity(state, f"Escaneo de {name} falló: {exc}")
            _save_json(_STATE_PATH, state)
        _emit_scan_log(f"El escaneo falló: {exc}")
        _emit_updated()
    finally:
        _scan_lock.release()


def start_scan(target: dict) -> tuple[bool, str]:
    """Shared entry point for kicking off a scan — used by
    api_bughunter_scan below (manual, from the Scan tab) AND by
    core.background_loops's Auto Mode tick (Phase 3). Non-blocking: tries
    to acquire _scan_lock, and if that succeeds, updates state + starts the
    background thread and returns immediately. Returns (ok, message) —
    ok=False (lock already held) means the caller should NOT treat this as
    a scan having started."""
    if not _scan_lock.acquire(blocking=False):
        return False, "Ya hay un escaneo en curso — espera a que termine."

    with _data_lock:
        state = _load_state()
        state["current_activity"] = f"Escaneando {target['name']}"
        _log_activity(state, f"Escaneo iniciado: {target['name']}")
        _save_json(_STATE_PATH, state)
    _emit_updated()

    threading.Thread(target=_run_scan_thread, args=(target,), daemon=True).start()
    return True, f"Escaneo iniciado para {target['name']}."


@app.route("/api/bughunter/scan", methods=["POST", "OPTIONS"])
def api_bughunter_scan():
    """Body: {target_id}. Runs the real scan engine (core/bughunter_scan.py
    — passive/read-only checks only, see that module's docstring for the
    hard rule) on a background thread. Returns immediately; progress
    streams via the 'bughunter_scan_log' socket event, final result via
    'bughunter_updated' (same as every other mutation here)."""
    if request.method == "OPTIONS":
        return "", 204
    data = request.get_json(force=True, silent=True) or {}
    target_id = data.get("target_id")

    scope = _load_json_array(_SCOPE_PATH)
    target = next((t for t in scope if t.get("id") == target_id), None)
    if not target:
        return jsonify({"error": "target not in Scope"}), 404

    ok, message = start_scan(target)
    if not ok:
        return jsonify({"error": "scan already running", "message": message}), 409
    return jsonify({"ok": True, "message": message})
