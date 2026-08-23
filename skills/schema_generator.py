"""Schema/mind-map generation skill — thin HugoSkill wrapper over
core.commands.generate_schema (ESTUDIO -> ESQUEMAS; makes its own Groq
call and persists the result). commands imported lazily inside execute(),
not at module scope, so importing this skill at loader-startup doesn't
also pull in core.commands' full dependency graph (voice/session/etc.) —
same lazy-import convention used throughout core/ to sidestep circular
imports (see e.g. core/intent.py's module comment).

`context` may carry {"schema_type": "outline" | "mapa conceptual" |
"estructura", "conversation_context": str}, mirroring generate_schema()'s
own parameters."""
from skills import HugoSkill


class SchemaGeneratorSkill(HugoSkill):
    name = "schema_generator"
    flag = "skill_schemas"
    description = "Genera esquemas y mapas conceptuales para ESTUDIO -> ESQUEMAS."
    triggers = ["mapa conceptual de", "estructura", "haz un esquema de"]

    def execute(self, query: str, context: dict) -> str:
        from core import commands
        context = context or {}
        return commands.generate_schema(
            query,
            context=context.get("conversation_context"),
            schema_type=context.get("schema_type", "outline"),
        )
