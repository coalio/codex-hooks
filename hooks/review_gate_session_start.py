#!/usr/bin/env python3
"""Record the initial repository state for a Codex review-gate session."""

from review_gate_common import (
    ReviewGateError, hook_repository, hook_session_id, load_session_state,
    new_session_state, read_hook_input, repository_fingerprint, write_hook_output,
    write_session_state,
)


def main() -> None:
    """Initialize one immutable baseline and preserve it across resumed contexts."""

    # Resolve the authoritative session repository before creating state.
    payload = read_hook_input("SessionStart")
    repository = hook_repository(payload)
    if repository is None:
        write_hook_output({"continue": True})
        return

    # Preserve the original baseline across resume, clear, and compaction.
    session_id = hook_session_id(payload)
    if load_session_state(repository, session_id) is not None:
        write_hook_output({"continue": True})
        return
    state = new_session_state(
        repository, session_id, repository_fingerprint(repository),
        payload.get("turn_id"), str(payload.get("source", "unknown")),
    )
    write_session_state(repository, session_id, state)
    write_hook_output({"continue": True})


if __name__ == "__main__":
    try:
        main()
    except ReviewGateError as exc:
        write_hook_output({
            "continue": True,
            "systemMessage": f"Review-gate baseline could not be initialized; Stop will fail closed. {exc}",
        })
