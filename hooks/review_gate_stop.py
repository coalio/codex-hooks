#!/usr/bin/env python3
"""Route changed Git sessions through review and final conclusion generation."""

from __future__ import annotations

import hashlib
import json
import shlex
from datetime import datetime
from pathlib import Path

from review_gate_common import (
    CLASSIFIER_NAME, CONCLUSION_NAME, REVIEWER_NAME, ReviewGateError,
    baseline_matches, current_result, hook_repository, hook_session_id,
    is_exempt_repository, load_session_state, manifest_route, record_agent_result,
    repository_fingerprint, read_hook_input, session_delta_artifact, update_delta,
    stop_without_approval, write_hook_output, write_session_state,
)
from review_gate_evidence import find_gate_evidence


_FINGERPRINT_SCRIPT = Path(__file__).with_name("review_gate_fingerprint.py").resolve()
_FINGERPRINT_COMMAND = f"/usr/bin/python3 {shlex.quote(str(_FINGERPRINT_SCRIPT))}"


def _agent_instruction(
    role: str, manifest: list[dict[str, str]], artifact: str, artifact_sha256: str,
    reason: str | None = None,
) -> str:
    """Build a compact, self-contained, no-history agent routing instruction."""

    manifest_json = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    exact_delta = (
        f" Read the protected session-delta artifact {artifact} first and verify its SHA-256 "
        f"is {artifact_sha256}; it contains the exact session-only delta for any path that was "
        "already dirty at session start."
    )
    if role == CLASSIFIER_NAME:
        return (
            "Spawn a fresh architecture_scope_classifier with fork_turns=none. Pass only a compact "
            "summary of the original request and this session-delta manifest: " + manifest_json + "."
            + exact_delta + " "
            "The classifier must inspect the manifest first, read only ambiguous changed portions, run "
            f"{_FINGERPRINT_COMMAND} immediately before its "
            "two-line verdict, and must not modify files or perform any other work. Wait for its result."
        )
    escalation = f" Classifier escalation reason: {reason}" if reason else ""
    return (
        "Spawn a fresh architecture_reviewer with fork_turns=none. Pass only a compact summary of the "
        "original request and this session-delta manifest: " + manifest_json + "." + exact_delta
        + escalation + " "
        "Have it inspect the changed files and only the surrounding code needed for architecture judgment. "
        "Wait for its result. If it returns FAIL, fix every actionable finding, rerun validation, then "
        "attempt to finish so Stop refreshes the fingerprint, manifest, and protected artifact before "
        "routing another fresh architecture_reviewer; never reuse this handoff after a file change. "
        f"Immediately before PASS the reviewer must run {_FINGERPRINT_COMMAND}. "
        "Do not modify repository files after PASS."
    )


def _block(payload: dict[str, object], detail: str, instruction: str) -> None:
    """Continue the parent loop with one explicit remediation path."""

    prefix = "The prior Stop continuation is still blocked. " if payload.get("stop_hook_active") else ""
    write_hook_output({"decision": "block", "reason": prefix + detail + " " + instruction})


def _conclusion_instruction(
    state: dict[str, object], manifest: list[dict[str, str]], approval: dict[str, object] | None,
) -> str:
    """Give the parent a source-grounded handoff for the final Terra conclusion."""

    # Expose only repository and gate facts; the parent adds its implementation ledger.
    delta = state.get("delta") if isinstance(state.get("delta"), dict) else {}
    baseline = state.get("baseline") if isinstance(state.get("baseline"), dict) else {}
    context = {
        "manifest": manifest,
        "baseline_head": baseline.get("head"),
        "current_head": delta.get("current_head"),
        "session_delta_artifact": delta.get("session_delta_artifact"),
        "session_delta_sha256": delta.get("session_delta_sha256"),
        "approval_role": approval.get("role") if approval else "remote_exemption",
        "approval_outcome": approval.get("outcome") if approval else "EXEMPT",
        "approval_reason": approval.get("reason") if approval else None,
    }
    return (
        "Spawn a fresh conclusion_writer with fork_turns=none. Pass this machine context: "
        + json.dumps(context, sort_keys=True, separators=(",", ":"))
        + ". Also pass a concise completion ledger containing the original request, material "
        "implementation decisions, every problem or failed attempt encountered and its resolution, "
        "validation commands/results, and commit/deployment facts. The conclusion writer must inspect "
        f"the exact current diff and relevant changed source, run {_FINGERPRINT_COMMAND} immediately "
        "before responding, and make no "
        "repository changes. Wait for it, then return its complete response as the final answer without "
        "replacing it with a generic review-passed summary."
    )


def _conclusion_follows_approval(
    conclusion: dict[str, object], approval: dict[str, object] | None,
    current_turn_id: object,
) -> bool:
    """Require the conclusion in this completion generation and after final approval."""

    if not isinstance(current_turn_id, str) or conclusion.get("turn_id") != current_turn_id:
        return False
    if approval is None:
        return True
    conclusion_time = conclusion.get("recorded_at")
    approval_time = approval.get("recorded_at")
    if not isinstance(conclusion_time, str) or not isinstance(approval_time, str):
        return False
    try:
        conclusion_timestamp = datetime.fromisoformat(conclusion_time.replace("Z", "+00:00"))
        approval_timestamp = datetime.fromisoformat(approval_time.replace("Z", "+00:00"))
        if conclusion_timestamp.tzinfo is None or approval_timestamp.tzinfo is None:
            return False
        return conclusion_timestamp > approval_timestamp
    except (TypeError, ValueError):
        return False


