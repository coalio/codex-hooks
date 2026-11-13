"""Strict evidence contract for the service and API ownership reviewer."""

from __future__ import annotations

import json
import re
from typing import Any, Literal, TypedDict


CONTRACT_REVIEWER_NAME = "service_contract_reviewer"
CHECK_NAMES = (
    "service_justification", "responsibility", "signature_semantics",
    "type_cohesion", "domain_generality",
)


class CheckEvidence(TypedDict):
    status: Literal["pass", "fail", "not_applicable"]
    evidence: str


class ContractFinding(TypedDict):
    check: str
    severity: Literal["low", "medium", "high", "critical"]
    path: str
    symbol: str
    problem: str
    minimal_correction: str


class ContractReview(TypedDict):
    verdict: Literal["PASS", "FAIL"]
    inventory_sha256: str
    reviewed_paths: list[str]
    checks: dict[str, CheckEvidence]
    findings: list[ContractFinding]


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous JSON instead of silently accepting the last duplicate."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate review field")
        result[key] = value
    return result


def nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def parse_contract_review(message: Any) -> ContractReview | None:
    """Require evidence for every rule and findings for every failed rule."""
    if not isinstance(message, str):
        return None
    try:
        result = json.loads(message, object_pairs_hook=unique_object)
    except (ValueError, TypeError):
        return None
    if not isinstance(result, dict) or set(result) != {
        "verdict", "inventory_sha256", "reviewed_paths", "checks", "findings",
    }:
        return None
    if result["verdict"] not in ("PASS", "FAIL"):
        return None
    digest = result["inventory_sha256"]
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        return None
    paths = result["reviewed_paths"]
    if not isinstance(paths, list) or not all(nonempty(path) for path in paths):
        return None
    if len(paths) != len(set(paths)):
        return None
    checks = result["checks"]
    if not isinstance(checks, dict) or set(checks) != set(CHECK_NAMES):
        return None
    for check in checks.values():
        if not isinstance(check, dict) or set(check) != {"status", "evidence"}:
            return None
        if check["status"] not in ("pass", "fail", "not_applicable") or not nonempty(check["evidence"]):
            return None

    # A verdict cannot contradict either the checklist or its actionable findings.
    findings = result["findings"]
    if not isinstance(findings, list):
        return None
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != {
            "check", "severity", "path", "symbol", "problem", "minimal_correction",
        } or not all(nonempty(value) for value in finding.values()):
            return None
        if finding["check"] not in CHECK_NAMES or finding["severity"] not in {"low", "medium", "high", "critical"}:
            return None
        if checks[finding["check"]]["status"] != "fail":
            return None
    failed = {name for name, check in checks.items() if check["status"] == "fail"}
    if failed != {finding["check"] for finding in findings}:
        return None
    if (result["verdict"] == "FAIL") != bool(findings):
        return None
    return result
