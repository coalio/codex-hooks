import tempfile
import unittest

from tools.usage.worker import Lifecycle
from tools.usage.state import Store


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.lifecycle = Lifecycle()
        self.hook = {"id": "hook-a", "thread_id": "thread-a", "keep_running": False}

    def test_stop_during_review_continuation_does_not_expire(self):
        event = {"hook_id": "hook-a", "event": "Stop"}
        self.assertIsNone(self.lifecycle.check(self.hook, event, "active", 0))
        self.assertIsNone(self.lifecycle.check(self.hook, event, "idle", 1))
        self.assertIsNone(self.lifecycle.check(self.hook, event, "active", 3))
        self.assertIsNone(self.lifecycle.check(self.hook, event, "idle", 4))
        self.assertEqual(self.lifecycle.check(self.hook, event, "idle", 20), "work_completed")

    def test_keep_running_survives_completion_but_not_session_end(self):
        self.hook["keep_running"] = True
        self.assertIsNone(self.lifecycle.check(self.hook, {}, "idle", 1))
        self.assertIsNone(self.lifecycle.check(self.hook, {}, "idle", 1000))
        self.assertEqual(self.lifecycle.check(self.hook, {
            "hook_id": "hook-a", "event": "SessionEnd"}, "idle", 1001), "session_stopped")

    def test_old_lifecycle_event_cannot_stop_replacement(self):
        self.assertIsNone(self.lifecycle.check(self.hook, {
            "hook_id": "old-hook", "event": "Interrupt"}, "active", 1))

    def test_unknown_status_is_not_evidence_of_completion(self):
        self.assertIsNone(self.lifecycle.check(self.hook, {}, "idle", 1))
        self.assertIsNone(self.lifecycle.check(self.hook, {}, None, 30))
        self.assertIsNone(self.lifecycle.check(self.hook, {}, "idle", 31))


if __name__ == "__main__":
    unittest.main()
