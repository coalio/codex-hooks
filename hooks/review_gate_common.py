#!/usr/bin/env python3
"""Shared state, Git, routing, and identity logic for the global review gate."""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


FINGERPRINT_VERSION = 1
STATE_SCHEMA_VERSION = 2
STATE_DIRECTORY_NAME = "codex-review-gate"
CLASSIFIER_NAME = "architecture_scope_classifier"
REVIEWER_NAME = "architecture_reviewer"
CONCLUSION_NAME = "conclusion_writer"
EXEMPT_REMOTE = "github.com/greysteelcompany/salesforce"
_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CLASSIFIER_PATTERN = re.compile(
    r"\A(ARCHITECTURE_REQUIRED|ARCHITECTURE_NOT_REQUIRED)\n"
    r"reason: ([^\r\n]+)\r?\n?\Z"
)
_CONCLUSION_HEADERS = (
    "## What changed",
    "## How it was implemented",
    "## Problems found and resolved",
    "## Validation",
)
_ENGINEERING_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cs", ".css", ".ex", ".exs", ".go", ".h",
    ".hpp", ".html", ".java", ".js", ".jsx", ".kt", ".kts", ".lua",
    ".php", ".proto", ".py", ".rb", ".rs", ".scala", ".sh", ".sql",
    ".svelte", ".swift", ".tsx", ".ts", ".vue",
}
_ENGINEERING_NAMES = {
    "cargo.lock", "cargo.toml", "compose.yaml", "compose.yml", "dockerfile",
    "gemfile", "gemfile.lock", "go.mod", "go.sum", "package-lock.json",
    "package.json", "pnpm-lock.yaml", "poetry.lock", "pom.xml", "pyproject.toml",
    "requirements.txt", "terraform.lock.hcl", "yarn.lock",
}
_ENGINEERING_PARTS = {
    ".github/workflows", "ansible", "charts", "deploy", "docker", "helm",
    "infra", "infrastructure", "k8s", "kubernetes", "migrations", "terraform",
}


class ReviewGateError(RuntimeError):
    """Raised when the gate cannot establish trustworthy state."""


