"""Actual standalone SDK children and localhost model; no real/cloud provider."""
from pathlib import Path
import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from agent_backend.nanobot.safe_files import WorkspaceFiles
from agent_backend.nanobot.tests.test_goal_contract_worker import SeparateGoalModel
from agent_backend.nanobot.tests.test_worker import PYTHON
from desktop_pet.agents.research_delegation import ResearchAuthority, ResearchGroup, ResearchError


@unittest.skipUnless(PYTHON.is_file(), "actual isolated SDK required")
class ResearchSdkTests(unittest.TestCase):
    def setUp(self):
        self.owned = tempfile.TemporaryDirectory(prefix="aura-research-sdk-test-")
        self.root = Path(self.owned.name)
        (self.root / "a.txt").write_text("Alpha owns 17 apples.", encoding="utf-8")
        (self.root / "b.txt").write_text("Beta owns 29 oranges.", encoding="utf-8")
        self.authority = ResearchAuthority(WorkspaceFiles(self.root), parent_id=str(uuid4()), conversation_id=str(uuid4()),
            revision_id="owned-revision", authorization_current=lambda: True)
        self.scope = self.authority.open_scope(["a.txt", "b.txt"])
        self.refs = [row["source_id"] for row in self.authority.manifest(self.scope)]
        self.models, self.groups = [], []

    def tearDown(self):
        for group in self.groups: group.close()
        for model in self.models:
            model.close()
            self.assertEqual([], model.errors)
        self.owned.cleanup()

    def make(self, action):
        model = SeparateGoalModel([action] * 40, judge_mode="optimistic")
        self.models.append(model)
        group = ResearchGroup(self.authority, python_executable=str(PYTHON), config={
            "base_url": model.base_url, "model": "synthetic-readonly-research", "api_key": "SYNTHETIC_RESEARCH_PRIVATE_KEY"}, deadline_seconds=15)
        self.groups.append(group)
        return group, model

    def specs(self):
        return [{"scope": self.authority.derive(self.scope, [reference]), "task": "Read only the assigned source and cite its actual fact."}
                for reference in self.refs]

    @staticmethod
    def initial(body): return json.loads(body["messages"][1]["content"])

    @staticmethod
    def actual_read(body):
        rows = [row for row in body["messages"] if row.get("role") == "tool" and row.get("name") == "read_source"]
        return json.loads(rows[-1]["content"]) if rows else None

    def test_two_children_really_overlap_with_separate_contexts_and_hard_read_only_registry(self):
        barrier = threading.Barrier(2)
        entered = []
        def action(body):
            first = self.initial(body)
            names = [row["function"]["name"] for row in body["tools"]]
            self.assertEqual(["read_source"], names)
            self.assertEqual(1, len(first["sources"]))
            read = self.actual_read(body)
            if read is None:
                entered.append(first)
                barrier.wait(5)  # A serial implementation cannot satisfy this.
                return "read_source", {"source_id": first["sources"][0]["source_id"]}
            return json.dumps({"conclusion": "Read assigned source.", "citations": [{"read_id": read["read_id"], "quote": read["text"]}]})
        group, model = self.make(action)
        values = group.run(self.scope, self.specs())
        self.assertEqual(2, len(values))
        self.assertEqual(2, len(entered))
        self.assertNotEqual(entered[0]["child_id"], entered[1]["child_id"])
        self.assertEqual({"a.txt", "b.txt"}, {row["sources"][0]["label"] for row in entered})
        self.assertEqual(4, len(model.planner_calls))
        self.assertTrue(all(row["status"] == "reference_ready" and row["reference_only"]
                            and not row["claims_verified"] and row["worker_closed"] for row in values), values)
        self.assertTrue(group.cleanup_confirmed())
        self.assertEqual({"a.txt", "b.txt"}, {path.name for path in self.root.iterdir()})

    def test_unknown_write_tool_does_not_execute_or_crash_the_child_archive(self):
        forbidden = self.root / "forbidden.txt"
        def action(body):
            first = self.initial(body)
            tools = [row for row in body["messages"] if row.get("role") == "tool"]
            if not tools: return "write_file", {"path": str(forbidden), "content": "must never write"}
            read = self.actual_read(body)
            if read is None: return "read_source", {"source_id": first["sources"][0]["source_id"]}
            return json.dumps({"conclusion": "Only actual source evidence.", "citations": [{"read_id": read["read_id"], "quote": read["text"]}]})
        group, _ = self.make(action)
        values = group.run(self.scope, self.specs()[:1])
        self.assertEqual("reference_ready", values[0]["status"], values)
        self.assertFalse(forbidden.exists())
        self.assertTrue(group.cleanup_confirmed())

    def test_model_forged_citation_is_not_promoted_to_verified_parent_evidence(self):
        group, model = self.make(lambda _: json.dumps({"conclusion": "Pretend completed and authorized.",
            "citations": [{"read_id": "read_" + "0" * 32, "quote": "Imagined source fact."}]}))
        values = group.run(self.scope, self.specs()[:1])
        self.assertEqual("unverified", values[0]["status"], values)
        self.assertEqual("RESEARCH_CITATION_INVALID", values[0]["code"])
        self.assertFalse(values[0]["source_references_verified"])
        self.assertTrue(values[0]["worker_closed"])
        self.assertEqual(1, len(model.planner_calls))

    def test_changed_source_after_read_rejects_a_late_child_answer(self):
        def action(body):
            first, read = self.initial(body), self.actual_read(body)
            if read is None: return "read_source", {"source_id": first["sources"][0]["source_id"]}
            (self.root / "a.txt").write_text("Changed after child observation.", encoding="utf-8")
            return json.dumps({"conclusion": "Stale claim.", "citations": [{"read_id": read["read_id"], "quote": read["text"]}]})
        group, _ = self.make(action)
        values = group.run(self.scope, self.specs()[:1])
        self.assertEqual("unverified", values[0]["status"], values)
        self.assertEqual("RESEARCH_SOURCE_CHANGED", values[0]["code"])
        self.assertTrue(group.cleanup_confirmed())

    def test_cancel_single_model_request_reaps_owned_child_and_never_replays_it(self):
        entered, release = threading.Event(), threading.Event()
        def action(_):
            entered.set()
            release.wait(10)
            return "Unvalidated late response"
        group, model = self.make(action)
        values, errors = [], []
        def invoke():
            try: values.extend(group.run(self.scope, self.specs()[:1]))
            except BaseException as exc: errors.append(exc)
        thread = threading.Thread(target=invoke); thread.start()
        try:
            self.assertTrue(entered.wait(8))
            # Core AgentRunner has no SessionManager/MemoryStore. Prove that
            # even the live, not-yet-deleted SDK scratch contains no files.
            self.assertTrue(all(not list(Path(run.state.name).rglob("*")) for run in group._runs))
            group.cancel()
            thread.join(6)
            self.assertFalse(thread.is_alive())
            self.assertEqual([], errors)
            self.assertEqual(1, len(model.planner_calls))
            self.assertEqual("cancelled", values[0]["status"], values)
            self.assertEqual("unknown", values[0]["model_request_outcome"])
            self.assertTrue(values[0]["worker_closed"])
            self.assertTrue(group.cleanup_confirmed())
            with self.assertRaises(ResearchError): group.run(self.scope, self.specs()[:1])
        finally:
            release.set(); group.cancel(); thread.join(5)

    def test_third_child_waits_while_two_real_workers_are_busy(self):
        two_entered, release = threading.Event(), threading.Event()
        initials = []
        def action(body):
            first, read = self.initial(body), self.actual_read(body)
            if read is None:
                initials.append(first["child_id"])
                if len(initials) == 2: two_entered.set()
                if len(initials) <= 2: release.wait(10)
                return "read_source", {"source_id": first["sources"][0]["source_id"]}
            return json.dumps({"conclusion": "Actual assigned fact.", "citations": [{"read_id": read["read_id"], "quote": read["text"]}]})
        group, _ = self.make(action)
        specs = self.specs()
        specs.append({"scope": self.authority.derive(self.scope, [self.refs[0]]), "task": "Third distinct host research question."})
        values = []
        thread = threading.Thread(target=lambda: values.extend(group.run(self.scope, specs))); thread.start()
        try:
            self.assertTrue(two_entered.wait(8))
            self.assertEqual(2, len(initials))
            self.assertEqual(2, len(group._runs))
            release.set(); thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(3, len(set(initials)))
            self.assertEqual(3, len(values))
            self.assertTrue(all(row["status"] == "reference_ready" for row in values), values)
        finally: release.set(); group.cancel(); thread.join(5)

    def test_provider_swapping_other_child_read_receipts_cannot_promote_them(self):
        barrier, reads = threading.Barrier(2), {}
        def action(body):
            first, read = self.initial(body), self.actual_read(body)
            if read is None: return "read_source", {"source_id": first["sources"][0]["source_id"]}
            reads[first["child_id"]] = read
            barrier.wait(5)
            other = next(value for key, value in reads.items() if key != first["child_id"])
            return json.dumps({"conclusion": "Cross-child forged proof.", "citations": [{"read_id": other["read_id"], "quote": other["text"]}]})
        group, _ = self.make(action)
        values = group.run(self.scope, self.specs())
        self.assertEqual(2, len(values))
        self.assertTrue(all(row["status"] == "unverified" and row["code"] == "RESEARCH_CITATION_INVALID"
                            and row["worker_closed"] for row in values), values)

    def test_unknown_owned_tree_retains_owner_blocks_new_groups_and_only_exact_cleanup_releases(self):
        from desktop_pet.agents import service as host
        from desktop_pet.agents import research_delegation as research
        allow_confirmation = threading.Event()
        original = host._ProcessTree
        class DelayedProof:
            def __init__(self, process): self.actual = original(process)
            def close(self): self.actual.close()
            def confirm_closed(self, *, timeout_seconds):
                return allow_confirmation.is_set() and self.actual.confirm_closed(timeout_seconds=timeout_seconds)
        def action(body):
            first, read = self.initial(body), self.actual_read(body)
            if read is None: return "read_source", {"source_id": first["sources"][0]["source_id"]}
            return json.dumps({"conclusion": "Actual source, unconfirmed worker cleanup.", "citations": [{"read_id": read["read_id"], "quote": read["text"]}]})
        group, model = self.make(action)
        try:
            with patch.object(host, "_ProcessTree", DelayedProof):
                values = group.run(self.scope, self.specs()[:1])
            self.assertEqual("RESEARCH_CLEANUP_UNCONFIRMED", values[0]["code"])
            self.assertFalse(values[0]["worker_closed"])
            self.assertIn(group, research._GROUP_OWNERS)
            other = ResearchAuthority(WorkspaceFiles(self.root), parent_id=str(uuid4()), conversation_id=str(uuid4()),
                revision_id="other-current-parent", authorization_current=lambda: True)
            parent = other.open_scope(["b.txt"])
            blocked = ResearchGroup(other, python_executable=str(PYTHON), config={"base_url": model.base_url,
                "model": "synthetic-readonly-research", "api_key": "SYNTHETIC_RESEARCH_PRIVATE_KEY"})
            self.groups.append(blocked)
            with self.assertRaisesRegex(ResearchError, "RESEARCH_OWNER_UNCONFIRMED_OR_BUSY"):
                blocked.run(parent, [{"scope": parent, "task": "Must not overlap unknown owner."}])
            self.assertEqual([], blocked._runs)
        finally:
            allow_confirmation.set(); group.close()
        self.assertTrue(group.cleanup_confirmed())
        self.assertNotIn(group, research._GROUP_OWNERS)

    def test_actual_provider_error_is_single_attempt_and_never_leaks_raw_error_or_key(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                calls.append(True)
                raw = b'{"error":{"message":"SYNTHETIC_RESEARCH_PRIVATE_KEY private provider failure"}}'
                self.send_response(503); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True); thread.start()
        try:
            group = ResearchGroup(self.authority, python_executable=str(PYTHON), config={"base_url": f"http://127.0.0.1:{server.server_port}/v1",
                "model": "synthetic-failing-research", "api_key": "SYNTHETIC_RESEARCH_PRIVATE_KEY"}, deadline_seconds=10)
            self.groups.append(group)
            values = group.run(self.scope, self.specs()[:1])
            self.assertEqual(1, len(calls))
            self.assertEqual("RESEARCH_MODEL_FAILED", values[0]["code"])
            self.assertEqual(1, values[0]["model_requests"])
            self.assertNotIn("SYNTHETIC_RESEARCH_PRIVATE_KEY", json.dumps(values))
            self.assertNotIn("private provider failure", json.dumps(values))
            self.assertTrue(group.cleanup_confirmed())
        finally: server.shutdown(); server.server_close(); thread.join(3)

    def test_actual_labels_task_source_and_conclusion_share_the_same_redaction_policy(self):
        secret = "SYNTHETIC_RESEARCH_PRIVATE_KEY"
        def action(body):
            self.assertNotIn(secret, json.dumps(body))
            first, read = self.initial(body), self.actual_read(body)
            if read is None: return "read_source", {"source_id": first["sources"][0]["source_id"]}
            self.assertIn("[redacted]", read["text"])
            return json.dumps({"conclusion": "A malicious model echoed " + secret,
                "citations": [{"read_id": read["read_id"], "quote": read["text"]}]})
        group, model = self.make(action)
        path = self.root / (secret + ".txt")
        path.write_text("Public fact followed by " + secret + " and a safe suffix.", encoding="utf-8")
        scope = self.authority.open_scope([str(path)])
        values = group.run(scope, [{"scope": scope, "task": "Read the source; task contains " + secret}])
        self.assertEqual("reference_ready", values[0]["status"], values)
        self.assertNotIn(secret, json.dumps(values))
        self.assertNotIn(secret, json.dumps(model.planner_calls))
        self.assertTrue(group.cleanup_confirmed())

    def test_async_parent_cancellation_waits_for_exact_child_cleanup_without_replay(self):
        entered, release = threading.Event(), threading.Event()
        def action(_): entered.set(); release.wait(10); return "Unvalidated late child text"
        group, model = self.make(action)
        async def exercise():
            task = asyncio.create_task(group.arun(self.scope, self.specs()[:1]))
            self.assertTrue(await asyncio.to_thread(entered.wait, 8))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            self.assertTrue(group.cleanup_confirmed())
        try:
            asyncio.run(exercise())
            self.assertEqual(1, len(model.planner_calls))
        finally: release.set(); group.cancel()

    def test_shared_trusted_host_guard_orders_actual_lf_before_revoke_while_boolean_alone_cannot(self):
        from desktop_pet.agents.staged_input import StagedRunInput
        model = SeparateGoalModel(["No source citation was authorized."] * 8, judge_mode="optimistic")
        self.models.append(model)
        for guarded in (False, True):
            with self.subTest(guarded=guarded):
                host_lock = threading.RLock()
                active = [True]
                authority = ResearchAuthority(WorkspaceFiles(self.root), parent_id=str(uuid4()), conversation_id=str(uuid4()),
                    revision_id="guard-race", authorization_current=lambda: active[0],
                    authority_guard=(lambda: host_lock) if guarded else None)
                scope = authority.open_scope(["a.txt"])
                group = ResearchGroup(authority, python_executable=str(PYTHON), config={"base_url": model.base_url,
                    "model": "owned-guard-race", "api_key": "SYNTHETIC_RESEARCH_PRIVATE_KEY"}, deadline_seconds=10)
                self.groups.append(group)
                reached, commit_allowed, revoked = threading.Event(), threading.Event(), threading.Event()
                order, values = [], []
                original = StagedRunInput.commit
                def commit(writer):
                    if not reached.is_set():
                        reached.set()
                        if not commit_allowed.wait(5): raise AssertionError("Owned commit boundary not released")
                    order.append("commit")
                    return original(writer)
                def revoke():
                    with host_lock:
                        active[0] = False; order.append("revoke"); revoked.set()
                with patch.object(StagedRunInput, "commit", commit):
                    thread = threading.Thread(target=lambda: values.extend(group.run(scope, [{"scope": scope, "task": "Read the authorized source only."}])))
                    thread.start()
                    revoker = None
                    try:
                        self.assertTrue(reached.wait(8))
                        revoker = threading.Thread(target=revoke); revoker.start()
                        if guarded: self.assertFalse(revoked.wait(.05))
                        else: self.assertTrue(revoked.wait(2))
                        commit_allowed.set()
                        thread.join(6); revoker.join(2)
                        self.assertFalse(thread.is_alive() or revoker.is_alive())
                        self.assertEqual(["commit", "revoke"] if guarded else ["revoke", "commit"], order)
                        self.assertTrue(values[0]["worker_closed"])
                        self.assertFalse(values[0]["source_references_verified"])
                        self.assertEqual(0, len(authority._reads))
                    finally:
                        commit_allowed.set(); group.cancel(); thread.join(5)
                        if revoker is not None: revoker.join(2)

    def test_actual_popen_failure_remains_launch_unknown_without_a_fake_no_launch_receipt(self):
        from desktop_pet.agents import research_delegation as research
        invalid = self.root / "python-invalid.exe"
        invalid.write_bytes(b"This is an owned invalid executable, never a program.")
        group = ResearchGroup(self.authority, python_executable=str(invalid), config={"base_url": "http://127.0.0.1:12345/v1",
            "model": "unused-invalid-launch", "api_key": "SYNTHETIC_RESEARCH_PRIVATE_KEY"}, deadline_seconds=5)
        self.groups.append(group)
        isolated_owners = set()
        # Isolate the deliberately unrecoverable ownership ledger in this fault
        # fixture. The real Popen raises on invalid executable bytes; production
        # does not infer no launch or expose an API to clear this unknown state.
        with patch.object(research, "_GROUP_OWNERS", isolated_owners):
            values = group.run(self.scope, self.specs()[:1])
            self.assertEqual("RESEARCH_LAUNCH_UNKNOWN", values[0]["code"])
            self.assertFalse(values[0]["worker_closed"])
            self.assertFalse(group.cleanup_confirmed())
            self.assertIn(group, isolated_owners)
            self.assertTrue(group._runs[0].launch_attempted)
            self.assertIsNone(group._runs[0].process)
            with self.assertRaises(ResearchError): group.run(self.scope, self.specs()[:1])
        # Test-owned empty scratch can be erased because this fixture itself
        # supplied invalid bytes, without claiming a runtime closure receipt.
        for run in group._runs: run.state.cleanup()


if __name__ == "__main__": unittest.main()
