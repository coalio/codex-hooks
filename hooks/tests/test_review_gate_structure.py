"""Structural extraction uses source syntax and exact Git session baselines."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from review_gate_common import ReviewGateError, new_session_state, repository_fingerprint, update_delta
from review_gate_structure import build_inventory, declaration_changes


class DeclarationTest(unittest.TestCase):
    def test_multiline_signatures_fields_aliases_and_body_only_edits(self):
        before = b'''from typing import TypeAlias
class Listing:
    id: str
    def retrieve_listing(self, *, limit: int = 5) -> Listing:
        return Listing()
'''
        after = b'''from typing import TypeAlias
class Listing:
    id: str
    report: str | None = None
    def retrieve_listing(self, *, limit: int = 5) -> Listing:
        return export_report()
Records: TypeAlias = list[Listing]
async def fetch(
    value: str, /, *options: str, strict: bool = True, **kwargs: int
) -> list[Listing]:
    return []
'''
        changes = {item["symbol"]: item for item in declaration_changes(before, after)}
        self.assertIn("Listing.retrieve_listing", changes)
        method = changes["Listing.retrieve_listing"]
        self.assertEqual(method["before"]["signature"], method["after"]["signature"])
        self.assertIn("report: str | None = None", changes["Listing"]["after"]["fields"])
        self.assertEqual(changes["Records"]["kind"], "type_alias_candidate")
        self.assertIn("async def fetch(value: str, /, *options: str", changes["fetch"]["after"]["signature"])

    def test_nested_definitions_overloads_and_deleted_contracts(self):
        changes = declaration_changes(b"def removed() -> int: return 1\n", b"if True:\n def added() -> str: return ''\n")
        self.assertEqual({(item["symbol"], item["status"]) for item in changes}, {("removed", "deleted"), ("added", "added")})


class StructuralInventoryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repository = Path(self.temporary.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repository / "service.py").write_text("def existing() -> int: return 1\n")
        self.git("add", ".")
        self.git("commit", "-qm", "test: baseline")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repository), *args], check=True, capture_output=True)

    def baseline(self):
        return new_session_state(self.repository, "structure-test", repository_fingerprint(self.repository), "turn", "test")

    def inventory(self, state):
        update_delta(state, self.repository, repository_fingerprint(self.repository))
        before = repository_fingerprint(self.repository)
        result = build_inventory(state, self.repository)
        self.assertEqual(before, repository_fingerprint(self.repository))
        return result

    def test_new_files_folders_and_types_after_commit_and_untracked_change(self):
        state = self.baseline()
        folder = self.repository / "provider" / "listing"
        folder.mkdir(parents=True)
        (folder / "models.py").write_text("class Listing:\n id: str\n")
        self.git("add", ".")
        self.git("commit", "-qm", "feat: listing")
        (folder / "service.py").write_text("def retrieve_listing(key: str) -> Listing: ...\n")
        result = self.inventory(state)
        self.assertEqual(result["new_folders"], ["provider", "provider/listing"])
        self.assertEqual(result["new_files"], ["provider/listing/models.py", "provider/listing/service.py"])
        self.assertEqual(result["files"][0]["surfaces"][0]["declarations"][0]["after"]["fields"], ["id: str"])

    def test_dirty_baseline_does_not_claim_prior_symbols(self):
        source = self.repository / "service.py"
        source.write_text(source.read_text() + "def earlier_user_work() -> str: return ''\n")
        state = self.baseline()
        source.write_text(source.read_text() + "def new_work() -> bool: return True\n")
        result = self.inventory(state)
        symbols = [item["symbol"] for item in result["files"][0]["surfaces"][0]["declarations"]]
        self.assertEqual(symbols, ["new_work"])

    def test_at_prefixed_paths_are_real_files_even_when_head_also_changes(self):
        state = self.baseline()
        (self.repository / "@types").mkdir()
        (self.repository / "@types/listing.ts").write_text("export interface Listing { id: string }\n")
        (self.repository / "@HEAD").write_text("This is an actual file\n")
        self.git("add", ".")
        self.git("commit", "-qm", "feat: scoped types")
        result = self.inventory(state)
        self.assertEqual(result["new_files"], ["@HEAD", "@types/listing.ts"])
        self.assertEqual(result["new_folders"], ["@types"])
        self.assertEqual([item["path"] for item in result["files"]], ["@HEAD", "@types/listing.ts"])

    def test_index_only_change_remains_visible_when_worktree_is_restored(self):
        state = self.baseline()
        source = self.repository / "service.py"
        original = source.read_text()
        source.write_text("def staged_contract() -> bytes: return b''\n")
        self.git("add", "service.py")
        source.write_text(original)
        result = self.inventory(state)
        surfaces = result["files"][0]["surfaces"]
        self.assertEqual([item["surface"] for item in surfaces], ["index"])
        self.assertIn("staged_contract", [item["symbol"] for item in surfaces[0]["declarations"]])

    def test_preexisting_staged_and_worktree_edits_use_distinct_baselines(self):
        source = self.repository / "service.py"
        source.write_text("def old_staged() -> int: return 1\n")
        self.git("add", "service.py")
        source.write_text("def old_worktree() -> str: return ''\n")
        state = self.baseline()
        source.write_text("def old_staged() -> int: return 2\n")
        self.git("add", "service.py")
        source.write_text("def old_worktree() -> str: return 'new'\n")
        surfaces = self.inventory(state)["files"][0]["surfaces"]
        self.assertEqual(surfaces[0]["declarations"][0]["symbol"], "old_worktree")
        self.assertEqual(surfaces[1]["declarations"][0]["symbol"], "old_staged")

    def test_parse_errors_other_languages_and_symlinks_require_manual_review(self):
        state = self.baseline()
        (self.repository / "broken.py").write_text("def incomplete(\n")
        (self.repository / "service.ts").write_text("export interface Listing { id: string }\n")
        (self.repository / "external.py").symlink_to("/etc/hosts")
        result = self.inventory(state)
        for file in result["files"]:
            self.assertEqual(file["surfaces"][0]["coverage"], "manual")
        self.assertNotIn("localhost", json.dumps(result))

    def test_stale_or_tampered_delta_is_rejected(self):
        state = self.baseline()
        source = self.repository / "service.py"
        source.write_text("def fresh() -> int: return 2\n")
        self.inventory(state)
        source.write_text("def stale() -> int: return 3\n")
        with self.assertRaises(ReviewGateError):
            build_inventory(state, self.repository)
        self.inventory(state)
        Path(state["delta"]["session_delta_artifact"]).write_text("{}")
        with self.assertRaises(ReviewGateError):
            build_inventory(state, self.repository)


if __name__ == "__main__":
    unittest.main()
