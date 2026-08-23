# ═══════════════════════════════════════════════════════════════════════════
# SOCIAL — Proactive Intelligence Phase 6 (final phase): HUGO knows who
# she's talking to, their relationship with Joan, and adapts accordingly.
#
# Builds on real signals that already exist in this codebase rather than
# inventing new ones: core.speaker's single enrolled voice fingerprint
# (Joan-only — see data/voice_fingerprint.json / core/speaker.py's own
# CONFIDENCE_HIGH/LOW), core.linguistic_fingerprint's score() (also
# Joan-only — one global fingerprint, not per-person), and
# data/discord_authorized.json's admin/user/blocked roles. None of these
# can currently identify a SPECIFIC non-Joan person by voice or writing
# style — there is exactly one enrolled voice/linguistic profile in this
# app, Joan's. _match_voice/_match_linguistic below can therefore only
# ever confirm "this is Joan" or "this is not confidently Joan"; they
# cannot tell two different non-Joan people apart. data/social_profiles.json
# still carries a voice_profile_id field per person so a future per-person
# enrollment slots in without a schema change — it's just not populated
# for anyone but Joan today. Discord IS a real multi-person channel
# (data/discord_authorized.json already tracks distinct user IDs), so
# _match_context's Discord branch is the one path that genuinely
# distinguishes specific non-Joan individuals right now.
#
# Same no-LLM, deterministic-heuristic discipline as Phase 2-5.
# ═══════════════════════════════════════════════════════════════════════════
import datetime
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

PROFILES_PATH = "data/social_profiles.json"

# Recalibrated 2026-08-10 (was 0.75, the spec's literal number): a genuine
# enrolled match on Joan's own real voice measured 0.48 raw — this compares
# the RAW single-utterance cosine score against a mean-of-N-samples
# fingerprint, a noisier signal than core.commands._identify_speaker_multi_factor's
# blended voice+linguistic+context score (which speaker.CONFIDENCE_HIGH=0.75
# is actually calibrated for). 0.35 leaves real margin below the one
# observed genuine value rather than sitting right at the edge of it —
# revisit if real usage shows either false accepts (raise it) or genuine
# Joan still failing to clear it (lower it further). See
# absorb_sample()'s own wiring in _match_voice below — every accepted match
# folds back into the fingerprint, so this should only need loosening once.
VOICE_CONFIDENCE_THRESHOLD      = 0.35
LINGUISTIC_CONFIDENCE_THRESHOLD = 0.65   # spec: "linguistic fingerprint match confidence > 0.65" — no evidence yet this needs adjusting
MAX_INTERACTIONS_STORED         = 100    # per person, rolling cap

_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


# ═══════════════════════════════════════════════════════════════════════════
# DATA MODELS
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Person:
    id:                    str
    name:                  str | None
    relationship_to_joan:  str   # self | friend | family | colleague | stranger
    trust_level:           float
    knows_hugo:            bool
    voice_profile_id:      str | None = None
    linguistic_profile:    dict = field(default_factory=dict)
    first_seen:            str = ""
    last_seen:             str = ""
    interaction_count:     int = 0
    confidence:            float = 0.0   # how sure identify_person() is about this match — not persisted, per-call only
    # Whether Joan has ever explicitly reviewed/set this person's trust tier
    # via the Personas UI (POST .../trust or POST /people, both set this
    # True) — distinct from trust_level itself, which can already be
    # nonzero from a system default (e.g. Discord's authorized-'user' role
    # auto-assigns 0.3, see _match_context below) without Joan ever having
    # looked at it. The Personas tab shows "Desconocido" for anyone with
    # this still False, regardless of their numeric trust_level.
    trust_confirmed:       bool = False


@dataclass
class Relationship:
    person_id:      str
    type:           str    # friend | family | colleague | acquaintance | stranger
    closeness:      float
    joan_sentiment: str    # positive | neutral | negative | unknown
    shared_topics:  list[str] = field(default_factory=list)
    notes:          list[str] = field(default_factory=list)


