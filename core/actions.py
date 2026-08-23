# ═══════════════════════════════════════════════════════════════════════════
# ACTIONS — deterministic tool-action execution (volume, open-app, calendar,
# reminders) for intents detected by core/intent.py. Split out of
# core/commands.py (pure refactor, no behavior change).
#
# Three-level action philosophy (see core/commands.py's own module comment
# for the full picture) — every handler here returns HUGO's FINAL spoken
# reply directly, not a raw result for _format_response()/Groq to phrase:
#   Level 1 (execute immediately)   — _execute_volume_control,
#     _execute_open_app, _execute_calendar_write, _execute_reminder_create,
#     _execute_start_investigation. A direct, unambiguous order with enough
#     detail: just do it, confirm briefly.
#   Level 3 (propose, then confirm) — _execute_calendar_propose,
#     _execute_reminder_propose, _execute_app_open_propose, plus
#     core.commands.generate_summary/generate_schema's own Estudio-save
#     proposal. Something merely IMPLIED in conversation: prepare it, stash
#     it in intent_mod._pending_action, ask naturally — never act on it
#     until _execute_pending_confirm sees it confirmed on the very next
#     turn (see core.intent._detect_intent's pending-action staleness
#     check for the "drops it silently after next message" rule).
# calendar_read is the one exception among these — it stays on the
# ORIGINAL get_time/get_date-style path (raw result → _format_response() →
# Groq), per spec: "Calendar reading injects results into Groq context for
# natural response". Level 2 (direct order requiring review before final
# execution — 'prepara un correo', 'redacta un mensaje') has no handler
# here yet: no email/message/document capability exists in this codebase
# to review before sending — see data/memory_instructions.json's "NO
# PUEDES" line. The propose/confirm machinery below is exactly what a
# future Level 2 handler would reuse once one exists.
# ═══════════════════════════════════════════════════════════════════════════
import datetime
import logging
import threading
import time

from core import memory
from core import tools
from core import intent as intent_mod
from core import investigations
from core.response import _pf

logger = logging.getLogger(__name__)

_NO_GROQ_INTENTS = {
    "volume_control", "open_app", "calendar_write",
    "reminder_create", "calendar_propose", "reminder_propose",
    "app_open_propose",
}
# pending_confirm is deliberately NOT in this set (bug fix — it used to be):
# _execute_pending_confirm's replies were fixed deterministic strings
# ("Hecho.", "Guardado en Estudio.") spoken completely verbatim, unlike
# every other action here, which at least confirms in HUGO's own template
# phrasing. Routing it through the normal result -> _format_response()
# path instead (same as the `else` branch below) means every confirmation
# now gets phrased naturally in context, same treatment
# core.commands._phrase_skill_result already gives skill results.
# start_investigation is also NOT in this set for the same reason (bug fix
# — it used to be): the fire-and-forget background thread that does the
# actual research (see _run_first_incubation_cycle) already means nothing
# on the conversation path waits on the investigation itself, so there was
# never a latency reason to keep the CONFIRMATION line hardcoded too — see
# _execute_start_investigation's own docstring.


