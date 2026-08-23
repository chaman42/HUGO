// notifications.js — pending-notification queue (data/notifications.json,
// core/notifications.py). Checked once per connect (see connection.js's
// jarvisSocket 'connect' handler) so a completed/ready investigation shows
// up the moment the app is open, shown subtly as a system line in the chat
// log — same treatment as the existing 'Jarvis online' system message.
// core.notifications._deliver_pending_notifications covers the voice path
// (LIRA mentions it naturally next time Joan talks to her) independently —
// whichever of the two reaches a given notification first marks it read,
// so it's never delivered twice.
async function _checkNotifications() {
  try {
    const res  = await fetch(`${JARVIS_API}/api/notifications`)
    const data = await res.json()
    const unread = data.unread || []
    for (const n of unread) {
      addMessage('system', n.message)
      fetch(`${JARVIS_API}/api/notifications/${n.id}/read`, { method: 'POST' }).catch(() => {})
    }
  } catch { /* best-effort — a missed notification just waits for the next connect or voice delivery */ }
}
