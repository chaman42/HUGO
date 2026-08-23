"""Flask routes: GET /api/notifications (unread pending-notification queue)
and POST /api/notifications/<id>/read (mark one as shown) — see
core/notifications.py. Backs ui/js/notifications.js's startup check; voice
delivery of the same queue is a separate path (see
core.notifications._deliver_pending_notifications, wired into
core/commands.py's dispatch_command())."""
import logging

from flask import jsonify

from core.server import app
from core import notifications

logger = logging.getLogger(__name__)


@app.route("/api/notifications")
def api_notifications():
    try:
        return jsonify({"unread": notifications.get_unread_notifications()})
    except Exception as exc:
        logger.error("Failed to load notifications: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/notifications/<notification_id>/read", methods=["POST"])
def api_notifications_read(notification_id):
    try:
        found = notifications.mark_notification_read(notification_id)
        return jsonify({"ok": found})
    except Exception as exc:
        logger.error("Failed to mark notification read: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500
