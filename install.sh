#!/bin/sh
set -eu

force=false
case "${1:-}" in
  "") ;;
  --force) force=true ;;
  *)
    echo "usage: $0 [--force]" >&2
    exit 2
    ;;
esac

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
codex_root=${CODEX_HOME:-${HOME:?HOME must be set when CODEX_HOME is unset}/.codex}
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_root="$codex_root/backups/codex-hooks-$timestamp-$$"

files='
AGENTS.md
hooks.json
agents/architecture-reviewer.toml
agents/architecture-scope-classifier.toml
agents/conclusion-writer.toml
hooks/architecture_review_stop.py
hooks/review_gate_common.py
hooks/review_gate_evidence.py
hooks/review_gate_fingerprint.py
hooks/review_gate_session_start.py
hooks/review_gate_stop.py
hooks/review_gate_subagent_stop.py
'

# Refuse before mutation when an existing installation would be overwritten implicitly.
conflicts=
for relative_path in $files; do
  source_path="$repository_root/$relative_path"
  target_path="$codex_root/$relative_path"
  if [ -e "$target_path" ] && ! cmp -s "$source_path" "$target_path"; then
    conflicts="$conflicts $relative_path"
  fi
done

if [ -n "$conflicts" ] && [ "$force" != true ]; then
  echo "Refusing to overwrite different Codex files:$conflicts" >&2
  echo "Review them, merge unrelated configuration manually, or rerun with --force." >&2
  exit 1
fi

# Back up only files that this installation will replace.
for relative_path in $conflicts; do
  target_path="$codex_root/$relative_path"
  backup_path="$backup_root/$relative_path"
  mkdir -p "$(dirname -- "$backup_path")"
  cp -p "$target_path" "$backup_path"
done

# Install the complete gate while leaving all unrelated Codex files untouched.
for relative_path in $files; do
  source_path="$repository_root/$relative_path"
  target_path="$codex_root/$relative_path"
  mkdir -p "$(dirname -- "$target_path")"
  case "$relative_path" in
    hooks/*.py) install -m 0755 "$source_path" "$target_path" ;;
    *) install -m 0644 "$source_path" "$target_path" ;;
  esac
done

echo "Installed Codex review hooks in $codex_root"
if [ -n "$conflicts" ]; then
  echo "Backed up replaced files in $backup_root"
fi
echo "Open /hooks in Codex, trust the exact definitions, then start a fresh session."
