"""Execution identity, delivery and fresh-evidence invariants on owned data."""
import base64
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

from agent_backend.nanobot.execution_state import EffectLedger, ExecutionConflict, action_identity
from desktop_pet.agents.artifacts import ArtifactStore
from desktop_pet.agents.interactions import InteractionBroker, checked_interaction
from desktop_pet.agents.journal import ExecutionJournal

BACKEND = Path(__file__).resolve().parents[1] / "agent_backend" / "nanobot"
sys.path.insert(0, str(BACKEND))
from safe_files import WorkspaceFiles
from task_harness import TaskHarness


class ExecutionUpgradeTests(unittest.TestCase):
    def test_call_identity_is_task_bound_and_reuse_cannot_change_arguments(self):
        ledger = EffectLedger()
        identity = action_identity("task-a", "call-1")
        self.assertNotEqual(identity, action_identity("task-b", "call-1"))
        ledger.begin(identity, "write_file", {"path": "a"}, domain="file")
        with self.assertRaisesRegex(ExecutionConflict, "ALREADY_RUNNING"):
            ledger.begin(identity, "write_file", {"path": "a"}, domain="file")
        ledger.settle(identity, "actual-observation")
        self.assertEqual(ledger.begin(identity, "write_file", {"path": "a"}, domain="file")["result"], "actual-observation")
        with self.assertRaisesRegex(ExecutionConflict, "CONFLICT"):
            ledger.begin(identity, "write_file", {"path": "b"}, domain="file")

    def test_uncertain_effect_blocks_other_tool_in_same_domain_until_actual_resolution(self):
        ledger = EffectLedger()
        ledger.begin("a", "computer_use", {}, domain="desktop")
        ledger.settle("a", "unknown", uncertain=True)
        with self.assertRaisesRegex(ExecutionConflict, "UNCERTAIN"):
            ledger.begin("b", "open_application", {}, domain="desktop")
        ledger.begin("c", "read_file", {}, domain="none")
        with self.assertRaisesRegex(ExecutionConflict, "FRESH_OBSERVATION"):
            ledger.resolve("desktop")
        ledger.resolve("desktop", observed=True)
        ledger.begin("d", "desktop_act", {}, domain="desktop")
        self.assertEqual(ledger.records["a"]["state"], "uncertain")

    def test_expiry_and_wrong_conversation_never_authorize(self):
        now = [100.0]
        broker = InteractionBroker(clock=lambda: now[0])
        frame = {"interaction_id": "interaction-" + "a" * 32, "kind": "question",
                 "question": "Choose", "options": ["鲸" * 240]}
        question = checked_interaction(frame)
        broker.open(question, conversation_id="a", task_id="task", lifetime=5)
        self.assertIsNone(broker.answer("b", question["id"], {"text": "yes"}))
        now[0] = 106.0
        self.assertIsNone(broker.answer("a", question["id"], {"text": "yes"}))
        self.assertFalse(broker.expired()["answer"]["allowed"])
        self.assertIsNone(broker.public())

    def test_delivery_copies_are_immutable_cid_bound_and_corruption_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "workspace").mkdir()
            files = WorkspaceFiles(root / "workspace")
            files.write("result.txt", "鲸鱼" * 100)
            raw = (files.root / "result.txt").read_bytes()
            store = ArtifactStore(root / "artifacts")
            cid, other = str(uuid4()), str(uuid4())
            metadata = store.register(cid, {"path": "result.txt", "passed": True,
                "sha256": hashlib.sha256(raw).hexdigest(), "scope": "file_readback"}, files)
            (files.root / "result.txt").write_text("changed", encoding="utf-8")
            data, offset = bytearray(), 0
            while True:
                page = store.get(cid, metadata["reference"], offset=offset, limit=73)
                data.extend(base64.b64decode(page["data_base64"], validate=True))
                if page["next_offset"] is None:
                    break
                offset = page["next_offset"]
            self.assertEqual(bytes(data), raw)
            self.assertEqual(store.list(other), [])
            with self.assertRaisesRegex(ValueError, "UNAVAILABLE"):
                store.get(other, metadata["reference"])
            (root / "artifacts" / cid / metadata["reference"]).write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "CORRUPTED"):
                store.get(cid, metadata["reference"])
            store.clear(cid)
            self.assertEqual(store.list(cid), [])
            self.assertEqual((files.root / "result.txt").read_text(), "changed")

    def test_artifact_cannot_register_failed_or_stale_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = WorkspaceFiles(root)
            files.write("a.txt", "actual")
            store = ArtifactStore(root / "delivery")
            for passed, digest in ((False, hashlib.sha256(b"actual").hexdigest()), (True, "0" * 64)):
                with self.subTest(passed=passed), self.assertRaises(ValueError):
                    store.register(str(uuid4()), {"path": "a.txt", "passed": passed, "sha256": digest}, files)

    def test_browser_evidence_does_not_clear_desktop_or_required_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = TaskHarness(Path(directory))
            harness.record_failure("browser_act", {"ref": "x"}, classification="uncertain")
            harness.record_failure("desktop_activate", {"window_ref": "y"}, classification="uncertain")
            harness.record_failure("read_file", {"path": "required.txt"}, classification="required")
            harness.resolve_effects({"browser_act"})
            self.assertEqual({key[0] for key in harness.failed_tools}, {"desktop_activate", "read_file"})
            result = json.loads(harness.finish_task("completed"))
            self.assertFalse(result["accepted"])

    def test_private_journal_is_append_only_redacted_and_conversation_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = ExecutionJournal(Path(directory) / "journal.sqlite3")
            journal.append("a", "task", "message", {"content": "fixture-secret"}, secrets=("fixture-secret",))
            journal.append("b", "task", "diagnostic", {"duration_ms": 3})
            rows = journal.read("a")
            self.assertEqual(len(rows), 1)
            self.assertNotIn("fixture-secret", json.dumps(rows))
            self.assertEqual(journal.read("a", after=rows[0]["seq"]), [])
            journal.clear("a")
            self.assertEqual(journal.read("a"), [])
            self.assertEqual(len(journal.read("b")), 1)

    def test_fresh_parent_check_supersedes_failed_judge_without_clearing_other_requirements(self):
        from gui_verification import report
        with tempfile.TemporaryDirectory() as directory:
            harness = TaskHarness(Path(directory), require_evidence=True)
            harness.begin_gui_action()
            harness.record_success("desktop_act", {"ref": "owned", "action": "check"}, "{}")
            harness.record_gui_verification(report("failed"))
            harness.record_failure("verify_desktop_goal", {})
            self.assertFalse(json.loads(harness.finish_task("completed"))["accepted"])
            harness.record_gui_verification(report("verified"))
            self.assertTrue(json.loads(harness.finish_task("completed"))["accepted"])
            harness.record_failure("read_file", {"path": "required.txt"}, classification="required")
            harness.record_gui_verification(report("verified"))
            self.assertFalse(json.loads(harness.finish_task("completed"))["accepted"])


if __name__ == "__main__":
    unittest.main()