def _execute_volume_control(parameters: dict) -> str:
    """Runs the requested volume action via core.tools and returns HUGO's
    final spoken reply. Every core.tools volume call already fails
    gracefully (None/False, never a raised exception, verified against a
    real Mac where the numeric level silently refuses to change on some
    audio configurations — see set_volume()'s own docstring) — this just
    turns that into a natural line instead of a silent no-op."""
    action = parameters.get("action")
    if action == "get":
        level = tools.get_volume()
        if level is None:
            return _pf("No he podido leer el volumen, señor.",
                        "No he podido leer el volumen ahora mismo.",
                        "No consigo leer el volumen — algo falla.")
        return _pf(f"El volumen está al {level}%, señor.",
                    f"El volumen está al {level}%.",
                    f"Volumen al {level}%, por si te interesa.")
    if action == "set":
        target = parameters.get("level", 50)
        if tools.set_volume(target):
            return _pf(f"Volumen ajustado al {target}%, señor.", f"Volumen al {target}%.", f"Volumen al {target}%, hecho.")
        return _pf("No he podido cambiar el volumen, señor — puede que falten permisos.",
                    "No he podido cambiar el volumen — puede que falten permisos.",
                    "No me ha dejado tocar el volumen — cosas de permisos, seguramente.")
    if action == "up":
        new_level = tools.volume_up(parameters.get("amount", 10))
        if new_level is None:
            return _pf("No he podido subir el volumen, señor.", "No he podido subir el volumen.", "No he podido subirlo, lo siento.")
        return _pf(f"Volumen subido al {new_level}%, señor.", f"Volumen subido al {new_level}%.", f"Ahí va, {new_level}%.")
    if action == "down":
        new_level = tools.volume_down(parameters.get("amount", 10))
        if new_level is None:
            return _pf("No he podido bajar el volumen, señor.", "No he podido bajar el volumen.", "No he podido bajarlo, lo siento.")
        return _pf(f"Volumen bajado al {new_level}%, señor.", f"Volumen bajado al {new_level}%.", f"Bajado a {new_level}%.")
    if action == "mute":
        if tools.mute_system():
            return _pf("Silenciado, señor.", "Silenciado.", "Mudo total.")
        return _pf("No he podido silenciar el Mac, señor.", "No he podido silenciarlo.", "No he podido silenciarlo — algo falla.")
    if action == "unmute":
        if tools.unmute_system():
            return _pf("Sonido restaurado, señor.", "Sonido activado de nuevo.", "Vuelve el sonido.")
        return _pf("No he podido quitar el silencio, señor.", "No he podido quitar el silencio.", "No he podido quitar el silencio, vaya.")
    return _pf("No he entendido qué quiere que haga con el volumen, señor.",
                "No he entendido qué quieres que haga con el volumen.",
                "No pillo qué quieres que haga con el volumen.")


def _execute_open_app(parameters: dict) -> str:
    """Opens an app via core.tools.open_app and returns HUGO's final spoken
    reply. A False return (not installed, typo, anything else) becomes a
    natural 'no la encuentro' line — never a crash."""
    name = (parameters.get("name") or "").strip()
    if not name:
        return _pf("¿Qué aplicación desea que abra, señor?", "¿Qué app quieres que abra?", "Vas a tener que decirme cuál.")
    resolved = tools.resolve_app_name(name)
    if tools.open_app(name):
        return _pf(f"Abriendo {resolved}, señor.", f"Abriendo {resolved}.", f"Va, abriendo {resolved}.")
    return _pf(f"No he encontrado {resolved} en este Mac, señor.",
                f"No encuentro {resolved} — ¿seguro que está instalada?",
                f"{resolved} no aparece por aquí — revisa si está instalada.")


def _execute_calendar_read(parameters: dict) -> str:
    """Returns a compact RAW text summary of the requested range's
    events — NOT HUGO's final reply (calendar_read isn't in
    _NO_GROQ_INTENTS; _format_response()/Groq phrases this naturally
    afterward, same treatment as get_time/get_date above)."""
    range_ = parameters.get("range", "today")
    events = tools.get_today_events() if range_ == "today" else tools.get_week_events()
    if not events:
        return "No hay eventos programados hoy." if range_ == "today" else "No hay eventos programados en los próximos 7 días."
    return "\n".join(f"{e['date']} {e['time']} — {e['title']}" for e in events)


