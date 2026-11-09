import unittest

from tools.usage.protocol import remaining_usage, reset_count


class UsageParsingTests(unittest.TestCase):
    def test_preserves_distinct_quota_windows(self):
        snapshot = {"rateLimitsByLimitId": {"codex": {
            "primary": {"usedPercent": 99, "windowDurationMins": 300, "resetsAt": 10},
            "secondary": {"usedPercent": 27.5, "windowDurationMins": 10080, "resetsAt": 20},
        }}, "rateLimits": {"primary": {"usedPercent": 1}}}
        result = remaining_usage(snapshot)
        self.assertEqual([x["remaining_percent"] for x in result], [1, 72.5])
        self.assertEqual(result[1]["window_minutes"], 10080)

    def test_missing_usage_is_unknown_and_excess_usage_is_zero_remaining(self):
        self.assertEqual(remaining_usage({}), [])
        self.assertEqual(remaining_usage({"rateLimits": {
            "limitId": "codex", "primary": {"usedPercent": 102}}})[0]["remaining_percent"], 0)

    def test_invalid_provider_percent_is_rejected(self):
        for value in [True, -1, "99", float("nan"), float("inf")]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                remaining_usage({"rateLimits": {"primary": {"usedPercent": value}}})

    def test_count_is_authoritative_even_if_details_are_capped(self):
        self.assertEqual(reset_count({"rateLimitResetCredits": {
            "availableCount": 5, "credits": [{"id": "one"}]}}), 5)
        self.assertIsNone(reset_count({"rateLimitResetCredits": None}))
        for value in [True, -1, "3"]:
            with self.assertRaises(ValueError):
                reset_count({"rateLimitResetCredits": {"availableCount": value}})


if __name__ == "__main__":
    unittest.main()
