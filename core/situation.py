# ═══════════════════════════════════════════════════════════════════════════
# SITUATION — Proactive Intelligence Phase 2: situation awareness.
#
# Phase 1 (see data/proactive_intelligence_audit.json) made LIRA remember and
# react. This phase makes her OBSERVE: a running snapshot of "what's going on
# right now" (data/situation.json), built from signals that already exist
# elsewhere in this codebase (session history, tasks.json, episodes.json,
# speaker confidence, logs) rather than any new sensor. Deliberately
# observation-only — detect_anomalies() flags deviations but nothing in this
# module acts on them; that's Phase 3 (judgment) and Phase 4 (initiative).
#
# Dependency-light by design (json/os/re/datetime/threading only, same
# discipline as core/task_engine.py and core/sleep.py) so this can be called
# both from the live jarvis.py process (conversation-context injection) and
# from the standalone scripts/reflective_mode.py sleep process
# (pattern/routine detection) without pulling in anything heavy.
#
# core.situation is imported at module level by core/personalities/base.py
# and core/routes_situation.py; it must never import core.commands or
# core.session at module level itself (both of those load early in the
# import chain) — any reach into them below is a function-local lazy import,
# same pattern used throughout this codebase (see core/intent.py's own
# module comment for the canonical explanation).
# ═══════════════════════════════════════════════════════════════════════════
import datetime
import json
import logging
import os
import re
import threading

logger = logging.getLogger(__name__)

SITUATION_PATH = "data/situation.json"
EPISODES_PATH  = "data/episodes.json"
TASKS_PATH     = "data/tasks.json"
LOGS_DIR       = "logs"

# Excluded from _tail_new_errors' ERROR-line scan below — these are the
# system's own decision logs (core.initiative/core.action_engine), which
# themselves quote past 'module_error' reports verbatim (e.g. 'ACT —
# "Posible error de módulo: ..."'). Scanning them fed each report back in
# as a brand-new module_error next cycle, nesting one quote level deeper
# forever (observed 2026-08-13: initiative.log lines several levels deep,
# spawning a matching pile of junk tasks via action_engine's
# 'módulo'/'error' keyword classifier). Genuine app errors live in
# activity.log/errors.log/werkzeug's own log, never in these two.
_SELF_LOG_FILES = {"initiative.log", "action_engine.log"}

MIN_OBSERVATIONS_FOR_PATTERN = 5     # spec: "Minimum 5 observations before registering a pattern"
ROUTINE_CONFIDENCE_THRESHOLD = 0.75  # spec: "Elevates high-confidence patterns (>0.75)"
MAX_EVENTS_LOGGED            = 200   # rolling cap, same idea as memory_episodes.MAX_EPISODES
MAX_UNRESOLVED_ANOMALIES     = 30

_DEFAULT_SITUATION = {
    "updated_at": "",
    "current": {
        "time_of_day":    "unknown",
        "day_type":       "unknown",
        "joan_state":     "unknown",
        "active_tasks":   [],
        "pending_topics": [],
        "social_context": "unknown",
    },
    "patterns":         [],
    "routines":         [],
    "anomalies":        [],
    "events":           [],
    "_task_ids_seen":   [],
    "_log_offsets":     {},
}


def _now() -> datetime.datetime:
    return datetime.datetime.now()


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _time_of_day(now: datetime.datetime | None = None) -> str:
    hour = (now or _now()).hour
    if 6 <= hour < 12:
        return "morning"
    if 12 <= hour < 19:
        return "afternoon"
    if 19 <= hour < 23:
        return "evening"
    return "night"


def _day_type(now: datetime.datetime | None = None) -> str:
    return "weekend" if (now or _now()).weekday() >= 5 else "weekday"


def _slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug[:max_len] or "pattern"


