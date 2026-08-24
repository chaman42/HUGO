"""The two long-running launcher routes that stream a shell script's output
while rebuilding/reinstalling the app: /api/update (Electron shell rebuild)
and /api/build_ios (iOS .ipa build). Split out from api_routes.py since both
are large, self-contained, and share nothing with the shorter routes there.
"""
import os
import subprocess
import threading
from functools import wraps

from flask import jsonify, request

from core.launcher_app import _BASE_DIR, app, socketio, logger
from core import process_manager as pm


def _joan_only(view):
    """Real incident (2026-08-24, found simulating Dani's first launch):
    neither of this file's routes had any authorization check — the
    "ACTUALIZAR SISTEMA"/"COMPILAR PARA IPHONE" Ajustes buttons are visible
    to anyone, Dani included, and nothing server-side stopped a non-Joan
    caller from POSTing here directly, e.g. triggering a full rebuild +
    git push (see api_update()'s own docstring) or an iOS build. Same
    permissive-default check as core.routes_social._current_person_is_joan/
    _joan_only — duplicated rather than imported since this module lives on
    the launcher's Flask app (core.launcher_app), a different process from
    core.server/core.routes_social entirely; see
    core.sleep_curiosity_search._admin_device_active for the same
    duplicate-rather-than-import precedent."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        try:
            from core import social
            present = social.social_engine.who_is_present()
            current = present[0] if present else None
            is_joan = current is None or current.id == "joan"
        except Exception:
            is_joan = True
        if not is_joan:
            return jsonify({"error": "not authorized"}), 403
        return view(*args, **kwargs)
    return wrapper


def emit_update_progress(stage: str, label: str) -> None:
    """Broadcast an update-rebuild milestone to the "Actualizar Sistema"
    button (see _applyUpdateProgress() in ui/index.html). See api_update()
    for where each stage actually fires, driven by streaming
    scripts/rebuild_app.sh's own real output rather than a fixed timer."""
    try:
        socketio.emit("update_progress", {"stage": stage, "label": label})
    except Exception:
        pass


def emit_build_ios_progress(stage: str, label: str) -> None:
    """Broadcast an iOS-build milestone to the "Compilar para iPhone" button
    (see _applyBuildIosProgress() in ui/index.html). See api_build_ios() for
    where each stage actually fires, driven by streaming
    scripts/build_ios.sh's own real output — same pattern as
    emit_update_progress()/api_update() above."""
    try:
        socketio.emit("build_ios_progress", {"stage": stage, "label": label})
    except Exception:
        pass


@app.route("/api/update", methods=["POST"])
@_joan_only
def api_update():
    """Run scripts/rebuild_app.sh synchronously — git pull, bump+push a
    Service Worker cache-bust commit, rebuild, reinstall
    /Applications/HUGO.app — and report whether it actually completed.

    Backs the "Actualizar HUGO" button in the System Info panel. Sets
    HUGO_FORCE_UPDATE=1 so rebuild_app.sh's "skip if HUGO is running" guard
    (meant for the unattended 6-hourly LaunchAgent) doesn't apply here — this
    call is always made from inside the very session being updated, which is
    exactly the explicit, user-initiated case that guard is meant to exempt.
    The same flag also means the script's commit-hash staleness check never
    skips this call, so every button press bumps ui/sw.js's cache key and
    rebuilds, guaranteeing users get the latest frontend (see the "Cache
    bust" step in rebuild_app.sh).

    Accepts an optional JSON body {"skip_claude_guard": true} — set only
    when the user has re-confirmed through the frontend's second "a Claude
    Code session is active, force anyway?" dialog (see ui/app.js) after a
    first attempt was skipped by rebuild_app.sh's Claude Code guard. Passed
    through as HUGO_SKIP_CLAUDE_GUARD=1, the one thing that lets that guard
    proceed instead of no-op'ing — see its comment in rebuild_app.sh for
    the data-loss risk this is knowingly overriding.

    On success, sets _pending_relaunch so electron/main.js's health poll
    picks it up and relaunches the app automatically (see that flag's
    definition above for why this is a poll rather than a direct signal).

    Streams scripts/rebuild_app.sh's own real output line-by-line (rather
    than subprocess.run()'s capture_output=True, which only returns
    everything at once when the whole 600s-capable call finishes) so
    real progress can be emitted via emit_update_progress() as the script
    actually moves through each phase — 'Descargando cambios...' (git
    pull/stash/cache-bust), 'Compilando...' (npm install/build),
    'Instalando...' (copying into /Applications), 'Reiniciando...' (done,
    about to relaunch) — matching the button's own stages in
    ui/index.html's _applyUpdateProgress(). Success/failure detection and
    error-message extraction preserve the exact same semantics as the old
    capture_output=True version — just fed by a streamed buffer instead of
    a single post-hoc string.
    """
    script = os.path.join(_BASE_DIR, "scripts", "rebuild_app.sh")
    emit_update_progress("downloading", "Descargando cambios...")

    body = request.get_json(silent=True) or {}
    skip_claude_guard = bool(body.get("skip_claude_guard"))

    TIMEOUT_S = 600
    lines: list[str] = []
    stage = "downloading"

    env = {**os.environ, "HUGO_FORCE_UPDATE": "1"}
    if skip_claude_guard:
        env["HUGO_SKIP_CLAUDE_GUARD"] = "1"

    try:
        proc = subprocess.Popen(
            ["/bin/bash", script],
            cwd=_BASE_DIR,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            env=env,
        )
    except Exception as exc:
        logger.error("Manual update failed to launch: %s", exc)
        return jsonify({"ok": False, "error": str(exc)})

    # A real deadline, not just a check-between-lines: `for line in proc.stdout`
    # blocks waiting for the NEXT line, so if the script ever hangs producing
    # zero output (e.g. a stuck network call), a check placed only inside the
    # loop body would never run. This timer fires independently and kills the
    # process regardless of output activity — matching the timeout guarantee
    # subprocess.run(timeout=...) used to give before this became streaming.
    timed_out = threading.Event()
    watchdog = threading.Timer(TIMEOUT_S, lambda: (timed_out.set(), proc.kill()))
    watchdog.daemon = True
    watchdog.start()

    try:
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n")
            lines.append(line)

            if stage == "downloading" and ("Running npm install" in line or "Running npm run build" in line):
                stage = "compiling"
                emit_update_progress(stage, "Compilando...")
            elif stage == "compiling" and "Installing to" in line:
                stage = "installing"
                emit_update_progress(stage, "Instalando...")
            elif stage == "installing" and "HUGO actualizada correctamente" in line:
                stage = "restarting"
                emit_update_progress(stage, "Reiniciando...")

        returncode = proc.wait(timeout=10)
    except Exception as exc:
        proc.kill()
        if timed_out.is_set():
            logger.error("Manual update timed out after %ds", TIMEOUT_S)
            return jsonify({"ok": False, "error": "timeout"})
        logger.error("Manual update failed while streaming output: %s", exc)
        return jsonify({"ok": False, "error": str(exc)})
    finally:
        watchdog.cancel()

    if timed_out.is_set():
        logger.error("Manual update timed out after %ds", TIMEOUT_S)
        return jsonify({"ok": False, "error": "timeout"})

    output = "\n".join(lines)
    logger.info("Manual update via /api/update — exit=%d", returncode)
    if output:
        logger.info("rebuild_app.sh output:\n%s", output)

    if returncode == 0 and "HUGO actualizada correctamente" in output:
        if stage != "restarting":
            emit_update_progress("restarting", "Reiniciando...")
        pm._pending_relaunch = True
        logger.info("Update complete — flagged for auto-relaunch.")
        return jsonify({"ok": True})

    non_empty = [l for l in lines if l.strip()]
    return jsonify({
        "ok": False,
        "error": non_empty[-1] if non_empty else "unknown error",
    })


