"""Usage actions and the reset policy, shared by MCP and the background worker."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from .protocol import Client, UsageError, local_tcp_servers, remaining_usage, reset_count, stopped_on_local_servers
from .state import process_alive, process_identity, stop_process, thread_key

POLL_SECONDS = 60


class PollDeferred(UsageError):
    def __init__(self, seconds):
        super().__init__("The shared usage polling interval has not elapsed")
        self.retry_after = max(1, seconds)


class UsageService:
    def __init__(self, store, client, thread_id, clock=time.time, poll_seconds=POLL_SECONDS):
        self.store, self.client = store, client
        self.thread_id, self.clock, self.poll_seconds = thread_key(thread_id), clock, poll_seconds

    def _snapshot(self, fresh=False, after_reset=False):
        # Share the account-read budget with every process, including manual tool calls.
        previous = self.store.read("poll") or {}
        delay = previous.get("next_at", 0) - self.clock()
        if delay > 0 and not after_reset:
            if not fresh and previous.get("endpoint") == self.client.endpoint and previous.get("snapshot"):
                return previous["snapshot"]
            raise PollDeferred(delay)
        try:
            snapshot = self.client.usage()
            remaining_usage(snapshot)
            reset_count(snapshot)
        except Exception as error:
            failures = min(previous.get("failures", 0) + 1, 8)
            delay = max(self.poll_seconds * 2 ** failures, getattr(error, "retry_after", 0), 1)
            self.store.write("poll", {"next_at": self.clock() + delay, "failures": failures})
            raise
        # Persist only quota data needed by this capability, omitting provider banners.
        snapshot = {key: snapshot.get(key) for key in (
            "accountId", "rateLimits", "rateLimitsByLimitId", "rateLimitResetCredits")}
        self.store.write("poll", {"snapshot": snapshot, "endpoint": self.client.endpoint,
                                  "fetched_at": self.clock(), "next_at": self.clock() + self.poll_seconds,
                                  "failures": 0})
        return snapshot

    def _redemption_backoff(self, error, failures):
        previous = self.store.read("poll") or {}
        delay = max(self.poll_seconds * 2 ** failures, getattr(error, "retry_after", 0), 1)
        previous.update(next_at=max(previous.get("next_at", 0), self.clock() + delay))
        self.store.write("poll", previous)

    def read(self):
        with self.store.guard():
            snapshot = self._snapshot()
            poll = self.store.read("poll")
            hook = self.store.read("hook")
            status = self._public(hook)
            if status:
                status["worker_alive"] = process_alive(hook.get("process"))
                worker_status = self.store.read("worker_status") or {}
                if worker_status.get("hook_id") == hook["id"]:
                    status["worker_status"] = worker_status
            return {"windows": remaining_usage(snapshot), "remaining_resets": reset_count(snapshot),
                    "fetched_at": poll["fetched_at"], "hook": status}

    @staticmethod
    def _account(snapshot):
        account = snapshot.get("accountId")
        if not isinstance(account, str) or not account:
            raise UsageError("Account identity is unavailable; reset automation cannot be authorized")
        return hashlib.sha256(account.encode()).hexdigest()

    @staticmethod
    def _public(hook):
        if hook is None:
            return None
        return {k: hook.get(k) for k in ("id", "thread_id", "status", "used", "limit", "keep_running", "reason")}

    def _owned(self):
        hook = self.store.read("hook")
        if hook and hook["thread_id"] != self.thread_id:
            raise UsageError(f"The usage hook is owned by thread {hook['thread_id']}")
        return hook

    def start(self, limit=None, keep_running=None, spawn=None):
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("limit_of_usages must be a positive integer")
        if keep_running is not None and type(keep_running) is not bool:
            raise ValueError("keep_running must be a boolean")
        with self.store.guard():
            old = self.store.read("hook")
            if old:
                if old["thread_id"] == self.thread_id and process_alive(old.get("process")):
                    if limit is not None and limit != old["limit"]:
                        raise UsageError("Expire the existing hook before changing its usage limit")
                    if keep_running is not None:
                        old["keep_running"] = keep_running
                    self.store.write("hook", old)
                    return self._public(old)
                self._reclaim(old)
            snapshot = self._snapshot()
            count = reset_count(snapshot)
            if count is None:
                raise UsageError("Available reset count is unknown")
            if count == 0:
                return {"status": "expired", "reason": "no_resets", "used": 0}
            pending = self.store.read("redemption")
            if pending and pending["status"] == "pending":
                raise UsageError("A prior reset outcome is unresolved; use_reset can reconcile the saved attempt")
            hook = {"id": str(uuid.uuid4()), "thread_id": self.thread_id,
                    "endpoint": self.client.endpoint, "account": self._account(snapshot),
                    "limit": limit, "used": 0, "keep_running": bool(keep_running),
                    "status": "running", "process": None, "awaiting_recovery": False, "first_check": True}
            hook["server_process"] = local_tcp_servers().get(self.client.endpoint)
            self.store.write("hook", hook)
            try:
                hook["process"] = (spawn or self._spawn)(hook)
                if not hook["process"]:
                    raise UsageError("The usage worker failed to start")
                self.store.write("hook", hook)
            except Exception:
                self.store.delete("hook")
                raise
            return self._public(hook)

    def _spawn(self, hook):
        # The detached worker survives the creating tool call and uses no model requests.
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("worker.py")), "--hook-id", hook["id"]],
            env={**os.environ, "CODEX_USAGE_STATE_DIR": str(self.store.root)},
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
        threading.Thread(target=process.wait, daemon=True).start()
        return process_identity(process.pid)

    def _reclaim(self, hook):
        # Both conditions are mandatory, and an unreadable thread remains unknown.
        if process_alive(hook.get("process")):
            raise UsageError(f"The usage hook is already running for thread {hook['thread_id']}")
        try:
            client = self.client if self.client.endpoint == hook["endpoint"] else Client(hook["endpoint"]).open()
        except Exception:
            original = hook.get("server_process")
            if original and not process_alive(original) and stopped_on_local_servers(hook["thread_id"]):
                self._finish(hook, "stale_owner")
                return
            raise UsageError("The old server is unavailable; stopped-thread ownership could not be verified") from None
        try:
            if client.thread_state(hook["thread_id"]) not in ("idle", "notLoaded"):
                raise UsageError("The prior owner is still running; its stale lock cannot be reclaimed")
        finally:
            if client is not self.client:
                client.close()
        self._finish(hook, "stale_owner")

    def reclaim_stale(self):
        with self.store.guard():
            hook = self.store.read("hook")
            if hook:
                self._reclaim(hook)
            return True

    def _finish(self, hook, reason):
        hook.update(status="expired", reason=reason)
        self.store.write("last_hook", self._public(hook))
        self.store.delete("hook")
        stop_process(hook.get("process"))
        return self._public(hook)

    def _observe_recovery(self, hook, snapshot):
        # A confirmed healthy read rearms the next depletion episode.
        windows = [w for w in remaining_usage(snapshot) if w["bucket"] == "codex"]
        if (self._account(snapshot) == hook["account"] and windows
                and all(w["remaining_percent"] > 1 for w in windows)):
            hook["awaiting_recovery"] = False
            self.store.write("hook", hook)

    def expire(self, reason="requested"):
        with self.store.guard():
            hook = self._owned()
            return self._finish(hook, reason) if hook else {"status": "expired", "reason": "no_hook"}

    def _consume(self, snapshot, hook=None):
        # Record intent before sending, and retain the key through uncertain responses.
        account = self._account(snapshot)
        journal = self.store.read("redemption")
        pending = journal and journal["status"] == "pending"
        if pending:
            if journal["account"] != account:
                raise UsageError("An unresolved reset belongs to a different account")
            if hook and journal["hook_id"] != hook["id"]:
                raise UsageError("An unresolved reset belongs to another hook")
        else:
            journal = {"key": str(uuid.uuid4()), "account": account, "status": "pending",
                       "hook_id": hook["id"] if hook else None}
            self.store.write("redemption", journal)
        try:
            outcome = self.client.call("account/rateLimitResetCredit/consume", {
                "idempotencyKey": journal["key"]}).get("outcome")
        except Exception as error:
            journal["failures"] = min(journal.get("failures", 0) + 1, 8)
            self.store.write("redemption", journal)
            self._redemption_backoff(error, journal["failures"])
            raise
        if outcome not in ("reset", "alreadyRedeemed", "nothingToReset", "noCredit"):
            raise UsageError("Reset outcome was not recognized; the saved attempt remains unresolved")
        # Commit the count and receipt together in the hook before settling the journal.
        if hook and outcome in ("reset", "alreadyRedeemed") and hook.get("last_key") != journal["key"]:
            hook.update(used=hook["used"] + 1, last_key=journal["key"], awaiting_recovery=True)
            self.store.write("hook", hook)
        journal.update(status="confirmed", outcome=outcome)
        self.store.write("redemption", journal)
        return outcome

    def use_reset(self):
        with self.store.guard():
            hook = self._owned()
            if hook and hook["limit"] is not None and hook["used"] >= hook["limit"]:
                self._finish(hook, "usage_limit")
                raise UsageError("The hook usage limit was already reached")
            snapshot = self._snapshot(fresh=True)
            if hook and self._account(snapshot) != hook["account"]:
                raise UsageError("The account changed; reset automation is suspended")
            outcome = self._consume(snapshot, hook)
            try:
                refreshed = self._snapshot(after_reset=True)
                if hook:
                    self._observe_recovery(hook, refreshed)
                result = {"outcome": outcome, "remaining_resets": reset_count(refreshed),
                          "windows": remaining_usage(refreshed)}
            except Exception:
                result = {"outcome": outcome, "readback": "pending"}
            if hook and hook["limit"] is not None and hook["used"] >= hook["limit"]:
                result["hook"] = self._finish(hook, "usage_limit")
            elif hook and (outcome == "noCredit" or result.get("remaining_resets") == 0):
                result["hook"] = self._finish(hook, "no_resets")
            return result

    def tick(self, expected_id=None):
        with self.store.guard():
            hook = self._owned()
            if not hook or (expected_id is not None and hook["id"] != expected_id):
                return {"status": "expired", "reason": "ownership_changed"}
            if hook["limit"] is not None and hook["used"] >= hook["limit"]:
                return self._finish(hook, "usage_limit")
            # Reuse the setup read only while it is fresh enough for the worker's first check.
            poll = self.store.read("poll") or {}
            just_read = hook.get("first_check") and 0 <= self.clock() - poll.get("fetched_at", 0) < 2
            snapshot = self._snapshot(fresh=not just_read)
            hook["first_check"] = False
            self.store.write("hook", hook)
            if self._account(snapshot) != hook["account"]:
                raise UsageError("The account changed; automatic resets are suspended")
            journal = self.store.read("redemption")
            pending = journal and journal["status"] == "pending" and journal["hook_id"] == hook["id"]
            count = reset_count(snapshot)
            if count == 0 and not pending:
                return self._finish(hook, "no_resets")
            windows = [w for w in remaining_usage(snapshot) if w["bucket"] == "codex"]
            low = any(w["remaining_percent"] <= 1 for w in windows)
            if not pending:
                if count is None or not windows:
                    raise UsageError("Quota or reset count is unavailable; automatic resets are suspended")
                if hook["awaiting_recovery"]:
                    if not low:
                        hook["awaiting_recovery"] = False
                        self.store.write("hook", hook)
                    return self._public(hook)
                if not low:
                    return self._public(hook)
            # Recheck lifecycle and thread state after the upstream read and before any write.
            event = self.store.read("event-" + self.thread_id) or {}
            if event.get("hook_id") == hook["id"] and event.get("event") in ("Interrupt", "SessionEnd", "expire"):
                return self._finish(hook, "session_stopped")
            if not hook["keep_running"] and self.client.thread_state(self.thread_id) != "active":
                return self._public(hook)
            event = self.store.read("event-" + self.thread_id) or {}
            if event.get("hook_id") == hook["id"] and event.get("event") in ("Interrupt", "SessionEnd", "expire"):
                return self._finish(hook, "session_stopped")
            outcome = self._consume(snapshot, hook)
            if outcome == "noCredit":
                return self._finish(hook, "no_resets")
            try:
                refreshed = self._snapshot(after_reset=True)
            finally:
                if hook["limit"] is not None and hook["used"] >= hook["limit"]:
                    self._finish(hook, "usage_limit")
            if hook["status"] == "expired":
                return self._public(hook)
            self._observe_recovery(hook, refreshed)
            if reset_count(refreshed) == 0:
                return self._finish(hook, "no_resets")
            return self._public(hook)
