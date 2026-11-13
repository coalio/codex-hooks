#!/usr/bin/env python3
"""Behavior tests for the global context-aware review gate."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


HOOK_DIRECTORY = Path(__file__).resolve().parents[1]
SESSION_START = HOOK_DIRECTORY / "review_gate_session_start.py"
SUBAGENT_STOP = HOOK_DIRECTORY / "review_gate_subagent_stop.py"
STOP = HOOK_DIRECTORY / "review_gate_stop.py"
LEGACY_STOP = HOOK_DIRECTORY / "architecture_review_stop.py"
sys.path.insert(0, str(HOOK_DIRECTORY))

from review_gate_common import (  # noqa: E402
    CLASSIFIER_NAME, CONCLUSION_NAME, REVIEWER_NAME, load_session_state,
    normalize_remote, repository_fingerprint, session_state_path,
)
from review_gate_contracts import CHECK_NAMES, CONTRACT_REVIEWER_NAME


CONCLUSION = """## What changed

Added the requested repository behavior in the exact changed file and preserved all unrelated paths.

## How it was implemented

The implementation follows the repository boundary, connects the new behavior to its owning component, and keeps the data flow explicit.

## Problems found and resolved

- **Problem:** The first validation attempt exposed a concrete setup mismatch. **Resolution:** The setup was corrected and the same validation was rerun successfully.

## Validation

