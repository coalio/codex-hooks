#!/usr/bin/env python3
"""Print a read-only repository fingerprint for gate transcript evidence."""

from pathlib import Path

from review_gate_common import ReviewGateError, discover_repository, repository_fingerprint


def main() -> None:
    """Emit one machine-readable marker for the repository containing the cwd."""

    # Resolve from the gate agent's actual cwd so a parent cannot substitute a tree.
    repository = discover_repository(Path.cwd())
    if repository is None:
        raise ReviewGateError("review gate agent is not running in a Git repository")
    print(f"CODEX_REVIEW_GATE_FINGERPRINT={repository_fingerprint(repository)}")


if __name__ == "__main__":
    main()
