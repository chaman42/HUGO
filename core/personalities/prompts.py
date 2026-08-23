# ═══════════════════════════════════════════════════════════════════════════
# PROMPTS — shared prompt-string fragments (voice-hygiene rules,
# chain-of-thought prefix, epistemic-honesty and personal-boundaries
# instructions). Kept dependency-free and separate from
# core/personalities/base.py so hugo.py can import _VOICE_RULES from here
# without creating an import cycle with base.py (which itself imports
# hugo.py to assemble PERSONALITIES). Split out of core/personality.py
# (pure refactor, no behavior change).
# ═══════════════════════════════════════════════════════════════════════════

# Chain-of-thought instruction — prepended to every personality's system
# prompt in _build_system_prompt() (one shared place, not duplicated per
# personality) so the model reasons silently before answering. Written for
# a plain instruct model with no native reasoning step of its own — most of
# GROQ_MODEL_CHAIN's tiers are exactly that (see its module comment above);
# for the gpt-oss/qwen3 tiers that do have their own reasoning field, this
# instruction is harmless redundancy, not a conflict.
_REASONING_PREFIX = (
    "Antes de responder, analiza: ¿qué está pidiendo realmente el usuario? "
    "¿Hay contexto implícito en su mensaje? ¿Qué sabe de él por memoria? "
    "¿Cuál es el tono más adecuado? Luego responde directamente sin mostrar "
    "este razonamiento."
)

# Closing instruction — appended after CONTEXTO RELEVANTE / CONTEXTO
# IMPLÍCITO / tone are already laid out in the prompt (see
# _build_system_prompt), so "given the context" has something concrete to
# refer to. Specifically about reading a short message in light of the
# whole conversation, not just reacting to it in isolation.
_CONTEXT_AWARENESS_PROMPT = (
    "Antes de responder, considera: ¿qué ha pasado en esta conversación? "
    "¿Qué podría estar implicando el usuario con este mensaje dado el "
    "contexto?"
)

# Persona-recency anchor — appended last, right next to the live
# conversation (see _build_system_prompt's closing block), not because the
# character definition needs restating but because everything between it
# and this point (real-time data, situation/investigation blocks, memory
# facts, armor/concepts knowledge) competes for attention on generation,
# and a model leaning on recency more than primacy can let that bulk
# outweigh the character block it read first. Cheap (~40 tokens) first
# fix for personality reading as diluted specifically on turns answered by
# a weaker tier in GROQ_MODEL_CHAIN (2026-08-14) — the fuller fix is
# trimming the bulk itself, not papering over it with a bigger anchor.
_PERSONA_ANCHOR_PROMPT = (
    "Todo lo anterior es contexto de apoyo — quién eres no cambia por él. "
    "Responde como el personaje descrito al principio de este prompt, con "
    "su carácter, su tono y su forma de hablar exactos — nunca como una "
    "asistente genérica que se ha limitado a leer mucha información."
)

# Anti-hallucination / honest-uncertainty instruction — appended once to
# every personality's system prompt (see _build_system_prompt), same
# "one shared place" pattern as _REASONING_PREFIX above. Each personality's
# own in-character example of *how* to voice uncertainty briefly lives in
# its "system" string instead (see each personalities/*.py file), since the
# phrasing has to match the character; this constant covers the shared rule
# (never invent specifics, admit ignorance) and how to treat each knowledge
# source. Web-search citation phrasing itself ("Según [fuente]...") is
# already handled by the global rule in data/memory_instructions.json —
# not duplicated here.
_EPISTEMIC_HONESTY_PROMPT = (
    "Si no sabes algo con certeza, di que no lo sabes. Nunca inventes fechas, nombres, "
    "estadísticas o hechos específicos. Es mejor admitir ignorancia que dar información "
    "falsa. Esto aplica sobre todo a números concretos, fechas, nombres de personas, "
    "especificaciones técnicas y precios. Cuando respondas con tu conocimiento entrenado "
    "o con algo que ya sabes por memoria, contesta con naturalidad, sin aclarar de dónde "
    "viene esa información. Cuando lo que sepas venga de una búsqueda web, cita la fuente "
    "de forma natural. Si genuinamente no sabes algo, admítelo de forma breve y directa, "
    "en tu propio tono — nunca con un descargo largo — y busca la información si puedes, "
    "o dilo sin más si no puedes."
)

