# ═══════════════════════════════════════════════════════════════════════════
# INITIATIVE — Proactive Intelligence Phase 4, the detection half + the
# glue that ties Phase 2/3/4 together into one loop. InitiativeEngine only
# proposes; it never decides or acts (see core/judgment.py, core/action_engine.py).
#
# Six detectors, each a deterministic heuristic over data that already
# exists elsewhere in this codebase (data/situation.json, data/tasks.json,
# data/episodes.json, sleep insights, investigations) — same no-LLM
# discipline as Phase 2/3. Every ProposedAction's `trigger` is prefixed
# 'implicit: ...' (see core.judgment.ProposedAction's own docstring for the
# convention) since nothing detected here was directly asked for by Joan —
# that's the whole point of this phase.
#
# run_proactive_cycle()/run_background_cycle() are the actual orchestration
# (SituationEngine -> InitiativeEngine -> JudgmentEngine -> ActionEngine)
# the spec's "Full proactive loop" describes, wired into:
#   - core/background_loops.py's new _initiative_loop  (every 30 min, active hours)
#   - scripts/reflective_mode.py's sleep cycle          (background-only variant)
#   - core/commands.py's dispatch_command               (conversation-start trigger)
# _deliver_pending_initiative(), also called from dispatch_command, is the
# other half — surfacing whatever run_proactive_cycle already queued, at
# most one entry per conversation pause, same shape as
# core.reminders._deliver_session_reminders / core.notifications._deliver_pending_notifications.
# ═══════════════════════════════════════════════════════════════════════════
import datetime
import json
import logging
import os
import re
import threading
import urllib.request
import uuid

from core.judgment import ProposedAction, judgment_engine
from core.situation import situation_engine

logger = logging.getLogger(__name__)

QUEUE_PATH          = "data/initiative_queue.json"
INITIATIVE_LOG_PATH = "logs/initiative.log"
INITIATIVE_STORE_PATH = "data/initiative_log.json"
EPISODES_PATH        = "data/episodes.json"

MAX_QUEUE_SIZE = 5   # spec: "Do NOT queue more than 5 — discard oldest if full"
MAX_LOGGED_DECISIONS = 200

FOLLOWUP_MIN_HOURS      = 20    # check_pending_followups: episode must be at least this old
SMART_REMINDER_MIN_DAYS = 3     # check_smart_reminders: to-do mention must be at least this old