def main() -> None:
    """Accept zero-token cases and route every exact changed fingerprint."""

    # Work outside Git never enters the review state machine.
    payload = read_hook_input("Stop")
    repository = hook_repository(payload)
    if repository is None:
        write_hook_output({"continue": True})
        return

    # A missing baseline needs a new session, so a continuation cannot repair it.
    session_id = hook_session_id(payload)
    state = load_session_state(repository, session_id)
    if state is None:
        stop_without_approval("The session baseline is missing.")
        return
    fingerprint = repository_fingerprint(repository)

    # Unchanged sessions need no diff-based conclusion.
    if baseline_matches(state, fingerprint):
        write_hook_output({"continue": True})
        return

    # Older baselines cannot reconstruct session-only hunks in pre-existing dirty files.
    baseline = state.get("baseline")
    if not isinstance(baseline, dict) or not isinstance(baseline.get("dirty_snapshots"), dict):
        stop_without_approval(
            "The session baseline predates exact dirty-path delta capture.",
        )
        return

    # The exact Salesforce remote skips architecture models but still receives a conclusion.
    exempt_repository = is_exempt_repository(state, repository)

    # Recover Codex 0.147 natural-completion evidence for every trusted role.
    evidence = find_gate_evidence(
        payload.get("transcript_path"), session_id, repository, fingerprint,
    )
    for role, item in evidence.items():
        # Preserve the original ordering when Stop rediscovers identical 0.147 evidence.
        existing = current_result(state, role, fingerprint)
        message_hash = hashlib.sha256(item["message"].encode()).hexdigest()
        same_evidence = (
            existing is not None
            and existing.get("message_sha256") == message_hash
            and existing.get("agent_id") == item["agent_id"]
        )
        child_conclusion_needs_parent_binding = (
            same_evidence
            and role == CONCLUSION_NAME
            and existing.get("turn_id") == item.get("turn_id")
            and existing.get("turn_id") != payload.get("turn_id")
        )
        if same_evidence and not child_conclusion_needs_parent_binding:
            continue

        # Bind a completed child conclusion to this parent generation exactly once.
        record_agent_result(
            state, role, fingerprint, payload.get("turn_id"), item["agent_id"],
            item["message"], item["completed_at"],
        )

    # Persist an exact compact delta before making or explaining the route.
    manifest = update_delta(state, repository, fingerprint)
    write_session_state(repository, session_id, state)
    artifact = session_delta_artifact(state)
    if artifact is None:
        raise ReviewGateError("the current session delta lacks its protected artifact")
    artifact_sha256 = state["delta"].get("session_delta_sha256")
    if not isinstance(artifact_sha256, str):
        raise ReviewGateError("the current session delta lacks its artifact hash")

    # Resolve the architecture decision before allowing conclusion generation.
    approval: dict[str, object] | None = None
    if not exempt_repository:
        reviewer = current_result(state, REVIEWER_NAME, fingerprint)
        if reviewer is not None and reviewer.get("outcome") == "PASS":
            approval = reviewer
        elif reviewer is not None and reviewer.get("outcome") == "FAIL":
            _block(
                payload,
                "The current architecture review returned FAIL.",
                _agent_instruction(REVIEWER_NAME, manifest, artifact, artifact_sha256),
            )
            return
        else:
            classifier = current_result(state, CLASSIFIER_NAME, fingerprint)
            if classifier is not None and classifier.get("outcome") == "ARCHITECTURE_NOT_REQUIRED":
                approval = classifier
            elif classifier is not None and classifier.get("outcome") == "ARCHITECTURE_REQUIRED":
                reason = str(classifier.get("reason") or "")
                _block(
                    payload,
                    "The current classifier requires architecture review.",
                    _agent_instruction(
                        REVIEWER_NAME, manifest, artifact, artifact_sha256, reason,
                    ),
                )
                return
            else:
                # Deterministic paths avoid classifier cost for obvious engineering work.
                role = manifest_route(manifest)
                _block(
                    payload,
                    "The current repository fingerprint has no trusted gate result.",
                    _agent_instruction(role, manifest, artifact, artifact_sha256),
                )
                return

    # The final role writes the actual answer only after the gate decision is settled.
    conclusion = current_result(state, CONCLUSION_NAME, fingerprint)
    if conclusion is not None and _conclusion_follows_approval(
        conclusion, approval, payload.get("turn_id"),
    ):
        write_hook_output({"continue": True})
        return
    _block(
        payload,
        "The current repository state is approved but lacks its final diff-based conclusion.",
        _conclusion_instruction(state, manifest, approval),
    )


if __name__ == "__main__":
    try:
        main()
    except ReviewGateError as exc:
        stop_without_approval(f"Gate state is unreadable or inconsistent: {exc}.")
