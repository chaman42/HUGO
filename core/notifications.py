# ═══════════════════════════════════════════════════════════════════════════
# NOTIFICATIONS — pending-notification queue (data/notifications.json).
# Written by core.sleep_phases_incubation when an investigation reaches
# 'lista_para_revision' or 'completada'; read two ways:
#   - GET /api/notifications (core/routes_notifications.py) → ui/js's
#     startup check, shown subtly in the chat log (see ui/js/notifications.js).
#   - _deliver_pending_notifications(), called at the top of every
#     dispatch_command() (core/commands.py) so LIRA mentions it naturally
#     the next time Joan talks to her — same "next real interaction" timing
#     as core.reminders._deliver_session_reminders, which this mirrors.
# Whichever channel reaches an unread notification first marks it read —
# there's only one 'read' flag per spec, so a notification is delivered
# exactly once, by whichever of voice/UI sees it first.
# ═══════════════════════════════════════════════════════════════════════════
import datetime
import json
import os
import threading
import uuid

NOTIFICATIONS_PATH = "data/notifications.json"

_notifications_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _load_notifications() -> list[dict]:
    try:
        with open(NOTIFICATIONS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _save_notifications(notifications: list[dict]) -> None:
    os.makedirs(os.path.dirname(NOTIFICATIONS_PATH) or ".", exist_ok=True)
    with open(NOTIFICATIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(notifications, f, ensure_ascii=False, indent=2)


def create_notification(type_: str, title: str, message: str, data: dict | None = None) -> dict:
    """`data` is optional and backward-compatible — every pre-existing
    call site omits it and gets the same note shape as before. Only
    consumed today by _deliver_pending_notifications() below, for
    'code_engine_install_approval' notifications (see
    core.code_engine.tools.dependency_manager._request_install_approval)
    that need to carry structured state (which package, which task)
    through to the moment they're actually spoken, not just a string."""
    with _notifications_lock:
        notifications = _load_notifications()
        note = {
            "id":         uuid.uuid4().hex[:12],
            "type":       type_,
            "title":      title,
            "message":    message,
            "created_at": _now_iso(),
            "read":       False,
        }
        if data:
            note["data"] = data
        notifications.append(note)
        _save_notifications(notifications)
    return note


def get_unread_notifications() -> list[dict]:
    """Read-only — backs GET /api/notifications. Does NOT mark anything
    read; the caller (ui/js/notifications.js) does that explicitly per
    notification once it's actually been shown, so a fetch that succeeds
    but fails to render never silently loses the notification."""
    with _notifications_lock:
        notifications = _load_notifications()
    return [n for n in notifications if isinstance(n, dict) and not n.get("read")]


def mark_notification_read(notification_id: str) -> bool:
    with _notifications_lock:
        notifications = _load_notifications()
        for n in notifications:
            if isinstance(n, dict) and n.get("id") == notification_id:
                n["read"] = True
                _save_notifications(notifications)
                return True
    return False


def _deliver_pending_notifications(personality: str) -> None:
    """Called at the top of every dispatch_command() call (see
    core/commands.py), same 'next real interaction' timing as
    core.reminders._deliver_session_reminders — mentions at most one
    unread notification per call (oldest first) so a backlog reads as one
    aside, not a monologue; any others surface on subsequent turns."""
    with _notifications_lock:
        notifications = _load_notifications()
        pending = [n for n in notifications if isinstance(n, dict) and not n.get("read")]
        if not pending:
            return
        due = pending[0]
        due["read"] = True
        _save_notifications(notifications)

    # Install-approval notifications carry a `data` payload and need Joan's
    # very next reply captured as a yes/no/'always' answer to THIS specific
    # request — same Level-3 propose/confirm slot core.actions already uses
    # for calendar events, reminders, app-open, etc. (see core.intent's own
    # docstring on _pending_action). Set it right here, at delivery time,
    # not when the notification was first created — Joan might not talk to
    # LIRA again for hours, and the proposal should only "exist" from the
    # moment she actually hears the question.
    message = due["message"]
    already_phrased = False
    if due.get("type") == "code_engine_install_approval" and due.get("data"):
        import time as _time
        import core.intent as intent_mod
        intent_mod._pending_action = {
            "kind": "install_package_approval",
            "data": due["data"],
            "at": _time.monotonic(),
        }
        phrased = _phrase_install_approval(due["data"], personality)
        if phrased:
            message = phrased
            already_phrased = True

    # Phrased naturally (see feedback_no_hardcoded_replies memory) — most
    # notification 'message' values are raw facts set at creation time
    # (core.notifications.create_notification's many call sites), not
    # already-natural prose. Skipped for the install-approval case above,
    # which already made its own natural-phrasing call — re-phrasing that
    # again risks subtly drifting the exact proposal wording Joan's next
    # yes/no reply is captured against.
    if not already_phrased:
        from core import response as response_mod
        message = response_mod._format_response(message, personality=personality)

    from core import background_loops
    background_loops._speak_unprompted(personality, message)


def _phrase_install_approval(data: dict, personality: str) -> str | None:
    """Turns the raw PyPI facts DependencyManager._request_install_approval()
    attached (data['research']) into a natural, personality-voiced
    question, via the same 'phrase this raw result' helper every other
    tool result already goes through (core.response._format_response —
    Groq-based, not the slow local Ollama code model, so this stays fast).
    None on any failure — the caller falls back to the plain deterministic
    message the notification was created with, never silence."""
    research = data.get("research") or {}
    name = data.get("name", "")
    if research.get("found"):
        raw = (
            f"Quiero instalar el paquete de Python '{name}', que no está en mi lista de confianza. "
            f"Datos de PyPI — descripción: {research.get('summary') or 'sin descripción'}; "
            f"autor: {research.get('author') or 'desconocido'}; "
            f"versión actual: {research.get('latest_version') or 'desconocida'}; "
            f"primera publicación: {(research.get('first_release') or '')[:10] or 'desconocida'}; "
            f"{research.get('release_count', 0)} versiones publicadas en total. "
            f"Pregúntame si lo instalo, y explica que puedo responder sí, no, o sí siempre (para confiar en él "
            f"automáticamente la próxima vez)."
        )
    else:
        raw = (
            f"Quiero instalar el paquete de Python '{name}', que no está en mi lista de confianza, "
            f"y no pude verificarlo en PyPI (sin conexión o no encontrado). "
            f"Pregúntame si lo instalo de todos modos, y explica que puedo responder sí, no, o sí siempre."
        )
    try:
        from core.response import _format_response
        return _format_response(raw, personality=personality)
    except Exception:
        return None