def _execute_calendar_write(parameters: dict) -> str:
    """Level 1 — explicit, direct order with a trigger verb ('crea/pon/
    agenda un evento...'). Per the three-level action philosophy: a direct
    order with enough detail just gets done, no confirmation round trip —
    creates the event immediately and confirms briefly. Still asks for
    clarification (never guesses) if date or time genuinely couldn't be
    parsed from the transcript — that's missing detail, not something
    Level 1 covers, and this returns without touching Calendar.app at all
    in that case."""
    date     = parameters.get("date")
    time_str = parameters.get("time")
    title    = parameters.get("title", "Evento")
    duration = parameters.get("duration", 60)

    if date is None or time_str is None:
        missing = "la fecha" if date is None else "la hora"
        return _pf(f"No he entendido {missing} del evento, señor — ¿puede repetirlo con más detalle?",
                    f"No he pillado {missing} del evento — ¿me lo repites con más detalle?",
                    f"Se me ha escapado {missing} del evento — repítemelo, anda.")

    ok = tools.create_event(title=title, date=date.isoformat(), time=time_str, duration=duration)
    if ok:
        return _pf("Evento añadido, señor.", "Hecho.", "Evento añadido.")
    return _pf("No he podido crear el evento, señor — puede que falten permisos de Calendario.",
                "No he podido crearlo — revisa los permisos de Calendario en Ajustes del Sistema.",
                "Se me ha resistido — probablemente un tema de permisos de Calendario.")


def _execute_reminder_create(parameters: dict) -> str:
    """Level 1 — explicit direct order ('recuérdame que...', 'crea un
    recordatorio para...'). Stores the reminder immediately (see
    core.reminders._add_reminder) and confirms briefly, same treatment as
    _execute_calendar_write above."""
    text = (parameters.get("text") or "").strip(" .,!?¡¿")
    if not text:
        return _pf("¿Qué quiere que le recuerde, señor?",
                    "¿Qué quieres que te recuerde?",
                    "¿Recordarte el qué, exactamente?")

    from core import reminders as reminders_mod
    from core import personality as personality_mod
    with personality_mod._personality_lock:
        current_p = personality_mod._personality

    minutes = reminders_mod._parse_relative_minutes(text)
    if minutes is not None:
        text = reminders_mod._strip_duration_phrase(text)
        trigger_at = (datetime.datetime.now() + datetime.timedelta(minutes=minutes)).isoformat(timespec="seconds")
        reminders_mod._add_reminder(text, current_p, "time", trigger_at)
    else:
        reminders_mod._add_reminder(text, current_p, "session", None)

    return _pf("Recordatorio guardado, señor.", "Hecho.", "Recordatorio guardado.")


def _execute_calendar_propose(parameters: dict) -> str:
    """Level 3 — implied action detected in conversation (see
    core.intent's calendar-implied patterns: 'tengo que ir a X mañana a
    las Y', 'quedamos el viernes', 'reunión el lunes'). Prepares the event
    and asks naturally, without creating anything yet — stashed in
    intent_mod._pending_action, only actually created once confirmed via
    _execute_pending_confirm on the immediately following turn, dropped
    silently otherwise."""
    title    = parameters.get("title", "Evento")
    date     = parameters["date"]   # core.intent only ever returns this intent once a date parsed
    time_str = parameters.get("time")
    duration = parameters.get("duration", 60)

    intent_mod._pending_action = {
        "kind": "calendar_event",
        "data": {"title": title, "date": date, "time": time_str, "duration": duration},
        "at": time.monotonic(),
    }
    date_str = f"{memory._DAYS_ES[date.weekday()]} {date.day} de {memory._MONTHS_ES[date.month - 1]}"
    when = f"el {date_str}" + (f" a las {time_str}" if time_str else "")
    return _pf(
        f"He preparado un evento — {title} {when}. ¿Lo añado, señor?",
        f"He preparado un evento — {title} {when}. ¿Lo añado?",
        f"Te lo apunto en el calendario si quieres — {title} {when}.",
    )