@dataclass
class BehaviorProfile:
    tone:                  str   # formal | casual | technical | warm | neutral
    response_length:       str   # brief | normal | detailed | minimal
    information_sharing:   str   # full | limited | minimal
    hugo_personality_mode: str   # normal | reserved | professional | friendly


@dataclass
class InfoPermissions:
    can_access_joan_schedule:        bool
    can_access_joan_projects:        bool
    can_access_joan_memory:          bool
    can_ask_hugo_personal_questions: bool
    hugo_acknowledges_knowing_joan:  bool


# ═══════════════════════════════════════════════════════════════════════════
# PROFILE STORE — data/social_profiles.json. Only 'joan' is seeded: the
# spec's own example schema shows an illustrative second person ('Paco') to
# demonstrate the shape a friend record takes, but there is no such person
# anywhere in this app's real data (checked data/, episodes, Discord auth —
# nothing) — seeding a fabricated friend would misrepresent this as
# already-observed history it isn't. Real entries appear here the first
# time identify_person() actually meets someone.
# ═══════════════════════════════════════════════════════════════════════════

_DEFAULT_PROFILES = {
    "people": {
        "joan": {
            "id": "joan", "name": "Joan", "relationship_to_joan": "self",
            "trust_level": 1.0, "knows_hugo": True, "voice_profile_id": "joan_primary",
            "trust_confirmed": True,
            "linguistic_profile": {}, "first_seen": "", "last_seen": "", "interaction_count": 0,
            "discord_id": None,
            "relationship": {"type": "self", "closeness": 1.0, "joan_sentiment": "n/a", "shared_topics": [], "notes": []},
            "interactions": [],
        },
    },
    "strangers_seen": 0,
    "introduction_template": "Soy HUGO.",
}