def read_hook_input(expected_event: str) -> dict[str, Any]:
    """Read and validate the JSON object supplied to a command hook."""

    # Fail closed when the hook receives malformed or event-mismatched input.
    try:
        payload = json.load(os.sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        raise ReviewGateError(f"invalid hook input: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReviewGateError("hook input must be a JSON object")
    if payload.get("hook_event_name") != expected_event:
        raise ReviewGateError(
            f"expected {expected_event} hook input, received "
            f"{payload.get('hook_event_name')!r}"
        )
    return payload


def write_hook_output(payload: dict[str, Any]) -> None:
    """Emit one compact JSON response for Codex."""

    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def discover_repository(cwd: str | os.PathLike[str]) -> Path | None:
    """Return the containing Git worktree root, or None outside a worktree."""

    # Treat a non-repository working directory as outside this gate.
    result = _run_git_raw(cwd, ["rev-parse", "--show-toplevel"])
    if result.returncode != 0:
        return None
    root = os.fsdecode(result.stdout.rstrip(b"\n"))
    if not root:
        raise ReviewGateError("Git returned an empty repository root")
    return Path(root).resolve()


def canonical_remote(repository: Path) -> str | None:
    """Return the normalized canonical remote, preferring origin then a sole remote."""

    # Prefer origin because it is Git's conventional canonical working remote.
    remotes = _run_git(repository, ["remote"]).stdout.decode("utf-8").splitlines()
    if "origin" in remotes:
        selected = "origin"
    elif len(remotes) == 1:
        selected = remotes[0]
    else:
        return None
    result = _run_git(repository, ["remote", "get-url", selected], check=False)
    if result.returncode != 0:
        return None
    return normalize_remote(result.stdout.decode("utf-8").strip())


def normalize_remote(value: str) -> str | None:
    """Normalize HTTPS, SSH URL, and SCP-like Git remotes to host/owner/repository."""

    # Parse SCP-like SSH forms before URL parsing treats the colon as a scheme.
    candidate = value.strip()
    scp_match = re.fullmatch(r"(?:[^@/]+@)?([^:/]+):/?(.+)", candidate)
    if scp_match and "://" not in candidate:
        host, path = scp_match.groups()
    else:
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https", "ssh", "git"} or not parsed.hostname:
            return None
        host, path = parsed.hostname, parsed.path
    normalized_path = path.strip("/")
    if normalized_path.lower().endswith(".git"):
        normalized_path = normalized_path[:-4]
    if not normalized_path or normalized_path.count("/") < 1:
        return None
    return f"{host.lower()}/{normalized_path.lower()}"


def is_exempt_repository(state: dict[str, Any], repository: Path) -> bool:
    """Require the initial and current canonical remote to match the exact exemption."""

    baseline_remote = state.get("baseline", {}).get("canonical_remote")
    return baseline_remote == EXEMPT_REMOTE and canonical_remote(repository) == EXEMPT_REMOTE


def repository_fingerprint(repository: Path) -> str:
    """Hash HEAD, index, tracked worktree changes, submodules, and untracked files."""

    # Bind the digest to its format and canonical worktree.
    digest = hashlib.sha256()
    _add_digest_record(digest, b"fingerprint-version", str(FINGERPRINT_VERSION).encode())
    _add_digest_record(digest, b"repository", os.fsencode(str(repository.resolve())))
    head = _run_git(repository, ["rev-parse", "--verify", "HEAD"], check=False)
    _add_digest_record(digest, b"head", head.stdout if head.returncode == 0 else b"UNBORN")
    index = _run_git(repository, ["ls-files", "--stage", "-z"]).stdout
    _add_digest_record(digest, b"index", index)

    # Capture tracked worktree and status state without refreshing the index.
    unstaged = _run_git(
        repository,
        ["diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv", "--submodule=diff"],
    ).stdout
    _add_digest_record(digest, b"unstaged", unstaged)
    status_output = _run_git(
        repository,
        ["status", "--porcelain=v2", "-z", "--untracked-files=all", "--ignore-submodules=none"],
    ).stdout
    _add_digest_record(digest, b"status", status_output)

    # Include raw content for every non-ignored untracked path.
    untracked = _run_git(
        repository, ["ls-files", "--others", "--exclude-standard", "-z"]
    ).stdout.split(b"\0")
    for relative_path in sorted(path for path in untracked if path):
        _hash_worktree_path(digest, repository, relative_path, b"untracked")

    # Recurse into initialized submodules so internal edits invalidate approvals.
    for relative_path in _gitlink_paths(index):
        absolute_path = repository / os.fsdecode(relative_path)
        child = discover_repository(absolute_path) if absolute_path.exists() else None
        child_fingerprint = (
            repository_fingerprint(child).encode()
            if child is not None and child != repository
            else b"UNAVAILABLE"
        )
        _add_digest_record(digest, b"submodule-path", relative_path)
        _add_digest_record(digest, b"submodule-fingerprint", child_fingerprint)
    return digest.hexdigest()


def repository_path_state(repository: Path) -> dict[str, str]:
    """Build a path-level snapshot used only to describe this session's net delta."""

    # Hash tracked and non-ignored untracked paths without storing file contents.
    paths = _run_git(
        repository, ["ls-files", "--cached", "--others", "--exclude-standard", "-z"]
    ).stdout.split(b"\0")
    state: dict[str, str] = {}
    for raw_path in sorted(path for path in paths if path):
        relative_path = os.fsdecode(raw_path)
        digest = hashlib.sha256()
        _hash_worktree_path(digest, repository, raw_path, b"path-state")
        index_rows = _run_git(
            repository, ["ls-files", "--stage", "-z", "--", relative_path]
        ).stdout
        _add_digest_record(digest, b"index", index_rows)
        state[relative_path] = digest.hexdigest()
    return state


def session_delta_manifest(state: dict[str, Any], repository: Path) -> list[dict[str, str]]:
    """Return a compact exact path list changed from the session baseline."""

    # Compare path snapshots so pre-existing dirty work is not attributed to this session.
    baseline = state["baseline"]
    before = baseline["path_state"]
    after = repository_path_state(repository)
    manifest: list[dict[str, str]] = []
    for path in sorted(set(before) | set(after)):
        if before.get(path) == after.get(path):
            continue
        status = "added" if path not in before else "deleted" if path not in after else "modified"
        manifest.append({"path": path, "status": status})

    # Represent commit-only and remote-identity changes without inventing file paths.
    current_head = _head(repository)
    if current_head != baseline.get("head"):
        manifest.append({"path": "@HEAD", "status": "modified"})
    current_remote = canonical_remote(repository)
    if current_remote != baseline.get("canonical_remote"):
        manifest.append({"path": "@REMOTE", "status": "modified"})
    return manifest


def session_delta_artifact(state: dict[str, Any]) -> str | None:
    """Return the protected exact-delta artifact recorded for the current state."""

    delta = state.get("delta")
    artifact = delta.get("session_delta_artifact") if isinstance(delta, dict) else None
    return artifact if isinstance(artifact, str) and artifact else None


def manifest_route(manifest: list[dict[str, str]]) -> str:
    """Route obvious engineering deltas directly; send all other deltas to classification."""

    # Special state changes require human-readable classification rather than guesses.
    for entry in manifest:
        path = entry["path"]
        if path.startswith("@"):
            continue
        lowered = path.lower()
        name = Path(lowered).name
        if Path(lowered).suffix in _ENGINEERING_SUFFIXES or name in _ENGINEERING_NAMES:
            return "architecture_reviewer"
        if any(part in lowered for part in _ENGINEERING_PARTS):
            return "architecture_reviewer"
    return "architecture_scope_classifier"


def new_session_state(
    repository: Path, session_id: str, fingerprint: str, turn_id: str | None, source: str
) -> dict[str, Any]:
    """Build the immutable baseline for a newly observed Codex session."""

    # Preserve exact content only for paths already dirty when the session starts.
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "session_id": session_id,
        "repository": str(repository.resolve()),
        "baseline": {
            "fingerprint": fingerprint,
            "path_state": repository_path_state(repository),
            "head": _head(repository),
            "canonical_remote": canonical_remote(repository),
            "dirty_snapshots": _capture_dirty_baseline(repository, session_id),
            "turn_id": turn_id,
            "source": source,
            "recorded_at": _utc_now(),
        },
        "delta": None,
        "classifier": None,
        "reviewer": None,
        "conclusion": None,
    }
    if repository_fingerprint(repository) != fingerprint:
        raise ReviewGateError("repository changed while capturing the session baseline")
    return state


def update_delta(state: dict[str, Any], repository: Path, fingerprint: str) -> list[dict[str, str]]:
    """Persist the current compact session-delta manifest for agent handoff."""

    manifest = session_delta_manifest(state, repository)
    state["delta"] = {
        "fingerprint": fingerprint,
        "manifest": manifest,
        "current_head": _head(repository),
        "recorded_at": _utc_now(),
    }
    artifact, artifact_sha256 = _write_session_delta_artifact(
        state, repository, fingerprint, manifest,
    )
    state["delta"]["session_delta_artifact"] = str(artifact)
    state["delta"]["session_delta_sha256"] = artifact_sha256
    return manifest


def record_agent_result(
    state: dict[str, Any], role: str, fingerprint: str, turn_id: str | None,
    agent_id: str, final_message: str, recorded_at: str | None = None,
) -> None:
    """Record one strictly parsed classifier, reviewer, or conclusion result."""

    # Parse role-specific output before minting fingerprint-bound evidence.
    if role == REVIEWER_NAME:
        outcome = "PASS" if is_valid_pass(final_message) else "FAIL"
        reason = None if outcome == "PASS" else final_message
    elif role == CLASSIFIER_NAME:
        parsed = parse_classifier_result(final_message)
        if parsed is None:
            state["classifier"] = None
            return
        outcome, reason = parsed
    elif role == CONCLUSION_NAME:
        if not is_valid_conclusion(final_message):
            state["conclusion"] = None
            return
        outcome = "READY"
        reason = None
    else:
        raise ReviewGateError(f"unsupported gate role: {role}")
    state[_result_key(role)] = {
        "fingerprint": fingerprint,
        "turn_id": turn_id,
        "agent_id": agent_id,
        "role": role,
        "outcome": outcome,
        "reason": reason,
        "message": final_message if role == CONCLUSION_NAME else None,
        "message_sha256": hashlib.sha256(final_message.encode()).hexdigest(),
        "recorded_at": recorded_at or _utc_now(),
    }


def clear_agent_result(state: dict[str, Any], role: str) -> None:
    """Revoke stale or malformed evidence for one gate role."""

    state[_result_key(role)] = None


def current_result(state: dict[str, Any], role: str, fingerprint: str) -> dict[str, Any] | None:
    """Return a role result only when it covers the exact current fingerprint."""

    result = state.get(_result_key(role))
    return result if isinstance(result, dict) and result.get("fingerprint") == fingerprint else None


def is_valid_pass(message: Any) -> bool:
    """Accept only a PASS-only reviewer result."""

    return isinstance(message, str) and message.rstrip("\r\n") == "PASS"


def is_valid_review_result(message: Any) -> bool:
    """Accept exact PASS or a structured FAIL finding response."""

    if is_valid_pass(message):
        return True
    return isinstance(message, str) and message.startswith("FAIL\n\n[")


def parse_classifier_result(message: Any) -> tuple[str, str] | None:
    """Parse the classifier's exact two-line contract."""

    if not isinstance(message, str):
        return None
    match = _CLASSIFIER_PATTERN.fullmatch(message)
    return (match.group(1), match.group(2)) if match else None


def is_valid_conclusion(message: Any) -> bool:
    """Require the complete diff-based conclusion contract with substantive prose."""

    # Reject preambles, duplicate headings, and status-only closeouts.
    if not isinstance(message, str) or len(message.strip()) < 240:
        return False
    normalized = message.strip()
    lines = normalized.splitlines()
    if lines[0] != _CONCLUSION_HEADERS[0]:
        return False
    if any(lines.count(header) != 1 for header in _CONCLUSION_HEADERS):
        return False
    positions = [normalized.find(header) for header in _CONCLUSION_HEADERS]
    if positions != sorted(positions):
        return False

    # Require content beneath every header rather than header-only boilerplate.
    boundaries = positions[1:] + [len(normalized)]
    for position, boundary, header in zip(positions, boundaries, _CONCLUSION_HEADERS):
        content = normalized[position + len(header):boundary].strip()
        if len(content) < 20:
            return False
    return True


def trusted_agent_role(payload: dict[str, Any]) -> str | None:
    """Identify a gate agent from native metadata or immutable transcript metadata."""

    # Prefer the native custom role when the hook exposes it directly.
    native_role = payload.get("agent_type")
    if native_role in {CLASSIFIER_NAME, REVIEWER_NAME, CONCLUSION_NAME}:
        return native_role

    # Current Codex may expose a task path, so inspect the child session envelope.
    transcript_path = payload.get("agent_transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    try:
        with open(transcript_path, encoding="utf-8") as transcript:
            first_record = transcript.readline(4 * 1024 * 1024)
    except OSError as exc:
        raise ReviewGateError(f"cannot read gate-agent transcript metadata: {exc}") from exc
    if not first_record.endswith("\n"):
        raise ReviewGateError("gate-agent transcript metadata is missing or too large")
    try:
        record = json.loads(first_record)
    except json.JSONDecodeError as exc:
        raise ReviewGateError(f"gate-agent transcript metadata is invalid: {exc}") from exc
    metadata = record.get("payload") if isinstance(record, dict) else None
    role = metadata.get("agent_role") if isinstance(metadata, dict) else None
    return role if role in {CLASSIFIER_NAME, REVIEWER_NAME, CONCLUSION_NAME} else None


def _result_key(role: str) -> str:
    """Map each trusted role to its isolated state slot."""

    if role == CLASSIFIER_NAME:
        return "classifier"
    if role == REVIEWER_NAME:
        return "reviewer"
    if role == CONCLUSION_NAME:
        return "conclusion"
    raise ReviewGateError(f"unsupported gate role: {role}")


def baseline_matches(state: dict[str, Any], fingerprint: str) -> bool:
    """Return whether the repository equals its session-start fingerprint."""

    return state.get("baseline", {}).get("fingerprint") == fingerprint


def load_session_state(repository: Path, session_id: str) -> dict[str, Any] | None:
    """Load and strictly validate session state for one repository."""

    path = session_state_path(repository, session_id)
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewGateError(f"cannot read review-gate state {path}: {exc}") from exc
    _validate_session_state(state, repository, session_id)
    return state


def write_session_state(repository: Path, session_id: str, state: dict[str, Any]) -> None:
    """Atomically persist validated state below worktree-specific Git metadata."""

    _validate_session_state(state, repository, session_id)
    path = session_state_path(repository, session_id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary_name = handle.name
            os.chmod(temporary_name, 0o600)
            json.dump(state, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except OSError as exc:
        raise ReviewGateError(f"cannot write review-gate state {path}: {exc}") from exc
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def session_state_path(repository: Path, session_id: str) -> Path:
    """Resolve session-isolated state below the worktree's Git directory."""

    if not session_id:
        raise ReviewGateError("hook input is missing session_id")
    session_key = hashlib.sha256(session_id.encode()).hexdigest()
    return _absolute_git_directory(repository) / STATE_DIRECTORY_NAME / "sessions" / f"{session_key}.json"


def hook_session_id(payload: dict[str, Any]) -> str:
    """Extract the required opaque Codex session identifier."""

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ReviewGateError("hook input is missing a valid session_id")
    return session_id


def hook_repository(payload: dict[str, Any]) -> Path | None:
    """Resolve the repository from the hook's authoritative session cwd."""

    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        raise ReviewGateError("hook input is missing a valid cwd")
    return discover_repository(cwd)


def _capture_dirty_baseline(repository: Path, session_id: str) -> dict[str, dict[str, Any]]:
    """Snapshot paths whose pre-session Git state cannot be reconstructed from HEAD."""

    # Limit protected snapshots to staged, unstaged, and untracked baseline paths.
    dirty_paths: set[bytes] = set()
    for arguments in (
        ["diff", "--name-only", "--no-renames", "-z"],
        ["diff", "--cached", "--name-only", "--no-renames", "-z"],
        ["ls-files", "--others", "--exclude-standard", "-z"],
    ):
        dirty_paths.update(path for path in _run_git(repository, arguments).stdout.split(b"\0") if path)

    # Store raw baseline bytes outside JSON with restrictive permissions.
    artifact_directory = _session_artifact_directory(repository, session_id)
    artifact_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    snapshots: dict[str, dict[str, Any]] = {}
    for raw_path in sorted(dirty_paths):
        relative_path = os.fsdecode(raw_path)
        material = _path_material(repository, relative_path)
        content = material.pop("content")
        snapshot_path = artifact_directory / f"baseline-{hashlib.sha256(raw_path).hexdigest()}.bin"
        _atomic_write_bytes(snapshot_path, content)
        material["content_file"] = str(snapshot_path)
        material["content_sha256"] = hashlib.sha256(content).hexdigest()
        snapshots[relative_path] = material
    return snapshots


def _write_session_delta_artifact(
    state: dict[str, Any], repository: Path, fingerprint: str,
    manifest: list[dict[str, str]],
) -> tuple[Path, str]:
    """Write an exact reconstructable delta for pre-existing dirty paths changed now."""

    # Compare protected baseline bytes only for paths that changed during this session.
    baseline = state.get("baseline")
    snapshots = baseline.get("dirty_snapshots") if isinstance(baseline, dict) else None
    if not isinstance(snapshots, dict):
        raise ReviewGateError(
            "session baseline predates exact dirty-path snapshots; start a fresh Codex session"
        )
    changed_paths = {entry["path"] for entry in manifest if not entry["path"].startswith("@")}
    exact_paths: list[dict[str, Any]] = []
    expected_directory = _session_artifact_directory(repository, str(state["session_id"])).resolve()
    for relative_path in sorted(changed_paths & set(snapshots)):
        baseline_material = snapshots[relative_path]
        if not isinstance(baseline_material, dict):
            raise ReviewGateError("session baseline has invalid dirty-path snapshot metadata")
        content_file = baseline_material.get("content_file")
        if not isinstance(content_file, str):
            raise ReviewGateError("session baseline snapshot is missing its protected content")
        snapshot_path = Path(content_file).resolve()
        if snapshot_path.parent != expected_directory or not snapshot_path.is_file():
            raise ReviewGateError("session baseline snapshot escaped its protected directory")
        baseline_content = snapshot_path.read_bytes()
        if hashlib.sha256(baseline_content).hexdigest() != baseline_material.get("content_sha256"):
            raise ReviewGateError("session baseline snapshot content no longer matches its hash")

        # Include exact bytes plus a readable session-only diff when both sides are UTF-8 text.
        current_material = _path_material(repository, relative_path)
        current_content = current_material.pop("content")
        exact_paths.append({
            "path": relative_path,
            "baseline": {
                key: value for key, value in baseline_material.items() if key != "content_file"
            } | {"content_base64": base64.b64encode(baseline_content).decode("ascii")},
            "current": current_material | {
                "content_sha256": hashlib.sha256(current_content).hexdigest(),
                "content_base64": base64.b64encode(current_content).decode("ascii"),
            },
            "session_text_diff": _text_delta(relative_path, baseline_content, current_content),
        })

    # Bind the artifact to the same baseline and current fingerprints as the gate state.
    artifact_payload = {
        "schema_version": 1,
        "repository": str(repository.resolve()),
        "baseline_fingerprint": baseline.get("fingerprint"),
        "current_fingerprint": fingerprint,
        "scope": "session-only changes to paths dirty at session start",
        "paths": exact_paths,
    }
    artifact_bytes = (
        json.dumps(artifact_payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    artifact_path = expected_directory / "session-delta.json"
    _atomic_write_bytes(artifact_path, artifact_bytes)
    return artifact_path, hashlib.sha256(artifact_bytes).hexdigest()


def _path_material(repository: Path, relative_path: str) -> dict[str, Any]:
    """Capture exact worktree bytes, mode, kind, and index identity for one path."""

    raw_path = os.fsencode(relative_path)
    absolute_path = os.path.join(os.fsencode(repository), raw_path)
    index_rows = _run_git(
        repository, ["ls-files", "--stage", "-z", "--", relative_path]
    ).stdout
    try:
        metadata = os.lstat(absolute_path)
    except FileNotFoundError:
        return {
            "kind": "missing", "mode": None,
            "index_base64": base64.b64encode(index_rows).decode("ascii"), "content": b"",
        }
    except OSError as exc:
        raise ReviewGateError(f"cannot snapshot {relative_path!r}: {exc}") from exc
    mode = stat.S_IFMT(metadata.st_mode) | (metadata.st_mode & 0o111)
    if stat.S_ISREG(metadata.st_mode):
        try:
            with open(absolute_path, "rb") as handle:
                before = os.fstat(handle.fileno())
                content = handle.read()
                after = os.fstat(handle.fileno())
        except OSError as exc:
            raise ReviewGateError(f"cannot snapshot {relative_path!r}: {exc}") from exc
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ReviewGateError(f"file changed while snapshotting: {relative_path!r}")
        kind = "file"
    elif stat.S_ISLNK(metadata.st_mode):
        content = os.readlink(absolute_path)
        kind = "symlink"
    else:
        content = b""
        kind = "special"
    return {
        "kind": kind, "mode": mode,
        "index_base64": base64.b64encode(index_rows).decode("ascii"), "content": content,
    }


def _text_delta(relative_path: str, baseline: bytes, current: bytes) -> str | None:
    """Render an exact unified diff when both snapshots are ordinary UTF-8 text."""

    if b"\0" in baseline or b"\0" in current:
        return None
    try:
        before = baseline.decode("utf-8").splitlines(keepends=True)
        after = current.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return None
    return "".join(difflib.unified_diff(
        before, after, fromfile=f"session-start/{relative_path}",
        tofile=f"current/{relative_path}", lineterm="\n",
    ))


def _session_artifact_directory(repository: Path, session_id: str) -> Path:
    """Resolve the protected artifact directory adjacent to one session state file."""

    state_path = session_state_path(repository, session_id)
    return state_path.parent / f"{state_path.stem}.artifacts"


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    """Write one protected artifact atomically without following a final symlink."""

    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False,
        ) as handle:
            temporary_name = handle.name
            os.chmod(temporary_name, 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except OSError as exc:
        raise ReviewGateError(f"cannot write protected gate artifact {path}: {exc}") from exc
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _head(repository: Path) -> str:
    """Return HEAD or the explicit unborn marker."""

    result = _run_git(repository, ["rev-parse", "--verify", "HEAD"], check=False)
    return result.stdout.decode().strip() if result.returncode == 0 else "UNBORN"


def _run_git(repository: str | os.PathLike[str], arguments: list[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    """Run Git without optional locks and return byte-exact output."""

    result = _run_git_raw(repository, arguments)
    if check and result.returncode != 0:
        error = os.fsdecode(result.stderr).strip() or "unknown Git error"
        raise ReviewGateError(f"git {' '.join(arguments)} failed: {error}")
    return result


def _run_git_raw(repository: str | os.PathLike[str], arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
    """Execute one local Git command with stable locale and no index refresh."""

    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["LC_ALL"] = "C"
    try:
        return subprocess.run(
            ["git", "-C", os.fspath(repository), *arguments], check=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
        )
    except OSError as exc:
        raise ReviewGateError(f"cannot execute Git: {exc}") from exc


def _absolute_git_directory(repository: Path) -> Path:
    """Resolve the worktree-specific Git metadata directory."""

    result = _run_git(repository, ["rev-parse", "--path-format=absolute", "--git-dir"])
    path = os.fsdecode(result.stdout.rstrip(b"\n"))
    if not path:
        raise ReviewGateError("Git returned an empty metadata directory")
    return Path(path).resolve()


def _add_digest_record(digest: "hashlib._Hash", label: bytes, payload: bytes) -> None:
    """Frame digest records so paths and content cannot collide."""

    for value in (label, payload):
        digest.update(len(value).to_bytes(8, byteorder="big", signed=False))
        digest.update(value)


def _hash_worktree_path(digest: "hashlib._Hash", repository: Path, relative_path: bytes, category: bytes) -> None:
    """Hash one filesystem entry without following symlinks."""

    absolute_path = os.path.join(os.fsencode(repository), relative_path)
    try:
        metadata = os.lstat(absolute_path)
    except FileNotFoundError:
        _add_digest_record(digest, b"missing", relative_path)
        return
    except OSError as exc:
        raise ReviewGateError(f"cannot inspect {os.fsdecode(relative_path)!r}: {exc}") from exc
    mode = stat.S_IFMT(metadata.st_mode) | (metadata.st_mode & 0o111)
    _add_digest_record(digest, b"path-category", category)
    _add_digest_record(digest, b"path", relative_path)
    _add_digest_record(digest, b"mode", str(mode).encode())
    if stat.S_ISREG(metadata.st_mode):
        content_digest = hashlib.sha256()
        with open(absolute_path, "rb") as handle:
            before = os.fstat(handle.fileno())
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                content_digest.update(chunk)
            after = os.fstat(handle.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ReviewGateError(f"file changed while fingerprinting: {os.fsdecode(relative_path)!r}")
        _add_digest_record(digest, b"content-sha256", content_digest.digest())
    elif stat.S_ISLNK(metadata.st_mode):
        _add_digest_record(digest, b"symlink-target", os.readlink(absolute_path))
    else:
        _add_digest_record(digest, b"special-node", f"{metadata.st_rdev}:{metadata.st_size}".encode())


def _gitlink_paths(index: bytes) -> list[bytes]:
    """Extract stage-zero submodule paths from index rows."""

    paths: list[bytes] = []
    for row in index.split(b"\0"):
        if row.startswith(b"160000 ") and b"\t" in row:
            metadata, path = row.split(b"\t", 1)
            if metadata.endswith(b" 0"):
                paths.append(path)
    return sorted(paths)


def _validate_session_state(state: Any, repository: Path, session_id: str) -> None:
    """Reject corrupt, cross-session, cross-repository, or forged-looking state."""

    # Validate the immutable session envelope and path-level baseline.
    if not isinstance(state, dict) or state.get("schema_version") != STATE_SCHEMA_VERSION:
        raise ReviewGateError("review-gate state has an unsupported schema")
    if state.get("session_id") != session_id:
        raise ReviewGateError("review-gate state belongs to another session")
    if state.get("repository") != str(repository.resolve()):
        raise ReviewGateError("review-gate state belongs to another repository")
    baseline = state.get("baseline")
    if not isinstance(baseline, dict) or not _valid_fingerprint(baseline.get("fingerprint")):
        raise ReviewGateError("review-gate state has an invalid baseline")
    if not isinstance(baseline.get("path_state"), dict):
        raise ReviewGateError("review-gate state lacks a path baseline")

    # Validate every optional result before Stop is allowed to trust it.
    for key, role in (
        ("classifier", CLASSIFIER_NAME),
        ("reviewer", REVIEWER_NAME),
        ("conclusion", CONCLUSION_NAME),
    ):
        result = state.get(key)
        if result is None:
            continue
        valid_outcomes = {
            CLASSIFIER_NAME: {"ARCHITECTURE_REQUIRED", "ARCHITECTURE_NOT_REQUIRED"},
            REVIEWER_NAME: {"PASS", "FAIL"},
            CONCLUSION_NAME: {"READY"},
        }[role]
        if (
            not isinstance(result, dict)
            or result.get("role") != role
            or result.get("outcome") not in valid_outcomes
            or not _valid_fingerprint(result.get("fingerprint"))
            or not isinstance(result.get("agent_id"), str)
            or not result.get("agent_id")
            or not _valid_fingerprint(result.get("message_sha256"))
        ):
            raise ReviewGateError(f"review-gate state has an invalid {key} result")
        if role == CONCLUSION_NAME and not is_valid_conclusion(result.get("message")):
            raise ReviewGateError("review-gate state has an invalid conclusion message")


def _valid_fingerprint(value: Any) -> bool:
    """Recognize the lowercase SHA-256 representation used in state."""

    return isinstance(value, str) and _FINGERPRINT_PATTERN.fullmatch(value) is not None


def _utc_now() -> str:
    """Return a stable UTC timestamp for audit state."""

    return datetime.now(timezone.utc).isoformat()