def _execute_reminder_propose(parameters: dict) -> str:
    """Level 3 — implied reminder detected in conversation ('no me olvides
    que...', 'tengo que acordarme de...'). Prepares it and asks — matches
    spec's own phrasing ('¿Cuándo?' rather than a plain yes/no, since no
    timing was mentioned yet). A bare 'sí' on the next turn still works via
    the universal confirm rule — see _execute_pending_confirm's reminder
    branch, which falls back to a session-based reminder (same default as
    the explicit Level 1 path) when no time phrase is in the confirming
    message either."""
    text = parameters["text"]   # core.intent only returns this intent with non-empty text

    intent_mod._pending_action = {
        "kind": "reminder",
        "data": {"text": text},
        "at": time.monotonic(),
    }
    return _pf(
        f"Le preparo un recordatorio para {text}, señor. ¿Cuándo se lo recuerdo?",
        f"Te preparo un recordatorio para {text}. ¿Cuándo?",
        f"¿Te lo recuerdo? Dime cuándo y lo dejo listo.",
    )


def _execute_app_open_propose(parameters: dict) -> str:
    """Level 3 — implied app-open mention ('quiero poner música', 'necesito
    ver el calendario'). Asks before opening rather than launching it
    outright, unlike the explicit open_app intent above (Level 1: 'abre
    Spotify' is a direct order, this is just a mention)."""
    app_key  = parameters.get("app_key", "")
    resolved = tools.resolve_app_name(app_key)

    intent_mod._pending_action = {
        "kind": "app_open",
        "data": {"name": app_key},
        "at": time.monotonic(),
    }
    return _pf(
        f"¿Le abro {resolved}, señor?",
        f"Te abro {resolved} si quieres.",
        f"¿Abro {resolved} o no hace falta?",
    )


def _execute_pending_confirm(parameters: dict) -> str:
    """Generic Level-3 confirmation handler — the single entry point every
    *_propose function above (plus core.commands.generate_summary/
    generate_schema's own Estudio-save proposal) eventually resolves
    through. Dispatches on intent_mod._pending_action['kind'] to actually
    perform (or cancel) the previously-prepared action. Always clears the
    pending slot first — a stale proposal can never be confirmed twice."""
    pending = intent_mod._pending_action
    intent_mod._pending_action = None

    if pending is None:
        return _pf("No hay nada pendiente de confirmar, señor.",
                    "No tengo nada pendiente de confirmar.",
                    "No hay nada pendiente, que yo sepa.")

    # install_package_approval needs to run its own cleanup (unblock/fail
    # the TaskEngine task, pop the pending-install record) on BOTH answers,
    # not just confirmed=True — every kind below it only ever does
    # something on confirmation, so the generic "not confirmed" early
    # return two lines down is correct for them but would silently orphan
    # this one's task/record on a plain "no". Intercepted here, before that
    # early return, for that reason alone.
    if pending["kind"] == "install_package_approval":
        return _execute_install_package_confirm(pending["data"], parameters)

    if not parameters.get("confirmed"):
        return _pf("Entendido, lo dejo así, señor.", "Vale, lo dejo.", "Perfecto, olvidado.")

    kind = pending["kind"]
    data = pending["data"]

    if kind == "calendar_event":
        ok = tools.create_event(
            title=data["title"], date=data["date"].isoformat(),
            time=data.get("time") or "12:00", duration=data.get("duration", 60),
        )
        if ok:
            return _pf("Hecho.", "Hecho.", "Hecho.")
        return _pf("No he podido crearlo — revisa los permisos de Calendario.",
                    "No he podido crearlo — revisa los permisos de Calendario.",
                    "No he podido crearlo — revisa los permisos de Calendario.")

    if kind == "reminder":
        from core import reminders as reminders_mod
        from core import personality as personality_mod
        with personality_mod._personality_lock:
            current_p = personality_mod._personality

        text    = data["text"]
        raw     = parameters.get("raw_transcript", "")
        minutes = reminders_mod._parse_relative_minutes(raw)
        if minutes is not None:
            trigger_at = (datetime.datetime.now() + datetime.timedelta(minutes=minutes)).isoformat(timespec="seconds")
            reminders_mod._add_reminder(text, current_p, "time", trigger_at)
        else:
            reminders_mod._add_reminder(text, current_p, "session", None)
        return _pf("Hecho.", "Hecho.", "Hecho.")

    if kind == "app_open":
        name     = data["name"]
        resolved = tools.resolve_app_name(name)
        if tools.open_app(name):
            return _pf(f"Abriendo {resolved}.", f"Abriendo {resolved}.", f"Abriendo {resolved}.")
        return _pf(f"No encuentro {resolved} — ¿seguro que está instalada?",
                    f"No encuentro {resolved} — ¿seguro que está instalada?",
                    f"No encuentro {resolved} — ¿seguro que está instalada?")

    # estudio_schema used to route through here too — generate_schema() now
    # saves immediately (Level 1, not Level 3; see its own docstring in
    # core/commands.py for why a directly-requested schema doesn't need a
    # confirm-before-persist round trip), so only summaries still propose.
    if kind == "estudio_summary":
        # Reaches back into core.commands via a function-local import to
        # avoid a circular import — commands.py already imports this
        # module at the top level (see its own module comment for the same
        # pattern used elsewhere in this codebase, e.g. background_loops.py).
        import core.commands as commands_mod
        commands_mod._append_json_record(commands_mod._SUMMARIES_PATH, data["record"])
        commands_mod._emit_estudio_updated("resumenes")
        return _pf("Guardado en Estudio.", "Guardado en Estudio.", "Guardado en Estudio.")

    return _pf("Hecho.", "Hecho.", "Hecho.")


