# active_person.py — which person (a core.social profile id, e.g. 'joan' or
# 'dani') HUGO is currently answering, for the duration of one turn. Read by
# core.groq_config and core.tools_search to bill that person's own API key
# instead of always using the shared/default one — see core.commands'
# _dispatch_command_impl, which sets this right after identify_person()
# resolves who's actually present, before any Groq/search call happens.
#
# A tiny leaf module on purpose — no imports from core.social/core.commands
# — so groq_config.py and tools_search.py can both depend on it without any
# circular-import risk.
#
# threading.local rather than a plain module-level variable: Flask/SocketIO
# serve concurrent requests on different worker threads, and a value must
# never leak from one person's turn into another's on a reused thread. Each
# new OS thread starts with its own empty local storage (get_active_person()
# defaults to None), so background threads (sleep cycles, memory extraction)
# naturally fall back to the shared/default key unless they set this
# themselves.
import threading

_local = threading.local()


def set_active_person(person_id: str | None) -> None:
    _local.person_id = person_id


def get_active_person() -> str | None:
    return getattr(_local, "person_id", None)
