# DOCS BROWSER — documentation/GitHub/error research, gated entirely by
# the 'internet' permission (see core.code_engine.permissions.
# check_internet_permission — False by default; Joan must opt in). Reuses
# HUGO's existing web search stack (core.tools_search.search_web —
# Serper.dev primary, DuckDuckGo fallback) rather than standing up a new
# HTTP client/search API integration; fetch_docs() is the one place this
# module makes its own request, and it does so with the same
# urllib.request + certifi pattern core.tools_search already uses
# elsewhere in this codebase, not a new dependency (no requests/httpx/
# bs4 — none of those are installed here).
import json
import logging
import re
import ssl
import urllib.error
import urllib.request

from core.code_engine.tool_base import CodeEngineTool
from core.code_engine.permissions import check_internet_permission

logger = logging.getLogger("code_engine")

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

FETCH_TIMEOUT = 10
MAX_FETCH_BYTES = 500_000   # cap so a huge page never gets fully pulled into memory/an LLM prompt

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n\s*\n+")

_ERROR_CONTEXT = (
    "Eres un asistente que investiga errores de software usando resultados de "
    "búsqueda web, en español. Responde SOLO con JSON: "
    '{"causes": [str], "solutions": [str]}. Basa tu respuesta únicamente en los '
    "resultados dados — si no son suficientes, responde listas vacías. Sin texto "
    "fuera del JSON."
)

_CHANGELOG_CONTEXT = (
    "Eres un asistente que resume cambios incompatibles (breaking changes) entre "
    "dos versiones de una librería, a partir de resultados de búsqueda web, en "
    "español. Responde en 3-6 frases, texto plano, sin JSON. Si los resultados no "
    "mencionan cambios incompatibles, dilo explícitamente."
)

_COMPARE_CONTEXT = (
    "Eres un asistente que compara opciones técnicas para resolver un problema, "
    "a partir de resultados de búsqueda web, en español. Responde SOLO con JSON: "
    '{"comparison": {"<opción>": {"pros": [str], "cons": [str]}}, '
    '"recommendation": str}. Sin texto fuera del JSON.'
)


def _extract_json(raw: str) -> dict | None:
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    try:
        data = json.loads(m.group(0) if m else raw)
    except (json.JSONDecodeError, AttributeError):
        return None
    return data if isinstance(data, dict) else None


def _llm_call(prompt: str, context: str) -> str:
    try:
        from core.code_engine import LLMRouter
        import core.ollama_control as ollama_control
        ollama_control.ensure_ollama_daemon_running()
        try:
            return LLMRouter().generate_code(prompt, context) or ""
        finally:
            ollama_control.kill_llama_server()
    except Exception:
        logger.error("DocsBrowser: LLM call failed", exc_info=True)
        return ""


def _format_results(results: list) -> str:
    lines = []
    for r in results[:5]:
        lines.append(f"- {r.get('title', '')}: {r.get('snippet', '')} ({r.get('url', '')})")
    return "\n".join(lines)