def _execute_install_package_confirm(data: dict, parameters: dict) -> str:
    """Resolves a DependencyManager install-approval prompt (see
    core.code_engine.tools.dependency_manager._request_install_approval) —
    the Claude-Code-style 'ask before running something with real effect'
    flow, applied to package installs specifically. `data` is the payload
    that notification carried (task_id, package, path, name); `parameters`
    is the same dict every _propose/*confirm handler gets, so `confirmed`
    and `raw_transcript` are both available here exactly like the reminder
    branch above uses `raw_transcript` for its own timing phrase.

    Three real outcomes, not two — matching how Joan can actually answer:
      - denied: task fails, pending record dropped, nothing installed.
      - approved: task completes, install runs in the background (can take
        up to 300s — see DependencyManager._do_install()'s own timeout;
        never block this reply on it, same fire-and-forget reasoning as
        core.code_engine_dispatch.dispatch_module_task).
      - approved AND the confirming phrase contains 'siempre': same as
        above, but the package is also added to the trusted allowlist
        first, so it never asks again."""
    from core.code_engine.tools import dependency_manager as dm_mod
    from core.task_engine import task_engine
    from core import personality as personality_mod

    with personality_mod._personality_lock:
        current_p = personality_mod._personality
    raw_transcript = parameters.get("raw_transcript", "")

    def _reply(result: str, fallback: str) -> str:
        """Phrases `result` through the same 'raw fact -> HUGO's own voice'
        helper every other tool result already uses (core.response.
        _format_response, Groq-based) instead of a hand-written per-
        personality template. `fallback` only ever speaks if that call
        itself fails. No `transcript` passed — that parameter means "the
        original command that led here" everywhere else it's used, and
        the confirming word itself ('sí'/'no') fed in as if it were that
        confused the phrasing (confirmed live: it started asking Joan what
        command she meant). `result` is already a complete, self-contained
        fact, so nothing is lost by leaving it out."""
        try:
            from core.response import _format_response
            return _format_response(result, personality=current_p)
        except Exception:
            return fallback

    task_id, package, path, name = data.get("task_id"), data.get("package"), data.get("path"), data.get("name")
    record = dm_mod.pop_pending_install(task_id) if task_id else None

    if not parameters.get("confirmed"):
        if task_id:
            task_engine.fail_task(task_id, f"Joan denegó la instalación de {name}.")
        return _reply(f"Instalación de '{name}' cancelada — no se instalará.", f"Entendido, no instalo {name}.")

    if record is None or not package or not path:
        return _reply(
            f"La solicitud de instalación de '{name}' ya no está disponible — puede haber expirado.",
            "No encuentro los detalles de esa instalación pendiente — puede que ya haya expirado.",
        )

    always = "siempre" in raw_transcript.lower()
    if always:
        dm_mod.add_trusted_package(name, "pypi")

    def _run():
        try:
            venv_python = dm_mod.DependencyManager()._find_venv_python(path)
            ok = dm_mod.DependencyManager()._do_install(path, venv_python, package) if venv_python else False
        except Exception:
            ok = False
        if ok:
            task_engine.complete_task(task_id)
        else:
            task_engine.fail_task(task_id, f"pip install falló para {name}")
        from core import notifications as notifications_mod
        notifications_mod.create_notification(
            "code_engine",
            f"{'Instalado' if ok else 'Falló la instalación'}: {name}",
            f"{name} {'se instaló correctamente' if ok else 'no se pudo instalar'}.",
        )

    import threading
    threading.Thread(target=_run, daemon=True, name="dependency-install-approved").start()

    if always:
        return _reply(
            f"Instalando el paquete '{name}' en segundo plano. Se ha añadido a la lista de paquetes de confianza, "
            f"así que no se volverá a preguntar por él.",
            f"Instalando {name} — y confiaré en él automáticamente la próxima vez.",
        )
    return _reply(f"Instalando el paquete '{name}' en segundo plano.", f"Instalando {name}.")