class SituationEngine:
    """Owns data/situation.json — the running snapshot of 'what's going on
    right now' plus the slower-moving patterns/routines/anomalies learned
    from it over time. One process-wide instance (see `situation_engine`
    at the bottom of this file), same shape as core.task_engine.TaskEngine."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    # ── persistence ──────────────────────────────────────────────────────

    def _load(self) -> dict:
        try:
            with open(SITUATION_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return json.loads(json.dumps(_DEFAULT_SITUATION))  # deep copy
        if not isinstance(data, dict):
            return json.loads(json.dumps(_DEFAULT_SITUATION))
        merged = json.loads(json.dumps(_DEFAULT_SITUATION))
        merged.update(data)
        merged["current"] = {**merged["current"], **(data.get("current") or {})}
        return merged

    def _save_locked(self, data: dict) -> None:
        """Caller must hold self._lock."""
        os.makedirs(os.path.dirname(SITUATION_PATH) or ".", exist_ok=True)
        with open(SITUATION_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ── signal gathering (each best-effort, never raises) ───────────────

    def track_task_state(self) -> dict:
        """Reads data/tasks.json — active (pending/in_progress/blocked)
        tasks right now, independent of any snapshot staleness."""
        try:
            with open(TASKS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {"active": [], "counts": {}}
        tasks = data.get("tasks", []) if isinstance(data, dict) else []
        active = [t for t in tasks if t.get("status") in ("pending", "in_progress", "blocked")]
        counts: dict[str, int] = {}
        for t in tasks:
            status = t.get("status", "unknown")
            counts[status] = counts.get(status, 0) + 1
        return {
            "active": [
                {"id": t.get("id"), "goal": t.get("goal"), "status": t.get("status")}
                for t in active
            ],
            "counts": counts,
        }

    def detect_social_context(self) -> dict:
        """alone / with_people / unknown, from the same multi-factor voice
        identification signal core.commands already computes per turn (see
        core.commands._identify_speaker_multi_factor and its module-level
        _last_speaker_confidence). No new voice data stored here — this
        only reads a confidence float that already exists (per Phase 1
        audit's still_open note, voice enrollment itself is unchanged)."""
        try:
            import core.commands as commands
            from core import speaker
            confidence = commands._last_speaker_confidence
        except Exception:
            return {"context": "unknown", "confidence": None}
        if confidence is None:
            return {"context": "unknown", "confidence": None}
        if confidence >= speaker.CONFIDENCE_HIGH:
            context = "alone"      # identified as Joan with high confidence
        elif confidence < speaker.CONFIDENCE_LOW:
            context = "with_people"  # not Joan, or too uncertain to be her
        else:
            context = "unknown"
        return {"context": context, "confidence": confidence}

    def _infer_joan_state(self, task_state: dict, now: datetime.datetime) -> str:
        """Best-effort joan_state from the limited signals actually
        available: last message length/topic shape (core.session has no
        per-turn timestamps — see its own module comment — so
        response_speed is not measurable and is deliberately left out
        rather than faked), active task count, and time of day."""
        try:
            import core.session as session_mod
            history = session_mod._get_history_snapshot()
        except Exception:
            history = []

        last_user = next((h["content"] for h in reversed(history) if h.get("role") == "user"), "")
        msg_len = len(last_user)
        active_count = len(task_state.get("active", []))
        time_of_day = _time_of_day(now)

        if not history:
            return "unknown"
        if active_count > 0 and msg_len > 40:
            return "working"
        if time_of_day == "night" and msg_len < 25:
            return "resting"
        if msg_len > 120:
            return "focused"
        if msg_len < 15 and active_count == 0:
            return "distracted"
        return "working" if active_count > 0 else "unknown"

    def _pending_topics(self, limit: int = 3) -> list[str]:
        """Recent episode topics, most-recent-first — the closest existing
        proxy for 'things Joan was in the middle of' without a dedicated
        open-topic tracker."""
        try:
            with open(EPISODES_PATH, "r", encoding="utf-8") as f:
                episodes = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []
        if not isinstance(episodes, list):
            return []
        topics = [e.get("topic") for e in episodes[-limit:] if e.get("topic")]
        return list(reversed(topics))

    def _tail_new_errors(self, data: dict) -> list[str]:
        """Best-effort scan of logs/*.log for ERROR lines written since the
        last check (byte offset per file, persisted in
        data['_log_offsets']). Never reads a whole log from scratch after
        the first pass. Returns short excerpts, not full lines."""
        found: list[str] = []
        offsets = data.setdefault("_log_offsets", {})
        try:
            if not os.path.isdir(LOGS_DIR):
                return found
            for name in os.listdir(LOGS_DIR):
                if not name.endswith(".log") or name in _SELF_LOG_FILES:
                    continue
                path = os.path.join(LOGS_DIR, name)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                start = offsets.get(name, size)   # first-ever pass: skip existing backlog
                if start > size:
                    start = 0   # log rotated/truncated
                if size > start:
                    try:
                        with open(path, "r", encoding="utf-8", errors="ignore") as f:
                            f.seek(start)
                            chunk = f.read(20000)   # cap — this is a signal, not a log viewer
                        for line in chunk.splitlines():
                            if "ERROR" in line:
                                found.append(f"{name}: {line[:200]}")
                    except OSError:
                        pass
                offsets[name] = size
        except Exception:
            logger.debug("_tail_new_errors failed (non-critical)", exc_info=True)
        return found[:5]

    # ── events / changes ─────────────────────────────────────────────────

    def detect_changes(self) -> list[dict]:
        """State-field transitions only (time_of_day/day_type/social_context
        changing since the last saved snapshot) — the subset of
        detect_events() that is purely 'X used to be A, now it's B'."""
        with self._lock:
            data = self._load()
            prev = data["current"]
            now = _now()
            fresh = {
                "time_of_day":    _time_of_day(now),
                "day_type":       _day_type(now),
                "social_context": self.detect_social_context()["context"],
            }
            changes = []
            ts = _now_iso()
            for field, new_val in fresh.items():
                old_val = prev.get(field)
                if old_val not in (None, "unknown") and old_val != new_val:
                    changes.append({
                        "event": f"{field}_changed",
                        "from": old_val, "to": new_val, "at": ts,
                    })
            return changes

    def detect_events(self) -> list[dict]:
        """Combines state-field transitions (detect_changes) with
        discrete signal-sourced events — task_created, module_error,
        silence_after_activity — appends everything new to
        data/situation.json's rolling 'events' log (capped at
        MAX_EVENTS_LOGGED), and returns only the events detected THIS
        call."""
        new_events: list[dict] = []
        ts = _now_iso()

        with self._lock:
            data = self._load()

            # time/day/social transitions
            prev = data["current"]
            now = _now()
            for field, new_val in (
                ("time_of_day", _time_of_day(now)),
                ("day_type", _day_type(now)),
            ):
                old_val = prev.get(field)
                if old_val not in (None, "unknown") and old_val != new_val:
                    new_events.append({"event": f"{field}_changed", "from": old_val, "to": new_val, "at": ts})

            # new tasks since last check
            task_state = self.track_task_state()
            seen_ids = set(data.get("_task_ids_seen", []))
            current_ids = {t["id"] for t in task_state["active"] if t.get("id")}
            for t in task_state["active"]:
                if t.get("id") and t["id"] not in seen_ids:
                    new_events.append({"event": "task_created", "task_id": t["id"], "goal": t.get("goal"), "at": ts})
            data["_task_ids_seen"] = sorted(seen_ids | current_ids)

            # module errors since last check
            for excerpt in self._tail_new_errors(data):
                new_events.append({"event": "module_error", "detail": excerpt, "at": ts})

            # silence after activity — a long gap since the last recorded
            # session end, discovered fresh (not re-flagged every call)
            try:
                with open("data/session_state.json", "r", encoding="utf-8") as f:
                    session_state = json.load(f)
                ended_at = session_state.get("ended_at")
                if ended_at:
                    gap_hours = (now - datetime.datetime.fromisoformat(ended_at)).total_seconds() / 3600
                    last_flagged = data.get("_last_silence_ended_at")
                    if gap_hours >= 3 and last_flagged != ended_at:
                        new_events.append({
                            "event": "silence_after_activity",
                            "gap_hours": round(gap_hours, 1), "since": ended_at, "at": ts,
                        })
                        data["_last_silence_ended_at"] = ended_at
            except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
                pass

            if new_events:
                events_log = data.get("events", [])
                events_log.extend(new_events)
                data["events"] = events_log[-MAX_EVENTS_LOGGED:]
                data["updated_at"] = ts
                self._save_locked(data)

        return new_events

    # ── snapshot ──────────────────────────────────────────────────────────

    def get_current_situation(self) -> dict:
        """Recomputes every 'current' field fresh, persists the refreshed
        snapshot (as a side effect, same as detect_events), and returns
        just the 'current' block — what GET /api/situation and the
        conversation-context injection both want."""
        self.detect_events()   # side effect: logs any new events, cheap no-op most calls

        with self._lock:
            data = self._load()
            now = _now()
            task_state = self.track_task_state()
            social = self.detect_social_context()
            current = {
                "time_of_day":    _time_of_day(now),
                "day_type":       _day_type(now),
                "joan_state":     self._infer_joan_state(task_state, now),
                "active_tasks":   [t["goal"] for t in task_state["active"] if t.get("goal")],
                "pending_topics": self._pending_topics(),
                "social_context": social["context"],
            }
            data["current"] = current
            data["updated_at"] = _now_iso()
            self._save_locked(data)
            return current

    def update_snapshot(self) -> None:
        """Refresh + persist 'current' only, no return value — the sleep-
        cycle call per the module spec (get_current_situation() already
        does this as a side effect; this wrapper just makes the sleep
        integration's intent explicit without depending on the return
        value)."""
        self.get_current_situation()

    # ── patterns ──────────────────────────────────────────────────────────

    def detect_patterns(self) -> list[dict]:
        """Reads data/episodes.json and looks for repeated topics — by
        weekday, and overall frequency — registering/reinforcing a pattern
        once a topic has been seen at least MIN_OBSERVATIONS_FOR_PATTERN
        times. Meant to run during sleep (see reflective_mode.py's
        on_cycle_complete wiring), not per-turn — episodes accumulate slowly
        enough that per-turn recomputation would be wasted work."""
        try:
            with open(EPISODES_PATH, "r", encoding="utf-8") as f:
                episodes = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            episodes = []
        if not isinstance(episodes, list) or not episodes:
            return []

        _WEEKDAYS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

        # overall topic frequency
        topic_dates: dict[str, list[str]] = {}
        weekday_topic_dates: dict[tuple[str, str], list[str]] = {}
        for e in episodes:
            topic = str(e.get("topic") or "").strip()
            date_str = str(e.get("date") or "")
            if not topic or not date_str:
                continue
            topic_dates.setdefault(topic, []).append(date_str)
            try:
                weekday = _WEEKDAYS_ES[datetime.date.fromisoformat(date_str[:10]).weekday()]
                weekday_topic_dates.setdefault((weekday, topic), []).append(date_str)
            except ValueError:
                continue

        candidates: list[dict] = []
        for topic, dates in topic_dates.items():
            if len(dates) >= MIN_OBSERVATIONS_FOR_PATTERN:
                candidates.append({
                    "kind":        "topic_cluster",
                    "description": f"Joan habla frecuentemente sobre: {topic}",
                    "observations": len(dates),
                    "last_seen":    max(dates),
                    "confidence":   min(0.95, 0.5 + 0.05 * len(dates)),
                })
        for (weekday, topic), dates in weekday_topic_dates.items():
            if len(dates) >= MIN_OBSERVATIONS_FOR_PATTERN:
                candidates.append({
                    "kind":        "weekday_topic",
                    "description": f"Joan suele hablar de '{topic}' los {weekday}",
                    "observations": len(dates),
                    "last_seen":    max(dates),
                    "confidence":   min(0.95, 0.5 + 0.05 * len(dates)),
                })

        if not candidates:
            return []

        with self._lock:
            data = self._load()
            existing = {p["id"]: p for p in data.get("patterns", [])}
            for c in candidates:
                pid = f"pattern_{_slugify(c['description'])}"
                existing[pid] = {
                    "id":           pid,
                    "description":  c["description"],
                    "confidence":   c["confidence"],
                    "observations": c["observations"],
                    "last_seen":    c["last_seen"],
                }
            data["patterns"] = list(existing.values())
            self._save_locked(data)
            return data["patterns"]

    def detect_routines(self) -> list[dict]:
        """Elevates patterns with confidence > ROUTINE_CONFIDENCE_THRESHOLD
        into routines — actionable predictions, distinct from the merely
        observational patterns list (see module docstring)."""
        with self._lock:
            data = self._load()
            patterns = data.get("patterns", [])
            existing = {r["id"]: r for r in data.get("routines", [])}
            for p in patterns:
                if p.get("confidence", 0) <= ROUTINE_CONFIDENCE_THRESHOLD:
                    continue
                rid = p["id"].replace("pattern_", "routine_", 1)
                existing[rid] = {
                    "id":                   rid,
                    "trigger":              p["description"],
                    "predicted_behavior":   f"Es probable que Joan continúe con: {p['description']}",
                    "confidence":           p["confidence"],
                    "useful_preparation":   [],
                    "source_pattern_id":    p["id"],
                }
            data["routines"] = list(existing.values())
            self._save_locked(data)
            return data["routines"]

    # ── anomalies ────────────────────────────────────────────────────────

    def detect_anomalies(self) -> list[dict]:
        """Flags behavior that deviates from established routines — active
        at unusual hours (no routine covers this time_of_day/day_type
        combination at all, despite routines existing), or a
        silence_after_activity event logged well outside any known active
        window. Observe only, per module spec: nothing here acts on an
        anomaly, it's just recorded with resolved=false for Phase 3/4 to
        consume later."""
        with self._lock:
            data = self._load()
            routines = data.get("routines", [])
            now = _now()
            time_of_day = _time_of_day(now)
            day_type = _day_type(now)
            new_anomalies = []

            if routines:
                covers_now = any(
                    time_of_day in r.get("trigger", "") or day_type in r.get("trigger", "")
                    for r in routines
                )
                if not covers_now and time_of_day == "night":
                    new_anomalies.append({
                        "detected_at": _now_iso(),
                        "description": f"Actividad a las {now.strftime('%H:%M')} — fuera de cualquier rutina conocida",
                        "resolved":    False,
                    })

            recent_silence = [e for e in data.get("events", []) if e.get("event") == "silence_after_activity"]
            if recent_silence:
                last = recent_silence[-1]
                already_recorded = any(
                    a.get("description", "").endswith(last.get("since", "\0")) for a in data.get("anomalies", [])
                )
                if not already_recorded and last.get("gap_hours", 0) >= 6:
                    new_anomalies.append({
                        "detected_at": _now_iso(),
                        "description": f"Silencio de {last['gap_hours']}h tras actividad — desde {last.get('since')}",
                        "resolved":    False,
                    })

            if new_anomalies:
                anomalies = data.get("anomalies", []) + new_anomalies
                data["anomalies"] = anomalies[-MAX_UNRESOLVED_ANOMALIES:]
                self._save_locked(data)
            return new_anomalies


situation_engine = SituationEngine()
