#!/usr/bin/env python3
"""Compatibility Stop entrypoint for sessions started before the gate migration."""

from __future__ import annotations

from review_gate_common import (
    EXEMPT_REMOTE,
    ReviewGateError,
    canonical_remote,
    hook_repository,
    read_hook_input,
    stop_without_approval,
    write_hook_output,
)


def main() -> None:
    """Release exempt legacy sessions and fail closed everywhere else."""

    # Old sessions cached this command before the generic gate became active.
    payload = read_hook_input("Stop")
    repository = hook_repository(payload)
    if repository is None or canonical_remote(repository) == EXEMPT_REMOTE:
        write_hook_output({"continue": True})
        return

    # A legacy session has no trustworthy generic baseline, so require a fresh session.
    stop_without_approval(
        "This session started before the context-aware review gate was activated.",
    )


if __name__ == "__main__":
    try:
        main()
    except ReviewGateError as exc:
        stop_without_approval(f"Legacy Stop compatibility failed closed: {exc}.")