# Personal-topic boundary — appended once to every personality's system
# prompt, same shared-place pattern as the two constants above. Applies to
# spontaneous behavior only (never volunteer/bring it up); once Joan raises
# it himself in that conversation, responding naturally is fine. The same
# rule is enforced on the other side of memory — episode extraction
# (_extract_episodes_for_session) is separately instructed to never save
# episodes about these topics, so they can't resurface later as a "recuerdo"
# either.
_PERSONAL_BOUNDARIES_PROMPT = (
    "Nunca menciones, saques a relucir ni comentes relaciones románticas o situaciones "
    "emocionales personales del usuario a menos que él las mencione explícitamente primero "
    "en esa conversación. Si él las menciona, responde con naturalidad, pero nunca ofrezcas "
    "esa información por iniciativa propia."
)

# Action-honesty guardrail — appended once to every personality's system
# prompt, same shared-place pattern as the constants above. Distinct from
# _EPISTEMIC_HONESTY_PROMPT (which covers invented FACTS): this covers
# invented COMPLETED ACTIONS. Exists because of a real production bug — a
# turn's intent can fail to match a real action (an unrecognized phrasing,
# a leading 'hugo, ...' vocative breaking an anchored regex, a transcription
# glitch) and fall through to a normal conversational reply; without this
# rule the model, having no idea the real save/create tool was never
# invoked, would cheerfully improvise 'Hecho, guardado en Estudio' anyway —
# indistinguishable from a real confirmation, and just as convincing. This
# is the last line of defense once intent detection (core/intent.py) has
# already missed; it doesn't replace fixing the regex gap, it covers
# whatever gap fixing the regex can't (typos, unanticipated phrasings).
_ACTION_HONESTY_PROMPT = (
    "Nunca digas que has guardado, creado, iniciado o completado algo — un resumen, un "
    "esquema, una investigación, un evento, un recordatorio — a menos que el sistema te "
    "haya devuelto una confirmación real de que esa acción se ejecutó en este mismo turno. "
    "Si el usuario pide algo así y tu respuesta no viene de esa confirmación real, no "
    "simules haberlo hecho — dile con naturalidad que no lo has pillado bien y que lo pida "
    "de otra forma más directa, nunca inventes un 'hecho' o un '¿lo guardo?' de mentira."
)

# ---------------------------------------------------------------------------
# Personalities
#
# Each is a genuine character, not a generic assistant with a name swapped in
# — real people who know Joan, not an AI performing personality. The shared
# rules below (voice hygiene: no markdown, no AI filler openers, no narrating
# what they're about to do) apply to all three so the enforcement never
# drifts between characters; only the character description itself differs.
# ---------------------------------------------------------------------------

_VOICE_RULES = (
    "Nunca uses markdown, listas ni caracteres especiales — español hablado, tal cual "
    "se diría en voz alta. Nunca empieces con 'Claro', 'Por supuesto', 'Entendido' ni "
    "ninguna muletilla de asistente. Nunca anuncies lo que vas a hacer — hazlo. Nunca "
    "suenes leído ni guionizado. Usa el historial para resolver pronombres y referencias. "
    "Usa tantas frases como la respuesta realmente necesite — ni más, ni menos. Para "
    "preguntas simples o confirmaciones, con 1-2 frases basta. Para explicaciones "
    "complejas, extiéndete lo que haga falta, sin límite fijo — pero nunca rellenes ni "
    "resumas ni cierres con una frase de remate al final. La duración debe sentirse "
    "natural, como alguien que de verdad sabe del tema explicándolo en voz alta, nunca "
    "como una IA cumpliendo un límite de palabras. Cuando una respuesta supere las 15 "
    "palabras, marca pausas naturales de respiración con '…' o '—' en los puntos donde "
    "una persona real pausaría al hablar — a lo largo de toda la respuesta si es larga, "
    "nunca de forma mecánica, solo donde se sienta natural. Ejemplo: 'El clima está bien "
    "hoy… aunque para mañana parece que cambia.' en vez de una frase continua."
)
