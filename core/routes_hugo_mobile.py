# ═══════════════════════════════════════════════════════════════════════════
# HUGO MOBILE — hosts the AltStore update source (source.json) and the .ipa
# it points to, straight off disk from ~/Developer/HUGO-Mobile/releases/
# (produced by that project's scripts/release.sh). This is what lets the
# phone app self-update: AltStore periodically polls source.json, compares
# its `version` against what's installed, and offers an "Update" button —
# no manual re-sideload after every change.
#
# Read-only, no auth (same trust boundary as the rest of this LAN-only
# server) — just static file serving under one URL prefix.
# ═══════════════════════════════════════════════════════════════════════════
import logging
import os

from flask import send_from_directory, abort

from core.server import app

logger = logging.getLogger(__name__)

_RELEASES_DIR = os.path.expanduser("~/Developer/HUGO-Mobile/releases")


@app.route("/hugo-mobile/source.json")
def hugo_mobile_source():
    if not os.path.isfile(os.path.join(_RELEASES_DIR, "source.json")):
        abort(404)
    return send_from_directory(_RELEASES_DIR, "source.json", mimetype="application/json")


@app.route("/hugo-mobile/releases/<path:filename>")
def hugo_mobile_release_file(filename: str):
    # send_from_directory already rejects path traversal outside _RELEASES_DIR
    if not os.path.isfile(os.path.join(_RELEASES_DIR, filename)):
        abort(404)
    return send_from_directory(_RELEASES_DIR, filename)
