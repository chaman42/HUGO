# SKILL FORGE — procedural knowledge (data/procedural_skills.json),
# extracted from completed TaskEngine tasks. Completely separate from
# skills/ (installable capability modules — see core/module_manager.py):
# a procedural skill is a piece of KNOW-HOW ("how to build a communication
# module without repeating past mistakes"), not runnable code. Nothing
# here writes to skills/, and nothing in skills/ reads this file.
#
# Uses core.sleep_llm._ollama_generate (llama3.2:3b — HUGO's existing
# conversational-adjacent local model, already used for sleep-phase
# reasoning) for extraction, NOT core.code_engine.LLMRouter's
# qwen2.5-coder — this is summarizing a completed task in natural
# language, not generating code.
import datetime
import json
import logging
import re
import threading

logger = logging.getLogger(__name__)

PROCEDURAL_SKILLS_PATH = "data/procedural_skills.json"

MIN_STEPS_TO_FORGE = 3
DUPLICATE_TAG_OVERLAP_THRESHOLD = 0.7

_CODE_GEN_KEYWORDS = (
    "código", "codigo", "implementa", "implementar", "programa", "programar",
    "desarrolla", "desarrollar", "escribe", "escribir código", "genera código",
    "module.json", "sandbox", "skill",
)

_STOPWORDS_ES = {
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del", "al",
    "a", "en", "para", "con", "por", "que", "y", "o", "su", "sus", "es", "se",
    "lo", "le", "les", "como", "sin", "sobre", "entre", "hacia", "hasta",
}


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _simple_tags(text: str) -> list:
    """Cheap, dependency-free keyword extraction — same 'simple keyword
    matching' philosophy as core.commands._keywords/_fact_similarity, used
    here so TaskEngine.create_task() can derive tags for
    find_relevant_skills() without needing an LLM call on the synchronous
    task-creation path."""
    words = re.findall(r"[a-záéíóúñ0-9]+", (text or "").lower())
    return [w for w in words if len(w) > 2 and w not in _STOPWORDS_ES][:8]


def _normalize_tags(raw_tags: list) -> list:
    """Ollama routinely returns multi-word phrases ('integración con
    whatsapp', 'cloud api') despite being asked for keywords — stored
    as-is, those would NEVER set-overlap with a plain goal's single-word
    _simple_tags(), silently breaking find_relevant_skills()/dedup for
    every skill an LLM phrased this way. Tokenize each returned tag
    through the same _simple_tags() logic and flatten, so stored tags are
    always single lowercase words, consistent with how goal-text tags are
    derived everywhere else in this file."""
    out, seen = [], set()
    for raw in raw_tags:
        for word in _simple_tags(str(raw)):
            if word not in seen:
                seen.add(word)
                out.append(word)
    return out[:8]


def _extract_json_block(raw: str) -> dict | None:
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    try:
        data = json.loads(m.group(0) if m else raw)
    except (json.JSONDecodeError, AttributeError):
        return None
    return data if isinstance(data, dict) else None


