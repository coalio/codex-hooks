"""Detached interval worker; all monitoring and reset decisions are programmatic."""

from __future__ import annotations

import argparse
from pathlib import Path
import signal
import sys
import threading
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.usage.protocol import Client
from tools.usage.lifecycle import event_for_hook
from tools.usage.service import PollDeferred, UsageService
from tools.usage.state import Store


class Lifecycle:
    def __init__(self):
        self.idle_since = None

    def check(self, hook, event, thread_state, now):
        # Immediate events revoke authority even while the app server is unavailable.
        if event.get("hook_id") == hook["id"] and event.get("event") in ("Interrupt", "SessionEnd", "expire"):
            return "session_stopped"
        if hook["keep_running"]:
            return None
        if thread_state not in ("idle", "notLoaded"):
            self.idle_since = None
            return None
        # A grace interval bridges the idle gap between a Stop review and its continuation.
        if self.idle_since is None:
            self.idle_since = now
        if now - self.idle_since >= 10:
            return "work_completed"
        return None


def run(hook_id, store=None):
    store = store or Store()
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    lifecycle, client, next_poll, reconnect_at = Lifecycle(), None, 0, 0
    failures = 0
    try:
        while not stopped.is_set():
            # Check ownership before connecting, so an expired or replaced worker cannot return.
            hook = store.read("hook")
            if not hook or hook["id"] != hook_id:
                return
            event = event_for_hook(store, hook)
            immediate = ("session_stopped" if event.get("hook_id") == hook_id and
                         event.get("event") in ("Interrupt", "SessionEnd", "expire") else None)
            if immediate:
                UsageService(store, None, hook["thread_id"]).expire(immediate)
                return
            if time.monotonic() < reconnect_at:
                stopped.wait(1)
                continue
            try:
                if client is None:
                    client = Client(hook["endpoint"]).open()
                # Status reads are local app-server calls, independent of the upstream poll budget.
                state = client.thread_state(hook["thread_id"])
                reason = lifecycle.check(hook, event, state, time.monotonic())
                if reason:
                    UsageService(store, client, hook["thread_id"]).expire(reason)
                    return
                if time.monotonic() >= next_poll:
                    result = UsageService(store, client, hook["thread_id"]).tick(hook_id)
                    if result["status"] == "expired":
                        return
                    next_poll = time.monotonic() + 60
                    store.write("worker_status", {"status": "running", "hook_id": hook_id})
                failures = 0
            except PollDeferred as error:
                next_poll = time.monotonic() + error.retry_after
            except Exception as error:
                # Do not log provider payloads, tokens, or launch another model to recover.
                failures = min(failures + 1, 8)
                reconnect_at = time.monotonic() + max(2 ** failures, getattr(error, "retry_after", 0))
                store.write("worker_status", {"status": "suspended", "error_type": type(error).__name__,
                                              "hook_id": hook_id, "retry_in_seconds": reconnect_at - time.monotonic()})
                lifecycle.idle_since = None
                if client:
                    client.close()
                    client = None
            stopped.wait(2)
    finally:
        if client:
            client.close()
        # Never remove another worker's ownership after replacement or explicit expiration.
        with store.guard():
            hook = store.read("hook")
            if hook and hook["id"] == hook_id:
                hook.update(status="expired", reason="worker_stopped")
                store.write("last_hook", UsageService._public(hook))
                store.delete("hook")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hook-id", required=True)
    run(parser.parse_args().hook_id)
