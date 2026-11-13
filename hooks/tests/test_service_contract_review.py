"""Behavioral coverage for the independent service-contract gate."""

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from review_gate_contracts import CHECK_NAMES, parse_contract_review


def review_message(inventory_hash="a" * 64):
    return {
        "verdict": "PASS",
        "inventory_sha256": inventory_hash,
        "reviewed_paths": ["service.py"],
        "checks": {
            name: {"status": "pass", "evidence": "service.py:CompanyService owns company records"}
            for name in CHECK_NAMES
        },
        "findings": [],
    }


class ContractReviewParsingTest(unittest.TestCase):
    def test_requires_all_five_checks_and_evidence(self):
        good = review_message()
        self.assertEqual(parse_contract_review(json.dumps(good)), good)
        for name in CHECK_NAMES:
            for mutation in ("missing", "empty", "unknown"):
                bad = copy.deepcopy(good)
                if mutation == "missing":
                    del bad["checks"][name]
                elif mutation == "empty":
                    bad["checks"][name]["evidence"] = "  "
                else:
                    bad["checks"][name]["status"] = "probably"
                with self.subTest(name=name, mutation=mutation):
                    self.assertIsNone(parse_contract_review(json.dumps(bad)))

    def test_bare_pass_cannot_bypass_contract_review(self):
        for message in ("PASS", "{}", "null", "[]", "not json"):
            self.assertIsNone(parse_contract_review(message))

    def test_pass_cannot_contain_failed_check_or_findings(self):
        bad = review_message()
        bad["checks"][CHECK_NAMES[0]]["status"] = "fail"
        self.assertIsNone(parse_contract_review(json.dumps(bad)))
        bad["verdict"] = "FAIL"
        self.assertIsNone(parse_contract_review(json.dumps(bad)))
        bad["findings"] = [{
            "check": CHECK_NAMES[0], "severity": "high", "path": "service.py",
            "symbol": "CompanyService", "problem": "Workflow policy has no shared owner",
            "minimal_correction": "Keep the policy in its workflow",
        }]
        self.assertIsNotNone(parse_contract_review(json.dumps(bad)))

    def test_rejects_duplicate_json_keys_and_unbound_inventory(self):
        good = json.dumps(review_message())
        self.assertIsNone(parse_contract_review(good.replace('"verdict": "PASS"', '"verdict": "FAIL", "verdict": "PASS"')))
        bad = review_message("not-a-hash")
        self.assertIsNone(parse_contract_review(json.dumps(bad)))

    def test_malformed_field_shapes_are_rejected_without_exceptions(self):
        good = review_message()
        for key in good:
            for value in (None, False, 9, [], {}, ""):
                bad = copy.deepcopy(good)
                bad[key] = value
                if bad == good or key == "findings" and value == []:
                    continue
                with self.subTest(key=key, value=value):
                    parsed = parse_contract_review(json.dumps(bad))
                    # An empty path set is legal for a commit-only delta, binding is checked by the gate.
                    if key == "reviewed_paths" and value == []:
                        self.assertIsNotNone(parsed)
                    else:
                        self.assertIsNone(parsed)


if __name__ == "__main__":
    unittest.main()
