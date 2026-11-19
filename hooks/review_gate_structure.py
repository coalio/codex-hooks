#!/usr/bin/env python3
"""Extract a read-only structural inventory from an exact session delta."""

from __future__ import annotations

import argparse
import ast
import base64
import difflib
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict

from review_gate_common import (
    ReviewGateError, discover_repository, load_session_state, repository_fingerprint,
)


class Declaration(TypedDict):
    symbol: str
    kind: str
    line: int
    signature: str
    decorators: list[str]
    fields: list[str]
    definition_hash: str


class DeclarationChange(TypedDict):
    symbol: str
    kind: str
    status: Literal["added", "modified", "deleted"]
    before: Declaration | None
    after: Declaration | None


class SurfaceInventory(TypedDict):
    surface: NotRequired[str]
    declarations: list[DeclarationChange]
    coverage: Literal["python_ast", "manual"]
    reason: str | None
    diff_excerpt: NotRequired[list[str]]
    diff_truncated: NotRequired[bool]


class FileInventory(TypedDict):
    path: str
    status: str
    name: str
    folder: str
    surfaces: list[SurfaceInventory]


class StructureInventory(TypedDict):
    schema_version: int
    fingerprint: str
    session_delta_sha256: str
    new_files: list[str]
    new_folders: list[str]
    files: list[FileInventory]
    coverage_note: str


def git_bytes(repository: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(repository), *arguments],
        capture_output=True, check=False,
    )
    if result.returncode:
        raise ReviewGateError("cannot read Git source material for structural review")
    return result.stdout


def index_content(repository: Path, rows: bytes) -> tuple[bytes, str]:
    """Read staged blobs without checking out code or following symlinks."""
    if not rows:
        return b"", "missing"
    entries = [row.split(b"\t", 1)[0].split() for row in rows.split(b"\0") if row]
    if len(entries) != 1 or entries[0][2] != b"0":
        return b"", "unmerged_index"
    mode, object_id, _ = entries[0]
    if mode not in {b"100644", b"100755"}:
        return b"", "non_file"
    return git_bytes(repository, "cat-file", "blob", object_id.decode("ascii")), "file"


def python_declarations(content: bytes) -> list[Declaration]:
    """Keep signatures, model fields and aliases separate from function bodies."""
    tree = ast.parse(content)
    declarations: list[Declaration] = []

    def visit(body: list[ast.stmt], scope: str = "") -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualified = f"{scope}.{node.name}" if scope else node.name
                decorators = [ast.unparse(value) for value in node.decorator_list]
                parameters = getattr(node, "type_params", [])
                generic = f"[{', '.join(ast.unparse(item) for item in parameters)}]" if parameters else ""
                if isinstance(node, ast.ClassDef):
                    bases = [ast.unparse(item) for item in [*node.bases, *node.keywords]]
                    signature = f"class {node.name}{generic}({', '.join(bases)}):"
                    kind = "class"
                    fields = [ast.unparse(item) for item in node.body if isinstance(item, (ast.AnnAssign, ast.Assign))]
                else:
                    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
                    signature = f"{prefix} {node.name}{generic}({ast.unparse(node.args)}){returns}:"
                    kind, fields = "function", []
                declarations.append({
                    "symbol": qualified, "kind": kind, "line": node.lineno,
                    "signature": signature, "decorators": decorators, "fields": fields,
                    "definition_hash": hashlib.sha256(ast.dump(node).encode()).hexdigest(),
                })
                visit(node.body, qualified)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if ast.unparse(node.annotation).split(".")[-1] == "TypeAlias":
                    declarations.append(alias_declaration(node, node.target.id, scope))
            elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                value = node.value
                factory = ast.unparse(value.func).split(".")[-1] if isinstance(value, ast.Call) else ""
                named_alias = node.targets[0].id[:1].isupper() and isinstance(value, (ast.Name, ast.Attribute))
                if factory in {"NewType", "TypeVar", "ParamSpec", "TypeVarTuple", "TypedDict", "NamedTuple", "TypeAliasType"} or isinstance(value, (ast.Subscript, ast.BinOp)) or named_alias:
                    declarations.append(alias_declaration(node, node.targets[0].id, scope))
            elif hasattr(ast, "TypeAlias") and isinstance(node, ast.TypeAlias):
                declarations.append(alias_declaration(node, ast.unparse(node.name), scope))
            else:
                # Conditional definitions and overloads remain visible in the inventory.
                for field in ("body", "orelse", "finalbody"):
                    nested = getattr(node, field, None)
                    if isinstance(nested, list):
                        visit(nested, scope)
                for handler in getattr(node, "handlers", []):
                    visit(handler.body, scope)
                for case in getattr(node, "cases", []):
                    visit(case.body, scope)
    visit(tree.body)
    return declarations


def alias_declaration(node: ast.AST, name: str, scope: str) -> Declaration:
    return {
        "symbol": f"{scope}.{name}" if scope else name, "kind": "type_alias_candidate",
        "line": node.lineno, "signature": ast.unparse(node), "fields": [], "decorators": [],
        "definition_hash": hashlib.sha256(ast.dump(node).encode()).hexdigest(),
    }


def declaration_changes(before: bytes, after: bytes) -> list[DeclarationChange]:
    """Include body-only changes because identical signatures can hide new behavior."""
    def keyed(content: bytes) -> dict[tuple[str, str, int], Declaration]:
        result: dict[tuple[str, str, int], Declaration] = {}
        occurrences: dict[tuple[str, str], int] = {}
        for item in python_declarations(content):
            name = item["symbol"], item["kind"]
            ordinal = occurrences.get(name, 0)
            occurrences[name] = ordinal + 1
            result[(*name, ordinal)] = item
        return result

    old, new = keyed(before), keyed(after)
    changes = []
    for key in sorted(old.keys() | new.keys()):
        left, right = old.get(key), new.get(key)
        if left and right and left["definition_hash"] == right["definition_hash"]:
            continue
        changes.append({
            "symbol": key[0], "kind": key[1],
            "status": "added" if left is None else "deleted" if right is None else "modified",
            "before": left, "after": right,
        })
    return changes