def _load() -> dict:
    try:
        with open(PROFILES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return json.loads(json.dumps(_DEFAULT_PROFILES))
    if not isinstance(data, dict) or not isinstance(data.get("people"), dict):
        return json.loads(json.dumps(_DEFAULT_PROFILES))
    data.setdefault("strangers_seen", 0)
    data.setdefault("introduction_template", "Soy HUGO.")
    data["people"].setdefault("joan", json.loads(json.dumps(_DEFAULT_PROFILES["people"]["joan"])))
    return data


def _save_locked(data: dict) -> None:
    os.makedirs(os.path.dirname(PROFILES_PATH) or ".", exist_ok=True)
    with open(PROFILES_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _person_from_record(record: dict) -> Person:
    person_id = record.get("id", "unknown")
    return Person(
        id=person_id,
        name=record.get("name"),
        relationship_to_joan=record.get("relationship_to_joan", "stranger"),
        trust_level=float(record.get("trust_level", 0.0)),
        knows_hugo=bool(record.get("knows_hugo", False)),
        voice_profile_id=record.get("voice_profile_id"),
        linguistic_profile=record.get("linguistic_profile", {}) or {},
        first_seen=record.get("first_seen", ""),
        last_seen=record.get("last_seen", ""),
        interaction_count=int(record.get("interaction_count", 0)),
        # Joan's own record is always treated as confirmed regardless of
        # what's actually stored — her trust is already hardcoded at 1.0
        # everywhere else (the trust/delete routes both refuse to touch
        # her), and this covers a profile store saved before this field
        # existed, which would otherwise show her as "Desconocido".
        trust_confirmed=True if person_id == "joan" else bool(record.get("trust_confirmed", False)),
    )


def get_all_people() -> list[Person]:
    return [_person_from_record(r) for r in _load()["people"].values()]


def get_person(person_id: str) -> Person | None:
    record = _load()["people"].get(person_id)
    return _person_from_record(record) if record else None


def _next_person_id_locked(data: dict) -> str:
    n = 1
    while f"person_{n:03d}" in data["people"]:
        n += 1
    return f"person_{n:03d}"


# ═══════════════════════════════════════════════════════════════════════════
# SOCIAL ENGINE
# ═══════════════════════════════════════════════════════════════════════════

# BEHAVIOR_BY_RELATIONSHIP is defined below get_behavior_profile — kept near
# its one caller (Phase 6, part 2). PERMISSIONS_BY_TRUST likewise.

class SocialEngine:

    # ── identification ───────────────────────────────────────────────────

    def _match_voice(self, voice_sample: dict) -> Person | None:
        """Only ever confirms Joan — see module docstring. voice_sample:
        {'audio_path': str | None}."""
        audio_path = (voice_sample or {}).get("audio_path")
        if not audio_path:
            return None
        try:
            from core import speaker
            confidence = speaker.identify_speaker(audio_path)
        except Exception:
            logger.debug("_match_voice: identify_speaker failed (non-critical)", exc_info=True)
            return None
        if confidence <= 0:
            return None
        person = get_person("joan")
        if person is None:
            return None
        person.confidence = confidence
        return person

    def _absorb_confirmed_sample(self, audio_path: str) -> None:
        """Background-thread target — see identify_person()'s own call
        site for why this only ever runs on an already-accepted match.
        Best-effort, never raises past this point (speaker.absorb_sample
        itself already never raises, this is just an extra safety net for
        the thread target)."""
        try:
            from core import speaker
            speaker.absorb_sample(audio_path)
        except Exception:
            logger.debug("_absorb_confirmed_sample failed (non-critical)", exc_info=True)

    def _match_linguistic(self, linguistic_sample: str) -> Person | None:
        """Same Joan-only limitation as _match_voice — core.linguistic_fingerprint
        has exactly one learned profile."""
        if not linguistic_sample:
            return None
        try:
            from core import linguistic_fingerprint
            confidence = linguistic_fingerprint.score(linguistic_sample)
        except Exception:
            logger.debug("_match_linguistic: score failed (non-critical)", exc_info=True)
            return None
        person = get_person("joan")
        if person is None:
            return None
        person.confidence = confidence
        return person

    def _match_context(self, discord_user_id: str | None = None) -> Person | None:
        """Discord ID → social profile, the one channel that genuinely
        distinguishes specific non-Joan individuals today (see module
        docstring). 'time, location patterns' from the spec's own comment
        have no real signal source in this app yet — not implemented
        rather than faked; this only ever resolves via Discord ID."""
        if not discord_user_id:
            return None
        data = _load()
        for record in data["people"].values():
            if record.get("discord_id") == discord_user_id:
                p = _person_from_record(record)
                p.confidence = 1.0   # a Discord ID match is exact, not probabilistic
                return p

        # New Discord person — create a real, persisted stranger record
        # (not the ephemeral 'unknown' fallback) since a Discord ID IS a
        # stable identifier we can recognize again next time, even though
        # we don't yet know their relationship to Joan or their name.
        try:
            from core import discord_bridge
            role = discord_bridge.get_role(discord_user_id)
        except Exception:
            role = "unknown"
        if role == "admin":
            return get_person("joan")   # Joan's own Discord ID (DISCORD_JOAN_ID)

        with _lock:
            data = _load()
            person_id = _next_person_id_locked(data)
            now = _now_iso()
            data["people"][person_id] = {
                "id": person_id, "name": None, "relationship_to_joan": "stranger",
                "trust_level": 0.3 if role == "user" else 0.0,   # Discord-authorized 'user' role is a mild trust signal Joan already granted, not full trust
                "knows_hugo": role == "user", "voice_profile_id": None,
                "linguistic_profile": {}, "first_seen": now, "last_seen": now, "interaction_count": 0,
                "discord_id": discord_user_id,
                "relationship": {"type": "stranger", "closeness": 0.1, "joan_sentiment": "unknown", "shared_topics": [], "notes": []},
                "interactions": [],
            }
            data["strangers_seen"] = data.get("strangers_seen", 0) + 1
            _save_locked(data)
        p = _person_from_record(data["people"][person_id])
        p.confidence = 1.0
        return p

    def identify_person(self, voice_sample: dict, linguistic_sample: str) -> Person:
        # A Discord ID is an authoritative, deterministic signal — not a
        # probabilistic one like voice/linguistic — so when one is present
        # it's checked FIRST, ahead of the fuzzy Joan-only linguistic
        # fingerprint below. Reordering this from a literal "voice, then
        # linguistic, then context" reading matters for real safety: this
        # app's linguistic fingerprint can only ever confirm "sounds like
        # Joan's known vocabulary" (see module docstring) — it cannot rule
        # OTHER people out, so a Discord stranger's message scoring above
        # LINGUISTIC_CONFIDENCE_THRESHOLD by vocabulary overlap alone would
        # otherwise misidentify them as Joan and grant Joan-tier
        # permissions, which is exactly what the hard 'never share Joan's
        # memory below trust 1.0' rule exists to prevent.
        discord_user_id = (voice_sample or {}).get("discord_user_id")
        if discord_user_id:
            person = self._match_context(discord_user_id)
            if person is not None:
                _mark_present(person)
                return person

        person = self._match_voice(voice_sample or {})
        if person is not None and person.confidence > VOICE_CONFIDENCE_THRESHOLD:
            _mark_present(person)
            # Every accepted match refines the fingerprint it was matched
            # against — see speaker.absorb_sample's own docstring for why
            # this only ever runs on an ALREADY-confirmed sample (never on
            # an unconfirmed/rejected one, which would let a false accept
            # quietly drift the fingerprint). Backgrounded — embedding a
            # fresh sample has real compute cost and must never add
            # latency to the turn that's already in flight.
            audio_path = (voice_sample or {}).get("audio_path")
            if audio_path:
                threading.Thread(
                    target=self._absorb_confirmed_sample, args=(audio_path,),
                    daemon=True, name="voice-absorb",
                ).start()
            return person

        person = self._match_linguistic(linguistic_sample)
        if person is not None and person.confidence > LINGUISTIC_CONFIDENCE_THRESHOLD:
            _mark_present(person)
            return person

        unknown = Person(id="unknown", name=None, relationship_to_joan="stranger", trust_level=0.0, knows_hugo=False)
        _mark_present(unknown)
        return unknown

    # ── relationships ────────────────────────────────────────────────────

    def get_relationship(self, person_id: str) -> Relationship:
        data = _load()
        record = data["people"].get(person_id)
        rel = (record or {}).get("relationship", {})
        return Relationship(
            person_id=person_id,
            type=rel.get("type", "stranger"),
            closeness=float(rel.get("closeness", 0.0)),
            joan_sentiment=rel.get("joan_sentiment", "unknown"),
            shared_topics=list(rel.get("shared_topics", [])),
            notes=list(rel.get("notes", [])),
        )

    def who_is_present(self) -> list[Person]:
        """Single-listener architecture (see core/listener.py) — this app
        has never tracked simultaneous multiple speakers, so 'present'
        realistically means 'the most recently identified speaker, if
        recent enough to still be considered around'. Falls back to Joan
        when nothing else is known (matches core.situation's own
        social_context default reasoning: no signal defaults to the
        common case, not to 'nobody')."""
        with _presence_lock:
            presence = dict(_last_presence) if _last_presence else None
        if presence is None:
            joan = get_person("joan")
            return [joan] if joan else []
        age = (datetime.datetime.now() - presence["at"]).total_seconds()
        if age > PRESENCE_TTL_SECONDS:
            joan = get_person("joan")
            return [joan] if joan else []
        return [presence["person"]]

    def update_interaction(self, person_id: str, interaction: dict) -> None:
        """interaction: {'topics_discussed': [...], 'tone': str, 'outcome':
        'positive'|'negative'|'neutral'}. Best-effort, never raises —
        called from conversation-handling code paths that shouldn't break
        over a bookkeeping failure."""
        try:
            with _lock:
                data = _load()
                record = data["people"].get(person_id)
                if record is None:
                    return
                now = _now_iso()
                record["last_seen"] = now
                record["interaction_count"] = record.get("interaction_count", 0) + 1
                entry = {
                    "person_id":        person_id,
                    "timestamp":        now,
                    "topics_discussed": interaction.get("topics_discussed", []),
                    "tone":             interaction.get("tone", ""),
                    "outcome":          interaction.get("outcome", "neutral"),
                }
                interactions = record.setdefault("interactions", [])
                interactions.append(entry)
                record["interactions"] = interactions[-MAX_INTERACTIONS_STORED:]
                _save_locked(data)
            self._update_relationship_depth(person_id, interaction)
        except Exception:
            logger.warning("update_interaction failed for %r (non-critical)", person_id, exc_info=True)

    def _update_relationship_depth(self, person_id: str, interaction: dict) -> None:
        """closeness increases with positive interactions, decreases with
        negative — Joan never sets this manually (spec: 'emerges from
        observed behavior'), only trust_level is Joan-controlled (see
        POST /api/social/people/<id>/trust)."""
        with _lock:
            data = _load()
            record = data["people"].get(person_id)
            if record is None:
                return
            rel = record.setdefault("relationship", {"type": record.get("relationship_to_joan", "stranger"), "closeness": 0.0, "joan_sentiment": "unknown", "shared_topics": [], "notes": []})

            outcome = interaction.get("outcome", "neutral")
            delta = {"positive": 0.05, "negative": -0.1, "neutral": 0.0}.get(outcome, 0.0)
            rel["closeness"] = max(0.0, min(1.0, rel.get("closeness", 0.0) + delta))

            topics = interaction.get("topics_discussed") or []
            shared = set(rel.get("shared_topics", []))
            shared.update(t for t in topics if t)
            rel["shared_topics"] = sorted(shared)[:30]

            rel["joan_sentiment"] = self._infer_joan_sentiment(record.get("name"))

            record["relationship"] = rel
            _save_locked(data)

    _POSITIVE_SENTIMENT_RE = re.compile(r"buen[oa]|genial|confío|cariño|aprecio|divertid", re.IGNORECASE)
    _NEGATIVE_SENTIMENT_RE = re.compile(r"mal[oa]|desconf[ií]|molest|pesad[oa]|evitar", re.IGNORECASE)

    def _infer_joan_sentiment(self, person_name: str | None) -> str:
        """Best-effort — scans recent episodes for mentions of this
        person's name near a sentiment-bearing word. No name -> no signal
        to key the search on -> 'unknown', never guessed."""
        if not person_name:
            return "unknown"
        try:
            with open("data/episodes.json", "r", encoding="utf-8") as f:
                episodes = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return "unknown"
        if not isinstance(episodes, list):
            return "unknown"
        name_lower = person_name.lower()
        pos = neg = 0
        for e in episodes[-30:]:
            text = f"{e.get('summary', '')} {e.get('emotional_tone', '')}".lower()
            if name_lower not in text:
                continue
            if self._POSITIVE_SENTIMENT_RE.search(text):
                pos += 1
            if self._NEGATIVE_SENTIMENT_RE.search(text):
                neg += 1
        if pos == 0 and neg == 0:
            return "unknown"
        if pos > neg:
            return "positive"
        if neg > pos:
            return "negative"
        return "neutral"

    # ── behavior adaptation ──────────────────────────────────────────────

    def get_behavior_profile(self, person_id: str) -> BehaviorProfile:
        person = get_person(person_id)
        rel_type = person.relationship_to_joan if person else "stranger"
        return BEHAVIOR_BY_RELATIONSHIP.get(rel_type, BEHAVIOR_BY_RELATIONSHIP["stranger"])

    def get_information_permissions(self, person_id: str) -> InfoPermissions:
        person = get_person(person_id)
        trust = person.trust_level if person else 0.0
        if trust >= 1.0:
            tier = PERMISSIONS_BY_TRUST[1.0]
        elif trust >= 0.5:
            tier = PERMISSIONS_BY_TRUST[0.5]
        else:
            tier = PERMISSIONS_BY_TRUST[0.0]
        return tier

    def adapt_context(self, person: Person, base_context: dict) -> dict:
        """Merges behavior_profile + information_permissions into
        base_context, and — when the speaker isn't Joan — strips the
        personal-memory-shaped keys Phase 2-5 add (situation snapshot's
        active_tasks/pending_topics, relevant facts, episodes) so a
        friend/stranger's context never carries Joan's private state
        through to whatever consumes base_context next (e.g. the system
        prompt builder — see core.personalities.base's own integration)."""
        behavior = self.get_behavior_profile(person.id)
        permissions = self.get_information_permissions(person.id)
        adapted = dict(base_context)
        adapted["behavior_profile"] = behavior
        adapted["information_permissions"] = permissions

        if not permissions.can_access_joan_memory:
            for key in ("situacion", "situation", "relevant_facts", "episodes", "active_tasks", "pending_topics"):
                adapted.pop(key, None)
        return adapted

    def introduce_hugo(self, person: Person) -> str:
        """Returns ONLY what to say — the 'does not introduce herself
        automatically, waits to be addressed' gating (spec) is the
        caller's job (see the pipeline integration), since that's about
        WHEN this gets called, not what it returns. person is accepted
        (per the class interface) but the template is currently the same
        regardless of who's asking — 'Soy HUGO.', nothing more, per spec's
        own explicit example; Joan expanding it later
        ('Puedes hablar con ella normal') is a trust_level change, handled
        by POST /api/social/people/<id>/trust, not a different string
        here."""
        data = _load()
        return data.get("introduction_template", "Soy HUGO.")

    def acknowledge_known(self, person_id: str) -> None:
        """Marks knows_hugo=True after a first acknowledged interaction —
        spec: 'Marks person as knows_hugo: true after first acknowledged
        interaction'. Separate from update_interaction() since this is a
        one-time flag flip, not a per-turn bookkeeping call."""
        with _lock:
            data = _load()
            record = data["people"].get(person_id)
            if record is not None and not record.get("knows_hugo"):
                record["knows_hugo"] = True
                _save_locked(data)

    # ── secret protection — hard filter, never LLM judgment ─────────────

    def _protect_secrets(self, response: str, permissions: InfoPermissions) -> str:
        """Runs on every response when the speaker isn't Joan (see pipeline
        integration). Sentence-level regex filter, not a rewrite — a
        sentence that trips any pattern below is dropped whole rather than
        edited, since a partial edit could leave a dangling, confusing
        fragment. Never touches a response when speaker IS Joan (callers
        only invoke this for non-Joan speakers in the first place — see
        core.personalities.base / core.discord_bridge)."""
        if not response:
            return response
        sentences = re.split(r"(?<=[.!?])\s+", response)
        kept = []
        for sentence in sentences:
            low = sentence.lower()
            if not permissions.can_access_joan_memory and _PERSONAL_MEMORY_LEAK_RE.search(low):
                continue
            if not permissions.can_access_joan_schedule and _SCHEDULE_LEAK_RE.search(low):
                continue
            if not permissions.can_access_joan_projects and _PROJECT_LEAK_RE.search(low):
                continue
            if not permissions.hugo_acknowledges_knowing_joan and _RELATIONSHIP_ACK_RE.search(low):
                continue
            kept.append(sentence)
        sanitized = " ".join(kept).strip()
        return sanitized if sanitized else "Prefiero no entrar en eso."


# ── behavior/permission tables ──────────────────────────────────────────────

BEHAVIOR_BY_RELATIONSHIP: dict[str, BehaviorProfile] = {
    "self":       BehaviorProfile(tone="casual",  response_length="normal", information_sharing="full",    hugo_personality_mode="normal"),
    "family":     BehaviorProfile(tone="warm",     response_length="brief",  information_sharing="limited", hugo_personality_mode="friendly"),
    "friend":     BehaviorProfile(tone="casual",  response_length="brief",  information_sharing="limited", hugo_personality_mode="friendly"),
    "colleague":  BehaviorProfile(tone="professional", response_length="brief", information_sharing="minimal", hugo_personality_mode="professional"),
    "acquaintance": BehaviorProfile(tone="neutral", response_length="minimal", information_sharing="minimal", hugo_personality_mode="reserved"),
    "stranger":   BehaviorProfile(tone="neutral",  response_length="minimal", information_sharing="minimal", hugo_personality_mode="reserved"),
}

PERMISSIONS_BY_TRUST: dict[float, InfoPermissions] = {
    1.0: InfoPermissions(   # Joan
        can_access_joan_schedule=True, can_access_joan_projects=True, can_access_joan_memory=True,
        can_ask_hugo_personal_questions=True, hugo_acknowledges_knowing_joan=True,
    ),
    0.5: InfoPermissions(   # trusted friend
        can_access_joan_schedule=False, can_access_joan_projects=False, can_access_joan_memory=False,
        can_ask_hugo_personal_questions=False, hugo_acknowledges_knowing_joan=True,
    ),
    0.0: InfoPermissions(   # stranger
        can_access_joan_schedule=False, can_access_joan_projects=False, can_access_joan_memory=False,
        can_ask_hugo_personal_questions=False, hugo_acknowledges_knowing_joan=False,
    ),
}

# Sentence-level leak patterns — deliberately coarse keyword/phrase regexes
# (same discipline as core/intent.py's own patterns), not an attempt at
# perfect NLP filtering. A false-positive drop (an innocuous sentence
# removed) is the safe failure direction; a false negative (a real leak
# kept) is not, so these lean broad on purpose.
_PERSONAL_MEMORY_LEAK_RE = re.compile(
    r"\bjoan\s+(?:me\s+dijo|mencion[oó]|me\s+cont[oó]|prefiere|odia|le\s+gusta|est[aá]\s+trabajando)\b|"
    r"\brecuerdo\s+que\s+joan\b|\bseg[uú]n\s+lo\s+que\s+joan\b",
    re.IGNORECASE,
)
_SCHEDULE_LEAK_RE = re.compile(
    r"\bjoan\s+tiene\s+(?:una\s+)?(?:reuni[oó]n|cita|evento)\b|\bla\s+agenda\s+de\s+joan\b|\bel\s+calendario\s+de\s+joan\b",
    re.IGNORECASE,
)
_PROJECT_LEAK_RE = re.compile(
    r"\bel\s+proyecto\s+de\s+joan\b|\bsu\s+casco\b|\bsu\s+armadura\b|\btarea\s+de\s+joan\b|\bjoan\s+est[aá]\s+construyendo\b",
    re.IGNORECASE,
)
_RELATIONSHIP_ACK_RE = re.compile(
    r"\bconozco\s+a\s+joan\b|\bsoy\s+la\s+asistente\s+de\s+joan\b|\btrabajo\s+(?:para|con)\s+joan\b|"
    r"\bjoan\s+es\s+mi\b",
    re.IGNORECASE,
)


_presence_lock = threading.Lock()
_last_presence: dict | None = None   # {"person": Person, "at": datetime}
PRESENCE_TTL_SECONDS = 15 * 60   # a speaker not re-identified within 15 min is no longer "present"


def _mark_present(person: Person) -> None:
    global _last_presence
    with _presence_lock:
        _last_presence = {"person": person, "at": datetime.datetime.now()}


social_engine = SocialEngine()
