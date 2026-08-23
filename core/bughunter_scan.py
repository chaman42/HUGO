"""core/bughunter_scan.py — BUG HUNTER scan engine (Phase 2, expanded
2026-08-18 with a second and third wave of checks — see the checklist
below). Third wave was inspired by a quick look at what 2026 bug-bounty
methodology writeups consistently flag as the highest-signal, most-missed
categories: leaked credentials in public code/JS, forgotten historically-
indexed paths, and undocumented API endpoints — see the checklist below
for exactly how each was kept within the passive/non-destructive rule.

Passive/read-only checks ONLY, per the hard non-destructive constraint (see
the "Bug Hunter Constraints" memory). Every check here observes what a
normal HTTPS client already sees on a plain GET (plus a custom Origin
header for the CORS check, and reading — never following — a redirect's
Location header for the open-redirect check) — response headers, TLS
metadata, a handful of well-known paths, public certificate-transparency
logs. Nothing here sends a payload, brute-forces anything, or writes to
the target in any way. If this list ever grows to include anything more
active, that's a deliberate, separately-reviewed decision — not something
to slip in here quietly.

Current checks: security headers (CSP presence AND quality — unsafe-inline/
unsafe-eval/wildcard sources, not just presence/absence — HSTS/
X-Frame-Options/Permissions-Policy/COOP/CORP/cookie flags including
__Host-/__Secure- prefix hygiene/version disclosure), mixed-content
detection (HTTPS page loading http:// subresources — free, reused from the
already-fetched body), TLS protocol+cert expiry, security.txt presence,
sensitive file/dir exposure (a short fixed path list, not a wordlist),
verbose error/debug disclosure (free — reused from an already-fetched
body), reflected-origin CORS misconfiguration, open redirect (a handful of
common param names), crt.sh subdomain discovery, subdomain-takeover
fingerprint matching against a small sample of what crt.sh found,
hardcoded-secret/source-map/API-endpoint exposure in linked JavaScript (a
fixed list of high-confidence vendor key-format patterns, not a generic
"long string near the word key" heuristic — see _SECRET_PATTERNS; endpoint
paths are informational only, surfaced via on_progress, never findings on
their own), SameSite folded into the existing cookie-flags check, SPF/DMARC
email-spoofing posture (plain DNS TXT lookups via the system `dig` binary
— never touches the target's web server itself), possible credential
exposure in public GitHub content (dork search via core.tools_search, same
"reuse web search, never touch the target" approach as program discovery
below), historically-indexed sensitive-looking paths from the Wayback
Machine's CDX API re-checked live (a path only becomes a finding if it's
still actually reachable today — see _check_wayback_paths), and the same
live-recheck treatment applied to interesting-looking paths the site
itself references in robots.txt's Disallow lines or sitemap.xml (see
_check_robots_sitemap_paths — robots.txt in particular is the site owner's
own curated "don't look here" list), and known-vulnerable-version
fingerprinting cross-referenced against NVD's public CVE database (a
short curated product list — WordPress/Drupal/jQuery/Apache/nginx/PHP —
matched only on unambiguous version signals, deliberately biased toward
false negatives over false positives per Joan's explicit instruction
2026-08-18: see the block comment above _check_known_vulnerable_versions
for every gate that enforces this), plain-HTTP-without-redirect detection
(_check_https_enforcement, one extra GET to the http:// scheme, run
against both the primary domain and every subdomain below), and — as of
this wave — the full core check suite (not just takeover fingerprinting)
run against a bounded sample of discovered subdomains too, not just the
primary domain's root URL (see _run_subdomain_check_suite and
_MAX_SUBDOMAINS_FULL_SCAN; deliberately excludes domain-level/third-party-
API-heavy checks like SPF/DMARC, GitHub, and Wayback/robots/sitemap —
those stay primary-domain-only). NVD CVE lookups are cached per
(product, version) for _NVD_CACHE_TTL, since Auto Mode's ~10-min re-scan
cadence plus the same product turning up on multiple subdomains would
otherwise mean repeated, wasted queries against NVD's unauthenticated
rate limit. The first wave (headers/TLS/security.txt/sensitive paths)
tends to produce low-severity findings many bounty programs explicitly
exclude — everything after that targets the kind of substantive,
commonly-accepted findings that are actually worth submitting. The
JS-secrets, GitHub, and Wayback checks in particular are the category
most invisible to anyone not specifically going looking for it — none of
it shows up rendering the page or browsing the site normally.

run_scan(target, on_progress) is the entry point core/bughunter_routes.py
calls on a background thread. It returns (findings, subdomains,
checked_hosts):
  - findings: list of dicts ready to append to data/bughunter_findings.json
    (id/target/title/severity/status/summary/description/repro_steps/
    impact/fix_suggestion/discovered_at/auto_resolvable all filled in —
    auto_resolvable is False for anything sourced from a third-party
    search/discovery step whose absence next scan isn't reliable evidence
    the issue is gone — see "no_auto_resolve" tagging in _check_github_
    exposure/_check_wayback_paths/_check_robots_sitemap_paths)
  - subdomains: list of hostnames discovered via crt.sh (informational —
    the caller decides whether/how to surface them, they are NOT
    auto-added to Scope)
  - checked_hosts: [primary_host] plus whichever subdomains actually got
    the full check suite this run (a bounded sample of `subdomains`, see
    _MAX_SUBDOMAINS_FULL_SCAN) — the caller uses this to scope its
    auto-resolve step to hosts genuinely re-verified this run

Each mechanical detection (a dict with title/severity/evidence/fix_hint) is
expanded into report prose via local Ollama when available
(_draft_finding_text), falling back to a plain template built straight
from the detection fields if Ollama is down — a finding is never dropped
for lack of a working LLM.
"""
import json
import logging
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

logger = logging.getLogger(__name__)

FETCH_TIMEOUT = 8
TLS_TIMEOUT   = 6

OLLAMA_HOST         = "http://localhost:11434"
OLLAMA_MODEL        = "llama3.2:3b"
OLLAMA_GENERATE_URL = f"{OLLAMA_HOST}/api/generate"

# Identifies LIRA honestly to whatever server answers — never masquerades
# as a browser. Points back at the allowlist file so anyone reading access
# logs can see exactly why this traffic exists.
_USER_AGENT = "LIRA-BugHunter/0.1 (passive recon only; authorized scope in data/bughunter_scope.json)"

# Short, fixed list — deliberately NOT a brute-force wordlist. Each path is
# either a classic accidental-exposure path (backups) or a dotfile/config
# file that should never be served.
_SENSITIVE_PATHS = [
    "/backup/", "/backups/", "/.git/config", "/.git/HEAD", "/.svn/entries",
    "/.env", "/.env.local", "/.htpasswd", "/uploads/", "/.DS_Store",
    "/docker-compose.yml", "/wp-config.php.bak", "/config.php.bak",
]

# Query-param names commonly used for post-login/post-action redirects —
# the classic open-redirect surface. Short and specific, not a wordlist.
_REDIRECT_PARAMS = ["next", "url", "redirect", "redirect_uri", "return", "return_to", "continue", "dest"]
_REDIRECT_TEST_TARGET = "https://lira-bughunter-redirect-test.invalid/"

# Response-body fingerprints for common "this subdomain points at a
# de-provisioned cloud resource" pages — the standard passive signal for a
# subdomain-takeover candidate. Matching one of these is suggestive, not
# certain — see _check_subdomain_takeover's evidence wording.
_TAKEOVER_FINGERPRINTS = [
    ("GitHub Pages", "there isn't a github pages site here"),
    ("Amazon S3", "nosuchbucket"),
    ("Heroku", "no such app"),
    ("Shopify", "sorry, this shop is currently unavailable"),
    ("Bitbucket", "repository not found"),
    ("Fastly", "fastly error: unknown domain"),
    ("Unbounce", "the requested url was not found on this server"),
    ("Pantheon", "404 error: unknown site"),
    ("Azure", "web app not found"),
    ("Surge.sh", "project not found"),
]

# Debug/error-page signatures worth flagging if they show up in a response
# we already fetched for another reason — no extra requests needed. Plain
# lowercase substrings, matched against the already-lowercased body.
_ERROR_DISCLOSURE_SIGNATURES = [
    "traceback (most recent call last)", "whitelabel error page",
    "django version", "stack trace", "at system.", "fatal error:",
    "warning: mysql_",
]