The focused behavior tests and repository-state checks passed for the final fingerprint, with no deployment performed.
"""


class ReviewGateHooksTest(unittest.TestCase):
    """Exercise hook I/O against disposable real Git repositories."""

    def setUp(self) -> None:
        """Create a committed repository and initialize one session."""

        # Use a real repository so index, worktree, commit, and remote semantics apply.
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repository = Path(self.temporary_directory.name)
        self._git("init", "-q")
        self._git("config", "user.name", "Review Gate Test")
        self._git("config", "user.email", "review-gate@example.invalid")
        (self.repository / "tracked.txt").write_text("baseline\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-qm", "test: baseline")
        self.session_id = "session-primary"
        self._start()

    def tearDown(self) -> None:
        """Remove the disposable repository and its session state."""

        self.temporary_directory.cleanup()

    def test_remote_url_normalization(self) -> None:
        """Equivalent HTTPS and SSH GitHub remotes must share one identity."""

        expected = "github.com/greysteelcompany/salesforce"
        self.assertEqual(normalize_remote("https://github.com/GreysteelCompany/salesforce.git"), expected)
        self.assertEqual(normalize_remote("git@github.com:GreysteelCompany/salesforce.git"), expected)
        self.assertEqual(normalize_remote("ssh://git@github.com/GreysteelCompany/salesforce"), expected)

    def test_outside_git_allows_stop(self) -> None:
        """A non-Git session never needs classifier or reviewer evidence."""

        with tempfile.TemporaryDirectory() as directory:
            response = self._run_hook(
                STOP, self._payload("Stop", cwd=directory, stop_hook_active=False), cwd=Path(directory),
            )
        self.assertTrue(response.get("continue"))

    def test_legacy_stop_releases_only_current_exempt_remote(self) -> None:
        """A cached pre-migration Stop command must not strand an exempt session."""

        self._git("remote", "add", "origin", "git@github.com:GreysteelCompany/salesforce.git")
        response = self._run_hook(
            LEGACY_STOP, self._payload("Stop", session="legacy-session"),
        )
        self.assertTrue(response.get("continue"))

        self._git("remote", "set-url", "origin", "https://github.com/example/other.git")
        for active in (False, True):
            with self.subTest(stop_hook_active=active):
                response = self._run_hook(
                    LEGACY_STOP, self._payload("Stop", session="legacy-session", stop_hook_active=active),
                )
                self._assert_terminal_stop(response)
                self.assertIn("fresh Codex session", response["stopReason"])

    def test_missing_baseline_stops_without_creating_state(self) -> None:
        """Clean, dirty, and committed work cannot cause an unrecoverable retry loop."""

        # A clean worktree does not prove that an unrecorded session made no commits.
        session = "session-without-baseline"
        for condition in ("clean", "dirty", "committed"):
            if condition == "dirty":
                (self.repository / "tracked.txt").write_text("changed\n", encoding="utf-8")
            elif condition == "committed":
                self._git("commit", "-qam", "test: commit without session baseline")
            fingerprint = repository_fingerprint(self.repository)
            for active in (False, True):
                with self.subTest(condition=condition, stop_hook_active=active):
                    response = self._stop(session, stop_hook_active=active)
                    self._assert_terminal_stop(response)
                    self.assertIn("baseline is missing", response["stopReason"])
                    self.assertIsNone(load_session_state(self.repository, session))
                    self.assertEqual(repository_fingerprint(self.repository), fingerprint)

    def test_corrupt_state_stops_without_replacing_it(self) -> None:
        """Unreadable state remains available for diagnosis and never becomes approval."""

        path = session_state_path(self.repository, self.session_id)
        path.write_text("{broken", encoding="utf-8")
        for active in (False, True):
            with self.subTest(stop_hook_active=active):
                self._assert_terminal_stop(self._stop(stop_hook_active=active))
                self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_invalid_baseline_stops_without_replacing_it(self) -> None:
        """A structurally invalid baseline cannot be reset from current work."""

        path = session_state_path(self.repository, self.session_id)
        state = json.loads(path.read_text(encoding="utf-8"))
        state["baseline"]["fingerprint"] = "invalid"
        path.write_text(json.dumps(state), encoding="utf-8")
        before = path.read_bytes()
        for active in (False, True):
            with self.subTest(stop_hook_active=active):
                self._assert_terminal_stop(self._stop(stop_hook_active=active))
                self.assertEqual(path.read_bytes(), before)

    def test_old_baseline_stops_when_exact_delta_is_unavailable(self) -> None:
        """Older state cannot attribute changed dirty files to this session."""

        path = session_state_path(self.repository, self.session_id)
        state = json.loads(path.read_text(encoding="utf-8"))
        del state["baseline"]["dirty_snapshots"]
        path.write_text(json.dumps(state), encoding="utf-8")
        before = path.read_bytes()
        (self.repository / "tracked.txt").write_text("changed\n", encoding="utf-8")
        for active in (False, True):
            with self.subTest(stop_hook_active=active):
                self._assert_terminal_stop(self._stop(stop_hook_active=active))
                self.assertEqual(path.read_bytes(), before)

    def test_missing_protected_baseline_stops_without_approval(self) -> None:
        """Lost dirty-path content cannot be reconstructed by another continuation."""

        (self.repository / "tracked.txt").write_text("preexisting\n", encoding="utf-8")
        session = "session-lost-snapshot"
        self._start(session)
        state = load_session_state(self.repository, session)
        snapshot = Path(state["baseline"]["dirty_snapshots"]["tracked.txt"]["content_file"])
        snapshot.unlink()
        (self.repository / "tracked.txt").write_text("session change\n", encoding="utf-8")
        before = session_state_path(self.repository, session).read_bytes()
        for active in (False, True):
            with self.subTest(stop_hook_active=active):
                self._assert_terminal_stop(self._stop(session, stop_hook_active=active))
                self.assertEqual(session_state_path(self.repository, session).read_bytes(), before)

    def test_invalid_stop_input_ends_both_entrypoints_without_continuation(self) -> None:
        """Malformed hook input must fail visibly without asking the model to repair it."""

        for script in (STOP, LEGACY_STOP):
            with self.subTest(script=script.name):
                self._assert_terminal_stop(self._run_hook(script, {"hook_event_name": "invalid"}))

    def test_valid_review_continuations_still_require_approval(self) -> None:
        """The loop fix must leave actionable reviewer and conclusion routing intact."""

        (self.repository / "code.py").write_text("changed\n", encoding="utf-8")
        response = self._stop(stop_hook_active=True)
        self.assertEqual(response.get("decision"), "block")
        self.assertIn("service_contract_reviewer", response["reason"])
        self.assertNotIn("continue", response)

        # A real review failure remains actionable even in a continued turn.
        self._review("FAIL\n\n[high] code.py:1\ninvariant: ownership\nproblem: mixed roles\nminimal correction: split roles")
        response = self._stop(stop_hook_active=True)
        self.assertEqual(response.get("decision"), "block")
        self.assertIn("returned FAIL", response["reason"])
        self.assertNotIn("continue", response)

        # Valid approval still needs a current conclusion before completion.
        self._review("PASS")
        response = self._stop(stop_hook_active=True)
        self.assertEqual(response.get("decision"), "block")
        self.assertIn("conclusion_writer", response["reason"])
        self._conclude(CONCLUSION)
        self.assertTrue(self._stop(stop_hook_active=True).get("continue"))

    def test_unchanged_repository_allows_stop(self) -> None:
        """The initial fingerprint is an unconditional zero-token acceptance."""

        response = self._stop()
        self.assertTrue(response.get("continue"))

    def test_exempt_https_remote_skips_review_but_requires_conclusion(self) -> None:
        """The exact Salesforce remote skips architecture models, not final synthesis."""

        session = "session-exempt-https"
        self._git("remote", "add", "origin", "https://github.com/GreysteelCompany/salesforce.git")
        self._start(session)
        (self.repository / "code.py").write_text("print('changed')\n", encoding="utf-8")
        response = self._stop(session)
        self.assertIn("conclusion_writer", response.get("reason", ""))
        self.assertNotIn("architecture_reviewer", response.get("reason", ""))
        self._conclude(CONCLUSION, session)
        self.assertTrue(self._stop(session).get("continue"))

    def test_exempt_ssh_remote_requires_only_conclusion(self) -> None:
        """The SSH form follows the same review-exempt conclusion path."""

        session = "session-exempt-ssh"
        self._git("remote", "add", "origin", "git@github.com:GreysteelCompany/salesforce.git")
        self._start(session)
        (self.repository / "metadata.xml").write_text("<changed/>\n", encoding="utf-8")
        self.assertIn("conclusion_writer", self._stop(session).get("reason", ""))
        self._conclude(CONCLUSION, session)
        self.assertTrue(self._stop(session).get("continue"))

    def test_remote_changed_to_exempt_after_start_does_not_bypass(self) -> None:
        """Both initial and current identities must match the exemption."""

        self._git("remote", "add", "origin", "https://github.com/example/other.git")
        self._git("remote", "set-url", "origin", "https://github.com/GreysteelCompany/salesforce.git")
        (self.repository / "code.py").write_text("print('changed')\n", encoding="utf-8")
        response = self._stop()
        self.assertEqual(response.get("decision"), "block")

    def test_obvious_code_change_routes_directly_to_reviewer(self) -> None:
        """Executable source avoids classifier-first token cost."""

        (self.repository / "code.py").write_text("print('changed')\n", encoding="utf-8")
        response = self._stop()
        self.assertIn("service_contract_reviewer", response.get("reason", ""))
        self.assertNotIn("architecture_scope_classifier", response.get("reason", ""))

    def test_documentation_change_routes_to_classifier(self) -> None:
        """Apparently non-engineering changes receive narrow classification."""

        (self.repository / "guide.md").write_text("documentation\n", encoding="utf-8")
        response = self._stop()
        self.assertIn("architecture_scope_classifier", response.get("reason", ""))

    def test_architecture_pass_alone_cannot_skip_service_contract_stage(self):
        (self.repository / "service.py").write_text("class ListingService: pass\n")
        self._agent_stop(REVIEWER_NAME, "early-architecture", "PASS", None)
        self._conclude(CONCLUSION)
        response = self._stop()
        self.assertIn(CONTRACT_REVIEWER_NAME, response["reason"])
        self.assertNotIn("conclusion_writer", response["reason"])

    def test_pre_specialist_session_stops_without_approval_or_retry_loop(self):
        state_path = session_state_path(self.repository, self.session_id)
        state = json.loads(state_path.read_text())
        del state["baseline"]["service_contract_version"]
        state_path.write_text(json.dumps(state))
        (self.repository / "service.py").write_text("class ListingService: pass\n")
        response = self._stop()
        self._assert_terminal_stop(response)
        self.assertIn("predates the service-contract reviewer", response["stopReason"])
        self.assertIn("existing unapproved change set", response["stopReason"])

    def test_contract_stage_requires_all_checks_and_exact_inventory_coverage(self):
        (self.repository / "service.py").write_text("class ListingService: pass\n")
        valid = self._contract_message()
        for change in ("bare", "missing_check", "wrong_hash", "missing_path", "extra_path"):
            message = json.loads(json.dumps(valid))
            if change == "missing_check":
                del message["checks"]["type_cohesion"]
            elif change == "wrong_hash":
                message["inventory_sha256"] = "0" * 64
            elif change == "missing_path":
                message["reviewed_paths"] = []
            elif change == "extra_path":
                message["reviewed_paths"].append("unrelated.py")
            with self.subTest(change=change):
                self._agent_stop(CONTRACT_REVIEWER_NAME, "contract-agent", "PASS" if change == "bare" else json.dumps(message), None)
                state = load_session_state(self.repository, self.session_id)
                self.assertIsNone(state.get("service_contract"))
                self.assertIn(CONTRACT_REVIEWER_NAME, self._stop()["reason"])

    def test_contract_pass_routes_to_architecture_and_changes_revoke_it(self):
        source = self.repository / "service.py"
        source.write_text("class ListingService: pass\n")
        self._contract_pass()
        response = self._stop()
        self.assertIn("Spawn a fresh architecture_reviewer", response["reason"])
        self.assertNotIn("Spawn a fresh service_contract_reviewer", response["reason"])
        source.write_text("class ChangedService: pass\n")
        self.assertIn(CONTRACT_REVIEWER_NAME, self._stop()["reason"])

    def test_early_architecture_approval_requires_fresh_review_then_conclusion(self):
        (self.repository / "service.py").write_text("class ListingService: pass\n")
        self._agent_stop(REVIEWER_NAME, "architecture-agent", "PASS", None)
        self._conclude(CONCLUSION)
        self._contract_pass()
        self.assertIn("Spawn a fresh architecture_reviewer", self._stop()["reason"])
        self._agent_stop(REVIEWER_NAME, "fresh-architecture-agent", "PASS", None)
        self.assertIn("conclusion_writer", self._stop()["reason"])
        self._conclude(CONCLUSION)
        self.assertTrue(self._stop().get("continue"))

    def test_transcript_architecture_approval_cannot_predate_specialist(self):
        (self.repository / "service.py").write_text("class ListingService: pass\n")
        message = json.dumps(self._contract_message())
        root = self._write_transcript(self.session_id, CONTRACT_REVIEWER_NAME, message, completed_at="2026-08-18T00:00:03Z")
        self._write_transcript(self.session_id, REVIEWER_NAME, "PASS", completed_at="2026-08-18T00:00:01Z")
        response = self._stop(transcript_path=str(root))
        self.assertIn("predates service-contract approval", response["reason"])
        self.assertIn("Spawn a fresh architecture_reviewer", response["reason"])

    def test_contract_findings_are_stored_and_block_architecture(self):
        (self.repository / "service.py").write_text("class ListingService: pass\n")
        message = self._contract_message()
        message["verdict"] = "FAIL"
        message["checks"]["signature_semantics"]["status"] = "fail"
        message["findings"] = [{
            "check": "signature_semantics", "severity": "high", "path": "service.py",
            "symbol": "retrieve_listing", "problem": "The function returns an export instead of a listing",
            "minimal_correction": "Separate listing retrieval from report export",
        }]
        self._agent_stop(CONTRACT_REVIEWER_NAME, "contract-agent", json.dumps(message), None)
        response = self._stop()
        self.assertIn("service-contract review returned FAIL", response["reason"])
        self.assertNotIn("Spawn a fresh architecture_reviewer", response["reason"])
        state = load_session_state(self.repository, self.session_id)
        self.assertEqual(json.loads(state["service_contract"]["message"]), message)

    def test_contract_transcript_recovers_only_exact_role_inventory_and_fingerprint(self):
        source = self.repository / "service.py"
        source.write_text("class ListingService: pass\n")
        message = json.dumps(self._contract_message())
        root = self._write_transcript(self.session_id, CONTRACT_REVIEWER_NAME, message)
        self.assertIn("Spawn a fresh architecture_reviewer", self._stop(transcript_path=str(root))["reason"])
        source.write_text("class DifferentService: pass\n")
        self.assertIn(CONTRACT_REVIEWER_NAME, self._stop(transcript_path=str(root))["reason"])
        invalid = json.dumps(self._contract_message()).replace('"inventory_sha256": "', '"inventory_sha256": "0')
        root = self._write_transcript(self.session_id, CONTRACT_REVIEWER_NAME, invalid)
        self.assertIn(CONTRACT_REVIEWER_NAME, self._stop(transcript_path=str(root))["reason"])

    def test_worker_cannot_submit_contract_evidence(self):
        (self.repository / "service.py").write_text("class ListingService: pass\n")
        message = json.dumps(self._contract_message())
        self._agent_stop("worker", "pretend-reviewer", message, None)
        state = load_session_state(self.repository, self.session_id)
        self.assertIsNone(state.get("service_contract"))

    def test_classifier_not_required_routes_to_conclusion(self) -> None:
        """A not-required verdict settles review and starts final synthesis."""

        (self.repository / "guide.md").write_text("documentation\n", encoding="utf-8")
        self._classify("ARCHITECTURE_NOT_REQUIRED\nreason: The change is pure documentation.")
        response = self._stop()
        self.assertIn("conclusion_writer", response.get("reason", ""))
        self._conclude(CONCLUSION)
        self.assertTrue(self._stop().get("continue"))

    def test_classifier_required_escalates_to_reviewer(self) -> None:
        """A strict required verdict blocks with its concise reason."""

        (self.repository / "metadata.xml").write_text("<Flow/>\n", encoding="utf-8")
        self._classify("ARCHITECTURE_REQUIRED\nreason: The change alters Salesforce metadata.")
        response = self._stop()
        self.assertEqual(response.get("decision"), "block")
        self.assertIn("service_contract_reviewer", response.get("reason", ""))
        self._contract_pass()
        response = self._stop()
        self.assertIn("architecture_reviewer", response.get("reason", ""))
        self.assertIn("Salesforce metadata", response.get("reason", ""))

    def test_malformed_classifier_output_blocks(self) -> None:
        """Classifier prose outside the two-line contract cannot approve."""

        (self.repository / "guide.md").write_text("documentation\n", encoding="utf-8")
        self._classify("ARCHITECTURE_NOT_REQUIRED because docs")
        response = self._stop()
        self.assertEqual(response.get("decision"), "block")
        self.assertIn("architecture_scope_classifier", response.get("reason", ""))

    def test_direct_reviewer_pass_routes_to_conclusion_without_classifier(self) -> None:
        """A direct PASS settles review but does not replace the conclusion."""

        (self.repository / "guide.md").write_text("documentation\n", encoding="utf-8")
        self._review("PASS")
        response = self._stop()
        self.assertIn("conclusion_writer", response.get("reason", ""))
        self.assertNotIn("architecture_scope_classifier", response.get("reason", ""))
        self._conclude(CONCLUSION)
        self.assertTrue(self._stop().get("continue"))

    def test_conclusion_before_final_approval_is_rejected_as_out_of_order(self) -> None:
        """A conclusion cannot predate the architecture decision it describes."""

        (self.repository / "code.py").write_text("print('changed')\n", encoding="utf-8")
        self._conclude(CONCLUSION)
        self._review("PASS")
        response = self._stop()
        self.assertEqual(response.get("decision"), "block")
        self.assertIn("conclusion_writer", response.get("reason", ""))

    def test_later_turn_requires_a_fresh_conclusion_for_the_updated_ledger(self) -> None:
        """A completion from an earlier parent turn cannot summarize later work."""

        (self.repository / "guide.md").write_text("documentation\n", encoding="utf-8")
        self._classify("ARCHITECTURE_NOT_REQUIRED\nreason: The change is pure documentation.")
        self._conclude(CONCLUSION)
        self.assertTrue(self._stop().get("continue"))
        later = self._stop(turn_id="turn-later")
        self.assertIn("conclusion_writer", later.get("reason", ""))
        self._conclude(CONCLUSION, turn_id="turn-later")
        self.assertTrue(self._stop(turn_id="turn-later").get("continue"))

    def test_later_turn_accepts_identical_text_from_a_fresh_conclusion_agent(self) -> None:
        """Transcript idempotence distinguishes a new generation with the same prose."""

        (self.repository / "code.py").write_text("changed\n", encoding="utf-8")
        self._review("PASS")
        root = self._write_transcript(
            self.session_id, CONCLUSION_NAME, CONCLUSION, agent_id="conclusion-first",
        )
        self.assertTrue(self._stop(transcript_path=str(root)).get("continue"))
        self.assertIn(
            "conclusion_writer",
            self._stop(transcript_path=str(root), turn_id="turn-later").get("reason", ""),
        )
        root = self._write_transcript(
            self.session_id, CONCLUSION_NAME, CONCLUSION,
            completed_at=datetime.now(timezone.utc).isoformat(),
            agent_id="conclusion-second",
        )
        self.assertTrue(self._stop(
            transcript_path=str(root), turn_id="turn-later",
        ).get("continue"))

    def test_child_conclusion_is_bound_once_to_its_parent_turn(self) -> None:
        """A real child turn is promoted once without becoming reusable later."""

        (self.repository / "code.py").write_text("changed\n", encoding="utf-8")
        self._review("PASS")
        root = self._write_transcript(
            self.session_id, CONCLUSION_NAME, CONCLUSION,
            agent_id="conclusion-agent", turn_id="turn-child",
        )
        self._conclude(CONCLUSION, turn_id="turn-child")

        state = load_session_state(self.repository, self.session_id)
        self.assertEqual(state["conclusion"]["turn_id"], "turn-child")
        self.assertTrue(self._stop(
            transcript_path=str(root), turn_id="turn-parent",
        ).get("continue"))
        state = load_session_state(self.repository, self.session_id)
        self.assertEqual(state["conclusion"]["turn_id"], "turn-parent")

        later = self._stop(transcript_path=str(root), turn_id="turn-later")
        self.assertIn("conclusion_writer", later.get("reason", ""))

    def test_cross_day_child_conclusion_is_bound_once(self) -> None:
        """A genuine child is discovered after day, month, and year rollovers."""

        (self.repository / "code.py").write_text("changed\n", encoding="utf-8")
        for before, after in (
            ("2026/09/15", "2026/09/16"),
            ("2026/09/30", "2026/10/01"),
            ("2026/12/31", "2027/01/01"),
        ):
            with self.subTest(before=before, after=after):
                self._review("PASS")
                root = self._write_transcript(
                    self.session_id, CONCLUSION_NAME, CONCLUSION,
                    agent_id="conclusion-agent", turn_id="turn-child",
                )
                child = root.parent / f"child-{self.session_id}-{CONCLUSION_NAME}.jsonl"
                sessions = self.repository / ".git" / before.replace("/", "-") / "sessions"
                parent_dir, child_dir = sessions / before, sessions / after
                parent_dir.mkdir(parents=True)
                child_dir.mkdir(parents=True)
                dated_root = parent_dir / root.name
                root.rename(dated_root)
                child.rename(child_dir / child.name)
                self._conclude(CONCLUSION, turn_id="turn-child")
                self.assertTrue(self._stop(
                    transcript_path=str(dated_root), turn_id="turn-parent",
                ).get("continue"))
                self.assertIn("conclusion_writer", self._stop(
                    transcript_path=str(dated_root), turn_id="turn-later",
                ).get("reason", ""))

    def test_cross_day_scan_rejects_untrusted_children(self) -> None:
        """Wider discovery does not relax child identity or fingerprint checks."""

        (self.repository / "code.py").write_text("changed\n", encoding="utf-8")
        self._review("PASS")
        for field, value in (
            ("session_id", "another-session"),
            ("parent_thread_id", "another-parent"),
            ("agent_role", "worker"),
            ("cwd", "/unrelated/repository"),
            ("fingerprint", "0" * 64),
        ):
            with self.subTest(field=field):
                root = self._write_transcript(self.session_id, CONCLUSION_NAME, CONCLUSION)
                child = root.parent / f"child-{self.session_id}-{CONCLUSION_NAME}.jsonl"
                records = [json.loads(line) for line in child.read_text().splitlines()]
                if field == "fingerprint":
                    records[1]["payload"]["output"]["text"] = (
                        f"CODEX_REVIEW_GATE_FINGERPRINT={value}\n"
                    )
                else:
                    records[0]["payload"][field] = value
                child.write_text("".join(json.dumps(record) + "\n" for record in records))
                sessions = self.repository / ".git" / field / "sessions"
                parent_dir, child_dir = sessions / "2026/09/15", sessions / "2026/09/16"
                parent_dir.mkdir(parents=True)
                child_dir.mkdir(parents=True)
                dated_root = parent_dir / root.name
                root.rename(dated_root)
                child.rename(child_dir / child.name)
                self.assertIn("conclusion_writer", self._stop(
                    transcript_path=str(dated_root), turn_id="turn-parent",
                ).get("reason", ""))

    def test_malformed_conclusion_cannot_complete(self) -> None:
        """A status-only closeout must not satisfy the conclusion contract."""

        (self.repository / "code.py").write_text("print('changed')\n", encoding="utf-8")
        self._review("PASS")
        self._conclude("Work completed and review passed.")
        state = load_session_state(self.repository, self.session_id)
        self.assertIsNone(state["conclusion"])
        self.assertIn("conclusion_writer", self._stop().get("reason", ""))

    def test_conclusion_preamble_cannot_complete(self) -> None:
        """The writer response must begin with the requested explanatory structure."""

        (self.repository / "code.py").write_text("print('changed')\n", encoding="utf-8")
        self._review("PASS")
        self._conclude("Work completed and review passed.\n\n" + CONCLUSION)
        self.assertIsNone(load_session_state(self.repository, self.session_id)["conclusion"])
        self.assertIn("conclusion_writer", self._stop().get("reason", ""))

    def test_post_conclusion_change_invalidates_review_and_conclusion(self) -> None:
        """Any later repository edit forces the review and conclusion pipeline again."""

        (self.repository / "code.py").write_text("first\n", encoding="utf-8")
        self._review("PASS")
        self._conclude(CONCLUSION)
        self.assertTrue(self._stop().get("continue"))
        (self.repository / "code.py").write_text("second\n", encoding="utf-8")
        response = self._stop()
        self.assertIn("service_contract_reviewer", response.get("reason", ""))
        self.assertNotIn("conclusion_writer", response.get("reason", ""))

    def test_reviewer_fail_is_stored_and_blocks(self) -> None:
        """A structured FAIL remains visible and cannot approve completion."""

        (self.repository / "code.py").write_text("print('changed')\n", encoding="utf-8")
        self._review("FAIL\n\n[high] code.py:1\ninvariant: ownership\nproblem: mixed roles\nminimal correction: split roles")
        state = load_session_state(self.repository, self.session_id)
        self.assertEqual(state["reviewer"]["outcome"], "FAIL")
        response = self._stop()
        self.assertEqual(response.get("decision"), "block")
        self.assertIn("attempt to finish", response.get("reason", ""))
        self.assertIn("never reuse this handoff", response.get("reason", ""))

    def test_malformed_reviewer_output_does_not_approve(self) -> None:
        """PASS-like prose is not a trusted reviewer result."""

        (self.repository / "code.py").write_text("print('changed')\n", encoding="utf-8")
        self._review("PASS with caveats")
        self.assertIsNone(load_session_state(self.repository, self.session_id)["reviewer"])

    def test_post_verdict_modification_invalidates_classifier(self) -> None:
        """A later edit invalidates a not-required verdict."""

        (self.repository / "guide.md").write_text("first\n", encoding="utf-8")
        self._classify("ARCHITECTURE_NOT_REQUIRED\nreason: The change is documentation only.")
        (self.repository / "guide.md").write_text("second\n", encoding="utf-8")
        self.assertEqual(self._stop().get("decision"), "block")

    def test_post_pass_modification_invalidates_reviewer(self) -> None:
        """A later edit invalidates an architecture PASS."""

        (self.repository / "code.py").write_text("first\n", encoding="utf-8")
        self._review("PASS")
        (self.repository / "code.py").write_text("second\n", encoding="utf-8")
        self.assertEqual(self._stop().get("decision"), "block")

    def test_preexisting_dirty_tree_uses_session_delta(self) -> None:
        """Only net changes after a new baseline appear in its manifest."""

        (self.repository / "tracked.txt").write_text("preexisting dirty\n", encoding="utf-8")
        session = "session-dirty"
        self._start(session)
        self.assertTrue(self._stop(session).get("continue"))
        (self.repository / "guide.md").write_text("new docs\n", encoding="utf-8")
        response = self._stop(session)
        self.assertIn('"path":"guide.md"', response.get("reason", ""))
        self.assertNotIn('"path":"tracked.txt"', response.get("reason", ""))

    def test_dirty_path_artifact_isolates_session_hunks_and_index_change(self) -> None:
        """Pre-existing staged and unstaged hunks remain separate from session work."""

        (self.repository / "tracked.txt").write_text(
            "baseline\nstaged-before\n", encoding="utf-8",
        )
        self._git("add", "tracked.txt")
        (self.repository / "tracked.txt").write_text(
            "baseline\nstaged-before\nunstaged-before\n", encoding="utf-8",
        )
        session = "session-dirty-same-path"
        self._start(session)
        (self.repository / "tracked.txt").write_text(
            "baseline\nstaged-before\nunstaged-before\nsession-only\n", encoding="utf-8",
        )
        self._git("add", "tracked.txt")

        response = self._stop(session)
        self.assertIn("session-delta.json", response.get("reason", ""))
        state = load_session_state(self.repository, session)
        artifact = Path(state["delta"]["session_delta_artifact"])
        evidence = json.loads(artifact.read_text(encoding="utf-8"))
        snapshot = Path(state["baseline"]["dirty_snapshots"]["tracked.txt"]["content_file"])
        self.assertEqual(stat.S_IMODE(artifact.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o600)
        self.assertEqual([item["path"] for item in evidence["paths"]], ["tracked.txt"])
        session_diff = evidence["paths"][0]["session_text_diff"]
        self.assertIn("+session-only", session_diff)
        self.assertNotIn("+staged-before", session_diff)
        self.assertNotIn("+unstaged-before", session_diff)
        self.assertNotEqual(
            evidence["paths"][0]["baseline"]["index_base64"],
            evidence["paths"][0]["current"]["index_base64"],
        )

    def test_commit_after_baseline_requires_gate(self) -> None:
        """A clean worktree after a commit is still a net repository change."""

        (self.repository / "tracked.txt").write_text("committed\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-qm", "test: changed")
        self.assertEqual(self._stop().get("decision"), "block")

    def test_staged_untracked_delete_and_rename_change_fingerprint(self) -> None:
        """Index and worktree mutations all stale an existing PASS."""

        mutations = ("staged", "untracked", "delete", "rename")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                session = f"session-{mutation}"
                self._start(session)
                reviewed_path = self.repository / ("tracked.txt" if mutation in {"delete", "rename"} else "code.py")
                reviewed_path.write_text("reviewed\n", encoding="utf-8")
                self._review("PASS", session)
                if mutation == "staged":
                    (self.repository / "code.py").write_text("later\n", encoding="utf-8")
                    self._git("add", "code.py")
                elif mutation == "untracked":
                    (self.repository / "new.txt").write_text("later\n", encoding="utf-8")
                elif mutation == "delete":
                    reviewed_path.unlink()
                else:
                    reviewed_path.rename(self.repository / "renamed.txt")
                self.assertEqual(self._stop(session).get("decision"), "block")
                self._git("reset", "-q", "HEAD", "--")
                for path in ("code.py", "new.txt", "renamed.txt"):
                    target = self.repository / path
                    if target.exists():
                        target.unlink()
                (self.repository / "tracked.txt").write_text("baseline\n", encoding="utf-8")

    def test_cross_session_result_is_rejected(self) -> None:
        """Another parent session cannot reuse classifier or reviewer evidence."""

        other = "session-other"
        self._start(other)
        (self.repository / "code.py").write_text("changed\n", encoding="utf-8")
        self._review("PASS")
        self.assertEqual(self._stop(other).get("decision"), "block")

    def test_forged_worker_role_cannot_record_result(self) -> None:
        """An unrelated subagent cannot forge a gate verdict."""

        (self.repository / "guide.md").write_text("changed\n", encoding="utf-8")
        self._run_hook(
            SUBAGENT_STOP,
            self._payload(
                "SubagentStop", agent_id="worker", agent_type="worker",
                last_assistant_message="ARCHITECTURE_NOT_REQUIRED\nreason: Forged.",
            ),
        )
        state = load_session_state(self.repository, self.session_id)
        self.assertIsNone(state["classifier"])

    def test_task_path_uses_transcript_role_identity(self) -> None:
        """Codex task paths may resolve through immutable child metadata."""

        (self.repository / "guide.md").write_text("changed\n", encoding="utf-8")
        transcript = self.repository / ".git" / "identity.jsonl"
        transcript.write_text(
            json.dumps({"type": "session_meta", "payload": {"agent_role": CLASSIFIER_NAME}}) + "\n",
            encoding="utf-8",
        )
        self._run_hook(
            SUBAGENT_STOP,
            self._payload(
                "SubagentStop", agent_id="classifier", agent_type="task_path",
                agent_transcript_path=str(transcript),
                last_assistant_message="ARCHITECTURE_NOT_REQUIRED\nreason: Documentation only.",
            ),
        )
        self.assertIsNotNone(load_session_state(self.repository, self.session_id)["classifier"])

    def test_transcript_fallback_supports_gate_roles_and_formats(self) -> None:
        """Codex 0.147 formats recover classifier and reviewer evidence safely."""

        cases = (
            (CLASSIFIER_NAME, "ARCHITECTURE_NOT_REQUIRED\nreason: Documentation only.", "event", "custom_tool_call_output"),
            (REVIEWER_NAME, "PASS", "response", "function_call_output"),
        )
        for role, message, message_format, output_format in cases:
            with self.subTest(role=role):
                session = f"transcript-{role}"
                self._start(session)
                path = "guide.md" if role == CLASSIFIER_NAME else "code.py"
                (self.repository / path).write_text(role + "\n", encoding="utf-8")
                if role == REVIEWER_NAME:
                    self._contract_pass(session)
                root = self._write_transcript(session, role, message, message_format, output_format)
                response = self._stop(session, transcript_path=str(root))
                self.assertIn("conclusion_writer", response.get("reason", ""))
                self._conclude(CONCLUSION, session)
                self.assertTrue(self._stop(session, transcript_path=str(root)).get("continue"))
                (self.repository / path).unlink()

    def test_transcript_fallback_recovers_conclusion(self) -> None:
        """A completed fingerprinted conclusion may recover without SubagentStop."""

        (self.repository / "code.py").write_text("changed\n", encoding="utf-8")
        self._review("PASS")
        root = self._write_transcript(
            self.session_id, CONCLUSION_NAME, CONCLUSION, "response", "custom_tool_call_output",
        )
        self.assertTrue(self._stop(transcript_path=str(root)).get("continue"))

    def test_transcript_fallback_preserves_review_then_conclusion_order(self) -> None:
        """One Stop may recover ordered review and conclusion transcripts together."""

        (self.repository / "code.py").write_text("changed\n", encoding="utf-8")
        contract_message = json.dumps(self._contract_message())
        self._write_transcript(
            self.session_id, CONTRACT_REVIEWER_NAME, contract_message,
            completed_at="2026-08-18T00:00:01Z",
        )
        root = self._write_transcript(
            self.session_id, REVIEWER_NAME, "PASS", completed_at="2026-08-18T00:00:03Z",
        )
        root = self._write_transcript(
            self.session_id, CONCLUSION_NAME, CONCLUSION,
            completed_at="2026-08-18T00:00:06Z",
        )
        self.assertTrue(self._stop(transcript_path=str(root)).get("continue"))
        state = load_session_state(self.repository, self.session_id)
        self.assertEqual(state["reviewer"]["recorded_at"], "2026-08-18T00:00:03Z")
        self.assertEqual(state["conclusion"]["recorded_at"], "2026-08-18T00:00:06Z")

    def test_transcript_is_rejected_when_stale_or_wrong_role(self) -> None:
        """Stale fingerprints and untrusted roles cannot approve through fallback."""

        (self.repository / "code.py").write_text("reviewed\n", encoding="utf-8")
        root = self._write_transcript(self.session_id, REVIEWER_NAME, "PASS")
        (self.repository / "code.py").write_text("later\n", encoding="utf-8")
        self.assertEqual(self._stop(transcript_path=str(root)).get("decision"), "block")
        root = self._write_transcript(self.session_id, "worker", "PASS")
        self.assertEqual(self._stop(transcript_path=str(root)).get("decision"), "block")

    def test_state_records_delta_identity_hash_and_fingerprint(self) -> None:
        """Persisted evidence contains the requested audit fields."""

        (self.repository / "guide.md").write_text("changed\n", encoding="utf-8")
        self._stop()
        self._classify("ARCHITECTURE_NOT_REQUIRED\nreason: Documentation only.")
        self._conclude(CONCLUSION)
        state = load_session_state(self.repository, self.session_id)
        self.assertEqual(state["delta"]["manifest"][0]["path"], "guide.md")
        self.assertEqual(state["classifier"]["agent_id"], "classifier-agent")
        self.assertEqual(len(state["classifier"]["message_sha256"]), 64)
        self.assertEqual(state["classifier"]["fingerprint"], repository_fingerprint(self.repository))
        self.assertEqual(state["conclusion"]["message"], CONCLUSION)
        self.assertEqual(state["conclusion"]["outcome"], "READY")

    def test_resume_does_not_reset_baseline(self) -> None:
        """Resume and compaction cannot erase an unreviewed change."""

        (self.repository / "guide.md").write_text("changed\n", encoding="utf-8")
        self._start(source="resume")
        self.assertEqual(self._stop().get("decision"), "block")

    def _start(self, session: str | None = None, source: str = "startup") -> None:
        """Record a baseline through the production SessionStart interface."""

        response = self._run_hook(
            SESSION_START, self._payload("SessionStart", session, source=source),
        )
        self.assertTrue(response.get("continue"))

    def _stop(self, session: str | None = None, **values: object) -> dict[str, object]:
        """Run Stop for one test session."""

        values.setdefault("stop_hook_active", False)
        return self._run_hook(
            STOP, self._payload("Stop", session, **values),
        )

    def _assert_terminal_stop(self, response: dict[str, object]) -> None:
        """The Codex wire response must end the turn and expose incomplete review."""

        self.assertIs(response.get("continue"), False)
        self.assertNotIn("decision", response)
        self.assertNotIn("reason", response)
        self.assertIn("without approval", response.get("stopReason", ""))
        self.assertEqual(response.get("systemMessage"), response["stopReason"])

    def _classify(self, message: str, session: str | None = None) -> None:
        """Run the dedicated classifier completion hook."""

        self._agent_stop(CLASSIFIER_NAME, "classifier-agent", message, session)

    def _review(self, message: str, session: str | None = None) -> None:
        """Complete the contract prerequisite, then simulate the architecture reviewer."""

        self._contract_pass(session)
        self._agent_stop(REVIEWER_NAME, "reviewer-agent", message, session)

    def _contract_message(self, session: str | None = None) -> dict:
        self._stop(session)
        state = load_session_state(self.repository, session or self.session_id)
        return {
            "verdict": "PASS", "inventory_sha256": state["delta"]["structure_sha256"],
            "reviewed_paths": state["delta"]["structure_paths"],
            "checks": {name: {"status": "not_applicable", "evidence": "The changed test fixture declares no service boundary"} for name in CHECK_NAMES},
            "findings": [],
        }

    def _contract_pass(self, session: str | None = None) -> None:
        self._agent_stop(CONTRACT_REVIEWER_NAME, "contract-agent", json.dumps(self._contract_message(session)), session)

    def _conclude(
        self, message: str, session: str | None = None, turn_id: str = "turn-test",
    ) -> None:
        """Run the dedicated conclusion-writer completion hook."""

        self._agent_stop(CONCLUSION_NAME, "conclusion-agent", message, session, turn_id)

    def _agent_stop(
        self, role: str, agent_id: str, message: str, session: str | None,
        turn_id: str = "turn-test",
    ) -> None:
        """Submit one native role result through SubagentStop."""

        self._run_hook(
            SUBAGENT_STOP,
            self._payload(
                "SubagentStop", session, agent_id=agent_id, agent_type=role,
                last_assistant_message=message, stop_hook_active=False, turn_id=turn_id,
            ),
        )

    def _write_transcript(
        self, session: str, role: str, message: str,
        message_format: str = "response", output_format: str = "function_call_output",
        completed_at: str | None = None, agent_id: str | None = None,
        turn_id: str = "turn-child",
    ) -> Path:
        """Write one completed child rollout in either Codex 0.147 message format."""

        root = self.repository / ".git" / f"root-{session}.jsonl"
        root.write_text("{}\n", encoding="utf-8")
        child = self.repository / ".git" / f"child-{session}-{role}.jsonl"
        fingerprint = repository_fingerprint(self.repository)
        records: list[dict[str, object]] = [
            {"timestamp": "2026-08-18T00:00:00Z", "type": "session_meta", "payload": {
                "id": agent_id or f"agent-{role}", "session_id": session,
                "parent_thread_id": session, "agent_role": role, "cwd": str(self.repository),
            }},
            {"timestamp": "2026-08-18T00:00:01Z", "type": "response_item", "payload": {
                "type": output_format,
                "output": {"text": f"CODEX_REVIEW_GATE_FINGERPRINT={fingerprint}\n"},
            }},
        ]
        if message_format == "event":
            records.append({"timestamp": "2026-08-18T00:00:02Z", "type": "event_msg", "payload": {
                "type": "agent_message", "message": message,
            }})
        else:
            records.append({"timestamp": "2026-08-18T00:00:02Z", "type": "response_item", "payload": {
                "type": "message", "role": "assistant", "phase": "final_answer",
                "content": [{"type": "output_text", "text": message}],
            }})
        records.append({
            "timestamp": completed_at or datetime.now(timezone.utc).isoformat(),
            "type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn_id},
        })
        child.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        return root

    def _payload(self, event: str, session: str | None = None, **values: object) -> dict[str, object]:
        """Build common and event-specific hook input fields."""

        return {
            "session_id": session or self.session_id, "turn_id": "turn-test",
            "transcript_path": None, "cwd": str(self.repository),
            "hook_event_name": event, "model": "test-model", **values,
        }

    def _run_hook(
        self, script: Path, payload: dict[str, object], cwd: Path | None = None,
    ) -> dict[str, object]:
        """Execute a hook and parse its single JSON response."""

        result = subprocess.run(
            [sys.executable, str(script)], cwd=cwd or self.repository,
            input=json.dumps(payload), text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"invalid hook JSON: {result.stdout!r}; stderr={result.stderr!r}; {exc}")

    def _git(self, *arguments: str) -> None:
        """Run one required Git setup or mutation command."""

        subprocess.run(
            ["git", "-C", str(self.repository), *arguments], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )


if __name__ == "__main__":
    unittest.main()
