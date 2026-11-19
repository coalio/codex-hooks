import copy
import tempfile
import unittest
from unittest.mock import patch

from tools.usage.service import UsageService
from tools.usage.state import Store, process_identity
from tools.usage.protocol import UsageError


def quota(used=99, credits=3, account="account-a"):
    return {"accountId": account, "rateLimits": {"limitId": "codex", "primary": {
        "usedPercent": used, "windowDurationMins": 300, "resetsAt": 1000}},
        "rateLimitResetCredits": {"availableCount": credits}}


class FakeClient:
    endpoint = "ws://127.0.0.1:1234"

    def __init__(self):
        self.snapshot = quota()
        self.state = "active"
        self.calls = []
        self.outcomes = ["reset"]

    def usage(self):
        return copy.deepcopy(self.snapshot)

    def thread_state(self, thread_id):
        return self.state

    def call(self, method, params):
        self.calls.append((method, params))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        self.snapshot = quota(5, 2)
        return {"outcome": outcome}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.client = FakeClient()
        self.service = UsageService(self.store, self.client, "thread-a", clock=lambda: 100, poll_seconds=0)

    def start(self, limit=None, keep=False):
        return self.service.start(limit, keep, spawn=lambda hook: process_identity())

    def test_threshold_and_limit_expire_and_remove_lock(self):
        self.start(limit=1)
        self.assertEqual(self.service.tick()["status"], "expired")
        self.assertIsNone(self.store.read("hook"))
        self.assertEqual(len(self.client.calls), 1)

    def test_above_threshold_does_not_consume(self):
        self.client.snapshot = quota(98.9)
        self.start()
        self.service.tick()
        self.assertEqual(self.client.calls, [])

    def test_unknown_count_never_authorizes_reset(self):
        self.client.snapshot["rateLimitResetCredits"] = None
        with self.assertRaises(UsageError):
            self.start()
        self.assertEqual(self.client.calls, [])

    def test_zero_credits_does_not_launch_worker(self):
        self.client.snapshot = quota(99, 0)
        with patch.object(self.service, "_spawn") as spawn:
            self.assertEqual(self.service.start()["reason"], "no_resets")
            spawn.assert_not_called()

    def test_timeout_reuses_durable_idempotency_key_and_counts_once(self):
        self.start(limit=2)
        self.client.outcomes = [TimeoutError(), "alreadyRedeemed"]
        with self.assertRaises(TimeoutError):
            self.service.tick()
        pending = self.store.read("redemption")
        self.service.clock = lambda: 102
        self.service.tick()
        self.assertEqual([x[1]["idempotencyKey"] for x in self.client.calls], [pending["key"]] * 2)
        self.assertEqual(self.store.read("hook")["used"], 1)

    def test_account_switch_blocks_redemption(self):
        self.start()
        self.client.snapshot = quota(account="account-b")
        with self.assertRaises(UsageError):
            self.service.tick()
        self.assertEqual(self.client.calls, [])

    def test_competing_owner_cannot_start_or_redeem(self):
        self.start()
        other = UsageService(self.store, self.client, "thread-b")
        with self.assertRaises(UsageError):
            other.start(spawn=lambda hook: process_identity())
        with self.assertRaises(UsageError):
            other.use_reset()

    def test_same_owner_reconfiguration_preserves_limit_and_usage(self):
        self.start(limit=2)
        self.service.tick()
        self.service.start(2, True)
        hook = self.store.read("hook")
        self.assertEqual(hook["used"], 1)
        self.assertTrue(hook["keep_running"])

    def test_stale_lock_requires_both_stopped_thread_and_dead_process(self):
        self.start()
        hook = self.store.read("hook")
        hook["process"] = {"pid": 99999999, "start": "missing", "boot": "missing"}
        self.store.write("hook", hook)
        with self.assertRaises(UsageError):
            self.service.start()
        self.client.state = "idle"
        self.assertTrue(self.service.reclaim_stale())
        self.assertIsNone(self.store.read("hook"))

    def test_unchanged_low_snapshot_cannot_burn_another_credit(self):
        self.start()
        original_call = self.client.call
        def stale_readback(method, params):
            result = original_call(method, params)
            self.client.snapshot = quota(99, 2)
            return result
        self.client.call = stale_readback
        self.service.tick()
        self.service.tick()
        self.assertEqual(len(self.client.calls), 1)

    def test_recovery_rearms_a_later_depletion_episode(self):
        self.start(limit=2)
        self.service.tick()
        self.client.snapshot = quota(99, 2)
        self.client.outcomes = ["reset"]
        self.assertEqual(self.service.tick()["reason"], "usage_limit")
        self.assertEqual(len(self.client.calls), 2)

    def test_owner_can_expire_hook_without_a_live_server(self):
        self.start()
        result = UsageService(self.store, None, "thread-a").expire()
        self.assertEqual(result["status"], "expired")
        self.assertIsNone(self.store.read("hook"))

    def test_manual_reset_cannot_bypass_active_hook_limit(self):
        self.start(limit=1)
        result = self.service.use_reset()
        self.assertEqual(result["hook"]["reason"], "usage_limit")
        self.assertEqual(result["hook"]["used"], 1)

    def test_shared_poll_budget_caches_reads_and_defers_fresh_actions(self):
        from tools.usage.service import PollDeferred
        self.service.poll_seconds = 60
        self.service.read()
        self.client.snapshot = quota(100, 1)
        reading = self.service.read()
        self.assertEqual(reading["remaining_resets"], 3)
        with self.assertRaises(PollDeferred):
            self.service.use_reset()
        self.assertEqual(self.client.calls, [])

    def test_cancel_during_status_read_prevents_redemption(self):
        hook = self.start()
        def state(_):
            self.store.write("event-thread-a", {"hook_id": hook["id"], "event": "Interrupt"})
            return "active"
        self.client.thread_state = state
        self.assertEqual(self.service.tick()["reason"], "session_stopped")
        self.assertEqual(self.client.calls, [])

    def test_unknown_thread_prevents_stale_lock_reclamation(self):
        self.start()
        hook = self.store.read("hook")
        hook["process"] = {"pid": 99999999, "start": "missing", "boot": "missing"}
        self.store.write("hook", hook)
        self.client.thread_state = lambda _: (_ for _ in ()).throw(TimeoutError())
        with self.assertRaises(TimeoutError):
            self.service.reclaim_stale()
        self.assertIsNotNone(self.store.read("hook"))

    def test_dead_server_recovery_also_checks_for_a_resumed_owner(self):
        self.start()
        hook = self.store.read("hook")
        dead = {"pid": 99999999, "start": "missing", "boot": "missing"}
        hook.update(process=dead, server_process=dead, endpoint="ws://127.0.0.1:1")
        self.store.write("hook", hook)
        with patch("tools.usage.service.Client") as client, patch(
                "tools.usage.service.stopped_on_local_servers", return_value=False) as stopped:
            client.return_value.open.side_effect = ConnectionError()
            with self.assertRaises(UsageError):
                self.service.reclaim_stale()
            stopped.return_value = True
            self.assertTrue(self.service.reclaim_stale())
        self.assertIsNone(self.store.read("hook"))


if __name__ == "__main__":
    unittest.main()