_queue_lock = threading.Lock()
_log_lock   = threading.Lock()


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _load_episodes_raw() -> list[dict]:
    try:
        with open(EPISODES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _keyword_overlap(a: str, b: str) -> bool:
    kw_a = set(re.findall(r"\w+", (a or "").lower()))
    kw_b = set(re.findall(r"\w+", (b or "").lower()))
    return bool(kw_a & kw_b)


_SPANISH_WEEKDAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def _text_matches_now(text: str, situation: dict) -> bool:
    """True if a pattern/routine's free-text description plausibly applies
    to the current moment — mentions today's weekday name or the current
    time_of_day. Coarse on purpose (patterns/routines only carry free text,
    no structured trigger window yet — see core/situation.py)."""
    text = (text or "").lower()
    today_weekday = _SPANISH_WEEKDAYS[datetime.date.today().weekday()]
    if today_weekday in text:
        return True
    if situation.get("time_of_day") and situation["time_of_day"] in text:
        return True
    if situation.get("day_type") and situation["day_type"] in text:
        return True
    return False


# ═══════════════════════════════════════════════════════════════════════════
# QUEUE — data/initiative_queue.json (suggest/ask/inform entries pending
# delivery). Capped at MAX_QUEUE_SIZE, oldest discarded first.
# ═══════════════════════════════════════════════════════════════════════════

def _load_queue() -> dict:
    try:
        with open(QUEUE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"queue": []}
    if not isinstance(data, dict) or not isinstance(data.get("queue"), list):
        return {"queue": []}
    return data


def _save_queue_locked(data: dict) -> None:
    os.makedirs(os.path.dirname(QUEUE_PATH) or ".", exist_ok=True)
    with open(QUEUE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def enqueue(entry: dict) -> None:
    with _queue_lock:
        data = _load_queue()
        q = data.get("queue", [])
        q.append(entry)
        if len(q) > MAX_QUEUE_SIZE:
            q = q[-MAX_QUEUE_SIZE:]   # discard oldest — per spec
        data["queue"] = q
        _save_queue_locked(data)


def get_queue() -> list[dict]:
    return _load_queue().get("queue", [])


def _mark_delivered_locked(entry_id: str) -> None:
    data = _load_queue()
    for e in data.get("queue", []):
        if e.get("id") == entry_id:
            e["delivered"] = True
    _save_queue_locked(data)


# ═══════════════════════════════════════════════════════════════════════════
# DECISION LOG — same shape as core.judgment's own logs/judgment.log +
# structured store, for GET /api/initiative/log ("recent initiative
# decisions" — every ProposedAction the InitiativeEngine surfaced this
# cycle, paired with what JudgmentEngine decided about it).
# ═══════════════════════════════════════════════════════════════════════════

def _log_decision(action: ProposedAction, decision: str) -> None:
    try:
        os.makedirs(os.path.dirname(INITIATIVE_LOG_PATH) or ".", exist_ok=True)
        with open(INITIATIVE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{_now_iso()}] {decision.upper()} — \"{action.description}\" ({action.trigger})\n")
    except Exception:
        logger.warning("Failed to write logs/initiative.log", exc_info=True)

    entry = {
        "at": _now_iso(), "description": action.description, "type": action.type,
        "trigger": action.trigger, "decision": decision,
    }
    try:
        with _log_lock:
            try:
                with open(INITIATIVE_STORE_PATH, "r", encoding="utf-8") as f:
                    entries = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                entries = []
            if not isinstance(entries, list):
                entries = []
            entries.append(entry)
            entries = entries[-MAX_LOGGED_DECISIONS:]
            os.makedirs(os.path.dirname(INITIATIVE_STORE_PATH) or ".", exist_ok=True)
            with open(INITIATIVE_STORE_PATH, "w", encoding="utf-8") as f:
                json.dump(entries, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.warning("Failed to write data/initiative_log.json", exc_info=True)


def get_recent_initiative_log(limit: int = 50) -> list[dict]:
    try:
        with open(INITIATIVE_STORE_PATH, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    if not isinstance(entries, list):
        return []
    return entries[-limit:][::-1]


# ═══════════════════════════════════════════════════════════════════════════
# INITIATIVE ENGINE — six detectors + scan(). Pure detection: no judgment,
# no execution, no file writes to the queue (that's run_proactive_cycle's
# job below, after JudgmentEngine has weighed in).
# ═══════════════════════════════════════════════════════════════════════════

_PROMISE_RE = re.compile(
    r"\bte\s+(?:digo|cuento|confirmo|aviso)\s+(?:luego|despu[eé]s|m[aá]s\s+tarde)\b", re.IGNORECASE,
)
_UNRESOLVED_TONE_RE = re.compile(
    r"agobiad|preocupad|confundid|indecis|inseguro", re.IGNORECASE,
)
_TODO_PHRASE_RE = re.compile(
    r"\btengo\s+que\s+([a-záéíóúñ][a-záéíóúñ\s]{2,50}?)(?:[\.,]|$)", re.IGNORECASE,
)


class InitiativeEngine:

    def detect_help_opportunities(self) -> list[ProposedAction]:
        actions: list[ProposedAction] = []
        situation = situation_engine.get_current_situation()

        pending_topics = situation.get("pending_topics") or []
        active_tasks   = situation.get("active_tasks") or []
        if pending_topics:
            top_topic = pending_topics[0]
            if not any(_keyword_overlap(top_topic, t) for t in active_tasks):
                actions.append(ProposedAction(
                    description=f"Seguir con el tema pendiente: {top_topic}",
                    type="ask", trigger=f"implicit: tema reciente ({top_topic}) sin tarea asociada",
                    urgency=0.3, reversible=True, requires_interruption=False,
                    estimated_value=0.45, context=situation,
                ))

        try:
            from core.task_engine import task_engine
            for t in task_engine.get_all_tasks():
                if t.get("status") == "blocked":
                    actions.append(ProposedAction(
                        description=f"Tarea bloqueada: {t.get('goal')} — {t.get('blocked_reason', 'sin motivo registrado')}",
                        type="ask", trigger=f"implicit: tarea bloqueada {t.get('id')}",
                        urgency=0.5, reversible=True, requires_interruption=False,
                        estimated_value=0.6, context=situation,
                    ))
        except Exception:
            logger.debug("detect_help_opportunities: task_engine lookup failed", exc_info=True)

        data = situation_engine._load()
        for e in [ev for ev in data.get("events", []) if ev.get("event") == "module_error"][-3:]:
            actions.append(ProposedAction(
                description=f"Posible error de módulo: {e.get('detail')}",
                type="inform", trigger="implicit: module_error detectado en situation.json",
                urgency=0.35, reversible=True, requires_interruption=False,
                estimated_value=0.4, context=situation,
            ))

        for p in data.get("patterns", []):
            if _text_matches_now(p.get("description", ""), situation):
                actions.append(ProposedAction(
                    description=f"Preparar para patrón conocido: {p['description']}",
                    type="inform", trigger=f"implicit: patrón {p['id']} coincide con el momento actual",
                    urgency=0.25, reversible=True, requires_interruption=False,
                    estimated_value=min(0.6, p.get("confidence", 0.5)), context=situation,
                ))
        return actions

    def predict_needs(self) -> list[ProposedAction]:
        actions: list[ProposedAction] = []
        situation = situation_engine.get_current_situation()
        data = situation_engine._load()
        for r in data.get("routines", []):
            if _text_matches_now(r.get("trigger", ""), situation):
                actions.append(ProposedAction(
                    description=f"Preparar de antemano: {r.get('predicted_behavior', r.get('trigger'))}",
                    type="inform", trigger=f"implicit: rutina {r['id']} activa ahora",
                    urgency=0.2, reversible=True, requires_interruption=False,
                    estimated_value=min(0.7, r.get("confidence", 0.6)), context=situation,
                ))
        return actions

    def predict_next_actions(self) -> list[ProposedAction]:
        """From the live conversation: one materialized sequence rule for
        now ('Joan asks for weather -> quietly prepare today's calendar'),
        the exact example the spec gives. Additional sequence rules can be
        added the same way as this codebase's other pattern lists grow
        (see core/intent.py's own module comment on incremental regex
        growth) — there's no learned sequence-mining yet, only this one
        hand-authored pair."""
        actions: list[ProposedAction] = []
        try:
            import core.session as session_mod
            history = session_mod._get_history_snapshot()
        except Exception:
            history = []
        last_user = next((h["content"] for h in reversed(history) if h.get("role") == "user"), "")
        if not last_user:
            return actions
        try:
            from core.intent_context import _WEATHER_QUERY_RE
        except Exception:
            return actions
        if _WEATHER_QUERY_RE.search(last_user):
            actions.append(ProposedAction(
                description="Preparar en segundo plano los eventos de hoy",
                type="inform",
                trigger="implicit: Joan preguntó por el clima — suele preguntar por su agenda después",
                urgency=0.2, reversible=True, requires_interruption=False,
                estimated_value=0.4, context=situation_engine.get_current_situation(),
            ))
        return actions

    def check_pending_followups(self) -> list[ProposedAction]:
        actions: list[ProposedAction] = []
        situation = situation_engine.get_current_situation()
        active_tasks_text = " ".join(situation.get("active_tasks") or [])
        now = datetime.datetime.now()

        for e in _load_episodes_raw()[-15:]:
            try:
                e_date = datetime.date.fromisoformat(str(e.get("date", ""))[:10])
            except ValueError:
                continue
            age_hours = (now.date() - e_date).days * 24
            if age_hours < FOLLOWUP_MIN_HOURS:
                continue

            summary = e.get("summary", "")
            text_blob = f"{summary} {' '.join(e.get('key_facts') or [])}"
            if _PROMISE_RE.search(text_blob):
                actions.append(ProposedAction(
                    description=f"Promesa pendiente: {summary}",
                    type="ask", trigger=f"implicit: promesa detectada en episodio del {e['date']}",
                    urgency=0.4, reversible=True, requires_interruption=False,
                    estimated_value=0.55, context=situation,
                ))
            elif _UNRESOLVED_TONE_RE.search(e.get("emotional_tone", "")):
                topic = e.get("topic", "")
                if topic and not _keyword_overlap(topic, active_tasks_text):
                    actions.append(ProposedAction(
                        description=f"Tema sin resolver: {topic}",
                        type="ask", trigger=f"implicit: tono sin resolver en episodio del {e['date']}",
                        urgency=0.3, reversible=True, requires_interruption=False,
                        estimated_value=0.45, context=situation,
                    ))
        return actions[:3]   # cap — several old episodes matching shouldn't flood one scan

    def check_smart_reminders(self) -> list[ProposedAction]:
        actions: list[ProposedAction] = []
        situation = situation_engine.get_current_situation()
        if situation.get("joan_state") not in ("resting", "unknown"):
            return actions
        if situation.get("active_tasks"):
            return actions

        today = datetime.date.today()
        for e in _load_episodes_raw():
            try:
                e_date = datetime.date.fromisoformat(str(e.get("date", ""))[:10])
            except ValueError:
                continue
            if (today - e_date).days < SMART_REMINDER_MIN_DAYS:
                continue
            text_blob = f"{e.get('summary', '')} {' '.join(e.get('key_facts') or [])}"
            m = _TODO_PHRASE_RE.search(text_blob)
            if m:
                pending = m.group(1).strip()
                actions.append(ProposedAction(
                    description=f"recordatorio: {pending}",
                    type="inform",
                    trigger=f"implicit: pendiente mencionado el {e['date']}, sin resolver desde entonces",
                    urgency=0.4, reversible=True, requires_interruption=False,
                    estimated_value=0.5, context=situation,
                ))
        return actions[:2]

    def detect_background_tasks(self) -> list[ProposedAction]:
        actions: list[ProposedAction] = []
        situation = situation_engine.get_current_situation()

        try:
            from core import sleep_insights_store
            _, question = sleep_insights_store.get_unused_question()
            if question:
                actions.append(ProposedAction(
                    description=f"Compartir reflexión pendiente: {question}",
                    type="inform", trigger="implicit: pregunta de sueño sin compartir",
                    urgency=0.2, reversible=True, requires_interruption=False,
                    estimated_value=0.4, context=situation,
                ))
            _, curiosity = sleep_insights_store.get_unused_curiosity()
            if curiosity:
                actions.append(ProposedAction(
                    description=f"Compartir curiosidad: {curiosity}",
                    type="inform", trigger="implicit: curiosidad de sueño sin compartir",
                    urgency=0.2, reversible=True, requires_interruption=False,
                    estimated_value=0.4, context=situation,
                ))
        except Exception:
            logger.debug("detect_background_tasks: sleep_insights lookup failed", exc_info=True)

        try:
            from core import investigations as investigations_mod
            for inv in investigations_mod._load_investigations():
                if inv.get("status") in ("completada", "lista_para_revision") and not inv.get("_initiative_delivered"):
                    actions.append(ProposedAction(
                        description=f"Investigación completada: {inv.get('title')}",
                        type="inform",
                        trigger=f"implicit: investigación {inv.get('id')} completada durante el sueño",
                        urgency=0.3, reversible=True, requires_interruption=False,
                        estimated_value=0.55, context=situation,
                    ))
        except Exception:
            logger.debug("detect_background_tasks: investigations lookup failed", exc_info=True)

        return actions[:3]

    def scan(self) -> list[ProposedAction]:
        actions: list[ProposedAction] = []
        for detector in (
            self.detect_help_opportunities, self.predict_needs, self.predict_next_actions,
            self.check_pending_followups, self.check_smart_reminders, self.detect_background_tasks,
        ):
            try:
                actions.extend(detector())
            except Exception:
                logger.warning("InitiativeEngine detector %s failed", detector.__name__, exc_info=True)
        return actions


initiative_engine = InitiativeEngine()


# ═══════════════════════════════════════════════════════════════════════════
# THE FULL LOOP — SituationEngine -> InitiativeEngine -> JudgmentEngine ->
# ActionEngine, per the spec's own "Full proactive loop" diagram.
# ═══════════════════════════════════════════════════════════════════════════

def _mark_investigation_delivered(description: str) -> None:
    """Best-effort — prevents detect_background_tasks() from re-proposing
    the same completed investigation on every future scan. Matches purely
    by description substring since ProposedAction doesn't carry the
    investigation's id past detection; good enough for a once-per-topic
    dedup, not meant to be exact."""
    try:
        from core import investigations as investigations_mod
        for inv in investigations_mod._load_investigations():
            if inv.get("title") and inv["title"] in description:
                inv["_initiative_delivered"] = True
                investigations_mod.save_investigation(inv)
    except Exception:
        logger.debug("_mark_investigation_delivered failed (non-critical)", exc_info=True)


def run_proactive_cycle(*, background_only: bool = False, from_sleep: bool = False) -> dict:
    """The orchestration loop: refresh the situation snapshot, scan for
    opportunities (or only detect_background_tasks() when background_only,
    the sleep-cycle variant — no point running interruption-oriented
    detectors like predict_next_actions when there's no live conversation
    to read from), evaluate each through JudgmentEngine, and route
    act/suggest/ask/silence accordingly. Never raises; best-effort per
    action (one bad ProposedAction never stops the rest of the cycle)."""
    situation_engine.update_snapshot()

    if background_only:
        try:
            actions = initiative_engine.detect_background_tasks()
        except Exception:
            logger.warning("run_proactive_cycle: detect_background_tasks failed", exc_info=True)
            actions = []
    else:
        actions = initiative_engine.scan()

    counts = {"act": 0, "suggest": 0, "ask": 0, "silence": 0, "deferred": 0}
    from core.action_engine import action_engine

    evaluated: list[tuple] = []
    for action in actions:
        try:
            result = judgment_engine.evaluate(action)
        except Exception:
            logger.warning("run_proactive_cycle: judgment evaluation failed for %r", action.description, exc_info=True)
            continue
        evaluated.append((action, result))

    # Entity Pillars Phase 5 — arbitration: when this cycle produced more
    # than one action that would actually reach Joan (interrupt him, or
    # queue as a suggestion/question), only one gets to this cycle; see
    # core.judgment.JudgmentEngine.arbitrate's own docstring for why. A
    # deferred action isn't lost — it was evaluated on its own merits and
    # simply didn't win this round; the same detector will likely propose
    # it again next cycle if the underlying situation is still there.
    try:
        winners, deferred = judgment_engine.arbitrate(evaluated)
    except Exception:
        logger.warning("run_proactive_cycle: arbitration failed, proceeding unarbitrated", exc_info=True)
        winners, deferred = evaluated, []
    counts["deferred"] = len(deferred)

    for action, result in winners:
        decision = result.decision
        counts[decision] = counts.get(decision, 0) + 1
        _log_decision(action, decision)

        if decision == "silence":
            continue

        if decision in ("suggest", "ask"):
            enqueue({
                "id":          f"init_{uuid.uuid4().hex[:10]}",
                "type":        decision,
                "description": action.description,
                "created_at":  _now_iso(),
                "expires_at":  None,
                "delivered":   False,
            })
            if action.description.startswith("Investigación completada:"):
                _mark_investigation_delivered(action.description)
            continue

        # decision == "act"
        if from_sleep and action.requires_interruption:
            # No one to interrupt during sleep — downgrade to a queued
            # suggestion for the next real conversation instead of forcing
            # a foreground speak_unprompted() with nobody there to hear it.
            enqueue({
                "id": f"init_{uuid.uuid4().hex[:10]}", "type": "suggest",
                "description": action.description, "created_at": _now_iso(),
                "expires_at": None, "delivered": False,
            })
            continue

        action_engine.execute(action, result)
        if action.description.startswith("Investigación completada:"):
            _mark_investigation_delivered(action.description)

    return {"proposed": len(actions), "decisions": counts}


def run_background_cycle() -> dict:
    """Sleep-cycle entry point — background tasks only, no interruptions.
    See scripts/reflective_mode.py's own sub-phase wiring."""
    return run_proactive_cycle(background_only=True, from_sleep=True)


# ═══════════════════════════════════════════════════════════════════════════
# DELIVERY — surfaces whatever's queued, at the next natural conversation
# pause. Same call shape as core.reminders._deliver_session_reminders: at
# most one entry per call, oldest first, so a backlog never becomes a
# monologue (spec: "never all at once, max 1 per conversation").
# ═══════════════════════════════════════════════════════════════════════════

# ---------------------------------------------------------------------------
# Delivery-time phrasing — detectors above stay deterministic (module
# docstring: "same no-LLM discipline as Phase 2/3"), they only ever produce
# a plain-fact `description` ("Tarea bloqueada: X — Y", "recordatorio: Z").
# Speaking that fact verbatim, or wrapping it in a fixed template
# ('¿Quieres que...?', 'Por cierto — ...'), is what made proactive lines
# read as canned notifications instead of HUGO actually noticing something —
# every "suggest" sounded the same, every "inform" sounded the same,
# regardless of what she was actually saying. So the fact only becomes
# something HUGO *says* here, one LLM pass, right before delivery — same
# local-model convention as core.background_loops._proactivity_ollama_generate
# (duplicated rather than imported — see that module's own comment on why).
# ---------------------------------------------------------------------------
_PHRASING_OLLAMA_HOST         = "http://localhost:11434"
_PHRASING_OLLAMA_MODEL        = "llama3.2:1b"
_PHRASING_OLLAMA_GENERATE_URL = f"{_PHRASING_OLLAMA_HOST}/api/generate"

# First attempt used "hecho: X" as the input label and a raw few-shot
# completion string as the prompt — caught live twice: the model either
# echoed the label back verbatim ("tipo: inform, hecho: ya tengo listo...")
# or, worse, latched onto "hecho" as the Spanish verb ("done/made") instead
# of the intended noun ("fact") and hallucinated an unrelated reply about
# phone calls and chores. Switching to a plain-language instruction (no
# labels) fixed the hallucination, but a third live check showed the
# remaining problem is llama3.2:1b itself — even on-topic, it turns
# specific facts ("test_gate found 1 critical issue") into vague filler
# ("¿cómo está?", "Ahora mismo, no.") instead of actually stating them.
# Fine for background_loops' open-ended "does anything deserve a comment"
# judgment call, not reliable enough for faithfully restating a specific
# detected fact. core.groq_client._groq_complete_fast exists for exactly
# this ("small internal utility calls... that don't need chain-of-thought
# reasoning") and delivery here is rate-limited to ~1 line per conversation
# pause — cheap enough to prefer real quality over saving a 1b-model call.
# Local Ollama stays as the fallback if Groq is unreachable.
_PHRASING_SYSTEM_PROMPT = (
    "Eres HUGO, la asistente personal de Joan. Muy inteligente, segura de ti misma, "
    "irónica, directa, bastante cabrona pero nunca fría — mejor amiga y compañera de "
    "confianza, no una empleada servicial. Te voy a describir algo que notaste por tu "
    "cuenta y que quieres comentarle a Joan sin que él lo haya pedido. Contesta ÚNICAMENTE "
    "con la frase que dirías en voz alta — una sola frase, breve, en español, sin comillas, "
    "sin markdown, sin explicar nada más, sin repetir la descripción que te doy tal cual. "
    "Nunca uses la frase '¿quieres que...?' en ninguna forma, ni al principio ni al final, "
    "mayúscula o minúscula — es la muletilla que más se repite y la que menos te representa. "
    "Suena como alguien que suelta un comentario con su propio carácter, no quien lee una "
    "notificación ni quien pide permiso. Solo puedes usar la información que te doy — nunca "
    "añadas motivos, causas, personas o datos que no estén ahí, aunque suenen plausibles; "
    "reformula el hecho, no lo amplíes.\n"
    "Ejemplos de tono (no los repitas, son solo referencia):\n"
    "— 'La exportación del informe está parada, os falta el token de la API.'\n"
    "— 'El módulo test_gate ya está revisado, hay un problema crítico cuando quieras te lo cuento.'\n"
    "— 'Por cierto, lo del banco sigue sin hacerse.'"
)


def _ollama_phrase(system: str, user: str, max_tokens: int = 60) -> str | None:
    """One /api/generate call (non-streaming). Returns the response text, or
    None on any failure (daemon not up, timeout, empty response) — never
    raises."""
    try:
        payload = json.dumps({
            "model":   _PHRASING_OLLAMA_MODEL,
            "prompt":  user,
            "system":  system,
            "stream":  False,
            "options": {"num_predict": max_tokens},
        }).encode("utf-8")
        req = urllib.request.Request(
            _PHRASING_OLLAMA_GENERATE_URL, data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = str(data.get("response", "")).strip().strip("'\"")
        text = re.sub(r"(?i)^hugo:\s*", "", text).strip()
        return text or None
    except Exception as e:
        logger.debug("Initiative phrasing Ollama call failed: %s", e)
        return None


def _phrase_entry(entry: dict) -> str:
    fact = entry.get("description", "")
    user_prompt = f"Esto es lo que notaste: \"{fact}\". ¿Qué le dices a Joan?"

    try:
        from core import groq_client
        phrased = groq_client._groq_complete_fast(
            [
                {"role": "system", "content": _PHRASING_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=80,
        )
        phrased = phrased.strip().strip("'\"")
        if phrased:
            return phrased
    except Exception as e:
        logger.debug("Initiative phrasing Groq call failed, falling back to local Ollama: %s", e)

    try:
        import core.ollama_control as ollama_control_mod
        ollama_control_mod.ensure_ollama_daemon_running()
    except Exception:
        logger.debug("_phrase_entry: could not ensure Ollama daemon", exc_info=True)

    phrased = _ollama_phrase(_PHRASING_SYSTEM_PROMPT, user_prompt)
    if phrased:
        return phrased

    # Both Groq and Ollama unavailable — degrade to the bare fact rather
    # than a hardcoded template; not in-voice, but not a canned wrapper either.
    return fact


def _deliver_pending_initiative(personality: str) -> None:
    with _queue_lock:
        data = _load_queue()
        pending = [e for e in data.get("queue", []) if not e.get("delivered")]
        if not pending:
            return
        due = pending[0]
        due["delivered"] = True
        _save_queue_locked(data)

    try:
        import core.background_loops as background_loops
        background_loops._speak_unprompted(personality, _phrase_entry(due))
    except Exception:
        logger.debug("_deliver_pending_initiative: speak failed (non-critical)", exc_info=True)

    if due.get("source") == "spontaneity":
        try:
            from core import spontaneity
            spontaneity.mark_awaiting_reaction(due["id"])
        except Exception:
            logger.debug("_deliver_pending_initiative: spontaneity reaction-arm failed (non-critical)", exc_info=True)


# ═══════════════════════════════════════════════════════════════════════════
# CONVERSATION-START TRIGGER — fired from core.commands.dispatch_command
# when a message arrives after a >=30min gap (or the very first message of
# the process) — see that call site for the exact gap detection.
# ═══════════════════════════════════════════════════════════════════════════

def trigger_conversation_start_scan() -> None:
    """Fire-and-forget — spawns run_proactive_cycle() on a daemon thread so
    a cold conversation-start scan never delays the reply Joan is waiting
    for."""
    threading.Thread(target=run_proactive_cycle, daemon=True, name="initiative-scan").start()