def _generate_investigation_title(topic: str) -> str | None:
    """Ollama pass (same local/free call as the rest of the Sleep System's
    own title generation — core.sleep_curiosity_search._synthesize_topic_finding,
    core.skill_forge._generate_title) that turns the raw spoken topic into a
    short ESTUDIO card title, e.g. 'quiero saber si el nuevo protocolo X
    afecta al rendimiento del servidor' → 'Impacto del protocolo X en
    rendimiento'. None on any failure — caller keeps the raw-topic title
    (create_investigation's own fallback) rather than blocking on this."""
    from core.sleep_llm import _ollama_generate
    system = (
        "Generas títulos breves para investigaciones que Joan le pide a "
        "HUGO. Responde EXCLUSIVAMENTE con el título — sin comillas, sin "
        "puntuación final, sin explicación — máximo 8 palabras."
    )
    text = _ollama_generate(system, f"Tema: {topic}", max_tokens=30)
    if not text:
        return None
    title = text.strip().strip('"').strip("«»").splitlines()[0].strip()
    return title[:80] or None


def _run_first_incubation_cycle(inv: dict) -> None:
    """Fire-and-forget: gives a brand-new investigation its first reasoning
    cycle right away instead of leaving it as an empty placeholder until
    whenever the next sleep session happens to run — same
    'threading.Thread(daemon=True)' pattern as
    core.memory_extract._extract_and_save_memory, for the same reason (the
    reply above is already on its way back to Joan; nothing in the
    conversation path should wait on this).

    Also gives the investigation a proper title here (see
    _generate_investigation_title) rather than inside create_investigation
    itself — that call sits on the conversation path and must stay
    deterministic/instant (see _execute_start_investigation's own
    docstring); this thread is exactly where a slow local Ollama call
    belongs. Saved and emitted separately, before the (possibly much
    longer) incubation cycle below, so the ESTUDIO card gets its real title
    promptly instead of waiting on the whole first research pass.

    Reuses core.sleep_phases_incubation._run_incubation_cycle verbatim —
    the exact same multi-source-search-then-reason logic the Sleep System's
    🧪 Incubación phase runs overnight — rather than a second, parallel
    research implementation that could drift out of sync with it. Sleep
    still picks the investigation back up on its own normal schedule
    afterward (see core/investigations.py's ACTIVE_STATUSES) and keeps
    refining it; this just means Joan doesn't have to wait for a sleep
    cycle to get a first pass. Budget matches PHASE_3_INCUBATION's own
    per-session budget (see core/sleep_state.py) since this is standing in
    for exactly one sleep session's worth of work on exactly one
    investigation, not the up-to-3-way split a real sleep session divides
    it into."""
    import core.commands as commands_mod
    try:
        better_title = _generate_investigation_title(inv.get("question") or inv.get("title") or "")
        if better_title:
            inv["title"] = better_title
            investigations.save_investigation(inv)
            commands_mod._emit_estudio_updated("investigaciones")
    except Exception:
        logger.warning("Investigation title generation failed (non-critical)", exc_info=True)
    try:
        from core.sleep_phases_incubation import _run_incubation_cycle
        _run_incubation_cycle(inv, budget=400)
        commands_mod._emit_estudio_updated("investigaciones")
    except Exception:
        # Non-critical — sleep's own Incubación phase will pick this
        # investigation up on its normal schedule regardless (see
        # ACTIVE_STATUSES), same as any other cycle that fails mid-run.
        logger.warning("Immediate incubation cycle failed (non-critical)", exc_info=True)


