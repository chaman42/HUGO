"""Calculator skill — thin LiraSkill wrapper over core.tools.evaluate_math
(safe local expression evaluation, no LLM call; see core/tools_search.py).
No arithmetic logic lives here."""
from skills import LiraSkill
from core import tools


class CalculatorSkill(LiraSkill):
    name = "calculator"
    description = "Evalúa expresiones aritméticas localmente, sin llamar al LLM."
    triggers = ["cuánto es", "calcula", "suma", "resta", "multiplica", "divide"]

    def execute(self, query: str, context: dict) -> str:
        result = tools.evaluate_math(query)
        return result if result is not None else "No reconocí una expresión matemática."