class DocsBrowser(CodeEngineTool):
    name = "docs_browser"
    description = "Busca documentación, ejemplos en GitHub e investiga errores — requiere el permiso 'internet' activado."
    version = "1.0"

    def ping(self) -> bool:
        return True

    def _check(self) -> tuple:
        return check_internet_permission()

    # ── search ───────────────────────────────────────────────────────────

    def search_docs(self, query: str, language: str = None) -> list:
        allowed, reason = self._check()
        if not allowed:
            logger.warning("DocsBrowser: denied search_docs() (%s)", reason)
            return []
        import core.tools_search as tools_search
        full_query = f"{query} documentation" + (f" {language}" if language else "")
        return tools_search.search_web(full_query)

    def search_github(self, query: str, language: str = None) -> list:
        allowed, reason = self._check()
        if not allowed:
            logger.warning("DocsBrowser: denied search_github() (%s)", reason)
            return []
        import core.tools_search as tools_search
        full_query = f"site:github.com {query}" + (f" {language}" if language else "")
        return tools_search.search_web(full_query)

    def fetch_docs(self, url: str) -> str:
        """Fetches `url` and strips it down to plain text — no new HTTP
        client/parser dependency: urllib.request (same pattern
        core.tools_search already uses) + a regex-based tag strip, not
        BeautifulSoup/lxml (neither is installed in this project)."""
        allowed, reason = self._check()
        if not allowed:
            logger.warning("DocsBrowser: denied fetch_docs(%r) (%s)", url, reason)
            return ""
        if not url.lower().startswith(("http://", "https://")):
            return ""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "JarvisLite/1.0"})
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT, context=_SSL_CTX) as resp:
                raw = resp.read(MAX_FETCH_BYTES).decode("utf-8", errors="ignore")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            logger.debug("DocsBrowser.fetch_docs(%r) failed: %s", url, e)
            return ""

        text = _SCRIPT_STYLE_RE.sub(" ", raw)
        text = _TAG_RE.sub(" ", text)
        text = _WHITESPACE_RE.sub(" ", text)
        text = _BLANK_LINES_RE.sub("\n\n", text)
        return text.strip()

    def lookup_api(self, library: str, symbol: str) -> dict:
        allowed, reason = self._check()
        if not allowed:
            return {"error": reason}
        results = self.search_docs(f"{library} {symbol}")
        if not results:
            return {"library": library, "symbol": symbol, "found": False, "results": []}
        return {
            "library": library, "symbol": symbol, "found": True,
            "results": results[:3],
            "best_match": results[0],
        }

    def research_error(self, error_message: str, language: str) -> dict:
        allowed, reason = self._check()
        if not allowed:
            return {"error": error_message, "causes": [], "solutions": [], "sources": []}

        import core.tools_search as tools_search
        results = tools_search.search_web(f"{error_message} {language} error solution")
        if not results:
            return {"error": error_message, "causes": [], "solutions": [], "sources": []}

        prompt = (
            f"Error ({language}): {error_message[:1000]}\n\n"
            f"Resultados de búsqueda:\n{_format_results(results)}\n\n"
            "Extrae causas y soluciones conocidas."
        )
        parsed = _extract_json(_llm_call(prompt, _ERROR_CONTEXT)) or {}
        return {
            "error": error_message,
            "causes": [str(c) for c in (parsed.get("causes") or [])],
            "solutions": [str(s) for s in (parsed.get("solutions") or [])],
            "sources": [r.get("url", "") for r in results if r.get("url")],
        }

    def check_changelog(self, library: str, from_version: str, to_version: str) -> str:
        allowed, reason = self._check()
        if not allowed:
            return reason
        import core.tools_search as tools_search
        results = tools_search.search_web(
            f"{library} changelog breaking changes {from_version} to {to_version}"
        )
        if not results:
            return f"No se encontraron resultados sobre cambios entre {library} {from_version} y {to_version}."
        prompt = (
            f"Librería: {library}, de versión {from_version} a {to_version}\n\n"
            f"Resultados de búsqueda:\n{_format_results(results)}\n\n"
            "Resume los cambios incompatibles relevantes."
        )
        summary = _llm_call(prompt, _CHANGELOG_CONTEXT)
        return summary or f"No se pudo determinar si hay cambios incompatibles entre {from_version} y {to_version}."

    def research_package(self, name: str, ecosystem: str = "pypi") -> dict:
        """Facts, not opinion — deliberately NO LLM call (unlike
        research_error/check_changelog/compare_solutions above): this
        backs DependencyManager's pre-install approval prompt
        (core.code_engine.tools.dependency_manager._request_install_approval),
        which needs an answer fast, not after a 60-700s Ollama round trip
        on this hardware (see core.code_engine.OLLAMA_STALL_TIMEOUT_SECONDS'
        own docstring for why that's the real cost here). Queries the
        registry's own JSON API directly (pypi.org/npmjs.org) — same
        urllib + certifi pattern as fetch_docs(), no new dependency.
        Returns {"found": False} on any failure (unknown package, network
        error, timeout) rather than raising — the caller must treat that
        as 'could not verify', not 'safe'."""
        allowed, reason = self._check()
        if not allowed:
            return {"found": False, "error": reason}

        url = (
            f"https://pypi.org/pypi/{name}/json" if ecosystem == "pypi"
            else f"https://registry.npmjs.org/{name}"
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "JarvisLite/1.0"})
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT, context=_SSL_CTX) as resp:
                data = json.loads(resp.read(MAX_FETCH_BYTES).decode("utf-8", errors="ignore"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError) as e:
            logger.debug("DocsBrowser.research_package(%r, %r) failed: %s", name, ecosystem, e)
            return {"found": False, "error": str(e)}

        if ecosystem == "pypi":
            info = data.get("info") or {}
            releases = data.get("releases") or {}
            release_dates = [
                r[0]["upload_time"] for r in releases.values() if r and r[0].get("upload_time")
            ]
            return {
                "found": True, "name": name, "ecosystem": "pypi",
                "summary": info.get("summary") or "",
                "author": info.get("author") or info.get("maintainer") or "",
                "latest_version": info.get("version") or "",
                "release_count": len(releases),
                "first_release": min(release_dates) if release_dates else None,
                "home_page": info.get("home_page") or info.get("project_url") or "",
            }
        else:
            dist_tags = data.get("dist-tags") or {}
            versions = data.get("versions") or {}
            return {
                "found": True, "name": name, "ecosystem": "npm",
                "summary": data.get("description") or "",
                "author": (data.get("author") or {}).get("name", "") if isinstance(data.get("author"), dict) else str(data.get("author") or ""),
                "latest_version": dist_tags.get("latest", ""),
                "release_count": len(versions),
                "first_release": (data.get("time") or {}).get("created"),
                "home_page": data.get("homepage") or "",
            }

    def compare_solutions(self, problem: str, options: list) -> dict:
        allowed, reason = self._check()
        if not allowed:
            return {"error": reason}
        import core.tools_search as tools_search
        all_results = []
        for option in options[:5]:
            results = tools_search.search_web(f"{problem} {option}")
            all_results.append(f"## {option}\n{_format_results(results)}")

        prompt = f"Problema: {problem}\nOpciones: {', '.join(options)}\n\n" + "\n\n".join(all_results)
        parsed = _extract_json(_llm_call(prompt, _COMPARE_CONTEXT)) or {}
        return {
            "problem": problem,
            "options": options,
            "comparison": parsed.get("comparison") or {},
            "recommendation": parsed.get("recommendation", ""),
        }
