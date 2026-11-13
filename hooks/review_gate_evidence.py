#!/usr/bin/env python3
"""Recover gate-agent evidence from completed Codex child transcripts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterator

from review_gate_contracts import CONTRACT_REVIEWER_NAME, parse_contract_review

from review_gate_common import (
    CLASSIFIER_NAME, CONCLUSION_NAME, REVIEWER_NAME, ReviewGateError,
    is_valid_conclusion, is_valid_review_result, parse_classifier_result,
)


_FINGERPRINT_MARKER = re.compile(
    r"(?:^|\n)CODEX_REVIEW_GATE_FINGERPRINT=([0-9a-f]{64})(?:\n|$)"
)


def find_gate_evidence(
    transcript_path: Any, parent_session_id: str, repository: Path,
    current_fingerprint: str,
) -> dict[str, dict[str, str]]:
    """Find the newest completed result for each trusted gate role."""

    # A missing root transcript cannot prove that an independent child ran.
    if not isinstance(transcript_path, str) or not transcript_path:
        return {}
    root_transcript = Path(transcript_path).resolve()
    if not root_transcript.is_file():
        return {}

    # Sessions spanning midnight can write children into another date directory.
    candidates = root_transcript.parent.glob("*.jsonl")
    date_parts = root_transcript.parent.parts[-3:]
    if (
        len(root_transcript.parents) >= 4
        and root_transcript.parents[3].name == "sessions"
        and re.fullmatch(r"[0-9]{4}/[0-9]{2}/[0-9]{2}", "/".join(date_parts))
    ):
        candidates = root_transcript.parents[3].glob(
            "[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]/*.jsonl"
        )

    # Identity, role, repository, and fingerprint checks still bind every child.
    evidence: dict[str, dict[str, str]] = {}
    for candidate in candidates:
        if candidate.resolve() == root_transcript:
            continue
        parsed = _parse_candidate(
            candidate, parent_session_id, repository, current_fingerprint,
        )
        if parsed is None:
            continue
        existing = evidence.get(parsed["role"])
        if existing is None or parsed["completed_at"] > existing["completed_at"]:
            evidence[parsed["role"]] = parsed
    return evidence


def _parse_candidate(
    candidate: Path, parent_session_id: str, repository: Path,
    current_fingerprint: str,
) -> dict[str, str] | None:
    """Validate one child transcript's identity, marker, verdict, and completion."""

    # Bind the child to the parent session, repository, and configured role.
    try:
        with candidate.open(encoding="utf-8") as transcript:
            first_line = transcript.readline(4 * 1024 * 1024)
            if not first_line.endswith("\n"):
                return None
            first_record = json.loads(first_line)
            metadata = first_record.get("payload") if isinstance(first_record, dict) else None
            role = _expected_role(metadata, parent_session_id, repository)
            if role is None:
                return None

            # Track markers only from tool output and verdicts only from final messages.
            latest_marker: str | None = None
            latest_message: str | None = None
            completed_at: str | None = None
            completed_turn_id: str | None = None
            for line in transcript:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    return None
                marker = _fingerprint_from_output(record)
                if marker is not None:
                    latest_marker = marker
                message = _assistant_message(record)
                if message is not None:
                    latest_message = message
                task_turn_id = _task_complete_turn_id(record)
                if task_turn_id is not None:
                    completed_at = str(record.get("timestamp") or "")
                    completed_turn_id = task_turn_id
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewGateError(f"cannot inspect gate-agent transcript: {exc}") from exc

    # Require a strict role-specific verdict bound to the current fingerprint.
    if role == CONTRACT_REVIEWER_NAME:
        valid_message = parse_contract_review(latest_message) is not None
    elif role == REVIEWER_NAME:
        valid_message = is_valid_review_result(latest_message)
    elif role == CLASSIFIER_NAME:
        valid_message = parse_classifier_result(latest_message) is not None
    else:
        valid_message = is_valid_conclusion(latest_message)
    if (
        not valid_message
        or latest_marker != current_fingerprint
        or not completed_at
        or not completed_turn_id
    ):
        return None
    return {
        "role": role,
        "agent_id": str(metadata.get("id") or candidate.stem),
        "message": str(latest_message),
        "completed_at": completed_at,
        "turn_id": completed_turn_id,
        "transcript": str(candidate),
    }


def _expected_role(metadata: Any, parent_session_id: str, repository: Path) -> str | None:
    """Return the role only for the exact parent session and repository."""

    if not isinstance(metadata, dict):
        return None
    role = metadata.get("agent_role")
    cwd = metadata.get("cwd")
    if (
        role not in {CLASSIFIER_NAME, REVIEWER_NAME, CONTRACT_REVIEWER_NAME, CONCLUSION_NAME}
        or metadata.get("session_id") != parent_session_id
        or metadata.get("parent_thread_id") != parent_session_id
        or not isinstance(cwd, str)
        or not cwd
        or Path(cwd).resolve() != repository.resolve()
    ):
        return None
    return role


def _fingerprint_from_output(record: Any) -> str | None:
    """Extract the marker only from either supported tool-output envelope."""

    if not isinstance(record, dict) or record.get("type") != "response_item":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") not in {
        "function_call_output", "custom_tool_call_output",
    }:
        return None
    for text in _text_values(payload.get("output")):
        match = _FINGERPRINT_MARKER.search(text)
        if match is not None:
            return match.group(1)
    return None


def _assistant_message(record: Any) -> str | None:
    """Read final assistant text from Codex 0.147 event or response-item formats."""

    if not isinstance(record, dict):
        return None
    if record.get("type") == "event_msg":
        payload = record.get("payload")
        if isinstance(payload, dict) and payload.get("type") == "agent_message":
            message = payload.get("message")
            return message if isinstance(message, str) else None
    if record.get("type") != "response_item":
        return None
    payload = record.get("payload")
    if (
        not isinstance(payload, dict)
        or payload.get("type") != "message"
        or payload.get("role") != "assistant"
        or payload.get("phase") != "final_answer"
    ):
        return None
    parts = list(_message_text_values(payload.get("content")))
    return "".join(parts) if parts else None


def _task_complete_turn_id(record: Any) -> str | None:
    """Return the completed child turn that produced the final response."""

    payload = record.get("payload") if isinstance(record, dict) else None
    if (
        record.get("type") != "event_msg"
        or not isinstance(payload, dict)
        or payload.get("type") != "task_complete"
    ):
        return None
    turn_id = payload.get("turn_id")
    return turn_id if isinstance(turn_id, str) and turn_id else None


def _text_values(value: Any) -> Iterator[str]:
    """Yield string leaves from structured tool output."""

    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _text_values(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _text_values(item)


def _message_text_values(value: Any) -> Iterator[str]:
    """Yield only assistant-authored text fields from final content blocks."""

    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _message_text_values(item)
    elif isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str):
            yield text