# Short, fixed, high-confidence secret patterns — deliberately NOT a
# generic "any long quoted string near the word 'key'" heuristic (that
# would flood Findings with minified-JS false positives). Each pattern is
# a real vendor key-format prefix/shape, the same category of thing bounty
# programs consistently pay out for when found hardcoded in shipped JS.
_SECRET_PATTERNS = [
    ("AWS Access Key ID",       re.compile(r'\bAKIA[0-9A-Z]{16}\b')),
    ("Stripe Live Secret Key",  re.compile(r'\bsk_live_[0-9a-zA-Z]{24,}\b')),
    ("Google API Key",          re.compile(r'\bAIza[0-9A-Za-z\-_]{35}\b')),
    ("Slack Token",             re.compile(r'\bxox[baprs]-[0-9A-Za-z-]{10,}\b')),
    ("JSON Web Token",          re.compile(r'\bey[A-Za-z0-9_-]{10,}\.ey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b')),
    ("Private Key Block",       re.compile(r'-----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----')),
]

# Matches a <script src="..."> tag's URL — deliberately simple (no full
# HTML parser dependency for one attribute extraction).
_SCRIPT_SRC_RE = re.compile(r'<script[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)

_MAX_JS_FILES_SCANNED = 8

# Quoted, path-like strings that look like an API endpoint hardcoded into
# a JS bundle — e.g. "/api/v2/users", '/graphql', "/internal/admin/stats".
# Deliberately requires a leading slash plus a recognizable API-ish segment
# so this doesn't just match every relative asset path (images, CSS) in the
# bundle — informational only (not a finding by itself, nothing here is
# actually probed), surfaced for Joan to manually explore with real auth
# context, the same "endpoint discovery" step every API-focused bounty
# methodology leads with.
_JS_ENDPOINT_RE = re.compile(
    r'["\'](/(?:api|graphql|v[0-9]+|internal|admin|rest)(?:/[A-Za-z0-9_\-./{}]*)?)["\']',
    re.IGNORECASE,
)
_MAX_ENDPOINTS_SURFACED = 20


def _ollama_available() -> bool:
    try:
        req = urllib.request.Request(f"{OLLAMA_HOST}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False


def _ollama_generate(system: str, user: str, max_tokens: int = 400) -> str | None:
    try:
        payload = json.dumps({
            "model":   OLLAMA_MODEL,
            "prompt":  user,
            "system":  system,
            "stream":  False,
            "options": {"num_predict": max_tokens},
        }).encode("utf-8")
        req = urllib.request.Request(
            OLLAMA_GENERATE_URL, data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = str(data.get("response", "")).strip()
        return text or None
    except Exception as e:
        logger.debug("Ollama call failed during Bug Hunter scan: %s", e)
        return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Lets _fetch(..., follow_redirects=False) see the FIRST hop's status/
    Location header instead of silently following it — needed for the open-
    redirect check, which has to inspect where the server tried to send us,
    not just where we'd eventually land."""
    def redirect_request(self, *args, **kwargs):
        return None


# SSL context baked into the handler itself (rather than passed per-call)
# since build_opener()'s .open() doesn't take a context= kwarg the way
# urlopen() does.
_NO_REDIRECT_OPENER = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=_SSL_CTX), _NoRedirect,
)


def _fetch(url: str, timeout: int = FETCH_TIMEOUT, extra_headers: dict | None = None, follow_redirects: bool = True):
    """GET url, return (status, headers, body_text) or (None, None, None)
    on any failure. Never raises — every check below treats a failed fetch
    as 'nothing to report', not a crash. follow_redirects=False stops at
    the first hop instead of chasing Location headers — see _NoRedirect."""
    headers = {"User-Agent": _USER_AGENT}
    if extra_headers:
        headers.update(extra_headers)
    try:
        req = urllib.request.Request(url, method="GET", headers=headers)
        if follow_redirects:
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
                body = resp.read(200_000).decode("utf-8", errors="replace")
                return resp.status, resp.headers, body
        else:
            with _NO_REDIRECT_OPENER.open(req, timeout=timeout) as resp:
                body = resp.read(200_000).decode("utf-8", errors="replace")
                return resp.status, resp.headers, body
    except urllib.error.HTTPError as e:
        try:
            body = e.read(200_000).decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return e.code, e.headers, body
    except Exception:
        return None, None, None


def _base_url(domain: str) -> str:
    domain = domain.strip().lstrip("*.")
    if domain.startswith("http://") or domain.startswith("https://"):
        return domain.rstrip("/")
    return f"https://{domain}"


# ── Checks — each returns a list of {"title", "severity", "evidence",
#    "fix_hint"} detections. severity is "critica"|"alta"|"media"|"baja",
#    matching the Findings schema. ─────────────────────────────────────────

def _check_headers_from_response(base_url: str, headers, body: str | None = None) -> list[dict]:
    """Takes an already-fetched response's headers (run_scan's reachability
    check at the top already did this GET — no need for a second one).
    body is optional and only used for the mixed-content check below —
    every other check here only needs headers."""
    detections = []
    if headers is None:
        return detections

    csp = headers.get("Content-Security-Policy") or ""
    if not csp:
        detections.append({
            "title":    "Cabecera Content-Security-Policy ausente",
            "severity": "media",
            "evidence": f"La respuesta de GET {base_url}/ no incluye la cabecera Content-Security-Policy.",
            "fix_hint": "Añadir una cabecera Content-Security-Policy razonablemente restrictiva (por ejemplo default-src 'self') y ajustarla a los recursos que la aplicación realmente carga.",
        })
    else:
        # Quality check, not just presence — a CSP with 'unsafe-inline' or
        # 'unsafe-eval' on script-src, or a bare wildcard source, defeats
        # most of what a CSP is for (it still blocks framing/some things
        # via other directives, so this doesn't replace the presence
        # check above — it's additive).
        csp_lower = csp.lower()
        weak_directives = []
        for directive in ("script-src", "default-src"):
            m = re.search(rf'{directive}\s+([^;]+)', csp_lower)
            if not m:
                continue
            value = m.group(1)
            if "unsafe-inline" in value:
                weak_directives.append(f"{directive} permite 'unsafe-inline'")
            if "unsafe-eval" in value:
                weak_directives.append(f"{directive} permite 'unsafe-eval'")
            if re.search(r'(?<![\w-])\*(?![\w-])', value):
                weak_directives.append(f"{directive} permite un origen comodín '*'")
        if weak_directives:
            detections.append({
                "title":    "Content-Security-Policy presente pero debilitada",
                "severity": "media",
                "evidence": f"La CSP de {base_url}/ incluye: {'; '.join(weak_directives)}. CSP completa: {csp[:300]}",
                "fix_hint": "Eliminar 'unsafe-inline'/'unsafe-eval' y orígenes comodín de script-src/default-src; usar nonces o hashes para scripts inline si son necesarios.",
            })

    if base_url.startswith("https://") and not headers.get("Strict-Transport-Security"):
        detections.append({
            "title":    "Cabecera Strict-Transport-Security (HSTS) ausente",
            "severity": "media",
            "evidence": f"La respuesta HTTPS de {base_url}/ no incluye Strict-Transport-Security.",
            "fix_hint": "Añadir Strict-Transport-Security con un max-age razonable (por ejemplo un año) e includeSubDomains.",
        })
    if not headers.get("X-Frame-Options") and "frame-ancestors" not in csp:
        detections.append({
            "title":    "Cabecera X-Frame-Options ausente",
            "severity": "baja",
            "evidence": f"La respuesta de {base_url}/ no incluye X-Frame-Options ni una directiva frame-ancestors en la CSP.",
            "fix_hint": "Añadir X-Frame-Options: DENY (o SAMEORIGIN), o una directiva frame-ancestors en la CSP.",
        })

    # Modern isolation headers — none of the above three cover these.
    # Presence-only (baja), same treatment as X-Frame-Options: helpful
    # hardening, not something most bounty programs pay for on its own.
    if not headers.get("Permissions-Policy"):
        detections.append({
            "title":    "Cabecera Permissions-Policy ausente",
            "severity": "baja",
            "evidence": f"La respuesta de {base_url}/ no incluye Permissions-Policy.",
            "fix_hint": "Añadir Permissions-Policy restringiendo APIs sensibles del navegador (cámara, micrófono, geolocalización, etc.) que la aplicación no necesita.",
        })
    if not headers.get("Cross-Origin-Opener-Policy"):
        detections.append({
            "title":    "Cabecera Cross-Origin-Opener-Policy ausente",
            "severity": "baja",
            "evidence": f"La respuesta de {base_url}/ no incluye Cross-Origin-Opener-Policy.",
            "fix_hint": "Añadir Cross-Origin-Opener-Policy: same-origin para aislar la página de ventanas de origen cruzado (mitiga ataques tipo Spectre/XS-Leaks).",
        })
    if not headers.get("Cross-Origin-Resource-Policy"):
        detections.append({
            "title":    "Cabecera Cross-Origin-Resource-Policy ausente",
            "severity": "baja",
            "evidence": f"La respuesta de {base_url}/ no incluye Cross-Origin-Resource-Policy.",
            "fix_hint": "Añadir Cross-Origin-Resource-Policy: same-origin (o same-site) para evitar que otros orígenes carguen este recurso directamente.",
        })

    for leaky_header in ("Server", "X-Powered-By"):
        value = headers.get(leaky_header)
        if value and re.search(r'\d', value):
            detections.append({
                "title":    f"Versión de software expuesta en la cabecera {leaky_header}",
                "severity": "baja",
                "evidence": f"{leaky_header}: {value}",
                "fix_hint": f"Configurar el servidor para omitir la versión en la cabecera {leaky_header}.",
            })

    cookies = headers.get_all("Set-Cookie") if hasattr(headers, "get_all") else []
    for cookie in (cookies or []):
        name = cookie.split("=", 1)[0].strip()
        lower = cookie.lower()
        missing = []
        if base_url.startswith("https://") and "secure" not in lower:
            missing.append("Secure")
        if "httponly" not in lower:
            missing.append("HttpOnly")
        if "samesite" not in lower:
            missing.append("SameSite")
        looks_like_session = any(kw in name.lower() for kw in ("sess", "token", "auth", "sid"))
        if missing:
            detections.append({
                "title":    f"Cookie '{name}' sin el atributo {' y '.join(missing)}",
                "severity": "alta" if looks_like_session else "media",
                "evidence": f"Set-Cookie observado en {base_url}/: {cookie[:200]}",
                "fix_hint": f"Añadir {' y '.join(missing)} a la cookie '{name}'.",
            })
        # __Host-/__Secure- prefixes carry browser-ENFORCED guarantees (a
        # cookie named __Host-x literally cannot be set without Secure, a
        # matching Path=/, and no Domain attribute — the browser rejects
        # it otherwise), stronger than any attribute alone. Recommendation-
        # tier (baja), not a missing control — a cookie without the prefix
        # isn't broken, just missing a stronger guarantee.
        if (looks_like_session and base_url.startswith("https://")
                and not name.startswith(("__Host-", "__Secure-"))):
            detections.append({
                "title":    f"Cookie de sesión '{name}' sin prefijo __Host- o __Secure-",
                "severity": "baja",
                "evidence": f"Set-Cookie observado en {base_url}/: {cookie[:200]}",
                "fix_hint": f"Considerar renombrar '{name}' con el prefijo __Host- (requiere Secure, Path=/, sin Domain) para que el navegador rechace la cookie si esas condiciones no se cumplen.",
            })

    if body and base_url.startswith("https://"):
        # Mixed content — an HTTPS page pulling in plain-http:// subresources
        # (scripts especially: an on-path attacker can tamper with an
        # unencrypted script and get full page control even though the
        # page itself loaded over HTTPS). Reuses the body already fetched
        # for the reachability check, no extra request.
        http_srcs = set(re.findall(r'(?:src|href)=["\']http://([^"\']+)["\']', body, re.IGNORECASE))
        if http_srcs:
            examples = ", ".join(f"http://{s}" for s in list(http_srcs)[:5])
            detections.append({
                "title":    "Contenido mixto: recursos cargados por HTTP en una página HTTPS",
                "severity": "media",
                "evidence": f"La página HTTPS {base_url}/ referencia recursos por HTTP sin cifrar, por ejemplo: {examples}",
                "fix_hint": "Cambiar todas las referencias a recursos (scripts, hojas de estilo, imágenes) para que usen HTTPS o URLs relativas al protocolo.",
            })

    return detections


# ── Known-vulnerable version fingerprinting — deliberately the most
#    conservative check in this file. Joan's explicit instruction: when
#    forced to choose, prefer a false negative over a false positive here.
#    Every gate below exists to enforce that:
#      - Only a short, curated list of products with unambiguous version
#        signals (a generator meta tag, a clearly-versioned script
#        filename, a version-bearing Server/X-Powered-By header) is even
#        considered — no guessing from ambiguous strings.
#      - A malformed/partial version string (anything not cleanly
#        MAJOR.MINOR or MAJOR.MINOR.PATCH) is skipped outright rather than
#        rounded or fuzzy-matched.
#      - The actual CVE correlation is delegated to NVD's own cpeName
#        lookup (services.nvd.nist.gov) rather than a hand-rolled version-
#        range parser here — NVD resolves whether the exact observed
#        version falls inside each CVE's affected range, so this file
#        never has to (and never risks getting a range comparison wrong).
#      - Only CRITICAL/HIGH-severity CVEs are surfaced; MEDIUM/LOW are
#        dropped as noise not worth a false-positive risk.
#      - The resulting finding is capped at "alta" even when NVD says
#        CRITICAL — this is a version correlation, not a confirmed
#        exploit, and is worded as such in the evidence text.
#      - Any failure anywhere in this chain (fingerprint didn't match,
#        NVD unreachable/rate-limited, bad JSON) silently yields nothing,
#        never a guess. ──────────────────────────────────────────────────

# vendor:product exactly as NVD's CPE dictionary names them. Short and
# curated on purpose — same "fixed list, not a wordlist" philosophy as
# _SENSITIVE_PATHS and _TAKEOVER_FINGERPRINTS elsewhere in this file.
_VERSION_CPE_MAP = {
    "wordpress": "wordpress:wordpress",
    "drupal":    "drupal:drupal",
    "jquery":    "jquery:jquery",
    "apache":    "apache:http_server",
    "nginx":     "nginx:nginx",
    "php":       "php:php",
}
_CLEAN_VERSION_RE = re.compile(r'^[0-9]+\.[0-9]+(?:\.[0-9]+)?$')
_GENERATOR_META_PRODUCT_RE = re.compile(r'(?i)\b(wordpress|drupal)\b[^0-9<]{0,15}([0-9]+(?:\.[0-9]+){1,2})')
_HEADER_PRODUCT_VERSION_RE = re.compile(r'(?i)\b(apache|nginx|php)\b/([0-9]+(?:\.[0-9]+){1,2})')


def _fingerprint_versions(root_headers, root_body: str | None) -> dict[str, tuple[str, str]]:
    """Returns {product_key: (version, evidence_note)} for whatever matched
    — at most one version per product, first unambiguous signal wins.
    Every signal here is something a normal client already receives on the
    reachability GET; nothing extra is fetched."""
    found: dict[str, tuple[str, str]] = {}

    if root_body:
        gm = _GENERATOR_META_PRODUCT_RE.search(root_body)
        if gm:
            product = gm.group(1).lower()
            found[product] = (gm.group(2), f"La etiqueta meta 'generator' de la página anuncia {gm.group(0).strip()}.")

        for src in _SCRIPT_SRC_RE.findall(root_body):
            if "jquery" in src.lower() and "jquery" not in found:
                vm = re.search(r'([0-9]+\.[0-9]+\.[0-9]+)', src)
                if vm:
                    found["jquery"] = (vm.group(1), f"Script cargado con nombre/versión visible: {src}")

    if root_headers:
        for header_name in ("Server", "X-Powered-By"):
            value = root_headers.get(header_name)
            if not value:
                continue
            hm = _HEADER_PRODUCT_VERSION_RE.search(value)
            if hm:
                product = hm.group(1).lower()
                found.setdefault(product, (hm.group(2), f"Cabecera {header_name}: {value}"))

    return found


# A CVE list for a given (product, version) doesn't change minute to
# minute, but Auto Mode re-scans the same target every ~10 min and (once
# the subdomain-suite extension below is in play) the same product can get
# fingerprinted on several subdomains in a single scan — without caching
# that's needless repeated traffic against NVD's unauthenticated rate limit
# (5 req/30s) for zero new information. Same dict+lock+monotonic-timestamp
# pattern as core.tools_search's _search_cache, just a longer TTL since CVE
# publication is far slower-moving than search results.
_NVD_CACHE_TTL = 6 * 3600  # 6 hours
_nvd_cache: dict[tuple[str, str], dict] = {}
_nvd_cache_lock = threading.Lock()


def _nvd_lookup_high_severity_cves(product_key: str, version: str) -> list[tuple[str, str, float | None]]:
    """Cached wrapper around _nvd_fetch_high_severity_cves — see that
    function for the actual NVD query/parsing. Returns up to 3
    (cve_id, severity, score) tuples, CRITICAL first, MEDIUM/LOW dropped
    entirely. Never raises."""
    cache_key = (product_key, version)
    now_ts = time.monotonic()
    with _nvd_cache_lock:
        cached = _nvd_cache.get(cache_key)
        if cached is not None and (now_ts - cached["timestamp"]) < _NVD_CACHE_TTL:
            return cached["data"]

    result = _nvd_fetch_high_severity_cves(product_key, version)

    with _nvd_cache_lock:
        _nvd_cache[cache_key] = {"data": result, "timestamp": now_ts}
    return result


def _nvd_fetch_high_severity_cves(product_key: str, version: str) -> list[tuple[str, str, float | None]]:
    """Queries NVD's public CVE API 2.0 for the exact CPE (product+version)
    — NVD's own matching logic resolves whether that exact version falls
    inside each CVE's affected range, so no version-range parsing happens
    in this file. No API key used (fine at this call volume — a handful of
    fingerprinted products per scan, cached across repeat scans by the
    wrapper above). Returns up to 3 (cve_id, severity, score) tuples,
    CRITICAL first, MEDIUM/LOW dropped entirely. Never raises; any failure
    (unreachable, rate-limited, bad JSON, unexpected schema) returns []."""
    cpe_product = _VERSION_CPE_MAP.get(product_key)
    if not cpe_product:
        return []
    cpe_name = f"cpe:2.3:a:{cpe_product}:{version}:*:*:*:*:*:*:*"
    url = f"https://services.nvd.nist.gov/rest/json/cves/2.0?cpeName={urllib.parse.quote(cpe_name)}&resultsPerPage=20"
    status, _, body = _fetch(url, timeout=12)
    if status != 200 or not body:
        return []
    try:
        data = json.loads(body)
    except Exception:
        return []

    results = []
    for vuln in data.get("vulnerabilities", []) or []:
        cve = vuln.get("cve", {})
        cve_id = cve.get("id")
        if not cve_id:
            continue
        metrics = cve.get("metrics", {}) or {}
        severity, score = None, None
        for metric_key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
            entries = metrics.get(metric_key)
            if entries:
                cvss_data = entries[0].get("cvssData", {}) or {}
                severity = (entries[0].get("baseSeverity") or cvss_data.get("baseSeverity") or "").upper()
                score = cvss_data.get("baseScore")
                break
        if severity not in ("CRITICAL", "HIGH"):
            continue
        results.append((cve_id, severity, score))

    results.sort(key=lambda r: (r[1] != "CRITICAL", -(r[2] or 0)))
    return results[:3]


def _check_known_vulnerable_versions(root_headers, root_body: str | None) -> list[dict]:
    detections = []
    for product_key, (version, evidence_note) in _fingerprint_versions(root_headers, root_body).items():
        if not _CLEAN_VERSION_RE.match(version):
            continue  # ambiguous/partial version — skip rather than risk a wrong match
        try:
            cves = _nvd_lookup_high_severity_cves(product_key, version)
        except Exception:
            cves = []
        if not cves:
            continue
        cve_summary = "; ".join(
            f"{cid} ({sev}{f', CVSS {score}' if score is not None else ''})" for cid, sev, score in cves
        )
        detections.append({
            "title":    f"Versión de {product_key} potencialmente vulnerable ({version})",
            "severity": "alta",  # capped here even if NVD says CRITICAL — see block comment above
            "evidence": (
                f"{evidence_note} La National Vulnerability Database (NVD) lista CVE(s) de severidad "
                f"alta/crítica para {product_key} {version}: {cve_summary}. Esto es una correlación por "
                "número de versión contra NVD, no una explotación confirmada contra este objetivo — "
                "verificar manualmente antes de reportar."
            ),
            "fix_hint": f"Confirmar la versión exacta de {product_key} en uso y actualizar a la última versión estable, revisando el changelog de seguridad de cada CVE listado.",
        })
    return detections


def _check_tls(domain: str) -> list[dict]:
    detections = []
    host = domain.strip().lstrip("*.").split("/")[0]
    try:
        with socket.create_connection((host, 443), timeout=TLS_TIMEOUT) as sock:
            with _SSL_CTX.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
                version = ssock.version()
    except Exception:
        return detections

    if version in ("TLSv1", "TLSv1.1"):
        detections.append({
            "title":    f"Protocolo TLS obsoleto en uso ({version})",
            "severity": "media",
            "evidence": f"La conexión TLS a {host}:443 negoció {version}.",
            "fix_hint": "Deshabilitar TLS 1.0/1.1 en el servidor y exigir TLS 1.2 o superior.",
        })

    not_after = (cert or {}).get("notAfter")
    if not_after:
        try:
            expiry = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
            days_left = (expiry - datetime.now(timezone.utc)).days
            if days_left < 14:
                detections.append({
                    "title":    "Certificado TLS expirado" if days_left < 0 else "Certificado TLS próximo a expirar",
                    "severity": "alta",
                    "evidence": f"El certificado de {host} {'expiró' if days_left < 0 else 'expira'} el {not_after} ({days_left} días).",
                    "fix_hint": "Renovar el certificado TLS antes de su fecha de expiración.",
                })
        except Exception:
            pass
    return detections


def _check_https_enforcement(domain: str) -> list[dict]:
    """One extra GET to the plain http:// scheme (never following
    redirects, so the response actually seen is the server's own first
    hop) — same spirit as the TLS check above, just checking that HTTP
    even bothers to redirect to HTTPS at all. If the plain-HTTP port
    doesn't answer (closed/firewalled/timeout), that's not a finding —
    it's arguably the safer configuration, so this stays silent rather
    than guessing."""
    host = domain.strip().lstrip("*.").split("/")[0]
    http_url = f"http://{host}/"
    status, headers, _ = _fetch(http_url, follow_redirects=False)
    if status is None:
        return []

    if 300 <= status < 400:
        location = (headers.get("Location") or "") if headers else ""
        if location.lower().startswith("https://"):
            return []  # correctly redirects to HTTPS — nothing to report
        return [{
            "title":    "Redirección desde HTTP no apunta a HTTPS",
            "severity": "media",
            "evidence": f"GET {http_url} devuelve HTTP {status} con Location: {location or '(vacío)'} — no es una URL https://.",
            "fix_hint": "Cambiar la redirección para que apunte explícitamente a la versión HTTPS del sitio.",
        }]

    if status == 200:
        return [{
            "title":    "El sitio responde por HTTP sin forzar redirección a HTTPS",
            "severity": "media",
            "evidence": f"GET {http_url} devuelve HTTP 200 (contenido servido en claro) en lugar de redirigir a HTTPS.",
            "fix_hint": "Configurar el servidor para redirigir toda petición HTTP a HTTPS (301/308) y considerar HSTS con includeSubDomains.",
        }]
    return []


def _check_security_txt(base_url: str) -> list[dict]:
    for path in ("/.well-known/security.txt", "/security.txt"):
        status, _, body = _fetch(base_url + path)
        if status == 200 and body and "contact" in body.lower():
            return []
    return [{
        "title":    "Sin archivo security.txt publicado",
        "severity": "baja",
        "evidence": f"Ni {base_url}/.well-known/security.txt ni {base_url}/security.txt devuelven un archivo válido.",
        "fix_hint": "Publicar un security.txt (RFC 9116) en /.well-known/security.txt con un contacto de seguridad.",
    }]


def _looks_like_directory_listing(body: str) -> bool:
    if not body:
        return False
    lowered = body.lower()
    return "index of /" in lowered or "<title>index of" in lowered or "parent directory</a>" in lowered


def _check_sensitive_paths(base_url: str, paths: list[str] | None = None, source_note: str = "", no_auto_resolve: bool = False) -> list[dict]:
    """paths defaults to the fixed _SENSITIVE_PATHS list; callers can pass a
    different (still short, bounded) list — e.g. _check_wayback_paths passes
    historically-indexed paths instead. source_note, if given, is appended
    to each finding's evidence so it's clear where the path came from.
    no_auto_resolve=True tags every detection with "no_auto_resolve": True
    — set by callers whose input path list comes from a third-party
    discovery source that can vary run to run (Wayback's CDX API, robots.txt
    changing) rather than a fixed list checked identically every scan; see
    _run_scan_thread's auto-resolve logic in core/bughunter_routes.py for
    why that distinction matters (a path not rediscovered this run isn't
    reliable evidence the underlying exposure is actually gone)."""
    detections = []
    paths = _SENSITIVE_PATHS if paths is None else paths
    if not paths:
        return detections
    # Baseline against a path that shouldn't exist, so a "soft 404" (a site
    # that returns 200 + a friendly error page for everything) doesn't get
    # misread as every sensitive path being exposed.
    probe_path = f"/__lira_bughunter_probe_{uuid.uuid4().hex[:10]}__"
    baseline_status, _, baseline_body = _fetch(base_url + probe_path)

    for path in paths:
        status, _, body = _fetch(base_url + path)
        if status != 200 or not body:
            continue
        if baseline_status == 200 and body.strip() == (baseline_body or "").strip():
            continue

        suffix = f" {source_note}" if source_note else ""
        if path.endswith("/"):
            if _looks_like_directory_listing(body):
                detections.append({
                    "title":    f"Listado de directorio expuesto en {path}",
                    "severity": "baja",
                    "evidence": f"GET {base_url}{path} devuelve un listado de directorio (HTTP {status}).{suffix}",
                    "fix_hint": "Desactivar el listado de directorio en la configuración del servidor y devolver 403/404 para esa ruta.",
                })
        else:
            detections.append({
                "title":    f"Archivo potencialmente sensible accesible en {path}",
                "severity": "alta",
                "evidence": f"GET {base_url}{path} devuelve HTTP {status} con contenido.{suffix}",
                "fix_hint": f"Restringir el acceso público a {path} (moverlo fuera del webroot o bloquearlo en la configuración del servidor).",
            })
    if no_auto_resolve:
        for d in detections:
            d["no_auto_resolve"] = True
    return detections


# Substrings that make a historically-indexed Wayback Machine path worth
# actually re-checking live today — deliberately the same "accidental
# exposure" spirit as _SENSITIVE_PATHS, just sourced dynamically instead of
# a fixed list. A path like /old-blog/post-42 indexed by Wayback is not
# interesting; /admin/backup.zip indexed by Wayback IS worth a live GET.
_WAYBACK_INTERESTING_KEYWORDS = [
    "admin", "backup", ".env", ".git", "config", "swagger", "api-docs",
    "openapi", "debug", "phpinfo", ".sql", ".zip", ".bak", "internal",
    "staging", "wp-config", ".htpasswd",
]
_MAX_WAYBACK_PATHS_CHECKED = 12


def _discover_wayback_interesting_paths(domain: str, limit: int = _MAX_WAYBACK_PATHS_CHECKED) -> list[str]:
    """Queries archive.org's public CDX API for URLs it has ever indexed
    under this domain — a request to archive.org, never to the target
    itself, so this is zero-risk recon even before anything gets re-checked
    live. Filters down to paths matching _WAYBACK_INTERESTING_KEYWORDS
    (things worth a live re-check), deduped, bounded to `limit`. A path
    showing up here only means it existed at some point in the past — it's
    _check_sensitive_paths's job (called by the caller) to confirm it's
    still actually accessible before anything becomes a real finding."""
    host = domain.strip().lstrip("*.").split("/")[0]
    cdx_url = (
        "http://web.archive.org/cdx/search/cdx"
        f"?url={urllib.parse.quote(host)}/*&output=json&collapse=urlkey"
        "&filter=statuscode:200&limit=5000"
    )
    status, _, body = _fetch(cdx_url, timeout=12)
    if status != 200 or not body:
        return []
    try:
        rows = json.loads(body)
    except Exception:
        return []
    if len(rows) < 2:
        return []

    paths = []
    seen = set()
    for row in rows[1:]:  # rows[0] is the CDX column header
        if len(row) < 3:
            continue
        original_url = row[2]
        try:
            parsed = urllib.parse.urlparse(original_url)
        except Exception:
            continue
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        lowered = path.lower()
        if path in seen or not any(kw in lowered for kw in _WAYBACK_INTERESTING_KEYWORDS):
            continue
        seen.add(path)
        paths.append(path)
        if len(paths) >= limit:
            break
    return paths


def _check_wayback_paths(base_url: str, domain: str) -> list[dict]:
    """Historical-recon-turned-live-check: finds paths that were once
    indexed and look accidentally-sensitive (see keyword list above), then
    re-checks each live with the same baseline/soft-404 logic
    _check_sensitive_paths already uses for its fixed list — a historical
    hit only becomes a finding if it's still actually reachable today."""
    interesting = _discover_wayback_interesting_paths(domain)
    if not interesting:
        return []
    return _check_sensitive_paths(
        base_url, paths=interesting,
        source_note="(ruta descubierta vía indexación histórica de Wayback Machine — no en la lista fija de rutas comunes.)",
        no_auto_resolve=True,
    )


_MAX_ROBOTS_SITEMAP_PATHS = 15
_ROBOTS_DISALLOW_RE  = re.compile(r'(?im)^\s*disallow:\s*(\S+)')
_SITEMAP_LOC_RE      = re.compile(r'(?i)<loc>\s*([^<\s]+)\s*</loc>')


def _discover_robots_sitemap_paths(base_url: str) -> list[str]:
    """Two more free-to-read, deliberately-published files, same spirit as
    security.txt — a normal client can always GET these. robots.txt's
    Disallow lines are the site owner's OWN curated "don't look here" list
    (higher signal than a generic wordlist by definition — someone
    specifically decided to hide it, then told search engines exactly
    where it is), and sitemap.xml occasionally lists a forgotten/staging
    URL that slipped in. Both filtered through the same
    _WAYBACK_INTERESTING_KEYWORDS list used for Wayback paths, so a
    Disallow: /search or /cart (SEO noise, not a security concern) doesn't
    turn into a false 'sensitive file' finding just for being disallowed."""
    paths: list[str] = []
    seen = set()

    robots_status, _, robots_body = _fetch(base_url + "/robots.txt")
    if robots_status == 200 and robots_body:
        for raw in _ROBOTS_DISALLOW_RE.findall(robots_body):
            path = raw.strip()
            if not path or path == "/" or "*" in path:
                continue
            if not path.startswith("/"):
                path = "/" + path
            lowered = path.lower()
            if path in seen or not any(kw in lowered for kw in _WAYBACK_INTERESTING_KEYWORDS):
                continue
            seen.add(path)
            paths.append(path)
            if len(paths) >= _MAX_ROBOTS_SITEMAP_PATHS:
                return paths

    sitemap_status, _, sitemap_body = _fetch(base_url + "/sitemap.xml")
    if sitemap_status == 200 and sitemap_body:
        host = urllib.parse.urlparse(base_url).netloc
        for loc in _SITEMAP_LOC_RE.findall(sitemap_body):
            try:
                parsed = urllib.parse.urlparse(loc)
            except Exception:
                continue
            if parsed.netloc and parsed.netloc != host:
                continue  # sitemap index pointing at a different host — not ours to check
            path = parsed.path or "/"
            lowered = path.lower()
            if path in seen or not any(kw in lowered for kw in _WAYBACK_INTERESTING_KEYWORDS):
                continue
            seen.add(path)
            paths.append(path)
            if len(paths) >= _MAX_ROBOTS_SITEMAP_PATHS:
                break

    return paths


def _check_robots_sitemap_paths(base_url: str) -> list[dict]:
    """Same live-recheck pattern as _check_wayback_paths — a path showing
    up in robots.txt/sitemap.xml only means the site owner referenced it,
    not that it's actually still reachable or was ever public content."""
    interesting = _discover_robots_sitemap_paths(base_url)
    if not interesting:
        return []
    return _check_sensitive_paths(
        base_url, paths=interesting,
        source_note="(ruta señalada en robots.txt o sitemap.xml — no en la lista fija de rutas comunes.)",
        no_auto_resolve=True,
    )


def _check_error_disclosure(base_url: str, body: str | None) -> list[dict]:
    """Free check — no extra request, just inspects a body already fetched
    for another reason (the reachability GET at the top of run_scan)."""
    if not body:
        return []
    lowered = body.lower()
    for signature in _ERROR_DISCLOSURE_SIGNATURES:
        if signature in lowered:
            return [{
                "title":    "Página de error con información de depuración expuesta",
                "severity": "media",
                "evidence": f"La respuesta de {base_url}/ contiene el texto '{signature}', típico de una página de error/depuración de framework.",
                "fix_hint": "Desactivar el modo de depuración/verbose errors en producción y servir una página de error genérica.",
            }]
    return []


def _dig_txt(name: str) -> list[str]:
    """Plain DNS TXT lookup via the system `dig` binary (no extra Python DNS
    dependency needed — dnspython isn't installed in this project, and this
    is a read-only public DNS query, not a request to the target itself).
    Returns raw TXT record strings (quotes stripped), or [] on any failure
    (missing `dig`, timeout, NXDOMAIN, etc.) — never raises."""
    import subprocess
    try:
        result = subprocess.run(
            ["dig", "+short", "+time=4", "+tries=1", "TXT", name],
            capture_output=True, text=True, timeout=6,
        )
        if result.returncode != 0:
            return []
        return [line.strip().strip('"') for line in result.stdout.splitlines() if line.strip()]
    except Exception:
        return []


def _check_email_spoofing(domain: str) -> list[dict]:
    """Passive DNS-only check — never touches the target's web server at
    all, just public TXT records for the domain itself and _dmarc.<domain>.
    Missing/weak SPF or DMARC is invisible to anyone just browsing the
    site (it only matters for mail delivery) but is exactly what lets an
    attacker send convincingly spoofed phishing email 'from' this domain —
    a routinely-accepted bug bounty finding on many programs."""
    host = domain.strip().lstrip("*.").split("/")[0]
    detections = []

    spf_records = [r for r in _dig_txt(host) if r.lower().startswith("v=spf1")]
    if not spf_records:
        detections.append({
            "title":    "Sin registro SPF publicado",
            "severity": "media",
            "evidence": f"No se encontró ningún registro TXT que empiece con 'v=spf1' para {host}.",
            "fix_hint": f"Publicar un registro SPF (TXT en {host}) que declare explícitamente qué servidores están autorizados a enviar correo en nombre del dominio, terminando en '-all' (fallo estricto) en vez de '~all' o '?all'.",
        })
    elif any(r.rstrip().endswith(("?all", "+all")) for r in spf_records):
        detections.append({
            "title":    "Registro SPF permisivo (?all o +all)",
            "severity": "media",
            "evidence": f"El registro SPF de {host} termina en un calificador permisivo: {spf_records[0]}",
            "fix_hint": "Cambiar el calificador final a '-all' (fallo estricto) una vez confirmados todos los remitentes legítimos.",
        })

    dmarc_records = [r for r in _dig_txt(f"_dmarc.{host}") if r.lower().startswith("v=dmarc1")]
    if not dmarc_records:
        detections.append({
            "title":    "Sin registro DMARC publicado",
            "severity": "media",
            "evidence": f"No se encontró ningún registro TXT en _dmarc.{host} que empiece con 'v=DMARC1'.",
            "fix_hint": f"Publicar un registro DMARC en _dmarc.{host} (por ejemplo 'v=DMARC1; p=quarantine; rua=mailto:...') para que los receptores de correo sepan qué hacer con mensajes que fallan SPF/DKIM.",
        })
    elif "p=none" in dmarc_records[0].lower():
        detections.append({
            "title":    "Política DMARC en modo 'none' (solo monitorización)",
            "severity": "baja",
            "evidence": f"El registro DMARC de {host} usa p=none: {dmarc_records[0]}",
            "fix_hint": "Una vez confirmado que el correo legítimo pasa SPF/DKIM, mover la política a p=quarantine o p=reject para que realmente bloquee el correo spoofeado.",
        })
    return detections


# Dork queries for finding public GitHub content that references this
# domain alongside something that smells like a real credential. Each is
# deliberately narrow (a specific filename/keyword pattern known to
# correlate with a real leaked secret, not just "site:github.com
# <domain>") to keep noise down — see discover_program_suggestions above
# for the same "reuse web search, never touch the target" approach.
_GITHUB_DORK_TEMPLATES = [
    'site:github.com "{host}" ("api_key" OR "apikey" OR "api key") -site:github.com/search',
    'site:github.com "{host}" (".env" OR "secret_key" OR "SECRET_KEY")',
    'site:github.com "{host}" "BEGIN PRIVATE KEY"',
    'site:github.com "{host}" filename:.env',
]


def _check_github_exposure(domain: str) -> list[dict]:
    """Passive third-party search — never sends a single request to the
    target. Reuses core.tools_search.search_web() (same infra
    discover_program_suggestions already uses) to look for public GitHub
    content that mentions this domain alongside credential-shaped keywords.
    A hit is a SIGNAL a human should go read, not a confirmed leak — the
    search result snippet alone can't prove a real secret is there (could
    be a placeholder, an example, a false-positive keyword match) — worded
    as 'posible' throughout, same treatment as the subdomain-takeover
    fingerprint check. Never raises; a failed search for one dork just
    contributes nothing."""
    from core import tools_search

    host = domain.strip().lstrip("*.").split("/")[0]
    detections = []
    seen_urls = set()
    for template in _GITHUB_DORK_TEMPLATES:
        query = template.format(host=host)
        try:
            hits = tools_search.search_web(query)
        except Exception:
            hits = []
        for hit in hits:
            url = (hit.get("url") or "").strip()
            if not url or "github.com" not in url or url in seen_urls:
                continue
            seen_urls.add(url)
            snippet = (hit.get("snippet") or "").strip()
            detections.append({
                "title":    "Posible exposición de credenciales en repositorio público de GitHub",
                "severity": "media",
                "evidence": f"Búsqueda '{query}' devuelve {url}"
                            + (f" — fragmento: \"{snippet[:200]}\"" if snippet else "") + ".",
                "fix_hint": "Revisar manualmente el repositorio/archivo. Si contiene una credencial real, revocarla de inmediato, eliminarla del historial de git (no basta con un nuevo commit que la borre), y moverla a un gestor de secretos.",
                # Search ranking varies run to run — not finding this hit
                # again next scan is not evidence the repo/file is gone, so
                # this must never be auto-resolved, only manually dismissed.
                "no_auto_resolve": True,
            })
    return detections


def _check_js_secrets(base_url: str, root_body: str | None) -> tuple[list[dict], list[str]]:
    """Passive: extracts <script src> URLs from the already-fetched home
    page, GETs each one (a normal browser already downloads these — no
    extra surface touched), and does three things per file:
      1) Checks for a hardcoded secret matching one of the fixed vendor-key
         patterns in _SECRET_PATTERNS.
      2) Checks for a source map at <script-url>.map — leaks the original,
         unminified source (and often comments/internal paths/sometimes
         secrets that got minified out of the shipped bundle).
      3) Extracts API-endpoint-looking paths (_JS_ENDPOINT_RE) — purely
         informational, returned separately from `detections` since these
         aren't findings on their own, just discovery for a human to
         explore with real auth context.
    All three are the kind of thing invisible to a normal skim of the
    rendered page — you have to actually go looking in what got shipped.
    Bounded to _MAX_JS_FILES_SCANNED files so this stays "read what's
    linked", not an unbounded crawl. Returns (detections, endpoints)."""
    detections: list[dict] = []
    endpoints: list[str] = []
    if not root_body:
        return detections, endpoints

    srcs = _SCRIPT_SRC_RE.findall(root_body)
    seen_urls = set()
    seen_endpoints = set()
    checked = 0
    for src in srcs:
        if checked >= _MAX_JS_FILES_SCANNED:
            break
        js_url = urllib.parse.urljoin(base_url + "/", src)
        if js_url in seen_urls or not js_url.startswith(("http://", "https://")):
            continue
        seen_urls.add(js_url)
        checked += 1

        status, _, body = _fetch(js_url)
        if status != 200 or not body:
            continue

        if len(endpoints) < _MAX_ENDPOINTS_SURFACED:
            for match in _JS_ENDPOINT_RE.findall(body):
                if match not in seen_endpoints:
                    seen_endpoints.add(match)
                    endpoints.append(match)
                    if len(endpoints) >= _MAX_ENDPOINTS_SURFACED:
                        break

        for label, pattern in _SECRET_PATTERNS:
            match = pattern.search(body)
            if match:
                snippet = match.group(0)
                redacted = snippet[:6] + "…" + snippet[-4:] if len(snippet) > 12 else "…"
                detections.append({
                    "title":    f"Posible {label} expuesta en JavaScript servido públicamente",
                    "severity": "critica",
                    "evidence": f"GET {js_url} contiene una cadena que coincide con el formato de {label} ({redacted}).",
                    "fix_hint": "Revocar la credencial inmediatamente, eliminarla del bundle, y moverla a una variable de entorno/secreto del lado del servidor — nunca en código servido al cliente.",
                })
                break  # one hit per file is enough to flag it for review

        map_url = js_url + ".map"
        map_status, _, map_body = _fetch(map_url)
        if map_status == 200 and map_body and '"sources"' in map_body[:2000]:
            detections.append({
                "title":    "Source map expuesto — código fuente sin minificar accesible",
                "severity": "media",
                "evidence": f"GET {map_url} devuelve un source map válido (HTTP {map_status}), exponiendo el código fuente original de {js_url}.",
                "fix_hint": "No publicar archivos .map en producción (excluirlos del build desplegado, o restringir su acceso), ya que revelan la estructura interna y comentarios del código fuente.",
            })
    return detections, endpoints


def _check_cors(base_url: str) -> list[dict]:
    """Reflected-origin CORS misconfiguration — sends a normal GET with an
    Origin header naming an unrelated test domain and checks whether the
    server reflects it back in Access-Control-Allow-Origin WHILE also
    allowing credentials. That combination (not a bare Access-Control-
    Allow-Origin: *, which is normal for public APIs) is what actually
    makes cross-origin data theft possible — purely observational, no
    cross-origin request is ever really made."""
    test_origin = "https://lira-bughunter-cors-test.invalid"
    status, headers, _ = _fetch(base_url + "/", extra_headers={"Origin": test_origin})
    if status is None or headers is None:
        return []
    acao = headers.get("Access-Control-Allow-Origin")
    acac = (headers.get("Access-Control-Allow-Credentials") or "").lower()
    if acao == test_origin and acac == "true":
        return [{
            "title":    "CORS mal configurado: refleja el Origin y permite credenciales",
            "severity": "alta",
            "evidence": f"GET {base_url}/ con cabecera 'Origin: {test_origin}' devuelve Access-Control-Allow-Origin: {test_origin} y Access-Control-Allow-Credentials: true.",
            "fix_hint": "No reflejar dinámicamente cualquier Origin en Access-Control-Allow-Origin cuando Access-Control-Allow-Credentials es true — usar una lista explícita de orígenes permitidos.",
        }]
    return []


def _check_open_redirect(base_url: str) -> list[dict]:
    """Passive: appends a benign external test URL to a handful of common
    redirect-param names and checks whether the server's FIRST response
    (no redirects followed — see _fetch's follow_redirects=False) actually
    points at that exact external address. Never visits the test address
    itself, just reads the Location header the server would have sent."""
    for param in _REDIRECT_PARAMS:
        url = f"{base_url}/?{param}={urllib.parse.quote(_REDIRECT_TEST_TARGET, safe='')}"
        status, headers, _ = _fetch(url, follow_redirects=False)
        if status is None or headers is None or not (300 <= status < 400):
            continue
        location = headers.get("Location") or ""
        if location.startswith(_REDIRECT_TEST_TARGET):
            return [{
                "title":    f"Redirección abierta a través del parámetro '{param}'",
                "severity": "media",
                "evidence": f"GET {url} devuelve HTTP {status} con Location: {location}",
                "fix_hint": f"Validar '{param}' contra una lista de destinos permitidos (o rutas relativas propias) en vez de redirigir a cualquier URL recibida.",
            }]
    return []


def _check_subdomain_takeover(subdomains: list[str]) -> list[dict]:
    """Passive fingerprint match against a handful of discovered subdomains
    (from crt.sh — never a fresh brute-force enumeration) — a normal GET to
    each, checking the response body against known 'this cloud resource no
    longer exists / was never claimed' pages. A match is SUGGESTIVE of a
    takeover candidate, not proof — worded as 'posible' throughout, and
    Joan should manually confirm (e.g. actually check the DNS CNAME) before
    treating it as confirmed in a report. Bounded to a small sample so this
    never turns into a mass-scan of every discovered subdomain."""
    detections = []
    for sub in subdomains[:8]:
        status, _, body = _fetch(f"https://{sub}/", timeout=6)
        if status is None or not body:
            continue
        lowered = body.lower()
        for service, fingerprint in _TAKEOVER_FINGERPRINTS:
            if fingerprint in lowered:
                detections.append({
                    "title":    f"Posible toma de control de subdominio: {sub}",
                    "severity": "alta",
                    "evidence": f"GET https://{sub}/ devuelve una página que coincide con la huella de '{service}' ({fingerprint!r}), típica de un recurso en la nube ya no reclamado.",
                    "fix_hint": f"Confirmar manualmente el registro DNS (CNAME) de {sub} y, si apunta a un recurso de {service} no reclamado, eliminar el registro DNS o reclamar el recurso.",
                })
                break
    return detections


# Discovered subdomains previously only ever got the takeover-fingerprint
# check above — every other check in this file only ever ran against the
# Scope target's own root URL, even though a dev/staging subdomain or an
# app's actual routes are routinely where the real findings are. Bounded
# well below the takeover check's own 8-sample: each subdomain here runs
# ~8 checks itself (up to ~9 requests each with JS/version-fingerprint
# fetches), so this already multiplies total scan requests several times
# over — kept small deliberately so a single scan stays a reasonable
# duration, especially under Auto Mode's ~10-min cadence.
_MAX_SUBDOMAINS_FULL_SCAN = 5


def _run_subdomain_check_suite(sub: str) -> list[dict]:
    """Runs the same core checks as run_scan's primary-domain pass, against
    one discovered subdomain. Deliberately excludes checks that are
    domain-level or third-party-API-heavy rather than per-host: SPF/DMARC
    (mail is normally configured at the apex, not per subdomain — running
    it here would just be noise), GitHub-exposure and Wayback/robots/
    sitemap (org-wide or crawl-derived, not meaningfully different per
    subdomain, and each is its own external API call that scales badly
    once multiplied by _MAX_SUBDOMAINS_FULL_SCAN). security.txt is skipped
    too — already a low-severity, frequently-excluded finding at the
    primary domain; not worth the extra requests repeated per subdomain.
    Each finding's title gets prefixed with the subdomain (e.g.
    '[sub.example.com] Cabecera HSTS ausente') — not just cosmetic: the
    same check title recurring across several subdomains in one scan would
    otherwise collide under _run_scan_thread's (target, title) dedup key
    in core/bughunter_routes.py and silently drop every subdomain's
    version but the first, since `target` there is the whole Scope entry's
    name, not a per-host value. Never raises — a failure fetching one
    subdomain just yields no detections for it."""
    base_url = f"https://{sub}"
    status, headers, body = _fetch(base_url + "/")
    if status is None:
        return []

    detections: list[dict] = []
    detections.extend(_check_headers_from_response(base_url, headers, body))
    detections.extend(_check_error_disclosure(base_url, body))
    detections.extend(_check_known_vulnerable_versions(headers, body))
    js_detections, _js_endpoints = _check_js_secrets(base_url, body)
    detections.extend(js_detections)
    detections.extend(_check_tls(sub))
    detections.extend(_check_https_enforcement(sub))
    detections.extend(_check_sensitive_paths(base_url))
    detections.extend(_check_cors(base_url))
    detections.extend(_check_open_redirect(base_url))
    for d in detections:
        d["title"] = f"[{sub}] {d['title']}"
    return detections


def discover_subdomains(domain: str, limit: int = 25) -> list[str]:
    """Passive-only — queries public certificate-transparency logs (crt.sh),
    never touches the target itself. Returns a deduped, sorted list of
    subdomains (may be empty on any failure/timeout). Informational: the
    caller does NOT auto-add these to Scope, Joan reviews and adds them
    manually if he wants LIRA working on them."""
    host = domain.strip().lstrip("*.").split("/")[0]
    url = f"https://crt.sh/?q=%25.{urllib.parse.quote(host)}&output=json"
    status, _, body = _fetch(url, timeout=10)
    if status != 200 or not body:
        return []
    try:
        rows = json.loads(body)
    except Exception:
        return []
    names = set()
    for row in rows:
        for name in str(row.get("name_value", "")).split("\n"):
            name = name.strip().lower()
            if name and "*" not in name and name.endswith(host):
                names.add(name)
    return sorted(names)[:limit]


def _draft_finding_text(detection: dict, target_name: str) -> dict:
    """Turns a mechanical detection into the report prose the Findings UI
    shows. Tries Ollama first; falls back to a template built straight from
    the detection fields if Ollama is unavailable or the response doesn't
    parse — a finding is never dropped for lack of a working LLM."""
    if _ollama_available():
        system = (
            "Eres una analista de seguridad redactando un hallazgo de bug bounty en español, "
            "para un informe que se copiará y pegará tal cual a la empresa. Sé precisa, técnica "
            "pero clara, y NUNCA inventes datos que no te den — usa solo la evidencia "
            "proporcionada. No sugieras nada destructivo ni ofensivo, solo la corrección defensiva."
        )
        user = (
            f"Objetivo: {target_name}\n"
            f"Título del hallazgo: {detection['title']}\n"
            f"Evidencia observada: {detection['evidence']}\n"
            f"Pista de corrección: {detection['fix_hint']}\n\n"
            "Devuelve exactamente 3 párrafos separados por una línea en blanco, sin "
            "encabezados ni numeración:\n"
            "1) Descripción técnica del hallazgo (2-4 frases).\n"
            "2) Impacto potencial si no se corrige (2-3 frases).\n"
            "3) Sugerencia de corrección concreta (1-3 frases)."
        )
        text = _ollama_generate(system, user, max_tokens=400)
        if text:
            parts = [p.strip() for p in re.split(r'\n\s*\n', text.strip()) if p.strip()]
            if len(parts) >= 3:
                return {"description": parts[0], "impact": parts[1], "fix_suggestion": parts[2]}

    return {
        "description":   detection["evidence"],
        "impact":        "Este hallazgo, combinado con otras debilidades, puede ampliar el impacto de un ataque futuro contra este objetivo.",
        "fix_suggestion": detection["fix_hint"],
    }


def run_scan(target: dict, on_progress=None):
    """Runs every passive check against target (a Scope entry:
    id/name/domain/...). Returns (findings, subdomains, checked_hosts) —
    see module docstring. checked_hosts is [primary_host] plus whichever
    subdomains actually got the full check suite this run (bounded by
    _MAX_SUBDOMAINS_FULL_SCAN — NOT the same as `subdomains`, which is
    every host crt.sh returned, most of which were never actually
    checked). core.bughunter_routes._run_scan_thread needs this to know
    which existing findings are even eligible to auto-resolve — a
    subdomain that dropped out of this run's sample wasn't re-verified,
    so its old findings must be left alone, not assumed fixed.
    on_progress(str), if given, is called with a short message after each
    step — feeds the Scan tab's live log. Never raises; a failing check
    just contributes nothing."""
    def _progress(msg: str):
        if on_progress:
            try:
                on_progress(msg)
            except Exception:
                pass

    domain = target.get("domain", "")
    base_url = _base_url(domain)
    target_name = target.get("name", domain)
    primary_host = domain.strip().lstrip("*.").split("/")[0]

    detections: list[dict] = []

    # Reachability gate — every check below (except crt.sh, which queries a
    # third party, not the target) assumes the target actually answers.
    # Without this, an unreachable domain (typo, DNS not yet propagated, a
    # placeholder Scope entry) would silently produce misleading findings
    # like "no security.txt published" when the real story is "nothing here
    # responded at all" — reported as a finding, wrongly, in a real run of
    # this scan before this check was added.
    _progress(f"Comprobando accesibilidad de {base_url}")
    root_status, root_headers, root_body = _fetch(base_url + "/")
    if root_status is None:
        _progress(f"{base_url} no respondió — no se puede escanear (¿dominio incorrecto o inaccesible?)")
        return [], [], []

    _progress(f"Comprobando cabeceras de seguridad en {base_url}")
    detections.extend(_check_headers_from_response(base_url, root_headers, root_body))
    detections.extend(_check_error_disclosure(base_url, root_body))

    _progress("Buscando versiones de software identificables y CVEs conocidos (NVD)")
    detections.extend(_check_known_vulnerable_versions(root_headers, root_body))

    _progress("Buscando secretos expuestos, source maps y endpoints en el JavaScript servido")
    js_detections, js_endpoints = _check_js_secrets(base_url, root_body)
    detections.extend(js_detections)
    if js_endpoints:
        _progress(f"{len(js_endpoints)} endpoint(s) de API descubiertos en el JS — revisar manualmente con contexto de autenticación: {', '.join(js_endpoints[:10])}")

    _progress(f"Comprobando configuración TLS de {domain}")
    detections.extend(_check_tls(domain))

    _progress(f"Comprobando redirección forzada a HTTPS en {domain}")
    detections.extend(_check_https_enforcement(domain))

    _progress("Comprobando publicación de security.txt")
    detections.extend(_check_security_txt(base_url))

    _progress(f"Comprobando registros SPF/DMARC de {domain}")
    detections.extend(_check_email_spoofing(domain))

    _progress("Buscando posibles credenciales expuestas en GitHub público")
    detections.extend(_check_github_exposure(domain))

    _progress("Comprobando rutas comunes de exposición accidental")
    detections.extend(_check_sensitive_paths(base_url))

    _progress("Comprobando rutas indexadas históricamente en Wayback Machine")
    detections.extend(_check_wayback_paths(base_url, domain))

    _progress("Comprobando rutas señaladas en robots.txt y sitemap.xml")
    detections.extend(_check_robots_sitemap_paths(base_url))

    _progress("Comprobando configuración de CORS")
    detections.extend(_check_cors(base_url))

    _progress("Comprobando redirecciones abiertas en parámetros comunes")
    detections.extend(_check_open_redirect(base_url))

    _progress("Buscando subdominios en registros públicos de certificados (crt.sh)")
    subdomains = discover_subdomains(domain)
    checked_hosts = [primary_host]
    if subdomains:
        _progress(f"{len(subdomains)} subdominio(s) encontrados en crt.sh — revisar si deben añadirse a Scope")
        _progress("Comprobando posible toma de control de subdominios descubiertos")
        detections.extend(_check_subdomain_takeover(subdomains))

        full_scan_subs = subdomains[:_MAX_SUBDOMAINS_FULL_SCAN]
        checked_hosts.extend(full_scan_subs)
        _progress(f"Ejecutando batería completa de comprobaciones en {len(full_scan_subs)} subdominio(s): {', '.join(full_scan_subs)}")
        for sub in full_scan_subs:
            sub_detections = _run_subdomain_check_suite(sub)
            if sub_detections:
                _progress(f"{len(sub_detections)} hallazgo(s) en {sub}")
            detections.extend(sub_detections)

    _progress(f"Redactando reportes ({len(detections)} hallazgo(s) detectado(s))")
    findings = []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for d in detections:
        draft = _draft_finding_text(d, target_name)
        findings.append({
            "id":             uuid.uuid4().hex[:12],
            "target":         target_name,
            "title":          d["title"],
            "severity":       d["severity"],
            "status":         "nuevo",
            "summary":        draft["description"][:220],
            "description":    draft["description"],
            "repro_steps":    [d["evidence"]],
            "impact":         draft["impact"],
            "fix_suggestion": draft["fix_suggestion"],
            "discovered_at":  now,
            # See core.bughunter_routes._run_scan_thread's auto-resolve
            # step: only findings from checks that reliably re-verify live
            # state every scan (not third-party search/discovery-source
            # dependent ones — see "no_auto_resolve" tagging above) opt in.
            "auto_resolvable": not d.get("no_auto_resolve", False),
        })
    return findings, subdomains, checked_hosts


# ── Program discovery — Auto Mode's "find new candidates" side activity,
#    separate from run_scan() above (which only ever touches a target
#    already in Scope). Reuses LIRA's existing web-search infra rather than
#    scraping each platform's directory app directly (several are
#    JS-rendered SPAs a plain GET wouldn't yield anything useful from, and
#    Serper's search index already has the individual program pages
#    indexed). Results are NEVER added to Scope automatically — this is a
#    discovery aid, callers must surface suggestions for Joan to review and
#    manually promote. See the "Bug Hunter Constraints" memory. ───────────

_DISCOVERY_QUERIES = [
    ("HackerOne",  'site:hackerone.com "bug bounty" -site:hackerone.com/directory -site:hackerone.com/blog -site:hackerone.com/resources'),
    ("Bugcrowd",   'site:bugcrowd.com "bug bounty" -site:bugcrowd.com/programs -site:bugcrowd.com/blog'),
    ("Intigriti",  'site:intigriti.com "bug bounty" OR "vulnerability disclosure"'),
    ("YesWeHack",  'site:yeswehack.com programs "bug bounty"'),
]

# Search results whose URL path contains any of these are the platform's
# own generic pages (directory listing, blog post, docs...), not an
# individual program page — filtered out rather than surfaced as a
# "candidate program" suggestion.
#
# "bug-bounty-list"/"bug-bounty-programs"/"product/bug-bounty-program"
# found live 2026-08-18 (Bugcrowd's own list page, Intigriti's own list
# page, YesWeHack's own marketing page all slipped through as
# "suggestions" that could never actually become a real Scope target).
# Deliberately NOT the bare substring "bug-bounty-program" (singular) —
# several real individual YesWeHack program slugs end in exactly that
# (e.g. "bind-bug-bounty-program", "doctolib-public-bug-bounty-program"),
# so that would silently exclude genuine candidates too. Each keyword here
# is specific enough to hit only the platform's own directory/marketing
# page, not an individual program's URL.
_SUGGESTION_EXCLUDE_PATH_KEYWORDS = [
    "directory", "blog", "resources", "about", "login", "signin", "signup",
    "docs", "api", "support", "terms", "privacy", "careers", "press",
    "help", "faq", "pricing", "contact",
    "bug-bounty-list", "bug-bounty-programs", "product/bug-bounty-program",
]
# Bugcrowd's own bare listing page is exactly this path (no further
# segment) — "engagements/<company>" (an individual program, e.g.
# "engagements/lastpass") must NOT be caught by a substring match on
# "engagements", so this is checked as an exact match, separately below.
_SUGGESTION_EXCLUDE_EXACT_PATHS = {"engagements"}


def discover_program_suggestions(existing_urls: set, max_results: int = 5) -> list[dict]:
    """Searches for candidate bug-bounty program pages on the platforms
    listed in ui/js/bughunter.js's BH_KNOWN_PROGRAMS, via
    core.tools_search.search_web() (Serper primary, DuckDuckGo fallback —
    same infra core.sleep_curiosity_search already uses). existing_urls is
    the set of URLs already known (pending or previously dismissed —
    caller's job to include both) so the same program doesn't get
    re-suggested every discovery tick.

    Heuristic, not verified: a result passing the path-keyword filter is
    LIKELY an individual program page, not guaranteed. That's fine — a
    human reviews every suggestion before it ever becomes a real Scope
    entry, so noise here costs a moment of Joan's attention, not a scan
    against something unauthorized. Never raises; a failed search for one
    platform just contributes nothing for that platform.
    """
    from core import tools_search

    suggestions = []
    for platform, query in _DISCOVERY_QUERIES:
        if len(suggestions) >= max_results:
            break
        try:
            hits = tools_search.search_web(query)
        except Exception:
            hits = []
        for hit in hits:
            url = (hit.get("url") or "").strip()
            if not url or url in existing_urls:
                continue
            path = urllib.parse.urlparse(url).path.strip("/").lower()
            if not path or path in _SUGGESTION_EXCLUDE_EXACT_PATHS or any(kw in path for kw in _SUGGESTION_EXCLUDE_PATH_KEYWORDS):
                continue
            suggestions.append({
                "name":     hit.get("title") or path.split("/")[0],
                "platform": platform,
                "url":      url,
                "note":     hit.get("snippet") or "",
            })
            existing_urls.add(url)
            if len(suggestions) >= max_results:
                break
    return suggestions
