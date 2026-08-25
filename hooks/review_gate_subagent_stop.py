#!/usr/bin/env python3
"""Record trusted classifier, reviewer, and conclusion outcomes."""

from review_gate_common import (
    CLASSIFIER_NAME, CONCLUSION_NAME, REVIEWER_NAME, ReviewGateError,
    clear_agent_result, hook_repository, hook_session_id, is_valid_conclusion,
    is_valid_review_result, load_session_state, parse_classifier_result,
    read_hook_input, record_agent_result,
    repository_fingerprint, trusted_agent_role, write_hook_output,
    write_session_state,
)


def main() -> None:
    """Bind a strict gate-agent result to the exact current fingerprint."""

    # Ignore ordinary subagents because this hook observes every stopped agent.
    payload = read_hook_input("SubagentStop")
    role = trusted_agent_role(payload)
    if role not in {CLASSIFIER_NAME, REVIEWER_NAME, CONCLUSION_NAME}:
        write_hook_output({"continue": True})
        return
    repository = hook_repository(payload)
    if repository is None:
        write_hook_output({"continue": True})
        return

    # Require this parent session's baseline before accepting evidence.
    session_id = hook_session_id(payload)
    state = load_session_state(repository, session_id)
    if state is None:
        raise ReviewGateError("session baseline is missing")
    message = payload.get("last_assistant_message")
    agent_id = str(payload.get("agent_id") or "")
    if role == REVIEWER_NAME:
        valid = is_valid_review_result(message)
    elif role == CLASSIFIER_NAME:
        valid = parse_classifier_result(message) is not None
    else:
        valid = is_valid_conclusion(message)
    if valid and agent_id:
        record_agent_result(
            state, role, repository_fingerprint(repository), payload.get("turn_id"), agent_id, message,
        )
    else:
        clear_agent_result(state, role)
    write_session_state(repository, session_id, state)
    write_hook_output({"continue": True})


if __name__ == "__main__":
    try:
        main()
    except ReviewGateError as exc:
        write_hook_output({"continue": False, "stopReason": f"Review-gate result could not be recorded: {exc}"})