class SkillForge:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._extraction_cache: dict = {}   # {"task_id": ..., "data": {...}} — one Ollama call per forge

    # ── persistence ──────────────────────────────────────────────────────

    def _load(self) -> dict:
        try:
            with open(PROCEDURAL_SKILLS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {"skills": []}
        if not isinstance(data, dict) or not isinstance(data.get("skills"), list):
            return {"skills": []}
        return data

    def _save_locked(self, data: dict) -> None:
        """Caller must hold self._lock."""
        import os
        os.makedirs(os.path.dirname(PROCEDURAL_SKILLS_PATH) or ".", exist_ok=True)
        with open(PROCEDURAL_SKILLS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _next_id_locked(self, data: dict) -> str:
        nums = []
        for s in data["skills"]:
            m = re.match(r"skill_(\d+)$", str(s.get("id", "")))
            if m:
                nums.append(int(m.group(1)))
        return f"skill_{(max(nums) + 1) if nums else 1:03d}"

    # ── queries (also back the API routes) ──────────────────────────────

    def get_all_skills(self) -> list:
        with self._lock:
            return list(self._load()["skills"])

    def get_skill(self, skill_id: str) -> dict:
        with self._lock:
            data = self._load()
        entry = next((s for s in data["skills"] if s.get("id") == skill_id), None)
        return dict(entry) if entry else {}

    def find_relevant_skills(self, goal: str, tags: list) -> list:
        """Called by TaskEngine before starting a new task (and by
        CodeEngine when building a generation prompt — see that module).
        Pure tag-overlap matching, no LLM call, so it's cheap enough to run
        on a synchronous task-creation path. Sorted by use_count (most
        battle-tested procedures first), capped at 5."""
        goal_words = set(_simple_tags(goal)) | {str(t).lower().strip() for t in (tags or []) if str(t).strip()}
        if not goal_words:
            return []
        matches = []
        for s in self.get_all_skills():
            skill_tags = {str(t).lower() for t in s.get("trigger_tags", [])}
            if goal_words & skill_tags:
                matches.append(s)
        matches.sort(key=lambda s: s.get("use_count", 0), reverse=True)
        return matches[:5]

    def apply_skill(self, skill_id: str) -> dict:
        """Marks a skill as used — call this whenever its procedure
        actually informed a new task/generation, not just because it
        appeared in find_relevant_skills()'s results."""
        with self._lock:
            data = self._load()
            entry = next((s for s in data["skills"] if s.get("id") == skill_id), None)
            if entry is None:
                return {}
            entry["use_count"] = entry.get("use_count", 0) + 1
            entry["last_used"] = _now_iso()
            self._save_locked(data)
            return dict(entry)

    # ── forging ──────────────────────────────────────────────────────────

    def _should_forge(self, task: dict) -> bool:
        """Only forge if the task had 3+ steps AND at least one step reads
        like real code-generation work — a two-step errand or a purely
        research/organizational task doesn't deserve a standing
        procedure."""
        steps = task.get("steps", [])
        if len(steps) < MIN_STEPS_TO_FORGE:
            return False
        text = " ".join(s.get("description", "") for s in steps).lower()
        return any(kw in text for kw in _CODE_GEN_KEYWORDS)

    def _ollama_extract(self, task: dict) -> dict:
        """One combined Ollama call producing {title, procedure, pitfalls,
        tags, verification_steps} together. _extract_procedure()/
        _extract_pitfalls()/_extract_tags()/_generate_title()/
        _extract_verification_steps() below all read from this single
        cached result (keyed by task id) rather than each triggering their
        own Ollama round trip."""
        cache = self._extraction_cache
        if cache.get("task_id") == task.get("id") and cache.get("data"):
            return cache["data"]

        goal = task.get("goal", "")
        fallback = {
            "title":              (goal or "Procedimiento")[:80],
            "procedure":          [s.get("description", "") for s in task.get("steps", [])],
            "pitfalls":           [],
            "tags":               _simple_tags(goal),
            "verification_steps": [],
        }

        steps_text = "\n".join(
            f"- {s.get('description', '')}: {s.get('result') or '(sin resultado)'}"
            for s in task.get("steps", [])
        )
        prompt = (
            f"Objetivo de la tarea: {goal}\n\n"
            f"Pasos completados:\n{steps_text}\n\n"
            "Extrae el conocimiento reutilizable de esta tarea ya completada, "
            "para que la próxima vez que HUGO haga algo parecido no repita "
            "errores. Responde SOLO con un JSON con estas claves exactas:\n"
            '"title": string, título breve del procedimiento\n'
            '"procedure": lista de strings, pasos numerados como "1. ..."\n'
            '"pitfalls": lista de strings, errores o problemas a evitar (vacía si ninguno)\n'
            '"tags": lista de 3 a 6 palabras clave en minúsculas\n'
            '"verification_steps": lista de strings, cómo comprobar que salió bien\n'
            "Sin texto fuera del JSON."
        )

        try:
            from core.sleep_llm import _ollama_generate
            raw = _ollama_generate(
                "Eres HUGO extrayendo conocimiento procedimental de una tarea completada, en español.",
                prompt, max_tokens=500,
            )
        except Exception as e:
            logger.warning("SkillForge: Ollama extraction failed (%s) — using fallback", e)
            raw = None

        parsed = _extract_json_block(raw) if raw else None
        if parsed is None:
            data = fallback
        else:
            data = {
                "title":              str(parsed.get("title") or fallback["title"])[:100],
                "procedure":          [str(x) for x in (parsed.get("procedure") or fallback["procedure"])][:10],
                "pitfalls":           [str(x) for x in (parsed.get("pitfalls") or [])][:10],
                "tags":               _normalize_tags(parsed.get("tags") or fallback["tags"]),
                "verification_steps": [str(x) for x in (parsed.get("verification_steps") or [])][:10],
            }

        cache["task_id"], cache["data"] = task.get("id"), data
        return data

    def _extract_procedure(self, task: dict) -> list:
        return self._ollama_extract(task).get("procedure") or []

    def _extract_pitfalls(self, task: dict) -> list:
        return self._ollama_extract(task).get("pitfalls") or []

    def _extract_tags(self, task: dict) -> list:
        return self._ollama_extract(task).get("tags") or []

    def _extract_verification_steps(self, task: dict) -> list:
        return self._ollama_extract(task).get("verification_steps") or []

    def _generate_title(self, task: dict) -> str:
        return self._ollama_extract(task).get("title") or (task.get("goal") or "Procedimiento")[:80]

    def _find_duplicate(self, tags: list) -> dict | None:
        """>70% (Jaccard) tag overlap with an existing skill — merge into
        it instead of creating a near-duplicate entry."""
        if not tags:
            return None
        tag_set = {str(t).lower() for t in tags}
        best, best_overlap = None, 0.0
        for s in self.get_all_skills():
            existing = {str(t).lower() for t in s.get("trigger_tags", [])}
            union = tag_set | existing
            if not union:
                continue
            overlap = len(tag_set & existing) / len(union)
            if overlap > DUPLICATE_TAG_OVERLAP_THRESHOLD and overlap > best_overlap:
                best, best_overlap = s, overlap
        return best

    def _merge_into(self, existing_id: str, procedure: list, pitfalls: list, task_id: str) -> None:
        with self._lock:
            data = self._load()
            entry = next((s for s in data["skills"] if s.get("id") == existing_id), None)
            if entry is None:
                return
            merged_pitfalls = list(entry.get("pitfalls", []))
            for p in pitfalls:
                if p not in merged_pitfalls:
                    merged_pitfalls.append(p)
            entry["pitfalls"] = merged_pitfalls[:15]
            if procedure:
                entry["procedure"] = procedure   # the newer task's version supersedes the old one
            entry["generated_from_task"] = task_id
            self._save_locked(data)

    def forge_from_task(self, task_id: str) -> str | None:
        """Called automatically when a task reaches 'completed' (both
        TaskEngine.complete_task()'s explicit override and
        TaskEngine.advance_task()'s natural last-step completion — see
        that module; the spec names complete_task() as the trigger, but
        most real completions happen via advance_task() during a sleep
        cycle, so both call this). Returns the skill id if forged/merged,
        None if the task was too simple, not code-related, or not
        actually completed — this NEVER forges from a failed/blocked
        task."""
        from core.task_engine import task_engine
        task = task_engine.get_task_status(task_id)
        if not task or task.get("status") != "completed":
            return None
        if not self._should_forge(task):
            return None

        try:
            import core.ollama_control as ollama_control
            ollama_control.ensure_ollama_daemon_running()
            procedure    = self._extract_procedure(task)
            pitfalls     = self._extract_pitfalls(task)
            tags         = self._extract_tags(task)
            title        = self._generate_title(task)
            verification = self._extract_verification_steps(task)
        finally:
            try:
                import core.ollama_control as ollama_control
                ollama_control.kill_llama_server()
            except Exception:
                pass

        duplicate = self._find_duplicate(tags)
        if duplicate:
            self._merge_into(duplicate["id"], procedure, pitfalls, task_id)
            logger.info("SkillForge: merged task %s into existing skill %s", task_id, duplicate["id"])
            return duplicate["id"]

        skill = {
            "id":                  None,
            "title":               title,
            "created_at":          _now_iso(),
            "use_count":           0,
            "last_used":           None,
            "trigger_tags":        tags,
            "procedure":           procedure,
            "pitfalls":            pitfalls,
            "verification_steps":  verification,
            "generated_from_task": task_id,
        }
        with self._lock:
            data = self._load()
            skill["id"] = self._next_id_locked(data)
            data["skills"].append(skill)
            self._save_locked(data)
        logger.info("SkillForge: forged new skill %s from task %s", skill["id"], task_id)
        return skill["id"]


skill_forge = SkillForge()
