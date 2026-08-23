# ═══════════════════════════════════════════════════════════════════════════
# INVESTIGATIONS — investigation lifecycle storage (data/investigations.json).
# Created by core.intent's start_investigation intent (see
# core/actions.py._execute_start_investigation), advanced one reasoning
# cycle per sleep session by core.sleep_phases_incubation's Phase 3
# (🧪 Incubación), and displayed read-only by core/estudio_routes.py
# (ESTUDIO → INVESTIGACIÓN — see ui/js/estudio.js).
#
# Dependency-light (json/os/threading/uuid/datetime only), same discipline
# as core/reminders.py and the Sleep System's own core/sleep_state.py — the
# incubation phase runs inside scripts/reflective_mode.py, which must not
# pull in the audio/TTS stack.
#
# Status lifecycle: activa (just created, no cycle run yet) → incubando
# (mid-reasoning, 1+ cycles run) → lista_para_revision (confidence > 0.85,
# awaiting Joan's review) or completada (cycles_processed hit the cap, or a
# previously non-empty sub_questions list came back empty — "no more gaps
# found"). Both terminal-ish statuses stop drawing further incubation
# cycles — see get_active_investigations().
# ═══════════════════════════════════════════════════════════════════════════
import datetime
import json
import os
import threading
import uuid

INVESTIGATIONS_PATH = "data/investigations.json"

_investigations_lock = threading.Lock()

# Statuses still eligible for an incubation cycle (see get_active_investigations).
ACTIVE_STATUSES = ("activa", "incubando")


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _load_investigations() -> list[dict]:
    try:
        with open(INVESTIGATIONS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _save_investigations(investigations: list[dict]) -> None:
    os.makedirs(os.path.dirname(INVESTIGATIONS_PATH) or ".", exist_ok=True)
    with open(INVESTIGATIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(investigations, f, ensure_ascii=False, indent=2)


def create_investigation(topic: str) -> dict:
    """Starts a new investigation from a user request — 'activa' until the
    Sleep System's Incubación phase picks it up. `topic` is the raw text
    following the trigger phrase ('investiga X', 'quiero saber sobre X',
    'analiza X en profundidad' — see core/intent.py); used verbatim as both
    the display title and the seed question.

    'methodology'/'sources'/'summary'/'conclusions' mirror the existing
    frontend-facing fields core/estudio_routes.py already documents;
    'hypotheses'/'sub_questions'/'cycles_processed'/'id'/'created_at' are
    new, written by the incubation phase (core/sleep_phases_incubation.py)."""
    topic = (topic or "").strip(" ?¡!.")
    if not topic:
        topic = "tema sin especificar"
    with _investigations_lock:
        investigations = _load_investigations()
        inv = {
            "id":               uuid.uuid4().hex[:12],
            "title":            topic[:80],
            "question":         topic,
            "status":           "activa",
            "date":             _now_iso(),
            "created_at":       _now_iso(),
            "summary":          "",
            "methodology":      "Incubación durante ciclos de sueño (Ollama llama3.2:3b).",
            "hypotheses":       [],
            "conclusions":      "",
            "confidence":       0.0,
            "sources":          [],
            "sub_questions":    [],
            "cycles_processed": 0,
        }
        investigations.append(inv)
        _save_investigations(investigations)
    try:
        from core.internal_state import nudge
        nudge("curiosidad", 0.1, f"nueva investigación iniciada: {topic[:60]}")
    except Exception:
        pass
    return inv


def get_active_investigations() -> list[dict]:
    """Investigations still eligible for an incubation cycle (see
    ACTIVE_STATUSES) — excludes 'lista_para_revision' and 'completada',
    both of which are done reasoning autonomously and just waiting on Joan."""
    with _investigations_lock:
        investigations = _load_investigations()
    return [i for i in investigations if isinstance(i, dict) and i.get("status") in ACTIVE_STATUSES]


def get_investigations_for_context(limit: int = 5) -> list[dict]:
    """Investigations worth surfacing in the system prompt UNPROMPTED (see
    core/personalities/base.py's INVESTIGACIONES block) — so Joan can ask
    'qué has encontrado?' (or any other phrasing) and HUGO already has the
    state in context, rather than needing a dedicated regex intent to
    recognize a fixed command. Active/incubando investigations always come
    first (still in progress, always worth surfacing); lista_para_revision/
    completada ones fill the rest of the budget, most recent first, so an
    old finished investigation doesn't crowd out a newer one once there are
    more than `limit` total."""
    with _investigations_lock:
        investigations = _load_investigations()
    investigations = [i for i in investigations if isinstance(i, dict)]
    active = [i for i in investigations if i.get("status") in ACTIVE_STATUSES]
    done = sorted(
        (i for i in investigations if i.get("status") not in ACTIVE_STATUSES),
        key=lambda i: i.get("date") or i.get("created_at") or "",
        reverse=True,
    )
    return (active + done)[:limit]


_STATUS_LABELS = {
    "activa":              "recién pedida, aún sin ciclos de análisis",
    "incubando":           "en curso",
    "lista_para_revision": "lista para revisar",
    "completada":          "completada",
}


def format_investigations_block(investigations: list[dict]) -> str:
    """Renders get_investigations_for_context()'s output as plain lines for
    the system prompt — one line per investigation, title + status + best
    available finding (conclusions if there are any, else the
    highest-confidence hypothesis so far, else 'sin resultados todavía').
    Empty string if the list is empty, same "omit entirely rather than show
    a broken/empty block" convention as the other context blocks in
    core/personalities/base.py."""
    if not investigations:
        return ""
    lines = []
    for inv in investigations:
        title  = inv.get("title") or inv.get("question") or "tema sin título"
        status = _STATUS_LABELS.get(inv.get("status"), inv.get("status") or "desconocido")
        finding = (inv.get("conclusions") or "").strip()
        if not finding:
            hypotheses = inv.get("hypotheses") or []
            if hypotheses:
                best = max(hypotheses, key=lambda h: h.get("confidence", 0))
                finding = f"hipótesis más fuerte hasta ahora: {best.get('text', '')} (confianza {best.get('confidence', 0):.0%})"
            else:
                finding = "sin resultados todavía"
        lines.append(f"- \"{title}\" [{status}]: {finding}")
    return "\n".join(lines)


def save_investigation(updated: dict) -> None:
    """Writes one investigation back by id — read-modify-write against the
    full file under the lock, so a concurrent write (e.g. a new
    create_investigation() call mid-sleep-cycle) never clobbers siblings."""
    with _investigations_lock:
        investigations = _load_investigations()
        for i, inv in enumerate(investigations):
            if isinstance(inv, dict) and inv.get("id") == updated.get("id"):
                investigations[i] = updated
                break
        else:
            investigations.append(updated)
        _save_investigations(investigations)
