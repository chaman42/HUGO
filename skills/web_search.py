"""Web search skill — thin HugoSkill wrapper over core.tools.search_web /
format_search_results (Serper.dev primary, DuckDuckGo Instant Answer API
fallback; see core/tools_search.py). No search logic lives here."""
from skills import HugoSkill
from core import tools


class WebSearchSkill(HugoSkill):
    name = "web_search"
    description = "Busca información actual en internet (Serper.dev / DuckDuckGo)."
    triggers = ["busca", "búscame", "investiga en internet", "qué está pasando con", "últimas noticias"]

    def execute(self, query: str, context: dict) -> str:
        results = tools.search_web(query)
        return tools.format_search_results(results)
