from skills import HugoSkill

class lanzar_moneda(HugoSkill):
    name = "lanzar_moneda"
    description = "lanzar una moneda"
    triggers = ["lanzar moneda", "tira una moneda"]

    def execute(self, query: str, context: dict) -> str:
        try:
            import random
        except ImportError:
            return "Lo siento, la biblioteca estándar 'random' no está instalada."

        result = random.choice(["Cara", "Cruz"])
        return f"La moneda ha salido {result}."