def _execute_start_investigation(parameters: dict) -> str:
    """'investiga X' / 'quiero saber sobre X' / 'analiza X en profundidad' —
    starts an investigation (core/investigations.py) and gives it an
    immediate first research pass in the background (see
    _run_first_incubation_cycle) — searching multiple sources and forming
    initial hypotheses right away, not just leaving an empty placeholder
    for whenever sleep next runs. The Sleep System's Incubación phase (see
    core/sleep_phases_incubation.py) keeps refining it further overnight.
    Returns a raw result for the caller to phrase naturally via
    response._format_response (bug fix — this used to be a hardcoded
    _pf() triple, identically worded every time; see _NO_GROQ_INTENTS'
    comment on why start_investigation was pulled out of that set). This
    is safe to route through Groq because the actual research never sits
    on this path — it already runs in the background thread started
    below, before this function even returns.

    Emits 'estudio_updated' right after creating the entry — same live-
    refresh treatment as _execute_pending_confirm's estudio_summary branch
    (and core.commands.generate_schema's own direct save) above, which this
    was missing until now: without it, a new card only ever appeared once
    Joan left ESTUDIO and came back (ui/js/section-nav.js refetches on
    every section-open regardless), never live while already sitting on
    the INVESTIGACIÓN tab. Reaches back into core.commands via a
    function-local import for the same circular-import reason as that
    branch."""
    topic = (parameters.get("topic") or "").strip()
    if not topic:
        return "Joan no ha especificado sobre qué investigar — pídele que aclare el tema."
    inv = investigations.create_investigation(topic)
    import core.commands as commands_mod
    commands_mod._emit_estudio_updated("investigaciones")
    threading.Thread(
        target=_run_first_incubation_cycle, args=(inv,), daemon=True, name="investigation-first-cycle",
    ).start()
    return f"Investigación iniciada sobre: {topic}. Investigando en segundo plano — se avisará cuando haya resultados."


def _execute_action(intent: str, parameters: dict):
    now = datetime.datetime.now()
    if intent == "get_time":
        return now.strftime("%H:%M")
    if intent == "get_date":
        return (
            f"{memory._DAYS_ES[now.weekday()]}, "
            f"{now.day} de {memory._MONTHS_ES[now.month - 1]} "
            f"de {now.year}"
        )
    if intent == "volume_control":
        return _execute_volume_control(parameters)
    if intent == "open_app":
        return _execute_open_app(parameters)
    if intent == "calendar_read":
        return _execute_calendar_read(parameters)
    if intent == "calendar_write":
        return _execute_calendar_write(parameters)
    if intent == "reminder_create":
        return _execute_reminder_create(parameters)
    if intent == "calendar_propose":
        return _execute_calendar_propose(parameters)
    if intent == "reminder_propose":
        return _execute_reminder_propose(parameters)
    if intent == "app_open_propose":
        return _execute_app_open_propose(parameters)
    if intent == "pending_confirm":
        return _execute_pending_confirm(parameters)
    if intent == "start_investigation":
        return _execute_start_investigation(parameters)
    return None
