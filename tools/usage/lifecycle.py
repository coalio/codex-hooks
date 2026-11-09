"""Fast standard-library-only bridge from Codex lifecycle events to the worker."""

import time

from .state import Store, thread_key


def record_event(payload, store=None):
    event = payload.get("hook_event_name")
    if event not in ("Stop", "Interrupt", "SessionEnd"):
        return {}
    thread_id = thread_key(payload.get("session_id"))
    store = store or Store()
    hook = store.read("hook")
    if hook and hook["thread_id"] == thread_id:
        # The hook generation prevents a delayed event from revoking a newer worker.
        store.write("event-" + thread_id, {"hook_id": hook["id"], "event": event,
                                          "turn_id": payload.get("turn_id"), "at": time.time()})
    return {}