@app.route("/api/build_ios", methods=["POST"])
@_joan_only
def api_build_ios():
    """Run scripts/build_ios.sh synchronously — cap sync -> xcodebuild
    archive -> xcodebuild -exportArchive — and report the resulting .ipa
    path. Backs the "Compilar para iPhone" button in Ajustes.

    Same streaming-subprocess pattern as api_update() above: real progress
    is emitted via emit_build_ios_progress() as the script's own output
    crosses each phase boundary ('Running npx cap sync ios' -> Sincronizando,
    'Archiving' -> Compilando, 'ready at' -> done with the .ipa path), not a
    fixed timer. A full Xcode install (not just Command Line Tools) is
    required — build_ios.sh's own preflight check fails fast with a clear
    message if that's missing, which surfaces here as data.error same as
    any other failure.

    Longer timeout than api_update()'s 600s: a first-time SPM package
    resolution + archive build genuinely can take longer than an Electron
    rebuild.
    """
    script = os.path.join(_BASE_DIR, "scripts", "build_ios.sh")
    emit_build_ios_progress("syncing", "Sincronizando...")

    TIMEOUT_S = 1200
    lines: list[str] = []
    stage = "syncing"
    ipa_path = ""

    try:
        proc = subprocess.Popen(
            ["/bin/bash", script],
            cwd=_BASE_DIR,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except Exception as exc:
        logger.error("iOS build failed to launch: %s", exc)
        return jsonify({"ok": False, "error": str(exc)})

    timed_out = threading.Event()
    watchdog = threading.Timer(TIMEOUT_S, lambda: (timed_out.set(), proc.kill()))
    watchdog.daemon = True
    watchdog.start()

    try:
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n")
            lines.append(line)

            if stage == "syncing" and "Archiving" in line:
                stage = "compiling"
                emit_build_ios_progress(stage, "Compilando...")
            elif line.startswith("/") and line.endswith(".ipa"):
                # build_ios.sh's final `echo "$IPA_OUT"` — the one line in
                # its output that's a bare path, not a "[timestamp] ..." log
                # line, so this match is unambiguous.
                ipa_path = line

        returncode = proc.wait(timeout=10)
    except Exception as exc:
        proc.kill()
        if timed_out.is_set():
            logger.error("iOS build timed out after %ds", TIMEOUT_S)
            return jsonify({"ok": False, "error": "timeout"})
        logger.error("iOS build failed while streaming output: %s", exc)
        return jsonify({"ok": False, "error": str(exc)})
    finally:
        watchdog.cancel()

    if timed_out.is_set():
        logger.error("iOS build timed out after %ds", TIMEOUT_S)
        return jsonify({"ok": False, "error": "timeout"})

    output = "\n".join(lines)
    logger.info("iOS build via /api/build_ios — exit=%d", returncode)
    if output:
        logger.info("build_ios.sh output:\n%s", output)

    if returncode == 0 and ipa_path:
        emit_build_ios_progress("done", "IPA lista")
        return jsonify({"ok": True, "ipa_path": ipa_path})

    non_empty = [l for l in lines if l.strip()]
    return jsonify({
        "ok": False,
        "error": non_empty[-1] if non_empty else "unknown error",
    })