def surface_inventory(path: str, before: bytes, after: bytes, kinds: tuple[str, str]) -> SurfaceInventory:
    """Mark extraction limits explicitly so another language cannot bypass review."""
    result: SurfaceInventory = {"declarations": [], "coverage": "manual", "reason": "non_python_file"}
    if any(kind not in {"file", "missing"} for kind in kinds):
        result["reason"] = ",".join(kinds)
        return result
    if Path(path).suffix in {".py", ".pyi"}:
        try:
            result["declarations"] = declaration_changes(before, after)
            result.update(coverage="python_ast", reason=None)
            return result
        except (SyntaxError, ValueError, UnicodeError, RecursionError) as error:
            result["reason"] = f"python_parse_error:{type(error).__name__}"
    try:
        delta = list(difflib.unified_diff(
            before.decode("utf-8").splitlines(), after.decode("utf-8").splitlines(),
            fromfile=path, tofile=path, lineterm="",
        ))
        result["diff_excerpt"] = delta[:160]
        result["diff_truncated"] = len(delta) > 160
    except UnicodeError:
        result["reason"] = "binary_or_non_utf8"
    return result


def directory_names(paths: Any) -> set[str]:
    return {str(parent) for path in paths for parent in Path(path).parents if str(parent) != "."}


def build_inventory(state: dict[str, Any], repository: Path) -> StructureInventory:
    """Compare worktree and index against their own authoritative session baselines."""
    delta, baseline = state["delta"], state["baseline"]
    fingerprint = repository_fingerprint(repository)
    if delta["fingerprint"] != fingerprint:
        raise ReviewGateError("structural inventory requires a fresh session delta")
    raw = Path(delta["session_delta_artifact"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != delta["session_delta_sha256"]:
        raise ReviewGateError("protected delta hash does not match")
    artifact = json.loads(raw)
    if artifact["current_fingerprint"] != fingerprint or artifact["repository"] != str(repository.resolve()):
        raise ReviewGateError("protected delta belongs to another repository state")
    dirty = {item["path"]: item for item in artifact["paths"]}
    files = []
    for entry in delta["manifest"]:
        path = entry["path"]
        if entry.get("kind") == "state":
            continue
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ReviewGateError("structural inventory path escaped the repository")
        absolute = repository / relative
        # Parent symlinks and non-files are reported without reading external data.
        safe = absolute.parent.resolve().is_relative_to(repository.resolve())
        if path in dirty:
            original, current = dirty[path]["baseline"], dirty[path]["current"]
            before = base64.b64decode(original["content_base64"], validate=True)
            after = base64.b64decode(current["content_base64"], validate=True)
            old_index = base64.b64decode(original["index_base64"], validate=True)
            new_index = base64.b64decode(current["index_base64"], validate=True)
            old_kind, new_kind = original["kind"], current["kind"]
        else:
            exists_before = path in baseline["path_state"]
            old_index = git_bytes(repository, "ls-tree", "-z", baseline["head"], "--", path) if exists_before else b""
            if old_index:
                metadata, filename = old_index.split(b"\t", 1)
                mode, _, object_id = metadata.split()
                old_index = b" ".join((mode, object_id, b"0")) + b"\t" + filename
            before, old_kind = index_content(repository, old_index)
            new_index = git_bytes(repository, "ls-files", "--stage", "-z", "--", path)
            new_kind = "file" if safe and absolute.is_file() and not absolute.is_symlink() else "missing" if not absolute.exists() else "non_file"
            after = absolute.read_bytes() if new_kind == "file" else b""
        staged_before, staged_old_kind = index_content(repository, old_index)
        staged_after, staged_new_kind = index_content(repository, new_index)
        surfaces = []
        for surface, left, right, kinds in (
            ("worktree", before, after, (old_kind, new_kind)),
            ("index", staged_before, staged_after, (staged_old_kind, staged_new_kind)),
        ):
            if left != right or kinds[0] != kinds[1]:
                surfaces.append({"surface": surface, **surface_inventory(path, left, right, kinds)})
        files.append({**entry, "name": relative.name, "folder": str(relative.parent), "surfaces": surfaces})
    added = [entry["path"] for entry in delta["manifest"] if entry["status"] == "added" and entry.get("kind") != "state"]
    inventory: StructureInventory = {
        "schema_version": 1, "fingerprint": fingerprint,
        "session_delta_sha256": delta["session_delta_sha256"],
        "new_files": added,
        "new_folders": sorted(directory_names(added) - directory_names(baseline["path_state"])),
        "files": files,
        "coverage_note": "Python declarations are parsed without executing code, other files and parse failures require manual signature and type inspection",
    }
    if repository_fingerprint(repository) != fingerprint:
        raise ReviewGateError("repository changed during structural extraction")
    return inventory


def inventory_bytes(inventory: StructureInventory) -> bytes:
    return (json.dumps(inventory, sort_keys=True, separators=(",", ":")) + "\n").encode()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    arguments = parser.parse_args()
    repository = discover_repository(Path.cwd())
    if repository is None:
        raise ReviewGateError("structural extraction requires a Git repository")
    state = load_session_state(repository, arguments.session_id)
    if state is None or not state.get("delta"):
        raise ReviewGateError("structural extraction requires the Stop hook's current session delta")
    print(inventory_bytes(build_inventory(state, repository)).decode(), end="")


if __name__ == "__main__":
    main()
