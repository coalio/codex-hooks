import os
import unittest
from unittest.mock import patch

from tools.usage.server import validate


class InputTests(unittest.TestCase):
    def test_rejects_invalid_arguments_before_any_effect(self):
        for args in [{}, {"action": "other"}, {"hook": "other"},
                     {"hook": "use_reset_automatically", "limit_of_usages": True},
                     {"hook": "use_reset_automatically", "limit_of_usages": 0},
                     {"hook": "use_reset_automatically", "keep_running": "yes"},
                     {"action": "expire_hooks", "hook": "use_reset_automatically"},
                     {"action": "read_remaining_percent", "keep_running": True}]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                validate({**args, "thread_id": "owner"})

    def test_explicit_thread_wins_over_process_environment(self):
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "another-thread"}):
            self.assertEqual(validate({"action": "read_remaining_resets", "thread_id": "owner"}), "owner")

    def test_mcp_requires_thread_identity_when_environment_has_none(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            validate({"hook": "use_reset_automatically"})
